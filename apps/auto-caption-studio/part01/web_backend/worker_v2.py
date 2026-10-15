from __future__ import annotations

import json
from pathlib import Path
import subprocess
import time
import traceback
import uuid

from smart_scroll.output_validator import validate_video_file
from smart_scroll.audio_mixer import prepare_final_audio, cleanup_temporary_audio

from .clean_script_guard import normalize_clean_script, script_sha256, validate_clean_script
from .database import connect, now_iso
from .drive_uploader import ensure_drive_schema
from .drive_ingress import claim_next_ingress, process_ingress
from .job_states import CANCELLED, COMPLETED, FAILED
from .process_manager import (
    ManagedProcessCancelled,
    ManagedProcessError,
    run_cancellable_command,
    run_render_in_subprocess,
)
from .queue_store import (
    JobConflict,
    claim_next_job,
    finish_job,
    get_job,
    heartbeat_job,
    interrupt_job,
    is_cancel_requested,
    record_progress,
    release_job_to_queue,
    reconcile_stale_jobs,
)
from .settings_bridge import build_project_settings, load_settings
from .shared_pexels_library import apply_shared_pexels_settings, inject_server_pexels_key


AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"}


def _persist_effective_job_settings(job_id: str, settings: dict) -> None:
    if not isinstance(settings, dict):
        raise RuntimeError("Effective settings must be a JSON object.")
    payload = json.dumps(settings, ensure_ascii=False, separators=(",", ":"))
    with connect() as db:
        db.execute(
            "UPDATE jobs SET settings_json=?, updated_at=? WHERE id=?",
            (payload, now_iso(), job_id),
        )
        db.commit()


def _project(project_id: str):
    with connect() as db:
        return db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()


def _is_windows_path(value: object) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    return (
        len(text) >= 3 and text[1] == ":" and text[2] in ("\\", "/")
    ) or "\\" in text


def _sanitize_remote_settings(saved: dict, project_path: Path) -> dict:
    cleaned = dict(saved or {})
    # Fixed-size compression was removed. Scrub the legacy field from old jobs.
    cleaned.pop("post_render_compress_target_mb", None)
    for key in (
        "audio_path", "render_audio_path", "output_path", "output_folder",
        "background_image_path", "background_video_path",
        "foreground_image_path", "foreground_folder_path",
        "music_folder_path", "music_assigned_path",
        "piper_model_path", "pexels_library_folder",
    ):
        if _is_windows_path(cleaned.get(key)):
            cleaned[key] = ""

    for key in ("pexels_cache_paths", "background_video_paths", "music_paths"):
        values = cleaned.get(key, [])
        if not isinstance(values, list):
            values = []
        cleaned[key] = [
            str(value)
            for value in values
            if not _is_windows_path(value) and Path(str(value)).exists()
        ]

    metadata = cleaned.get("metadata", {})
    metadata = metadata if isinstance(metadata, dict) else {}
    input_dir = project_path / "input"

    font_upload_name = Path(str(metadata.get("custom_font_upload_name") or "")).name
    if font_upload_name:
        font_path = input_dir / font_upload_name
        if not font_path.is_file() or font_path.suffix.lower() not in {".ttf", ".otf"}:
            raise RuntimeError(f"Uploaded custom font is missing on VPS: {font_upload_name}")
        metadata["custom_font_vps_path"] = str(font_path)

    foreground_upload_name = Path(
        str(metadata.get("foreground_upload_name") or "")
    ).name
    if foreground_upload_name:
        foreground_path = input_dir / foreground_upload_name
        if not foreground_path.is_file():
            raise RuntimeError(
                f"Uploaded foreground image is missing on VPS: {foreground_upload_name}"
            )
        cleaned["foreground_image_path"] = str(foreground_path)
        cleaned["foreground_enabled"] = True
        cleaned["foreground_use_folder_rotation"] = False
        cleaned["foreground_folder_path"] = ""
        metadata["foreground_vps_path"] = str(foreground_path)

    cleaned["metadata"] = metadata
    if not cleaned.get("pexels_cache_paths"):
        cleaned["auto_prepare_backgrounds"] = True
        if cleaned.get("pexels_enabled"):
            cleaned["pexels_download_enabled"] = True
            cleaned["pexels_use_library"] = True
    return apply_shared_pexels_settings(cleaned)


def _render_stage(message: str) -> str:
    text = message.lower()
    if "compress" in text:
        return "compressing"
    if "whisper" in text or "caption" in text:
        return "transcribing"
    if "background" in text or "pexels" in text or "prepar" in text:
        return "preparing"
    if "frame " in text or "render" in text or "ffmpeg" in text:
        return "rendering"
    return "rendering"


def _append_log(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text.rstrip() + "\n")


def process_job(job: dict, worker_id: str, shutdown_requested=lambda: False) -> dict:
    job_id = str(job["id"])
    log_path = Path(str(job.get("log_path") or ""))
    owned_mix_path: str | None = None

    def cancelled() -> bool:
        return is_cancel_requested(job_id) or bool(shutdown_requested())

    def heartbeat(stage: str | None = None) -> None:
        if not heartbeat_job(job_id, worker_id, stage=stage):
            raise JobConflict("Worker lease was lost")

    def set_progress(percent: int, stage: str, message: str, stage_progress: int | None = None) -> None:
        if cancelled():
            raise ManagedProcessCancelled("JOB_CANCELLED_BY_USER")
        record_progress(
            job_id,
            worker_id,
            progress=percent,
            stage=stage,
            message=message,
            stage_progress=stage_progress,
        )
        _append_log(
            log_path,
            f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {percent}% | {stage} | {message}",
        )

    try:
        ensure_drive_schema()
        heartbeat("preparing")
        project = _project(str(job["project_id"]))
        if project is None:
            raise RuntimeError("Project not found")
        project_path = Path(project["project_path"])
        input_dir = project_path / "input"
        if not input_dir.is_dir():
            raise RuntimeError("Project input directory is missing")

        audio_files = sorted(
            path for path in input_dir.iterdir()
            if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS
        )
        if not audio_files:
            raise RuntimeError("No uploaded audio file found")
        script_files = sorted(path for path in input_dir.glob("*.txt") if path.is_file())
        if not script_files:
            raise RuntimeError("Clean script TXT is missing")

        clean_script_text = normalize_clean_script(
            script_files[0].read_text(encoding="utf-8", errors="replace")
        )
        validate_clean_script(clean_script_text)
        source_audio = audio_files[0]
        mode = str(job["settings"].get("_render_mode", "full"))
        output_path = Path(str(job["output_path"]))
        preview_path = Path(str(job["preview_path"]))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        set_progress(3, "preparing", "Validating uploaded project inputs", 20)
        duration_result = run_cancellable_command(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(source_audio),
            ],
            cancel_requested=cancelled,
            heartbeat=lambda: heartbeat("preparing"),
            timeout=120,
        )
        if duration_result.returncode != 0:
            raise RuntimeError(
                "FFprobe duration failed: " + (duration_result.stderr or "")[-1200:]
            )
        duration = float((duration_result.stdout or "").strip())

        render_audio = source_audio
        render_target = output_path
        if mode == "preview":
            try:
                preview_seconds = float(job["settings"].get("_preview_duration_seconds", 15) or 15)
            except Exception:
                preview_seconds = 15.0
            preview_seconds = max(1.0, preview_seconds)
            render_audio = project_path / "temp" / f"preview_audio_{job_id}.wav"
            render_audio.parent.mkdir(parents=True, exist_ok=True)
            preview_audio_result = run_cancellable_command(
                [
                    "ffmpeg", "-y", "-v", "error", "-i", str(source_audio),
                    "-t", f"{preview_seconds:.3f}", "-c:a", "pcm_s16le", str(render_audio),
                ],
                cancel_requested=cancelled,
                heartbeat=lambda: heartbeat("preparing"),
                timeout=300,
            )
            if preview_audio_result.returncode != 0:
                raise RuntimeError(
                    "Preview audio preparation failed: "
                    + (preview_audio_result.stderr or "")[-1200:]
                )
            render_target = preview_path
            duration = min(duration, preview_seconds)

        saved = load_settings(project_path)
        saved.update(
            {key: value for key, value in job["settings"].items() if not key.startswith("_")}
        )
        # These server-owned flags must survive a crash during rendering so a
        # recovered Phase 3 job still hands publication to the dedicated Drive
        # uploader instead of uploading inside the render stage.
        saved.update({
            key: value for key, value in job["settings"].items()
            if key == "_render_mode" or key.startswith("_pipeline_")
        })
        saved = _sanitize_remote_settings(saved, project_path)
        _persist_effective_job_settings(job_id, saved)
        render_saved = inject_server_pexels_key(saved)
        settings = build_project_settings(
            project_name=str(project["name"]),
            job_id=job_id,
            text=clean_script_text,
            audio_path=str(render_audio),
            output_path=str(render_target),
            output_folder=str(render_target.parent),
            duration_seconds=duration,
            saved=render_saved,
        )

        metadata = dict(saved.get("metadata") or {})
        if bool(metadata.get("web_flow_v1")):
            # New Web Flow reuses the proven ACS mixer for voice processing and
            # optional background music. Legacy VPS jobs keep their old path.
            def audio_progress(percent: int, message: str) -> None:
                overall = min(9, 5 + int(max(0, min(100, percent)) * 0.04))
                set_progress(overall, "preparing_audio", message, percent)

            mix_result = prepare_final_audio(
                settings,
                progress_callback=audio_progress,
                cancel_callback=cancelled,
                max_duration=duration,
                output_tag="preview_mix" if mode == "preview" else "final_mix",
            )
            if mix_result is not None and mix_result.path:
                owned_mix_path = str(mix_result.path)
                settings.render_audio_path = owned_mix_path

        _append_log(
            log_path,
            "\n".join(
                [
                    "SCRIPT_SOURCE: uploaded_clean_txt",
                    f"SCRIPT_CHARS: {len(clean_script_text)}",
                    f"SCRIPT_SHA256: {script_sha256(clean_script_text)}",
                    f"CUSTOM_FONT_UPLOAD: {metadata.get('custom_font_upload_name', '')}",
                    f"CUSTOM_FONT_VPS_PATH: {metadata.get('custom_font_vps_path', '')}",
                    f"FOREGROUND_UPLOAD: {metadata.get('foreground_upload_name', '')}",
                    f"FOREGROUND_VPS_PATH: {metadata.get('foreground_vps_path', '')}",
                ]
            ),
        )

        set_progress(8, "preparing", "Starting isolated render process", 90)

        def render_progress(percent: int, message: str) -> None:
            stage = _render_stage(message)
            overall = min(88, 10 + int(max(0, min(100, percent)) * 0.78))
            set_progress(overall, stage, message, percent)

        run_render_in_subprocess(
            settings,
            progress=render_progress,
            cancel_requested=cancelled,
            heartbeat=lambda: heartbeat(),
        )
        if owned_mix_path:
            cleanup_temporary_audio(owned_mix_path)
            owned_mix_path = None

        set_progress(89, "validating_render", "Validating rendered MP4", 10)
        if cancelled():
            raise ManagedProcessCancelled("JOB_CANCELLED_BY_USER")
        validation = validate_video_file(str(render_target))
        heartbeat("validating_render")
        _append_log(log_path, f"RENDER_VALIDATION: {validation}")

        if mode == "preview":
            return finish_job(job_id, worker_id, COMPLETED, message="Preview completed")

        return release_job_to_queue(
            job_id, worker_id, pipeline_stage="ready_compress", progress=89,
            message="Render validated; waiting for compression",
        )

    except BaseException as exc:
        if owned_mix_path:
            cleanup_temporary_audio(owned_mix_path)
            owned_mix_path = None
        if shutdown_requested():
            reason = "Worker shutdown interrupted the active attempt; startup recovery may requeue it"
            _append_log(log_path, reason)
            try:
                return interrupt_job(job_id, worker_id, reason)
            except JobConflict:
                return get_job(job_id)
        was_cancelled = isinstance(exc, ManagedProcessCancelled) or is_cancel_requested(job_id)
        status = CANCELLED if was_cancelled else FAILED
        message = "Cancelled" if was_cancelled else "Failed"
        error_text = "" if was_cancelled else f"{type(exc).__name__}: {exc}"
        if not was_cancelled:
            _append_log(log_path, traceback.format_exc())
        try:
            return finish_job(
                job_id,
                worker_id,
                status,
                message=message,
                error_text=error_text,
            )
        except JobConflict:
            _append_log(
                log_path,
                "ATTEMPT_FINALIZATION_SKIPPED: worker lease no longer owns job; recovery evidence remains in database",
            )
            return get_job(job_id)


def run_once(worker_id: str | None = None, shutdown_requested=lambda: False) -> dict:
    active_worker_id = worker_id or f"worker-{uuid.uuid4().hex}"
    # This is deliberately periodic, not startup-only: if services restart before
    # an old lease expires, a later idle iteration still supplies recovery evidence
    # and requeues the abandoned attempt.
    reconcile_stale_jobs(auto_requeue=True)
    ingress = claim_next_ingress(active_worker_id)
    if ingress is not None:
        return process_ingress(ingress, active_worker_id)
    job = claim_next_job(active_worker_id)
    if job is None:
        return {"status": "idle"}
    return process_job(job, active_worker_id, shutdown_requested=shutdown_requested)

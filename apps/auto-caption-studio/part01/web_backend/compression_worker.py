from __future__ import annotations

from pathlib import Path
import traceback

from smart_scroll.compressor_engine import CompressionCancelled, compress_video_file
from smart_scroll.output_validator import validate_video_file

from .database import connect, now_iso
from .job_states import CANCELLED, COMPLETED, FAILED
from .process_manager import ManagedProcessCancelled, run_cancellable_command
from .queue_store import (
    JobConflict,
    finish_job,
    get_job,
    heartbeat_job,
    interrupt_job,
    is_cancel_requested,
    record_progress,
    release_job_to_queue,
)


def _append_log(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text.rstrip() + "\n")


def compress_job(job: dict, worker_id: str, shutdown_requested=lambda: False) -> dict:
    job_id = str(job["id"])
    output_path = Path(str(job.get("output_path") or ""))
    preview_path = Path(str(job.get("preview_path") or ""))
    log_path = Path(str(job.get("log_path") or ""))
    settings = dict(job.get("settings") or {})

    def cancelled() -> bool:
        return is_cancel_requested(job_id) or bool(shutdown_requested())

    def heartbeat(stage: str = "compressing") -> None:
        if not heartbeat_job(job_id, worker_id, stage=stage):
            raise JobConflict("Compressor lease was lost")

    def progress(percent: int, message: str) -> None:
        if cancelled():
            raise ManagedProcessCancelled("JOB_CANCELLED_BY_USER")
        overall = 89 + int(max(0, min(100, percent)) * 0.04)
        record_progress(
            job_id, worker_id, progress=overall, stage="compressing",
            stage_progress=percent, message=message,
        )
        _append_log(log_path, f"COMPRESSION {percent}% | {message}")

    try:
        if not output_path.is_file():
            raise RuntimeError("Rendered MP4 is missing before compression")
        heartbeat()
        if bool(settings.get("post_render_compress_enabled", False)):
            quality = str(settings.get("post_render_compress_quality") or "Balanced Fast")
            encoder = str(settings.get("post_render_compress_encoder") or "CPU Fast (Reliable)")
            progress(1, f"Compression started: {quality} / {encoder}")
            try:
                result = compress_video_file(
                    output_path,
                    quality=quality,
                    encoder=encoder,
                    overwrite_only_smaller=bool(
                        settings.get("post_render_compress_overwrite_only_smaller", True)
                    ),
                    keep_failed_temp=bool(
                        settings.get("post_render_compress_keep_failed_temp", False)
                    ),
                    progress_callback=progress,
                    log_callback=lambda message: _append_log(log_path, message),
                    cancel_callback=cancelled,
                )
            except CompressionCancelled as exc:
                raise ManagedProcessCancelled("JOB_CANCELLED_BY_USER") from exc
            _append_log(log_path, f"COMPRESSION: {result.message}")
        else:
            progress(100, "Compression disabled; rendered file passed through")

        record_progress(
            job_id, worker_id, progress=93, stage="validating",
            stage_progress=20, message="Validating compressed MP4",
        )
        validation = validate_video_file(str(output_path))
        heartbeat("validating")
        _append_log(log_path, f"VALIDATION: {validation}")

        preview_path.parent.mkdir(parents=True, exist_ok=True)
        record_progress(
            job_id, worker_id, progress=94, stage="generating_preview",
            stage_progress=10, message="Generating output preview",
        )
        preview_result = run_cancellable_command(
            [
                "ffmpeg", "-y", "-v", "error", "-i", str(output_path),
                "-t", "15", "-c:v", "libx264", "-preset", "ultrafast",
                "-crf", "29", "-pix_fmt", "yuv420p", "-c:a", "aac",
                "-b:a", "96k", "-movflags", "+faststart", str(preview_path),
            ],
            cancel_requested=cancelled,
            heartbeat=lambda: heartbeat("generating_preview"),
            timeout=900,
        )
        if preview_result.returncode != 0:
            raise RuntimeError(
                "Preview generation failed: " + (preview_result.stderr or "")[-1200:]
            )
        metadata = settings.get("metadata") if isinstance(settings.get("metadata"), dict) else {}
        is_web_flow = bool(metadata.get("web_flow_v1"))
        drive_enabled = bool(metadata.get("web_drive_upload_enabled", True))
        with connect() as db:
            if is_web_flow and not drive_enabled:
                db.execute(
                    """UPDATE jobs SET render_verified=1,pipeline_stage='completed',
                       drive_status='not_required',drive_verified=0,local_cleanup_status='retained',updated_at=?
                       WHERE id=?""",
                    (now_iso(), job_id),
                )
            else:
                db.execute(
                    "UPDATE jobs SET render_verified=1,updated_at=? WHERE id=?",
                    (now_iso(), job_id),
                )
            db.commit()
        if is_web_flow and not drive_enabled:
            return finish_job(
                job_id, worker_id, COMPLETED,
                message="Completed locally; Drive upload disabled by New Web Flow output settings",
            )
        return release_job_to_queue(
            job_id, worker_id, pipeline_stage="ready_upload", progress=95,
            message="Compression and validation complete; waiting for Drive upload",
        )
    except BaseException as exc:
        if shutdown_requested():
            reason = "Compressor shutdown interrupted the active stage; recovery may requeue it"
            _append_log(log_path, reason)
            try:
                return interrupt_job(job_id, worker_id, reason)
            except JobConflict:
                return get_job(job_id)
        was_cancelled = isinstance(exc, ManagedProcessCancelled) or is_cancel_requested(job_id)
        if not was_cancelled:
            _append_log(log_path, traceback.format_exc())
        try:
            return finish_job(
                job_id, worker_id, CANCELLED if was_cancelled else FAILED,
                message="Cancelled" if was_cancelled else "Compression failed",
                error_text="" if was_cancelled else f"{type(exc).__name__}: {exc}",
            )
        except JobConflict:
            return get_job(job_id)

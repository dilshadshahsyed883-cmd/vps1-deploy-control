from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

import requests

import master_controller as mc
import app as scene_app

WORKER = "scene_builder"
STATE_DIR = Path(os.environ.get("AUTOFLOW_SCENE_STATE", "/var/lib/autoflow-scene-worker"))
OUTPUT_DIR = STATE_DIR / "output"
STATUS_PATH = STATE_DIR / "status.json"
IDLE_SECONDS = float(os.environ.get("AUTOFLOW_SCENE_IDLE_SECONDS", "3"))
POLL_SECONDS = float(os.environ.get("AUTOFLOW_SCENE_POLL_SECONDS", "5"))
AUDIO_TIMEOUT = int(os.environ.get("AUTOFLOW_AUDIO_TIMEOUT_SECONDS", "3600"))
SCENE_TIMEOUT = int(os.environ.get("AUTOFLOW_SCENE_TIMEOUT_SECONDS", "43200"))


def now() -> str:
    return mc._now()


def write_status(state: str, **extra: Any) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"worker": WORKER, "state": state, "updated_at": now(), **extra}
    tmp = STATUS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(STATUS_PATH)


def recover_owned_running() -> int:
    # Scene submissions are content-fingerprinted, so replay after service restart is idempotent.
    with mc._db() as conn:
        changed = conn.execute(
            "UPDATE trigger_jobs SET status='QUEUED',locked_at='',updated_at=? WHERE worker=? AND status='RUNNING'",
            (now(), WORKER),
        ).rowcount
    return int(changed)


def claim_job() -> dict[str, Any] | None:
    with mc._db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """SELECT j.* FROM trigger_jobs j JOIN projects p ON p.id=j.project_id
               WHERE j.worker=? AND j.status='QUEUED' AND p.enabled=1 ORDER BY j.id LIMIT 1""",
            (WORKER,),
        ).fetchone()
        if not row:
            conn.commit()
            return None
        stamp = now()
        changed = conn.execute(
            "UPDATE trigger_jobs SET status='RUNNING',attempt_count=attempt_count+1,locked_at=?,updated_at=? WHERE id=? AND status='QUEUED'",
            (stamp, stamp, row["id"]),
        ).rowcount
        if not changed:
            conn.commit()
            return None
        job = conn.execute("SELECT * FROM trigger_jobs WHERE id=?", (row["id"],)).fetchone()
        project_row = mc._project_exists(conn, int(job["project_id"]))
        project = mc._project_dict(conn, project_row)
        conn.commit()
    data = dict(job)
    data["payload"] = mc._json_load(data.pop("payload_json", "{}"), {})
    data["project"] = project
    return data


def finish_job(job: dict[str, Any], success: bool, result: dict[str, Any] | None = None, error: str = "") -> None:
    result = result if isinstance(result, dict) else {}
    with mc._db() as conn:
        row = conn.execute("SELECT * FROM trigger_jobs WHERE id=? AND worker=?", (int(job["id"]), WORKER)).fetchone()
        if not row:
            return
        status = "COMPLETED" if success else "FAILED"
        conn.execute(
            "UPDATE trigger_jobs SET status=?,result_json=?,error_text=?,updated_at=? WHERE id=?",
            (status, json.dumps(result, ensure_ascii=False), str(error)[:4000], now(), int(job["id"])),
        )
        if success:
            next_event = mc.ACTION_SUCCESS_EVENT.get(row["action"])
            if next_event:
                project = mc._project_exists(conn, int(row["project_id"]))
                if str(project["flow_mode"] or "full_auto") == "full_auto":
                    merged = dict(mc._json_load(row["payload_json"], {})); merged.update(result)
                    mc._enqueue_event(conn, int(row["project_id"]), row["permanent_id"], next_event, merged)


def first_text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def scene_text(job: dict[str, Any]) -> str:
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    scene_input = payload.get("scene_builder_input") if isinstance(payload.get("scene_builder_input"), dict) else {}
    text = first_text(payload, "scene_plan_text", "scene_text", "structured_scene_plan", "script_text")
    if not text:
        text = first_text(scene_input, "scene_plan_text", "scene_text", "structured_scene_plan")
    if text:
        return text
    for candidate in (
        first_text(payload, "scene_plan_path", "scene_text_path", "script_path"),
        first_text(scene_input, "scene_plan_path", "scene_text_path"),
    ):
        if candidate:
            path = Path(candidate)
            if path.is_file():
                return path.read_text(encoding="utf-8-sig").strip()
    raise RuntimeError("Scene plan text/path is missing. ChatGPT worker must return scene_plan_text or scene_plan_path before Scene Builder can run.")


def resolve_audio_source(job: dict[str, Any]) -> Path:
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    scene_input = payload.get("scene_builder_input") if isinstance(payload.get("scene_builder_input"), dict) else {}
    candidates = [
        first_text(payload, "processed_audio_path", "audio_path", "voice_path", "narration_path"),
        first_text(scene_input, "audio_source"),
    ]
    for raw in candidates:
        if not raw:
            continue
        path = Path(raw)
        if path.is_file():
            return path
    raise RuntimeError("Local Algrow audio path is missing. Algrow worker must return audio_path before Scene Builder can run.")


# SCENE_INTERNAL_AUDIO_AUTOFLOW_HANDOFF_V1
# Auto Flow no longer creates a localhost standalone Audio Engine job. It packages
# the original Algrow narration plus a worker-owned DSP contract. The Scene Builder
# worker runs the internal voice engine before Whisper/timing, then owns Music/SFX.
def _contract_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", "disabled", ""}


def internal_audio_contract(job: dict[str, Any]) -> dict[str, Any]:
    project = job.get("project") if isinstance(job.get("project"), dict) else {}
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    scene_input = payload.get("scene_builder_input") if isinstance(payload.get("scene_builder_input"), dict) else {}
    contract = scene_input.get("audio_engine") if isinstance(scene_input.get("audio_engine"), dict) else {}
    enabled = _contract_bool(contract.get("enabled", project.get("audio_engine_enabled", True)), True)
    preset = str(contract.get("preset") or project.get("audio_engine_preset") or "documentary").strip() or "documentary"
    if preset not in {"documentary", "clean", "natural", "deep", "youtube"}:
        preset = "documentary"
    return {
        "enabled": bool(enabled),
        "preset": preset,
        "architecture": "scene_builder_worker_internal",
        "voice_only": True,
        "timeline_owner": "scene_builder",
        "music_owner": "scene_builder",
        "source_app": "autoflow_scene_builder",
    }


def package_and_submit(job: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    text = scene_text(job)
    source_audio = resolve_audio_source(job)
    project = job.get("project") if isinstance(job.get("project"), dict) else {}
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    scene_input = payload.get("scene_builder_input") if isinstance(payload.get("scene_builder_input"), dict) else {}
    preset_id = str(project.get("scene_builder_preset") or "").strip()
    project_name = f"AF-{job['permanent_id']}"
    audio_contract = internal_audio_contract(job)
    channel_name = str(
        scene_input.get("channel_name")
        or payload.get("channel_name")
        or project.get("scene_builder_channel")
        or project.get("channel_name")
        or "General"
    ).strip() or "General"
    with tempfile.TemporaryDirectory(prefix="autoflow_scene_") as td:
        temp_dir = Path(td)
        scene_app.validate_narration_media(source_audio)
        resolved_preset, profile = scene_app.resolve_scene_profile(preset_id)
        profile = dict(profile)
        profile["_internal_audio_engine"] = dict(audio_contract)
        package_path = temp_dir / "project.zip"
        scene_app.build_project_package(
            project_name,
            text,
            source_audio,
            resolved_preset,
            profile,
            channel_name,
            package_path,
        )
        with zipfile.ZipFile(package_path, "r") as package:
            packaged_project = json.loads(package.read("project.json").decode("utf-8"))
        packaged_contract = ((packaged_project.get("metadata") or {}).get("internal_audio_engine") or {})
        if bool(packaged_contract.get("enabled")) != bool(audio_contract.get("enabled")) or str(packaged_contract.get("preset") or "") != str(audio_contract.get("preset") or ""):
            raise RuntimeError("Scene Builder package did not preserve Auto Flow internal Audio Engine contract")
        fingerprint_payload = {
            "owner_username": "autoflow",
            "permanent_id": str(job["permanent_id"]),
            "project": packaged_project,
            "script_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "audio_sha256": hashlib.sha256(source_audio.read_bytes()).hexdigest(),
            "submission_version": "autoflow-scene-worker-v2-internal-audio",
        }
        fingerprint = hashlib.sha256(json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
        with package_path.open("rb") as handle:
            response = scene_app.worker_request(
                "POST", "/jobs",
                params={"client_job_key": fingerprint, "submission_version": "autoflow-scene-worker-v2-internal-audio"},
                files={"file": ("project.zip", handle, "application/zip")},
                timeout=3600,
            ).json()
        scene_id = str(response.get("id") or response.get("job_id") or "")
        if not scene_id:
            raise RuntimeError("Scene Builder accepted package but returned no job ID")
        audio_meta = {
            "enabled": bool(audio_contract.get("enabled")),
            "preset": str(audio_contract.get("preset") or "documentary"),
            "backend": "scene_builder_worker_internal",
            "preprocessed": False,
        }
        return scene_id, {"fingerprint": fingerprint, "preset": resolved_preset, "audio_engine": audio_meta, "submit": response}



def wait_scene(job: dict[str, Any], scene_id: str, submit_meta: dict[str, Any]) -> dict[str, Any]:
    deadline = time.monotonic() + SCENE_TIMEOUT
    last: dict[str, Any] = {}
    while True:
        raw = scene_app.worker_request("GET", f"/jobs/{scene_id}", timeout=60).json()
        item = raw.get("job", raw) if isinstance(raw, dict) else {}
        last = item if isinstance(item, dict) else {}
        status = str(last.get("status") or "").lower()
        write_status("scene_builder", controller_job_id=job["id"], permanent_id=job["permanent_id"], scene_job_id=scene_id, scene_status=status)
        if status in {"failed", "cancelled"}:
            raise RuntimeError(f"Scene Builder job {scene_id} {status}: {last.get('error') or last.get('error_text') or ''}")
        if status in {"completed", "cleaned", "delivered"}:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Scene Builder job {scene_id} timed out at status={status}")
        time.sleep(POLL_SECONDS)

    result: dict[str, Any] = {
        "scene_builder_job_id": scene_id,
        "scene_builder_status": str(last.get("status") or ""),
        "scene_builder_submit": submit_meta,
    }
    key_map = {
        "drive_video_url": ("drive_video_url", "drive_url", "uploaded_url", "remote_url"),
        "video_url": ("video_url", "output_url", "final_url"),
        "video_path": ("video_path", "output_path", "final_path"),
    }
    for out_key, keys in key_map.items():
        for key in keys:
            value = last.get(key)
            if isinstance(value, str) and value.strip():
                result[out_key] = value.strip(); break

    if not any(result.get(k) for k in ("drive_video_url", "video_url", "video_path")):
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in str(job["permanent_id"]))[:120] or f"job-{job['id']}"
        target = OUTPUT_DIR / f"{safe}-{scene_id}.mp4"
        try:
            response = scene_app.worker_request("GET", f"/jobs/{scene_id}/download", timeout=3600, stream=True)
            with target.open("wb") as handle:
                for chunk in response.iter_content(1024 * 1024):
                    if chunk: handle.write(chunk)
            if target.is_file() and target.stat().st_size > 0:
                result["video_path"] = str(target)
        except Exception as exc:
            result["download_warning"] = str(exc)[:500]
    if not any(result.get(k) for k in ("drive_video_url", "video_url", "video_path")):
        raise RuntimeError(f"Scene Builder completed job {scene_id} but no final video source was retrievable")
    return result


def process(job: dict[str, Any]) -> dict[str, Any]:
    write_status("preparing", controller_job_id=job["id"], permanent_id=job["permanent_id"])
    scene_id, submit_meta = package_and_submit(job)
    return wait_scene(job, scene_id, submit_meta)


def main() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    recovered = recover_owned_running()
    write_status("idle", recovered_running=recovered)
    while True:
        job = claim_job()
        if not job:
            write_status("idle")
            time.sleep(IDLE_SECONDS)
            continue
        try:
            result = process(job)
            finish_job(job, True, result=result)
            write_status("completed", controller_job_id=job["id"], permanent_id=job["permanent_id"], result=result)
        except Exception as exc:
            finish_job(job, False, error=str(exc))
            write_status("failed", controller_job_id=job.get("id"), permanent_id=job.get("permanent_id"), error=str(exc)[:1000])
            time.sleep(2)


if __name__ == "__main__":
    main()

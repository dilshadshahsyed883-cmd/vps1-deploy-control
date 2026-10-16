from __future__ import annotations

import json
import logging
from pathlib import Path
import shutil
import signal
import subprocess
import threading
import time
import uuid

from smart_scroll.caption_sync import build_caption_segments, caption_generation_signature
from .clean_script_guard import normalize_clean_script, validate_clean_script

from .database import connect, initialize_storage, now_iso
from .config import projects_root
from .drive_ingress import claim_next_ingress, process_ingress
from .drive_uploader import mark_drive_failure, upload_completed_job
from .job_states import COMPLETED, FAILED, QUEUED, RUNNING
from .queue_store import (
    finish_job, get_job, heartbeat_job, is_cancel_requested, record_progress,
    interrupt_job, reconcile_stale_jobs, transition_job_in_db, _lease_expiry,
)


from .web_flow_runtime import claim_allowed as web_flow_claim_allowed, scene_builder_busy as web_flow_scene_builder_busy


ACTIVE_BATCH_KEY = "active_batch_id"
LAST_CATEGORY_KEY = "last_completed_category"


def _state(db, name: str) -> str:
    row = db.execute("SELECT value FROM scheduler_state WHERE name=?", (name,)).fetchone()
    return str(row["value"] if row else "")


def _set_state(db, name: str, value: str) -> None:
    db.execute(
        """INSERT INTO scheduler_state(name,value,updated_at) VALUES (?,?,?)
           ON CONFLICT(name) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
        (name, value, now_iso()),
    )


def _refresh_active_batch(db) -> str:
    """Return the only batch eligible for processing, rotating categories batch-wise."""
    active_id = _state(db, ACTIVE_BATCH_KEY)
    if active_id:
        batch = db.execute("SELECT * FROM upload_batches WHERE id=?", (active_id,)).fetchone()
        if batch is not None:
            if str(batch["control_state"] or "active") == "paused":
                _set_state(db, ACTIVE_BATCH_KEY, "")
                active_id = ""
            else:
                unfinished = db.execute(
                    """SELECT COUNT(*) FROM jobs WHERE batch_id=?
                       AND status NOT IN ('completed','failed','cancelled','interrupted')""", (active_id,)
                ).fetchone()[0]
                complete_upload = int(batch["staged_count"] or 0) >= int(batch["expected_count"] or 0)
                if unfinished:
                    return active_id
                if not complete_upload:
                    # A legacy/abandoned partial batch must never block every newer
                    # accepted batch after its staged work has reached a terminal state.
                    ts = now_iso()
                    db.execute(
                        "UPDATE upload_batches SET status='failed',completed_at=?,updated_at=? WHERE id=?",
                        (ts, ts, active_id),
                    )
                    _set_state(db, ACTIVE_BATCH_KEY, "")
                    active_id = ""
                else:
                    ts = now_iso()
                    db.execute(
                        "UPDATE upload_batches SET status='completed',completed_at=?,updated_at=? WHERE id=?",
                        (ts, ts, active_id),
                    )
                    _set_state(db, LAST_CATEGORY_KEY, str(batch["category"] or "Uncategorized"))
                    _set_state(db, ACTIVE_BATCH_KEY, "")
        else:
            _set_state(db, ACTIVE_BATCH_KEY, "")

    last_category = _state(db, LAST_CATEGORY_KEY)
    candidates = db.execute(
        """SELECT b.* FROM upload_batches b
           WHERE b.status IN ('uploading','accepted','processing')
             AND COALESCE(b.control_state,'active')='active'
             AND EXISTS (SELECT 1 FROM jobs j WHERE j.batch_id=b.id AND j.status='queued')
           ORDER BY b.created_at,b.id"""
    ).fetchall()
    if not candidates:
        return ""
    selected = next(
        (row for row in candidates if str(row["category"] or "") != last_category),
        candidates[0],
    )
    batch_id = str(selected["id"])
    ts = now_iso()
    db.execute(
        """UPDATE upload_batches SET status='processing',
           processing_started_at=COALESCE(processing_started_at,?),updated_at=? WHERE id=?""",
        (ts, ts, batch_id),
    )
    _set_state(db, ACTIVE_BATCH_KEY, batch_id)
    return batch_id


def claim_stage(stage: str, worker_id: str) -> dict | None:
    initialize_storage()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        control_name = {
            "ready_transcribe": "transcribe",
            "ready_render": "render",
            "ready_compress": "compress",
            "ready_upload": "upload",
            "ready_publish": "upload",
        }.get(stage, stage)
        control = db.execute("SELECT paused FROM acs_pipeline_controls WHERE name=?", (control_name,)).fetchone()
        if control is not None and int(control["paused"] or 0):
            db.commit()
            return None
        batch_id = _refresh_active_batch(db)
        params: list[object] = [QUEUED, stage]
        batch_clause = ""
        if batch_id:
            batch_clause = "AND j.batch_id=?"
            params.append(batch_id)
        else:
            batch_clause = "AND COALESCE(j.batch_id,'')=''"
        candidates = db.execute(
            f"""SELECT j.* FROM jobs j
                LEFT JOIN batch_items bi ON bi.job_id=j.id
                WHERE j.status=? AND j.pipeline_stage=? AND j.cancel_requested=0 {batch_clause}
                ORDER BY COALESCE(bi.item_order,2147483647),j.queue_sequence,j.created_at,j.id LIMIT 100""",
            params,
        ).fetchall()
        sb_busy = web_flow_scene_builder_busy()
        row = next((candidate for candidate in candidates if web_flow_claim_allowed(db, candidate, sb_busy=sb_busy)), None)
        if row is None:
            db.commit()
            return None
        transition_job_in_db(
            db, str(row["id"]), RUNNING, stage=stage, progress=int(row["progress"] or 1),
            message=f"{stage.replace('_',' ').title()} worker claimed project",
            worker_id=worker_id, lease_expires_at=_lease_expiry(),
            event_type="pipeline_stage_claimed",
            detail={"pipeline_stage": stage, "worker_id": worker_id},
        )
        db.commit()
        job_id = str(row["id"])
    return get_job(job_id)


def release_stage(job_id: str, worker_id: str, next_stage: str, progress: int, message: str) -> dict:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        transition_job_in_db(
            db, job_id, QUEUED, stage=next_stage, progress=progress, message=message,
            worker_id="", event_type="pipeline_stage_completed",
            detail={"next_stage": next_stage},
        )
        db.execute(
            """UPDATE jobs SET pipeline_stage=?,worker_id='',lease_expires_at=NULL,
               last_heartbeat_at=NULL,updated_at=? WHERE id=?""",
            (next_stage, now_iso(), job_id),
        )
        db.commit()
    return get_job(job_id)


def _project_files(job: dict) -> tuple[Path, Path, Path]:
    with connect() as db:
        project = db.execute("SELECT * FROM projects WHERE id=?", (job["project_id"],)).fetchone()
    if project is None:
        raise RuntimeError("Project not found")
    root = Path(project["project_path"])
    input_dir = root / "input"
    audio = next((p for p in sorted(input_dir.iterdir()) if p.suffix.lower() in {'.wav','.mp3','.m4a','.aac','.flac','.ogg'}), None)
    scripts = sorted(input_dir.glob("*.txt"))
    if audio is None or not scripts:
        raise RuntimeError("Transcription input audio or script is missing")
    return root, audio, scripts[0]


def transcribe_job(job: dict, worker_id: str) -> dict:
    job_id = str(job["id"])
    try:
        root, audio, script = _project_files(job)
        text = normalize_clean_script(script.read_text(encoding="utf-8", errors="replace"))
        validate_clean_script(text)
        probe = subprocess.run(
            ["ffprobe","-v","error","-show_entries","format=duration","-of","default=noprint_wrappers=1:nokey=1",str(audio)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=120,
        )
        if probe.returncode:
            raise RuntimeError("FFprobe failed before transcription: " + probe.stderr[-1000:])
        duration = float(probe.stdout.strip())
        settings = dict(job.get("settings") or {})
        metadata = dict(settings.get("metadata") or {})

        if not bool(settings.get("caption_mode", True)):
            metadata.update({"caption_segments": [], "caption_audio_duration": duration,
                             "caption_sync_engine": "skipped-scrolling-text", "transcription_skipped": True})
            settings["metadata"] = metadata
            settings["_pipeline_v3"] = True
            settings["_pipeline_defer_publish"] = True
            artifact = root / "artifacts" / f"{job_id}_transcription_skipped.json"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
            with connect() as db:
                db.execute("UPDATE jobs SET settings_json=?,transcription_path=?,updated_at=? WHERE id=?",
                           (json.dumps(settings, ensure_ascii=False, separators=(",", ":")), str(artifact), now_iso(), job_id))
                db.commit()
            return release_stage(job_id, worker_id, "ready_render", 35, "Scrolling text mode; transcription skipped")

        def cancelled() -> bool:
            return is_cancel_requested(job_id)

        def progress(value: int, message: str) -> None:
            record_progress(
                job_id, worker_id, progress=min(34, 5 + int(value * 0.29)),
                stage="transcribing", stage_progress=value, message=message,
            )

        caption_source = str(settings.get("caption_text_source") or "whisper").lower().strip()
        min_duration = (
            float(settings.get("caption_min_duration") or 0.0)
            if bool(settings.get("caption_heavy_enabled", False)) else 0.0
        )
        tighten_gaps = (
            bool(settings.get("caption_gap_tighten", False))
            if bool(settings.get("caption_heavy_enabled", False)) else False
        )
        segments, detected, engine = build_caption_segments(
            text=text, audio_path=audio, duration=duration,
            sync_mode=str(settings.get("caption_sync_mode") or "better"),
            language=str(settings.get("caption_language") or "auto"),
            max_words=int(settings.get("caption_max_words") or 8),
            offset=float(settings.get("caption_timing_offset") or 0.0),
            text_source=caption_source,
            prefer_script_language=(
                bool(settings.get("caption_prefer_script_language", True))
                and caption_source != "whisper"
            ),
            model_size=str(settings.get("caption_whisper_model") or "base"),
            min_duration=min_duration,
            tighten_gaps=tighten_gaps,
            progress_callback=progress, cancel_callback=cancelled,
        )
        if cancelled():
            raise RuntimeError("Transcription cancelled")
        metadata.update({
            "caption_segments": [segment.to_dict() for segment in segments],
            "caption_detected_language": detected,
            "caption_sync_engine": engine,
            "caption_audio_duration": duration,
            "caption_generation_signature": caption_generation_signature(
                text=text,
                audio_path=str(audio),
                language=str(settings.get("caption_language") or "auto"),
                model_size=str(settings.get("caption_whisper_model") or "base"),
                max_words=int(settings.get("caption_max_words") or 8),
                offset=float(settings.get("caption_timing_offset") or 0.0),
                min_duration=min_duration,
                tighten_gaps=tighten_gaps,
                voice_speed_percent=int(settings.get("voice_speed_percent") or 100),
                text_source=caption_source,
            ),
        })
        settings["metadata"] = metadata
        settings["_pipeline_v3"] = True
        settings["_pipeline_defer_publish"] = True
        artifact = root / "artifacts" / f"{job_id}_transcription.json"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        with connect() as db:
            db.execute(
                "UPDATE jobs SET settings_json=?,transcription_path=?,updated_at=? WHERE id=?",
                (json.dumps(settings, ensure_ascii=False, separators=(",",":")), str(artifact), now_iso(), job_id),
            )
            db.commit()
        return release_stage(job_id, worker_id, "ready_render", 35, "Transcription saved; waiting for render")
    except BaseException as exc:
        return finish_job(job_id, worker_id, FAILED, message="Transcription failed", error_text=f"{type(exc).__name__}: {exc}")


def publish_job(job: dict, worker_id: str, shutdown_requested=lambda: False) -> dict:
    job_id = str(job["id"])
    try:
        if shutdown_requested():
            raise InterruptedError("Uploader shutdown requested")
        record_progress(job_id, worker_id, progress=96, stage="publishing", stage_progress=5, message="Uploading validated video to Google Drive")
        result = upload_completed_job(
            job_id, str(job["output_path"]), now_iso(),
            cancel_requested=lambda: is_cancel_requested(job_id) or bool(shutdown_requested()),
            heartbeat=lambda: heartbeat_job(job_id, worker_id, stage="publishing"),
        )
        # Drive verification is the completion boundary. Once the uploader
        # returns successfully, the remote object has already been verified.
        # Cleanup therefore uses only durable local state and performs zero
        # additional Google Drive calls.
        settings = dict(job.get("settings") or {})
        metadata = settings.get("metadata") if isinstance(settings.get("metadata"), dict) else {}
        cleanup_requested = bool(metadata.get("web_cleanup_after_verify", False)) and not bool(metadata.get("web_preview_reference", False))
        with connect() as db:
            db.execute(
                """UPDATE jobs
                   SET pipeline_stage='completed',
                       local_cleanup_status=?,
                       cleanup_error='',
                       updated_at=?
                   WHERE id=?""",
                ("cleanup_pending" if cleanup_requested else "retained", now_iso(), job_id),
            )
            db.commit()
        completed = finish_job(
            job_id, worker_id, COMPLETED,
            message=("Completed and Drive verified; local cleanup pending"
                     if cleanup_requested else
                     "Completed and Drive verified; local files retained by policy"),
        )
        if cleanup_requested:
            try:
                # Fast path: reclaim this project's local payload immediately.
                # The background cleanup loop remains as a local-only retry path.
                _cleanup_verified_projects_once(preferred_job_id=job_id, limit=1)
            except BaseException:
                logging.exception("Immediate ACS cleanup failed for job %s; local retry loop will retry", job_id)
        return completed
    except BaseException as exc:
        if shutdown_requested():
            reason = "Drive uploader shutdown interrupted the active stage; recovery may requeue it"
            try:
                return interrupt_job(job_id, worker_id, reason)
            except Exception:
                return get_job(job_id)
        mark_drive_failure(job_id, str(exc), now_iso())
        return finish_job(job_id, worker_id, FAILED, message="Drive publication failed", error_text=f"{type(exc).__name__}: {exc}")



AUTO_CLEAN_BATCH_SIZE = 10
AUTO_CLEAN_POLL_SECONDS = 1.0


def _safe_project_root(value: str) -> Path | None:
    """Return a verified per-project path under the ACS projects root."""
    try:
        root = Path(str(value or "")).resolve()
        base = projects_root().resolve()
        if root == base or base not in root.parents:
            return None
        return root
    except Exception:
        return None


def _cleanup_verified_projects_once(*, preferred_job_id: str = "", limit: int = AUTO_CLEAN_BATCH_SIZE) -> dict:
    """Reclaim Drive-verified local ACS projects without another Drive call.

    Each project becomes cleanup-eligible independently as soon as its durable
    jobs.drive_verified flag is set. Batch completion is intentionally irrelevant.
    A small local chunk limit protects disk/DB latency if many projects become
    eligible at once.
    """
    initialize_storage()
    limit = max(1, min(AUTO_CLEAN_BATCH_SIZE, int(limit or AUTO_CLEAN_BATCH_SIZE)))
    with connect() as db:
        params: list[object] = []
        preferred = ""
        if preferred_job_id:
            preferred = "AND j.id=?"
            params.append(str(preferred_job_id))
        params.append(limit)
        rows = db.execute(
            f"""SELECT j.id AS job_id,j.project_id,j.settings_json,j.drive_status,
                       j.drive_verified,j.local_cleanup_status,j.cleanup_error,
                       p.project_path
                FROM jobs j
                JOIN projects p ON p.id=j.project_id
                WHERE j.status='completed'
                  AND j.drive_verified=1
                  AND COALESCE(j.local_cleanup_status,'retained')<>'cleared'
                  {preferred}
                ORDER BY
                  CASE WHEN COALESCE(j.local_cleanup_status,'')='cleanup_pending' THEN 0 ELSE 1 END,
                  j.updated_at,j.id
                LIMIT ?""",
            params,
        ).fetchall()

    cleaned = 0
    blocked = 0
    for row in rows:
        job_id = str(row["job_id"])
        try:
            settings = json.loads(str(row["settings_json"] or "{}"))
        except Exception:
            settings = {}
        metadata = settings.get("metadata") if isinstance(settings.get("metadata"), dict) else {}
        if not bool(metadata.get("web_cleanup_after_verify", False)):
            with connect() as db:
                db.execute(
                    "UPDATE jobs SET local_cleanup_status='retained',cleanup_error='',updated_at=? WHERE id=?",
                    (now_iso(), job_id),
                )
                db.commit()
            continue
        if bool(metadata.get("web_preview_reference", False)):
            with connect() as db:
                db.execute(
                    "UPDATE jobs SET local_cleanup_status='retained',cleanup_error='',updated_at=? WHERE id=?",
                    (now_iso(), job_id),
                )
                db.commit()
            continue

        root = _safe_project_root(str(row["project_path"] or ""))
        if root is None:
            blocked += 1
            err = f"Unsafe project path refused: {row['project_path']}"
            logging.error("Immediate cleanup refused unsafe project path for job %s: %s", job_id, row["project_path"])
            with connect() as db:
                db.execute(
                    """UPDATE jobs SET local_cleanup_status='cleanup_failed',
                       cleanup_error=?,updated_at=? WHERE id=?""",
                    (err, now_iso(), job_id),
                )
                db.commit()
            continue

        try:
            if root.exists():
                shutil.rmtree(root)
            with connect() as db:
                ts = now_iso()
                db.execute(
                    """UPDATE jobs
                       SET local_cleanup_status='cleared',
                           cleanup_error='',
                           message='Drive verified; local project auto-cleaned immediately',
                           updated_at=?
                       WHERE id=? AND drive_verified=1 AND status='completed'""",
                    (ts, job_id),
                )
                db.execute(
                    "UPDATE projects SET status='cleaned' WHERE id=?",
                    (str(row["project_id"]),),
                )
                db.commit()
            cleaned += 1
        except Exception as exc:
            blocked += 1
            logging.exception("Immediate cleanup failed for ACS job %s", job_id)
            with connect() as db:
                db.execute(
                    """UPDATE jobs SET local_cleanup_status='cleanup_failed',
                       cleanup_error=?,updated_at=? WHERE id=?""",
                    (f"{type(exc).__name__}: {exc}"[:4000], now_iso(), job_id),
                )
                db.commit()

    return {"cleaned": cleaned, "blocked": blocked}


def _cleanup_loop(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            result = _cleanup_verified_projects_once()
            if result.get("cleaned") or result.get("blocked"):
                logging.info("ACS immediate verified cleanup: %s", result)
        except BaseException:
            logging.exception("ACS immediate verified cleanup loop failure")
        stop.wait(AUTO_CLEAN_POLL_SECONDS)


def _loop(name: str, claim, process, stop: threading.Event) -> None:
    worker_id = f"{name}-{uuid.uuid4().hex[:12]}"
    while not stop.is_set():
        try:
            item = claim(worker_id)
            if item is None:
                stop.wait(0.75)
            else:
                process(item, worker_id)
        except BaseException:
            logging.exception("Phase 3 %s loop failure", name)
            stop.wait(2.0)


def main() -> int:
    initialize_storage()
    reconcile_stale_jobs(auto_requeue=True)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_args: stop.set())
    loops = [
        ("ingress", claim_next_ingress, process_ingress),
        ("transcribe", lambda wid: claim_stage("ready_transcribe", wid), transcribe_job),
    ]
    threads = [threading.Thread(target=_loop, args=(*item, stop), name=f"phase3-{item[0]}", daemon=True) for item in loops]
    threads.append(threading.Thread(target=_cleanup_loop, args=(stop,), name="phase3-batch-cleanup", daemon=True))
    for thread in threads: thread.start()
    while not stop.wait(1.0):
        pass
    for thread in threads: thread.join(timeout=15)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

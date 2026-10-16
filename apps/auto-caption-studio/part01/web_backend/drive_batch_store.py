from __future__ import annotations

import json
from typing import Any

from .database import connect, initialize_storage, now_iso

TERMINAL_JOB_STATES = {"completed", "failed", "cancelled", "interrupted"}
TERMINAL_BATCH_STATES = {"completed", "failed", "cancelled"}


def ensure_drive_batch_schema() -> None:
    initialize_storage()
    with connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS drive_batch_queue (
                id TEXT PRIMARY KEY,
                batch_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                item_order INTEGER NOT NULL,
                project_name TEXT NOT NULL COLLATE NOCASE,
                project_id TEXT NOT NULL,
                job_id TEXT NOT NULL,
                txt_remote TEXT NOT NULL,
                audio_remote TEXT NOT NULL,
                source_folder TEXT NOT NULL,
                used_folder TEXT NOT NULL,
                used_txt_remote TEXT NOT NULL DEFAULT '',
                used_audio_remote TEXT NOT NULL DEFAULT '',
                settings_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'remote_waiting',
                retry_count INTEGER NOT NULL DEFAULT 0,
                next_retry_at TEXT NOT NULL DEFAULT '',
                error_text TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(batch_id,item_order),
                UNIQUE(batch_id,project_name),
                UNIQUE(job_id),
                UNIQUE(project_id)
            );
            CREATE INDEX IF NOT EXISTS idx_drive_batch_queue_batch_order
                ON drive_batch_queue(batch_id,item_order);
            CREATE INDEX IF NOT EXISTS idx_drive_batch_queue_owner_status
                ON drive_batch_queue(owner_id,status,created_at);
            CREATE INDEX IF NOT EXISTS idx_drive_batch_queue_remote
                ON drive_batch_queue(owner_id,txt_remote,audio_remote);
            """
        )
        db.commit()


def _settings(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def job_release_ready(job: dict[str, Any]) -> bool:
    status = str(job.get("job_status") or job.get("status") or "")
    if status in {"failed", "cancelled", "interrupted"}:
        return True
    if status != "completed":
        return False
    settings = _settings(str(job.get("settings_json") or "{}"))
    metadata = settings.get("metadata") if isinstance(settings.get("metadata"), dict) else {}
    drive_enabled = bool(metadata.get("web_drive_upload_enabled", True))
    if not drive_enabled:
        return str(job.get("drive_status") or "") == "not_required"
    return int(job.get("drive_verified") or 0) == 1 and str(job.get("local_cleanup_status") or "") == "cleared"


def job_production_slot_ready(job: dict[str, Any]) -> bool:
    """Release the next production job after local compression completes.

    Drive upload, verification, and cleanup remain a separate background
    lifecycle and continue to use ``job_release_ready`` above.
    """
    status = str(job.get("job_status") or job.get("status") or "")
    if status in {"failed", "cancelled", "interrupted"}:
        return True
    pipeline_stage = str(job.get("pipeline_stage") or "")
    return pipeline_stage in {"ready_upload", "completed"} or status == "completed"


def deferred_batch_summaries(owner_id: str, *, include_all: bool = False, limit: int = 30) -> list[dict[str, Any]]:
    ensure_drive_batch_schema()
    with connect() as db:
        where = "" if include_all else "WHERE q.owner_id=?"
        params: list[Any] = [] if include_all else [owner_id]
        rows = db.execute(
            f"""
            SELECT q.*,b.status AS batch_status,b.control_state,b.category,b.expected_count,
                   b.staged_count,b.verified_count,b.created_at AS batch_created_at,
                   j.status AS job_status,j.pipeline_stage,j.current_stage,j.progress,
                   j.drive_status,j.drive_verified,j.local_cleanup_status
            FROM drive_batch_queue q
            JOIN upload_batches b ON b.id=q.batch_id
            LEFT JOIN jobs j ON j.id=q.job_id
            {where}
            ORDER BY b.created_at DESC,q.item_order ASC
            """,
            params,
        ).fetchall()
    grouped: dict[str, dict[str, Any]] = {}
    for raw in rows:
        row = dict(raw)
        bid = str(row["batch_id"])
        g = grouped.setdefault(bid, {
            "batch_id": bid,
            "category": str(row.get("category") or "Web Flow"),
            "batch_status": str(row.get("batch_status") or "accepted"),
            "control_state": str(row.get("control_state") or "active"),
            "source_folder": str(row.get("source_folder") or ""),
            "used_folder": str(row.get("used_folder") or "Used"),
            "total": int(row.get("expected_count") or 0),
            "on_drive": 0, "copying": 0, "waiting": 0, "active": 0,
            "completed": 0, "failed": 0, "moved_to_used": 0,
            "current_project": "", "next_project": "", "last_error": "",
            "created_at": str(row.get("batch_created_at") or ""),
        })
        qstatus = str(row.get("status") or "remote_waiting")
        jstatus = str(row.get("job_status") or "held")
        if qstatus in {"remote_waiting", "retry_wait"}:
            g["on_drive"] += 1
        elif qstatus in {"copying", "move_pending"}:
            g["copying"] += 1
        if str(row.get("used_txt_remote") or "") and str(row.get("used_audio_remote") or ""):
            g["moved_to_used"] += 1
        if qstatus == "staged" and jstatus == "held":
            g["waiting"] += 1
            if not g["next_project"]:
                g["next_project"] = str(row.get("project_name") or "")
        if qstatus == "staged" and jstatus in {"queued", "running", "completed"} and not job_release_ready(row):
            g["active"] += 1
            if not g["current_project"]:
                g["current_project"] = str(row.get("project_name") or "")
        if jstatus == "completed" and job_release_ready(row):
            g["completed"] += 1
        elif jstatus in {"failed", "cancelled", "interrupted"}:
            g["failed"] += 1
        if row.get("error_text"):
            g["last_error"] = str(row.get("error_text"))
    items = list(grouped.values())
    items.sort(key=lambda x: x["created_at"], reverse=True)
    return items[:max(1, int(limit))]


def recover_interrupted_copy_states() -> int:
    ensure_drive_batch_schema()
    ts = now_iso()
    with connect() as db:
        rows = db.execute(
            """SELECT q.id,j.drive_input_verified FROM drive_batch_queue q
               LEFT JOIN jobs j ON j.id=q.job_id WHERE q.status='copying'"""
        ).fetchall()
        for row in rows:
            db.execute(
                "UPDATE drive_batch_queue SET status=?,updated_at=? WHERE id=?",
                ("move_pending" if int(row["drive_input_verified"] or 0) else "remote_waiting", ts, str(row["id"])),
            )
        db.commit()
    return len(rows)

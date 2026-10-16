from __future__ import annotations

import os
import shutil
from pathlib import Path

MAX_PENDING_UPLOADS = max(1, int(os.environ.get("ACS_MAX_PENDING_UPLOADS", "20")))
RESUME_PENDING_UPLOADS = max(0, min(MAX_PENDING_UPLOADS - 1, int(os.environ.get("ACS_RESUME_PENDING_UPLOADS", "15"))))
MIN_FREE_BYTES = max(0, int(os.environ.get("ACS_MIN_FREE_BYTES", str(10 * 1024 * 1024 * 1024))))
RESUME_FREE_BYTES = max(MIN_FREE_BYTES, int(os.environ.get("ACS_RESUME_FREE_BYTES", str(MIN_FREE_BYTES))))
STATE_KEY = "acs_upload_backpressure"

def pending_upload_count(db) -> int:
    row = db.execute("""SELECT COUNT(*) AS n FROM jobs
       WHERE drive_verified=0
         AND (drive_status='uploading'
              OR (pipeline_stage IN ('ready_upload','ready_publish')
                  AND status IN ('queued','running')))""").fetchone()
    return int(row["n"] if row is not None else 0)

def held_drive_count(db) -> int:
    row = db.execute("""SELECT COUNT(*) AS n
       FROM drive_batch_queue q JOIN jobs j ON j.id=q.job_id
       WHERE q.status='staged' AND j.status='held'""").fetchone()
    return int(row["n"] if row is not None else 0)

RENDER_STATE_KEY = "acs_render_backpressure"


def _latched(db, state_key: str = STATE_KEY) -> bool:
    row = db.execute("SELECT value FROM scheduler_state WHERE name=?", (state_key,)).fetchone()
    return bool(row and str(row["value"] or "") == "blocked")


def render_admission(db, root: str | Path | None = None) -> tuple[bool, str]:
    """Admit local rendering using disk safety only; Drive output is independent."""
    return _admission(db, root=root, include_pending_uploads=False, state_key=RENDER_STATE_KEY)


def prefetch_admission(db, root: str | Path | None = None) -> tuple[bool, str]:
    """Keep the existing Drive-prefetch backlog guard separate from rendering."""
    return _admission(db, root=root, include_pending_uploads=True, state_key=STATE_KEY)


def _admission(
    db,
    root: str | Path | None = None,
    *,
    include_pending_uploads: bool,
    state_key: str,
) -> tuple[bool, str]:
    pending = pending_upload_count(db) if include_pending_uploads else 0
    check_path = Path(root or os.environ.get("AUTO_CAPTION_APP_ROOT", "/home/ubuntu/apps/auto_caption_studio"))
    try:
        free = int(shutil.disk_usage(check_path).free)
    except OSError:
        free = 0
    latched = _latched(db, state_key)
    upload_blocked = include_pending_uploads and (
        pending >= MAX_PENDING_UPLOADS or (latched and pending > RESUME_PENDING_UPLOADS)
    )
    blocked = free < MIN_FREE_BYTES or upload_blocked
    next_state = "blocked" if blocked else "open"
    db.execute("""INSERT INTO scheduler_state(name,value,updated_at) VALUES(?,?,datetime('now'))
       ON CONFLICT(name) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
       (state_key, next_state))
    if free < MIN_FREE_BYTES:
        return False, f"disk_free_bytes={free}<min_free_bytes={MIN_FREE_BYTES}"
    if upload_blocked:
        return False, f"pending_uploads={pending};resume_at<={RESUME_PENDING_UPLOADS}"
    if free < RESUME_FREE_BYTES:
        return False, f"disk_free_bytes={free}<resume_free_bytes={RESUME_FREE_BYTES}"
    if include_pending_uploads:
        return True, f"admitted pending_uploads={pending} disk_free_bytes={free}"
    return True, f"admitted local_render disk_free_bytes={free}"

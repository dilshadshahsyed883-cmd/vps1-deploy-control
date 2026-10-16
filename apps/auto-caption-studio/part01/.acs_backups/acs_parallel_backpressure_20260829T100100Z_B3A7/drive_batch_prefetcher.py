from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import time
from typing import Any

from .database import connect, now_iso
from .drive_batch_store import ensure_drive_batch_schema, job_release_ready, recover_interrupted_copy_states
from .job_states import QUEUED
from .queue_store import transition_job_in_db
from .web_flow_api import (
    _acs_rclone, _drive_copyto, _encode_drive_id_file, _load_drive_runtime,
    _parse_drive_file_source, _parse_drive_folder_source, _sha256,
    _validate_audio_file,
)

POLL_SECONDS = max(1.0, float(os.environ.get("ACS_DRIVE_PREFETCH_POLL_SECONDS", "2")))
MAX_RETRY_SECONDS = 900
_USED_READY: set[tuple[str, str]] = set()


def _retry_after(retry_count: int) -> str:
    seconds = min(MAX_RETRY_SECONDS, 30 * (2 ** min(5, max(0, retry_count - 1))))
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _due(value: str) -> bool:
    if not value:
        return True
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc) <= datetime.now(timezone.utc)
    except Exception:
        return True


def _project_root(project_id: str) -> Path:
    with connect() as db:
        row = db.execute("SELECT project_path FROM projects WHERE id=?", (project_id,)).fetchone()
    if row is None:
        raise RuntimeError("Deferred Drive project row is missing")
    return Path(str(row["project_path"]))


def _remote_exists(source: dict[str, str]) -> bool:
    try:
        _config, remote, _root = _load_drive_runtime()
        if source["kind"] == "id":
            args = ["lsjson", "--stat", "--drive-root-folder-id", source["folder_id"], f"{remote}:{source['path']}"]
        else:
            args = ["lsjson", "--stat", f"{remote}:{source['path']}"]
        _acs_rclone(args, timeout=120)
        return True
    except Exception:
        return False


def _used_destination(row: dict[str, Any], remote_value: str) -> str:
    src = _parse_drive_file_source(remote_value)
    name = Path(src["path"]).name
    used_folder = str(row["used_folder"] or "Used").strip("/")
    folder = _parse_drive_folder_source(str(row["source_folder"]))
    if src["kind"] == "id":
        return _encode_drive_id_file(src["folder_id"], f"{used_folder}/{name}")
    base = str(folder.get("path") or "").strip("/")
    return f"{base}/{used_folder}/{name}".strip("/")


def _ensure_used_folder(row: dict[str, Any]) -> None:
    folder = _parse_drive_folder_source(str(row["source_folder"]))
    used_folder = str(row["used_folder"] or "Used").strip("/")
    key = (str(folder.get("folder_id") or folder.get("path") or ""), used_folder)
    if key in _USED_READY:
        return
    _config, remote, _root = _load_drive_runtime()
    if folder["kind"] == "id":
        _acs_rclone(["mkdir", "--drive-root-folder-id", folder["folder_id"], f"{remote}:{used_folder}"], timeout=120)
    else:
        base = str(folder.get("path") or "").strip("/")
        _acs_rclone(["mkdir", f"{remote}:{base}/{used_folder}".rstrip("/")], timeout=120)
    _USED_READY.add(key)


def _move_one(source_value: str, dest_value: str) -> None:
    source = _parse_drive_file_source(source_value)
    dest = _parse_drive_file_source(dest_value)
    if _remote_exists(dest):
        return
    _config, remote, _root = _load_drive_runtime()
    try:
        if source["kind"] == "id":
            _acs_rclone([
                "moveto", "--drive-root-folder-id", source["folder_id"],
                f"{remote}:{source['path']}", f"{remote}:{dest['path']}",
            ], timeout=600)
        else:
            _acs_rclone(["moveto", f"{remote}:{source['path']}", f"{remote}:{dest['path']}"], timeout=600)
    except Exception:
        if not _remote_exists(dest):
            raise
    if not _remote_exists(dest):
        raise RuntimeError("Drive Used move could not be verified")


def _finish_used_move(row: dict[str, Any]) -> None:
    _ensure_used_folder(row)
    txt_dest = str(row.get("used_txt_remote") or "") or _used_destination(row, str(row["txt_remote"]))
    audio_dest = str(row.get("used_audio_remote") or "") or _used_destination(row, str(row["audio_remote"]))
    _move_one(str(row["txt_remote"]), txt_dest)
    with connect() as db:
        db.execute("UPDATE drive_batch_queue SET used_txt_remote=?,updated_at=? WHERE id=?", (txt_dest, now_iso(), str(row["id"])))
        db.commit()
    _move_one(str(row["audio_remote"]), audio_dest)
    with connect() as db:
        db.execute(
            """UPDATE drive_batch_queue SET used_txt_remote=?,used_audio_remote=?,status='staged',
               retry_count=0,next_retry_at='',error_text='',updated_at=? WHERE id=?""",
            (txt_dest, audio_dest, now_iso(), str(row["id"])),
        )
        job = db.execute("SELECT settings_json FROM jobs WHERE id=?", (str(row["job_id"]),)).fetchone()
        if job is not None:
            try:
                settings = json.loads(str(job["settings_json"] or "{}"))
            except Exception:
                settings = {}
            metadata = settings.get("metadata") if isinstance(settings.get("metadata"), dict) else {}
            metadata.update({
                "drive_source_moved_to_used": True,
                "drive_source_original_txt": str(row["txt_remote"]),
                "drive_source_original_audio": str(row["audio_remote"]),
                "drive_source_used_txt": txt_dest,
                "drive_source_used_audio": audio_dest,
                "drive_source_used_folder": str(row["used_folder"]),
            })
            settings["metadata"] = metadata
            db.execute("UPDATE jobs SET settings_json=?,updated_at=? WHERE id=?", (json.dumps(settings, ensure_ascii=False, separators=(",", ":")), now_iso(), str(row["job_id"])))
        db.commit()


def _copy_into_project(row: dict[str, Any]) -> None:
    root = _project_root(str(row["project_id"]))
    root.joinpath("input").mkdir(parents=True, exist_ok=True)
    txt_source = _parse_drive_file_source(str(row["txt_remote"]))
    audio_source = _parse_drive_file_source(str(row["audio_remote"]))
    txt_name = Path(txt_source["path"]).name
    audio_name = Path(audio_source["path"]).name
    project_name = str(row["project_name"])
    script_path = root / "input" / f"{project_name}.txt"
    audio_path = root / "input" / f"{project_name}{Path(audio_name).suffix.lower()}"
    _config, remote, _drive_root = _load_drive_runtime()
    if not script_path.is_file() or script_path.stat().st_size <= 0:
        _drive_copyto(remote, txt_source, script_path)
    if not audio_path.is_file() or audio_path.stat().st_size <= 0:
        _drive_copyto(remote, audio_source, audio_path)
    if not script_path.is_file() or script_path.stat().st_size <= 0:
        raise RuntimeError("Drive TXT copy verification failed")
    _validate_audio_file(audio_path)
    script_sha, audio_sha = _sha256(script_path), _sha256(audio_path)
    ts = now_iso()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            """UPDATE jobs SET input_transport='google_drive_deferred',drive_input_status='verified',
               drive_input_verified=1,current_stage='held',message='Drive source copied and verified — moving originals to Used',updated_at=?
               WHERE id=?""",
            (ts, str(row["job_id"])),
        )
        db.execute(
            """UPDATE batch_items SET upload_verified=1,script_sha256=?,audio_sha256=?,script_size=?,audio_size=?,updated_at=?
               WHERE job_id=?""",
            (script_sha, audio_sha, script_path.stat().st_size, audio_path.stat().st_size, ts, str(row["job_id"])),
        )
        verified = int(db.execute("SELECT COUNT(*) FROM batch_items WHERE batch_id=? AND upload_verified=1", (str(row["batch_id"]),)).fetchone()[0])
        db.execute("UPDATE upload_batches SET verified_count=?,updated_at=? WHERE id=?", (verified, ts, str(row["batch_id"])))
        db.execute("UPDATE drive_batch_queue SET status='move_pending',updated_at=?,error_text='' WHERE id=?", (ts, str(row["id"])))
        db.commit()


def _mark_retry(row_id: str, exc: Exception) -> None:
    with connect() as db:
        current = db.execute("SELECT retry_count,status FROM drive_batch_queue WHERE id=?", (row_id,)).fetchone()
        count = int(current["retry_count"] or 0) + 1 if current else 1
        state = "move_pending" if current and str(current["status"]) == "move_pending" else "retry_wait"
        db.execute(
            """UPDATE drive_batch_queue SET status=?,retry_count=?,next_retry_at=?,error_text=?,updated_at=? WHERE id=?""",
            (state, count, _retry_after(count), str(exc)[-1200:], now_iso(), row_id),
        )
        db.commit()


def _materialize(row: dict[str, Any], *, activate: bool) -> bool:
    row_id = str(row["id"])
    try:
        with connect() as db:
            db.execute("UPDATE drive_batch_queue SET status='copying',error_text='',updated_at=? WHERE id=?", (now_iso(), row_id))
            db.commit()
        _copy_into_project(row)
        with connect() as db:
            fresh = db.execute("SELECT * FROM drive_batch_queue WHERE id=?", (row_id,)).fetchone()
        if fresh is None:
            return False
        _finish_used_move(dict(fresh))
        if activate:
            _release_job(str(row["job_id"]))
        return True
    except Exception as exc:
        _mark_retry(row_id, exc)
        return False


def _release_job(job_id: str) -> None:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if job is not None and str(job["status"] or "") == "held":
            transition_job_in_db(
                db, job_id, QUEUED, stage="queued", progress=0,
                message="Drive source verified and moved to Used — queued for production",
                event_type="deferred_drive_released",
            )
        db.commit()


def _batch_rows(batch_id: str) -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute(
            """SELECT q.*,j.status AS job_status,j.pipeline_stage,j.current_stage,j.progress,j.settings_json AS job_settings_json,
                      j.drive_status,j.drive_verified,j.local_cleanup_status
               FROM drive_batch_queue q JOIN jobs j ON j.id=q.job_id
               WHERE q.batch_id=? ORDER BY q.item_order""", (batch_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def _job_ready(row: dict[str, Any]) -> bool:
    return job_release_ready({
        "status": row.get("job_status"), "settings_json": row.get("job_settings_json"),
        "drive_status": row.get("drive_status"), "drive_verified": row.get("drive_verified"),
        "local_cleanup_status": row.get("local_cleanup_status"),
    })


def _refresh_batch_terminal_state(batch_id: str) -> bool:
    """Mark a deferred batch completed once every saved project is terminal.

    Render failures remain visible in per-project/summary failed counts; completion
    here means the batch has no remaining production work.
    """
    rows = _batch_rows(batch_id)
    if not rows:
        return False
    if not all(str(r["status"]) in {"completed", "failed"} for r in rows):
        return False
    ts = now_iso()
    with connect() as db:
        db.execute("UPDATE upload_batches SET status='completed',control_state='paused',updated_at=?,finalized_at=? WHERE id=?", (ts, ts, batch_id))
        db.commit()
    return True


def _process_batch(batch: dict[str, Any]) -> None:
    batch_id = str(batch["id"])
    control = str(batch.get("control_state") or "active")
    rows = _batch_rows(batch_id)

    # Finish any already-copied item move before considering new Drive reads.
    for row in rows:
        if str(row["status"]) == "move_pending" and _due(str(row.get("next_retry_at") or "")):
            try:
                _finish_used_move(row)
            except Exception as exc:
                _mark_retry(str(row["id"]), exc)
            return

    rows = _batch_rows(batch_id)
    for row in rows:
        if str(row["status"]) == "staged" and _job_ready(row):
            with connect() as db:
                terminal = str(row.get("job_status") or "failed")
                db.execute("UPDATE drive_batch_queue SET status=?,updated_at=? WHERE id=?", ("completed" if terminal == "completed" else "failed", now_iso(), str(row["id"])))
                db.commit()
    rows = _batch_rows(batch_id)
    if _refresh_batch_terminal_state(batch_id):
        return

    active = next((r for r in rows if str(r["status"]) == "staged" and str(r["job_status"]) in {"queued", "running", "completed"} and not _job_ready(r)), None)
    waiting = next((r for r in rows if str(r["status"]) == "staged" and str(r["job_status"]) == "held"), None)

    if control == "paused":
        return
    if control == "pause_after_current":
        if active is None:
            with connect() as db:
                db.execute("UPDATE upload_batches SET control_state='paused',updated_at=? WHERE id=?", (now_iso(), batch_id))
                db.commit()
        return

    if active is None:
        if waiting is not None:
            _release_job(str(waiting["job_id"]))
            active = waiting
        else:
            candidate = next((r for r in rows if str(r["status"]) in {"remote_waiting", "retry_wait"} and _due(str(r.get("next_retry_at") or ""))), None)
            if candidate is not None:
                if not _materialize(candidate, activate=True):
                    return
                active = candidate
        rows = _batch_rows(batch_id)
        waiting = next((r for r in rows if str(r["status"]) == "staged" and str(r["job_status"]) == "held"), None)

    if active is not None and waiting is None:
        candidate = next((r for r in rows if str(r["status"]) in {"remote_waiting", "retry_wait"} and _due(str(r.get("next_retry_at") or ""))), None)
        if candidate is not None:
            _materialize(candidate, activate=False)


def _select_batch() -> dict[str, Any] | None:
    with connect() as db:
        # Keep global behavior to one Drive batch at a time: first prefer a batch
        # that already owns a local active/waiting slot, then the oldest active batch.
        rows = db.execute(
            """SELECT DISTINCT b.* FROM upload_batches b JOIN drive_batch_queue q ON q.batch_id=b.id
               WHERE b.status NOT IN ('completed','failed','cancelled')
               ORDER BY b.created_at,b.id"""
        ).fetchall()
        candidates = [dict(r) for r in rows]
        for batch in candidates:
            items = _batch_rows(str(batch["id"]))
            if any(str(x["status"]) in {"copying", "move_pending", "staged"} and str(x["job_status"]) not in {"failed", "cancelled", "interrupted"} for x in items):
                return batch
        return next((b for b in candidates if str(b.get("control_state") or "active") in {"active", "pause_after_current"}), None)


def run_once() -> None:
    ensure_drive_batch_schema()
    batch = _select_batch()
    if batch is not None:
        _process_batch(batch)


def main() -> None:
    ensure_drive_batch_schema()
    recovered = recover_interrupted_copy_states()
    print(f"ACS_DRIVE_PREFETCHER_START recovered={recovered}", flush=True)
    while True:
        try:
            run_once()
        except Exception as exc:
            print(f"ACS_DRIVE_PREFETCHER_ERROR {type(exc).__name__}: {exc}", flush=True)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()

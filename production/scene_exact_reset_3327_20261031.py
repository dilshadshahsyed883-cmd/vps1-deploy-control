"""Guarded, exact-job Scene Builder reset for the owner's stuck 97% render.

Only the named job's local project and worker queue records may be deleted.
Unrelated jobs, permanent media, web accounts, releases, uploaders and Auto Script stay unchanged.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import time
import urllib.error

from production import scene_production_worker as worker
from production.scene_exact_job_cancel_20261027 import _fetch, _verified_job, TARGET_JOB, TARGET_PROJECT

MARKER = "[vps1-scene-exact-reset-3327-20261031]"
TERMINAL = frozenset({"failed", "cancelled", "completed", "cleaned"})
CANCELLABLE = frozenset({
    "queued", "preparing", "downloading_media", "waiting_for_audit",
    "ready_to_render", "rendering", "ready_to_upload", "drive_uploading", "running",
    "failed", "cancelled",
})


def require(ok: bool, message: str) -> None:
    if not ok:
        raise RuntimeError("SCENE_EXACT_RESET_BLOCKED: " + message)


def inspect_queue(db_path: Path) -> dict | None:
    """Refuse to reset if any different queued, active or unknown-status project exists."""
    require(db_path.is_file() and not db_path.is_symlink(), "unknown worker database")
    with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        fields = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        require({"id", "project_name", "status", "drive_destination"} <= fields,
                "unexpected queue schema")
        for row in conn.execute("SELECT id,status FROM jobs WHERE id<>?", (TARGET_JOB,)):
            require(isinstance(row["status"], str) and row["status"].lower() in TERMINAL,
                    "another project is queued/active or has unknown status")
        target = conn.execute(
            "SELECT id,project_name,status,drive_destination FROM jobs WHERE id=?",
            (TARGET_JOB,),
        ).fetchone()
        if target is None:
            return None
        target = dict(target)
        require(target["project_name"] == TARGET_PROJECT, "target identity mismatch")
        require(str(target["status"]).lower() in CANCELLABLE,
                "target completed or has unrecognized status")
        require(not str(target["drive_destination"] or "").strip(),
                "Drive destination exists; do not delete remote media")
        return target


def _worker_health(token: str) -> bool:
    try:
        result = _fetch("GET", "/health", token)
        return result.get("ok") is True and result.get("admission_only") is False
    except (OSError, ValueError, RuntimeError):
        return False


def reset_exact_scene_job() -> str:
    require(os.geteuid() == 0 and socket.gethostname() == "vps-01a23801",
            "protected VPS_1 root host required")
    require(worker.RECORD.is_file() and not worker.RECORD.is_symlink(),
            "owned worker record absent")
    record = json.loads(worker.RECORD.read_text(encoding="utf-8"))
    require(record.get("owner") == "scene-production-worker-v1"
            and record.get("unit") == worker.UNIT
            and record.get("state") == str(worker.STATE)
            and record.get("endpoint") == worker.URL,
            "worker ownership mismatch")
    require(worker.ENV.is_file() and not worker.ENV.is_symlink(),
            "worker credentials unavailable")
    env = worker.entries(worker.ENV.read_text(encoding="utf-8"))
    token = str(env.get("SCENE_WORKER_TOKEN") or "")
    require(re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token) is not None
            and env.get("SCENE_WORKER_ADMISSION_ONLY") == "0",
            "not the authenticated writable worker")
    db_path = worker.STATE / "queue.sqlite3"
    current = inspect_queue(db_path)
    if current is None:
        print("SCENE_EXACT_RESET=ALREADY_ABSENT; NO_MUTATIONS", flush=True)
        return "already_absent"
    require(subprocess.run(["systemctl", "is-active", "--quiet", worker.UNIT],
                           timeout=10).returncode == 0, "owned worker service is not active")
    responsive = _worker_health(token)
    # A worker exhausted by open-file errors may fail even its health API.
    # The separately validated owned queue and service still allow a safe
    # worker-only restart, but no unverified HTTP mutation is attempted.
    if responsive:
        _verified_job(_fetch("GET", f"/jobs/{TARGET_JOB}", token))
        if current["status"].lower() not in TERMINAL:
            ack = _verified_job(_fetch("POST", f"/jobs/{TARGET_JOB}/cancel", token))
            require(int(ack.get("cancel_requested") or 0) == 1
                    or str(ack.get("status") or "").lower() == "cancelled",
                    "cancellation not acknowledged")
            for _ in range(4):
                time.sleep(1)
                if _verified_job(_fetch("GET", f"/jobs/{TARGET_JOB}", token)).get("status") == "cancelled":
                    break
    else:
        print("SCENE_EXACT_RESET=HEALTH_UNAVAILABLE; USING_VERIFIED_WORKER_ONLY_RECOVERY", flush=True)

    # Exact-job deletion must not affect the Drive copy (if one appears).
    current = inspect_queue(db_path)
    require(current is not None, "target disappeared during reset")
    if responsive and current["status"].lower() == "cancelled" and int(
            _verified_job(_fetch("GET", f"/jobs/{TARGET_JOB}", token)).get("cancel_requested") or 0
    ) == 1:
        result = _fetch("DELETE", f"/jobs/{TARGET_JOB}", token)
    else:
        # Persistently stuck or failed target: stop only the Scene render worker.
        # Worker startup marks interrupted jobs as failed, which would prevent
        # the normal cancellation+delete endpoint; restore cancelled state
        # explicitly *after the service is fully stopped*, then restart and let
        # the worker's built-in authenticated DELETE safely clean its own files.
        stop_attempted = False
        try:
            stop_attempted = True
            subprocess.run(["systemctl", "stop", worker.UNIT], check=True, timeout=55)
            require(subprocess.run(["systemctl", "is-active", "--quiet", worker.UNIT],
                                   timeout=10).returncode != 0,
                    "renderer still active after systemd stop")
            current = inspect_queue(db_path)
            require(current is not None, "target disappeared while stopping")
            require(current["status"].lower() in CANCELLABLE, "unexpected stopped target")
            backups = worker.STATE / "operator-backups"
            require(not backups.is_symlink(), "unsafe backup directory")
            backups.mkdir(mode=0o700, exist_ok=True)
            require(backups.is_dir(), "backup destination not directory")
            backup = backups / ("queue-before-3327-reset-" + str(time.time_ns()) + ".sqlite3")
            require(not backup.exists(), "backup path collision")
            with sqlite3.connect(str(db_path), timeout=20) as source, sqlite3.connect(str(backup), timeout=20) as destination:
                source.backup(destination)
            os.chmod(backup, 0o600)
            stamp = datetime.now(timezone.utc).isoformat()
            with sqlite3.connect(str(db_path), timeout=20) as conn:
                updated = conn.execute(
                    "UPDATE jobs SET status='cancelled',stage='Cancelled',cancel_requested=1,"
                    "cancelled_at=?,updated_at=?,message='Owner-authorized exact-job reset' "
                    "WHERE id=? AND project_name=? AND status IN (" +
                    ",".join("?" for _ in CANCELLABLE) + ")",
                    (stamp, stamp, TARGET_JOB, TARGET_PROJECT, *sorted(CANCELLABLE)),
                )
                require(updated.rowcount == 1, "target state changed concurrently")
            print("SCENE_EXACT_RESET=WORKER_STOPPED DB_BACKUP=CREATED", flush=True)
        finally:
            if stop_attempted:
                subprocess.run(["systemctl", "start", worker.UNIT], check=True, timeout=55)
        for _ in range(50):
            if _worker_health(token):
                break
            time.sleep(0.4)
        require(_worker_health(token), "Scene worker failed restart health")
        require(_verified_job(_fetch("GET", f"/jobs/{TARGET_JOB}", token))["status"] == "cancelled",
                "worker did not load cancelled exact job")
        require(inspect_queue(db_path)["status"] == "cancelled", "queue changed after restart")
        result = _fetch("DELETE", f"/jobs/{TARGET_JOB}", token)

    require(result.get("deleted") == TARGET_JOB and result.get("local_files_deleted") is True,
            "worker did not verify local deletion")
    require(result.get("drive_deleted") is False, "unexpected Drive deletion")
    require(inspect_queue(db_path) is None, "target queue record remains")
    require(not (worker.STATE / "jobs" / TARGET_JOB).exists(), "target media directory remains")
    require(_worker_health(token), "worker not healthy after exact reset")
    print("SCENE_EXACT_RESET=PASS TARGET_DELETED=YES WORKER_HEALTHY=YES OTHER_JOBS_PRESERVED=YES DRIVE_UNTOUCHED=YES", flush=True)
    return "deleted"

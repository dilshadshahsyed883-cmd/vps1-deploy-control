"""Transactional, isolated job-control policy for Kokoro and Edge SQLite queues.

No source-file, audio-output or Google Drive deletion takes place here. "delete"
is a reversible-in-database history hide, leaving retention ownership to each app.
"""
from __future__ import annotations

import os
import re
import sqlite3
import time
from datetime import datetime, timezone


class QueueActionError(Exception):
    def __init__(self, code: int, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(detail)


PREFIX = {"kokoro": "KOJ", "edge": "EDJ"}
ACTIONS = frozenset({"pause", "resume", "cancel", "skip", "retry", "delete"})


def utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def attempt_limit(engine):
    key = "KOKORO_MAX_ATTEMPTS" if engine == "kokoro" else "EDGE_MAX_ATTEMPTS"
    try:
        return max(1, min(20, int(os.environ.get(key, "3"))))
    except (TypeError, ValueError):
        return 3


def migrate(conn, engine):
    """Additive migration, never rebuild or discard existing job records."""
    if engine not in PREFIX:
        raise ValueError("Unknown engine")
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        required = {
            "hidden_at": "TEXT",
            "last_action": "TEXT",
            "progress": "INTEGER NOT NULL DEFAULT 0",
            "attempt": "INTEGER NOT NULL DEFAULT 0",
            "retry_at": "REAL NOT NULL DEFAULT 0",
        }
        for name, sqltype in required.items():
            if name not in existing:
                conn.execute("ALTER TABLE jobs ADD COLUMN " + name + " " + sqltype)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def allowed(job, engine):
    if job.get("hidden_at"):
        return []
    state = job["status"]
    actions = []
    if state == "queued":
        actions = ["pause", "cancel", "skip"]
    elif state == "processing":
        actions = (["pause"] if engine == "edge" else []) + ["cancel"]
    elif state == "paused":
        actions = ["resume", "cancel"]
    elif state in ("retry_wait",):
        actions = ["cancel"]
    elif state in ("pause_requested",):
        actions = ["cancel"]
    if state in ("failed", "cancelled") and int(job.get("attempt") or 0) < attempt_limit(engine):
        actions.append("retry")
    # History hide is independent of delivery. Completed audio is still a
    # terminal synthesis job even if its Google Drive upload fails or retries.
    # The delete action only sets hidden_at: neither audio/Drive files nor
    # the delivery worker's pending uploads are removed or cancelled.
    if state in ("completed", "failed", "cancelled", "skipped"):
        actions.append("delete")
    return actions


def job_view(row, engine):
    job = dict(row)
    action_list = allowed(job, engine)
    status = job["status"]
    # "retry_pending" on completed jobs is *delivery* retry, not synthesis retry.
    job["queue_status"] = "retry_pending" if status == "retry_wait" else status
    job["allowed_actions"] = action_list
    job["max_attempts"] = attempt_limit(engine)
    job["output_available"] = status == "completed" and (
        bool(job.get("wav_file")) or bool(job.get("mp3_file")))
    return job


def apply(conn, engine, job_id, action):
    if engine not in PREFIX:
        raise QueueActionError(404, "Unknown engine")
    if not re.fullmatch(PREFIX[engine] + r"-[A-F0-9]{16}", job_id):
        raise QueueActionError(404, "Job not found")
    if action not in ACTIONS:
        raise QueueActionError(404, "Unsupported action")

    # BEGIN IMMEDIATE serializes competing requests from distinct API workers.
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None or row["hidden_at"]:
            raise QueueActionError(404, "Job not found")
        job = dict(row)
        old = job["status"]
        permitted = allowed(job, engine)
        target = None
        if action in permitted:
            if action == "pause":
                target = "pause_requested" if old == "processing" else "paused"
            elif action == "resume":
                target = "queued"
            elif action == "cancel":
                target = "cancel_requested" if old in ("processing", "pause_requested") else "cancelled"
            elif action == "skip":
                target = "skipped"
            elif action == "retry":
                target = "retry_wait"
            elif action == "delete":
                conn.execute("UPDATE jobs SET hidden_at=?,last_action=? WHERE job_id=?",
                             (utc(), "delete", job_id))
                conn.execute("COMMIT")
                return {"job_id": job_id, "status": old, "hidden": True, "idempotent": False}
        else:
            stable = {
                "pause": ("paused", "pause_requested"),
                "resume": ("queued",),
                "cancel": ("cancelled", "cancel_requested"),
                "skip": ("skipped",),
                "retry": ("retry_wait",),
            }
            if action == job.get("last_action") and old in stable.get(action, ()):
                conn.execute("COMMIT")
                return {"job_id": job_id, "status": old, "idempotent": True}
            raise QueueActionError(409, "Cannot " + action + " job in state " + old)
        updates = {"status": target, "last_action": action}
        columns = {column[1] for column in conn.execute("PRAGMA table_info(jobs)")}
        if "updated_at" in columns:
            updates["updated_at"] = utc()
        if action in ("cancel", "skip") and target in ("cancelled", "skipped"):
            updates["delivery_status"] = "not_applicable"
        if action == "retry":
            # Retain input + any Drive receipts; worker uses its own per-job folder.
            # Manual retry admitted only under the synthesis attempt limit.
            updates["retry_at"] = time.time() + 2
            updates["error"] = None
            updates["progress"] = 0
        expression = ", ".join(name + "=?" for name in updates)
        conn.execute("UPDATE jobs SET " + expression + " WHERE job_id=?",
                     (*updates.values(), job_id))
        conn.execute("COMMIT")
        return {"job_id": job_id, "status": target, "idempotent": False}
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise

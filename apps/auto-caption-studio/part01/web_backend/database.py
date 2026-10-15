from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import inspect
import json
from pathlib import Path
import sqlite3
from typing import Callable
import uuid

from .config import (
    database_path,
    logs_root,
    outputs_root,
    previews_root,
    projects_root,
    uploads_root,
)


Migration = Callable[[sqlite3.Connection], None]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=30.0, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=30000")
    db.execute("PRAGMA journal_mode=WAL")
    return db


def initialize_storage() -> None:
    for path in (
        projects_root(), uploads_root(), outputs_root(), previews_root(), logs_root()
    ):
        path.mkdir(parents=True, exist_ok=True)
    run_migrations()


def _table_columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in db.execute(f"PRAGMA table_info({table})")}


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _add_column(db: sqlite3.Connection, table: str, name: str, ddl: str) -> None:
    if name not in _table_columns(db, table):
        db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _migration_001_initial(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'created',
            created_at TEXT NOT NULL,
            project_path TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            status TEXT NOT NULL,
            progress INTEGER NOT NULL DEFAULT 0,
            message TEXT NOT NULL DEFAULT '',
            settings_json TEXT NOT NULL DEFAULT '{}',
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            attempt INTEGER NOT NULL DEFAULT 1,
            active_attempt_id TEXT,
            output_path TEXT NOT NULL DEFAULT '',
            preview_path TEXT NOT NULL DEFAULT '',
            log_path TEXT NOT NULL DEFAULT '',
            error_text TEXT NOT NULL DEFAULT '',
            current_stage TEXT NOT NULL DEFAULT 'queued',
            stage_progress INTEGER NOT NULL DEFAULT 0,
            worker_id TEXT NOT NULL DEFAULT '',
            last_heartbeat_at TEXT,
            lease_expires_at TEXT,
            recovery_note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            batch_id TEXT NOT NULL DEFAULT '',
            client_item_id TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT 'Uncategorized',
            upload_verified INTEGER NOT NULL DEFAULT 0,
            drive_status TEXT NOT NULL DEFAULT 'pending',
            drive_remote_path TEXT NOT NULL DEFAULT '',
            drive_error TEXT NOT NULL DEFAULT '',
            drive_uploaded_at TEXT,
            drive_verified INTEGER NOT NULL DEFAULT 0,
            local_cleanup_status TEXT NOT NULL DEFAULT 'retained',
            FOREIGN KEY(project_id) REFERENCES projects(id)
        );

        CREATE TABLE IF NOT EXISTS app_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            detail_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS upload_batches (
            id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            status TEXT NOT NULL,
            expected_count INTEGER NOT NULL DEFAULT 0,
            staged_count INTEGER NOT NULL DEFAULT 0,
            verified_count INTEGER NOT NULL DEFAULT 0,
            category TEXT NOT NULL DEFAULT 'Uncategorized',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            finalized_at TEXT,
            error_text TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS batch_items (
            id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL,
            client_item_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            job_id TEXT NOT NULL,
            project_name TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'Uncategorized',
            upload_verified INTEGER NOT NULL DEFAULT 0,
            script_sha256 TEXT NOT NULL DEFAULT '',
            audio_sha256 TEXT NOT NULL DEFAULT '',
            script_size INTEGER NOT NULL DEFAULT 0,
            audio_size INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(batch_id, client_item_id),
            FOREIGN KEY(batch_id) REFERENCES upload_batches(id),
            FOREIGN KEY(project_id) REFERENCES projects(id),
            FOREIGN KEY(job_id) REFERENCES jobs(id)
        );
        """
    )


def _migration_002_phase1_runtime(db: sqlite3.Connection) -> None:
    if not _table_exists(db, "projects") or not _table_exists(db, "jobs"):
        _migration_001_initial(db)

    job_columns = {
        "settings_json": "TEXT NOT NULL DEFAULT '{}'",
        "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
        "attempt": "INTEGER NOT NULL DEFAULT 1",
        "active_attempt_id": "TEXT",
        "output_path": "TEXT NOT NULL DEFAULT ''",
        "preview_path": "TEXT NOT NULL DEFAULT ''",
        "log_path": "TEXT NOT NULL DEFAULT ''",
        "error_text": "TEXT NOT NULL DEFAULT ''",
        "current_stage": "TEXT NOT NULL DEFAULT 'queued'",
        "stage_progress": "INTEGER NOT NULL DEFAULT 0",
        "worker_id": "TEXT NOT NULL DEFAULT ''",
        "last_heartbeat_at": "TEXT",
        "lease_expires_at": "TEXT",
        "recovery_note": "TEXT NOT NULL DEFAULT ''",
        "started_at": "TEXT",
        "completed_at": "TEXT",
        "batch_id": "TEXT NOT NULL DEFAULT ''",
        "client_item_id": "TEXT NOT NULL DEFAULT ''",
        "category": "TEXT NOT NULL DEFAULT 'Uncategorized'",
        "upload_verified": "INTEGER NOT NULL DEFAULT 0",
        "drive_status": "TEXT NOT NULL DEFAULT 'pending'",
        "drive_remote_path": "TEXT NOT NULL DEFAULT ''",
        "drive_error": "TEXT NOT NULL DEFAULT ''",
        "drive_uploaded_at": "TEXT",
        "drive_verified": "INTEGER NOT NULL DEFAULT 0",
        "local_cleanup_status": "TEXT NOT NULL DEFAULT 'retained'",
    }
    for name, ddl in job_columns.items():
        _add_column(db, "jobs", name, ddl)

    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS job_attempts (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            attempt_number INTEGER NOT NULL,
            status TEXT NOT NULL,
            worker_id TEXT NOT NULL DEFAULT '',
            current_stage TEXT NOT NULL DEFAULT 'queued',
            progress INTEGER NOT NULL DEFAULT 0,
            stage_progress INTEGER NOT NULL DEFAULT 0,
            message TEXT NOT NULL DEFAULT '',
            error_text TEXT NOT NULL DEFAULT '',
            lease_expires_at TEXT,
            heartbeat_at TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            UNIQUE(job_id, attempt_number),
            FOREIGN KEY(job_id) REFERENCES jobs(id)
        );

        CREATE TABLE IF NOT EXISTS job_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            attempt_id TEXT,
            event_type TEXT NOT NULL,
            from_status TEXT NOT NULL DEFAULT '',
            to_status TEXT NOT NULL DEFAULT '',
            stage TEXT NOT NULL DEFAULT '',
            progress INTEGER NOT NULL DEFAULT 0,
            message TEXT NOT NULL DEFAULT '',
            detail_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY(job_id) REFERENCES jobs(id),
            FOREIGN KEY(attempt_id) REFERENCES job_attempts(id)
        );

        CREATE INDEX IF NOT EXISTS idx_jobs_status_created
            ON jobs(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_jobs_project
            ON jobs(project_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_attempts_job
            ON job_attempts(job_id, attempt_number);
        CREATE INDEX IF NOT EXISTS idx_attempts_lease
            ON job_attempts(status, lease_expires_at);
        CREATE INDEX IF NOT EXISTS idx_events_job_sequence
            ON job_events(job_id, id);
        """
    )


def _normalize_legacy_status(value: str) -> str:
    status = (value or "").strip().lower()
    aliases = {
        "pending": "queued",
        "waiting": "queued",
        "processing": "interrupted",
        "rendering": "interrupted",
        "done": "completed",
    }
    status = aliases.get(status, status)
    if status not in {
        "held", "queued", "running", "interrupted",
        "completed", "failed", "cancelled",
    }:
        return "failed"
    return status


def _migration_003_phase1_backfill(db: sqlite3.Connection) -> None:
    rows = db.execute("SELECT * FROM jobs ORDER BY created_at, id").fetchall()
    for row in rows:
        job = dict(row)
        old_status = str(job.get("status") or "queued")
        status = _normalize_legacy_status(old_status)
        attempt_number = max(1, int(job.get("attempt") or 1))
        attempt_id = str(job.get("active_attempt_id") or "").strip()
        existing = db.execute(
            "SELECT * FROM job_attempts WHERE job_id=? ORDER BY attempt_number DESC LIMIT 1",
            (job["id"],),
        ).fetchone()
        if existing is None:
            attempt_id = attempt_id or uuid.uuid4().hex
            ts = str(job.get("updated_at") or job.get("created_at") or now_iso())
            finished = ts if status in {"completed", "failed", "cancelled", "interrupted"} else None
            db.execute(
                """
                INSERT INTO job_attempts (
                    id, job_id, attempt_number, status, worker_id,
                    current_stage, progress, stage_progress, message, error_text,
                    lease_expires_at, heartbeat_at, created_at, started_at,
                    finished_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id, job["id"], attempt_number, status,
                    str(job.get("worker_id") or ""),
                    str(job.get("current_stage") or status),
                    int(job.get("progress") or 0),
                    int(job.get("stage_progress") or 0),
                    str(job.get("message") or ""),
                    str(job.get("error_text") or ""),
                    job.get("last_heartbeat_at"),
                    str(job.get("created_at") or ts),
                    job.get("started_at"), finished, ts,
                ),
            )
        else:
            attempt_id = str(existing["id"])
            attempt_number = int(existing["attempt_number"])

        recovery_note = str(job.get("recovery_note") or "")
        if old_status.lower() in {"rendering", "processing", "running"}:
            recovery_note = recovery_note or "Legacy active job interrupted during Phase 1 migration; eligible for explicit startup recovery."
        error_text = str(job.get("error_text") or "")
        if status == "failed" and old_status.lower() not in {"failed"}:
            error_text = error_text or f"Unsupported legacy job status: {old_status}"

        db.execute(
            """
            UPDATE jobs
            SET status=?, attempt=?, active_attempt_id=?, current_stage=?,
                worker_id='', lease_expires_at=NULL, recovery_note=?,
                error_text=?, updated_at=?
            WHERE id=?
            """,
            (
                status, attempt_number, attempt_id,
                str(job.get("current_stage") or status), recovery_note,
                error_text, now_iso(), job["id"],
            ),
        )
        if db.execute(
            "SELECT 1 FROM job_events WHERE job_id=? LIMIT 1", (job["id"],)
        ).fetchone() is None:
            db.execute(
                """
                INSERT INTO job_events (
                    job_id, attempt_id, event_type, from_status, to_status,
                    stage, progress, message, detail_json, created_at
                ) VALUES (?, ?, 'migration_import', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job["id"], attempt_id, old_status, status,
                    str(job.get("current_stage") or status),
                    int(job.get("progress") or 0),
                    "Imported existing job into Phase 1 attempt/event model",
                    json.dumps({"legacy_status": old_status}, separators=(",", ":")),
                    now_iso(),
                ),
            )


def _migration_004_drive_ingress(db: sqlite3.Connection) -> None:
    """Durable Desktop -> Drive -> VPS ingestion state."""
    for name, ddl in {
        "input_transport": "TEXT NOT NULL DEFAULT 'direct_upload'",
        "drive_input_status": "TEXT NOT NULL DEFAULT 'not_required'",
        "drive_input_folder": "TEXT NOT NULL DEFAULT ''",
        "drive_manifest_sha256": "TEXT NOT NULL DEFAULT ''",
        "drive_input_verified": "INTEGER NOT NULL DEFAULT 0",
    }.items():
        _add_column(db, "jobs", name, ddl)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS drive_ingress (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL UNIQUE,
            job_id TEXT NOT NULL UNIQUE,
            owner_id TEXT NOT NULL,
            remote_folder TEXT NOT NULL,
            manifest_sha256 TEXT NOT NULL,
            manifest_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            worker_id TEXT NOT NULL DEFAULT '',
            lease_expires_at TEXT,
            heartbeat_at TEXT,
            attempt INTEGER NOT NULL DEFAULT 1,
            file_count INTEGER NOT NULL DEFAULT 0,
            bytes_total INTEGER NOT NULL DEFAULT 0,
            bytes_downloaded INTEGER NOT NULL DEFAULT 0,
            error_text TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            FOREIGN KEY(project_id) REFERENCES projects(id),
            FOREIGN KEY(job_id) REFERENCES jobs(id)
        );
        CREATE INDEX IF NOT EXISTS idx_drive_ingress_status_created
            ON drive_ingress(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_drive_ingress_lease
            ON drive_ingress(status, lease_expires_at);
        """
    )


def _migration_005_batch_pipeline(db: sqlite3.Connection) -> None:
    """Durable batch rotation and independent transcription/render/publish stages."""
    for name, ddl in {
        "pipeline_stage": "TEXT NOT NULL DEFAULT 'ready_transcribe'",
        "transcription_path": "TEXT NOT NULL DEFAULT ''",
        "render_verified": "INTEGER NOT NULL DEFAULT 0",
        "drive_web_url": "TEXT NOT NULL DEFAULT ''",
    }.items():
        _add_column(db, "jobs", name, ddl)
    for name, ddl in {
        "settings_json": "TEXT NOT NULL DEFAULT '{}'",
        "processing_started_at": "TEXT",
        "completed_at": "TEXT",
    }.items():
        _add_column(db, "upload_batches", name, ddl)
    _add_column(db, "batch_items", "item_order", "INTEGER NOT NULL DEFAULT 0")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS scheduler_state (
            name TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_jobs_pipeline
            ON jobs(status, pipeline_stage, batch_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_batches_rotation
            ON upload_batches(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_batch_items_order
            ON batch_items(batch_id, item_order, created_at);
        """
    )
    db.execute(
        """UPDATE jobs SET pipeline_stage=CASE
             WHEN drive_verified=1 THEN 'completed'
             WHEN status='completed' THEN 'completed'
             WHEN current_stage IN ('publishing','ready_to_publish') THEN 'ready_publish'
             WHEN current_stage IN ('rendering','validating','generating_preview') THEN 'ready_render'
             ELSE 'ready_transcribe' END
           WHERE pipeline_stage='' OR pipeline_stage='ready_transcribe'"""
    )
    for batch in db.execute("SELECT id FROM upload_batches ORDER BY created_at,id").fetchall():
        rows = db.execute(
            "SELECT id FROM batch_items WHERE batch_id=? ORDER BY created_at,id", (batch["id"],)
        ).fetchall()
        for position, row in enumerate(rows, 1):
            db.execute("UPDATE batch_items SET item_order=? WHERE id=?", (position, row["id"]))


def _migration_006_acs_control_and_shared_assets(db: sqlite3.Connection) -> None:
    """Additive ACS control/shared-asset schema; legacy queue rows are never rewritten."""
    for name, ddl in {
        "settings_version": "INTEGER NOT NULL DEFAULT 1",
        "submission_protocol_version": "INTEGER NOT NULL DEFAULT 1",
        "idempotency_key": "TEXT NOT NULL DEFAULT ''",
        "fg_name": "TEXT NOT NULL DEFAULT ''",
        "fg_sha256": "TEXT NOT NULL DEFAULT ''",
        "finalized_settings_sha256": "TEXT NOT NULL DEFAULT ''",
    }.items():
        _add_column(db, "upload_batches", name, ddl)
    for name, ddl in {
        "priority": "INTEGER NOT NULL DEFAULT 0",
        "queue_sequence": "INTEGER NOT NULL DEFAULT 0",
        "cancel_after_upload_attempt": "INTEGER NOT NULL DEFAULT 0",
        "cleanup_eligible_at": "TEXT",
        "cleanup_error": "TEXT NOT NULL DEFAULT ''",
    }.items():
        _add_column(db, "jobs", name, ddl)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS batch_upload_chunks (
            id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, chunk_index INTEGER NOT NULL,
            idempotency_key TEXT NOT NULL, project_count INTEGER NOT NULL,
            payload_sha256 TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'accepted',
            created_at TEXT NOT NULL, UNIQUE(batch_id, chunk_index),
            UNIQUE(batch_id, idempotency_key), FOREIGN KEY(batch_id) REFERENCES upload_batches(id)
        );
        CREATE TABLE IF NOT EXISTS shared_fg_assets (
            name TEXT PRIMARY KEY, category TEXT NOT NULL, relative_path TEXT NOT NULL UNIQUE,
            sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL, width INTEGER NOT NULL DEFAULT 0,
            height INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'available',
            modified_at TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS cleanup_runtime (
            name TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_jobs_priority_queue
            ON jobs(status, pipeline_stage, priority DESC, queue_sequence, created_at);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_batches_idempotency
            ON upload_batches(owner_id, idempotency_key) WHERE idempotency_key<>'';
        """
    )
    db.execute(
        "INSERT OR IGNORE INTO cleanup_runtime(name,value,updated_at) VALUES('generation_started_at',?,?)",
        (now_iso(), now_iso()),
    )
    next_sequence = int(db.execute("SELECT COALESCE(MAX(queue_sequence),0) FROM jobs").fetchone()[0])
    for row in db.execute("SELECT id FROM jobs WHERE queue_sequence=0 ORDER BY created_at,id").fetchall():
        next_sequence += 1
        db.execute("UPDATE jobs SET queue_sequence=? WHERE id=?", (next_sequence, row["id"]))


def _migration_007_acs_operational_controls(db: sqlite3.Connection) -> None:
    _add_column(db, "upload_batches", "control_state", "TEXT NOT NULL DEFAULT 'active'")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS acs_pipeline_controls (
            name TEXT PRIMARY KEY, paused INTEGER NOT NULL DEFAULT 0,
            actor TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS acs_control_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL,
            action TEXT NOT NULL, target_type TEXT NOT NULL, target_id TEXT NOT NULL,
            detail_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_acs_control_audit_created
            ON acs_control_audit(created_at DESC,id DESC);
        """
    )
    for name in ("voice", "transcribe", "render", "upload"):
        db.execute(
            "INSERT OR IGNORE INTO acs_pipeline_controls(name,paused,actor,updated_at) VALUES(?,0,'',?)",
            (name, now_iso()),
        )


def _migration_2026081501_separated_workers(db: sqlite3.Connection) -> None:
    """Add durable compressor/upload queues and observable worker runtime state."""
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS acs_worker_runtime (
            name TEXT PRIMARY KEY,
            worker_id TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT 'offline',
            current_job_id TEXT NOT NULL DEFAULT '',
            last_heartbeat TEXT NOT NULL,
            started_at TEXT NOT NULL,
            message TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_acs_worker_runtime_heartbeat
            ON acs_worker_runtime(last_heartbeat);
        """
    )
    db.execute(
        """INSERT OR IGNORE INTO acs_pipeline_controls(name,paused,actor,updated_at)
           VALUES('compress',0,'',?)""",
        (now_iso(),),
    )
    # ready_publish was the previous name for the dedicated Drive upload queue.
    # Only non-terminal rows need remapping; historical completed rows are evidence.
    db.execute(
        """UPDATE jobs SET pipeline_stage='ready_upload',updated_at=?
           WHERE pipeline_stage='ready_publish'
             AND status NOT IN ('completed','cancelled')""",
        (now_iso(),),
    )


MIGRATIONS: tuple[tuple[int, str, Migration], ...] = (
    (1, "initial_schema", _migration_001_initial),
    (2, "phase1_runtime_schema", _migration_002_phase1_runtime),
    (3, "phase1_attempt_event_backfill", _migration_003_phase1_backfill),
    (4, "drive_ingress", _migration_004_drive_ingress),
    (5, "batch_pipeline", _migration_005_batch_pipeline),
    (6, "acs_control_and_shared_assets", _migration_006_acs_control_and_shared_assets),
    (7, "acs_operational_controls", _migration_007_acs_operational_controls),
    (2026081501, "worker_separation_20260815_v1", _migration_2026081501_separated_workers),
)


def _migration_checksum(name: str, migration: Migration) -> str:
    source = inspect.getsource(migration)
    return hashlib.sha256(f"{name}\n{source}".encode("utf-8")).hexdigest()


def run_migrations() -> list[int]:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=30.0)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=30000")
    applied_now: list[int] = []
    try:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                checksum TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
        db.commit()
        existing = {
            int(row["version"]): dict(row)
            for row in db.execute("SELECT * FROM schema_migrations")
        }
        for version, name, migration in MIGRATIONS:
            checksum = _migration_checksum(name, migration)
            if version in existing:
                if str(existing[version]["checksum"]) != checksum:
                    raise RuntimeError(
                        f"Migration checksum mismatch for version {version}: {name}"
                    )
                continue
            db.execute("BEGIN IMMEDIATE")
            try:
                migration(db)
                db.execute(
                    "INSERT INTO schema_migrations(version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
                    (version, name, checksum, now_iso()),
                )
                db.commit()
                applied_now.append(version)
            except Exception:
                db.rollback()
                raise
        db.execute("PRAGMA journal_mode=WAL")
        db.commit()
    finally:
        db.close()
    return applied_now


def migration_status() -> list[dict]:
    with connect() as db:
        if not _table_exists(db, "schema_migrations"):
            return []
        return [
            dict(row)
            for row in db.execute(
                "SELECT version, name, checksum, applied_at FROM schema_migrations ORDER BY version"
            )
        ]

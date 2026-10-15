from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any
import uuid

from .config import lease_seconds as configured_lease_seconds, max_attempts
from .database import connect, initialize_storage, now_iso
from .job_states import (
    CANCELLED,
    COMPLETED,
    FAILED,
    HELD,
    INTERRUPTED,
    QUEUED,
    RETRYABLE_STATES,
    RUNNING,
    TERMINAL_STATES,
    InvalidJobTransition,
    validate_transition,
)


class JobNotFound(KeyError):
    pass


class JobConflict(RuntimeError):
    pass


def _lease_expiry(seconds: int | None = None) -> str:
    duration = max(15, int(seconds or configured_lease_seconds()))
    return (datetime.now(timezone.utc) + timedelta(seconds=duration)).isoformat()


def _json_settings(value: dict[str, Any] | None) -> str:
    return json.dumps(value or {}, ensure_ascii=False, separators=(",", ":"))


def _decode_job(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["settings"] = json.loads(item.pop("settings_json", "{}") or "{}")
    item["cancel_requested"] = bool(item.get("cancel_requested"))
    item["drive_verified"] = bool(item.get("drive_verified"))
    item["upload_verified"] = bool(item.get("upload_verified"))
    return item


def _event(
    db: sqlite3.Connection,
    *,
    job_id: str,
    attempt_id: str | None,
    event_type: str,
    from_status: str = "",
    to_status: str = "",
    stage: str = "",
    progress: int = 0,
    message: str = "",
    detail: dict[str, Any] | None = None,
    created_at: str | None = None,
) -> None:
    db.execute(
        """
        INSERT INTO job_events (
            job_id, attempt_id, event_type, from_status, to_status,
            stage, progress, message, detail_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            attempt_id,
            event_type,
            from_status,
            to_status,
            stage,
            max(0, min(100, int(progress))),
            str(message),
            json.dumps(detail or {}, ensure_ascii=False, separators=(",", ":")),
            created_at or now_iso(),
        ),
    )


def _create_attempt(
    db: sqlite3.Connection,
    *,
    job_id: str,
    attempt_number: int,
    status: str,
    stage: str,
    message: str,
    created_at: str,
) -> str:
    attempt_id = uuid.uuid4().hex
    db.execute(
        """
        INSERT INTO job_attempts (
            id, job_id, attempt_number, status, current_stage,
            progress, stage_progress, message, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 0, 0, ?, ?, ?)
        """,
        (
            attempt_id,
            job_id,
            int(attempt_number),
            status,
            stage,
            message,
            created_at,
            created_at,
        ),
    )
    return attempt_id


def create_job_record(
    db: sqlite3.Connection,
    *,
    project: sqlite3.Row | dict[str, Any],
    settings: dict[str, Any],
    initial_status: str = QUEUED,
    job_id: str | None = None,
    batch_id: str = "",
    client_item_id: str = "",
    category: str = "Uncategorized",
    upload_verified: bool = False,
) -> str:
    if initial_status not in {HELD, QUEUED}:
        raise ValueError(f"New jobs may only start held or queued, not {initial_status}")
    job_id = job_id or uuid.uuid4().hex
    ts = now_iso()
    project_item = dict(project)
    project_path = Path(project_item["project_path"])
    log_path = project_path / "logs" / f"job_{job_id}.log"
    output_path = project_path / "output" / f"{job_id}.mp4"
    preview_path = project_path / "preview" / f"{job_id}_preview.mp4"
    message = "VPS Ready — waiting for complete batch" if initial_status == HELD else "Queued"
    stage = "held" if initial_status == HELD else "queued"
    db.execute(
        """
        INSERT INTO jobs (
            id, project_id, status, progress, message, settings_json,
            cancel_requested, attempt, active_attempt_id,
            output_path, preview_path, log_path, error_text,
            current_stage, stage_progress, created_at, updated_at,
            batch_id, client_item_id, category, upload_verified,
            drive_status, drive_remote_path, drive_error, drive_verified,
            local_cleanup_status
        ) VALUES (?, ?, ?, 0, ?, ?, 0, 1, NULL, ?, ?, ?, '', ?, 0, ?, ?, ?, ?, ?, ?,
                  'pending', '', '', 0, 'retained')
        """,
        (
            job_id,
            project_item["id"],
            initial_status,
            message,
            _json_settings(settings),
            str(output_path),
            str(preview_path),
            str(log_path),
            stage,
            ts,
            ts,
            str(batch_id),
            str(client_item_id),
            str(category or "Uncategorized"),
            1 if upload_verified else 0,
        ),
    )
    attempt_id = _create_attempt(
        db,
        job_id=job_id,
        attempt_number=1,
        status=initial_status,
        stage=stage,
        message=message,
        created_at=ts,
    )
    db.execute(
        "UPDATE jobs SET active_attempt_id=? WHERE id=?",
        (attempt_id, job_id),
    )
    _event(
        db,
        job_id=job_id,
        attempt_id=attempt_id,
        event_type="job_created",
        to_status=initial_status,
        stage=stage,
        message=message,
    )
    return job_id


def create_job(project_id: str, settings: dict[str, Any]) -> dict[str, Any]:
    initialize_storage()
    with connect() as db:
        project = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if project is None:
            raise JobNotFound("Project not found")
        db.execute("BEGIN IMMEDIATE")
        job_id = create_job_record(db, project=project, settings=settings)
        db.commit()
    return get_job(job_id)


def get_job(job_id: str) -> dict[str, Any]:
    initialize_storage()
    with connect() as db:
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise JobNotFound("Job not found")
    return _decode_job(row)


def list_jobs(
    project_id: str | None = None,
    *,
    owner_id: str | None = None,
    include_all_owners: bool = False,
) -> list[dict[str, Any]]:
    initialize_storage()
    query = "SELECT j.* FROM jobs j JOIN projects p ON p.id=j.project_id WHERE 1=1"
    params: list[Any] = []
    if project_id:
        query += " AND j.project_id=?"
        params.append(project_id)
    if owner_id and not include_all_owners:
        query += " AND p.owner_id=?"
        params.append(owner_id)
    query += " ORDER BY j.created_at DESC"
    with connect() as db:
        rows = db.execute(query, params).fetchall()
    return [_decode_job(row) for row in rows]


def list_attempts(job_id: str) -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM job_attempts WHERE job_id=? ORDER BY attempt_number",
            (job_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_events(job_id: str, after_id: int = 0, limit: int = 500) -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute(
            """
            SELECT * FROM job_events
            WHERE job_id=? AND id>?
            ORDER BY id ASC LIMIT ?
            """,
            (job_id, max(0, int(after_id)), max(1, min(2000, int(limit)))),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["detail"] = json.loads(item.pop("detail_json") or "{}")
        except json.JSONDecodeError:
            item["detail"] = {}
            item.pop("detail_json", None)
        result.append(item)
    return result


def transition_job_in_db(
    db: sqlite3.Connection,
    job_id: str,
    target_status: str,
    *,
    stage: str | None = None,
    progress: int | None = None,
    message: str = "",
    error_text: str | None = None,
    worker_id: str | None = None,
    lease_expires_at: str | None = None,
    event_type: str = "state_changed",
    detail: dict[str, Any] | None = None,
) -> None:
    row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise JobNotFound("Job not found")
    current = str(row["status"])
    validate_transition(current, target_status)
    ts = now_iso()
    stage_value = str(stage or row["current_stage"] or target_status)
    progress_value = int(row["progress"] if progress is None else progress)
    terminal = target_status in TERMINAL_STATES or target_status == INTERRUPTED
    started_at = row["started_at"]
    if target_status == RUNNING and not started_at:
        started_at = ts
    completed_at = ts if terminal else None
    next_worker = str(worker_id if worker_id is not None else row["worker_id"] or "")
    next_lease = lease_expires_at if target_status == RUNNING else None
    db.execute(
        """
        UPDATE jobs
        SET status=?, current_stage=?, progress=?, stage_progress=?, message=?,
            error_text=?, worker_id=?, lease_expires_at=?,
            last_heartbeat_at=?, started_at=?, completed_at=?, updated_at=?
        WHERE id=?
        """,
        (
            target_status,
            stage_value,
            max(0, min(100, progress_value)),
            max(0, min(100, progress_value)),
            str(message or row["message"] or target_status),
            str(row["error_text"] if error_text is None else error_text),
            next_worker,
            next_lease,
            ts if target_status == RUNNING else row["last_heartbeat_at"],
            started_at,
            completed_at,
            ts,
            job_id,
        ),
    )
    attempt_id = str(row["active_attempt_id"] or "") or None
    if attempt_id:
        db.execute(
            """
            UPDATE job_attempts
            SET status=?, worker_id=?, current_stage=?, progress=?,
                stage_progress=?, message=?, error_text=?, lease_expires_at=?,
                heartbeat_at=?, started_at=COALESCE(started_at, ?),
                finished_at=?, updated_at=?
            WHERE id=?
            """,
            (
                target_status,
                next_worker,
                stage_value,
                max(0, min(100, progress_value)),
                max(0, min(100, progress_value)),
                str(message or row["message"] or target_status),
                str(row["error_text"] if error_text is None else error_text),
                next_lease,
                ts if target_status == RUNNING else row["last_heartbeat_at"],
                ts if target_status == RUNNING else None,
                completed_at,
                ts,
                attempt_id,
            ),
        )
    _event(
        db,
        job_id=job_id,
        attempt_id=attempt_id,
        event_type=event_type,
        from_status=current,
        to_status=target_status,
        stage=stage_value,
        progress=progress_value,
        message=str(message or target_status),
        detail=detail,
        created_at=ts,
    )


def transition_job(job_id: str, target_status: str, **kwargs: Any) -> dict[str, Any]:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        transition_job_in_db(db, job_id, target_status, **kwargs)
        db.commit()
    return get_job(job_id)


def claim_next_job(worker_id: str, lease_seconds: int | None = None) -> dict[str, Any] | None:
    initialize_storage()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            """
            SELECT * FROM jobs
            WHERE status=? AND cancel_requested=0
            ORDER BY created_at ASC LIMIT 1
            """,
            (QUEUED,),
        ).fetchone()
        if row is None:
            db.commit()
            return None
        transition_job_in_db(
            db,
            str(row["id"]),
            RUNNING,
            stage="preparing",
            progress=1,
            message="Worker claimed job",
            worker_id=worker_id,
            lease_expires_at=_lease_expiry(lease_seconds),
            event_type="job_claimed",
            detail={"worker_id": worker_id},
        )
        db.commit()
        job_id = str(row["id"])
    return get_job(job_id)


def heartbeat_job(
    job_id: str,
    worker_id: str,
    *,
    stage: str | None = None,
    lease_seconds: int | None = None,
) -> bool:
    ts = now_iso()
    expiry = _lease_expiry(lease_seconds)
    with connect() as db:
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None or row["status"] != RUNNING or row["worker_id"] != worker_id:
            return False
        next_stage = str(stage or row["current_stage"] or "running")
        db.execute(
            """
            UPDATE jobs SET last_heartbeat_at=?, lease_expires_at=?,
                current_stage=?, updated_at=? WHERE id=?
            """,
            (ts, expiry, next_stage, ts, job_id),
        )
        if row["active_attempt_id"]:
            db.execute(
                """
                UPDATE job_attempts SET heartbeat_at=?, lease_expires_at=?,
                    current_stage=?, updated_at=? WHERE id=?
                """,
                (ts, expiry, next_stage, ts, row["active_attempt_id"]),
            )
        db.commit()
    return True


def record_progress(
    job_id: str,
    worker_id: str,
    *,
    progress: int,
    stage: str,
    message: str,
    stage_progress: int | None = None,
) -> None:
    ts = now_iso()
    expiry = _lease_expiry()
    overall = max(1, min(99, int(progress)))
    stage_value = max(0, min(100, int(stage_progress if stage_progress is not None else progress)))
    with connect() as db:
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise JobNotFound("Job not found")
        if row["status"] != RUNNING or row["worker_id"] != worker_id:
            raise JobConflict("Worker no longer owns this job lease")
        db.execute(
            """
            UPDATE jobs SET progress=?, stage_progress=?, current_stage=?,
                message=?, last_heartbeat_at=?, lease_expires_at=?, updated_at=?
            WHERE id=?
            """,
            (overall, stage_value, stage, message, ts, expiry, ts, job_id),
        )
        attempt_id = str(row["active_attempt_id"] or "") or None
        if attempt_id:
            db.execute(
                """
                UPDATE job_attempts SET progress=?, stage_progress=?,
                    current_stage=?, message=?, heartbeat_at=?,
                    lease_expires_at=?, updated_at=? WHERE id=?
                """,
                (overall, stage_value, stage, message, ts, expiry, ts, attempt_id),
            )
        _event(
            db,
            job_id=job_id,
            attempt_id=attempt_id,
            event_type="progress",
            from_status=RUNNING,
            to_status=RUNNING,
            stage=stage,
            progress=overall,
            message=message,
            created_at=ts,
        )
        db.commit()


def is_cancel_requested(job_id: str) -> bool:
    with connect() as db:
        row = db.execute(
            "SELECT cancel_requested, status FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
    return bool(row and (row["cancel_requested"] or row["status"] == CANCELLED))


def finish_job(
    job_id: str,
    worker_id: str,
    status: str,
    *,
    message: str,
    error_text: str = "",
) -> dict[str, Any]:
    if status not in {COMPLETED, FAILED, CANCELLED}:
        raise ValueError("finish_job requires a terminal status")
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise JobNotFound("Job not found")
        if row["status"] != RUNNING:
            raise JobConflict(f"Cannot finish job from state {row['status']}")
        if row["worker_id"] != worker_id:
            raise JobConflict("Worker no longer owns this job")
        transition_job_in_db(
            db,
            job_id,
            status,
            stage=status,
            progress=100 if status == COMPLETED else int(row["progress"] or 0),
            message=message,
            error_text=error_text,
            worker_id="",
            event_type="attempt_finished",
        )
        db.execute(
            "UPDATE jobs SET cancel_requested=0, worker_id='', lease_expires_at=NULL WHERE id=?",
            (job_id,),
        )
        db.commit()
    return get_job(job_id)


def release_job_to_queue(
    job_id: str,
    worker_id: str,
    *,
    pipeline_stage: str,
    progress: int,
    message: str,
) -> dict[str, Any]:
    """Finish one non-terminal Phase 3 stage without creating a retry attempt."""
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise JobNotFound("Job not found")
        if row["status"] != RUNNING or row["worker_id"] != worker_id:
            raise JobConflict("Worker no longer owns this pipeline stage")
        transition_job_in_db(
            db, job_id, QUEUED, stage=pipeline_stage, progress=progress,
            message=message, worker_id="", event_type="pipeline_stage_completed",
            detail={"next_stage": pipeline_stage},
        )
        db.execute(
            """UPDATE jobs SET pipeline_stage=?,worker_id='',lease_expires_at=NULL,
               last_heartbeat_at=NULL,updated_at=? WHERE id=?""",
            (pipeline_stage, now_iso(), job_id),
        )
        db.commit()
    return get_job(job_id)


def interrupt_job(job_id: str, worker_id: str, reason: str) -> dict[str, Any]:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise JobNotFound("Job not found")
        if row["status"] != RUNNING or row["worker_id"] != worker_id:
            raise JobConflict("Worker no longer owns this job")
        transition_job_in_db(
            db,
            job_id,
            INTERRUPTED,
            stage="interrupted",
            progress=int(row["progress"] or 0),
            message=reason,
            error_text=reason,
            worker_id="",
            event_type="worker_interrupted",
        )
        db.execute(
            """
            UPDATE jobs SET worker_id='', lease_expires_at=NULL,
                recovery_note=?, updated_at=? WHERE id=?
            """,
            (reason, now_iso(), job_id),
        )
        db.commit()
    return get_job(job_id)


def request_cancel(job_id: str) -> dict[str, Any]:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise JobNotFound("Job not found")
        status = str(row["status"])
        if status in TERMINAL_STATES:
            db.commit()
            return _decode_job(row)
        db.execute(
            "UPDATE jobs SET cancel_requested=1, message='Cancellation requested', updated_at=? WHERE id=?",
            (now_iso(), job_id),
        )
        _event(
            db,
            job_id=job_id,
            attempt_id=str(row["active_attempt_id"] or "") or None,
            event_type="cancel_requested",
            from_status=status,
            to_status=status,
            stage=str(row["current_stage"] or status),
            progress=int(row["progress"] or 0),
            message="Cancellation requested",
        )
        if status in {HELD, QUEUED, INTERRUPTED}:
            transition_job_in_db(
                db,
                job_id,
                CANCELLED,
                stage="cancelled",
                message="Cancelled before processing",
                event_type="attempt_finished",
            )
            db.execute("UPDATE jobs SET cancel_requested=0 WHERE id=?", (job_id,))
        db.commit()
    return get_job(job_id)


def retry_job(job_id: str) -> dict[str, Any]:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            """
            SELECT j.*, p.project_path AS owner_project_path
            FROM jobs j JOIN projects p ON p.id=j.project_id
            WHERE j.id=?
            """,
            (job_id,),
        ).fetchone()
        if row is None:
            raise JobNotFound("Job not found")
        current = str(row["status"])
        if current not in RETRYABLE_STATES:
            raise InvalidJobTransition(f"Job in state {current} cannot be retried")
        ingress = db.execute(
            "SELECT * FROM drive_ingress WHERE job_id=?", (job_id,)
        ).fetchone()
        needs_drive_download = bool(ingress is not None and ingress["status"] != "ready")
        retry_status = HELD if needs_drive_download else QUEUED
        retry_stage = "waiting_for_drive_download" if needs_drive_download else "queued"
        retry_message = "Waiting to retry Drive download" if needs_drive_download else "Queued for retry"
        validate_transition(current, retry_status)
        project_root = Path(str(row["owner_project_path"])).resolve()
        removed_artifacts: list[str] = []
        for field in ("output_path", "preview_path"):
            value = str(row[field] or "").strip()
            if not value:
                continue
            artifact = Path(value).resolve()
            try:
                artifact.relative_to(project_root)
            except ValueError as exc:
                raise JobConflict(
                    f"Refusing to clean retry artifact outside project: {artifact}"
                ) from exc
            if artifact.exists():
                if not artifact.is_file():
                    raise JobConflict(f"Retry artifact is not a file: {artifact}")
                artifact.unlink()
                removed_artifacts.append(str(artifact))
        next_attempt = int(row["attempt"] or 1) + 1
        ts = now_iso()
        attempt_id = _create_attempt(
            db,
            job_id=job_id,
            attempt_number=next_attempt,
            status=retry_status,
            stage=retry_stage,
            message=retry_message,
            created_at=ts,
        )
        db.execute(
            """
            UPDATE jobs
            SET status=?, progress=0, stage_progress=0, current_stage=?,
                message=?, cancel_requested=0,
                attempt=?, active_attempt_id=?, error_text='',
                worker_id='', last_heartbeat_at=NULL, lease_expires_at=NULL,
                started_at=NULL, completed_at=NULL,
                drive_status='pending', drive_remote_path='', drive_error='',
                drive_web_url='',
                drive_uploaded_at=NULL, drive_verified=0,
                local_cleanup_status='retained', recovery_note='', updated_at=?
            WHERE id=?
            """,
            (retry_status, retry_stage, retry_message, next_attempt, attempt_id, ts, job_id),
        )
        try:
            retry_settings = json.loads(str(row["settings_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            retry_settings = {}
        retry_pipeline_stage = "ready_render" if not bool(retry_settings.get("caption_mode", True)) else "ready_transcribe"
        db.execute(
            "UPDATE jobs SET pipeline_stage=?,transcription_path='',render_verified=0 WHERE id=?",
            (retry_pipeline_stage, job_id),
        )
        if needs_drive_download:
            db.execute(
                """UPDATE drive_ingress SET status='pending', worker_id='',
                       lease_expires_at=NULL, heartbeat_at=NULL, attempt=attempt+1,
                       bytes_downloaded=0, error_text='', completed_at=NULL, updated_at=?
                   WHERE job_id=?""",
                (ts, job_id),
            )
            db.execute(
                "UPDATE jobs SET drive_input_status='pending',drive_input_verified=0 WHERE id=?",
                (job_id,),
            )
        _event(
            db,
            job_id=job_id,
            attempt_id=attempt_id,
            event_type="drive_retry_pending" if needs_drive_download else "retry_queued",
            from_status=current,
            to_status=retry_status,
            stage=retry_stage,
            message=f"Retry attempt {next_attempt}: {retry_message}",
            detail={
                "attempt_number": next_attempt,
                "removed_artifacts": removed_artifacts,
                "publication_state_reset": True,
            },
            created_at=ts,
        )
        db.commit()
    return get_job(job_id)


def reconcile_stale_jobs(
    *,
    auto_requeue: bool = True,
    maximum_attempts: int | None = None,
) -> list[dict[str, Any]]:
    initialize_storage()
    limit = max(1, int(maximum_attempts or max_attempts()))
    now = now_iso()
    recovered: list[dict[str, Any]] = []
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute(
            """
            SELECT * FROM jobs
            WHERE status=? AND (lease_expires_at IS NULL OR lease_expires_at<?)
            ORDER BY created_at
            """,
            (RUNNING, now),
        ).fetchall()
        for row in rows:
            job_id = str(row["id"])
            old_attempt_id = str(row["active_attempt_id"] or "") or None
            note = "Expired worker lease recovered at service startup"
            transition_job_in_db(
                db,
                job_id,
                INTERRUPTED,
                stage="interrupted",
                message=note,
                error_text=note,
                worker_id="",
                event_type="stale_lease_recovered",
                detail={
                    "previous_worker_id": str(row["worker_id"] or ""),
                    "lease_expires_at": row["lease_expires_at"],
                    "attempt_number": int(row["attempt"] or 1),
                },
            )
            db.execute(
                """
                UPDATE jobs SET recovery_note=?, worker_id='',
                    lease_expires_at=NULL, updated_at=? WHERE id=?
                """,
                (note, now, job_id),
            )
            action = "interrupted"
            if bool(row["drive_verified"]) and str(row["drive_remote_path"] or ""):
                transition_job_in_db(
                    db,
                    job_id,
                    COMPLETED,
                    stage="completed",
                    progress=100,
                    message="Recovered completed job from verified Drive publication",
                    worker_id="",
                    event_type="recovery_completed",
                    detail={"drive_remote_path": str(row["drive_remote_path"])},
                )
                action = "completed"
            elif auto_requeue and int(row["attempt"] or 1) < limit:
                validate_transition(INTERRUPTED, QUEUED)
                next_attempt = int(row["attempt"] or 1) + 1
                attempt_id = _create_attempt(
                    db,
                    job_id=job_id,
                    attempt_number=next_attempt,
                    status=QUEUED,
                    stage="queued",
                    message="Requeued after stale worker lease",
                    created_at=now,
                )
                db.execute(
                    """
                    UPDATE jobs SET status=?, progress=0, stage_progress=0,
                        current_stage='queued', message=?, cancel_requested=0,
                        attempt=?, active_attempt_id=?, error_text='',
                        worker_id='', last_heartbeat_at=NULL, lease_expires_at=NULL,
                        started_at=NULL, completed_at=NULL,
                        drive_status='pending', drive_remote_path='', drive_error='',
                        drive_uploaded_at=NULL, drive_verified=0,
                        local_cleanup_status='retained', updated_at=?
                    WHERE id=?
                    """,
                    (
                        QUEUED,
                        "Requeued after stale worker lease",
                        next_attempt,
                        attempt_id,
                        now,
                        job_id,
                    ),
                )
                _event(
                    db,
                    job_id=job_id,
                    attempt_id=attempt_id,
                    event_type="recovery_requeued",
                    from_status=INTERRUPTED,
                    to_status=QUEUED,
                    stage="queued",
                    message=f"Recovery created attempt {next_attempt}",
                    detail={"previous_attempt_id": old_attempt_id},
                    created_at=now,
                )
                action = "requeued"
            recovered.append({"job_id": job_id, "action": action})
        db.commit()
    return recovered


def update_job(job_id: str, **fields: Any) -> None:
    """Compatibility update for non-state metadata.

    State changes must use transition_job/finish_job/retry_job so attempts and
    events cannot diverge from the jobs row.
    """
    allowed = {
        "output_path", "preview_path", "log_path", "settings_json",
        "drive_status", "drive_remote_path", "drive_error",
        "drive_uploaded_at", "drive_verified", "local_cleanup_status",
        "recovery_note",
    }
    payload = {key: value for key, value in fields.items() if key in allowed}
    if not payload:
        return
    payload["updated_at"] = now_iso()
    columns = ", ".join(f"{key}=?" for key in payload)
    values = list(payload.values()) + [job_id]
    with connect() as db:
        db.execute(f"UPDATE jobs SET {columns} WHERE id=?", values)
        db.commit()


def ensure_queue_schema() -> None:
    initialize_storage()

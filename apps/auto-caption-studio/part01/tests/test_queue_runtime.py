from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


def test_retry_creates_new_attempt_and_clears_artifacts_and_publication_state(project_factory):
    project = project_factory()
    from web_backend.database import connect
    from web_backend.job_states import FAILED
    from web_backend.queue_store import (
        claim_next_job,
        create_job,
        finish_job,
        get_job,
        list_attempts,
        list_events,
        retry_job,
    )

    job = create_job(project["id"], {"caption_mode": True})
    claimed = claim_next_job("worker-a", lease_seconds=30)
    assert claimed and claimed["status"] == "running"
    finish_job(claimed["id"], "worker-a", FAILED, message="Failed", error_text="boom")
    failed = get_job(job["id"])
    output_path = Path(failed["output_path"])
    preview_path = Path(failed["preview_path"])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(b"stale output")
    preview_path.write_bytes(b"stale preview")
    with connect() as db:
        db.execute(
            """
            UPDATE jobs SET drive_status='completed', drive_remote_path='old.mp4',
                drive_error='old', drive_uploaded_at='old', drive_verified=1
            WHERE id=?
            """,
            (job["id"],),
        )
        db.commit()
    retried = retry_job(job["id"])
    assert retried["status"] == "queued"
    assert retried["attempt"] == 2
    assert retried["drive_status"] == "pending"
    assert retried["drive_remote_path"] == ""
    assert retried["drive_verified"] is False
    assert not output_path.exists()
    assert not preview_path.exists()
    attempts = list_attempts(job["id"])
    assert [attempt["attempt_number"] for attempt in attempts] == [1, 2]
    assert [attempt["status"] for attempt in attempts] == ["failed", "queued"]
    retry_event = [
        event for event in list_events(job["id"])
        if event["event_type"] == "retry_queued"
    ][0]
    assert sorted(retry_event["detail"]["removed_artifacts"]) == sorted(
        [str(output_path.resolve()), str(preview_path.resolve())]
    )


def test_completed_job_cannot_be_retried(project_factory):
    project_factory()
    from web_backend.job_states import COMPLETED, InvalidJobTransition
    from web_backend.queue_store import claim_next_job, create_job, finish_job, retry_job

    job = create_job("project-1", {})
    claim_next_job("worker-a")
    finish_job(job["id"], "worker-a", COMPLETED, message="Completed")
    with pytest.raises(InvalidJobTransition):
        retry_job(job["id"])


def test_stale_lease_recovery_records_evidence_and_new_attempt(project_factory):
    project_factory()
    from web_backend.database import connect
    from web_backend.queue_store import (
        claim_next_job,
        create_job,
        get_job,
        list_attempts,
        list_events,
        reconcile_stale_jobs,
    )

    job = create_job("project-1", {})
    claim_next_job("dead-worker", lease_seconds=15)
    expired = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    with connect() as db:
        db.execute(
            "UPDATE jobs SET lease_expires_at=?, last_heartbeat_at=? WHERE id=?",
            (expired, expired, job["id"]),
        )
        db.execute(
            "UPDATE job_attempts SET lease_expires_at=?, heartbeat_at=? WHERE job_id=?",
            (expired, expired, job["id"]),
        )
        db.commit()

    recovered = reconcile_stale_jobs(auto_requeue=True, maximum_attempts=3)
    assert recovered == [{"job_id": job["id"], "action": "requeued"}]
    current = get_job(job["id"])
    assert current["status"] == "queued"
    assert current["attempt"] == 2
    assert len(list_attempts(job["id"])) == 2
    event_types = [event["event_type"] for event in list_events(job["id"])]
    assert "stale_lease_recovered" in event_types
    assert "recovery_requeued" in event_types


def test_stale_lease_with_verified_publication_recovers_completed(project_factory):
    project_factory()
    from web_backend.database import connect
    from web_backend.queue_store import (
        claim_next_job,
        create_job,
        get_job,
        list_attempts,
        list_events,
        reconcile_stale_jobs,
    )

    job = create_job("project-1", {})
    claim_next_job("dead-worker", lease_seconds=15)
    expired = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    with connect() as db:
        db.execute(
            """
            UPDATE jobs SET lease_expires_at=?, last_heartbeat_at=?,
                drive_status='completed', drive_remote_path='result.mp4',
                drive_verified=1 WHERE id=?
            """,
            (expired, expired, job["id"]),
        )
        db.commit()

    recovered = reconcile_stale_jobs(auto_requeue=True, maximum_attempts=3)
    assert recovered == [{"job_id": job["id"], "action": "completed"}]
    assert get_job(job["id"])["status"] == "completed"
    assert len(list_attempts(job["id"])) == 1
    event_types = [event["event_type"] for event in list_events(job["id"])]
    assert "stale_lease_recovered" in event_types
    assert "recovery_completed" in event_types


def test_progress_is_persisted_in_job_attempt_and_events(project_factory):
    project_factory()
    from web_backend.queue_store import (
        claim_next_job,
        create_job,
        get_job,
        list_attempts,
        list_events,
        record_progress,
    )

    job = create_job("project-1", {})
    claim_next_job("worker-a")
    record_progress(
        job["id"],
        "worker-a",
        progress=37,
        stage="rendering",
        message="Frame 37/100",
        stage_progress=37,
    )
    current = get_job(job["id"])
    assert current["progress"] == 37
    assert current["current_stage"] == "rendering"
    attempt = list_attempts(job["id"])[0]
    assert attempt["progress"] == 37
    assert attempt["heartbeat_at"]
    assert any(event["event_type"] == "progress" for event in list_events(job["id"]))


def test_queued_cancel_is_terminal_and_evidenced(project_factory):
    project_factory()
    from web_backend.queue_store import create_job, list_events, request_cancel

    job = create_job("project-1", {})
    cancelled = request_cancel(job["id"])
    assert cancelled["status"] == "cancelled"
    assert cancelled["cancel_requested"] is False
    types = [event["event_type"] for event in list_events(job["id"])]
    assert "cancel_requested" in types
    assert "attempt_finished" in types

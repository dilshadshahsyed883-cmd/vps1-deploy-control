from __future__ import annotations


def test_pre_render_failure_finishes_attempt_instead_of_sticking(project_factory):
    project_factory()
    from web_backend.queue_store import claim_next_job, create_job, get_job, list_attempts, list_events
    from web_backend.worker_v2 import process_job

    job = create_job("project-1", {})
    claimed = claim_next_job("worker-a")
    result = process_job(claimed, "worker-a")
    assert result["status"] == "failed"
    assert "audio" in result["error_text"].lower()
    assert get_job(job["id"])["status"] == "failed"
    assert list_attempts(job["id"])[0]["status"] == "failed"
    assert any(event["event_type"] == "attempt_finished" for event in list_events(job["id"]))


def test_worker_shutdown_records_interruption_for_recovery(project_factory):
    project_factory()
    from web_backend.queue_store import claim_next_job, create_job, list_events
    from web_backend.worker_v2 import process_job

    job = create_job("project-1", {})
    claimed = claim_next_job("worker-a")
    result = process_job(claimed, "worker-a", shutdown_requested=lambda: True)
    assert result["status"] == "interrupted"
    assert result["recovery_note"]
    assert any(event["event_type"] == "worker_interrupted" for event in list_events(job["id"]))


def test_worker_iteration_reconciles_leases_after_startup(project_factory, monkeypatch):
    project_factory()
    from datetime import datetime, timedelta, timezone
    from web_backend.database import connect
    from web_backend.queue_store import claim_next_job, create_job
    import web_backend.worker_v2 as worker

    job = create_job("project-1", {})
    claim_next_job("dead-worker", lease_seconds=15)
    expired = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    with connect() as db:
        db.execute(
            "UPDATE jobs SET lease_expires_at=?, last_heartbeat_at=? WHERE id=?",
            (expired, expired, job["id"]),
        )
        db.commit()

    monkeypatch.setattr(worker, "process_job", lambda claimed, worker_id, shutdown_requested: claimed)
    claimed = worker.run_once("replacement-worker")
    assert claimed["id"] == job["id"]
    assert claimed["attempt"] == 2
    assert claimed["status"] == "running"

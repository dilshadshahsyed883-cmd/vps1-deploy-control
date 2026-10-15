from __future__ import annotations

from fastapi.testclient import TestClient


ADMIN = {"X-Auth-User": "admin", "X-App-Access": "both"}


def test_live_uses_authoritative_stage_and_drive_counts(db_env, project_factory, monkeypatch):
    from web_backend.app import app
    import web_backend.app as app_module
    from web_backend.database import connect
    from web_backend.queue_store import create_job

    for project_id, stage in (
        ("render", "ready_render"),
        ("compress", "ready_compress"),
        ("upload", "ready_upload"),
    ):
        project_factory(project_id, owner="admin")
        job = create_job(project_id, {})
        with connect() as db:
            db.execute(
                "UPDATE jobs SET pipeline_stage=?,status='queued' WHERE id=?",
                (stage, job["id"]),
            )
            if project_id == "upload":
                db.execute("UPDATE jobs SET drive_verified=1 WHERE id=?", (job["id"],))
            db.commit()

    monkeypatch.setattr(app_module, "algrow_snapshot", lambda _owner=None: {
        "available": True, "received": 375, "queue": 2, "counts": {"pending": 2},
        "paused": False, "active": None, "runtime": None, "error": "",
    })
    with TestClient(app) as client:
        response = client.get("/api/live", headers=ADMIN)
    assert response.status_code == 200
    counts = response.json()["pipeline_counts"]
    assert counts["received_from_drive"] == 375
    assert counts["algrow_queue"] == 2
    assert counts["renderer_queue"] == 1
    assert counts["compression_queue"] == 1
    assert counts["upload_queue"] == 1
    assert counts["drive_verified"] == 1


def test_admin_can_pause_and_resume_compressor(db_env):
    from web_backend.app import app
    from web_backend.database import connect

    with TestClient(app) as client:
        paused = client.post(
            "/api/acs-control/pipelines/compress/pause", headers=ADMIN
        )
        resumed = client.post(
            "/api/acs-control/pipelines/compress/resume", headers=ADMIN
        )
    assert paused.status_code == 200
    assert paused.json()["paused"] is True
    assert resumed.status_code == 200
    assert resumed.json()["paused"] is False
    with connect() as db:
        assert db.execute(
            "SELECT paused FROM acs_pipeline_controls WHERE name='compress'"
        ).fetchone()[0] == 0


def test_compressor_releases_job_to_upload_queue(db_env, project_factory, monkeypatch):
    from types import SimpleNamespace

    from web_backend.compression_worker import compress_job
    import web_backend.compression_worker as compressor
    from web_backend.database import connect
    from web_backend.pipeline_v3 import claim_stage
    from web_backend.queue_store import create_job

    project_factory("compression-handoff")
    job = create_job("compression-handoff", {"post_render_compress_enabled": False})
    with connect() as db:
        db.execute(
            "UPDATE jobs SET pipeline_stage='ready_compress' WHERE id=?", (job["id"],)
        )
        db.commit()
    claimed = claim_stage("ready_compress", "compressor-test")
    output = claimed["output_path"]
    from pathlib import Path
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    Path(output).write_bytes(b"rendered-video")
    monkeypatch.setattr(compressor, "validate_video_file", lambda _path: {"ok": True})
    monkeypatch.setattr(
        compressor, "run_cancellable_command",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stderr=""),
    )
    result = compress_job(claimed, "compressor-test")
    assert result["status"] == "queued"
    assert result["pipeline_stage"] == "ready_upload"
    assert result["render_verified"] == 1

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient


ALICE = {"X-Auth-User": "alice", "X-App-Access": "auto-caption"}
BOB = {"X-Auth-User": "bob", "X-App-Access": "auto-caption"}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _inspected(project_id: str, audio: bytes = b"audio", script: bytes = b"hello"):
    manifest = {
        "manifest_version": 1,
        "project_id": project_id,
        "owner_id": "alice",
        "project_name": "Drive Project",
        "source_mode": "script_captions",
        "category": "Tests",
        "settings": {"caption_mode": True, "caption_text_source": "script"},
        "files": [
            {"role": "audio", "relative_path": "source/audio.wav", "size": len(audio), "sha256": _sha(audio)},
            {"role": "script", "relative_path": "source/script.txt", "size": len(script), "sha256": _sha(script)},
        ],
    }
    return {
        "folder": f"Auto Caption Studio/Incoming/{project_id}",
        "manifest": manifest,
        "manifest_sha256": "a" * 64,
        "file_count": 2,
        "bytes_total": len(audio) + len(script),
    }


def test_drive_submit_is_owned_and_idempotent(db_env, monkeypatch):
    from web_backend.app import app
    import web_backend.drive_ingress_api as api

    with TestClient(app) as client:
        project = client.post("/api/projects", json={"name": "Drive"}, headers=ALICE).json()
        inspected = _inspected(project["id"])
        monkeypatch.setattr(api, "validate_remote_submission", lambda project_id, owner: inspected)
        created = client.post(
            f"/api/projects/{project['id']}/drive-submit",
            json={"expected_manifest_sha256": "a" * 64}, headers=ALICE,
        )
        assert created.status_code == 200, created.text
        job = created.json()["job"]
        assert job["status"] == "held"
        assert job["input_transport"] == "google_drive"
        assert job["drive_input_status"] == "pending"
        again = client.post(
            f"/api/projects/{project['id']}/drive-submit", json={}, headers=ALICE,
        )
        assert again.status_code == 200 and again.json()["idempotent"] is True
        assert again.json()["job"]["id"] == job["id"]
        assert client.post(
            f"/api/projects/{project['id']}/drive-submit", json={}, headers=BOB,
        ).status_code == 403
        assert client.get(f"/api/jobs/{job['id']}/drive-ingress", headers=BOB).status_code == 403


def test_drive_download_verifies_files_before_queue(db_env, monkeypatch):
    from web_backend.app import app
    from web_backend.database import connect
    from web_backend.drive_ingress import claim_next_ingress, process_ingress
    import web_backend.drive_ingress as ingress_module
    import web_backend.drive_ingress_api as api

    audio = b"audio-bytes"; script = b"caption script"
    with TestClient(app) as client:
        project = client.post("/api/projects", json={"name": "Drive"}, headers=ALICE).json()
        inspected = _inspected(project["id"], audio, script)
        monkeypatch.setattr(api, "validate_remote_submission", lambda project_id, owner: inspected)
        job = client.post(f"/api/projects/{project['id']}/drive-submit", json={}, headers=ALICE).json()["job"]

    sources = {"source/audio.wav": audio, "source/script.txt": script}
    monkeypatch.setattr(ingress_module, "_load_runtime", lambda: ("config", "gdrive", "root"))

    def fake_run(command, *, cancel_requested, heartbeat, timeout, poll_seconds):
        heartbeat()
        remote_path = command[2].split(":", 1)[1]
        relative = remote_path.split(f"/{project['id']}/", 1)[1]
        Path(command[3]).write_bytes(sources[relative])
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(ingress_module, "run_cancellable_command", fake_run)
    claimed = claim_next_ingress("drive-worker")
    assert claimed and claimed["job_id"] == job["id"]
    result = process_ingress(claimed, "drive-worker")
    assert result["status"] == "ready"
    with connect() as db:
        current = db.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone()
        project_row = db.execute("SELECT * FROM projects WHERE id=?", (project["id"],)).fetchone()
        ingress = db.execute("SELECT * FROM drive_ingress WHERE job_id=?", (job["id"],)).fetchone()
    assert current["status"] == "queued" and current["drive_input_verified"] == 1
    assert ingress["status"] == "ready" and ingress["bytes_downloaded"] == len(audio) + len(script)
    input_dir = Path(project_row["project_path"]) / "input"
    assert (input_dir / "audio.wav").read_bytes() == audio
    assert (input_dir / "script.txt").read_bytes() == script


def test_drive_failure_retry_creates_held_attempt(db_env, monkeypatch):
    from web_backend.app import app
    from web_backend.database import connect
    from web_backend.job_states import FAILED
    from web_backend.queue_store import retry_job, transition_job
    import web_backend.drive_ingress_api as api

    with TestClient(app) as client:
        project = client.post("/api/projects", json={"name": "Drive"}, headers=ALICE).json()
        monkeypatch.setattr(api, "validate_remote_submission", lambda project_id, owner: _inspected(project_id))
        job = client.post(f"/api/projects/{project['id']}/drive-submit", json={}, headers=ALICE).json()["job"]
    transition_job(job["id"], FAILED, stage="drive_download_failed", message="failed")
    with connect() as db:
        db.execute("UPDATE drive_ingress SET status='failed' WHERE job_id=?", (job["id"],)); db.commit()
    retried = retry_job(job["id"])
    assert retried["status"] == "held" and retried["attempt"] == 2
    with connect() as db:
        ingress = db.execute("SELECT * FROM drive_ingress WHERE job_id=?", (job["id"],)).fetchone()
    assert ingress["status"] == "pending" and ingress["attempt"] == 2


def test_expired_drive_lease_is_recovered_with_event(db_env, monkeypatch):
    from web_backend.app import app
    from web_backend.database import connect
    from web_backend.drive_ingress import claim_next_ingress
    import web_backend.drive_ingress_api as api

    with TestClient(app) as client:
        project = client.post("/api/projects", json={"name": "Drive"}, headers=ALICE).json()
        monkeypatch.setattr(api, "validate_remote_submission", lambda project_id, owner: _inspected(project_id))
        job = client.post(f"/api/projects/{project['id']}/drive-submit", json={}, headers=ALICE).json()["job"]
    with connect() as db:
        db.execute(
            """UPDATE drive_ingress SET status='downloading',worker_id='dead-worker',
                   lease_expires_at='2000-01-01T00:00:00+00:00' WHERE job_id=?""",
            (job["id"],),
        )
        db.commit()
    claimed = claim_next_ingress("replacement-worker")
    assert claimed and claimed["job_id"] == job["id"]
    with connect() as db:
        events = db.execute(
            "SELECT event_type FROM job_events WHERE job_id=? ORDER BY id", (job["id"],)
        ).fetchall()
        current = db.execute("SELECT recovery_note FROM jobs WHERE id=?", (job["id"],)).fetchone()
    assert "drive_ingress_recovered" in [row["event_type"] for row in events]
    assert "safely requeued" in current["recovery_note"]

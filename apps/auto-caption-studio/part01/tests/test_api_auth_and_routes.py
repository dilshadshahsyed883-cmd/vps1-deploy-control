from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient


ALICE = {"X-Auth-User": "alice", "X-App-Access": "auto-caption"}
BOB = {"X-Auth-User": "bob", "X-App-Access": "auto-caption"}


def _route_keys(app):
    keys = []
    for route in _production_routes(app):
        path = getattr(route, "path", "")
        methods = tuple(sorted(getattr(route, "methods", set()) or set()))
        if path.startswith("/api/") or path.startswith("/jobs/"):
            keys.append((path, methods))
    return keys


def _production_routes(app):
    """Flatten FastAPI's lazy included-router wrappers for route audits."""
    pending = list(app.routes)
    while pending:
        route = pending.pop(0)
        original_router = getattr(route, "original_router", None)
        if original_router is not None:
            pending[0:0] = list(original_router.routes)
            continue
        yield route


def test_no_duplicate_production_api_routes(db_env):
    from web_backend.app import app

    keys = _route_keys(app)
    assert len(keys) == len(set(keys))


def test_every_production_data_route_has_shared_auth_dependency(db_env):
    from web_backend.app import app
    from web_backend.auth import (
        get_current_principal,
        require_job_access,
        require_project_access,
    )

    protected_calls = {
        get_current_principal,
        require_job_access,
        require_project_access,
    }

    def dependency_calls(dependant):
        calls = {dependant.call}
        for child in dependant.dependencies:
            calls.update(dependency_calls(child))
        return calls

    checked = []
    for route in _production_routes(app):
        path = getattr(route, "path", "")
        if path == "/health":
            continue
        if not (path == "/" or path.startswith("/api/") or path.startswith("/jobs/")):
            continue
        calls = dependency_calls(route.dependant)
        assert calls & protected_calls, f"Missing shared auth dependency: {path}"
        checked.append(path)
    assert "/api/projects/{project_id}/upload" in checked
    assert "/api/jobs/{job_id}/logs" in checked
    assert "/api/jobs/{job_id}/output" in checked
    assert "/api/jobs/{job_id}/preview" in checked
    assert "/jobs/{job_id}" in checked
    assert "/api/batches" in checked
    assert "/api/batches/{batch_id}" in checked


def test_project_job_and_artifact_routes_enforce_ownership(db_env):
    from web_backend.app import app

    with TestClient(app) as client:
        created = client.post("/api/projects", json={"name": "Owned"}, headers=ALICE)
        assert created.status_code == 200
        project_id = created.json()["id"]
        uploaded = client.post(
            f"/api/projects/{project_id}/upload",
            files={"file": ("script.txt", b"hello")},
            headers=ALICE,
        )
        assert uploaded.status_code == 200
        job = client.post(
            f"/api/projects/{project_id}/submit",
            json={"caption_mode": True},
            headers=ALICE,
        )
        assert job.status_code == 200
        job_id = job.json()["id"]

        project_checks = [
            ("GET", f"/api/projects/{project_id}"),
            ("GET", f"/api/projects/{project_id}/settings"),
            ("PUT", f"/api/projects/{project_id}/settings"),
            ("POST", f"/api/projects/{project_id}/submit"),
            ("POST", f"/api/projects/{project_id}/preview"),
            ("POST", f"/api/projects/{project_id}/render"),
        ]
        for method, path in project_checks:
            response = client.request(method, path, json={}, headers=BOB)
            assert response.status_code == 403, (method, path, response.text)
        forbidden_upload = client.post(
            f"/api/projects/{project_id}/upload",
            files={"file": ("forbidden.txt", b"no")},
            headers=BOB,
        )
        assert forbidden_upload.status_code == 403

        job_checks = [
            ("GET", f"/api/jobs/{job_id}"),
            ("GET", f"/api/jobs/{job_id}/attempts"),
            ("GET", f"/api/jobs/{job_id}/events"),
            ("GET", f"/api/jobs/{job_id}/logs"),
            ("GET", f"/api/jobs/{job_id}/output"),
            ("GET", f"/api/jobs/{job_id}/preview"),
            ("POST", f"/api/jobs/{job_id}/cancel"),
            ("POST", f"/api/jobs/{job_id}/retry"),
            ("GET", f"/jobs/{job_id}"),
        ]
        for method, path in job_checks:
            response = client.request(method, path, headers=BOB)
            assert response.status_code == 403, (method, path, response.text)

        assert client.get(f"/api/projects/{project_id}").status_code == 401
        assert client.get(f"/api/jobs/{job_id}").status_code == 401
        assert client.get("/api/jobs", params={"project_id": project_id}, headers=BOB).status_code == 403


def test_batch_routes_use_authenticated_owner_not_payload_owner(db_env):
    from web_backend.app import app

    with TestClient(app) as client:
        response = client.post(
            "/api/batches",
            json={"expected_count": 1, "owner_id": "bob", "category": "Test"},
            headers=ALICE,
        )
        assert response.status_code == 200
        batch_id = response.json()["id"]
        assert client.get(f"/api/batches/{batch_id}", headers=ALICE).status_code == 200
        assert client.get(f"/api/batches/{batch_id}", headers=BOB).status_code == 403
        assert client.post(
            f"/api/batches/{batch_id}/finalize",
            json={"expected_count": 1},
            headers=BOB,
        ).status_code == 403


def test_job_listing_is_owner_scoped(db_env):
    from web_backend.app import app

    with TestClient(app) as client:
        alice_project = client.post("/api/projects", json={"name": "A"}, headers=ALICE).json()
        bob_project = client.post("/api/projects", json={"name": "B"}, headers=BOB).json()
        client.post(f"/api/projects/{alice_project['id']}/submit", json={}, headers=ALICE)
        client.post(f"/api/projects/{bob_project['id']}/submit", json={}, headers=BOB)
        alice_jobs = client.get("/api/jobs", headers=ALICE).json()
        bob_jobs = client.get("/api/jobs", headers=BOB).json()
        assert len(alice_jobs) == 1
        assert len(bob_jobs) == 1
        assert alice_jobs[0]["project_id"] == alice_project["id"]
        assert bob_jobs[0]["project_id"] == bob_project["id"]

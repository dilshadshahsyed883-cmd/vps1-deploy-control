"""HeyGen Studio VPS_1 contract: navigation, owner isolation and GPU proxy."""
from __future__ import annotations

import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

SCENE = Path(__file__).resolve().parents[1] / "apps/scene-builder/part01"
sys.path.insert(0, str(SCENE))
from heygen_studio import HeygenBridge, register_heygen_studio


class HeygenStudioTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.previous = {}
        from os import environ
        for name in ("HEYGEN_WORKER_ALLOWED_HOSTS", "HEYGEN_WORKER_URL",
                     "HEYGEN_WORKER_API_KEY", "HEYGEN_SALAD_API_KEY"):
            self.previous[name] = environ.get(name)
        environ["HEYGEN_WORKER_ALLOWED_HOSTS"] = "worker.example"
        environ.pop("HEYGEN_WORKER_URL", None)
        environ.pop("HEYGEN_WORKER_API_KEY", None)
        environ.pop("HEYGEN_SALAD_API_KEY", None)
        self.addCleanup(self.restore_env)

        self.remote_jobs = {}
        self.remote_token = None
        self.remote_salad_key = None
        self.identity = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        def upstream(request: httpx.Request):
            self.remote_token = request.headers.get("Authorization")
            self.remote_salad_key = request.headers.get("Salad-Api-Key")
            path = request.url.path
            if path == "/api/v1/engine":
                return httpx.Response(200, json={"engine": "sadtalker", "models_ready": True,
                                                 "entrypoint_found": True})
            if path == "/api/v1/jobs" and request.method == "GET":
                return httpx.Response(200, json=[
                    {"id": jid, "status": status} for jid, status in self.remote_jobs.items()
                ])
            if path == "/api/v1/jobs" and request.method == "POST":
                raw = request.read()
                if b"portrait.png" not in raw or b"speech.wav" not in raw:
                    return httpx.Response(415, json={"detail":"Invalid multipart files"})
                self.remote_jobs[self.identity] = "completed"
                return httpx.Response(202, json={"id": self.identity, "status": "queued"})
            if path == "/api/v1/jobs/" + self.identity:
                if request.method == "GET":
                    return httpx.Response(200, json={"id": self.identity,
                                                    "status": self.remote_jobs.get(self.identity, "queued")})
                if request.method == "DELETE":
                    self.remote_jobs.pop(self.identity, None)
                    return httpx.Response(204)
            if path == "/api/v1/jobs/" + self.identity + "/video":
                return httpx.Response(200, content=b"0000ftypmp42-mock", headers={"Content-Type": "video/mp4"})
            if path == "/api/v1/jobs/" + self.identity + "/cancel":
                self.remote_jobs[self.identity] = "cancelled"
                return httpx.Response(200, json={"status": "cancelled"})
            if path == "/api/v1/jobs/" + self.identity + "/retry":
                self.remote_jobs[self.identity] = "queued"
                return httpx.Response(200, json={"status": "queued"})
            return httpx.Response(404)

        app = FastAPI()
        def login(req: Request):
            if not req.session.get("authenticated"):
                raise HTTPException(401, "Login required")
        def admin(req: Request):
            if not req.session.get("is_admin"):
                raise HTTPException(403, "Administrator access required")
        self.bridge = register_heygen_studio(
            app, Jinja2Templates(directory=str(SCENE / "templates")),
            self.tmp.name, login, login, transport=httpx.MockTransport(upstream))
        self.addCleanup(self.bridge.media.pool.shutdown, wait=True)
        self.addCleanup(self.bridge.outbox.close)

        @app.get("/_fixture/session/{role}/{owner}")
        def session(req: Request, role: str, owner: str):
            req.session.clear()
            if role in ("admin", "member"):
                req.session.update({"authenticated": True, "is_admin": role == "admin",
                                    "username": owner})
            return {"role": role}

        app.add_middleware(SessionMiddleware, secret_key="heygen-test-session")
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def restore_env(self):
        from os import environ
        for key, value in self.previous.items():
            if value is None:
                environ.pop(key, None)
            else:
                environ[key] = value

    def login(self, role="admin", owner="alice"):
        self.client.get(f"/_fixture/session/{role}/{owner}")

    def token(self):
        page = self.client.get("/heygen-studio/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Create Video", page.text)
        self.assertIn("Jobs &amp; Videos", page.text)
        self.assertIn('href="/heygen-studio/">HeyGen Studio</a>', page.text)
        token = re.search(r'data-csrf="([^"]+)"', page.text)
        self.assertIsNotNone(token)
        return {"X-Heygen-CSRF": token.group(1)}

    def configure(self, headers):
        return self.client.post("/api/heygen-studio/config", headers=headers, json={
            "endpoint": "https://worker.example", "token": "sample-secret",
            "salad_api_key": "sample-salad-secret"})

    @staticmethod
    def uploads():
        return {
            "image": ("portrait.png", b"\x89PNG\r\n\x1a\n"+"image".encode(), "image/png"),
            "audio": ("speech.wav", b"RIFF\x10\x00\x00\x00WAVEaudio", "audio/wav"),
        }

    def test_admin_create_job_then_stream_and_manage(self):
        self.login()
        headers = self.token()
        self.assertEqual(self.client.get("/api/heygen-studio/jobs").json(), {"items":[]})
        self.assertEqual(self.configure(headers).status_code, 200)
        cfg = self.client.get("/api/heygen-studio/config").json()
        self.assertTrue(cfg["configured"])
        self.assertTrue(cfg["has_token"])
        self.assertTrue(cfg["has_salad_key"])
        self.assertNotIn("sample-secret", str(cfg))
        self.assertNotIn("sample-salad-secret", str(cfg))
        self.assertNotIn("sample-secret", self.client.get("/heygen-studio/").text)
        self.assertEqual(self.client.get("/api/heygen-studio/engine").json()["engine"], "sadtalker")
        self.assertEqual(self.remote_token, "Bearer sample-secret")
        self.assertEqual(self.remote_salad_key, "sample-salad-secret")
        created = self.client.post("/api/heygen-studio/jobs", headers=headers, files=self.uploads())
        self.assertEqual(created.status_code, 202, created.text)
        self.assertEqual(created.json()["id"], self.identity)
        jobs = self.client.get("/api/heygen-studio/jobs").json()["items"]
        self.assertEqual(jobs[0]["status"], "completed")
        stream = self.client.get(f"/api/heygen-studio/jobs/{self.identity}/video")
        self.assertEqual(stream.status_code, 200)
        self.assertEqual(stream.content, b"0000ftypmp42-mock")
        self.assertEqual(stream.headers["cache-control"], "private, no-store")
        download = self.client.get(f"/api/heygen-studio/jobs/{self.identity}/video?download=1")
        self.assertIn("attachment", download.headers["content-disposition"])
        self.assertEqual(self.client.post(f"/api/heygen-studio/jobs/{self.identity}/cancel", headers=headers).status_code, 200)
        self.assertEqual(self.client.post(f"/api/heygen-studio/jobs/{self.identity}/retry", headers=headers).status_code, 200)
        self.assertEqual(self.client.post(f"/api/heygen-studio/jobs/{self.identity}/delete", headers=headers).status_code, 200)
        self.assertEqual(self.client.get("/api/heygen-studio/jobs").json(), {"items":[]})

    def test_blank_credentials_preserve_existing_keys(self):
        self.login()
        headers = self.token()
        self.assertEqual(self.configure(headers).status_code, 200)
        saved = self.client.post("/api/heygen-studio/config", headers=headers, json={
            "endpoint": "https://worker.example", "token": "",
            "salad_api_key": "",
        })
        self.assertEqual(saved.status_code, 200)
        self.assertTrue(saved.json()["has_token"])
        self.assertTrue(saved.json()["has_salad_key"])
        self.assertNotIn("sample-salad-secret", saved.text)
        self.assertEqual(self.client.get("/api/heygen-studio/engine").status_code, 200)
        self.assertEqual(self.remote_token, "Bearer sample-secret")
        self.assertEqual(self.remote_salad_key, "sample-salad-secret")

    def test_legacy_config_table_adds_salad_key_column(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / "heygen-studio"
            data.mkdir()
            with sqlite3.connect(data / "bridge.sqlite3") as con:
                con.execute("CREATE TABLE config(owner TEXT PRIMARY KEY, endpoint TEXT NOT NULL, token TEXT NOT NULL)")
                con.execute("INSERT INTO config VALUES(?,?,?)",
                            ("alice", "https://worker.example", "old-avatar-key"))
            bridge = HeygenBridge(Path(folder), transport=self.bridge.transport)
            self.addCleanup(bridge.media.pool.shutdown, wait=True)
            self.addCleanup(bridge.outbox.close)
            before = bridge.config("alice")
            self.assertTrue(before["has_token"])
            self.assertFalse(before["has_salad_key"])
            after = bridge.save("alice", {
                "endpoint": "https://worker.example",
                "salad_api_key": "new-gateway-key",
            })
            self.assertTrue(after["has_salad_key"])
            self.assertNotIn("new-gateway-key", str(after))
            with bridge._client("alice") as client:
                self.assertEqual(client.headers["Authorization"], "Bearer old-avatar-key")
                self.assertEqual(client.headers["Salad-Api-Key"], "new-gateway-key")

    def test_guest_denied_but_active_member_has_owner_scoped_access(self):
        self.login(role="guest")
        for path in ("/heygen-studio/", "/api/heygen-studio/config",
                     "/api/heygen-studio/jobs", "/api/heygen-studio/engine"):
            self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.post("/api/heygen-studio/config", json={}).status_code, 401)

        self.login(role="member", owner="bob")
        member_headers = self.token()
        self.assertEqual(self.client.get("/api/heygen-studio/config").status_code, 200)
        self.assertEqual(self.configure(member_headers).status_code, 200)
        self.assertEqual(self.client.get("/api/heygen-studio/engine").status_code, 200)
        self.assertEqual(self.client.get("/api/heygen-studio/jobs").json(), {"items": []})

        self.login(role="admin", owner="alice")
        self.token()
        alice_config = self.client.get("/api/heygen-studio/config").json()
        self.assertFalse(alice_config["has_token"])
        self.assertFalse(alice_config["has_salad_key"])
        self.login(role="member", owner="bob")
        self.token()
        bob_config = self.client.get("/api/heygen-studio/config").json()
        self.assertTrue(bob_config["has_token"])
        self.assertTrue(bob_config["has_salad_key"])

    def test_exact_approved_salad_gateway_not_arbitrary_subdomain(self):
        self.login(role="member")
        headers = self.token()
        good = self.client.post("/api/heygen-studio/config", headers=headers, json={
            "endpoint": "https://tangelo-taco-eq6z0bazejc7gmng.salad.cloud",
            "token": "example-test-only", "salad_api_key": "fake-gateway-token",
        })
        self.assertEqual(good.status_code, 200, good.text)
        bad = self.client.post("/api/heygen-studio/config", headers=headers, json={
            "endpoint": "https://unapproved-subdomain.salad.cloud",
            "token": "example-test-only",
        })
        self.assertEqual(bad.status_code, 422, bad.text)

    def test_candidate_guard_allows_only_exact_heygen_actions(self):
        from precutover_policy import mutation_allowed

        allowed = [
            ("POST", "/api/heygen-studio/config"),
            ("POST", "/api/heygen-studio/jobs"),
            ("POST", "/api/heygen-studio/queue/start"),
            ("POST", "/api/heygen-studio/queue/pause"),
            ("POST", "/api/heygen-studio/queue/resume"),
            ("POST", "/api/heygen-studio/jobs/" + "a" * 32 + "/cancel"),
            ("POST", "/api/heygen-studio/jobs/" + "a" * 32 + "/retry"),
            ("POST", "/api/heygen-studio/jobs/" + "a" * 32 + "/delete"),
            ("POST", "/api/heygen-studio/jobs/" + "a" * 32 + "/save"),
        ]
        for method, path in allowed:
            self.assertTrue(mutation_allowed(method, path, False), (method, path))
        denied = [
            ("POST", "/api/heygen-studio/admin/override"),
            ("POST", "/api/heygen-studio/jobs/invalid-id/delete"),
            ("POST", "/api/heygen-studio/queue/unknown"),
            ("DELETE", "/api/heygen-studio/config"),
            ("POST", "/api/random/studio/save"),
        ]
        for method, path in denied:
            self.assertFalse(mutation_allowed(method, path, False), (method, path))

    def test_production_routes_use_active_owner_not_admin_gate(self):
        source = (SCENE / "app.py").read_text(encoding="utf-8")
        self.assertIn(
            "HEYGEN_STUDIO = register_heygen_studio(app, templates, DATA_DIR, require_login, require_login)",
            source,
        )
        self.assertNotIn('request.url.path.rstrip("/") == "/heygen-studio" or', source)
        self.assertIn(
            "CLOUD_RENDER = register_cloud_render(app, templates, DATA_DIR, require_admin, require_login)",
            source,
        )

    def test_csrf_owner_isolation_and_format_validation(self):
        self.login(owner="alice")
        headers = self.token()
        self.assertEqual(self.client.post("/api/heygen-studio/config", json={
            "endpoint": "https://worker.example", "token": "bad"}).status_code, 403)
        self.assertEqual(self.client.post("/api/heygen-studio/config", json={}, headers={
            "X-Heygen-CSRF": "bad"}).status_code, 403)
        self.assertEqual(self.configure(headers).status_code, 200)
        wrong = self.uploads()
        wrong["image"] = ("portrait.png", b"fake png", "image/png")
        self.assertEqual(self.client.post("/api/heygen-studio/jobs", headers=headers, files=wrong).status_code, 415)
        self.assertEqual(self.client.post("/api/heygen-studio/jobs", headers=headers, files=self.uploads()).status_code, 202)
        self.login(owner="bob")
        bob_headers = self.token()
        self.assertEqual(self.client.get("/api/heygen-studio/jobs").json(), {"items":[]})
        self.assertEqual(self.client.get(f"/api/heygen-studio/jobs/{self.identity}/video").status_code, 404)
        self.assertEqual(self.client.post(f"/api/heygen-studio/jobs/{self.identity}/delete",
                                          headers=bob_headers).status_code, 404)
        self.login(owner="alice")
        self.assertEqual(len(self.client.get("/api/heygen-studio/jobs").json()["items"]), 1)

    def test_offline_queue_api_and_owner_isolation(self):
        self.login(owner="alice")
        csrf = self.token()
        self.assertEqual(self.client.get("/api/heygen-studio/queue").json()["state"], "running")
        paused = self.client.post("/api/heygen-studio/queue/pause", headers=csrf)
        self.assertEqual(paused.json()["state"], "paused")
        result = self.client.post("/api/heygen-studio/jobs", headers=csrf, files=self.uploads())
        self.assertEqual(result.status_code, 202)
        queued = result.json()["id"]
        self.assertEqual(result.json()["status"], "waiting_network")
        jobs = self.client.get("/api/heygen-studio/jobs").json()["items"]
        self.assertEqual([row["id"] for row in jobs], [queued])
        self.assertEqual(jobs[0]["status"], "waiting_network")
        self.login(owner="bob")
        bob_csrf = self.token()
        self.assertEqual(self.client.get("/api/heygen-studio/jobs").json()["items"], [])
        self.assertEqual(self.client.post(
            f"/api/heygen-studio/jobs/{queued}/delete", headers=bob_csrf).status_code, 404)
        self.login(owner="alice")
        csrf = self.token()
        cancelled = self.client.post(
            f"/api/heygen-studio/jobs/{queued}/cancel", headers=csrf)
        self.assertEqual(cancelled.json()["status"], "cancelled")
        removed = self.client.post(
            f"/api/heygen-studio/jobs/{queued}/delete", headers=csrf)
        self.assertEqual(removed.status_code, 200)
        self.assertTrue(removed.json()["deleted"])
        self.assertEqual(self.client.get("/api/heygen-studio/jobs").json()["items"], [])

    def test_endpoint_restrictions_and_offline_fail_closed(self):
        self.login()
        headers = self.token()
        for endpoint in ("http://worker.example", "https://127.0.0.1:9000",
                         "https://worker.example@evil.example", "https://worker.example/path"):
            response = self.client.post("/api/heygen-studio/config", headers=headers,
                                        json={"endpoint": endpoint, "token":"sample-secret"})
            self.assertEqual(response.status_code, 422, (endpoint, response.text))
        waiting = self.client.post("/api/heygen-studio/jobs", headers=headers,
                                   files=self.uploads())
        self.assertEqual(waiting.status_code, 202)
        self.assertEqual(waiting.json()["status"], "waiting_network")
        self.assertEqual(self.client.get("/api/heygen-studio/queue").json()["state"], "running")


if __name__ == "__main__":
    unittest.main()

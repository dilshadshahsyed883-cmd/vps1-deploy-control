"""HeyGen Studio VPS_1 contract: navigation, owner isolation and GPU proxy."""
from __future__ import annotations

import re
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
from heygen_studio import register_heygen_studio


class HeygenStudioTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.previous = {}
        from os import environ
        for name in ("HEYGEN_WORKER_ALLOWED_HOSTS", "HEYGEN_WORKER_URL", "HEYGEN_WORKER_API_KEY"):
            self.previous[name] = environ.get(name)
        environ["HEYGEN_WORKER_ALLOWED_HOSTS"] = "worker.example"
        environ.pop("HEYGEN_WORKER_URL", None)
        environ.pop("HEYGEN_WORKER_API_KEY", None)
        self.addCleanup(self.restore_env)

        self.remote_jobs = {}
        self.remote_token = None
        self.identity = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        def upstream(request: httpx.Request):
            self.remote_token = request.headers.get("Authorization")
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
            self.tmp.name, admin, login, transport=httpx.MockTransport(upstream))

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
            "endpoint": "https://worker.example", "token": "sample-secret"})

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
        self.assertNotIn("sample-secret", str(cfg))
        self.assertNotIn("sample-secret", self.client.get("/heygen-studio/").text)
        self.assertEqual(self.client.get("/api/heygen-studio/engine").json()["engine"], "sadtalker")
        self.assertEqual(self.remote_token, "Bearer sample-secret")
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

    def test_nonadmins_and_guests_are_denied(self):
        for role in ("guest", "member"):
            self.login(role=role)
            for path in ("/heygen-studio/", "/api/heygen-studio/config",
                         "/api/heygen-studio/jobs", "/api/heygen-studio/engine"):
                self.assertIn(self.client.get(path).status_code, (401, 403))
            self.assertIn(self.client.post("/api/heygen-studio/config", json={}).status_code, (401, 403))

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

    def test_endpoint_restrictions_and_offline_fail_closed(self):
        self.login()
        headers = self.token()
        for endpoint in ("http://worker.example", "https://127.0.0.1:9000",
                         "https://worker.example@evil.example", "https://worker.example/path"):
            response = self.client.post("/api/heygen-studio/config", headers=headers,
                                        json={"endpoint": endpoint, "token":"sample-secret"})
            self.assertEqual(response.status_code, 422, (endpoint, response.text))
        self.assertEqual(self.client.post("/api/heygen-studio/jobs", headers=headers,
                                         files=self.uploads()).status_code, 503)


if __name__ == "__main__":
    unittest.main()

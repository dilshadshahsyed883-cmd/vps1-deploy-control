"""Isolated Imagen/Qwen Voice Clone VPS bridge tests; no real GPU or network."""
from __future__ import annotations

import os
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
from experiment_gpu_studios import ExperimentGpuStudios, register_experiment_gpu_studios


class GpuStudioTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        prior = os.environ.get("EXPERIMENT_GPU_ALLOWED_HOSTS")
        os.environ["EXPERIMENT_GPU_ALLOWED_HOSTS"] = "worker.example"
        self.addCleanup(lambda: (
            os.environ.pop("EXPERIMENT_GPU_ALLOWED_HOSTS", None)
            if prior is None else os.environ.__setitem__("EXPERIMENT_GPU_ALLOWED_HOSTS", prior)))
        self.seen = []
        self.ready = True
        self.corrupt_image = False
        self.upstream_offline = False

        def upstream(req: httpx.Request):
            self.seen.append((req.method, req.url.path, dict(req.headers)))
            if self.upstream_offline:
                raise httpx.ConnectError("mock GPU worker offline")
            path = req.url.path
            if path == "/health":
                return httpx.Response(200, json={"ok": True, "engine": "configured"})
            if path == "/ready":
                return httpx.Response(200, json={"ready": self.ready})
            if path == "/capabilities":
                return httpx.Response(200, json={"engine": "flux2-klein-4b", "modes": ["generate", "edit"]})
            if path == "/runtime":
                return httpx.Response(200, json={"model": "qwen3-tts-0.6b", "gpu": "cuda"})
            if path == "/jobs" and req.method == "POST":
                body = req.read()
                assert b"Photorealistic" in body, body[:100]
                return httpx.Response(202, json={"id": "img-001", "status": "queued"})
            if path == "/v1/jobs" and req.method == "POST":
                body = req.read()
                assert b"ref_audio_b64" in body
                assert b"ref_text" in body
                return httpx.Response(202, json={"id": "wav-001", "status": "queued"})
            if path == "/jobs/img-001" and req.method == "GET":
                return httpx.Response(200, json={
                    "id": "img-001", "status": "completed",
                    "outputs": [{"url": "/outputs/img-001/scene-000.png"}],
                    "generation_seconds": 5.2})
            if path == "/jobs/img-001/cancel" and req.method == "POST":
                return httpx.Response(200, json={"cancelled": True})
            if path == "/outputs/img-001/scene-000.png":
                return httpx.Response(200, content=(
                    b"<script>alert(1)</script>" if self.corrupt_image else b"\x89PNG\r\n\x1a\nphoto"))
            if path == "/v1/jobs/wav-001" and req.method == "GET":
                return httpx.Response(200, json={
                    "id": "wav-001", "status": "completed", "audio_duration_seconds": 3,
                    "generation_seconds": 1.5, "speed_factor": 2})
            if path == "/v1/jobs/wav-001/audio":
                return httpx.Response(200, content=b"RIFFxxxxWAVEdata")
            return httpx.Response(404, json={"error": "unexpected mock path"})

        app = FastAPI()
        def login(req: Request):
            if not req.session.get("authenticated"):
                raise HTTPException(401, "Login required")
        self.bridge = register_experiment_gpu_studios(
            app, Jinja2Templates(directory=str(SCENE / "templates")),
            self.folder.name, login, transport=httpx.MockTransport(upstream))

        @app.get("/_fixture/{username}")
        def session(req: Request, username: str):
            req.session.clear()
            if username != "guest":
                req.session.update({"authenticated": True, "username": username, "is_admin": username == "alice"})
            return {"ok": True}

        app.add_middleware(SessionMiddleware, secret_key="experiment-gpu-fixture")
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def login(self, username="alice"):
        self.client.get("/_fixture/" + username)

    def token(self, kind):
        route = "/imagen-studio/" if kind == "imagen" else "/voice-clone-studio/"
        page = self.client.get(route)
        self.assertEqual(page.status_code, 200, page.text[:300])
        self.assertEqual(page.text.count('id="eg-root"'), 1)
        match = re.search(r'data-csrf="([^"]+)"', page.text)
        self.assertIsNotNone(match)
        return {"X-Experiment-GPU-CSRF": match.group(1)}

    def configure(self, kind, headers, mode=None):
        return self.client.post("/api/experiment-gpu/" + kind + "/config", headers=headers, json={
            "endpoint": "https://worker.example", "api_key": "private-worker-secret",
            "salad_key": "private-gateway-key",
            "auth_mode": mode or ("bearer" if kind == "voice-clone" else "x-api-key"),
            "image_ref": "ghcr.io/dilshadshahsyed883-cmd/" +
                ("imagen:flux2-klein-4b-abc123" if kind == "imagen" else "voice_clone:qwen3-tts-0.6b-abc123")
        })

    def test_imagen_generation_is_owner_scoped_with_authenticated_output(self):
        self.login()
        headers = self.token("imagen")
        base = "/api/experiment-gpu/imagen"
        self.assertEqual(self.client.post(base + "/config", json={}).status_code, 403)
        cfg = self.configure("imagen", headers)
        self.assertEqual(cfg.status_code, 200, cfg.text)
        self.assertTrue(cfg.json()["configured"])
        self.assertNotIn("private-worker-secret", cfg.text)
        self.assertNotIn("private-gateway-key", cfg.text)
        self.assertTrue(self.client.get(base + "/health").json()["ready"])
        models = self.client.get(base + "/models").json()
        self.assertEqual(models["engine"], "flux2-klein-4b")
        job = self.client.post(base + "/jobs", headers=headers, data={
            "prompt": "Photorealistic scene", "mode": "generate", "count": "2",
            "width": "1024", "height": "576"
        })
        self.assertEqual(job.status_code, 202, job.text)
        self.assertEqual(job.json()["id"], "img-001")
        self.assertEqual(self.client.get(base + "/jobs").json()["items"][0]["id"], "img-001")
        status = self.client.get(base + "/jobs/img-001").json()
        self.assertEqual(status["output_count"], 1)
        self.assertEqual(status["status"], "completed")
        media = self.client.get(base + "/jobs/img-001/media")
        self.assertEqual(media.status_code, 200)
        self.assertEqual(media.content[:4], b"\x89PNG")
        self.assertEqual(media.headers["cache-control"], "private, no-store")
        self.assertIn("attachment", media.headers["content-disposition"])
        inline = self.client.get(base + "/jobs/img-001/media?preview=1")
        self.assertEqual(inline.status_code, 200)
        self.assertIn("inline", inline.headers["content-disposition"])
        self.assertEqual(self.client.post(base + "/jobs/img-001/cancel", headers=headers).status_code, 200)
        self.assertEqual(self.seen[-1][1], "/jobs/img-001/cancel")
        self.assertEqual(self.seen[-1][2]["x-api-key"], "private-worker-secret")
        self.assertEqual(self.seen[-1][2]["salad-api-key"], "private-gateway-key")
        self.login("bob")
        self.assertEqual(self.client.get(base + "/jobs").json()["items"], [])
        self.assertEqual(self.client.get(base + "/jobs/img-001").status_code, 404)
        self.assertEqual(self.client.get(base + "/jobs/img-001/media").status_code, 404)
        self.assertFalse(self.client.get(base + "/config").json()["configured"])

    def test_voice_clone_qwen_adapter_and_download(self):
        self.login("bob")
        headers = self.token("voice-clone")
        base = "/api/experiment-gpu/voice-clone"
        response = self.configure("voice-clone", headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get(base + "/models").json()["model"], "qwen3-tts-0.6b")
        result = self.client.post(base + "/jobs", headers=headers,
            data={"text": "Test narration", "ref_text": "Reference voice words",
                  "language": "English"},
            files={"reference": ("reference.wav", b"RIFF0000WAVEvoice", "audio/wav")})
        self.assertEqual(result.status_code, 202, result.text)
        self.assertEqual(result.json()["id"], "wav-001")
        self.assertEqual(self.client.get(base + "/jobs/wav-001").json()["speed_factor"], 2)
        audio = self.client.get(base + "/jobs/wav-001/media")
        self.assertEqual(audio.status_code, 200)
        self.assertTrue(audio.content.startswith(b"RIFF"))
        self.assertEqual(audio.headers["content-type"], "audio/wav")
        matching_calls = [row for row in self.seen if row[1] == "/v1/jobs"]
        self.assertEqual(matching_calls[-1][2]["authorization"], "Bearer private-worker-secret")
        self.assertEqual(self.client.post(base + "/jobs/wav-001/cancel", headers=headers).status_code, 405)
        self.login("alice")
        self.assertEqual(self.client.get(base + "/jobs").json()["items"], [])

    def test_rejects_wrong_host_mutations_and_project_image(self):
        self.login()
        headers = self.token("imagen")
        base = "/api/experiment-gpu/imagen"
        for endpoint in ("http://worker.example", "https://127.0.0.1",
                         "https://worker.example.evil.test", "https://worker.example:8443",
                         "https://user:password@worker.example"):
            response = self.client.post(base + "/config", headers=headers, json={"endpoint": endpoint})
            self.assertEqual(response.status_code, 422, endpoint)
        response = self.client.post(base + "/config", headers=headers,
            json={"image_ref": "ghcr.io/dilshadshahsyed883-cmd/voice_clone:qwen3-tts-test"})
        self.assertEqual(response.status_code, 422)
        response = self.client.post(base + "/config", headers=headers,
            json={"image_ref": "ghcr.io/dilshadshahsyed883-cmd/imagen:latest"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.client.get(base + "/jobs/../other").status_code, 404)
        self.login("guest")
        self.assertEqual(self.client.get("/imagen-studio/").status_code, 401)
        self.assertEqual(self.client.get(base + "/config").status_code, 401)

    def test_readiness_requires_explicit_true_even_with_http_200(self):
        self.login()
        headers = self.token("imagen")
        self.configure("imagen", headers)
        self.ready = False
        state = self.client.get("/api/experiment-gpu/imagen/health")
        self.assertEqual(state.status_code, 200)
        self.assertTrue(state.json()["alive"])
        self.assertFalse(state.json()["ready"])

    def test_invalid_generation_numbers_return_422_not_500(self):
        self.login()
        headers = self.token("imagen")
        self.configure("imagen", headers)
        base = "/api/experiment-gpu/imagen/jobs"
        for bad in ({"width": "bogus"}, {"height": "3.5"}, {"count": "nope"}):
            result = self.client.post(base, headers=headers,
                                      data={"prompt": "Photorealistic scene", **bad})
            self.assertEqual(result.status_code, 422, result.text)

    def test_invalid_gpu_image_bytes_fail_closed_without_cached_html(self):
        self.login()
        headers = self.token("imagen")
        self.configure("imagen", headers)
        base = "/api/experiment-gpu/imagen"
        self.client.post(base + "/jobs", headers=headers,
                         data={"prompt": "Photorealistic scene"})
        self.corrupt_image = True
        result = self.client.get(base + "/jobs/img-001/media?preview=1")
        self.assertEqual(result.status_code, 502)
        self.assertNotIn("text/html", result.headers.get("content-type", ""))
        self.corrupt_image = False
        valid = self.client.get(base + "/jobs/img-001/media?preview=1")
        self.assertEqual(valid.status_code, 200, valid.text[:100] if valid.headers["content-type"].startswith("text/") else "")
        self.assertEqual(valid.headers["x-content-type-options"], "nosniff")
        self.assertEqual(valid.headers["x-vps-media-saved"], "true")

    def test_saved_output_remains_private_and_available_when_gpu_swaps(self):
        self.login("alice")
        headers = self.token("imagen")
        self.configure("imagen", headers)
        base = "/api/experiment-gpu/imagen"
        created = self.client.post(base + "/jobs", headers=headers,
                                   data={"prompt": "Photorealistic scene"})
        self.assertEqual(created.status_code, 202)
        self.assertEqual(self.client.get(base + "/jobs/img-001").json()["status"], "completed")
        first = self.client.get(base + "/jobs/img-001/media?preview=1")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.headers["x-vps-media-saved"], "true")
        self.assertIn("inline", first.headers["content-disposition"])
        from stat import S_IMODE
        media = self.bridge.saved_path("imagen", "alice", "img-001", 0)
        self.assertTrue(media.is_file())
        self.assertEqual(S_IMODE(media.stat().st_mode), 0o600)
        for directory in (media.parent, *list(media.parents)[1:4]):
            self.assertEqual(S_IMODE(directory.stat().st_mode), 0o700)
        self.login("bob")
        self.assertEqual(self.client.get(base + "/jobs/img-001/media").status_code, 404)
        self.login("alice")
        headers = self.token("imagen")
        prior = os.environ["EXPERIMENT_GPU_ALLOWED_HOSTS"]
        os.environ["EXPERIMENT_GPU_ALLOWED_HOSTS"] = "worker.example,other.example"
        self.addCleanup(lambda: os.environ.__setitem__("EXPERIMENT_GPU_ALLOWED_HOSTS", prior))
        change = self.client.post(base + "/config", headers=headers, json={
            "endpoint": "https://other.example"})
        self.assertEqual(change.status_code, 200, change.text)
        self.upstream_offline = True
        offline = self.client.get(base + "/jobs/img-001").json()
        self.assertEqual(offline["status"], "completed")
        self.assertEqual(offline["output_count"], 1)
        self.assertEqual(offline["saved_output_count"], 1)
        self.assertIn("previous GPU gateway", offline["worker_error"])
        saved = self.client.get(base + "/jobs/img-001/media")
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.content, first.content)
        self.assertEqual(saved.headers["x-vps-media-saved"], "true")
        self.assertIn("attachment", saved.headers["content-disposition"])
        self.assertEqual(self.client.get(base + "/jobs").json()["items"][0]["id"], "img-001")

    def test_corrupted_saved_media_fails_integrity_check(self):
        self.login()
        headers = self.token("imagen")
        self.configure("imagen", headers)
        base = "/api/experiment-gpu/imagen"
        self.assertEqual(self.client.post(base + "/jobs", headers=headers,
                                          data={"prompt": "Photorealistic scene"}).status_code, 202)
        self.assertEqual(self.client.get(base + "/jobs/img-001/media").status_code, 200)
        self.bridge.saved_path("imagen", "alice", "img-001", 0).write_bytes(b"tampered")
        tampered = self.client.get(base + "/jobs/img-001/media?preview=1")
        self.assertEqual(tampered.status_code, 502)
        self.assertIn("integrity", tampered.json()["detail"].lower())

    def test_job_not_cached_is_not_misrouted_after_gateway_swap(self):
        self.login()
        headers = self.token("imagen")
        self.configure("imagen", headers)
        base = "/api/experiment-gpu/imagen"
        self.assertEqual(self.client.post(base + "/jobs", headers=headers,
                                          data={"prompt": "Photorealistic scene"}).status_code, 202)
        os.environ["EXPERIMENT_GPU_ALLOWED_HOSTS"] = "worker.example,other.example"
        self.assertEqual(self.client.post(base + "/config", headers=headers,
                                          json={"endpoint": "https://other.example"}).status_code, 200)
        missing = self.client.get(base + "/jobs/img-001/media")
        self.assertEqual(missing.status_code, 409)
        self.assertEqual(self.client.post(base + "/jobs/img-001/cancel",
                                          headers=headers).status_code, 409)

    def test_duplicate_worker_id_does_not_replace_saved_job(self):
        self.login()
        headers = self.token("imagen")
        self.configure("imagen", headers)
        base = "/api/experiment-gpu/imagen"
        payload = {"prompt": "Photorealistic scene"}
        self.assertEqual(self.client.post(base + "/jobs", headers=headers, data=payload).status_code, 202)
        self.assertEqual(self.client.post(base + "/jobs", headers=headers, data=payload).status_code, 409)
        self.assertEqual(len(self.client.get(base + "/jobs").json()["items"]), 1)

    def test_gpu_configuration_has_private_filesystem_permissions(self):
        import stat
        self.assertEqual(stat.S_IMODE(self.bridge.root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.bridge.db.stat().st_mode), 0o600)

    def test_old_studios_and_salad_routes_preserved(self):
        root = SCENE
        base = (root / "templates/base.html").read_text()
        shell = (root / "static/vps1_native_site_shell.js").read_text()
        concat = (root / "templates/cloud_render.html").read_text()
        heygen = (root / "templates/heygen_studio.html").read_text()
        app = (root / "app.py").read_text()
        for label in ("Concat", "HeyGen", "Voice Clone", "Imagen"):
            self.assertIn(label, base)
            self.assertIn(label, shell)
        self.assertIn('href="/cloud-render/">Concat</a>', base)
        self.assertIn('id="cloud-heading">Concat</h2>', concat)
        self.assertIn('id="hg-models" hidden', heygen)
        self.assertIn('id="eg-gallery" hidden', (root / "templates/experiment_gpu_studio.html").read_text())
        self.assertIn("register_cloud_render(app, templates, DATA_DIR", app)
        self.assertIn("register_heygen_studio(app, templates, DATA_DIR", app)
        self.assertIn("register_experiment_gpu_studios(app, templates, DATA_DIR", app)


if __name__ == "__main__":
    unittest.main()

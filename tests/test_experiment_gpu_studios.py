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

        def upstream(req: httpx.Request):
            self.seen.append((req.method, req.url.path, dict(req.headers)))
            path = req.url.path
            if path == "/health":
                return httpx.Response(200, json={"ok": True, "engine": "configured"})
            if path == "/ready":
                return httpx.Response(200, json={"ready": True})
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
                return httpx.Response(200, content=b"\x89PNG\r\n\x1a\nphoto")
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

    def test_qwen_edit_base64_request_and_scene_gallery_downloads(self):
        import base64
        import json
        self.login()
        headers = self.token("imagen")
        self.configure("imagen", headers)
        base = "/api/experiment-gpu/imagen"
        reference = b"\x89PNG\r\n\x1a\nreference"
        seen = []

        def worker(req):
            seen.append(req.url.path)
            if req.url.path == "/capabilities":
                return httpx.Response(200, json={
                    "engine": "qwen-image-edit-2511", "supported_modes": ["edit"]})
            if req.url.path == "/jobs" and req.method == "POST":
                body = json.loads(req.content)
                self.assertEqual(body["mode"], "edit")
                self.assertEqual(len(body["scenes"]), 2)
                for scene in body["scenes"]:
                    self.assertEqual(base64.b64decode(scene["references"][0]), reference)
                    self.assertNotIn("width", scene)
                    self.assertNotIn("height", scene)
                return httpx.Response(202, json={"id": "qwen-001"})
            if req.url.path == "/jobs/qwen-001":
                return httpx.Response(200, json={"status": "completed", "scenes": [
                    {"status": "failed", "output": None},
                    {"status": "completed", "output": "scene-1.png"}]})
            if req.url.path == "/jobs/qwen-001/output/1":
                self.assertEqual(req.headers["x-api-key"], "private-worker-secret")
                return httpx.Response(200, content=reference)
            self.fail("Unexpected Qwen request: " + req.url.path)

        self.bridge.transport = httpx.MockTransport(worker)
        self.assertEqual(self.client.post(base + "/jobs", headers=headers, data={
            "prompt": "Edit scene", "mode": "generate"}).status_code, 422)
        for data, files in (({"prompt": "Edit", "mode": "edit"}, {}),
                            ({"prompt": "x" * 2049, "mode": "edit"}, {})):
            self.assertEqual(self.client.post(base + "/jobs", headers=headers,
                data=data, files=files).status_code, 422)
        job = self.client.post(base + "/jobs", headers=headers, data={
            "prompt": "Edit scene", "mode": "edit", "count": 2},
            files={"reference": ("reference.png", reference, "image/png")})
        self.assertEqual(job.status_code, 202, job.text)
        self.assertNotIn("/assets", seen)
        detail = self.client.get(base + "/jobs/qwen-001").json()
        self.assertEqual(detail["output_count"], 1)
        for suffix, disposition in (("", "attachment"), ("?preview=1", "inline")):
            media = self.client.get(base + "/jobs/qwen-001/media" + suffix)
            self.assertEqual(media.status_code, 200, media.text)
            self.assertEqual(media.content, reference)
            self.assertIn(disposition, media.headers["content-disposition"])
        self.assertEqual(self.client.get(base + "/jobs/qwen-001/media?index=1").status_code, 404)

    def test_sana_generation_and_flux_asset_edit_remain_compatible(self):
        import json
        self.login()
        headers = self.token("imagen")
        self.configure("imagen", headers)
        base = "/api/experiment-gpu/imagen"
        for engine, mode in (("sana-sprint-06b", "generate"), ("flux2-klein-4b", "edit")):
            with self.subTest(engine=engine):
                calls = []
                def worker(req):
                    calls.append(req.url.path)
                    if req.url.path == "/capabilities":
                        return httpx.Response(200, json={"engine": engine, "modes":
                            ["generate"] if engine.startswith("sana") else ["generate", "edit"]})
                    if req.url.path == "/assets":
                        self.assertIn(b"reference", req.content)
                        return httpx.Response(200, json={"asset_id": "ref-001"})
                    if req.url.path == "/jobs":
                        body = json.loads(req.content)
                        self.assertEqual(body["mode"], mode)
                        self.assertEqual(body["scenes"][0]["width"], 1024)
                        self.assertEqual(body["scenes"][0]["height"], 576)
                        if mode == "edit":
                            self.assertEqual(body["scenes"][0]["references"], ["ref-001"])
                        return httpx.Response(202, json={"id": engine})
                    self.fail("Unexpected path: " + req.url.path)
                self.bridge.transport = httpx.MockTransport(worker)
                result = self.client.post(base + "/jobs", headers=headers,
                    data={"prompt": "Scene", "mode": mode}, files={"reference": (
                        "reference.png", b"\x89PNG\r\n\x1a\nreference", "image/png")})
                self.assertEqual(result.status_code, 202, result.text)
                self.assertEqual("/assets" in calls, mode == "edit")
                if engine.startswith("sana"):
                    self.assertEqual(self.client.post(base + "/jobs", headers=headers,
                        data={"prompt": "Edit", "mode": "edit"}).status_code, 422)

    def test_imagen_download_contracts_reject_untrusted_paths(self):
        self.login()
        headers = self.token("imagen")
        self.configure("imagen", headers)
        self.client.post("/api/experiment-gpu/imagen/jobs", headers=headers,
                         data={"prompt": "Photorealistic scene"})
        for path, expected in (
            ("/outputs/img-001/scene-000.png", 200),  # FLUX / Z-Image
            ("/jobs/img-001/outputs/scene-001.png", 200),  # Sana Sprint
            ("https://worker.example/jobs/img-001/outputs/scene-001.png", 200),
            ("/jobs/other/outputs/scene-001.png", 502),
            ("/jobs/img-001/outputs/../secret.png", 502),
            ("/jobs/img-001/outputs/%2e%2e%2fsecret.png", 502),
            ("/jobs/img-001/outputs/scene.png?token=secret", 502),
            ("https://evil.example/outputs/img-001/scene.png", 502),
            ("https://worker.example:8443/outputs/img-001/scene.png", 502),
            ("https://worker.example:bad/outputs/img-001/scene.png", 502),
            ("/jobs/img-001/output/0", 502),  # No fabricated Qwen filename
        ):
            with self.subTest(path=path):
                requests = []
                def worker(req):
                    requests.append(req.url.path)
                    if req.url.path == "/jobs/img-001":
                        return httpx.Response(200, json={"status": "completed",
                            "outputs": [{"url": path}]})
                    return httpx.Response(200, content=b"\x89PNG\r\n\x1a\nphoto")
                self.bridge.transport = httpx.MockTransport(worker)
                result = self.client.get("/api/experiment-gpu/imagen/jobs/img-001/media")
                self.assertEqual(result.status_code, expected, result.text)
                if expected != 200:
                    self.assertEqual(requests, ["/jobs/img-001"])

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

    def test_candidate_imagen_voice_gpu_writes_are_exact_and_csrf_guarded(self):
        from precutover_policy import mutation_allowed

        for studio in ("imagen", "voice-clone"):
            for action in ("config", "jobs"):
                path = f"/api/experiment-gpu/{studio}/{action}"
                self.assertTrue(mutation_allowed("POST", path, False), path)
        self.assertTrue(mutation_allowed(
            "POST", "/api/experiment-gpu/imagen/jobs/img_A-1/cancel", False
        ))
        for method, path in (
            ("POST", "/api/experiment-gpu/imagen/admin"),
            ("POST", "/api/experiment-gpu/imagen/config/rotate"),
            ("POST", "/api/experiment-gpu/voice-clone/jobs/img_A-1/cancel"),
            ("DELETE", "/api/experiment-gpu/imagen/config"),
            ("POST", "/api/experiment-gpu/imagen/jobs/../../other/cancel"),
            ("POST", "/api/experiment-gpu/imagen/jobs/" + "a" * 129 + "/cancel"),
            ("POST", "/api/experiment-gpu/unknown/config"),
        ):
            self.assertFalse(mutation_allowed(method, path, False), (method, path))

        # Allowlisting must not bypass CSRF or owner-scoped configuration.
        self.login("alice")
        csrf = self.token("imagen")
        config_path = "/api/experiment-gpu/imagen/config"
        self.assertEqual(self.client.post(config_path, json={
            "endpoint": "https://worker.example"
        }).status_code, 403)
        self.assertEqual(self.configure("imagen", csrf).status_code, 200)
        self.login("bob")
        self.assertFalse(self.client.get(config_path).json()["configured"])

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
        self.assertIn('href="/concat-studio/concat/">Concat</a>', base)
        self.assertIn('href="/concat-studio/concat-uploader/">Concat Uploader</a>', base)
        self.assertIn('id="cloud-heading">Concat</h2>', concat)
        self.assertIn('id="hg-models" hidden', heygen)
        self.assertIn('id="eg-gallery" hidden', (root / "templates/experiment_gpu_studio.html").read_text())
        self.assertIn("register_cloud_render(app, templates, DATA_DIR", app)
        self.assertIn("register_heygen_studio(app, templates, DATA_DIR", app)
        self.assertIn("register_experiment_gpu_studios(app, templates, DATA_DIR", app)


if __name__ == "__main__":
    unittest.main()

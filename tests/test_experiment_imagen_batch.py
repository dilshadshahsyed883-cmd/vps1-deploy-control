"""Admin-only batch prompt, durable PNG, restart, ZIP, and offline regression tests."""
from __future__ import annotations

import io
import json
import os
import re
import struct
import sys
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

SCENE = Path(__file__).resolve().parents[1] / "apps/scene-builder/part01"
sys.path.insert(0, str(SCENE))
from experiment_gpu_studios import register_experiment_gpu_studios
from experiment_imagen_batch import ImagenBatchQueue, parse_prompt_lines, validate_png


def tiny_png():
    def chunk(kind, body):
        return (struct.pack(">I", len(body)) + kind + body +
                struct.pack(">I", zlib.crc32(kind + body) & 0xffffffff))
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    image = zlib.compress(b"\x00\x40\x80\xc0")
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", image) + chunk(b"IEND", b"")


class ImagenBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        prior = os.environ.get("EXPERIMENT_GPU_ALLOWED_HOSTS")
        os.environ["EXPERIMENT_GPU_ALLOWED_HOSTS"] = "worker.example"
        self.addCleanup(lambda: (
            os.environ.pop("EXPERIMENT_GPU_ALLOWED_HOSTS", None) if prior is None
            else os.environ.__setitem__("EXPERIMENT_GPU_ALLOWED_HOSTS", prior)))
        self.submissions = []

        def upstream(req):
            path = req.url.path
            if path == "/capabilities":
                return httpx.Response(200, json={"engine": "sana-sprint-06b", "modes": ["generate"]})
            if path == "/health":
                return httpx.Response(200, json={"ok": True})
            if path == "/ready":
                return httpx.Response(200, json={"ready": True})
            if path == "/jobs" and req.method == "POST":
                body = json.loads(req.content)
                self.submissions.append(body["scenes"][0]["prompt"])
                job = f"gpu-{len(self.submissions)}"
                return httpx.Response(202, json={"job_id": job})
            match = re.fullmatch(r"/jobs/(gpu-\d+)", path)
            if match:
                return httpx.Response(200, json={
                    "id": match.group(1), "status": "completed",
                    "outputs": [{"url": f"/jobs/{match.group(1)}/outputs/scene_001.png"}]})
            if re.fullmatch(r"/jobs/gpu-\d+/outputs/scene_001.png", path):
                return httpx.Response(200, content=tiny_png())
            return httpx.Response(404, json={"detail": "not found"})

        app = FastAPI()
        def login(req: Request):
            if not req.session.get("authenticated"):
                raise HTTPException(401, "Login required")
        self.bridge = register_experiment_gpu_studios(
            app, Jinja2Templates(directory=str(SCENE / "templates")),
            self.temp.name, login, transport=httpx.MockTransport(upstream))

        @app.get("/_login/{username}")
        def set_session(req: Request, username: str):
            req.session.clear()
            if username != "guest":
                req.session.update({"authenticated": True, "username": username,
                                    "is_admin": username == "alice"})
            return {"ok": True}
        app.add_middleware(SessionMiddleware, secret_key="imagen-batch-fixture")
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.client.get("/_login/alice")
        page = self.client.get("/imagen-studio/")
        self.csrf = {"X-Experiment-GPU-CSRF":
                     re.search(r'data-csrf="([^"]+)"', page.text).group(1)}
        config = self.client.post("/api/experiment-gpu/imagen/config",
            headers=self.csrf, json={
                "endpoint": "https://worker.example", "api_key": "test-worker-token",
                "auth_mode": "x-api-key"})
        self.assertEqual(config.status_code, 200, config.text)

    def test_parse_prompt_ids_are_strict_and_line_order_preserved(self):
        self.assertEqual(parse_prompt_lines(
            "prompt1: A sunrise: with mountains\nprompt2: A red panda"),
            [("prompt1", "A sunrise: with mountains"), ("prompt2", "A red panda")])
        for bad in ("prompt1: hello\nPROMPT1: duplicate", "../secret: bad",
                    "no colon", "prompt1:  ", "😄: funny"):
            with self.subTest(bad=bad):
                with self.assertRaises(HTTPException):
                    parse_prompt_lines(bad)
        with self.assertRaises(HTTPException):
            parse_prompt_lines("\n".join(f"p{i}: sunset" for i in range(1001)))

    def test_png_crc_and_incomplete_file_are_rejected(self):
        validate_png(tiny_png())
        for bad in (tiny_png()[:-7], tiny_png()[:-8] + b"badbytes",
                    b"\x89PNG\r\n\x1a\nnotpng"):
            with self.assertRaises(HTTPException):
                validate_png(bad)

    def test_batch_is_persistent_and_zip_works_when_gpu_offline(self):
        base = "/api/experiment-gpu/imagen"
        created = self.client.post(base + "/batches", headers=self.csrf,
            json={"prompts": "prompt1: Photorealistic sunrise\nprompt2: Photorealistic moon",
                  "width": 1024, "height": 576})
        self.assertEqual(created.status_code, 202, created.text)
        bid = created.json()["batch_id"]
        queue = self.bridge.imagen_queue
        # Two rounds per item: submit, poll completed, store on VPS.
        for _ in range(8):
            queue.tick()
        details = self.client.get(base + f"/batches/{bid}").json()
        self.assertEqual([row["state"] for row in details["items"]], ["saved", "saved"])
        self.assertEqual(self.submissions, ["Photorealistic sunrise", "Photorealistic moon"])
        self.assertTrue((queue.root / "batches" / bid / "prompt1.png").is_file())
        only_one = self.client.get(base + f"/batches/{bid}/download?ids=prompt2")
        self.assertEqual(only_one.status_code, 200, only_one.text[:300])
        with zipfile.ZipFile(io.BytesIO(only_one.content)) as z:
            self.assertEqual(z.namelist(), ["prompt2.png"])
            self.assertEqual(z.read("prompt2.png"), tiny_png())
        zip_all = self.client.get(base + f"/batches/{bid}/download")
        with zipfile.ZipFile(io.BytesIO(zip_all.content)) as z:
            self.assertEqual(z.namelist(), ["prompt1.png", "prompt2.png"])
        self.bridge.transport = httpx.MockTransport(lambda req: httpx.Response(503))
        offline = self.client.get(base + f"/batches/{bid}/media/prompt1")
        self.assertEqual(offline.status_code, 200)
        self.assertEqual(offline.headers["x-vps-media-saved"], "true")
        self.assertEqual(offline.content, tiny_png())
        status = self.client.get(base + "/jobs/gpu-1")
        self.assertEqual(status.json()["saved_to_vps"], 1)
        self.assertEqual(self.client.get(base + "/jobs/gpu-1/media").content, tiny_png())

    def test_single_job_is_auto_saved_and_survives_worker_shutdown(self):
        base = "/api/experiment-gpu/imagen"
        post = self.client.post(base + "/jobs", headers=self.csrf,
            data={"prompt": "Photorealistic burger", "mode": "generate"})
        self.assertEqual(post.status_code, 202, post.text)
        self.bridge.imagen_queue.tick()
        self.assertTrue((self.bridge.imagen_queue.root / "single" / "gpu-1" /
                         "scene_001.png").is_file())
        self.bridge.transport = httpx.MockTransport(lambda req: httpx.Response(503))
        result = self.client.get(base + "/jobs/gpu-1/media")
        self.assertEqual(result.status_code, 200)
        self.assertIn("scene_001.png", result.headers["content-disposition"])

    def test_admin_only_csrf_and_no_bad_selection_or_overwrite(self):
        base = "/api/experiment-gpu/imagen"
        self.assertEqual(self.client.post(base + "/batches",
                         json={"prompts": "prompt1: hello"}).status_code, 403)
        batch = self.client.post(base + "/batches", headers=self.csrf,
            json={"prompts": "p1: Photorealistic lake"}).json()["batch_id"]
        self.assertEqual(self.client.get(base + f"/batches/{batch}/download?ids=bad").status_code, 409)
        self.assertEqual(self.client.post(base + f"/batches/{batch}/retry",
                        headers=self.csrf, json={"ids": ["p1"]}).status_code, 409)
        self.assertEqual(self.client.post(base + f"/batches/{batch}/retry",
                        json={"ids": ["p1"]}).status_code, 403)
        self.client.get("/_login/bob")
        self.assertEqual(self.client.get(base + "/batches").status_code, 403)
        self.assertEqual(self.client.get(base + f"/batches/{batch}").status_code, 403)
        self.assertEqual(self.client.get(base + f"/batches/{batch}/download").status_code, 403)
        self.assertEqual(self.client.post(base + "/batches", headers=self.csrf,
                        json={"prompts": "p2: lake"}).status_code, 403)

    def test_queue_reattaches_after_new_process_object(self):
        base = "/api/experiment-gpu/imagen"
        batch = self.client.post(base + "/batches", headers=self.csrf,
            json={"prompts": "prompt1: Photorealistic green hills"}).json()["batch_id"]
        self.bridge.imagen_queue.tick()  # stores remote job id, not yet image
        self.assertEqual(len(self.submissions), 1)
        restarted = ImagenBatchQueue(self.bridge)
        restarted.tick()
        self.assertEqual(len(self.submissions), 1, "Must reattach, not submit duplicate")
        self.assertEqual(restarted.details(batch)["items"][0]["state"], "saved")


if __name__ == "__main__":
    unittest.main()

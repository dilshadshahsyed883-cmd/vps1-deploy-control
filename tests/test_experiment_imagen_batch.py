"""Admin-only batch prompt, durable PNG, restart, ZIP, and offline regression tests."""
from __future__ import annotations

import io
import json
import os
import re
import struct
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
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
        self.upstream_running = False

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
                    "id": match.group(1), "status": "rendering" if self.upstream_running else "completed",
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

    def test_batch_pipeline_keeps_gpu_fed_without_exceeding_eight_inflight(self):
        base = "/api/experiment-gpu/imagen"
        self.upstream_running = True
        response = self.client.post(base + "/batches", headers=self.csrf,
            json={"prompts": "\\n".join(f"p{i}: landscape {i}" for i in range(20))})
        self.assertEqual(response.status_code, 202, response.text)
        batch_id = response.json()["batch_id"]
        queue = self.bridge.imagen_queue
        queue.tick()
        self.assertEqual(len(self.submissions), 8, "First tick must pipeline, not submit one")
        states = [x["state"] for x in queue.details(batch_id)["items"]]
        self.assertEqual(states.count("submitted"), 8)
        self.assertEqual(states.count("pending"), 12)
        queue.tick()  # Jobs are still rendering: cap must prevent runaway submissions.
        self.assertEqual(len(self.submissions), 8)

        self.upstream_running = False
        with patch("experiment_imagen_batch.time.time", return_value=time.time() + 10):
            queue.tick()
        self.assertEqual(len(self.submissions), 16)
        states = [x["state"] for x in queue.details(batch_id)["items"]]
        self.assertEqual(states.count("saved"), 8)
        self.assertEqual(states.count("submitted"), 8)
        with patch("experiment_imagen_batch.time.time", return_value=time.time() + 20):
            queue.tick()
        self.assertEqual(len(self.submissions), 20)
        with patch("experiment_imagen_batch.time.time", return_value=time.time() + 30):
            queue.tick()
        states = [x["state"] for x in queue.details(batch_id)["items"]]
        self.assertEqual(states, ["saved"] * 20)
        self.assertEqual(len(self.submissions), 20, "No duplicate remote job submissions")
        self.assertTrue((queue.root / "batches" / batch_id / "p19.png").exists())

    def test_gpu_unavailable_stops_new_batch_submissions_temporarily(self):
        base = "/api/experiment-gpu/imagen"
        result = self.client.post(base + "/batches", headers=self.csrf,
            json={"prompts": "\\n".join(f"p{i}: test {i}" for i in range(20))})
        batch_id = result.json()["batch_id"]
        original = self.bridge.transport
        self.bridge.transport = httpx.MockTransport(
            lambda req: httpx.Response(503) if req.method == "POST" and req.url.path == "/jobs"
            else original.handle_request(req))
        queue = self.bridge.imagen_queue
        queue.tick()
        states = [x["state"] for x in queue.details(batch_id)["items"]]
        self.assertEqual(states.count("waiting_gpu"), 1)
        self.assertEqual(states.count("pending"), 19)
        self.assertGreater(queue.gpu_retry_after, time.monotonic())
        queue.tick()
        self.assertEqual(len(self.submissions), 0, "Offline worker must not receive a burst")

    def test_single_job_transfer_still_runs_under_batch_load(self):
        base = "/api/experiment-gpu/imagen"
        created = self.client.post(base + "/jobs", headers=self.csrf,
            data={"prompt": "Single request", "mode": "generate"})
        self.assertEqual(created.status_code, 202)
        job_id = created.json()["id"]
        self.client.post(base + "/batches", headers=self.csrf,
            json={"prompts": "\\n".join(f"p{i}: image {i}" for i in range(20))})
        self.bridge.imagen_queue.tick()
        path = self.bridge.imagen_queue.root / "single" / job_id / "scene_001.png"
        self.assertTrue(path.is_file(), "Existing single-image VPS transfer must not starve")

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


    def test_delete_individual_batch_png_and_entire_finished_batch(self):
        base = "/api/experiment-gpu/imagen"
        created = self.client.post(base + "/batches", headers=self.csrf,
            json={"prompts": "p1: landscape\np2: city"}).json()
        batch_id = created["batch_id"]
        for _ in range(8):
            self.bridge.imagen_queue.tick()
        batch_dir = self.bridge.imagen_queue.root / "batches" / batch_id
        self.assertTrue((batch_dir / "p1.png").exists())
        detail = self.client.get(base + f"/batches/{batch_id}").json()
        job_id = detail["items"][0]["remote_job_id"]
        delete = self.client.delete(base + f"/jobs/{job_id}/outputs/0", headers=self.csrf)
        self.assertEqual(delete.status_code, 200, delete.text)
        self.assertFalse((batch_dir / "p1.png").exists())
        self.assertEqual(self.client.get(base + f"/batches/{batch_id}/media/p1").status_code, 404)
        self.assertEqual(self.client.get(base + f"/jobs/{job_id}/media").status_code, 404)
        self.assertEqual(self.client.get(base + f"/jobs/{job_id}").json()["output_indices"], [])
        self.assertEqual(self.client.get(base + f"/batches/{batch_id}").json()["items"][0]["state"],
                         "deleted")
        zipped = self.client.get(base + f"/batches/{batch_id}/download")
        with zipfile.ZipFile(io.BytesIO(zipped.content)) as archive:
            self.assertEqual(archive.namelist(), ["p2.png"])
        done = self.client.delete(base + f"/batches/{batch_id}", headers=self.csrf)
        self.assertEqual(done.status_code, 200, done.text)
        self.assertFalse(batch_dir.exists())
        self.assertEqual(self.client.get(base + f"/batches/{batch_id}").status_code, 404)
        self.assertEqual(self.client.get(base + f"/jobs/{job_id}").status_code, 404)

    def test_delete_single_output_survives_gpu_offline_and_delete_job(self):
        base = "/api/experiment-gpu/imagen"
        created = self.client.post(base + "/jobs", headers=self.csrf,
            data={"prompt": "A landscape"}).json()
        job_id = created["id"]
        self.bridge.imagen_queue.tick()
        stored = self.bridge.imagen_queue.root / "single" / job_id / "scene_001.png"
        self.assertTrue(stored.is_file())
        self.bridge.transport = httpx.MockTransport(lambda req: httpx.Response(503))
        self.assertEqual(self.client.get(base + f"/jobs/{job_id}/media").status_code, 200)
        self.assertEqual(self.client.delete(base + f"/jobs/{job_id}/outputs/0").status_code, 403)
        removed = self.client.delete(base + f"/jobs/{job_id}/outputs/0", headers=self.csrf)
        self.assertEqual(removed.status_code, 200, removed.text)
        self.assertFalse(stored.exists())
        self.assertEqual(self.client.get(base + f"/jobs/{job_id}/media").status_code, 404)
        self.assertEqual(self.client.get(base + f"/jobs/{job_id}").json()["output_count"], 0)
        self.assertEqual(self.client.delete(
            base + f"/jobs/{job_id}/outputs/0", headers=self.csrf).status_code, 404)
        deleted = self.client.delete(base + f"/jobs/{job_id}", headers=self.csrf)
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(self.client.get(base + f"/jobs/{job_id}").status_code, 404)
        self.assertEqual(self.client.get(base + "/jobs").json()["items"], [])
        self.assertFalse(stored.parent.exists())

    def test_delete_queued_batch_and_reject_active_work_or_other_users(self):
        base = "/api/experiment-gpu/imagen"
        batch_id = self.client.post(base + "/batches", headers=self.csrf,
            json={"prompts": "p1: one\np2: two"}).json()["batch_id"]
        self.assertEqual(self.client.delete(base + f"/batches/{batch_id}").status_code, 403)
        self.assertEqual(self.client.delete(base + f"/batches/{batch_id}", headers=self.csrf).status_code, 200)
        self.assertEqual(len(self.submissions), 0, "Deleting pending batch must not start GPU work")
        original = self.bridge.transport
        offline_id = self.client.post(base + "/batches", headers=self.csrf,
            json={"prompts": "offline1: no GPU yet"}).json()["batch_id"]
        self.bridge.transport = httpx.MockTransport(lambda req: httpx.Response(503))
        self.bridge.imagen_queue.tick()  # waiting_gpu, no upstream job ID
        self.assertEqual(self.client.get(base + f"/batches/{offline_id}").json()[
            "items"][0]["state"], "waiting_gpu")
        self.assertEqual(self.client.delete(base + f"/batches/{offline_id}",
            headers=self.csrf).status_code, 200)
        self.bridge.transport = original
        batch_id = self.client.post(base + "/batches", headers=self.csrf,
            json={"prompts": "p3: three"}).json()["batch_id"]
        self.bridge.imagen_queue.tick()  # submitted, but not saved
        self.assertEqual(self.client.delete(base + f"/batches/{batch_id}",
            headers=self.csrf).status_code, 409)
        self.client.get("/_login/bob")
        self.assertEqual(self.client.delete(base + f"/batches/{batch_id}",
            headers=self.csrf).status_code, 403)
        self.assertEqual(self.client.delete(base + "/jobs/gpu-1",
            headers=self.csrf).status_code, 403)

    def test_precutover_allows_only_exact_imagen_delete_routes(self):
        from precutover_policy import mutation_allowed
        for path in (
            "/api/experiment-gpu/imagen/jobs/gpu-1",
            "/api/experiment-gpu/imagen/jobs/gpu-1/outputs/0",
            "/api/experiment-gpu/imagen/batches/" + "a" * 32,
        ):
            self.assertTrue(mutation_allowed("DELETE", path, False), path)
        for path in (
            "/api/experiment-gpu/voice-clone/jobs/gpu-1",
            "/api/experiment-gpu/imagen/jobs/../../foo",
            "/api/experiment-gpu/imagen/jobs/gpu-1/outputs/8",
            "/api/experiment-gpu/imagen/batches/" + "z" * 32,
        ):
            self.assertFalse(mutation_allowed("DELETE", path, False), path)

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

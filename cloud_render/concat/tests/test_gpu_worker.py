"""Offline tests for authenticated Concat HTTP bridge and retained project data."""
import hashlib
import http.client
import io
import json
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cloud_render.concat.gpu.worker import Handler, RenderWorker, SaladDualStackHTTPServer, WorkerError

MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 80
TOKEN = "a" * 48


def bundle():
    with io.BytesIO() as buffer:
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("bundle.json", json.dumps({"version": 1,
                "project_file": "concat.json", "references": []}))
            archive.writestr("project/concat.json", json.dumps({"media": []}))
        return buffer.getvalue()


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.rendered = []
        self.delivered = []
        self.worker = RenderWorker(Path(self.temp.name), binary=Path(sys.executable),
            token=TOKEN, renderer=self.render, deliver=self.deliver)
        self.data = bundle()
        self.sha = hashlib.sha256(self.data).hexdigest()

    def render(self, project, output):
        self.rendered.append(project)
        self.assertTrue((project / "concat.json").is_file())
        output.write_bytes(MP4)

    def deliver(self, job, output):
        self.delivered.append(job)

    def tearDown(self):
        self.worker.pool.shutdown(wait=True)
        self.temp.cleanup()

    def wait(self):
        self.worker.pool.shutdown(wait=True)

    def add(self, job="abc"):
        return self.worker.accept(job, io.BytesIO(self.data), len(self.data), self.sha)

    def test_queued_project_restores_and_renders_local_mp4(self):
        self.add()
        self.wait()
        entry = self.worker.status("abc")
        self.assertEqual(entry["state"], "rendered")
        self.assertEqual(entry["output_sha256"], hashlib.sha256(MP4).hexdigest())
        self.assertEqual(len(self.rendered), 1)
        self.assertTrue((self.worker.inputs / "abc/project.zip").exists())
        self.assertTrue((self.worker.outputs / "abc.mp4").is_file())

    def test_refuse_wrong_checksum_duplicate_and_bad_ids(self):
        with self.assertRaises(WorkerError):
            self.worker.accept("../oops", io.BytesIO(self.data), len(self.data), self.sha)
        with self.assertRaises(WorkerError):
            self.worker.accept("nope", io.BytesIO(self.data), len(self.data), "0" * 64)
        self.assertFalse((self.worker.inputs / "nope").exists())
        self.add("abc")
        with self.assertRaises(WorkerError) as raised:
            self.add("abc")
        self.assertEqual(raised.exception.status, 409)

    def test_refuse_truncated_upload_and_keep_queue_clean(self):
        with self.assertRaises(WorkerError):
            self.worker.accept("half", io.BytesIO(self.data[:5]), len(self.data), self.sha)
        self.assertFalse((self.worker.inputs / "half").exists())

    def test_delivery_ack_cleanup_is_per_job(self):
        self.worker.receiver = "https://app.scenebuilder.online"
        self.add("one")
        self.wait()
        self.assertEqual(self.worker.status("one")["state"], "delivered")
        self.assertEqual(self.delivered, ["one"])
        self.assertFalse((self.worker.outputs / "one.mp4").exists())
        self.assertFalse((self.worker.inputs / "one").exists())

    def test_delivery_failure_keeps_input_and_output(self):
        self.worker.receiver = "https://app.scenebuilder.online"
        def fail(job, output):
            raise RuntimeError("receiver unavailable")
        self.worker.deliver = fail
        self.add("one")
        self.wait()
        self.assertEqual(self.worker.status("one")["state"], "delivery_failed")
        self.assertTrue((self.worker.inputs / "one/project.zip").exists())
        self.assertTrue((self.worker.outputs / "one.mp4").exists())

    def test_interrupted_render_requires_explicit_recovery(self):
        self.worker._save("old", {"job": "old", "state": "running"})
        fresh = RenderWorker(Path(self.temp.name), binary=Path(sys.executable), token=TOKEN,
                             renderer=self.render)
        self.assertEqual(fresh.status("old")["state"], "failed")
        fresh.pool.shutdown(wait=True)

    def test_native_cli_uses_upstream_project_open_and_export_run(self):
        project = Path(self.temp.name) / "project"
        project.mkdir()
        output = Path(self.temp.name) / "output.mp4"

        def simulate(cmd, **kwargs):
            self.assertEqual(cmd[1], "api")
            calls = [json.loads(line) for line in kwargs["input"].decode().splitlines()]
            self.assertEqual([call["method"] for call in calls], ["project.open", "export.run"])
            self.assertEqual(calls[1]["params"]["output"], str(output))
            output.write_bytes(MP4)
            for msg in (
                {"jsonrpc": "2.0", "id": 1, "result": {"project": {}}},
                {"jsonrpc": "2.0", "id": 2, "result": {"job": "j1"}},
                {"jsonrpc": "2.0", "method": "export.done", "params": {"job": "j1"}}
            ):
                kwargs["stdout"].write((json.dumps(msg) + "\n").encode())
            return SimpleNamespace(returncode=0)
        with patch("cloud_render.concat.gpu.worker.subprocess.run", side_effect=simulate):
            self.worker._native_render(project, output)

    def test_http_auth_health_and_ingest(self):
        server = SaladDualStackHTTPServer(("::", 0), Handler)
        server.worker = self.worker
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        try:
            # IPv6 must accept Salad Gateway ingress, while IPv4 stays usable.
            conn6 = http.client.HTTPConnection("::1", server.server_address[1], timeout=10)
            try:
                conn6.request("GET", "/health/ready")
                response6 = conn6.getresponse()
                self.assertEqual(response6.status, 200)
                response6.read()
            finally:
                conn6.close()
            conn.request("GET", "/health/ready")
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            conn.request("GET", "/api/jobs/abc")
            response = conn.getresponse()
            self.assertEqual(response.status, 401)
            response.read()
            # 401 intentionally closes the unauthorized socket to avoid body desync.
            conn.close()
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
            conn.request("POST", "/api/jobs/job-1", body=self.data, headers={
                "Authorization": "Bearer " + TOKEN,
                "Content-Type": "application/zip",
                "X-Content-SHA256": self.sha})
            response = conn.getresponse()
            self.assertEqual(response.status, 202)
            response.read()
            self.wait()
            conn.request("GET", "/api/jobs/job-1",
                         headers={"Authorization": "Bearer " + TOKEN})
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["state"], "rendered")
            conn.request("GET", "/api/jobs/job-1/output",
                         headers={"Authorization": "Bearer " + TOKEN})
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), MP4)
        finally:
            conn.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()

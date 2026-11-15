"""Offline HeyGen submission and retry behavior, without a GPU."""
from __future__ import annotations

import io
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx

SCENE = Path(__file__).resolve().parents[1] / "apps/scene-builder/part01"
sys.path.insert(0, str(SCENE))
from heygen_outbox import HeygenOutbox


class OutboxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "bridge.sqlite3"
        with self.connection() as con:
            con.execute("CREATE TABLE jobs(owner TEXT,id TEXT,created_at TEXT,PRIMARY KEY(owner,id))")
        self.has_capability = True
        self.online = True
        self.accepted = []
        self.raise_after_accept = False

        def upstream(req):
            if not self.online:
                raise httpx.ConnectError("worker offline", request=req)
            if req.url.path.endswith("/engine"):
                return httpx.Response(200, json={
                    "supports_idempotent_submit": self.has_capability,
                    "entrypoint_found": True, "models_ready": True})
            if req.url.path.endswith("/jobs") and req.method == "POST":
                job_id = req.headers.get("X-Heygen-Request-ID")
                self.accepted.append(job_id)
                if self.raise_after_accept:
                    self.raise_after_accept = False
                    raise httpx.ReadError("network dropped after accepting job", request=req)
                return httpx.Response(202, json={"id": job_id, "status": "queued"})
            return httpx.Response(404)
        self.transport = httpx.MockTransport(upstream)
        self.box = HeygenOutbox(self.root, self.connection, self.client, auto_start=False)
        self.addCleanup(self.box.close)

    def connection(self):
        con = sqlite3.connect(str(self.db))
        con.row_factory = sqlite3.Row
        return con

    def client(self, owner, timeout=600):
        return httpx.Client(base_url="https://worker.test/",
                            transport=self.transport, timeout=timeout)

    def queued(self, owner="alice"):
        return self.box.add(
            owner, SimpleNamespace(file=io.BytesIO(b"\x89PNG\r\n\x1a\nimage")),
            SimpleNamespace(file=io.BytesIO(b"RIFF\x10\0\0\0WAVEaudio")),
            ".png", ".wav")["id"]

    def test_offline_recovered_without_duplicate_request_id(self):
        job_id = self.queued()
        self.online = False
        self.box.dispatch("alice", job_id)
        self.assertEqual(self.box.snapshot("alice")[job_id]["state"], "waiting_network")
        self.online = True
        self.raise_after_accept = True
        self.box.dispatch("alice", job_id)
        self.assertEqual(self.box.snapshot("alice")[job_id]["state"], "waiting_network")
        self.box.dispatch("alice", job_id)
        self.assertEqual(self.box.snapshot("alice")[job_id]["state"], "sent")
        self.assertEqual(self.accepted, [job_id, job_id])
        self.assertFalse((self.box.folder("alice", job_id) / "image.png").exists())
        with self.connection() as con:
            self.assertIsNotNone(con.execute(
                "SELECT 1 FROM jobs WHERE owner=? AND id=?", ("alice", job_id)).fetchone())

    def test_worker_ignoring_request_id_fails_closed(self):
        job_id = self.queued()
        self.transport = httpx.MockTransport(lambda request:
            httpx.Response(200, json={"supports_idempotent_submit": True,
                                       "entrypoint_found": True, "models_ready": True})
            if request.url.path.endswith("/engine") else
            httpx.Response(202, json={"id": "b" * 32}))
        self.box.dispatch("alice", job_id)
        self.assertEqual(self.box.snapshot("alice")[job_id]["state"], "failed")
        # No further automatic dispatch: manual retry is necessary.
        self.box.dispatch("alice", job_id)
        self.assertEqual(self.box.snapshot("alice")[job_id]["state"], "failed")

    def test_old_worker_never_receives_auto_retry(self):
        job_id = self.queued()
        self.has_capability = False
        self.box.dispatch("alice", job_id)
        self.assertEqual(self.box.snapshot("alice")[job_id]["state"], "waiting_upgrade")
        self.assertEqual(self.accepted, [])
        self.has_capability = True
        self.box.dispatch("alice", job_id)
        self.assertEqual(self.box.snapshot("alice")[job_id]["state"], "sent")
        self.assertEqual(self.accepted, [job_id])

    def test_pause_resume_and_cancel_delete_are_owner_scoped(self):
        job_id = self.queued()
        self.box.control("alice", "pause")
        self.box.dispatch("alice", job_id)
        self.assertEqual(self.accepted, [])
        self.assertEqual(self.box.state("alice"), "paused")
        self.assertEqual(self.box.state("bob"), "running")
        self.box.control("alice", "resume")
        self.assertEqual(self.box.action("alice", job_id, "cancel")["status"], "cancelled")
        self.assertEqual(self.box.snapshot("bob"), {})
        self.assertTrue(self.box.action("alice", job_id, "delete")["deleted"])
        self.assertNotIn(job_id, self.box.snapshot("alice"))
        with self.connection() as con:
            self.assertIsNone(con.execute(
                "SELECT 1 FROM jobs WHERE owner=? AND id=?", ("alice", job_id)).fetchone())

    def test_crash_recovery_retries_ambiguous_upload(self):
        job_id = self.queued()
        with self.connection() as con:
            con.execute("UPDATE heygen_outbox SET state='uploading' WHERE id=?", (job_id,))
        new = HeygenOutbox(self.root, self.connection, self.client, auto_start=False)
        self.addCleanup(new.close)
        self.assertEqual(new.snapshot("alice")[job_id]["state"], "waiting_network")
        new.dispatch("alice", job_id)
        self.assertEqual(new.snapshot("alice")[job_id]["state"], "sent")

    def test_completed_gpu_job_mirrors_without_open_browser(self):
        job_id = self.queued()
        self.box.dispatch("alice", job_id)
        self.assertEqual(self.box.snapshot("alice")[job_id]["state"], "sent")
        completed = []
        self.transport = httpx.MockTransport(lambda request:
            httpx.Response(200, json={"status": "completed"}))
        watcher = HeygenOutbox(self.root, self.connection, self.client, auto_start=False,
                              on_complete=lambda owner, job: completed.append((owner, job)),
                              cached=lambda owner, job: False)
        self.addCleanup(watcher.close)
        watcher.observe_completed()
        self.assertEqual(completed, [("alice", job_id)])
        cached_watcher = HeygenOutbox(self.root, self.connection, self.client, auto_start=False,
                                     on_complete=lambda owner, job: completed.append((owner, job)),
                                     cached=lambda owner, job: True)
        self.addCleanup(cached_watcher.close)
        cached_watcher.observe_completed()
        self.assertEqual(len(completed), 1)

    def test_unconfigured_worker_inputs_are_private_and_persisted(self):
        job_id = self.queued()
        path = self.box.folder("alice", job_id)
        self.assertTrue((path / "image.png").is_file())
        self.assertTrue((path / "audio.wav").is_file())
        self.assertNotEqual(self.box.folder("alice", job_id), self.box.folder("bob", job_id))
        self.assertTrue(self.box.action("alice", job_id, "delete")["deleted"])
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()

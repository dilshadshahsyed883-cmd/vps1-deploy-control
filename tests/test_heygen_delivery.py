"""Offline and verified output delivery tests for the isolated HeyGen VPS mirror."""
from __future__ import annotations

import hashlib
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

import httpx

SCENE = Path(__file__).resolve().parents[1] / "apps/scene-builder/part01"
sys.path.insert(0, str(SCENE))
from heygen_delivery import VerifiedMedia, verify_mp4


class VerifiedMediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.db = self.base / "jobs.sqlite3"
        with self.connection() as con:
            con.execute("CREATE TABLE jobs(owner TEXT,id TEXT,PRIMARY KEY(owner,id))")
            con.execute("INSERT INTO jobs VALUES(?,?)", ("alice", "a" * 32))
            con.execute("INSERT INTO jobs VALUES(?,?)", ("bob", "a" * 32))
        self.payload = b"\x00\x00\x00\x18ftypmp42" + b"x" * 4096
        self.respond = lambda req: httpx.Response(
            200, content=self.payload, headers={"content-type": "video/mp4"})
        self.calls = 0

        def transport(request: httpx.Request):
            self.calls += 1
            return self.respond(request)

        self.network = httpx.MockTransport(transport)
        self.media = VerifiedMedia(self.base, self.connection, self.client,
                                  validate=self.validation)

    def connection(self):
        con = sqlite3.connect(str(self.db))
        con.row_factory = sqlite3.Row
        return con

    def client(self, owner, timeout=300):
        return httpx.Client(base_url="https://worker.example/",
                            transport=self.network, timeout=timeout)

    @staticmethod
    def validation(path):
        if not path.read_bytes().startswith(b"\x00\x00\x00\x18ftyp"):
            raise ValueError("bad MP4")
        return {"frames": 25, "duration": 1.0}

    def test_successful_owner_scoped_mirror_and_recovery(self):
        job = "a" * 32
        self.media._mirror("alice", job)
        info = self.media.snapshot("alice", job)
        self.assertEqual(info["state"], "ready")
        self.assertEqual(info["size"], len(self.payload))
        self.assertEqual(info["sha256"], hashlib.sha256(self.payload).hexdigest())
        self.assertEqual(self.media.snapshot("bob", job)["state"], "pending")
        self.assertNotEqual(self.media.file("alice", job), self.media.file("bob", job))
        response = self.media.response("alice", job, True)
        self.assertIsNotNone(response)
        self.assertEqual(response.filename, f"heygen-{job}.mp4")
        self.assertEqual(response.content_disposition_type, "attachment")
        # A service restart can recover a completed filesystem rename without redownloading.
        with self.connection() as con:
            con.execute("DELETE FROM heygen_media WHERE owner=? AND id=?", ("alice", job))
        first_calls = self.calls
        self.media._mirror("alice", job)
        self.assertEqual(self.calls, first_calls)
        self.assertEqual(self.media.snapshot("alice", job)["state"], "ready")

    def test_failed_transfer_preserves_remote_and_never_serves_bad_output(self):
        self.respond = lambda req: httpx.Response(
            200, content=b"not a video", headers={"content-type": "video/mp4"})
        self.media._mirror("alice", "a" * 32)
        self.assertEqual(self.media.snapshot("alice", "a" * 32)["state"], "failed")
        self.assertIsNone(self.media.response("alice", "a" * 32))
        self.assertFalse(self.media.file("alice", "a" * 32).exists())
        self.assertFalse(self.media.file("alice", "a" * 32).with_suffix(".part").exists())

    def test_missing_audio_video_probe_fails_closed(self):
        with self.assertRaises(ValueError):
            verify_mp4(Path(self.tmp.name) / "not-present.mp4")

    def test_oversized_response_header_fails_closed(self):
        self.respond = lambda req: httpx.Response(
            200, content=self.payload,
            headers={"content-type": "video/mp4", "content-length": str(4 * 1024**3)})
        self.media._mirror("alice", "a" * 32)
        self.assertEqual(self.media.snapshot("alice", "a" * 32)["state"], "failed")

    def test_owned_deletion_only_removes_one_owners_local_media(self):
        job = "a" * 32
        self.media._mirror("alice", job)
        self.media._mirror("bob", job)
        self.media.delete("alice", job)
        self.assertEqual(self.media.snapshot("alice", job)["state"], "pending")
        self.assertEqual(self.media.snapshot("bob", job)["state"], "ready")
        self.assertEqual(self.media.file("bob", job).read_bytes(), self.payload)

    def test_async_schedule_only_one_transfer(self):
        job = "a" * 32
        self.assertEqual(self.media.schedule("alice", job)["state"], "copying")
        for _ in range(100):
            if self.media.snapshot("alice", job)["state"] == "ready":
                break
            time.sleep(0.02)
        self.assertEqual(self.media.snapshot("alice", job)["state"], "ready")
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.media.schedule("alice", job)["state"], "ready")


if __name__ == "__main__":
    unittest.main()

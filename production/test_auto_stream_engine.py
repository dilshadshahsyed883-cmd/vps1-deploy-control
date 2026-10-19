"""Auto Stream engine CI: isolated ephemeral files, no live YouTube calls."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "apps" / "short-stream" / "part01"
sys.path.insert(0, str(SOURCE))
from auto_stream_engine import StreamEngine, scheduled_utc, router

CHANNEL = "UC" + "q" * 22


class StreamEngineTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="auto-stream-ci-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.media = self.base / "media"
        (self.media / "live").mkdir(parents=True)
        (self.media / "live" / "clip.mp4").write_bytes(b"test-media")
        self.engine = StreamEngine(self.base / "engine", self.media)
        self.data = dict(channel_id=CHANNEL, media_path="live/clip.mp4",
                         title="Live Trial", idempotency_key="trial-00001")

    def test_no_publishing_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            job = self.engine.create(self.data)
            self.assertEqual(job["status"], "DRAFT")
            self.engine.prepare(job["id"])
            with self.assertRaises(PermissionError):
                self.engine.start(job["id"])
            self.assertEqual(self.engine.tick(), 0)
            self.assertFalse(self.engine.health()["publishing_enabled"])
            self.assertFalse(self.engine.health()["youtube_live_verified"])

    def test_idempotent_create_and_conflicting_payload(self):
        first = self.engine.create(self.data)
        self.assertEqual(first["id"], self.engine.create(self.data)["id"])
        with self.assertRaisesRegex(ValueError, "Idempotency"):
            self.engine.create({**self.data, "title": "different"})

    def test_channel_and_path_validation(self):
        for bad in ("../elsewhere.mp4", "/etc/passwd", "live/missing.mp4", "live/../../../etc/passwd"):
            with self.assertRaises(ValueError):
                self.engine.create({**self.data, "media_path": bad})
        with self.assertRaises(ValueError):
            self.engine.create({**self.data, "channel_id": "not-a-youtube-channel"})
        (self.media / "live" / "redirect.mp4").symlink_to(self.media / "live" / "clip.mp4")
        with self.assertRaises(ValueError):
            self.engine.create({**self.data, "media_path": "live/redirect.mp4"})

    def test_timezone_required_and_utc_conversion(self):
        with self.assertRaises(ValueError):
            scheduled_utc("2026-10-20T10:00:00")
        self.assertEqual(scheduled_utc("2026-10-20T13:00:00+05:00"),
                         "2026-10-20T08:00:00+00:00")

    def test_separate_persistence_and_restart_reconciliation(self):
        j = self.engine.create(self.data)
        self.engine.prepare(j["id"])
        with self.engine._connect() as con:
            con.execute("UPDATE jobs SET status='INGESTING' WHERE id=?", (j["id"],))
        new_engine = StreamEngine(self.base / "engine", self.media)
        self.assertEqual(new_engine.get(j["id"])["status"], "INTERRUPTED")
        self.assertEqual(list(self.base.glob("*uploader*")), [])
        self.assertEqual(new_engine.get(j["id"])["channel_id"], CHANNEL)

    def test_schedule_is_draft_until_prepared(self):
        p = {**self.data, "scheduled_at": "2026-11-01T10:30:00+05:00"}
        job = self.engine.create(p)
        self.assertEqual(job["status"], "DRAFT")
        prepared = self.engine.prepare(job["id"])
        self.assertEqual(prepared["status"], "SCHEDULED")
        self.assertEqual(prepared["scheduled_at"], "2026-11-01T05:30:00+00:00")

    def test_secret_permissions_fail_closed(self):
        secret_dir = self.base / "engine" / "secrets"
        secret_dir.mkdir()
        file = secret_dir / (CHANNEL + ".key")
        file.write_text("fictional-stream-secret")
        file.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "0600"):
            self.engine._stream_key(CHANNEL)
        file.chmod(0o600)
        self.assertEqual(self.engine._stream_key(CHANNEL), "fictional-stream-secret")

    def test_channel_lease_blocks_parallel_active_jobs(self):
        a = self.engine.create(self.data)
        b = self.engine.create({**self.data, "idempotency_key": "trial-00002"})
        with self.engine._connect() as con:
            con.execute("UPDATE jobs SET status='INGESTING' WHERE id=?", (a["id"],))
            with self.assertRaises(Exception):
                con.execute("UPDATE jobs SET status='STARTING' WHERE id=?", (b["id"],))

    def test_cancel_is_idempotent(self):
        j = self.engine.create(self.data)
        self.assertEqual(self.engine.stop(j["id"])["status"], "CANCELLED")
        self.assertEqual(self.engine.stop(j["id"])["status"], "CANCELLED")

    def test_router_exists_without_mutating_legacy_sessions(self):
        paths = {r.path for r in router.routes}
        self.assertIn("/engine/health", paths)
        self.assertIn("/engine/jobs/{job_id}/start", paths)
        self.assertNotIn("/api/sessions/{sid}/start", paths)


if __name__ == "__main__":
    unittest.main()

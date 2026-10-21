"""Kokoro TTS worker isolation, real heartbeat freshness and no-job-loss fences."""
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from production import voice_kokoro_worker_recovery as repair
from production import workers, runtime


class KokoroHeartbeatRecoveryTests(unittest.TestCase):
    def test_future_voice_web_restarts_do_not_stop_kokoro_or_edge(self):
        for key, part, entry in workers.SPECS["voice-studio"]:
            unit = workers.unit("voice-studio", "a" * 40, key, part, entry)
            expected = ("Wants=" if key in ("edge-worker", "kokoro-worker")
                        else "Requires=") + repair.WEB
            self.assertIn(expected, unit)
            if key in ("edge-worker", "kokoro-worker"):
                self.assertNotIn("Requires=" + repair.WEB, unit)

    def test_legacy_kokoro_unit_repair_idempotent(self):
        modern = workers.unit("voice-studio", "a" * 40,
                              "kokoro-worker", "part02", "kokoro_worker.py")
        old = modern.replace(repair.NEW_DEP, repair.OLD_DEP)
        self.assertEqual(repair.dependency_rewrite(old), modern)
        self.assertEqual(repair.dependency_rewrite(modern), modern)

    def test_unowned_or_ambiguous_unit_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "ownership mismatch"):
            repair.dependency_rewrite("[Service]\nExecStart=/bin/false\n")
        valid = workers.unit("voice-studio", "a" * 40, "kokoro-worker",
                             "part02", "kokoro_worker.py")
        for invalid in (valid.replace(repair.NEW_DEP, ""),
                        valid.replace(repair.NEW_DEP, repair.NEW_DEP + "\n" + repair.OLD_DEP)):
            with self.assertRaisesRegex(RuntimeError, "dependencies"):
                repair.dependency_rewrite(invalid)

    def test_inflight_synthesis_and_drive_refuse_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "jobs.sqlite3"
            with sqlite3.connect(db) as c:
                c.execute("create table jobs(status text,delivery_status text)")
                c.execute("insert into jobs values('processing','pending')")
            with patch.object(repair, "DB", db):
                with self.assertRaisesRegex(RuntimeError, "active"):
                    repair.lock_idle_queue()
                with sqlite3.connect(db) as c:
                    c.execute("update jobs set status='completed',delivery_status='transferring'")
                with self.assertRaisesRegex(RuntimeError, "active"):
                    repair.lock_idle_queue()

    def test_completed_and_queued_rows_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "jobs.sqlite3"
            with sqlite3.connect(db) as c:
                c.execute("create table jobs(status text,delivery_status text)")
                c.executemany("insert into jobs values(?,?)", [
                    ("queued", "waiting_generation"), ("completed", "retry_pending")])
            with patch.object(repair, "DB", db):
                transaction = repair.lock_idle_queue()
                transaction.rollback()
                transaction.close()
            with sqlite3.connect(db) as c:
                self.assertEqual(c.execute("select * from jobs").fetchall(), [
                    ("queued", "waiting_generation"), ("completed", "retry_pending")])

    def test_heartbeat_rejects_old_timestamp_and_old_worker_epoch(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "voice-studio/kokoro/data/heartbeat.json"
            path.parent.mkdir(parents=True)
            with patch.object(repair, "DATA", Path(folder)):
                path.write_text(json.dumps({"ready": True, "epoch": time.time() - 80}))
                self.assertFalse(repair.heartbeat_fresh())
                path.write_text(json.dumps({"ready": True, "epoch": time.time()}))
                self.assertTrue(repair.heartbeat_fresh())
                self.assertFalse(repair.heartbeat_fresh(since=time.time() + 40))
                self.assertTrue(repair.heartbeat_fresh(since=time.time() - 1))

    def test_ready_key_false_does_not_pass(self):
        with patch.object(repair, "active", return_value=True), \
             patch.object(repair, "worker_status",
                          return_value={"ready": False, "reason": "worker_heartbeat_stale",
                                        "mode": "kokoro-onnx-cpu"}), \
             patch.object(repair, "heartbeat_fresh", return_value=True), \
             patch.object(repair.time, "monotonic", side_effect=[0, 0, 101]), \
             patch.object(repair.time, "sleep", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "worker_heartbeat_stale"):
                repair.await_ready(seconds=100)

    def test_future_dispatch_is_kokoro_only(self):
        import inspect
        code = inspect.getsource(runtime.install)
        self.assertIn("[vps1-voice-kokoro-recovery-only]", code)
        self.assertIn("repair_kokoro_worker(revision)", code)
        self.assertIn("repair_edge_worker(revision)", code)


if __name__ == "__main__":
    unittest.main()

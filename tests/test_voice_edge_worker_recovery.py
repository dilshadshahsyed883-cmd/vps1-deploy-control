"""Edge TTS heartbeat repair: isolate unit and require genuine readiness."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from production import voice_edge_worker_recovery as repair
from production import workers


class EdgeHeartbeatRecoveryTests(unittest.TestCase):
    def test_future_install_keeps_edge_running_during_voice_web_restart(self):
        unit = workers.unit("voice-studio", "a" * 40, "edge-worker",
                            "part03", "edge_worker.py")
        self.assertIn("Wants=vps1-production-voice-studio.service", unit)
        self.assertNotIn("Requires=vps1-production-voice-studio.service", unit)
        for key, part, entry in workers.SPECS["voice-studio"]:
            if key != "edge-worker":
                self.assertIn("Requires=vps1-production-voice-studio.service",
                              workers.unit("voice-studio", "a" * 40, key, part, entry))

    def test_live_legacy_unit_can_be_repaired_idempotently(self):
        old = workers.unit("voice-studio", "a" * 40, "edge-worker",
                           "part03", "edge_worker.py").replace(
                               "Wants=vps1-production-voice-studio.service",
                               "Requires=vps1-production-voice-studio.service")
        changed = repair.dependency_rewrite(old)
        self.assertEqual(changed, repair.dependency_rewrite(changed))
        self.assertEqual(changed.count(repair.NEW_DEP), 1)
        self.assertNotIn(repair.OLD_DEP, changed)

    def test_wrong_unit_is_rejected_without_mutation(self):
        with self.assertRaisesRegex(RuntimeError, "ownership mismatch"):
            repair.dependency_rewrite("[Service]\nExecStart=/bin/true\n")
        with self.assertRaisesRegex(RuntimeError, "unknown worker dependencies"):
            repair.dependency_rewrite(workers.unit("voice-studio", "a"*40,
                "edge-worker", "part03", "edge_worker.py").replace(
                    "Wants=vps1-production-voice-studio.service", ""))

    def test_busy_job_never_restarts_even_with_stale_heartbeat(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "jobs.sqlite3"
            import sqlite3
            with sqlite3.connect(db) as conn:
                conn.execute("create table jobs(status text,delivery_status text)")
                conn.execute("insert into jobs values('processing','pending')")
            with patch.object(repair, "DB", db):
                with self.assertRaisesRegex(RuntimeError, "active"):
                    repair.lock_idle_queue()
            with sqlite3.connect(db) as conn:
                conn.execute("update jobs set status='completed'")
                conn.execute("update jobs set delivery_status='transferring'")
            with patch.object(repair, "DB", db):
                with self.assertRaisesRegex(RuntimeError, "active"):
                    repair.lock_idle_queue()

    def test_idle_database_lock_is_released_without_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "jobs.sqlite3"
            import sqlite3
            with sqlite3.connect(db) as conn:
                conn.execute("create table jobs(status text,delivery_status text)")
                conn.execute("insert into jobs values('completed','cleaned')")
            with patch.object(repair, "DB", db):
                conn = repair.lock_idle_queue()
                conn.rollback()
                conn.close()
            with sqlite3.connect(db) as conn:
                self.assertEqual(conn.execute("select status from jobs").fetchone()[0],
                                 "completed")

    def test_queue_release_rejects_false_ready(self):
        from production import voice_queue_release as api
        # Prior code checked only membership: "ready" in health.
        with patch.object(api, "local_endpoint", return_value=(200, {
            "ready": False, "reason": "worker_heartbeat_stale"})):
            with self.assertRaisesRegex(RuntimeError, "worker not ready"):
                api.verify_queues()


if __name__ == "__main__":
    unittest.main()

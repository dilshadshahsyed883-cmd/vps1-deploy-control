"""Cross-mode admission and concurrency regression tests (no uploads)."""
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1] / "apps" / "local-upload-manager" / "part01"
sys.path.insert(0, str(HERE))
from lum_engine import mode_coordinator as c


class ModeCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "coordinator.sqlite3"
        self.patch_env = patch.dict(os.environ, {
            "LUM_MODE_COORDINATOR_DB": str(self.path),
            "LUM_MULTI_MODE_CLAIMS_ARMED": "1",
        })
        self.patch_env.start()
        self.addCleanup(self.patch_env.stop)

    def seal(self, receipts=None):
        return c.reconcile_receipts(
            receipts or [], verified_modes={"engine1", "engine2", "shorts"}
        )

    def test_read_only_status_does_not_create_ledger(self):
        self.assertFalse(c.status()["ready"])
        self.assertFalse(self.path.exists())

    def test_default_disabled_never_creates_db(self):
        with patch.dict(os.environ, {"LUM_MULTI_MODE_CLAIMS_ARMED": "0"}):
            with self.assertRaises(c.AdmissionBlocked):
                c.reserve("engine1", "UC01", "one.mp4")
        self.assertFalse(self.path.exists())

    def test_inventory_all_modes_required(self):
        with self.assertRaises(c.AdmissionBlocked):
            c.reconcile_receipts([], verified_modes={"engine1", "engine2"})
        self.assertFalse(self.path.exists())
        self.seal()
        self.assertTrue(c.status()["ready"])
        self.assertTrue(c.status()["history_reconciled"])

    def test_preserve_historical_receipts_and_conflicts(self):
        self.seal([("engine1", "UC01", "One.MP4"),
                   ("engine2", "UC01", "/videos/one.mp4")])
        self.assertEqual(c.status()["claim_count"], 1)
        with self.assertRaises(c.AdmissionBlocked):
            c.reserve("shorts", "UC01", "ONE.mp4")
        with self.assertRaises(c.AdmissionBlocked):
            self.seal()

    def test_cross_mode_duplicate_claim_blocked(self):
        self.seal()
        token = c.reserve("engine1", "UC01", "movie.mp4")
        self.assertEqual(c.status()["blocking_count"], 1)
        with self.assertRaises(c.AdmissionBlocked):
            c.reserve("shorts", "UC01", "movie.MP4")
        with self.assertRaises(c.AdmissionBlocked):
            c.settle(token, mode="engine2", outcome="FAILED_SAFE")
        c.settle(token, mode="engine1", outcome="COMPLETED", youtube_video_id="YT_OK")
        with self.assertRaises(c.AdmissionBlocked):
            c.reserve("engine2", "UC01", "movie.mp4")
        self.assertEqual(c.status()["claim_count"], 1)

    def test_safe_retry_but_ambiguous_blocks(self):
        self.seal()
        one = c.reserve("engine1", "UC01", "a.mp4")
        with self.assertRaises(c.AdmissionBlocked):
            c.settle(one, mode="engine1", outcome="FAILED_SAFE", remote_started=True)
        c.settle(one, mode="engine1", outcome="FAILED_SAFE", remote_started=False)
        two = c.reserve("shorts", "UC01", "a.mp4")
        self.assertNotEqual(one, two)
        c.settle(two, mode="shorts", outcome="AMBIGUOUS", remote_started=True)
        with self.assertRaises(c.AdmissionBlocked):
            c.reserve("engine1", "UC01", "a.mp4")

    def test_short_stream_not_a_standard_upload_mode(self):
        self.seal()
        with self.assertRaises(ValueError):
            c.reserve("short_stream", "UC01", "movie.mp4")

    def test_unreconciled_claim_denied(self):
        with self.assertRaises(c.AdmissionBlocked):
            c.reserve("engine2", "UC01", "movie.mp4")
        self.assertFalse(c.status()["ready"])

    def test_two_workers_racing_only_one_claims(self):
        self.seal()
        successes, failures = [], []
        barrier = threading.Barrier(2)
        def attempt(mode):
            barrier.wait()
            try:
                successes.append(c.reserve(mode, "UC01", "same.mp4"))
            except c.AdmissionBlocked:
                failures.append(mode)
        t1 = threading.Thread(target=attempt, args=("engine1",))
        t2 = threading.Thread(target=attempt, args=("shorts",))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)
        self.assertFalse(t1.is_alive() or t2.is_alive())
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual(c.status()["claim_count"], 1)


if __name__ == "__main__":
    unittest.main()

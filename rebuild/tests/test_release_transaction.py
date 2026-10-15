"""Guarded synthetic activation and automatic rollback tests."""
import json
from pathlib import Path
import tempfile
import unittest

from rebuild import fresh_vps, release_transaction

OLD = "a" * 40
NEW = "b" * 40


class PromotionSafety(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(work.cleanup)
        self.root = Path(work.name)
        self.app = "short-maker"
        fresh_vps.deploy_app(self.root, self.app, OLD)
        self.calls = []
        self.data = self.root / "var/lib/fresh-website" / self.app / "uploads" / "user-file.txt"
        self.data.write_text("preserve-user-data")
        self.queue = self.root / "var/lib/fresh-website" / self.app / "queue" / "active-job.txt"
        self.queue.write_text("queue-state")
        # Install another app to prove that release transaction is isolated.
        fresh_vps.deploy_app(self.root, "short-stream", OLD)
        self.other = (self.root / "srv/fresh-website/current/short-stream").resolve()

    def restart(self, app):
        self.calls.append(app)

    def health(self, app, sha):
        return True

    def current(self):
        return (self.root / "srv/fresh-website/current" / self.app).resolve().name

    def test_healthy_candidate_promoted_without_touching_data(self):
        result = release_transaction.transact(self.root, self.app, NEW,
            self.restart, self.health, snapshot_confirmed=True)
        self.assertEqual(result["status"], "CANDIDATE_HEALTHY")
        self.assertEqual(self.current(), NEW)
        self.assertEqual(self.calls, [self.app])
        self.assertEqual(self.data.read_text(), "preserve-user-data")
        self.assertEqual(self.queue.read_text(), "queue-state")
        self.assertEqual((self.root/"srv/fresh-website/current/short-stream").resolve(), self.other)

    def test_failed_health_reverts_to_old_release(self):
        def reject_new(app, sha):
            return sha == OLD
        with self.assertRaisesRegex(release_transaction.PromotionFailed, "verified restored"):
            release_transaction.transact(self.root, self.app, NEW,
                self.restart, reject_new, snapshot_confirmed=True)
        self.assertEqual(self.current(), OLD)
        self.assertEqual(self.calls, [self.app, self.app])
        self.assertEqual(self.data.read_text(), "preserve-user-data")

    def test_failed_service_restart_rolls_back(self):
        def restart_once(app):
            self.calls.append(app)
            if len(self.calls) == 1:
                raise RuntimeError("synthetic systemd failure")
        with self.assertRaises(release_transaction.PromotionFailed):
            release_transaction.transact(self.root, self.app, NEW,
                restart_once, self.health, snapshot_confirmed=True)
        self.assertEqual(self.current(), OLD)
        self.assertEqual(len(self.calls), 2)

    def test_rollback_failure_never_claimed_success(self):
        def always_restart_fails(app):
            raise RuntimeError("service restart unavailable")
        with self.assertRaisesRegex(release_transaction.PromotionFailed, "CRITICAL"):
            release_transaction.transact(self.root, self.app, NEW,
                always_restart_fails, self.health, snapshot_confirmed=True)
        self.assertEqual(self.current(), OLD)

    def test_requires_snapshot_and_old_healthy(self):
        with self.assertRaisesRegex(ValueError, "snapshot"):
            release_transaction.transact(self.root, self.app, NEW,
                self.restart, self.health)
        def old_unhealthy(app, sha):
            return False
        with self.assertRaisesRegex(ValueError, "existing release health failed"):
            release_transaction.transact(self.root, self.app, NEW,
                self.restart, old_unhealthy, snapshot_confirmed=True)
        self.assertEqual(self.current(), OLD)

    def test_no_change_does_not_restart(self):
        result = release_transaction.transact(self.root, self.app, OLD,
            self.restart, self.health, snapshot_confirmed=True)
        self.assertEqual(result["status"], "NO_CHANGE_HEALTHY")
        self.assertEqual(self.calls, [])

    def test_offline_app_blocked(self):
        with self.assertRaises(ValueError):
            release_transaction.transact(self.root, "auto-script", NEW,
                self.restart, self.health, snapshot_confirmed=True)

    def test_fake_hash_and_outside_tmp_rejected(self):
        with self.assertRaises(ValueError):
            release_transaction.transact(self.root, self.app, "../bad",
                self.restart, self.health, snapshot_confirmed=True)
        with self.assertRaises(ValueError):
            release_transaction.transact("/srv/fresh-website", self.app, NEW,
                self.restart, self.health, snapshot_confirmed=True)

    def test_first_live_install_not_auto_promoted(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as new_root:
            with self.assertRaisesRegex(ValueError, "initial installation"):
                release_transaction.transact(new_root, self.app, NEW,
                    self.restart, self.health, snapshot_confirmed=True)

    def test_foreign_active_symlink_blocks(self):
        ptr = self.root / "srv/fresh-website/current" / self.app
        ptr.unlink()
        ptr.symlink_to(self.root)
        with self.assertRaises(ValueError):
            release_transaction.transact(self.root, self.app, NEW,
                self.restart, self.health, snapshot_confirmed=True)


if __name__ == "__main__":
    unittest.main()

"""One-host serialized queue, safety gates, and synthetic rollback integration."""
import concurrent.futures
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from rebuild import fresh_vps
from rebuild.deploy_control import IntentQueue
from rebuild.deploy_engine import (
    Engine, QueueStore, RetryablePreparationError, SimulatedBackend,
    deployment_contracts,
)
from rebuild.release_transaction import PromotionFailed

OLD = "a" * 40
NEW = "b" * 40
NEXT = "c" * 40


class Backend:
    def __init__(self):
        self.current = OLD
        self.calls = []
        self.delay_once = False
        self.rollback = False
        self.critical = False
        self.fail_preflight = False

    def preflight(self, app, sha):
        if self.fail_preflight:
            raise ValueError("unhealthy existing service")
        if self.delay_once:
            self.delay_once = False
            raise RetryablePreparationError("temporarily unavailable artifact")
        return self.current

    def activate(self, app, sha):
        self.calls.append((app, sha))
        if self.critical:
            raise PromotionFailed("CRITICAL: candidate failed and rollback did not verify")
        if self.rollback:
            raise PromotionFailed("candidate failed; prior healthy revision verified restored")
        self.current = sha
        return {"status": "CANDIDATE_HEALTHY"}

    def inspect(self, app):
        return self.current, True


class QueueEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="vps1-q-")
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "queue.sqlite3"
        self.queue = QueueStore(self.db, lease_seconds=60, max_attempts=3)
        self.backend = Backend()
        self.engine = Engine(self.queue, self.backend)

    def test_duplicate_cross_tab_submit_and_fifo(self):
        a = self.queue.enqueue("short-maker", NEW)
        self.assertEqual(a["id"], QueueStore(self.db).enqueue("short-maker", NEW)["id"])
        b = self.queue.enqueue("voice-studio", NEXT)
        self.assertLess(a["id"], b["id"])
        self.assertEqual(self.engine.once(now=100)["status"], "VERIFIED")
        self.assertEqual(self.engine.once(now=101)["status"], "VERIFIED")
        self.assertEqual([x["id"] for x in self.queue.list()], [a["id"], b["id"]])

    def test_invalid_commit_and_offline_app(self):
        for app in ("auto-script", "unknown"):
            with self.assertRaises(ValueError):
                self.queue.enqueue(app, NEW)
        with self.assertRaises(ValueError):
            self.queue.enqueue("short-maker", "master")

    def test_exclusive_claim_during_other_process(self):
        self.queue.enqueue("short-maker", NEW)
        self.queue.enqueue("video-merger", NEXT)
        claimed = self.queue.claim(now=100)
        self.assertIsNotNone(claimed)
        self.assertIsNone(QueueStore(self.db).claim(now=101))
        self.queue.previous(claimed["id"], claimed["token"], OLD)
        self.queue.finish(claimed["id"], claimed["token"], "VERIFIED", now=102)
        self.assertEqual(self.queue.claim(now=103)["app"], "video-merger")

    def test_preparation_retry_backoff_and_no_overtake(self):
        first = self.queue.enqueue("short-maker", NEW)
        self.queue.enqueue("video-merger", NEXT)
        self.backend.delay_once = True
        state = self.engine.once(now=100)
        self.assertEqual(state["status"], "RETRY_WAIT")
        self.assertEqual(state["attempts"], 1)
        self.assertIsNone(self.engine.once(now=129))
        self.assertEqual(self.engine.once(now=130)["id"], first["id"])
        self.assertEqual(self.queue.list()[0]["status"], "VERIFIED")
        self.assertEqual(self.engine.once(now=131)["status"], "VERIFIED")

    def test_bounded_retry_limit(self):
        self.queue.enqueue("short-maker", NEW)
        for now in (100, 130, 190):
            self.backend.delay_once = True
            self.engine.once(now=now)
        self.assertEqual(self.queue.list()[0]["status"], "FAILED")
        self.assertEqual(self.queue.list()[0]["attempts"], 3)

    def test_rollback_terminal_keeps_queue_progressing(self):
        self.queue.enqueue("short-maker", NEW)
        self.queue.enqueue("voice-studio", NEXT)
        self.backend.rollback = True
        self.assertEqual(self.engine.once(now=100)["status"], "ROLLED_BACK")
        self.backend.rollback = False
        self.assertEqual(self.engine.once(now=101)["status"], "VERIFIED")

    def test_unknown_activation_result_freezes_queue(self):
        first = self.queue.enqueue("short-maker", NEW)
        self.queue.enqueue("voice-studio", NEXT)
        self.backend.critical = True
        self.assertEqual(self.engine.once(now=100)["status"], "NEEDS_ATTENTION")
        self.assertIsNone(self.engine.once(now=101))
        self.backend.critical = False
        self.assertEqual(self.engine.reconcile(first["id"])["status"], "ROLLED_BACK")
        self.assertEqual(self.engine.once(now=102)["status"], "VERIFIED")

    def test_crash_lease_expiry_fails_closed_and_reconcile_verified(self):
        job = self.queue.enqueue("short-maker", NEW)
        self.queue.enqueue("voice-studio", NEXT)
        locked = self.queue.claim(now=100)
        self.queue.previous(job["id"], locked["token"], OLD)
        # Simulated crash after pointer flip; no finish() was committed.
        self.backend.current = NEW
        self.assertIsNone(self.queue.claim(now=161))
        self.assertEqual(self.queue.list()[0]["status"], "NEEDS_ATTENTION")
        with self.assertRaises(RuntimeError):
            self.queue.finish(job["id"], locked["token"], "VERIFIED", now=162)
        self.assertEqual(self.engine.reconcile(job["id"])["status"], "VERIFIED")
        self.assertEqual(self.queue.claim(now=163)["app"], "voice-studio")

    def test_stale_lease_cannot_lie_about_success(self):
        job = self.queue.enqueue("short-maker", NEW)
        locked = self.queue.claim(now=100)
        ended = self.queue.finish(job["id"], locked["token"], "VERIFIED", now=161)
        self.assertEqual(ended["status"], "NEEDS_ATTENTION")

    def test_health_required_for_reconciliation(self):
        job = self.queue.enqueue("short-maker", NEW)
        token = self.queue.claim(now=100)["token"]
        self.queue.previous(job["id"], token, OLD)
        self.queue.finish(job["id"], token, "NEEDS_ATTENTION", now=101)
        with self.assertRaises(ValueError):
            self.queue.reconcile(job["id"], OLD, False, evidence="unhealthy")
        with self.assertRaises(ValueError):
            self.queue.reconcile(job["id"], NEXT, True, evidence="mismatch")

    def test_sqlite_survives_reinstantiation(self):
        self.queue.enqueue("short-maker", NEW)
        self.assertEqual(QueueStore(self.db).list()[0]["revision"], NEW)
        self.assertEqual(QueueStore(self.db).claim(now=100)["app"], "short-maker")

    def test_transient_error_after_preflight_requires_manual_reconcile(self):
        self.queue.enqueue("short-maker", NEW)
        def fail_during_activation(app, sha):
            raise RetryablePreparationError("possibly activated before I/O failure")
        self.backend.activate = fail_during_activation
        row = self.engine.once(now=100)
        self.assertEqual(row["status"], "NEEDS_ATTENTION")
        self.assertIsNone(self.engine.once(now=200))

    def test_persistent_worker_loop_finishes_multiple_jobs(self):
        self.queue.enqueue("short-maker", NEW)
        self.queue.enqueue("voice-studio", NEXT)
        self.assertEqual(self.engine.run_forever(max_cycles=3, poll_seconds=0.001), 3)
        self.assertEqual([j["status"] for j in self.queue.list()],
                         ["VERIFIED", "VERIFIED"])

    def test_symlink_database_path_rejected(self):
        target = Path(self.tmp.name) / "real.sqlite3"
        link = Path(self.tmp.name) / "linked.sqlite3"
        link.symlink_to(target)
        with self.assertRaises(ValueError):
            QueueStore(link)

    def test_prototype_queue_migration_blocks_orphaned_claim(self):
        legacy_db = Path(self.tmp.name) / "legacy.sqlite3"
        legacy = IntentQueue(legacy_db)
        legacy.enqueue("short-maker", NEW)
        legacy.claim()
        upgraded = QueueStore(legacy_db)
        self.assertEqual(upgraded.list()[0]["status"], "NEEDS_ATTENTION")
        self.assertIsNone(upgraded.claim(now=100))

    def test_multiple_independent_producers_keep_one_fifo(self):
        apps = ["short-maker", "voice-studio", "video-merger", "short-stream"]
        barrier = threading.Barrier(len(apps))
        def submit(i):
            barrier.wait()
            return QueueStore(self.db).enqueue(apps[i], chr(97+i)*40)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            jobs = list(pool.map(submit, range(len(apps))))
        self.assertEqual(len({j["id"] for j in jobs}), 4)
        self.assertEqual(len(self.queue.list()), 4)


class SyntheticReleaseIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="vps1-release-", dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.queue = QueueStore(self.root/"var/queue.sqlite3")
        self.app = "short-maker"
        fresh_vps.deploy_app(self.root, self.app, OLD)
        self.data = self.root/"var/lib/fresh-website"/self.app/"uploads"/"sensitive.txt"
        self.data.write_text("preserve")
        self.services = []
        self.health = lambda app, sha: True

    def make_engine(self):
        backend = SimulatedBackend(self.root, restart=self.services.append, healthy=self.health)
        return Engine(self.queue, backend)

    def current(self):
        return (self.root/"srv/fresh-website/current"/self.app).resolve().name

    def test_atomic_promotion_and_preserved_mutable_state(self):
        self.queue.enqueue(self.app, NEW)
        self.assertEqual(self.make_engine().once()["status"], "VERIFIED")
        self.assertEqual(self.current(), NEW)
        self.assertEqual(self.data.read_text(), "preserve")
        self.assertEqual(self.services, [self.app])

    def test_real_transaction_failing_health_restores_old_pointer(self):
        self.queue.enqueue(self.app, NEW)
        self.health = lambda app, sha: sha == OLD
        outcome = self.make_engine().once()
        self.assertEqual(outcome["status"], "ROLLED_BACK")
        self.assertEqual(self.current(), OLD)
        self.assertEqual(self.services, [self.app, self.app])
        self.assertEqual(self.data.read_text(), "preserve")


class ContractTests(unittest.TestCase):
    def test_all_active_apps_covered_and_auto_script_excluded(self):
        c = deployment_contracts()
        self.assertEqual(len(c), 10)
        self.assertNotIn("auto-script", c)
        for value in c.values():
            self.assertFalse(value["runtime_verified"])
            self.assertFalse(value["migrations"]["automatic"])
            self.assertIn("services", value)
            self.assertIn("workers", value)
            self.assertIn("dependencies", value)


if __name__ == "__main__":
    unittest.main()

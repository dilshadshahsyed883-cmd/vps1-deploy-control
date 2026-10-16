"""Tab 3: independent app CI and fail-closed deployment compatibility guards.

All tests are source-only/synthetic: never connect to VPS or enable services.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rebuild import builder, ci_release, deploy_control, website_shell
from rebuild.deploy_engine import Engine, QueueStore, deployment_contracts

ROOT = Path(__file__).resolve().parents[2]
OLD = "a" * 40
NEW = "b" * 40


class AppCompatibilityTests(unittest.TestCase):
    def test_each_of_ten_apps_has_independent_source_candidate(self):
        groups, profiles, active, offline = deploy_control.registry()
        self.assertEqual(len(active), 10)
        self.assertEqual(offline, {"auto-script"})
        registry = json.loads((ROOT / "rebuild/groups.json").read_text())
        for app in sorted(active):
            with self.subTest(app=app):
                path = f"apps/{app}/part01/source.py"
                ci = ci_release.classify([path], registry)
                planner = deploy_control.plan([path])
                self.assertFalse(ci["manual_review"])
                self.assertEqual(ci["apps"], [app])
                self.assertEqual(planner["source_release_candidates"], [app])
                self.assertFalse(planner["production_deployment_authorized"])
                self.assertIn(app, profiles)

    def test_shared_control_changes_fail_closed_but_other_apps_unchanged(self):
        registry = json.loads((ROOT / "rebuild/groups.json").read_text())
        for path in ("rebuild/groups.json", "rebuild/deploy_engine.py",
                     "infrastructure/registry/apps.json", "rebuild/shell/index.html"):
            with self.subTest(path=path):
                result = ci_release.classify(
                    ["apps/short-maker/part01/app.py", path], registry)
                self.assertTrue(result["manual_review"])
                self.assertEqual(result["apps"], [])

    def test_shell_has_no_unverified_live_routes_and_offline_app_is_absent(self):
        cfg = json.loads((ROOT / "rebuild/groups.json").read_text())
        profiles = json.loads((ROOT / "rebuild/install-profiles.json").read_text())
        routing = json.loads((ROOT / "infrastructure/registry/apps.json").read_text())
        self.assertEqual(routing["apps"], [])
        nav = website_shell.catalog(cfg, profiles, routing)
        self.assertEqual(len(nav["apps"]), 10)
        self.assertNotIn("auto-script", [x["id"] for x in nav["apps"]])
        self.assertTrue(all(not x["route_configured"]
                            and not x["runtime_verified"] for x in nav["apps"]))

    def test_all_ten_require_real_worker_health_and_database_approval(self):
        contracts = deployment_contracts()
        self.assertEqual(len(contracts), 10)
        for app, c in contracts.items():
            with self.subTest(app=app):
                self.assertFalse(c["runtime_verified"])
                self.assertFalse(c["health"]["enabled"])
                self.assertFalse(c["migrations"]["automatic"])
                self.assertEqual(c["workers"], [])
                self.assertTrue(c["pending"])


class VerifiedQueueBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="tab3-queue-")
        self.addCleanup(self.tmp.cleanup)
        self.queue = QueueStore(Path(self.tmp.name) / "deploy.sqlite3",
                                lease_seconds=60)

    def test_verified_enqueue_is_not_split_into_unverified_enqueue(self):
        manifest = builder.artifact("short-maker", NEW,
                                    Path(self.tmp.name) / "artifacts")
        with patch.object(self.queue, "enqueue",
                          side_effect=AssertionError("unverified queue publication")):
            item = self.queue.enqueue_verified("short-maker", NEW, manifest)
        self.assertEqual(item["status"], "QUEUED")
        self.assertRegex(item["archive_sha256"], r"^[a-f0-9]{64}$")
        self.assertEqual(self.queue.enqueue_verified(
            "short-maker", NEW, manifest)["id"], item["id"])

    def test_processed_unverified_intent_cannot_be_retroactively_authenticated(self):
        manifest = builder.artifact("short-maker", NEW,
                                    Path(self.tmp.name) / "artifacts")
        self.queue.enqueue("short-maker", NEW)
        claimed = self.queue.claim(now=100)
        self.queue.finish(claimed["id"], claimed["token"], "FAILED", now=101)
        with self.assertRaises(ValueError):
            self.queue.enqueue_verified("short-maker", NEW, manifest)

    def test_successful_activate_without_matching_live_revision_freezes_fifo(self):
        class Backend:
            requires_verified_artifact = False  # synthetic backend only
            def preflight(self, app, revision):
                return OLD
            def activate(self, app, revision):
                return {"status": "success"}  # returns without actually updating
            def inspect(self, app):
                return OLD, True
        self.queue.enqueue("short-maker", NEW)
        self.queue.enqueue("voice-studio", "c" * 40)
        engine = Engine(self.queue, Backend())
        result = engine.once(now=100)
        self.assertEqual(result["status"], "NEEDS_ATTENTION")
        self.assertIn("post-activation", result["error"])
        self.assertIsNone(engine.once(now=101))

    def test_successful_activate_with_unhealthy_new_revision_is_not_verified(self):
        class Backend:
            requires_verified_artifact = False  # synthetic backend only
            def preflight(self, app, revision):
                return OLD
            def activate(self, app, revision):
                return {"status": "success"}
            def inspect(self, app):
                return NEW, False
        self.queue.enqueue("short-maker", NEW)
        result = Engine(self.queue, Backend()).once(now=100)
        self.assertEqual(result["status"], "NEEDS_ATTENTION")


if __name__ == "__main__":
    unittest.main()

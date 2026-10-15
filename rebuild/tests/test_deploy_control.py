"""CI-only app planner/queue tests; never contacts a production service."""
import tempfile
import unittest
from pathlib import Path

from rebuild.deploy_control import IntentQueue, plan

A = "a" * 40
B = "b" * 40


class PlannerTests(unittest.TestCase):
    def test_independent_edit(self):
        p = plan(["apps/short-maker/part01/app.py"])
        self.assertEqual(p["source_release_candidates"], ["short-maker"])
        self.assertEqual(p["validation_apps"], ["short-maker"])
        self.assertFalse(p["production_deployment_authorized"])

    def test_dependency_expands_tests_not_releases(self):
        p = plan(["apps/scene-builder/part01/app.py"])
        self.assertEqual(p["source_release_candidates"], ["scene-builder"])
        self.assertIn("voice-studio", p["validation_apps"])

    def test_alias_and_offline(self):
        p = plan(["apps/kokoro/kokoro_api.py", "apps/auto-script/auto_script.py"])
        self.assertEqual(p["source_release_candidates"], ["voice-studio"])
        self.assertEqual(p["offline_app_changes"], ["auto-script"])

    def test_shared(self):
        p = plan(["rebuild/dependency-budget.json", "rebuild/shell/assets/shell.css"])
        self.assertEqual(len(p["validation_apps"]), 10)
        self.assertEqual(p["source_release_candidates"], [])
        self.assertTrue(p["shell_build_required"])

    def test_unknown_app_fail_closed(self):
        for path in ("apps/new-app/main.py", "apps/../secrets.env", "/etc/passwd"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                plan([path])


class QueueTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="vps1-control-")
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "queue.sqlite3"
        self.q = IntentQueue(self.path)

    def test_fifo_dedup_and_serial(self):
        first = self.q.enqueue("short-maker", A)
        self.assertEqual(first["id"], self.q.enqueue("short-maker", A)["id"])
        second = self.q.enqueue("voice-studio", B)
        self.assertLess(first["id"], second["id"])
        self.assertEqual(self.q.claim()["id"], first["id"])
        self.assertIsNone(self.q.claim())
        self.q.finish(first["id"], "verified")
        self.assertEqual(self.q.claim()["id"], second["id"])
        self.q.finish(second["id"], "rolled_back")
        self.assertEqual([x["status"] for x in self.q.list()],
                         ["VERIFIED", "ROLLED_BACK"])

    def test_reopen_persists_queue(self):
        self.q.enqueue("short-maker", A)
        self.assertEqual(IntentQueue(self.path).claim()["app"], "short-maker")

    def test_invalid_and_unclaimed_fail(self):
        for name in ("auto-script", "missing"):
            with self.assertRaises(ValueError):
                self.q.enqueue(name, A)
        with self.assertRaises(ValueError):
            self.q.enqueue("short-maker", "not-a-sha")
        with self.assertRaises(ValueError):
            self.q.finish(999, "verified")
        with self.assertRaises(ValueError):
            self.q.finish(999, "success")


if __name__ == "__main__":
    unittest.main()

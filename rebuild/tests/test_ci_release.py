"""Fail-closed app-diff detection; shared files must not trigger rolling deploy."""
import unittest
from rebuild import ci_release

REGISTRY = {
    "groups": [
        {"id": "scene-builder", "deployment_mode": "active"},
        {"id": "voice-studio", "deployment_mode": "active"},
        {"id": "auto-script", "deployment_mode": "offline"},
    ]
}


class ClassificationTest(unittest.TestCase):
    def test_one_app_is_independent(self):
        r = ci_release.classify(["apps/voice-studio/part01/server.py"], REGISTRY)
        self.assertEqual(r["apps"], ["voice-studio"])
        self.assertFalse(r["manual_review"])
        self.assertEqual(r["matrix"], {"include": [{"app": "voice-studio"}]})

    def test_two_apps_have_deterministic_matrix(self):
        r = ci_release.classify([
            "apps/voice-studio/part01/a.py",
            "apps/scene-builder/part01/index.html",
            "apps/voice-studio/part01/a.py",
        ], REGISTRY)
        self.assertEqual(r["apps"], ["scene-builder", "voice-studio"])

    def test_offline_auto_script_is_never_released(self):
        r = ci_release.classify(["apps/auto-script/app.py"], REGISTRY)
        self.assertEqual(r["apps"], [])
        self.assertEqual(r["offline_ignored"], ["auto-script"])

    def test_kokoro_snapshot_needs_app_owned_sync(self):
        r = ci_release.classify(["apps/kokoro/kokoro_api.py",
                                 "apps/voice-studio/part01/kokoro_api.py"], REGISTRY)
        self.assertTrue(r["manual_review"])
        self.assertEqual(r["apps"], [])
        self.assertIn("snapshot requires owned app source sync", " ".join(r["reasons"]))

    def test_shared_shell_blocks_release_even_with_app_change(self):
        r = ci_release.classify([
            "apps/voice-studio/part01/a.py", "rebuild/shell/index.html"
        ], REGISTRY)
        self.assertTrue(r["manual_review"])
        self.assertEqual(r["apps"], [])

    def test_registry_and_deployer_changes_require_review(self):
        for path in ("rebuild/groups.json", "rebuild/install-profiles.json",
                     "scripts/stage_fresh_app.py", "rebuild/builder.py"):
            with self.subTest(path=path):
                r = ci_release.classify([path, "apps/scene-builder/a.py"], REGISTRY)
                self.assertTrue(r["manual_review"])
                self.assertEqual(r["count"], 0)

    def test_unknown_app_is_blocked(self):
        r = ci_release.classify(["apps/new-app/source.py"], REGISTRY)
        self.assertTrue(r["manual_review"])
        self.assertEqual(r["apps"], [])

    def test_unknown_workflow_or_script_blocks_app_rollout(self):
        for path in (".github/workflows/new-deploy.yml",
                     "rebuild/unknown_installer.py", "scripts/helper.sh",
                     ".gitignore"):
            with self.subTest(path=path):
                result = ci_release.classify(
                    ["apps/voice-studio/part01/a.py", path], REGISTRY)
                self.assertTrue(result["manual_review"])
                self.assertEqual(result["apps"], [])

    def test_docs_and_tests_never_roll_out_code(self):
        r = ci_release.classify([
            "docs/SOURCE_OF_TRUTH_CUTOVER.md", "rebuild/tests/test_ci_release.py"
        ], REGISTRY)
        self.assertEqual(r["count"], 0)
        self.assertFalse(r["manual_review"])

    def test_bad_paths_fail_closed(self):
        for path in ("../apps/voice-studio/a.py", "/apps/voice-studio/a.py"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                ci_release.classify([path], REGISTRY)

    def test_invalid_commit_refs_fail_closed(self):
        for ref in ("x", "0" * 40):
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                ci_release.changed_paths(ref, "a" * 40)


if __name__ == "__main__":
    unittest.main()

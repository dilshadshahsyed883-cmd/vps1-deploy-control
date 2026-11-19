"""Offline fail-closed tests for the Scene-only Concat SSO release lane."""
from __future__ import annotations

from pathlib import Path
import unittest

from production.scene_sso_only_release import (
    ALLOWED_CHANGES, changed_files, environment_presence,
    require_sso_only_diff,
)


class SceneSSOOnlyReleaseTests(unittest.TestCase):
    def setUp(self):
        self.old = {
            "part01/app.py": "before",
            "part01/static/scene.css": "unchanged",
            "part02/smart_scroll/lut_engine.py": "same-worker",
        }
        self.new = dict(self.old)
        self.new["part01/app.py"] = "after"
        self.new["part01/concat_sso_issuer.py"] = "new"

    def test_exact_two_file_diff_allowed(self):
        self.assertEqual(changed_files(self.old, self.new), ALLOWED_CHANGES)
        require_sso_only_diff(self.old, self.new)

    def test_reject_unrelated_lut_or_worker_change(self):
        for path in ("part02/smart_scroll/lut_engine.py",
                     "part01/static/scene.css",
                     "part02/api_server.py"):
            with self.subTest(path=path):
                bad = dict(self.new)
                bad[path] = "changed"
                with self.assertRaises(RuntimeError):
                    require_sso_only_diff(self.old, bad)

    def test_reject_missing_issuer_or_unchanged_scene_wiring(self):
        no_issuer = dict(self.new)
        del no_issuer["part01/concat_sso_issuer.py"]
        with self.assertRaises(RuntimeError):
            require_sso_only_diff(self.old, no_issuer)
        no_wiring = dict(self.new)
        no_wiring["part01/app.py"] = self.old["part01/app.py"]
        with self.assertRaises(RuntimeError):
            require_sso_only_diff(self.old, no_wiring)
        already_present = dict(self.old)
        already_present["part01/concat_sso_issuer.py"] = "old-issuer"
        with self.assertRaises(RuntimeError):
            require_sso_only_diff(already_present, self.new)

    def test_scene_environment_enforces_isolated_signing_key(self):
        good = (
            "WEB_DATA_DIR=/var/lib/fresh-website/scene-builder/web\n"
            "SESSION_SECRET=other-session\n"
            "WEB_ACCESS_KEY=other-access\n"
            "CONCAT_SCENE_SSO_SECRET=" + "k" * 48 + "\n"
        )
        self.assertIn("CONCAT_SCENE_SSO_SECRET", environment_presence(good))
        for bad in (
            good.replace("k" * 48, "short"),
            good.replace("k" * 48, "other-session"),
            good.replace("k" * 48, "other-access"),
            good.replace("web\n", "unexpected\n"),
            good + "CONCAT_SCENE_SSO_SECRET=duplicate\n",
        ):
            with self.subTest(bad=bad[:35]):
                with self.assertRaises(RuntimeError):
                    environment_presence(bad)

    def test_no_unrelated_release_paths_or_auth_bypass(self):
        source = (Path(__file__).resolve().parents[1] /
                  "production/scene_sso_only_release.py").read_text()
        self.assertNotIn("provision_builtins(", source)
        self.assertNotIn("worker_source_refresh.refresh(", source)
        self.assertNotIn("install_browser(", source)
        self.assertNotIn("systemctl\", \"restart\", \"vps1-scene-production-worker", source)
        self.assertIn('fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)', source)
        self.assertIn("source.backup(destination)", source)
        self.assertIn('atomic_link(CURRENT, previous)', source)


if __name__ == "__main__":
    unittest.main()

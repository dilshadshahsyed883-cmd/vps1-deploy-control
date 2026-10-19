"""Source-level regression tests for the existing Scene candidate adapter."""
from pathlib import Path
import ast
import unittest


ROOT = Path(__file__).resolve().parents[1]


class SceneCandidateReleaseContract(unittest.TestCase):
    def test_candidate_update_is_scoped_and_reversible(self):
        source = (ROOT / "production/scene_candidate_release.py").read_text()
        ast.parse(source)
        self.assertIn('UNIT = "vps1-scene-candidate.service"', source)
        self.assertIn('CURRENT = BASE / "current/scene-builder"', source)
        self.assertIn('STATE = Path("/var/lib/fresh-website/scene-builder")', source)
        self.assertIn('atomic_link(CURRENT, previous)', source)
        self.assertIn('backup_databases(STATE', source)
        self.assertIn('/internal/workspace-auth?app=short-maker', source)
        self.assertIn('(\"script_work\", \"/script-work\")', source)
        self.assertIn('(\"storage_manager\", \"/admin/storage\")', source)
        self.assertNotIn('auto-script', source.lower())
        self.assertIn('dashboard_no_algrow', source)
        self.assertIn('("user_management", "/user-management")', source)
        self.assertIn('"Invalid user-management action token"', source)
        self.assertIn('user_management_post_csrf', source)
        self.assertIn('"/api/session/identity"', source)
        self.assertIn('Scene administrator session identity is unavailable', source)
        self.assertIn('native_sidebar_role', source)
        self.assertIn('obsolete Algrow Voice dashboard card remains in the API', source)
        self.assertIn('"/api/dashboard/v1"', source)

    def test_anonymous_identity_redirect_cannot_be_followed_to_login(self):
        from urllib.request import Request
        from production.scene_candidate_release import _NoRedirect
        no_redirect = _NoRedirect()
        req = Request("http://127.0.0.1:18080/api/session/identity")
        self.assertIsNone(no_redirect.redirect_request(
            req, None, 303, "See Other", {"Location": "/login"},
            "http://127.0.0.1:18080/login"))
        source = (ROOT / "production/scene_candidate_release.py").read_text()
        self.assertIn('_request("/api/session/identity", follow_redirects=False)', source)

    def test_runtime_routes_existing_scene_candidate_to_adapter(self):
        source = (ROOT / "production/runtime.py").read_text()
        tree = ast.parse(source)
        self.assertTrue(tree.body)
        self.assertIn("production.scene_candidate_release", source)
        self.assertIn("install_scene_candidate(revision)", source)


    def test_storage_bridge_adds_two_keys_and_is_idempotent(self):
        from production.scene_candidate_release import _storage_candidate_env_patch
        nl = chr(10)
        token = "X" * 40
        original = "UNCHANGED=preserve" + nl + "SIBLING=keep" + nl
        expected = original + "STORAGE_MANAGER_URL=http://127.0.0.1:8149" + nl + "STORAGE_MANAGER_TOKEN=" + token + nl
        self.assertEqual(_storage_candidate_env_patch(original, token), expected)
        self.assertEqual(_storage_candidate_env_patch(expected, token), expected)

    def test_storage_bridge_replaces_only_stale_candidate_token(self):
        from production.scene_candidate_release import _storage_candidate_env_patch
        nl = chr(10)
        token = "X" * 40
        original = nl.join(["STORAGE_MANAGER_URL=http://127.0.0.1:8149",
                            "STORAGE_MANAGER_TOKEN=stale", "UNCHANGED=preserved", ""])
        updated = _storage_candidate_env_patch(original, token)
        self.assertIn("STORAGE_MANAGER_TOKEN=" + token + nl, updated)
        self.assertIn("UNCHANGED=preserved" + nl, updated)
        self.assertNotIn("TOKEN=stale", updated)

    def test_storage_bridge_preserves_alternate_and_rejects_duplicates(self):
        from production.scene_candidate_release import _storage_candidate_env_patch
        nl = chr(10)
        token = "X" * 40
        alternate = nl.join(["STORAGE_MANAGER_URL=http://127.0.0.1:8799",
                             "STORAGE_MANAGER_TOKEN=custom", ""])
        self.assertEqual(_storage_candidate_env_patch(alternate, token), alternate)
        duplicate = nl.join(["STORAGE_MANAGER_TOKEN=one", "STORAGE_MANAGER_TOKEN=two", ""])
        with self.assertRaises(RuntimeError):
            _storage_candidate_env_patch(duplicate, token)
        with self.assertRaises(ValueError):
            _storage_candidate_env_patch("", "short")

    def test_candidate_checks_authenticated_read_only_backend_and_env_restore(self):
        source = (ROOT / "production/scene_candidate_release.py").read_text()
        for marker in ('"/api/admin/storage/capabilities"', '"/api/admin/storage/metrics"',
                       'capabilities.get("can_modify") is not False', '_atomic_scene_env(original_env)'):
            self.assertIn(marker, source)
        self.assertNotIn("print(token)", source)



if __name__ == "__main__":
    unittest.main()

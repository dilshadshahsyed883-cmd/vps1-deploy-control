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
        self.assertIn('obsolete Algrow Voice dashboard card remains in the API', source)
        self.assertIn('"/api/dashboard/v1"', source)

    def test_runtime_routes_existing_scene_candidate_to_adapter(self):
        source = (ROOT / "production/runtime.py").read_text()
        tree = ast.parse(source)
        self.assertTrue(tree.body)
        self.assertIn("production.scene_candidate_release", source)
        self.assertIn("install_scene_candidate(revision)", source)


if __name__ == "__main__":
    unittest.main()

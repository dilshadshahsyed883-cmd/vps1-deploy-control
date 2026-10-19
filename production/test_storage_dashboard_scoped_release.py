"""Scope checks for the protected-main Scene dashboard release."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]

class StorageDashboardRelease(unittest.TestCase):
    def test_only_scene_builder_is_deployed(self):
        workflow = (ROOT / ".github/workflows/vps1-storage-dashboard-release.yml").read_text()
        self.assertIn("[vps1-storage-dashboard-release]", workflow)
        self.assertIn("install $SOURCE_SHA scene-builder", workflow)
        self.assertNotIn("install $SOURCE_SHA local-upload-manager", workflow)
        self.assertIn("STORAGE_BACKEND_PROVISIONING=SEPARATE", workflow)

    def test_asset_cache_busted_and_storage_fails_closed(self):
        html = (ROOT / "apps/scene-builder/part01/templates/dashboard.html").read_text()
        scene = (ROOT / "production/scene_candidate_release.py").read_text()
        js = (ROOT / "apps/scene-builder/part01/static/storage_manager.js").read_text()
        self.assertIn("dashboard_control_center.js?v=20261019-hide-algrow-v3", html)
        self.assertIn('dashboard_no_algrow', scene)
        self.assertIn("let managerFallback = true", js)
        self.assertIn("let managerCanModify = false", js)
        self.assertIn("scanUnavailableMessage", js)
        self.assertIn("service_details", js)

    def test_storage_manager_reports_absent_units_without_enabling_actions(self):
        backend = (ROOT / "apps/vps-storage-manager/part01/storage_manager.py").read_text()
        self.assertIn('"service_details": {name: service_detail(name) for name in MONITORED_SERVICES}', backend)
        self.assertIn('"present": present', backend)
        self.assertIn('managerCanModify && present && startable.has(key)', js)

if __name__ == "__main__":
    unittest.main()

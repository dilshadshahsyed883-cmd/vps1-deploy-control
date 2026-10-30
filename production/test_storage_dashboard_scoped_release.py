"""Scoped Storage Manager dashboard release regression checks."""
from pathlib import Path
import importlib.util
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCENE = ROOT / "apps/scene-builder/part01"
MANAGER = ROOT / "apps/vps-storage-manager/part01/storage_manager.py"


class StorageDashboardRelease(unittest.TestCase):
    def test_only_scene_builder_is_deployed(self):
        workflow = (ROOT / ".github/workflows/vps1-storage-dashboard-release.yml").read_text()
        self.assertIn("[vps1-storage-dashboard-release]", workflow)
        self.assertIn("install $SOURCE_SHA scene-builder", workflow)
        self.assertNotIn("install $SOURCE_SHA local-upload-manager", workflow)
        self.assertIn("STORAGE_BACKEND_PROVISIONING=SEPARATE", workflow)

    def test_backend_retirement_has_an_isolated_guarded_release_lane(self):
        workflow = (ROOT / ".github/workflows/vps1-storage-backend-retirement-release.yml").read_text()
        self.assertIn("[vps1-storage-backend-retirement-release]", workflow)
        self.assertIn("install $SOURCE_SHA vps-storage-manager", workflow)
        self.assertIn("curl -fsS http://127.0.0.1:8149/health", workflow)
        self.assertIn("CLEANUP_NOT_EXECUTED=YES", workflow)

    def test_asset_cache_busted_and_storage_fails_closed(self):
        dashboard = (SCENE / "templates/dashboard.html").read_text()
        scene = (ROOT / "production/scene_candidate_release.py").read_text()
        template = (SCENE / "templates/storage_manager.html").read_text()
        js = (SCENE / "static/storage_manager.js").read_text()
        self.assertIn("dashboard_control_center.js?v=20261019-hide-algrow-v3", dashboard)
        self.assertIn("dashboard_no_algrow", scene)
        self.assertIn("method:\u0027GET\u0027", js)
        self.assertNotIn("method:\u0027POST\u0027", js)
        self.assertIn("renderCleanupStatus", js)
        self.assertIn("storage_manager.js?v=20261030-gc-audit-v3", template)

    def test_service_health_and_process_cards_removed_entirely(self):
        template = (SCENE / "templates/storage_manager.html").read_text()
        js = (SCENE / "static/storage_manager.js").read_text()
        css = (SCENE / "static/storage_manager.css").read_text()
        scene = (SCENE / "app.py").read_text()
        manager = MANAGER.read_text()

        for token in ("Service health", "Highest resource processes", 'id="service-grid"', 'id="process-list"'):
            self.assertNotIn(token, template)
        for token in ("renderServiceHealth", "activateService", "service_details", "service-grid", "process-list", "/services/"):
            self.assertNotIn(token, js)
        for token in (".sm-service", ".sm-process", ".sm-overview-grid"):
            self.assertNotIn(token, css)
        self.assertNotIn('@app.post("/api/admin/storage/services/{service_name}/start")', scene)
        for token in ("STARTABLE_SERVICES", "MONITORED_SERVICES", "service_detail(",
                      "start_allowlisted_service(", '"/v1/services/start"', '"processes":', '"service_details":'):
            self.assertNotIn(token, manager)
        for token in ('id="cpu-value"', 'id="ram-value"', 'id="disk-value"',
                      'id="deployment-gc-state"', 'id="gc-last-freed"',
                      'id="gc-next-run"', 'id="refresh-cleanup-status"'):
            self.assertIn(token, template)
        for token in ('id="policy-form"', 'id="full-production-clean"',
                      'id="confirm-dialog"', 'id="category-grid"'):
            self.assertNotIn(token, template)
        for token in ('if path == "/v1/metrics"', 'if path == "/v1/cleanup"',
                      'if path == "/v1/full-production-clean/status"', "def service_status("):
            self.assertIn(token, manager)

    def test_metrics_exclude_service_process_probes(self):
        spec = importlib.util.spec_from_file_location("storage_manager_panels_removed", MANAGER)
        manager = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(manager)
        # The main metrics endpoint should not call ps or systemctl.
        with patch.object(manager, "run", side_effect=AssertionError("unwanted subprocess diagnostics")):
            result = manager.metrics()
        self.assertTrue(result["ok"])
        self.assertIn("cpu", result)
        self.assertIn("memory", result)
        self.assertIn("disk", result)
        for field in ("services", "service_details", "startable_services", "processes"):
            self.assertNotIn(field, result)


if __name__ == "__main__":
    unittest.main()

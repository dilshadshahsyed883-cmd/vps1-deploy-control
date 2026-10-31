"""Dashboard is status/resources-only for admin and non-admin users."""
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "apps/scene-builder/part01/static"
TEMPLATE = STATIC.parent / "templates/dashboard.html"


class DashboardCompactContract(unittest.TestCase):
    def test_shared_dashboard_has_only_status_and_resources(self):
        html = TEMPLATE.read_text(encoding="utf-8")
        for expected in ("VPS Status", "CPU Usage", "RAM Usage", "Storage Usage", "Services Health", "System Resources", "dashboard-auto-refresh", "dashboard-updated"):
            self.assertIn(expected, html)
        for removed in ("overview-app-grid", "attention-list", "activity-preview", "Auto Caption Studio", "Recent Activity", ">Attention</h2>"):
            self.assertNotIn(removed, html)
        self.assertIn("{% macro dashboard_content() %}", html)
        self.assertNotIn("is_admin", html)
        workspace = (TEMPLATE.parent / "persistent_workspace.html").read_text(encoding="utf-8")
        self.assertIn("dashboard_content()", workspace)

    def test_adapter_has_no_removed_dom_writes(self):
        js = (STATIC / "dashboard_control_center.js").read_text(encoding="utf-8")
        for removed in ("overview-app-grid", "attention-list", "activity-preview"):
            self.assertNotIn(removed, js)
        for retained in ("vps-status", "resource-cpu-bar", "free-storage", "dashboard-refresh-now"):
            self.assertIn(retained, js)
        css = (STATIC / "dashboard_control_center.css").read_text(encoding="utf-8")
        self.assertIn(".dashboard-bottom-grid{display:block;margin-top:16px}", css)


if __name__ == "__main__":
    unittest.main()

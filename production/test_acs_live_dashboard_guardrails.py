"""Focused offline regressions for the October ACS/Storage/Dashboard repair."""
from pathlib import Path
import ast
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCENE = ROOT / "apps/scene-builder/part01"
ACS = ROOT / "apps/auto-caption-studio/part01"


class IntegrationRoutingTests(unittest.TestCase):
    def test_acs_live_uses_production_prefix(self):
        html = (ACS / "web_frontend/templates/live.html").read_text()
        self.assertIn("location.pathname.startsWith('/auto-caption-studio/')", html)
        self.assertIn("fetch(\`\${root}/api/live\`", html)
        self.assertIn("fetch(\`\${root}/api/live-audit\`", html)

    def test_audio_engine_never_loads_broken_acs_root(self):
        html = (ACS / "web_backend/acs_studio.html").read_text()
        self.assertNotIn('src="/acs-audio-engine/"', html)
        self.assertNotIn("fetch('/acs-audio-engine/health'", html)
        self.assertIn("audio-engine-fallback", html)
        self.assertIn("fetch('/audio-engine/app'", html)
        self.assertIn("frame.src = '/audio-engine/app'", html)
        self.assertIn('id="internalAudioEngineCard"', html)

    def test_dashboard_actual_service_names_and_no_fake_failures(self):
        path = SCENE / "app.py"
        code = path.read_text()
        ast.parse(code, filename=str(path))
        self.assertIn("vps1-production-auto-caption-studio.service", code)
        self.assertIn("vps1-production-lum-worker.service", code)
        self.assertIn("for port in (8144, 8100):", code)
        self.assertIn("A disconnected API is not a failed media project.", code)
        self.assertIn("Backend unconfigured does not mean a cleanup operation failed.", code)
        js = (SCENE / "static/dashboard_control_center.js").read_text()
        self.assertIn("status=!online?'Offline'", js)

    def test_storage_fallback_is_metrics_only(self):
        js = (SCENE / "static/storage_manager.js").read_text()
        self.assertIn("async function readOnlyVpsMetrics()", js)
        fallback = js.split("async function readOnlyVpsMetrics()", 1)[1].split(
            "async function loadMetrics(", 1)[0]
        self.assertIn("fetch('/api/dashboard/v1'", fallback)
        self.assertNotIn("'/api/admin/storage/cleanup'", fallback)
        self.assertIn("scan.disabled = true", fallback)
        self.assertIn("fullCleanButton.disabled = true", fallback)

    def test_guarded_release_contains_both_updated_pages(self):
        from production.scene_uploader_ui_release import FILES
        for rel in ("app.py", "templates/dashboard.html", "templates/storage_manager.html",
                    "static/dashboard_control_center.js", "static/storage_manager.js"):
            self.assertIn(rel, FILES)


if __name__ == "__main__":
    unittest.main()

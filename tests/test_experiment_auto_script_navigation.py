"""Experiment Studio Auto Script UI navigation: static, offline and read-only tests."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCENE = ROOT / "apps/scene-builder/part01"

class AutoScriptTabTests(unittest.TestCase):
    def test_existing_engine_and_api_unchanged(self):
        engine = (ROOT / "apps/auto-script/auto_script.py").read_text()
        self.assertIn('@app.get("/auto-script")', engine)
        self.assertIn('"page": "auto_script"', engine)
        self.assertIn('@app.get("/api/auto-script/health")', engine)

    def test_alias_checks_login_and_activation(self):
        app = (SCENE / "app.py").read_text()
        route = app.split('@app.get("/experiment-studio/auto-script/")', 1)[1].split("# EXPERIMENT_STUDIO_BROWSER_ENGINE_V1", 1)[0]
        self.assertIn("require_login(request)", route)
        self.assertIn('os.getenv("FRESH_AUTO_SCRIPT_READY", "0") != "1"', route)
        self.assertIn('raise HTTPException(status_code=404', route)
        self.assertIn('RedirectResponse("/auto-script", status_code=303)', route)

    def test_legacy_auto_script_uses_experiment_subtab(self):
        base = (SCENE / "templates/base.html").read_text()
        rail = base.split('class="vps1-experiment-tabs"', 1)[1].split("</nav>", 1)[0]
        self.assertEqual(rail.count("<a "), 6)
        for label in ("Antidetect Browser", "Self Media Library", "Auto Script",
                      "Video Studio", "Voice Studio", "Upload Studio"):
            self.assertEqual(rail.count(">" + label + "</a>"), 1)
        self.assertIn("page=='auto_script'", rail)
        self.assertIn("page in ['experiment_studio','auto_script','local_upload_manager'", base)
        self.assertNotIn("<iframe", base.lower())

    def test_library_navigation_and_cache_busting(self):
        shell = (SCENE / "static/vps1_native_site_shell.js").read_text()
        self.assertIn('experiment-studio|self-media-library|auto-script', shell)
        self.assertIn('["Auto Script", "/experiment-studio/auto-script/", "auto-script"]', shell)
        sml = (ROOT / "apps/self-media-library/part01/static/index.html").read_text()
        self.assertIn("vps1_native_site_shell.js?v=20261025-auto-script-nav-v1", sml)

if __name__ == "__main__":
    unittest.main()

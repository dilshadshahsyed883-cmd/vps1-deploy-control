"""Offline ownership contract for Scene website one-login app directory."""
from pathlib import Path
import unittest
from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "apps/scene-builder/part01/templates/base.html"
DASHBOARD = ROOT / "apps/scene-builder/part01/templates/dashboard.html"
PRIVATE_APPS = (
    "short-maker", "short-stream", "self-media-library", "video-merger",
    "voice-studio",
)
GRANTED_APPS = ("auto-caption-studio", "local-upload-manager")


class SingleLoginNavigation(unittest.TestCase):
    def test_sidebar_and_dashboard_routes_and_no_offline_auto_script(self):
        for p in (BASE, DASHBOARD):
            source = p.read_text()
            with self.subTest(file=p.name):
                Environment().parse(source)
                for app in PRIVATE_APPS + GRANTED_APPS:
                    self.assertIn('href="/' + app + '/"', source)
                self.assertNotIn('href="/auto-script"', source)
                self.assertIn('request.session.get("is_admin")', source)

    def test_non_admin_app_entitlements_are_not_ignored(self):
        for p in (BASE, DASHBOARD):
            source = p.read_text()
            self.assertIn('["auto_caption","both","all"]', source)
            self.assertIn('["lum","all"]', source)

    def test_scene_login_logout_preserved(self):
        base = BASE.read_text()
        self.assertIn('href="/"', base)
        self.assertIn('action="/logout"', base)
        self.assertIn('href="/queue"', base)


if __name__ == "__main__":
    unittest.main()

"""CW-003 universal sidebar regression tests for independently served Studio pages."""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCENE = ROOT / "apps/scene-builder/part01"
SHELL_JS = SCENE / "static/vps1_native_site_shell.js"
SHELL_CSS = SCENE / "static/vps1_native_site_shell.css"
LEGACY_PAGES = {
    "scene-builder": SCENE / "templates/base.html",
    "short-maker": ROOT / "apps/short-maker/part01/app.py",
    "video-merger": ROOT / "apps/video-merger/part01/templates/index.html",
}
NATIVE_PAGES = (
    ROOT / "apps/auto-caption-studio/part01/web_backend/acs_studio.html",
    ROOT / "apps/voice-studio/part01/templates/voice_studio.html",
    ROOT / "apps/local-upload-manager/part01/templates/base.html",
)
SIDEBAR_VERSION = "vps1_native_site_shell.js?v=20261020-experiment-v1"
CSS_VERSION = "vps1_native_site_shell.css?v=20261020-experiment-v1"


class UniversalSidebarContracts(unittest.TestCase):
    def test_shared_navigation_script_is_loaded_once_by_legacy_pages(self):
        for name, path in LEGACY_PAGES.items():
            html = path.read_text(encoding="utf-8")
            with self.subTest(name=name):
                self.assertEqual(html.count(SIDEBAR_VERSION), 1)
                self.assertEqual(html.count('data-unify-sidebar="1"'), 1)
                self.assertEqual(html.count(CSS_VERSION), 1)
                self.assertIn('id="app-sidebar" class="sidebar"', html)

    def test_existing_native_apps_use_the_same_shared_script(self):
        for path in NATIVE_PAGES:
            with self.subTest(path=path):
                self.assertIn("vps1_native_site_shell.js", path.read_text(encoding="utf-8"))

    def test_one_registry_for_four_primary_sidebar_destinations(self):
        src = SHELL_JS.read_text(encoding="utf-8")
        links = (
            '["Dashboard", "/", "dashboard"]',
            '["Upload Studio", "/local-upload-manager", "upload"]',
            '["Video Studio", "/scene-builder/", "video"]',
            '["Voice Studio", "/voice-studio/app#vps1-production", "voice"]',
        )
        for marker in links:
            self.assertEqual(src.count(marker), 1, marker)
        self.assertIn("addPrimaryLinks(navigation, current)", src)
        self.assertIn("const voiceLink = addPrimaryLinks(navigation, current).voice;", src)
        self.assertIn("installLegacySidebar()", src)
        self.assertIn("const isCanonical =", src)
        self.assertNotIn('["Auto Script",', src)

    def test_experiment_studio_menu_and_subtab_are_canonical(self):
        js = SHELL_JS.read_text(encoding="utf-8")
        self.assertIn('["Experiment Studio", "/experiment-studio/", "experiment"]', js)
        self.assertIn('return "experiment";', js)
        template = (SCENE / "templates/base.html").read_text(encoding="utf-8")
        self.assertIn('href="/experiment-studio/"', template)
        self.assertIn('aria-label="Experiment Studio"', template)
        self.assertIn('href="/experiment-studio/antidetect-browser/"', template)
        routes = (SCENE / "app.py").read_text(encoding="utf-8")
        self.assertIn('@app.get("/experiment-studio/")', routes)
        self.assertIn('@app.get("/experiment-studio/antidetect-browser/"', routes)
        self.assertIn('require_login(request)', routes)
        page = (SCENE / "templates/experiment_studio.html").read_text(encoding="utf-8")
        self.assertIn('extends "base.html"', page)
        self.assertIn('Antidetect Browser', page)
        self.assertIn('not yet installed', page.lower())

    def test_legacy_sidebar_role_is_verified_and_fails_closed(self):
        src = SHELL_JS.read_text(encoding="utf-8")
        for marker in (
            'fetch("/api/session/identity"',
            'credentials: "same-origin"',
            'identity.is_admin === true',
            'account.dataset.vps1IdentityStatus = authenticated ? "verified" : "unavailable"',
            'loginLink.hidden = authenticated',
            'logout.hidden = !authenticated',
            'setIdentity(null)',
            'adminLink.remove()',
            'logout.action = "/logout"',
            'window.addEventListener("pageshow", refreshIdentity)',
        ):
            self.assertIn(marker, src)
        for name in ("short-maker", "video-merger"):
            html = LEGACY_PAGES[name].read_text(encoding="utf-8")
            with self.subTest(name=name):
                self.assertNotIn('<a href="/admin">Admin</a>', html)
                self.assertNotIn('>Auto Uploader</a>', html)

    def test_sidebar_geometry_and_hidden_identity_are_canonical(self):
        css = SHELL_CSS.read_text(encoding="utf-8")
        for marker in (
            "--vps1-sidebar-width:214px",
            "#app-sidebar.sidebar,#vps1-native-site-sidebar",
            "#app-sidebar.sidebar .sidebar-account[hidden]{display:none!important}",
            "#app-sidebar .server,#app-sidebar .server-dot",
            "@view-transition { navigation: auto; }",
        ):
            self.assertIn(marker, css)

    @unittest.skipUnless(shutil.which("node"), "Node runtime unavailable")
    def test_shared_browser_script_parses_as_javascript(self):
        result = subprocess.run(
            ["node", "--check", str(SHELL_JS)], capture_output=True,
            text=True, check=False, timeout=12,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()

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
SIDEBAR_VERSION = "vps1_native_site_shell.js?v=20261020-sidebar-stable-v4"
CSS_VERSION = "vps1_native_site_shell.css?v=20261020-sidebar-stable-v4"


class UniversalSidebarContracts(unittest.TestCase):
    def test_shared_navigation_script_is_loaded_once_by_legacy_pages(self):
        for name, path in LEGACY_PAGES.items():
            html = path.read_text(encoding="utf-8")
            with self.subTest(name=name):
                script_version = SIDEBAR_VERSION if name == "scene-builder" else "vps1_native_site_shell.js?v=20261020-experiment-v1"
                style_version = CSS_VERSION if name == "scene-builder" else "vps1_native_site_shell.css?v=20261020-experiment-v1"
                self.assertEqual(html.count(script_version), 1)
                self.assertEqual(html.count('data-unify-sidebar="1"'), 1)
                self.assertEqual(html.count(style_version), 1)
                self.assertIn('id="app-sidebar" class="sidebar"', html)

    def test_existing_native_apps_use_the_same_shared_script(self):
        for path in NATIVE_PAGES:
            with self.subTest(path=path):
                self.assertIn("vps1_native_site_shell.js", path.read_text(encoding="utf-8"))

    def test_one_registry_for_role_gated_primary_sidebar_destinations(self):
        src = SHELL_JS.read_text(encoding="utf-8")
        links = (
            '["Dashboard", "/", "dashboard"]',
            '["Song Studio", "/song-studio/", "song"]',
            '["Experiment Studio", "/experiment-studio/", "experiment"]',
            '["Voice Studio", "/voice-studio/app#vps1-production", "voice"]',
        )
        for marker in links:
            self.assertEqual(src.count(marker), 1, marker)
        self.assertIn("addPrimaryLinks(navigation, current)", src)
        self.assertIn("const voiceLink = primaryLinks.voice;", src)
        for forbidden in ('["Upload Studio", "/local-upload-manager", "upload"]',
                          '["Video Studio", "/scene-builder/", "video"]',):
            self.assertNotIn(forbidden, src.split("const PRIMARY_NAV =", 1)[1].split("]);", 1)[0])
        self.assertIn("const songLink = primaryLinks.song;", src)
        self.assertIn("installLegacySidebar()", src)
        self.assertIn("const isCanonical =", src)
        self.assertNotIn('["Auto Script",', src.split("const PRIMARY_NAV =", 1)[1].split("]);", 1)[0])

    def test_experiment_studio_menu_and_subtab_are_canonical(self):
        js = SHELL_JS.read_text(encoding="utf-8")
        self.assertIn('["Experiment Studio", "/experiment-studio/", "experiment"]', js)
        self.assertIn('return "experiment";', js)
        template = (SCENE / "templates/base.html").read_text(encoding="utf-8")
        self.assertIn('href="/experiment-studio/"', template)
        self.assertIn('aria-label="Experiment Studio"', template)
        self.assertIn('href="/experiment-studio/antidetect-browser/"', template)
        self.assertIn('href="/experiment-studio/self-media-library/"', template)
        self.assertIn('href="/experiment-studio/auto-script/"', template)
        self.assertIn("page in ['experiment_studio','auto_script','local_upload_manager'", template)
        self.assertEqual(template.count('aria-label="Experiment Studio"'), 1)
        routes = (SCENE / "app.py").read_text(encoding="utf-8")
        self.assertIn('@app.get("/experiment-studio/self-media-library/")', routes)
        self.assertIn('return RedirectResponse("/self-media-library/", status_code=303)', routes)
        self.assertIn('@app.get("/experiment-studio/")', routes)
        self.assertIn('@app.get("/experiment-studio/antidetect-browser/"', routes)
        self.assertIn('@app.get("/experiment-studio/auto-script/")', routes)
        self.assertIn('return RedirectResponse("/auto-script", status_code=303)', routes)
        self.assertIn('require_login(request)', routes)
        page = (SCENE / "templates/experiment_studio.html").read_text(encoding="utf-8")
        self.assertIn('extends "base.html"', page)
        self.assertIn('Antidetect Browser', page)
        self.assertIn('Browser profiles', page)
        self.assertIn('experiment_browser.js', page)

    def test_song_studio_reuses_downloader_and_reserves_independent_apps(self):
        src = SHELL_JS.read_text(encoding="utf-8")
        base = (SCENE / "templates/base.html").read_text(encoding="utf-8")
        voice = (ROOT / "apps/voice-studio/part01/templates/voice_studio.html").read_text(encoding="utf-8")
        scene = (SCENE / "app.py").read_text(encoding="utf-8")
        css = SHELL_CSS.read_text(encoding="utf-8")
        slot = (SCENE / "templates/song_studio_placeholder.html").read_text(encoding="utf-8")
        self.assertIn('["Song Studio", "/song-studio/", "song"]', src)
        self.assertIn('window.location?.hash === "#vs-audio" ||', src)
        self.assertIn('^\\/audio-downloader\\/?$/.test(window.location?.pathname', src)
        self.assertIn('tabs.setAttribute("aria-label", "Song Studio")', src)
        for name in ("Audio Downloader", "Song Maker", "Song Uploader"):
            self.assertIn(f'addLink(tabs, "{name}"', src)
            self.assertIn(name, base)
        self.assertIn('href="/song-studio/"', base)
        self.assertIn('aria-label="Song Studio"', base)
        self.assertIn('data-vps1-server-identity="1"', base)
        self.assertIn('id="vsEngineAudio" role="tab" aria-selected="false" hidden', voice)
        self.assertIn('SONG STUDIO · AUDIO EXTRACTION', voice)
        self.assertIn('window.dispatchEvent(new Event("vps1:voice-engine-changed"))', voice)
        self.assertIn('body.vps1-native-voice #vsEngineAudio{display:none!important}', css)
        # Song Studio must show only its own tabs; Voice Studio retains its engines.
        self.assertIn('body.vs-audio-active .vs-engine-selector{display:none!important}', voice)
        self.assertIn('@app.get("/song-studio/")', scene)
        self.assertIn('@app.get("/song-studio/audio-downloader/")', scene)
        self.assertIn('RedirectResponse("/audio-downloader/", status_code=303)', scene)
        self.assertIn('@app.get("/audio-downloader/", response_class=HTMLResponse)', scene)
        self.assertIn('_audio_downloader_canonical_html(upstream.content)', scene)
        self.assertIn('addLink(tabs, "Audio Downloader", "/audio-downloader/"', src)
        self.assertIn('@app.get("/song-studio/{studio_app}/", response_class=HTMLResponse)', scene)
        self.assertIn('names = {"song-uploader": "Song Uploader"}', scene)
        self.assertIn('return RedirectResponse("/song-maker/", status_code=303)', scene)
        self.assertIn('require_login(request)', scene)
        self.assertIn('Its processing service has not', slot)

    def test_library_uses_native_experiment_shell_with_five_independent_inner_tabs(self):
        source = SHELL_JS.read_text(encoding="utf-8")
        self.assertIn('["acs", "voice", "uploader", "stream", "library", "song", "song-uploader", "short-uploader", "copyright-uploader"]', source)
        self.assertIn('app === "library"', source)
        self.assertIn('experiment-studio|self-media-library', source)
        self.assertIn('["acs", "uploader", "stream", "library"].includes(app)', source)
        for label in ("Antidetect Browser", "Self Media Library", "Auto Script",
                      "Video Studio", "Upload Studio"):
            self.assertIn(f'["{label}",', source)
        self.assertIn("const createExperimentTabs =", source)
        self.assertIn("attachInnerTabs(tabs)", source)
        html = (ROOT / "apps/self-media-library/part01/static/index.html").read_text(encoding="utf-8")
        self.assertIn('data-workspace="library"', html)
        self.assertIn('aria-label="Self Media Library"', html)
        self.assertIn('class="sml-control-rail"', html)
        self.assertNotIn('<aside class="sidebar">', html)
        for subtab in ("dashboard", "sources", "processing", "library", "settings"):
            self.assertIn(f'data-page="{subtab}"', html)
        self.assertIn('id="healthText"', html)
        self.assertIn('id="healthDot"', html)
        css = SHELL_CSS.read_text(encoding="utf-8")
        self.assertIn("body.vps1-native-library .app-shell", css)
        self.assertIn(".vps1-native-experiment-tabs", css)

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

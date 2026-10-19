"""Native one-shell Video Studio, Voice Studio and Uploader regression tests."""
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCENE = ROOT / "apps/scene-builder/part01"
T = SCENE / "templates"


class SceneVideoStudioShell(unittest.TestCase):
    def test_native_redirect_release_retries_stale_scene_worker(self):
        from production.scene_uploader_ui_release import verify_video_studio
        seen = []
        destinations = {
            "/local-upload-manager/app?embedded=1&tab=dashboard":
                b'lum-shell vps1_native_site_shell.js',
            "/auto-caption-studio/api/web-flow/studio?embed=1":
                b'<html data-page="queue" data-page="upload" data-page="voice" '
                b'id="acs-native-live" vps1_native_site_shell.js '
                b"const API='/auto-caption-studio/api/web-flow'",
            "/short-maker/": b'id="makerTabBtn" data-video-studio-subnav',
            "/video-merger/": b'id="tab-merge" data-video-studio-subnav',
            "/voice-studio/app": b'vsEngineOmni vsEngineKokoro vps1_native_site_shell.js',
        }
        def get(path, cookie):
            seen.append(path)
            if path == "/video-studio/short-maker" and seen.count(path) == 1:
                return 404, "text/html", b"retired-worker"
            if path in destinations:
                return 200, "text/html; charset=utf-8", destinations[path]
            return 303, "text/html", b""
        with patch("production.route_uploader_scene_https.scene_cookie",
                   return_value="session=admin"), \
             patch("production.route_uploader_scene_https.https_get", side_effect=get), \
             patch("production.scene_uploader_ui_release.time.sleep"):
            verify_video_studio()
        self.assertEqual(seen.count("/video-studio/short-maker"), 2)

    def test_native_release_fails_closed_on_denial(self):
        from production.scene_uploader_ui_release import verify_video_studio
        with patch("production.route_uploader_scene_https.scene_cookie",
                   return_value="session=admin"), \
             patch("production.route_uploader_scene_https.https_get",
                   return_value=(403, "text/plain", b"denied")):
            with self.assertRaisesRegex(RuntimeError, "status=403"):
                verify_video_studio()

    def test_original_scene_navigation_and_video_tabs(self):
        html = (T / "base.html").read_text()
        for label in ("Dashboard", "Auto Uploader", "Video Studio", "Voice Studio", "Admin"):
            self.assertIn(label, html)
        for url in ("/queue", "/auto-caption/studio",
                    "/video-studio/short-maker", "/video-studio/video-merger"):
            self.assertIn('href="' + url + '"', html)
        self.assertNotIn('href="/auto-script"', html)

    def test_native_documents_and_no_outer_iframe_shells(self):
        app = (SCENE / "app.py").read_text()
        for target in ("/local-upload-manager/app?embedded=1&tab=dashboard",
                       "/auto-caption-studio/api/web-flow/studio?embed=1",
                       "/voice-studio/app"):
            self.assertIn('RedirectResponse("' + target + '"', app)
        self.assertIn("return RedirectResponse(url, status_code=303)", app)
        for fname in ("acs_studio_shell.html", "video_studio_embed.html",
                      "voice_studio_host.html"):
            self.assertNotIn("<iframe", (T / fname).read_text())
        self.assertNotIn("Voice Studio unavailable", (T / "voice_studio_host.html").read_text())
        native = (SCENE / "static/vps1_native_site_shell.js").read_text()
        self.assertIn("createElement", native)
        self.assertNotIn(".innerHTML", native)
        self.assertIn("vps1-native-site-sidebar", native)

    def test_native_sidebar_sources_admin_link_from_live_scene_role(self):
        native = (SCENE / "static/vps1_native_site_shell.js").read_text()
        css = (SCENE / "static/vps1_native_site_shell.css").read_text()
        app = (SCENE / "app.py").read_text()
        self.assertIn('@app.get("/api/session/identity")', app)
        self.assertIn("user_account(str(request.session.get", app)
        self.assertIn('headers={"Cache-Control": "private, no-store", "Vary": "Cookie"}', app)
        self.assertIn('fetch("/api/session/identity"', native)
        self.assertIn('credentials: "same-origin"', native)
        self.assertIn('identity.is_admin === true', native)
        self.assertIn('adminLink.href = "/admin"', native)
        self.assertIn('adminLink.remove()', native)
        self.assertIn('logout.action = "/logout"', native)
        self.assertIn('#vps1-native-site-sidebar [hidden]', css)

    def test_unified_sidebar_source_geometry_and_cross_document_transition(self):
        scene = (SCENE / "templates/base.html").read_text()
        native_css = (SCENE / "static/vps1_native_site_shell.css").read_text()
        native_js = (SCENE / "static/vps1_native_site_shell.js").read_text()
        self.assertIn('href="/static/vps1_native_site_shell.css?v=20261019-unified-v2"', scene)
        self.assertIn('id="app-sidebar" class="sidebar"', scene)
        self.assertIn("--vps1-sidebar-width:214px", native_css)
        self.assertIn("#app-sidebar.sidebar,#vps1-native-site-sidebar", native_css)
        self.assertIn("@view-transition { navigation: auto; }", native_css)
        self.assertIn("view-transition-name:vps1-global-sidebar", native_css)
        self.assertIn('html:has(script[data-workspace="uploader"]) body', native_css)
        self.assertIn('document.documentElement.classList.add("vps1-native-bootstrap")', native_js)
        self.assertIn("@media(max-width:760px)", native_css)

    def test_voice_sidebar_navigation_never_forces_redundant_page_reload(self):
        scene = (SCENE / "templates/base.html").read_text()
        native = (SCENE / "static/vps1_native_site_shell.js").read_text()
        app = (SCENE / "app.py").read_text()
        self.assertIn('href="/voice-studio/app#vps1-production"', scene)
        self.assertIn('"/voice-studio/app#vps1-production"', native)
        self.assertIn('window.vps1VoiceStudioTab("production")', native)
        self.assertIn('event.preventDefault()', native)
        self.assertIn('typeof window.vps1VoiceStudioTab !== "function"', native)
        self.assertIn('RedirectResponse("/voice-studio/app", status_code=303)', app)

    def test_native_studio_preserves_app_forms_tabs_and_api(self):
        root = ROOT / "apps"
        maker = (root / "short-maker/part01/app.py").read_text()
        merger = (root / "video-merger/part01/templates/index.html").read_text()
        voice = (root / "voice-studio/part01/templates/voice_studio.html").read_text()
        lum = (root / "local-upload-manager/part01/templates/base.html").read_text()
        acs = (root / "auto-caption-studio/part01/web_backend/acs_studio.html").read_text()
        for marker in ('id="makerTabBtn"', 'id="queueTabBtn"'):
            self.assertIn(marker, maker)
        for marker in ('id="tab-merge"', 'id="tab-library"'):
            self.assertIn(marker, merger)
        for marker in ('vsEngineOmni', 'vsEngineKokoro', 'vsEngineEdge', 'vsEngineAudio'):
            self.assertIn(marker, voice)
        for document in (voice, lum, acs):
            self.assertIn("vps1_native_site_shell.js", document)
        for tab in ("queue", "upload", "drive-profiles", "preset", "captions",
                    "pexels", "fg", "voice", "output"):
            self.assertIn('data-page="' + tab + '"', acs)
        self.assertIn('id="acs-native-live"', acs)
        self.assertIn('id="batchRows"', acs)
        self.assertIn("const API='/auto-caption/api/web-flow'", acs)
        self.assertNotIn('<iframe class="acs-live-embed-v1"', acs)
        live = (root / "auto-caption-studio/part01/web_frontend/templates/live.html").read_text()
        self.assertNotIn("['voice','Algrow',", live)
        self.assertIn("['render','Renderer',", live)

    def test_independent_nginx_prefix_rewrite_keeps_scene_assets(self):
        import html as html_util
        root = ROOT / "apps"
        for file in (
            root / "voice-studio/part01/templates/voice_studio.html",
            root / "auto-caption-studio/part01/web_backend/acs_studio.html",
        ):
            with self.subTest(file=file):
                source = file.read_text()
                for ext in ("js", "css"):
                    self.assertIn(
                        "/&#115;tatic/vps1_native_site_shell." + ext, source)
                    self.assertIn(
                        "/static/vps1_native_site_shell." + ext,
                        html_util.unescape(source))
                self.assertNotIn('src="/static/vps1_native_site_shell.js"', source)

    def test_guarded_scene_native_readback_gate(self):
        from tempfile import TemporaryDirectory
        from unittest.mock import patch
        from production.scene_candidate_release import _verify_native_ui_release
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("production.scene_uploader_ui_release.verify_video_studio") as check:
                _verify_native_ui_release(root)
                check.assert_not_called()
                file = root / "part01/static/vps1_native_site_shell.js"
                file.parent.mkdir(parents=True)
                file.write_text("/* native */")
                _verify_native_ui_release(root)
                check.assert_called_once_with()

    def test_authenticated_video_app_routing_for_both_roles(self):
        app = (SCENE / "app.py").read_text()
        body = app.split("def video_studio_embedded_app(", 1)[1].split(
            '@app.get("/auto-caption/studio"', 1)[0]
        self.assertIn("require_login(request)", body)
        self.assertNotIn("require_admin(request)", body)
        self.assertIn('"short-maker": ("Short Maker", "/short-maker/")', body)
        self.assertIn('"video-merger": ("Video Merger", "/video-merger/")', body)
        self.assertIn("if studio_app not in allowed:", body)
        self.assertIn("return RedirectResponse(url, status_code=303)", body)

    def test_jinja_fallback_templates_still_parse(self):
        from jinja2 import Environment
        for name in ("video_studio_embed.html", "acs_studio_shell.html",
                     "voice_studio_host.html", "base.html"):
            Environment().parse((T / name).read_text())


if __name__ == "__main__":
    unittest.main()

"""Preserve the original Scene sidebar and Video Studio subtabs."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
T = ROOT / "apps/scene-builder/part01/templates"


class SceneVideoStudioShell(unittest.TestCase):
    def test_release_readback_retries_stale_scene_worker(self):
        from unittest.mock import patch
        from production.scene_uploader_ui_release import verify_video_studio
        calls = []
        def get(path, cookie):
            calls.append(path)
            if path == "/video-studio/short-maker" and calls.count(path) == 1:
                return 404, "text/html", b"retired-worker"
            expected = {
                "/local-upload-manager": b'id="app-sidebar" /local-upload-manager/app?',
                "/auto-caption/studio": b'id="app-sidebar" src="/auto-caption-studio/api/web-flow/studio?embed=1"',
                "/auto-caption-studio/api/web-flow/studio?embed=1": (
                    b'<html data-page="queue" data-page="upload" data-page="preset" '
                    b'data-page="captions" data-page="pexels" data-page="fg" '
                    b'data-page="voice" data-page="drive-profiles" data-page="output" '
                    b"const API='/auto-caption-studio/api/web-flow' "
                    b'src="/auto-caption-studio/live?embed=1"'
                ),
                "/video-studio/short-maker": b'id="app-sidebar" src="/short-maker/"',
                "/video-studio/video-merger": b'id="app-sidebar" src="/video-merger/"',
            }
            return 200, "text/html; charset=utf-8", expected[path]
        with patch("production.route_uploader_scene_https.scene_cookie",
                   return_value="session=admin"), \
             patch("production.route_uploader_scene_https.https_get",
                   side_effect=get), \
             patch("production.scene_uploader_ui_release.time.sleep"):
            verify_video_studio()
        self.assertEqual(calls.count("/video-studio/short-maker"), 2)

    def test_release_readback_fails_closed_on_real_denial(self):
        from unittest.mock import patch
        from production.scene_uploader_ui_release import verify_video_studio
        with patch("production.route_uploader_scene_https.scene_cookie",
                   return_value="session=admin"), \
             patch("production.route_uploader_scene_https.https_get",
                   return_value=(403, "text/plain", b"denied")):
            with self.assertRaisesRegex(RuntimeError, "status=403"):
                verify_video_studio()

    def test_original_sidebar_and_video_studio_subtabs(self):
        html = (T / "base.html").read_text()
        for label in ("Dashboard", "Auto Uploader", "Video Studio", "Voice Studio", "Admin"):
            self.assertIn(label, html)
        self.assertIn('data-video-studio-subnav="1"', html)
        for link in ('/queue', '/auto-caption/studio',
                     '/video-studio/short-maker', '/video-studio/video-merger'):
            self.assertIn('href="' + link + '"', html)
        self.assertNotIn('href="/auto-script"', html)

    def test_native_acs_document_preserves_scripts_and_session(self):
        html = (T / "acs_studio_shell.html").read_text()
        self.assertIn('src="/auto-caption-studio/api/web-flow/studio?embed=1"', html)
        self.assertIn('id="acs-studio-frame"', html)
        self.assertNotIn('src="/auto-caption-studio/"', html)
        self.assertNotIn('root.innerHTML = html', html)

    def test_original_rich_acs_routes_and_full_feature_tabs(self):
        root = ROOT / "apps/auto-caption-studio/part01/web_backend"
        html = (root / "acs_studio.html").read_text()
        source = (root / "web_flow_api.py").read_text()
        self.assertIn('@router.get("/studio"', source)
        self.assertIn('html.replace("/auto-caption/api/", "/auto-caption-studio/api/")', source)
        self.assertIn('src="/auto-caption/live?embed=1"', html)
        for tab in ("queue", "upload", "drive-profiles", "preset", "captions",
                    "pexels", "fg", "voice", "output"):
            self.assertIn('data-page="' + tab + '"', html)
        result = html.replace("/auto-caption/api/", "/auto-caption-studio/api/")
        result = result.replace('src="/auto-caption/live?embed=1"',
                                'src="/auto-caption-studio/live?embed=1"')
        self.assertIn("const API='/auto-caption-studio/api/web-flow'", result)
        self.assertIn('src="/auto-caption-studio/live?embed=1"', result)
        self.assertNotIn("const API='/auto-caption/api/web-flow'", result)

    def test_jinja_parses_actual_app_wrapper_not_css_comments(self):
        from jinja2 import Environment
        for name in ("video_studio_embed.html", "acs_studio_shell.html", "base.html"):
            with self.subTest(template=name):
                Environment().parse((T / name).read_text())
        wrapper = (T / "video_studio_embed.html").read_text()
        self.assertNotIn("{#video-studio-app-frame", wrapper)
        self.assertIn(" #video-studio-app-frame", wrapper)

    def test_admin_only_independent_app_wrappers(self):
        app = (ROOT / "apps/scene-builder/part01/app.py").read_text()
        self.assertIn('@app.get("/video-studio/{studio_app}"', app)
        body = app.split("def video_studio_embedded_app(", 1)[1].split(
            '@app.get("/auto-caption/studio"', 1)[0]
        self.assertIn("require_admin(request)", body)
        self.assertIn('"short-maker": ("Short Maker", "/short-maker/")', body)
        self.assertIn('"video-merger": ("Video Merger", "/video-merger/")', body)
        self.assertIn("if studio_app not in allowed:", body)
        shell = (T / "video_studio_embed.html").read_text()
        self.assertIn('src="{{ studio_url }}"', shell)
        self.assertIn('extends "base.html"', shell)

    def test_uploader_exact_path_is_scene_shell(self):
        html = (T / "local_upload_manager.html").read_text()
        self.assertIn('{% extends "base.html" %}', html)
        self.assertIn('id="lum-host-frame"', html)
        self.assertIn('/local-upload-manager/app?embedded=1', html)

if __name__ == "__main__":
    unittest.main()

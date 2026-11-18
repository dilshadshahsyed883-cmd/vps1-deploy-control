"""Rendered HTML and scoped CSS regression tests for Experiment's four GPU studios.

Upload Studio's existing native page/layout must not be modified by this fix.
"""
from pathlib import Path
import re
import unittest
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parents[1] / "apps/scene-builder/part01"
TEMPLATE_DIR = ROOT / "templates"
SHELL = (TEMPLATE_DIR / "base.html").read_text(encoding="utf-8")
CSS_PATH = ROOT / "static/experiment_four_studio_layout.css"
CSS = CSS_PATH.read_text(encoding="utf-8")
FOUR_PAGES = (
    ("cloud_render", "Concat", "cloud-render"),
    ("heygen_studio", "HeyGen Studio", "heygen-studio"),
    ("imagen_studio", "Imagen", "imagen-studio"),
    ("voice_clone_studio", "Voice Clone", "voice-clone-studio"),
)


class ExperimentFourStudioLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=True)

    def render(self, page, admin=True):
        return self.env.get_template("base.html").render(
            page=page,
            request=SimpleNamespace(
                session={"is_admin": admin, "username": "owner", "studio_access": (), "app_access": "all"},
                query_params={},
            ),
            username="owner",
        )

    def test_all_four_studios_share_one_aligned_shell_and_navigation(self):
        for page, label, route in FOUR_PAGES:
            with self.subTest(page=page):
                html = self.render(page)
                self.assertIn('class="vps1-experiment-four-main ', html)
                self.assertIn('/static/experiment_four_studio_layout.css?v=20261116-1', html)
                if page == "cloud_render":
                    self.assertEqual(html.count('aria-label="Experiment Studio"'), 0)
                    self.assertEqual(html.count('aria-label="Concat Studio"'), 1)
                    self.assertIn('href="/concat-studio/concat/"', html)
                    self.assertIn('href="/concat-studio/concat-uploader/"', html)
                    self.assertRegex(html, r'<nav class="vps1-experiment-tabs vps1-concat-studio-tabs"')
                else:
                    self.assertEqual(html.count('aria-label="Experiment Studio"'), 1)
                    self.assertIn('href="/' + route + '/"', html)
                    self.assertRegex(html, r'<nav class="vps1-experiment-tabs"')
                self.assertIn(label, html)

    def test_upload_studio_uses_unchanged_native_layout(self):
        html = self.render("local_upload_manager")
        self.assertNotIn("vps1-experiment-four-main", html)
        self.assertNotIn("experiment_four_studio_layout.css", html)
        self.assertEqual(html.count('aria-label="Experiment Studio"'), 1)

    def test_unrelated_scene_builder_page_is_untouched(self):
        html = self.render("dashboard")
        self.assertNotIn("vps1-experiment-four-main", html)
        self.assertNotIn("experiment_four_studio_layout.css", html)

    def test_css_has_centered_equal_width_rails_and_safe_mobile_breakpoint(self):
        self.assertIn("main.vps1-experiment-four-main > .vps1-experiment-tabs,", CSS)
        self.assertIn("main.vps1-experiment-four-main > :is(#cloud-render,#heygen-studio,#eg-root)", CSS)
        self.assertIn("max-width:var(--vps1-ui-content-max,1400px)!important;", CSS)
        self.assertIn("max-width:none!important;", CSS)
        self.assertIn("width:calc(100% - var(--vps1-sidebar-width,214px))!important;", CSS)
        self.assertIn("@media (min-width:761px)", CSS)
        self.assertIn("@media (max-width:760px)", CSS)
        self.assertNotIn("body.vps1-native-uploader", CSS)
        self.assertNotIn("body.vps1-native-stream", CSS)


if __name__ == "__main__":
    unittest.main()

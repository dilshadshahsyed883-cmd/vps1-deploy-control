"""Native uploader header regression after no-outer-iframe Scene upgrade."""
import ast
from pathlib import Path
import unittest
ROOT=Path(__file__).resolve().parents[1]
LUM=ROOT/"apps/local-upload-manager/part01"
SCENE=ROOT/"apps/scene-builder/part01"

class NativeUploaderTopTabs(unittest.TestCase):
    def test_native_scene_uses_authenticated_uploader_document(self):
        app=(SCENE/"app.py").read_text()
        ast.parse(app)
        segment=app.split('@app.get("/local-upload-manager", response_class=HTMLResponse)',1)[1].split('@app.get(',1)[0]
        self.assertIn("require_local_upload_manager_access(request)",segment)
        self.assertIn('RedirectResponse("/local-upload-manager/app?embedded=1&tab=dashboard"',segment)
        native=(LUM/"templates/base.html").read_text()
        self.assertIn('vps1_native_site_shell.js',native)

    def test_one_primary_tab_bar_above_eight_workspace_tabs(self):
        html=(LUM/"templates/app.html").read_text()
        self.assertEqual(html.count('aria-label="Auto Uploader main subtabs"'),1)
        self.assertIn('href="/auto-stream/"',html)
        self.assertLess(html.index('class="lum-primary-workspaces"'),html.index('class="lum-statusbar"'))
        self.assertLess(html.index('class="lum-primary-workspaces"'),html.index('class="lum-tabs-shell"'))
        self.assertIn("{% if not embedded_view %}\n  <section class=\"lum-statusbar\">",html)
        self.assertIn("['engine1', 'engine2', 'shorts']",html)
        self.assertNotIn("['engine1', 'engine2', 'shorts', 'short_stream']",html)

    def test_iframed_isolated_engines_do_not_repeat_workspace_nav(self):
        html=(LUM/"templates/app.html").read_text()
        self.assertIn("engine_instance == 'engine1' or not embedded_view",html)
        self.assertIn('app?embedded=1&tab=auto_engine',html)
        self.assertIn("engine_instance == 'engine1'",html)

    def test_native_stream_is_dormant_and_separate(self):
        html=(LUM/"templates/app.html").read_text()
        self.assertIn("tab == 'auto_stream'",html)
        self.assertIn('href="/short-stream/"',html)
        self.assertIn("No stream is started or changed by opening this tab",html)
        self.assertNotIn('data-lum-engine="4"',html)

    def test_jinja_parses(self):
        try: from jinja2 import Environment
        except ImportError: self.skipTest("Jinja2 missing")
        Environment().parse((LUM/"templates/app.html").read_text())

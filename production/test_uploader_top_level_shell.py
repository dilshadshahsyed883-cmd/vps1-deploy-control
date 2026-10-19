"""Screenshot regression: one Scene header and only one three-mode uploader selector."""
import ast
from pathlib import Path
import unittest
ROOT=Path(__file__).resolve().parents[1]
SCENE=ROOT/"apps/scene-builder/part01"
LUM=ROOT/"apps/local-upload-manager/part01"
class UploaderTopLevelShellTests(unittest.TestCase):
    def test_scene_header_has_two_main_tabs(self):
        base=(SCENE/"templates/base.html").read_text()
        self.assertIn('aria-label="Auto Uploader and Auto Stream"',base)
        self.assertIn('href="/local-upload-manager?workspace=stream"',base)
        html=(SCENE/"templates/local_upload_manager.html").read_text()
        self.assertIn("{% if lum_workspace == 'stream' %}",html)
        self.assertEqual(html.count('data-lum-shared-tab="'),8)
        self.assertNotIn("data-lum-engine=",html)
        self.assertNotIn("lum-engine-selector",html)
        self.assertIn("LUM_SCENE_SINGLE_SHELL_NAV_V2",html)
        self.assertIn('id="lum-host-frame"',html)
        self.assertIn("Opening this workspace does not start, stop",html)
    def test_embedded_app_hides_duplicate_heading_and_primary_tabs(self):
        html=(LUM/"templates/app.html").read_text()
        self.assertIn('{% if not embedded_view %}\n  <section class="lum-statusbar">',html)
        self.assertIn("engine_instance == 'engine1' and not embedded_view",html)
        self.assertIn("['engine1', 'engine2', 'shorts']",html)
    def test_stream_navigation_remains_authorized_no_stream_execution(self):
        app=(SCENE/"app.py").read_text()
        ast.parse(app)
        route=app.split('@app.get("/local-upload-manager", response_class=HTMLResponse)',1)[1].split('@app.get(',1)[0]
        self.assertIn("require_local_upload_manager_access(request)",route)
        self.assertIn('"lum_workspace": selected_workspace',route)
        self.assertNotIn("start_stream",route)
        self.assertNotIn("enqueue",route)
    def test_old_fourth_mode_not_restored(self):
        js=(SCENE/"static/lum_host_state_v1.js").read_text()
        self.assertIn("['engine1','engine2','shorts'].includes(autoMode)",js)
        self.assertIn("validTabs.includes(tab)?tab:'dashboard'",js)
        self.assertNotIn("['engine1','engine2','shorts','short_stream']",js)
    def test_jinja_parses(self):
        try: from jinja2 import Environment
        except ImportError: self.skipTest("jinja2 missing")
        for p in (SCENE/"templates/base.html",SCENE/"templates/local_upload_manager.html",LUM/"templates/app.html"):
            with self.subTest(file=str(p)): Environment().parse(p.read_text())

"""Focused offline checks for Song/Short Uploader's four profile-list controls."""
import ast
import shutil
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "apps/local-upload-manager/part01/templates/app.html"
APP = ROOT / "apps/local-upload-manager/part01/lum_engine/standalone_app.py"
SCRIPT = ROOT / "apps/local-upload-manager/part01/static/auto_engine_v6.js"

LABELS = ("Generate Queue Now", "Preview Next Jobs", "Profile Schedules", "Start All Profiles")


class UploaderProfileBulkToolbarTests(unittest.TestCase):
    def render(self, instance):
        source = TEMPLATE.read_text()
        start = source.index('<section class="lum-card lum-ae-automation-controls"')
        end = source.index('</section>', start) + len('</section>')
        return Environment(autoescape=True).from_string(source[start:end]).render(
            engine_instance=instance, ae=SimpleNamespace(profiles=[]),
            p=None, base_path="/short-uploader" if instance == "shortstudio" else "/song-uploader",
            csrf="test-token"
        )

    def test_exact_controls_in_order_above_list_in_song_and_short(self):
        for instance in ("song", "shortstudio"):
            with self.subTest(instance=instance):
                html = self.render(instance)
                expected = LABELS if instance == "song" else tuple(label for label in LABELS if label != "Profile Schedules")
                positions = [html.index(">" + label + "<") for label in expected]
                self.assertEqual(positions, sorted(positions))
                self.assertLess(positions[-1], html.index('class="lum-table-wrap lum-ae-automation-profiles-wrap"'))
                self.assertEqual(html.count('data-ae-open-schedules'), 1 if instance == "song" else 0)
                if instance == "shortstudio":
                    self.assertNotIn(">Profile Schedules<", html)
                self.assertIn('data-ae-preview-all', html)
                self.assertIn('data-ae-jump-start-all', html)
                self.assertIn('/actions/auto-v6/generate-all', html)
                self.assertIn('name="confirmation" value="GENERATE ALL"', html)

    def test_engine1_does_not_receive_new_group_toolbar(self):
        html = self.render("engine1")
        for label in LABELS:
            self.assertNotIn(">" + label + "<", html)

    def test_backend_preview_is_read_only_and_generate_does_not_arm_engine(self):
        source = APP.read_text()
        parsed = ast.parse(source)
        functions = {node.name: ast.get_source_segment(source, node)
                     for node in parsed.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        generate = functions["auto_v6_generate_all"]
        preview = functions["auto_v6_preview_all"]
        self.assertIn("require_csrf(request,csrf_token)", generate)
        self.assertIn('str(confirmation).strip()!="GENERATE ALL"', generate)
        self.assertIn('{"song","shortstudio"}', generate)
        self.assertIn('get("song_folder_owner")', generate)
        self.assertIn('get("enabled")', generate)
        self.assertIn("v6_generate_queue", generate)
        self.assertNotIn("set_engine(", generate)
        self.assertNotIn("set_scheduler_armed(", generate)
        self.assertIn("require_heavy(user)", preview)
        self.assertIn("preview_only=True", preview)
        self.assertNotIn("v6_generate_queue", preview)
        self.assertNotIn("set_engine(", preview)
        self.assertIn('get("song_folder_owner")', preview)

    def test_preview_button_fetches_combined_profile_report(self):
        script = SCRIPT.read_text()
        self.assertIn("data-ae-preview-all", script)
        self.assertIn("'/actions/auto-v6/preview-all'", script)
        self.assertIn("lum-ae-bulk-preview", script)
        if shutil.which("node"):
            result = subprocess.run(["node", "--check", str(SCRIPT)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()

"""Short-only presentation regression: no profile data, defaults, or actions change."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "apps/local-upload-manager/part01"

class ShortProfileOrderOnlyTests(unittest.TestCase):
    def test_existing_controls_and_defaults_remain_in_original_template(self):
        html = (APP / "templates/app.html").read_text(encoding="utf-8")
        for marker in (
            'name="category_name"', 'name="short_vps_folder"',
            'id="short-profile-metadata-source"', 'name="drive_video_folder"',
            'name="metadata_sheet_url_or_id"', 'name="metadata_sheet_tab"',
            'name="manual_title"', 'name="manual_description"',
            'name="daily_upload_count"', 'name="schedule_times"',
            'name="after_upload_action"', 'name="work_mode"',
            'name="schedule_enabled"'):
            self.assertIn(marker, html)
        self.assertIn("('after_upload_action','delete')", (
            ROOT / "production/short_uploader_group_release.py").read_text())

    def test_script_only_moves_existing_nodes_and_shortstudio_is_required(self):
        js = (APP / "static/auto_engine_v6.js").read_text(encoding="utf-8")
        section = js.split("/* SHORT_PROFILE_REORDER_ONLY_V1",1)[1]
        self.assertIn("pathname.startsWith('/short-uploader/')", section)
        self.assertIn("panel.dataset.shortProfileOrder==='complete'", section)
        self.assertIn('nameSection.append(profileName)', section)
        self.assertIn('resourceHeading.after(mediaFolder,driveAlternative)', section)
        self.assertIn('panel.insertBefore(resources,main)', section)
        self.assertIn("MutationObserver", section)
        self.assertNotIn("cloneNode", section)
        self.assertNotIn(".value=", section)
        self.assertNotIn(".checked=", section)
        self.assertNotIn(".required=", section)
        self.assertNotIn("fetch(", section)

    def test_guarded_release_copies_only_short_js_and_cache_key(self):
        installer = (ROOT / "production/short_uploader_group_release.py").read_text()
        runtime = (ROOT / "production/runtime.py").read_text()
        workflow = (ROOT / ".github/workflows/vps1-short-studio-release.yml").read_text()
        base = (APP / "templates/base.html").read_text()
        self.assertIn('("static/auto_engine_v6.js", "templates/base.html") if reorder_only', installer)
        self.assertIn('verify_live(reorder_only=True)', installer)
        self.assertIn('SHORT_PROFILE_ORDER=PASS', installer)
        self.assertIn("profile-order=20261102-v1", base)
        self.assertIn("[vps1-shortstudio-profile-order-only]", runtime)
        self.assertIn("[vps1-shortstudio-profile-order-only]", workflow)
        self.assertIn("args.apps != ['local-upload-manager']", runtime)
        self.assertIn("idle()", installer)
        self.assertIn("SHORT_PROFILE_ORDER_RELEASE", installer)

if __name__ == "__main__":
    unittest.main()

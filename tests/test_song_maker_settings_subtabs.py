"""Regression checks for Song Maker Export/Video settings sub-tabs."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1] / "apps/song-maker/part01/songmaker/static"


class SongMakerSettingsSubtabsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "index.html").read_text(encoding="utf-8")
        cls.js = (ROOT / "app.js").read_text(encoding="utf-8")

    def test_exactly_two_settings_tabs(self):
        self.assertEqual(re.findall(r'data-settings-tab="([^"]+)"', self.html), ["export", "video"])
        for section in ("settingsExportPanel", "settingsVideoPanel"):
            self.assertIn(f'id="{section}"', self.html)
        self.assertIn('role="tablist"', self.html)
        self.assertIn("settingsTab(sessionStorage.getItem", self.js)

    def test_overlay_is_video_only_and_form_associated(self):
        export_panel = self.html.split('id="settingsExportPanel"', 1)[1].split('id="settingsVideoPanel"', 1)[0]
        video_panel = self.html.split('id="settingsVideoPanel"', 1)[1].split('</section>', 1)[0]
        self.assertNotIn('class="overlaySettings"', export_panel)
        self.assertIn('class="overlaySettings"', video_panel)
        for name in ("overlay_enabled", "overlay_preset", "overlay_strength", "overlay_speed"):
            self.assertRegex(video_panel, rf'name="{name}"[^>]*form="exportSettings"')
        self.assertIn('form="exportSettings" data-save-kind="video"', video_panel)

    def test_existing_controls_and_single_save_endpoint_preserved(self):
        for name in ("resolution", "fit", "quality", "auto_input_from_audio_downloader", "send_to_song_uploader"):
            self.assertIn(f'name="{name}"', self.html)
        self.assertIn('id="settings"', self.html)  # Admin Drive form stays independent
        self.assertIn("data.overlay_enabled=e.target.elements.overlay_enabled.checked", self.js)
        self.assertIn("api('export',{method:'POST'", self.js)
        self.assertIn("e.submitter?.dataset.saveKind==='video'", self.js)


if __name__ == "__main__":
    unittest.main()

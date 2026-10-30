"""Song Uploader must use Kokoro UI geometry without affecting other uploaders."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "apps/local-upload-manager/part01/templates/base.html"
CSS = ROOT / "apps/local-upload-manager/part01/static/song_uploader_kokoro_ui.css"


class SongUploaderKokoroParityTests(unittest.TestCase):
    def test_only_song_instance_loads_dedicated_ui(self):
        source = TEMPLATE.read_text()
        self.assertIn("{% if engine_instance == 'song' %}", source)
        self.assertIn("song_uploader_kokoro_ui.css", source)
        self.assertIn("lum-song-ui", source)
        self.assertIn("data-workspace=\"{{ 'song-uploader' if engine_instance == 'song' else 'uploader' }}\"", source)

    def test_kokoro_scale_and_mobile_behavior(self):
        css = CSS.read_text()
        self.assertIn("SONG_UPLOADER_KOKORO_PARITY_V1", css)
        self.assertIn("body.lum-song-ui", css)
        self.assertIn("max-width: 1400px", css)
        self.assertIn("min-height: 44px", css)
        self.assertIn("font-size: 13px", css)
        self.assertIn("@media (max-width: 760px)", css)
        self.assertIn("overflow-x: auto", css)
        self.assertNotIn("body.vps1-native-uploader", css)


if __name__ == "__main__":
    unittest.main()

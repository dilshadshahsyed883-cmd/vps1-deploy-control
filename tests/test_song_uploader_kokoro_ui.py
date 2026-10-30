"""Song and Short Studio uploaders share approved Kokoro geometry."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "apps/local-upload-manager/part01/templates/base.html"
CSS = ROOT / "apps/local-upload-manager/part01/static/song_uploader_kokoro_ui.css"


class SongUploaderKokoroParityTests(unittest.TestCase):
    def test_only_studio_instances_load_dedicated_ui(self):
        source = TEMPLATE.read_text()
        self.assertIn("{% if engine_instance in ['song', 'shortstudio'] %}", source)
        self.assertIn("song_uploader_kokoro_ui.css", source)
        self.assertIn("lum-song-ui", source)
        self.assertIn("data-workspace=\"{{ 'song-uploader' if engine_instance == 'song' else 'uploader' }}\"", source)

    def test_rendered_short_and_song_reuse_style_without_changing_workspace(self):
        from jinja2 import Environment
        template = Environment().from_string(TEMPLATE.read_text())
        for mode in ('song', 'shortstudio', 'engine1', 'engine2', 'shorts'):
            with self.subTest(mode=mode):
                page = template.render(engine_instance=mode, embedded_view=True,
                    base_path='/short-uploader' if mode == 'shortstudio' else '/uploader')
                styled = mode in {'song', 'shortstudio'}
                self.assertEqual('song_uploader_kokoro_ui.css' in page, styled)
                self.assertEqual('lum-song-ui' in page, styled)
                if mode == 'shortstudio':
                    self.assertIn('data-workspace="short-uploader"', page)
                    self.assertIn('<title>Short Uploader</title>', page)

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

"""Allowlisted current Song Studio shell signature for Voice UI-only release."""
import unittest
from pathlib import Path
from production.voice_template_release import (
    COPYRIGHT_STUDIO_ASSETS, SONG_STUDIO_ASSETS, NEW_ASSETS, OLD_ASSETS, PREVIOUS_ASSETS,
    patch_template, TITLE,
)
ROOT = Path(__file__).resolve().parents[1]


class SongStudioAssetsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "apps/voice-studio/part01/templates/voice_studio.html").read_bytes()

    def test_live_song_studio_signature_is_approved(self):
        self.assertIn(COPYRIGHT_STUDIO_ASSETS, self.source)
        self.assertNotIn(NEW_ASSETS, self.source)
        self.assertEqual(self.source.count(COPYRIGHT_STUDIO_ASSETS), 1)
        self.assertEqual(patch_template(self.source), self.source)

    def test_accepts_legacy_approved_and_rejects_duplicate_assets(self):
        old = self.source.replace(COPYRIGHT_STUDIO_ASSETS, NEW_ASSETS)
        self.assertEqual(patch_template(old), old)
        with self.assertRaises(RuntimeError):
            patch_template(self.source.replace(COPYRIGHT_STUDIO_ASSETS,
                                               SONG_STUDIO_ASSETS + SONG_STUDIO_ASSETS))
        with self.assertRaises(RuntimeError):
            patch_template(self.source.replace(COPYRIGHT_STUDIO_ASSETS,
                                               SONG_STUDIO_ASSETS + OLD_ASSETS))
        with self.assertRaises(RuntimeError):
            patch_template(self.source.replace(COPYRIGHT_STUDIO_ASSETS,
                                               SONG_STUDIO_ASSETS + PREVIOUS_ASSETS))

    def test_copyright_cache_version_preserves_legacy_and_rejects_mixed_assets(self):
        for assets in (SONG_STUDIO_ASSETS, NEW_ASSETS, COPYRIGHT_STUDIO_ASSETS):
            with self.subTest(assets=assets):
                body = self.source.replace(COPYRIGHT_STUDIO_ASSETS, assets)
                self.assertEqual(patch_template(body), body)
                with self.assertRaises(RuntimeError):
                    patch_template(body.replace(assets, assets + COPYRIGHT_STUDIO_ASSETS))
        with self.assertRaises(RuntimeError):
            patch_template(self.source.replace(b'copyright-nav=20261030-v3', b'copyright-nav=unreviewed'))

    def test_verified_exe_button_is_preserved_by_shell_guard(self):
        self.assertIn(b'id="vsaDesktopAppDownload"', patch_template(self.source))
        source_code = (ROOT / "production/voice_template_release.py").read_text()
        self.assertIn("source_html.count(NEW_LIMITS) == 1", source_code)
        self.assertIn("require_idle_audio_downloader()", source_code)
        self.assertIn("atomic_link(CURRENT, previous)", source_code)


if __name__ == "__main__":
    unittest.main()

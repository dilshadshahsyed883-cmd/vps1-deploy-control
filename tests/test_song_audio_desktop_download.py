"""Regression tests for the isolated Song Audio desktop EXE download link."""
import unittest
from pathlib import Path
from production.voice_template_release import (
    song_audio_desktop_download_overlay,
    SONG_AUDIO_DESKTOP_URL,
    SONG_AUDIO_DESKTOP_OPEN,
    SONG_AUDIO_DESKTOP_CLOSE,
    SONG_AUDIO_DESKTOP_STYLE,
    SONG_AUDIO_DESKTOP_BADGE,
)
ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "apps/voice-studio/part01/templates/voice_studio.html"


class SongAudioDesktopDownloadTests(unittest.TestCase):
    def bare(self, src):
        a = src.index(SONG_AUDIO_DESKTOP_OPEN)
        b = src.index(SONG_AUDIO_DESKTOP_CLOSE, a) + len(SONG_AUDIO_DESKTOP_CLOSE)
        old = src[:a] + SONG_AUDIO_DESKTOP_BADGE + src[b:]
        a = old.index(SONG_AUDIO_DESKTOP_STYLE)
        b = old.index(b"</style>", a) + len(b"</style>")
        return old[:a] + old[b:]

    def test_desktop_link_is_exact_release_and_accessible(self):
        src = SOURCE.read_bytes()
        self.assertEqual(src.count(SONG_AUDIO_DESKTOP_URL), 1)
        self.assertIn(b"SongAudioDownloader.exe", src)
        self.assertIn(b'rel="noopener noreferrer"', src)
        self.assertIn(b'aria-label="Download Song Audio Downloader desktop app for Windows"', src)

    def test_idempotent_installer_uses_only_header_and_scoped_css(self):
        src = SOURCE.read_bytes()
        bare = self.bare(src)
        output = song_audio_desktop_download_overlay(bare, src)
        self.assertEqual(song_audio_desktop_download_overlay(output, src), output)
        self.assertEqual(output.count(SONG_AUDIO_DESKTOP_URL), 1)
        self.assertEqual(output.count(SONG_AUDIO_DESKTOP_STYLE), 1)
        self.assertIn(b'id="vsaReady"', output)
        self.assertIn(b'id="vsaSongMaker"', output)
        self.assertIn(b'id="vsEngineKokoro"', output)
        self.assertIn(b'id="vsEngineEdge"', output)
        self.assertIn(b'id="vsaProxyEnabled"', output)

    def test_missing_original_status_fails_closed(self):
        src = SOURCE.read_bytes()
        bare = self.bare(src).replace(SONG_AUDIO_DESKTOP_BADGE, b"missing", 1)
        with self.assertRaises(RuntimeError):
            song_audio_desktop_download_overlay(bare, src)

    def test_guarded_voice_ui_only_install_contains_overlay(self):
        py = (ROOT / "production/voice_template_release.py").read_text()
        self.assertIn("patched_html = song_audio_desktop_download_overlay(patched_html, source_html)", py)
        workflow = (ROOT / ".github/workflows/vps1-voice-ui-only-release.yml").read_text()
        self.assertIn("PASS_VOICE_TEMPLATE_ONLY", workflow)
        self.assertIn("install $SOURCE_SHA voice-studio", workflow)


if __name__ == "__main__":
    unittest.main()

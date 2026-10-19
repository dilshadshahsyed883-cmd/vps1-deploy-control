"""Verify native Auto Stream V2 page is independent and free of secret rendering."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1] / "apps/short-stream/part01"))
from auto_stream_ui import PAGE, studio, router


class AutoStreamStudioTests(unittest.TestCase):
    def test_native_studio_contains_channels_and_sheet_tabs(self):
        self.assertIn('data-tab="channels"', PAGE)
        self.assertIn('data-tab="sheets"', PAGE)
        self.assertIn('data-tab="live"', PAGE)
        self.assertIn('data-tab="studio"', PAGE)
        self.assertIn("Create PRIVATE YouTube Broadcast", PAGE)
        self.assertIn("GO LIVE", PAGE)
        self.assertIn("ytFlags.go_live_enabled", PAGE)
        self.assertIn('Link read-only Sheet', PAGE)
        self.assertIn('OAuth configured', PAGE.lower().replace("oauth configured","OAuth configured"))
        self.assertIn('credentials:"same-origin"', PAGE)
        self.assertNotIn("<iframe", PAGE.lower())

    def test_no_credential_fields_exposed(self):
        for phrase in ('AUTO_STREAM_GOOGLE_CLIENT_SECRET', 'AUTO_STREAM_FERNET_KEY',
                       'refresh_token', 'stream_key'):
            self.assertNotIn(phrase, PAGE)

    def test_studio_route(self):
        self.assertIn("/engine/studio", [r.path for r in router.routes])
        self.assertEqual(studio().status_code, 200)


if __name__=="__main__":
    unittest.main()

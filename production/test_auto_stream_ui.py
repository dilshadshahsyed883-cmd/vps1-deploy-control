"""Verify the native Auto Stream page has exactly five primary tabs and no iframe."""
from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1] / "apps/short-stream/part01"))
from auto_stream_ui import PAGE, studio, router


class AutoStreamStudioTests(unittest.TestCase):
    def test_exact_five_primary_tabs(self):
        ids=re.findall(r'data-tab="([^"]+)"', PAGE)
        self.assertEqual(ids, ["dashboard","channels","sheets-sync","live-studio","live-queue"])
        for text in ("Dashboard","Channels","Sheets Sync","Live Studio","Live Queue"):
            self.assertIn(text, PAGE)
        self.assertIn("manual-ingest", PAGE)
        self.assertIn('credentials:"same-origin"', PAGE)
        self.assertNotIn("<iframe", PAGE.lower())
        self.assertIn('id="manual-key"', PAGE)

    def test_media_and_queue_controls_present(self):
        for text in ("Upload and checksum","Create DRAFT / queue","Preview","Sync DRAFTs",
                     "Create private OAuth broadcast","Start manual RTMPS","Verify remote",
                     "sessionStorage.setItem"):
            self.assertIn(text, PAGE)

    def test_no_credential_fields_exposed(self):
        for phrase in ("AUTO_STREAM_GOOGLE_CLIENT_SECRET", "AUTO_STREAM_FERNET_KEY",
                       "refresh_token"):
            self.assertNotIn(phrase, PAGE)

    def test_studio_route(self):
        self.assertIn("/engine/studio", [r.path for r in router.routes])
        self.assertEqual(studio().status_code, 200)


if __name__=="__main__":
    unittest.main()

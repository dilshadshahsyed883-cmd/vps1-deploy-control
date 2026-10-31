"""PR544: targeted Video Merger release must not select all applications."""
from pathlib import Path
import unittest

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/vps1-production.yml"
MARKER = "[vps1-production-release:video-merger]"

class VideoMergerIsolationReleaseTest(unittest.TestCase):
    def test_single_app_marker_is_enabled_and_selected(self):
        src = WORKFLOW.read_text(encoding="utf-8")
        self.assertEqual(src.count("contains(github.event.head_commit.message, '"+MARKER+"')"), 2)
        self.assertIn("contains(github.event.head_commit.message, '"+MARKER+"') && 'video-merger' ||", src)
        self.assertIn("APP_SELECTION:", src)
        self.assertIn("group: vps1-production-applications", src)
        self.assertIn('"sudo -n /usr/local/sbin/vps1-production-deploy $OPERATION $SOURCE_SHA $APP_SELECTION"', src)

if __name__ == "__main__":
    unittest.main()

"""Regression coverage: Drive metadata must not block a VPS Video Merger job."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "apps/video-merger/part01/app.py"

class DriveNameFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        with patch.dict(os.environ, {"VIDEO_MERGER_ROOT": str(root), "VIDEO_MERGER_PROXY_CONFIG_DIR": str(root / "proxy")}):
            spec = importlib.util.spec_from_file_location("video_merger_drive_name_test", SOURCE)
            self.app = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.app)

    def test_metadata_missing_uses_stable_drive_id(self):
        url = "https://drive.google.com/file/d/1TestDriveFileIdABCDE/view"
        with patch.object(self.app, "find_drive_item", return_value=None):
            self.assertEqual(self.app.source_display_filename({"kind": "url", "url": url}),
                             "Drive-1TestDriveFileIdABCDE.mp4")

    def test_drive_listing_error_does_not_block_naming(self):
        url = "https://drive.google.com/file/d/1TestDriveFileIdABCDE/view"
        with patch.object(self.app, "find_drive_item", side_effect=RuntimeError("Drive list unavailable")):
            self.assertEqual(self.app.source_display_filename({"kind": "url", "url": url}),
                             "Drive-1TestDriveFileIdABCDE.mp4")

    def test_valid_drive_metadata_still_wins(self):
        url = "https://drive.google.com/file/d/1TestDriveFileIdABCDE/view"
        with patch.object(self.app, "find_drive_item", return_value={"Name": "My Video.mov"}):
            self.assertEqual(self.app.source_display_filename({"kind": "url", "url": url}),
                             "My Video.mp4")

if __name__ == "__main__":
    unittest.main()

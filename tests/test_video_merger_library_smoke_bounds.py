"""Bounded Video Merger real-source Library smoke guard tests."""
import math
import unittest
from pathlib import Path
from production.video_merger_import_smoke import bounded_library_sample_seconds


class LibrarySmokeBounds(unittest.TestCase):
    def test_hour_length_legacy_video_uses_eight_second_real_clip(self):
        self.assertEqual(bounded_library_sample_seconds(2*3600),8)
        self.assertEqual(bounded_library_sample_seconds(10),8)

    def test_short_real_media_still_has_positive_trim_length(self):
        self.assertEqual(bounded_library_sample_seconds(2.1),1)
        self.assertEqual(bounded_library_sample_seconds(4),3)

    def test_missing_or_invalid_media_fails_closed(self):
        for duration in (0,1.5,-1,math.nan,math.inf,None,"200"):
            with self.subTest(duration=duration):
                with self.assertRaises(ValueError):
                    bounded_library_sample_seconds(duration)

    def test_smoke_requires_vps_only_and_optional_copy_before_pass(self):
        smoke=(Path(__file__).resolve().parents[1]/
               "production/video_merger_import_smoke.py").read_text()
        self.assertIn("results['library_vps_only']=='PASS'",smoke)
        self.assertIn("results['library_optional_drive']=='PASS'",smoke)
        self.assertIn("results['library_persistence']='PASS'",smoke)
        self.assertIn("app.process_library_import(item_id",smoke)
        self.assertIn("results['drive']=='PASS'",smoke)
        self.assertIn("library_error_class",smoke)


if __name__=="__main__":
    unittest.main()

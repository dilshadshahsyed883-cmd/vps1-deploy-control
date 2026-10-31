"""Regression for the duplicate Short Uploader introductory line that blocked guarded UI release."""
import unittest
from pathlib import Path


TEMPLATE = (Path(__file__).resolve().parents[1] /
            "apps/local-upload-manager/part01/templates/app.html")


class ShortUploaderAdminUIIntroTests(unittest.TestCase):
    def test_short_uploader_intro_uses_current_single_description(self):
        source = TEMPLATE.read_text(encoding="utf-8")
        self.assertNotIn("Group profiles, sources and upload queue.", source)
        self.assertIn(
            "{% if engine_instance == 'shortstudio' %}Manage group schedules and uploads.",
            source,
        )
        self.assertIn("Short Uploader · Auto Engine", source)


if __name__ == "__main__":
    unittest.main()

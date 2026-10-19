"""Protect shared website UX in the already-required offline-pilot CI."""
import unittest

from production.ui_design_contract import read_sources, violations


class UnifiedUiGovernanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sources = read_sources()

    def test_current_shared_shell_and_apps_match_approved_source_contract(self):
        self.assertEqual([], violations(self.sources))

    def test_sidebar_width_drift_is_blocked(self):
        modified = dict(self.sources)
        modified["shell_css"] = modified["shell_css"].replace(
            "--vps1-sidebar-width:214px", "--vps1-sidebar-width:240px")
        issues = violations(modified)
        self.assertTrue(any("214px" in message for message in issues), issues)

    def test_video_subtab_height_drift_is_blocked(self):
        modified = dict(self.sources)
        modified["geometry_css"] = modified["geometry_css"].replace(
            "min-height: 62px !important", "min-height: 80px !important")
        issues = violations(modified)
        self.assertTrue(any("tab height" in message for message in issues), issues)

    def test_upload_studio_duplicate_group_regression_is_blocked(self):
        modified = dict(self.sources)
        modified["shell_js"] = modified["shell_js"].replace(
            'addLink(navigation, "Upload Studio"',
            'addLink(navigation, "Auto Uploader"')
        issues = violations(modified)
        self.assertTrue(any("combined Upload Studio" in message for message in issues), issues)

    def test_unversioned_or_missing_video_geometry_is_blocked(self):
        modified = dict(self.sources)
        modified["short_maker"] = modified["short_maker"].replace(
            "vps1_video_studio_geometry.css?v=20261020-geometry-v1",
            "vps1_video_studio_geometry.css")
        issues = violations(modified)
        self.assertTrue(any("short_maker" in message for message in issues), issues)

    def test_future_html_document_must_reserve_native_shell(self):
        modified = dict(self.sources)
        modified["shell_css"] = modified["shell_css"].replace(
            'html:has(script[data-workspace="uploader"]) body',
            'html:has(script[data-workspace="uploader"]) .never-reserved')
        issues = violations(modified)
        self.assertTrue(any("left gutter" in message for message in issues), issues)


if __name__ == "__main__":
    unittest.main()

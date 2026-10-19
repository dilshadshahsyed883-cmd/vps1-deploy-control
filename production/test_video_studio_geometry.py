"""Video Studio's four independently served pages share one top-row grid."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCENE = ROOT / "apps/scene-builder/part01"
ACS = ROOT / "apps/auto-caption-studio/part01/web_backend/acs_studio.html"
MAKER = ROOT / "apps/short-maker/part01/app.py"
MERGER = ROOT / "apps/video-merger/part01/templates/index.html"
CSS = SCENE / "static/vps1_video_studio_geometry.css"
ASSET = "vps1_video_studio_geometry.css?v=20261020-geometry-v1"


class VideoStudioGeometryContracts(unittest.TestCase):
    def test_four_apps_load_one_shared_geometry_css(self):
        scene = (SCENE / "templates/base.html").read_text()
        acs = ACS.read_text()
        maker = MAKER.read_text()
        merger = MERGER.read_text()
        for path, source in (("scene", scene), ("acs", acs),
                             ("maker", maker), ("merger", merger)):
            with self.subTest(app=path):
                self.assertEqual(source.count(ASSET), 1)
                self.assertIn('data-video-studio-subnav="1"' if path != "acs"
                              else 'data-workspace="acs"', source)
        for page in (scene, acs, maker, merger):
            self.assertLess(page.index("vps1_native_site_shell.css"),
                            page.index(ASSET))
        # Merger/Maker's <style> originally overrode app-level shared
        # navigation. The geometry stylesheet must be linked after it.
        for page in (maker, merger):
            self.assertLess(page.index("</style>"), page.index(ASSET))
            self.assertLess(page.index(ASSET), page.index("</head>"))

    def test_canonical_four_tab_navigation_and_no_legacy_alias(self):
        for path in (SCENE / "templates/base.html", MAKER, MERGER):
            source = path.read_text()
            for route in ("/scene-builder/", "/auto-caption-studio/",
                          "/short-maker/", "/video-merger/"):
                self.assertIn('href="' + route + '"', source)
            self.assertNotIn('href="/auto-caption/studio"', source)

    def test_horizontal_and_vertical_baseline_owned_by_shared_css(self):
        css = CSS.read_text()
        for marker in ("--vps1-studio-top: 26px", "--vps1-studio-gutter: 34px",
                       "--vps1-studio-max: 1400px",
                       'body.vps1-native-acs > .vps1-native-video-tabs',
                       'body:has(> main > [data-video-studio-subnav="1"]) > main',
                       "flex-wrap: nowrap !important",
                       "overflow-x: auto !important",
                       "min-height: 62px !important",
                       "@media(max-width:760px)",
                       "--vps1-studio-top: 72px",
                       "--vps1-studio-gutter: 14px"):
            self.assertIn(marker, css)
        # Preserve independent app logic and API routes; only CSS & shell.
        self.assertNotIn("fetch(", css)
        self.assertNotIn("iframe", css.lower())

    def test_short_maker_mobile_nav_does_not_disappear(self):
        source = MAKER.read_text()
        self.assertIn('id="app-sidebar" class="sidebar"', source)
        self.assertIn('class="vps1-video-mobile-menu"', source)
        self.assertIn('class="vps1-video-mobile-overlay" hidden', source)
        self.assertIn('sidebar.classList.toggle("mobile-open",opened)', source)


    def test_video_geometry_release_only_reinstalls_four_apps(self):
        source = (ROOT / ".github/workflows/vps1-native-studio-release.yml").read_text()
        self.assertIn("[vps1-video-geometry-release]", source)
        self.assertIn('VIDEO_GEOMETRY_ONLY: ${{ contains(', source)
        self.assertIn('if [[ "$VIDEO_GEOMETRY_ONLY" == "true" ]]; then', source)
        self.assertIn(
            "for app in scene-builder short-maker video-merger auto-caption-studio; do",
            source)
        self.assertIn("VIDEO_STUDIO_GEOMETRY_RELEASE=PASS", source)
        self.assertIn("AUTO_SCRIPT=OFFLINE", source)
        # Existing original broader release path remains available separately.
        self.assertIn(
            "for app in short-maker video-merger auto-caption-studio "
            "voice-studio local-upload-manager; do", source)


if __name__ == "__main__":
    unittest.main()

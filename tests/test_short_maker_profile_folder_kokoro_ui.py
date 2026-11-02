"""UI-only regression gate for Short Maker Profiles and Folders Kokoro parity.

Authenticated browser screenshot and live deployment checks are separate gates.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAKER = ROOT / "apps/short-maker/part01/app.py"
CLIENT = ROOT / "apps/short-maker/part01/short_profile_client.js"
MARKER = "SHORT_MAKER_PROFILE_FOLDER_KOKORO_UI_V1"


class ShortMakerProfileFolderKokoroUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree = ast.parse(MAKER.read_text(encoding="utf-8"))
        cls.html = next(
            item.value.value for item in tree.body
            if isinstance(item, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "HTML"
                    for target in item.targets)
            and isinstance(item.value, ast.Constant)
        )
        assert cls.html.count(MARKER) == 1
        cls.css = cls.html.split("/* " + MARKER, 1)[1].split("</style>", 1)[0]

    def test_existing_shell_geometry_is_single_owner(self):
        self.assertIn("max-width:var(--vps1-studio-max,1400px)", self.css)
        self.assertIn("#shortProfilesPanel,#foldersPanel", self.css)
        for forbidden in ("#makerPanel", "#queuePanel", "#settingsPanel",
                          ".sidebar{", "main{", ".sm-tabs{"):
            self.assertNotIn(forbidden, self.css)

    def test_1440_viewport_and_responsive_breakpoints(self):
        for fragment in ("@media(max-width:1100px)",
                         "@media(max-width:760px)", "@media(max-width:390px)",
                         "grid-template-columns:repeat(2,minmax(0,1fr))",
                         "#foldersPanel[hidden]{display:none;}",
                         "min-width:0", "min-height:44px"):
            self.assertIn(fragment, self.css)

    def test_accessibility_and_kokoro_controls(self):
        for fragment in ("#shortProfilesPanel :is(button,input,select,a):focus-visible",
                         "#foldersPanel :is(button,input,select,a):focus-visible",
                         "outline:3px solid #8174eb",
                         "@media(prefers-reduced-motion:reduce)",
                         "#foldersPanel .queue-table-wrap",
                         "#shortProfilesPanel #shortProfilesRows .song-item"):
            self.assertIn(fragment, self.css)
        self.assertNotIn("pointer-events:none", self.css)

    def test_all_profile_and_folder_action_hooks_are_intact(self):
        for item in (
            "shortProfilesPanel", "shortProfileName", "shortProfileSource",
            "shortProfileSourceFallback", "shortProfileDestination",
            "shortProfileDailyCount", "shortProfileSchedule",
            "shortProfileSave", "shortProfileSync", "shortProfilePauseImport",
            "shortProfileResumeImport", "shortProfileCancelImport",
            "shortProfileRetryImport", "shortProfileNew", "shortProfilesRows",
            "foldersPanel", "foldersVpsDestination", "foldersCreateVps",
            "foldersVideoInput", "foldersStartUpload", "foldersPauseUpload",
            "foldersResumeUpload", "foldersCancelUpload", "foldersRetryUpload",
            "foldersClearFiles", "foldersUploadProgress", "foldersInventoryRows",
        ):
            self.assertIn('id="' + item + '"', self.html)
        for label in ("shortProfileName", "shortProfileSource", "shortProfileDestination",
                      "shortProfileDailyCount", "shortProfileSchedule",
                      "foldersVpsDestination", "foldersVideoInput"):
            self.assertIn('for="' + label + '"', self.html)
        self.assertIn("api/short-profiles", CLIENT.read_text(encoding="utf-8"))
        self.assertIn("api/vps-folders/verified-inventory", self.html)

    def test_css_only_not_an_api_or_feature_activation(self):
        for forbidden in ("<script", "fetch(", "localStorage", "sessionStorage",
                          "SHORT_MAKER_DAILY_PROFILES_ENABLED=1"):
            self.assertNotIn(forbidden, self.css)
        self.assertIn('id="shortProfileSave" disabled', self.html)
        self.assertIn('id="foldersStartUpload" type="button" disabled', self.html)
        self.assertIn("Auto Script stays OFFLINE", self.html)


if __name__ == "__main__":
    unittest.main()

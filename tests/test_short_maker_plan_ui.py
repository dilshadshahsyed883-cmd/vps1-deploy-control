"""Regression gates for the frozen Short Maker tab order and editable PC-folder suggestion.

This deliberately tests a staged PR. Feature APIs, media imports and scheduling
must pass separate integration gates before the Short Maker release can ship.
"""
from __future__ import annotations

import ast
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAKER = ROOT / "apps/short-maker/part01/app.py"


class ShortMakerLockedPlanUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = MAKER.read_text(encoding="utf-8")
        tree = ast.parse(cls.source)
        cls.html = next(node.value.value for node in tree.body if isinstance(node, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == "HTML" for t in node.targets)
                        and isinstance(node.value, ast.Constant))

    def test_approved_tab_order_is_preserved(self):
        tags = re.findall(r'<button id="(makerTabBtn|shortProfilesTabBtn|foldersTabBtn|queueTabBtn|settingsTabBtn)"', self.html)
        self.assertEqual(tags, ["makerTabBtn", "shortProfilesTabBtn", "foldersTabBtn",
                                "queueTabBtn", "settingsTabBtn"])
        for ident in ("shortProfilesPanel", "foldersPanel", "queuePanel", "settingsPanel"):
            self.assertIn(ident, self.html)

    def test_no_unsafe_early_release_of_incomplete_features(self):
        self.assertIn('id="shortProfileSave" disabled', self.html)
        self.assertIn('id="foldersStartUpload" type="button" disabled', self.html)
        self.assertIn("The existing empty-only delete API is not used for this workflow.", self.html)
        self.assertIn("Auto Script stays OFFLINE", self.html)
        self.assertIn("fetch('api/vps-folders'", self.html)
        self.assertIn('await loadOwnerFolderNames()', self.html)

    def test_editable_profile_name_suggestion_cases(self):
        if not shutil.which("node"):
            self.skipTest("Node.js required for the extracted browser function test")
        found = re.search(r'function shortProfileNameSuggestion\(.*?\n\}', self.html, re.S)
        self.assertIsNotNone(found, "Pure suggestion helper is missing")
        cases = [
            ("", "", "Daily Facts/voice.mp3", "Daily Facts"),
            ("Daily Facts", "Daily Facts", "New Folder/a.jpg", "New Folder"),
            ("Custom", "Daily Facts", "New Folder/a.jpg", "Custom"),
            ("", "", "پاکستانی شارٹس/1.jpg", "پاکستانی شارٹس"),
            ("Existing", "", "", "Existing"),
            ("Manual Name", "Old Folder", "Different/p.jpg", "Manual Name"),
        ]
        script = found.group(0) + "\n" + "\n".join(
            "if(shortProfileNameSuggestion(%r,%r,%r).name!==%r)throw Error('case %d');"
            % (*case, index) for index, case in enumerate(cases)
        )
        proc = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_existing_maker_pipeline_was_not_replaced(self):
        for tag in ('id="makerPanel"', 'id="images"', 'id="audio"', 'id="render"',
                    'id="queueRows"', 'id="settingsPanel"', 'id="shortVpsFolder"'):
            self.assertIn(tag, self.html)


if __name__ == "__main__":
    unittest.main()

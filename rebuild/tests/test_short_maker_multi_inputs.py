"""Offline regression checks for Short Maker's multi-folder import contract.

Extract only pure helpers to avoid importing FastAPI or touching production data.
"""
import ast
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[2] / "apps/short-maker/part01/app.py"
FUNCTIONS = {
    "normalize_drive_path",
    "parse_drive_folder_input",
    "parse_drive_folder_inputs",
    "import_drive_images",
}


def load_helpers():
    source = SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    picked = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS]
    if len(picked) != len(FUNCTIONS):
        raise AssertionError("Expected Drive helper functions are missing")
    namespace = {
        "json": json,
        "re": re,
        "shutil": shutil,
        "Path": Path,
        "safe_name": lambda name: Path(name).name,
        "set_state": lambda *args, **kwargs: None,
    }
    isolated = ast.Module(body=picked, type_ignores=[])
    exec(compile(isolated, str(SOURCE), "exec"), namespace)
    return namespace, source


class ShortMakerMultiInputTests(unittest.TestCase):
    def setUp(self):
        self.helpers, self.source = load_helpers()

    def test_single_folder_backwards_compatible(self):
        fn = self.helpers["parse_drive_folder_inputs"]
        self.assertEqual(fn("gdrive:Short_Maker/Input"), ["gdrive:Short_Maker/Input"])
        self.assertEqual(fn("https://drive.google.com/drive/folders/abcd1234"),
                         ["https://drive.google.com/drive/folders/abcd1234"])

    def test_multiple_links_remain_in_user_order(self):
        links = [
            "https://drive.google.com/drive/folders/folderA123",
            "gdrive:Images/2026",
            "https://drive.google.com/drive/u/1/folders/folderB456",
        ]
        self.assertEqual(self.helpers["parse_drive_folder_inputs"](
            paths_json=json.dumps(links)), links)

    def test_bad_or_duplicate_folders_are_rejected(self):
        fn = self.helpers["parse_drive_folder_inputs"]
        for value in ("[]", "{}", '["not a link"]', '"not-a-list"',
                      json.dumps(["gdrive:one"] * 2),
                      json.dumps(["https://drive.google.com/drive/folders/abcDEF",
                                  "https://drive.google.com/drive/u/1/folders/abcDEF"]),
                      json.dumps(["gdrive:one"] * 51)):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                fn(paths_json=value)

    def test_import_from_two_folders_with_same_image_names(self):
        namespace = self.helpers
        copies = []
        def fake_rclone(args, **kwargs):
            copies.append(args)
            target = Path(args[2])
            (target / "same-name.jpg").write_bytes(b"test image")
            (target / "ignore.txt").write_text("not image", encoding="utf-8")
        namespace["rclone_run"] = fake_rclone
        namespace["is_image"] = lambda path: path.suffix == ".jpg"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            results = namespace["import_drive_images"](
                "dummy-job", ["gdrive:one", "gdrive:two"], root)
            self.assertEqual(len(copies), 2)
            self.assertEqual([Path(p).name for p in results],
                             ["00001_same-name.jpg", "00002_same-name.jpg"])
            self.assertTrue(all(Path(p).exists() for p in results))
            self.assertFalse((root / "drive_input").exists())

    def test_user_interface_has_additive_song_and_link_controls(self):
        self.assertIn('id="addDriveLink"', self.source)
        self.assertIn('id="addAudioBtn"', self.source)
        self.assertIn("const selectedAudioFiles=[];", self.source)
        self.assertIn("selectedAudioFiles.forEach(f=>fd.append('audio'", self.source)
        self.assertIn("fd.append('drive_input_paths_json'", self.source)


if __name__ == "__main__":
    unittest.main()

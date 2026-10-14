import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from build_pilot import build
from validate_release import validate


class ReleaseValidationTests(unittest.TestCase):
    def test_valid_reproducible_pilot(self):
        manifest = build()
        digest1 = validate(manifest)
        digest2 = validate(build())
        self.assertEqual(digest1, digest2)

    def test_unknown_app_rejected(self):
        source = build()
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            bad = json.loads(source.read_text())
            bad["app_id"] = "omnivoice"  # protect production app
            (root / "release.json").write_text(json.dumps(bad))
            with self.assertRaises(ValueError):
                validate(root / "release.json")

    def test_traversal_rejected(self):
        source = build()
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            bad = json.loads(source.read_text())
            bad["artifact"] = "../pilot-site.tar.gz"
            (root / "release.json").write_text(json.dumps(bad))
            with self.assertRaises(ValueError):
                validate(root / "release.json")

    def test_bad_sha_rejected(self):
        source = build()
        bad = json.loads(source.read_text())
        bad["sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            (root / "release.json").write_text(json.dumps(bad))
            (root / bad["artifact"]).write_bytes((source.parent / bad["artifact"]).read_bytes())
            with self.assertRaisesRegex(ValueError, "sha256 mismatch"):
                validate(root / "release.json")


if __name__ == "__main__":
    unittest.main()

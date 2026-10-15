"""Focused tests for Auto Script's isolated 11th source release."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from rebuild import stage_auto_script

FILES = {
    "auto_script.py": b"def demo():\n    return True\n",
    "templates/auto_script.html": b"<h1>staging-only</h1>",
    "static/auto_script.js": b"console.log('staging');",
    "static/auto_script.css": b"body { color: black; }",
}


class IsolatedAutoScriptTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fresh-autoscript-test-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        for name, content in FILES.items():
            dest = self.source / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content)
        (self.source / "SOURCE_SHA256.json").write_text(json.dumps({
            "files": [{"path": name, "bytes": len(value),
                       "sha256": hashlib.sha256(value).hexdigest()}
                      for name, value in FILES.items()]
        }))
        self.root = self.base / "fresh-root"

    def test_stage_verify_isolated_rollback(self):
        first = "a" * 40
        second = "b" * 40
        a = stage_auto_script.stage(self.root, first, self.source)
        self.assertEqual(a["source"], "PASS")
        self.assertEqual(a["cutover"], "DISABLED")
        self.assertEqual(stage_auto_script.verify(self.root, first, self.source)["source"], "PASS")
        self.assertEqual(stage_auto_script.stage(self.root, second, self.source)["release"], second)
        self.assertEqual(stage_auto_script.rollback(self.root)["restored_revision"], first)
        self.assertEqual(
            (self.root / "srv/fresh-website/current/auto-script").resolve().name, first)
        self.assertFalse((self.root / "etc/systemd/system").exists())

    def test_tampering_fails_closed(self):
        (self.source / "auto_script.py").write_text("corrupted")
        with self.assertRaises(ValueError):
            stage_auto_script.stage(self.root, "c" * 40, self.source)

    def test_non_temp_roots_rejected(self):
        with self.assertRaises(ValueError):
            stage_auto_script.stage("/var/lib/fresh-website", "d" * 40, self.source)


if __name__ == "__main__":
    unittest.main()

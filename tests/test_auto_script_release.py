import importlib.util
import json
import pathlib
import tarfile
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("auto_release", ROOT / "tools" / "build_auto_script_release.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


class AutoScriptReleaseTests(unittest.TestCase):
    def make_sources(self, directory):
        root = pathlib.Path(directory) / "source"
        for name in release.FILES:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("def health(): return True\n" if name.endswith(".py") else "safe source\n")
        return root

    def test_missing_source_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = self.make_sources(tmp)
            (src / "auto_script.py").unlink()
            with self.assertRaises(ValueError):
                release.build(src, pathlib.Path(tmp) / "dist")

    def test_hardcoded_secret_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = self.make_sources(tmp)
            (src / "auto_script.py").write_text('api_key = "exposed-token"\n')
            with self.assertRaises(ValueError):
                release.build(src, pathlib.Path(tmp) / "dist")

    def test_deterministic_safe_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = self.make_sources(tmp)
            out = pathlib.Path(tmp) / "dist"
            first = release.build(src, out)
            second = release.build(src, out)
            self.assertEqual(first, second)
            m = json.loads((out / "release.json").read_text())
            self.assertEqual(m["app_id"], "auto-script")
            self.assertEqual(m["sha256"], first)
            with tarfile.open(out / "auto-script.tar.gz") as tar:
                paths = [m.name for m in tar.getmembers()]
            self.assertEqual(
                paths,
                ["auto-script/" + name for name in release.FILES] + ["auto-script/release-files.json"],
            )

    def test_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = self.make_sources(tmp)
            f = src / "auto_script.py"
            backup = src / "other.py"
            f.rename(backup)
            f.symlink_to(backup)
            with self.assertRaises(ValueError):
                release.validate_source(src)


if __name__ == "__main__":
    unittest.main()

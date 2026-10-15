import ast
import json
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "apps" / "kokoro"


class KokoroSnapshotTests(unittest.TestCase):
    def test_snapshot_contains_only_source_allowlist(self):
        expected = {
            "README.md",
            "SOURCE_MANIFEST.txt",
            "kokoro_api.py",
            "kokoro_worker.py",
            "voice_catalog.json",
        }
        self.assertEqual({p.name for p in APP.iterdir()}, expected)
        self.assertTrue(all(p.is_file() and not p.is_symlink() for p in APP.iterdir()))

    def test_python_sources_parse(self):
        for name in ("kokoro_api.py", "kokoro_worker.py"):
            path = APP / name
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    def test_voice_catalog_is_structured(self):
        catalog = json.loads((APP / "voice_catalog.json").read_text(encoding="utf-8"))
        self.assertIsInstance(catalog, list)
        self.assertTrue(catalog)
        self.assertTrue(all(isinstance(v, dict) and v.get("id") and v.get("language") for v in catalog))

    def test_queue_database_remains_app_local(self):
        for name in ("kokoro_api.py", "kokoro_worker.py"):
            source = (APP / name).read_text(encoding="utf-8")
            self.assertIn("kokoro_jobs.sqlite3", source)
        api = (APP / "kokoro_api.py").read_text(encoding="utf-8")
        worker = (APP / "kokoro_worker.py").read_text(encoding="utf-8")
        self.assertIn("sqlite3.connect", api)
        self.assertIn("sqlite3.connect", worker)
        self.assertNotIn("edge_jobs.sqlite3", api + worker)
        self.assertNotIn("jobs.sqlite3", api.replace("kokoro_jobs.sqlite3", "") + worker.replace("kokoro_jobs.sqlite3", ""))

    def test_kokoro_processing_engine_is_preserved(self):
        worker = (APP / "kokoro_worker.py").read_text(encoding="utf-8")
        self.assertIn("from kokoro_onnx import Kokoro", worker)
        self.assertIn("engine.create(", worker)


if __name__ == "__main__":
    unittest.main()

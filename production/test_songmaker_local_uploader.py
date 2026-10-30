"""Offline local Song Maker -> Song Uploader import contract; never calls Drive or YouTube."""
import importlib.util
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "apps/local-upload-manager/part01/lum_engine"
JOB = "a" * 32


class LocalSongMakerImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        package = types.ModuleType("isolated_songmaker_test")
        package.__path__ = [str(SOURCE)]
        fake_v6 = types.ModuleType("isolated_songmaker_test.auto_engine_v6")
        fake_v6.STAGING_ROOT = root / "staging"
        fake_v6.V6Store = object
        fake_v6._image_dimensions = lambda _path: (1280, 720)
        fake_v6.normalize_filename = lambda name: name.lower()
        fake_v6.safe_slug = lambda val, _fallback: val.replace(":", "-").lower()
        fake_v6.script_key_from_filename = lambda name: Path(name).stem.lower()
        sys.modules[package.__name__] = package
        sys.modules[fake_v6.__name__] = fake_v6
        self.addCleanup(sys.modules.pop, package.__name__, None)
        self.addCleanup(sys.modules.pop, fake_v6.__name__, None)
        name = "isolated_songmaker_test.songmaker_local"
        spec = importlib.util.spec_from_file_location(name, SOURCE / "songmaker_local.py")
        self.module = importlib.util.module_from_spec(spec)
        sys.modules[name] = self.module
        self.addCleanup(sys.modules.pop, name, None)
        spec.loader.exec_module(self.module)
        self.addCleanup(patch.dict(os.environ, {"LUM_SUITE_AUTH": "1"}).stop)
        env = patch.dict(os.environ, {"LUM_SUITE_AUTH": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.database = root / "local.sqlite3"
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE drive_videos(
                    id INTEGER PRIMARY KEY,profile_id INTEGER,drive_file_id TEXT,
                    filename TEXT,normalized_filename TEXT,script_key TEXT,
                    size_bytes INTEGER,source_folder_id TEXT,present INTEGER,
                    UNIQUE(profile_id,drive_file_id));
                CREATE TABLE drive_thumbnails(
                    id INTEGER PRIMARY KEY,profile_id INTEGER,drive_file_id TEXT,
                    filename TEXT,normalized_filename TEXT,size_bytes INTEGER,
                    source_folder_id TEXT,present INTEGER,
                    UNIQUE(profile_id,drive_file_id));
                CREATE TABLE profile_sources(
                    profile_id INTEGER PRIMARY KEY,local_songmaker_enabled INTEGER DEFAULT 0,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE job_sources(profile_id INTEGER,source_folder_id TEXT,state TEXT);
            """)
        class Store:
            def __init__(self, test):
                self.test = test
            def is_channel_profile(self, profile_id):
                return profile_id == 3
            def connect(self):
                return self.test.connect()
        self.store = Store(self)
        self.calls = []

        def fake_get(owner, path, target=None, maximum=1048576):
            self.calls.append((owner, path, maximum))
            if target is None:
                return {"outputs": [{"id": JOB, "name": "youtube123.mp4", "size": 9}]}
            target.write_bytes(b"123456789" if path.endswith("/video") else b"JPEG")
            return target.stat().st_size
        patcher = patch.object(self.module, "_get", side_effect=fake_get)
        patcher.start()
        self.addCleanup(patcher.stop)
        space = patch.object(self.module.shutil, "disk_usage",
                             return_value=types.SimpleNamespace(free=20_000_000_000))
        space.start()
        self.addCleanup(space.stop)

    def connect(self):
        conn = sqlite3.connect(self.database)
        conn.row_factory = sqlite3.Row
        return conn

    def test_import_uses_owner_scoped_local_media_and_is_idempotent(self):
        first = self.module.import_job(self.store, 3, "alice", JOB)
        self.assertEqual(first["status"], "imported")
        self.assertEqual(len(self.calls), 3)
        self.assertTrue(all(owner == "alice" for owner, _, _ in self.calls))
        with self.connect() as con:
            row = con.execute("SELECT * FROM drive_videos").fetchone()
            thumb = con.execute("SELECT * FROM drive_thumbnails").fetchone()
            source = con.execute("SELECT * FROM profile_sources").fetchone()
            self.assertEqual(row["source_folder_id"], "songmaker-local")
            self.assertEqual(thumb["filename"], "youtube123.jpg")
            self.assertEqual(source["local_songmaker_enabled"], 1)
        self.assertEqual(self.module.import_job(self.store, 3, "alice", JOB)["status"], "already_imported")
        self.assertEqual(len(self.calls), 4)  # catalog auth only; media not fetched twice
        self.assertTrue((self.module.STAGING_ROOT / "profile_0003" /
                         ("drive_songmaker-" + JOB) / "youtube123.mp4").is_file())

    def test_owner_invalid_or_profile_not_bound_fails_before_import(self):
        for profile, user, token in [(2, "alice", JOB), (3, "", JOB), (3, "a\\rb", JOB),
                                      (3, "alice", "not-a-job")]:
            with self.assertRaises(ValueError):
                self.module.import_job(self.store, profile, user, token)
        self.assertFalse(self.calls)

    def test_existing_drive_jobs_block_source_switch(self):
        with self.connect() as conn:
            conn.execute("INSERT INTO job_sources VALUES(3,'real-drive-folder','QUEUED')")
        with self.assertRaisesRegex(ValueError, "Finish existing Drive-source jobs"):
            self.module.import_job(self.store, 3, "alice", JOB)
        with self.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM drive_videos").fetchone()[0], 0)

    def test_schema_and_worker_never_fallback_to_drive_for_local_inputs(self):
        engine = (SOURCE / "auto_engine_v6.py").read_text()
        worker = (SOURCE / "auto_engine_v6_worker.py").read_text()
        web = (SOURCE / "standalone_app.py").read_text()
        self.assertIn("local_songmaker_enabled", engine)
        self.assertIn('if folder=="songmaker-local":', engine)
        self.assertIn('if clean(source.get("source_folder_id"))=="songmaker-local":', worker)
        self.assertIn("SONG_MAKER_LOCAL_ORIGINAL_RETAINED", worker)
        self.assertIn("require_csrf(request, csrf_token)", web)
        self.assertIn('os.getenv("LUM_ENGINE_INSTANCE") != "song"', web)


if __name__ == "__main__":
    unittest.main()

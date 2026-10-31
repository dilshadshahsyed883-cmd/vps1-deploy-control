"""Fail-closed fixtures for Song and Short data ownership migration."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from production.studio_haseeb1_assignment import (
    MigrationBlocked, assign_core, assign_projects, inspect_core,
    check_scene_account, read_only_audit,
)


def core_fixture(path):
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE channels(id INTEGER PRIMARY KEY,channel_id TEXT);
            CREATE TABLE channel_groups(id INTEGER PRIMARY KEY,group_name TEXT);
            CREATE TABLE channel_group_members(group_id INTEGER,channel_db_id INTEGER);
            CREATE TABLE sheet_sources(id INTEGER PRIMARY KEY);
            CREATE TABLE auto_engine_profiles(id INTEGER PRIMARY KEY);
            CREATE TABLE videos(id INTEGER PRIMARY KEY,status TEXT);
            INSERT INTO channels VALUES(1,'UC1');
            INSERT INTO channels VALUES(2,'UC2');
            INSERT INTO channel_groups VALUES(4,'Group A');
            INSERT INTO channel_group_members VALUES(4,1);
            INSERT INTO sheet_sources VALUES(7);
            INSERT INTO auto_engine_profiles VALUES(8);
            INSERT INTO videos VALUES(9,'QUEUED');
        """)


def projects_fixture(path):
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE projects(id INTEGER PRIMARY KEY,owner_username TEXT NOT NULL,
                                  status TEXT,channel_db_id INTEGER);
            INSERT INTO projects VALUES(1,'','QUEUED',1);
        """)


class Haseeb1Assignment(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.scene = self.root / "scene-builder/web/web.sqlite3"
        self.scene.parent.mkdir(parents=True)
        with sqlite3.connect(self.scene) as con:
            con.execute("CREATE TABLE users(username TEXT,is_active INTEGER,studio_access TEXT,is_admin INTEGER)")
            con.execute("INSERT INTO users VALUES('haseeb1',1,'song,short',0)")
        for leaf in ("lum-song","lum-shortstudio"):
            parent = self.root / "local-upload-manager" / leaf
            (parent / "core/data").mkdir(parents=True)
            core_fixture(parent / "core/data/uploader.db")
            projects_fixture(parent / "projects.sqlite3")

    def test_account_and_readonly_inventory(self):
        self.assertTrue(check_scene_account(self.scene)["active"])
        report = read_only_audit(self.root, self.scene)
        self.assertEqual(report["instances"]["song"]["entities"]["channel"]["to_assign"],2)
        self.assertEqual(report["instances"]["short"]["entities"]["profile"]["to_assign"],1)

    def test_assign_every_legacy_entity_atomically_and_idempotently(self):
        path = self.root / "local-upload-manager/lum-song/core/data/uploader.db"
        with sqlite3.connect(path) as db:
            db.execute("BEGIN IMMEDIATE")
            first = assign_core(db)
            self.assertEqual(first["channel"]["to_assign"],2)
            db.commit()
            self.assertEqual(assign_core(db)["channel"]["to_assign"],0)
            self.assertEqual(inspect_core(db)["group"]["already_owned"],1)

    def test_preserve_foreign_owned_records(self):
        path = self.root / "local-upload-manager/lum-song/core/data/uploader.db"
        with sqlite3.connect(path) as db:
            db.executescript("""CREATE TABLE studio_entity_owners(
                entity_kind TEXT,entity_id INTEGER,owner_username TEXT,
                PRIMARY KEY(entity_kind,entity_id));
                INSERT INTO studio_entity_owners VALUES('channel',1,'alice');""")
            with self.assertRaisesRegex(MigrationBlocked,"FOREIGN_OWNER"):
                assign_core(db)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM studio_entity_owners").fetchone()[0],1)

    def test_busy_worker_and_missing_auth_block(self):
        path = self.root / "local-upload-manager/lum-shortstudio/core/data/uploader.db"
        with sqlite3.connect(path) as db:
            db.execute("UPDATE videos SET status='UPLOADING'")
            with self.assertRaisesRegex(MigrationBlocked,"ACTIVE_UPLOAD"):
                assign_core(db)
        with sqlite3.connect(self.scene) as db:
            db.execute("UPDATE users SET is_active=0")
        with self.assertRaisesRegex(MigrationBlocked,"TARGET_NOT_ACTIVE"):
            check_scene_account(self.scene)

    def test_projects_assign_only_unassigned(self):
        path = self.root / "local-upload-manager/lum-shortstudio/projects.sqlite3"
        with sqlite3.connect(path) as db:
            count = assign_projects(db)
            self.assertEqual(count["to_assign"],1)
            self.assertEqual(assign_projects(db)["to_assign"],0)
            db.execute("INSERT INTO projects VALUES(2,'alice','QUEUED',2)")
            with self.assertRaisesRegex(MigrationBlocked,"SEPARATE_TRANSFER"):
                assign_projects(db)

    def test_does_not_touch_copyright_app(self):
        self.assertFalse((self.root / "copyright-uploader").exists())
        self.assertEqual(set(read_only_audit(self.root,self.scene)["instances"]),{"song","short"})



    def test_related_studio_inventory_is_read_only(self):
        from production.studio_haseeb1_assignment import inspect_related_stores
        song = self.root / "song-maker/state"
        song.mkdir(parents=True)
        with sqlite3.connect(song / "jobs.sqlite3") as con:
            con.execute("CREATE TABLE jobs(id TEXT, owner TEXT)")
            con.execute("INSERT INTO jobs VALUES('j1','admin')")
        audio = self.root / "voice-studio/audio-downloader/data"
        audio.mkdir(parents=True)
        with sqlite3.connect(audio / "jobs.sqlite3") as con:
            con.execute("CREATE TABLE jobs(job_id TEXT, owner_username TEXT)")
            con.execute("INSERT INTO jobs VALUES('a1','haseeb1')")
        short = self.root / "short-maker/jobs/j1"
        short.mkdir(parents=True)
        (short / "state.json").write_text('{"owner": "", "status": "completed"}')
        (short.parent / "retry.lock").write_text("staging")
        (short.parent / "scratch").mkdir()
        (short.parent.parent / "vps_short_folders").mkdir(parents=True,exist_ok=True)
        (short.parent.parent / "vps_short_folders" / ".index").write_text("metadata")
        folder = self.root / "short-maker/vps_short_folders/Default"
        folder.mkdir(parents=True)
        report = inspect_related_stores(self.root)
        self.assertEqual(report["song_maker"]["non_target"],1)
        self.assertEqual(report["audio_downloader"]["non_target"],0)
        self.assertEqual(report["short_maker"]["jobs_non_target"],1)
        self.assertEqual(report["short_maker"]["folders_non_target"],1)
        self.assertEqual(report["short_maker"]["unclassified_regular_job_files"],1)
        self.assertEqual(report["short_maker"]["unclassified_regular_folder_files"],1)
        self.assertEqual((short / "state.json").read_text(),
                         '{"owner": "", "status": "completed"}')


    def test_guarded_release_prints_offline_receipt(self):
        from pathlib import Path
        source = Path("production/studio_haseeb1_assignment.py").read_text()
        self.assertIn('"STUDIO_UPLOADER_ASSIGNMENT": "PASS"', source)
        self.assertIn('"auto_script": "OFFLINE"', source)
        self.assertIn('"shared_google_drive": "UNCHANGED"', source)
        self.assertIn('"other_studio_data": "NOT_MIGRATED"', source)

if __name__ == "__main__":
    unittest.main()

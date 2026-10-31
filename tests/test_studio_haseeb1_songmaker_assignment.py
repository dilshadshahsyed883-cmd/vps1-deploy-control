"""Verified haseeb+team -> haseeb1 Song Maker migration regression tests."""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from production import studio_haseeb1_songmaker_assignment as sm
from production.studio_haseeb1_assignment import MigrationBlocked


class SongMakerOwnership(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.root = self.home/"song-maker"
        (self.root/"state").mkdir(parents=True)
        self.scene = self.home/"web.sqlite3"
        with sqlite3.connect(self.scene) as db:
            db.execute("CREATE TABLE users(username TEXT,is_active INTEGER,"
                       "studio_access TEXT,is_admin INTEGER)")
            db.executemany("INSERT INTO users VALUES(?,?,?,?)", [
                ("haseeb1",1,"song,short",0), ("haseeb",1,"song",0),
                ("team",1,"song",0)])
        self.dbpath = self.root/"state/jobs.sqlite3"
        with sqlite3.connect(self.dbpath) as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE jobs(id TEXT PRIMARY KEY,owner TEXT,source_key TEXT,
                stage TEXT,lane TEXT,data TEXT,UNIQUE(owner,source_key));
            CREATE TABLE settings(owner TEXT PRIMARY KEY,data TEXT);
            CREATE TABLE intake(owner TEXT PRIMARY KEY,data TEXT);
            CREATE TABLE sync(owner TEXT PRIMARY KEY,data TEXT);
            CREATE TABLE exports(owner TEXT PRIMARY KEY,data TEXT);
            CREATE TABLE queue_control(owner TEXT PRIMARY KEY,paused INTEGER);
            CREATE TABLE song_folders(id TEXT PRIMARY KEY,owner TEXT,name TEXT);
            CREATE TABLE song_folder_path_owners(folder_id TEXT PRIMARY KEY,
                owner TEXT,original_owner_hash TEXT);
            CREATE TABLE song_folder_media(job_id TEXT PRIMARY KEY,folder_id TEXT);
            """)
            db.executemany("INSERT INTO jobs VALUES(?,?,?,?,?,?)",
                 [(f"job{i}","haseeb" if i<9 else "team",f"key{i}",
                   "Completed" if i<4 else ("Cancelled" if i<7 else
                   "Failed" if i==7 else "Needs Attention"),"",json.dumps(
                    {"song_folder_id":"a"*32 if i<9 else "",
                     "original_filename":"keep.mp4"}))
                  for i in range(11)])
            db.executemany("INSERT INTO settings VALUES(?,?)",
                           [("haseeb",json.dumps({"preset":"H"})),
                            ("team",json.dumps({"preset":"T"})),
                            ("__default__",json.dumps({"preset":"DEFAULT"}))])
            db.execute("INSERT INTO intake VALUES(?,?)",
                       ("haseeb",json.dumps({"song_folder_id":"a"*32})))
            db.executemany("INSERT INTO sync VALUES(?,?)",
                           [("haseeb",json.dumps({"last_sync":1})),
                            ("team",json.dumps({"last_sync":2}))])
            db.execute("INSERT INTO exports VALUES('haseeb','{}')")
            self.fid = "a"*32
            db.execute("INSERT INTO song_folders VALUES(?,?,?)",
                       (self.fid,"haseeb","Original"))
        self.media = self.root/"folders"/hashlib.sha256(b"haseeb").hexdigest()/self.fid
        self.media.mkdir(parents=True)
        (self.media/"output.mp4").write_bytes(b"original media")
        self.original = (self.media/"output.mp4").read_bytes()

    def _transfer(self):
        with sqlite3.connect(self.dbpath) as db:
            db.execute("BEGIN IMMEDIATE")
            result=sm._assign(db,self.root,sm._users(self.scene))
            db.commit()
        return result

    def test_exact_named_owner_inventory_and_protected_default(self):
        proof = sm.audit(self.root,self.scene)
        data = proof["song_maker"]
        self.assertEqual(data["total"],19)
        self.assertEqual(data["tables"]["jobs"],11)
        self.assertEqual(data["owner_classes"]["approved_source"],18)
        self.assertEqual(data["owner_classes"]["legacy_default"],1)
        self.assertEqual(data["nonterminal_jobs"],0)
        self.assertEqual(data["folder_paths_verified"],1)
        self.assertTrue(data["source_owner_verified"])
        self.assertTrue(data["eligible_for_claim"])
        self.assertNotIn("haseeb",str(proof))
        self.assertNotIn("team",str(proof))
        self.assertEqual((self.media/"output.mp4").read_bytes(),self.original)

    def test_transfer_jobs_metadata_archive_and_preserve_owner_hashed_folder(self):
        result=self._transfer()
        self.assertEqual(result["approved_source_records"],18)
        self.assertEqual(result["archived_config_snapshots"],4)
        self.assertTrue(result["folder_path_hash_preserved"])
        with sqlite3.connect(self.dbpath) as db:
            self.assertEqual(sm.post_verify(db,self.root)["target_jobs"],11)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs WHERE owner='haseeb1'").fetchone()[0],11)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM song_folders WHERE owner='haseeb1'").fetchone()[0],1)
            self.assertEqual(json.loads(db.execute("SELECT data FROM settings WHERE owner='haseeb1'").fetchone()[0])["preset"],"H")
            self.assertEqual(json.loads(db.execute("SELECT data FROM settings WHERE owner='__default__'").fetchone()[0])["preset"],"DEFAULT")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM song_maker_ownership_archive").fetchone()[0],4)
            self.assertEqual(db.execute("SELECT data FROM song_maker_ownership_archive "
                "WHERE source_table='settings' AND legacy_owner='team'").fetchone()[0],
                json.dumps({"preset":"T"}))
        self.assertEqual((self.media/"output.mp4").read_bytes(),self.original)
        sys.path.insert(0,str(Path("apps/song-maker/part01").resolve()))
        try:
            from songmaker.store import Store
            from songmaker.folders import Folders
            folders=Folders(Store(self.root))
            self.assertEqual(folders.path("haseeb1",self.fid),self.media)
            self.assertEqual(folders.get("haseeb1",self.fid)["name"],"Original")
            with self.assertRaises(ValueError):
                folders.get("haseeb",self.fid)
        finally:
            sys.path.pop(0)

    def test_cross_owner_duplicate_job_key_blocks_without_changes(self):
        with sqlite3.connect(self.dbpath) as db:
            db.execute("UPDATE jobs SET source_key='key0' WHERE id='job9'")
        with self.assertRaisesRegex(MigrationBlocked,"JOB_KEY_COLLISION"):
            self._transfer()
        with sqlite3.connect(self.dbpath) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs WHERE owner='team'").fetchone()[0],2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='song_maker_ownership_archive'").fetchone()[0],0)

    def test_owner_manifest_drift_blocks_foreign_named_account(self):
        with sqlite3.connect(self.dbpath) as db:
            db.execute("UPDATE jobs SET owner='unexpected' WHERE id='job0'")
        state=sm.audit(self.root,self.scene)["song_maker"]
        self.assertFalse(state["source_owner_verified"])
        self.assertFalse(state["eligible_for_claim"])
        with patch("shutil.disk_usage",return_value=SimpleNamespace(free=20_000_000_000)), \
             patch.object(sm.subprocess,"run") as run:
            with self.assertRaisesRegex(MigrationBlocked,"FOREIGN_OWNER_UNVERIFIED"):
                sm.install("a"*40,self.root,self.scene,self.home/"transactions")
            run.assert_not_called()
        self.assertEqual((self.media/"output.mp4").read_bytes(),self.original)

    def test_active_jobs_and_missing_media_block(self):
        with sqlite3.connect(self.dbpath) as db:
            db.execute("UPDATE jobs SET stage='Rendering',lane='render' WHERE id='job0'")
        self.assertEqual(sm.audit(self.root,self.scene)["song_maker"]["nonterminal_jobs"],1)
        self.assertFalse(sm.audit(self.root,self.scene)["song_maker"]["eligible_for_claim"])
        (self.media/"output.mp4").unlink()
        self.media.rmdir()
        self.assertFalse(sm.audit(self.root,self.scene)["song_maker"]["media_references_safe"])

    def test_wal_consistent_backup_media_unchanged(self):
        snapshot=self.home/"journal/backup/jobs.sqlite3"
        sm._backup(self.dbpath,snapshot)
        with sqlite3.connect(snapshot) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0],"ok")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],11)
        self.assertEqual(list((self.home/"journal").rglob("*.mp4")),[])
        self.assertEqual((self.media/"output.mp4").read_bytes(),self.original)

    def test_failure_after_commit_restores_full_sqlite_and_services(self):
        def service(*args,**kw):
            return SimpleNamespace(returncode=0)
        journal=self.home/"journals"
        with patch("shutil.disk_usage",return_value=SimpleNamespace(free=20_000_000_000)), \
             patch.object(sm.subprocess,"run",side_effect=service), \
             patch.object(sm,"post_verify",side_effect=MigrationBlocked("POSTVERIFY")):
            with self.assertRaisesRegex(MigrationBlocked,"POSTVERIFY"):
                sm.install("b"*40,self.root,self.scene,journal)
        with sqlite3.connect(self.dbpath) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs WHERE owner='haseeb'").fetchone()[0],9)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs WHERE owner='team'").fetchone()[0],2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='song_maker_ownership_archive'").fetchone()[0],0)
        receipt=json.loads((journal/("b"*40)/"receipt.json").read_text())
        self.assertEqual(receipt["state"],"COMPENSATED")
        self.assertEqual((self.media/"output.mp4").read_bytes(),self.original)


if __name__=="__main__":
    unittest.main()

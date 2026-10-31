"""Song Maker owner migration safety and WAL-compensation regressions."""
import hashlib
from pathlib import Path
import sqlite3
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
                ("haseeb1",1,"song,short",0), ("alice",1,"song",0),
                ("bob",1,"song",0), ("inactive",0,"song",0)])
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
            CREATE TABLE song_folder_media(job_id TEXT PRIMARY KEY,folder_id TEXT);
            """)
            db.executemany("INSERT INTO jobs VALUES(?,?,?,?,?,?)",
                 [(f"job{i}","alice",f"key{i}","Completed","","{}")
                  for i in range(11)])
            db.executemany("INSERT INTO settings VALUES(?,?)",
                           [("alice","{}"),("bob","{}"),("ghost","{}")])
            db.execute("INSERT INTO intake VALUES('alice','{}')")
            db.executemany("INSERT INTO sync VALUES(?,?)",
                           [("alice","{}"),("bob","{}")])
            db.execute("INSERT INTO exports VALUES('alice','{}')")
            self.fid = "a"*32
            db.execute("INSERT INTO song_folders VALUES(?,?,?)",
                       (self.fid,"alice","Original"))
        self.media = self.root/"folders"/hashlib.sha256(b"alice").hexdigest()/self.fid
        self.media.mkdir(parents=True)
        (self.media/"output.mp4").write_bytes(b"original media")
        self.original = (self.media/"output.mp4").read_bytes()

    def test_production_inventory_19_and_foreign_owner_block(self):
        proof = sm.audit(self.root,self.scene)
        data = proof["song_maker"]
        self.assertEqual(data["total"],19)
        self.assertEqual(data["tables"]["jobs"],11)
        self.assertEqual(data["owner_classes"]["other_active"],18)
        self.assertEqual(data["owner_classes"]["other_unrecognized"],1)
        self.assertEqual(data["nonterminal_jobs"],0)
        self.assertEqual(data["folder_paths_verified"],1)
        self.assertFalse(data["source_owner_verified"])
        self.assertFalse(data["eligible_for_claim"])
        self.assertNotIn("alice",str(proof))
        self.assertNotIn("ghost",str(proof))
        with patch("shutil.disk_usage",return_value=SimpleNamespace(free=20_000_000_000)), \
             patch.object(sm.subprocess, "run") as run:
            with self.assertRaisesRegex(MigrationBlocked,"FOREIGN_OWNER_UNVERIFIED"):
                sm.install("a"*40,self.root,self.scene,self.home/"journals")
            run.assert_not_called()
        self.assertFalse((self.home/"journals").exists())
        self.assertEqual((self.media/"output.mp4").read_bytes(),self.original)

    def test_active_jobs_and_owner_hashed_media_prevent_claim(self):
        with sqlite3.connect(self.dbpath) as db:
            db.execute("UPDATE jobs SET stage='Rendering',lane='render' WHERE id='job0'")
        state = sm.audit(self.root,self.scene)["song_maker"]
        self.assertEqual(state["nonterminal_jobs"],1)
        self.assertFalse(state["eligible_for_claim"])
        (self.media/"output.mp4").unlink()
        self.media.rmdir()
        state = sm.audit(self.root,self.scene)["song_maker"]
        self.assertEqual(state["missing_media_paths"],1)
        self.assertFalse(state["media_references_safe"])

    def test_exact_wal_snapshot_integrity_and_media_not_copied(self):
        snapshot = self.home/"journal/backup/jobs.sqlite3"
        sm._backup(self.dbpath,snapshot)
        with sqlite3.connect(snapshot) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0],"ok")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],11)
        self.assertEqual(list((self.home/"journal").rglob("*.mp4")),[])
        self.assertEqual((self.media/"output.mp4").read_bytes(),self.original)

    def test_eligible_first_claim_is_transactional_and_idempotent(self):
        # Tiny wholly-unassigned inventory proves safe first claim. Foreign
        # named owners in production never meet this condition.
        with sqlite3.connect(self.dbpath) as db:
            for table in sm.TABLES:
                db.execute("DELETE FROM "+table)
            db.execute("INSERT INTO jobs VALUES('j','', 'key','Completed','','{}')")
            db.execute("INSERT INTO settings VALUES('','{}')")
        with patch.object(sm,"EXPECTED",{"jobs":1,"settings":1,"intake":0,"sync":0,
                            "exports":0,"queue_control":0,"song_folders":0}):
            with sqlite3.connect(self.dbpath) as db:
                db.execute("BEGIN IMMEDIATE")
                before,after=sm._assign(db,self.root,sm._users(self.scene))
                self.assertEqual(before["eligible_unassigned"],2)
                self.assertEqual(after["eligible_unassigned"],0)
                db.commit()
            with sqlite3.connect(self.dbpath) as db:
                self.assertEqual(db.execute("SELECT owner FROM jobs").fetchone()[0],"haseeb1")
                self.assertEqual(db.execute("SELECT owner FROM settings").fetchone()[0],"haseeb1")
            with sqlite3.connect(self.dbpath) as db:
                db.execute("BEGIN IMMEDIATE")
                before,after=sm._assign(db,self.root,sm._users(self.scene))
                self.assertEqual(before,after)
                db.commit()

    def test_no_overwrite_existing_haseeb1_settings(self):
        with sqlite3.connect(self.dbpath) as db:
            for table in sm.TABLES:
                db.execute("DELETE FROM "+table)
            db.executemany("INSERT INTO settings VALUES(?,?)",
                           [("","original"),("haseeb1","current")])
            db.commit()
        with patch.object(sm,"EXPECTED",{"jobs":0,"settings":2,"intake":0,"sync":0,
                            "exports":0,"queue_control":0,"song_folders":0}):
            with sqlite3.connect(self.dbpath) as db:
                db.execute("BEGIN IMMEDIATE")
                with self.assertRaisesRegex(MigrationBlocked,"TARGET_RECORD_COLLISION"):
                    sm._assign(db,self.root,sm._users(self.scene))
                db.rollback()
            with sqlite3.connect(self.dbpath) as db:
                self.assertEqual(db.execute("SELECT data FROM settings WHERE owner='haseeb1'").fetchone()[0],"current")


if __name__=="__main__":
    unittest.main()

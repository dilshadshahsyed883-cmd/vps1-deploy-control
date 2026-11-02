"""Isolated transactional Short Folder retirement; no live deletion endpoint."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1] / "apps/short-maker/part01"


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT/(name+".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def hash_file(path):
    h=hashlib.sha256()
    with path.open("rb") as source:
        for part in iter(lambda:source.read(1024*1024),b""):
            h.update(part)
    return h.hexdigest()


class RetirementTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.folders=self.root/"folders"
        self.folders.mkdir()
        self.target=self.folders/"Owner Folder"
        self.target.mkdir()
        (self.target/".studio_owner").write_text("alice")
        self.video=self.target/"Owner Folder 001_ff0000.mp4"
        self.video.write_bytes(b"mp4-source"*1024)
        self.sha=hash_file(self.video)
        self.stored=self.root/"store"
        self.profiles=load("short_profiles").ShortProfileStore(self.stored)
        self.profile=self.profiles.create("alice",{
            "name":"Generator","source_import_id":"a"*32,
            "output_folder":"Owner Folder","videos_per_day":1,
            "schedule_hhmm":"02:15"},
            lambda who,sid:{"audio_count":1,"image_count":1},lambda name:name)
        with self.profiles._connect() as db:
            db.execute("UPDATE short_profiles SET enabled=1 WHERE id=?",
                       (self.profile["id"],))
        self.app=self.root/"uploader.db"
        self.engine=self.root/"engine.db"
        with sqlite3.connect(self.app) as db:
            db.executescript("""
                CREATE TABLE auto_engine_profiles(
                  id INTEGER PRIMARY KEY,category_name TEXT,enabled INTEGER,
                  schedule_enabled INTEGER,schedule_times TEXT);
                CREATE TABLE studio_entity_owners(
                  entity_kind TEXT,entity_id INTEGER,owner_username TEXT);
                CREATE TABLE videos(id INTEGER PRIMARY KEY,status TEXT,
                                    auto_engine_profile_id INTEGER);
                INSERT INTO auto_engine_profiles VALUES
                  (7,'Paused Short Profile',1,1,'09:00'),
                  (8,'Foreign Unchanged',1,1,'10:00');
                INSERT INTO studio_entity_owners VALUES
                  ('profile',7,'alice'),('profile',8,'bob');
            """)
        with sqlite3.connect(self.engine) as db:
            db.executescript("""
                CREATE TABLE profile_sources(
                  profile_id INTEGER PRIMARY KEY,short_folder_name TEXT);
                CREATE TABLE job_sources(video_id INTEGER PRIMARY KEY,
                  profile_id INTEGER,source_folder_id TEXT,filename TEXT,state TEXT);
                CREATE TABLE drive_videos(id INTEGER PRIMARY KEY,
                  profile_id INTEGER,filename TEXT,source_folder_id TEXT,present INTEGER);
                INSERT INTO profile_sources VALUES (7,'Owner Folder');
                INSERT INTO profile_sources VALUES (8,'Another Folder');
                INSERT INTO drive_videos VALUES(
                  300,7,'Owner Folder 001_ff0000.mp4','shortstudio-local:Owner Folder',1);
            """)
        self.jobs=[{
            "job_id":"a"*16,"owner":"alice","status":"completed",
            "config":{"short_vps_folder":"Owner Folder"},
            "outputs":[{
                "short_vps_folder":"Owner Folder",
                "short_vps_filename":self.video.name,
                "short_vps_sha256":self.sha
            }]
        }]
        inspector=load("short_folder_dependencies").ShortFolderPreflight(
            self.profiles,lambda:self.jobs,self.root/"ready",
            uploader_app_db=str(self.app),uploader_engine_db=str(self.engine))
        self.inspect=inspector
        self.leases=0
        @contextmanager
        def lease():
            self.leases+=1
            yield
        cls=load("short_folder_retirement").ShortFolderRetirement
        self.worker=cls(root=self.folders,journal_root=self.root/"journals",
                        profile_store=self.profiles,inspector=inspector,
                        lease_factory=lease)
        self.disabled=cls(root=self.folders,journal_root=self.root/"no-lease",
                          profile_store=self.profiles,inspector=inspector)

    def tearDown(self):
        self.temp.cleanup()

    def retire(self):
        return self.worker.retire(
            owner="alice",folder_name="Owner Folder",
            confirmation="Owner Folder",digest=hash_file,
            preserve_defaults=("Default","My_reserved"))

    def state(self):
        with self.profiles._connect() as maker, sqlite3.connect(self.app) as app, \
                sqlite3.connect(self.engine) as engine:
            return {
                "maker":maker.execute("SELECT enabled,output_folder,schedule_hhmm FROM short_profiles WHERE id=?",
                                      (self.profile["id"],)).fetchone(),
                "app":app.execute("SELECT enabled,schedule_enabled,schedule_times FROM auto_engine_profiles WHERE id=7").fetchone(),
                "engine":engine.execute("SELECT short_folder_name FROM profile_sources WHERE profile_id=7").fetchone(),
                "index":engine.execute("SELECT present FROM drive_videos WHERE id=300").fetchone(),
                "other":app.execute("SELECT enabled FROM auto_engine_profiles WHERE id=8").fetchone(),
            }

    def test_verified_retirement_detaches_only_own_and_preserves_history_schedule(self):
        result=self.retire()
        self.assertTrue(result["ok"])
        self.assertEqual(result["videos_deleted"],1)
        self.assertEqual(result["linked_profiles_detached"],2)
        self.assertFalse(self.target.exists())
        self.assertEqual(self.leases,1)
        self.assertEqual(list(self.folders.glob(".short-retirement-*")),[])
        data=self.state()
        self.assertEqual(data["maker"][0:2],(0,""))
        self.assertEqual(data["maker"][2],"02:15")
        self.assertEqual(data["app"],(0,1,"09:00"))
        self.assertEqual(data["engine"],("",))
        self.assertEqual(data["index"],(0,))
        self.assertEqual(data["other"],(1,))
        self.assertEqual(self.profiles.get("alice",self.profile["id"])["name"],"Generator")
        journals=list((self.root/"journals").glob("*.json"))
        self.assertEqual(len(journals),1)
        self.assertEqual(json.loads(journals[0].read_text())["phase"],"complete")
        self.assertEqual(self.worker.recover(hash_file),[])

    def test_real_action_is_unavailable_without_verified_cooperative_lease(self):
        with self.assertRaises(HTTPException) as err:
            self.disabled.retire(owner="alice",folder_name="Owner Folder",
                                 confirmation="Owner Folder",digest=hash_file)
        self.assertEqual(err.exception.status_code,503)
        self.assertTrue(self.video.exists())

    def test_wrong_confirmation_never_changes_media(self):
        with self.assertRaises(HTTPException) as err:
            self.worker.retire(owner="alice",folder_name="Owner Folder",
                               confirmation="Wrong",digest=hash_file)
        self.assertEqual(err.exception.status_code,409)
        self.assertTrue(self.video.exists())

    def test_replaced_mp4_blocks_retirement_and_link_mutation(self):
        self.video.write_bytes(b"changed"*1200)
        with self.assertRaises(HTTPException) as err:
            self.retire()
        self.assertEqual(err.exception.status_code,409)
        self.assertTrue(self.target.exists())
        self.assertEqual(self.state()["maker"][0:2],(1,"Owner Folder"))
        self.assertEqual(list((self.root/"journals").glob("*.json")),[])

    def test_unfinished_source_claim_never_detached(self):
        with sqlite3.connect(self.engine) as db:
            db.execute("""INSERT INTO job_sources VALUES(
               123,7,'shortstudio-local:Owner Folder',
               'Owner Folder 001_ff0000.mp4','QUEUED')""")
        with self.assertRaises(HTTPException) as err:
            self.retire()
        self.assertEqual(err.exception.status_code,409)
        self.assertEqual(self.state()["engine"],("Owner Folder",))
        self.assertTrue(self.video.exists())

    def test_foreign_reference_never_leaked_or_detached(self):
        with sqlite3.connect(self.engine) as db:
            db.execute("UPDATE profile_sources SET short_folder_name='Owner Folder' WHERE profile_id=8")
        with self.assertRaises(HTTPException) as err:
            self.retire()
        self.assertEqual(err.exception.status_code,409)
        self.assertNotIn("Foreign Unchanged",str(err.exception.detail))
        self.assertEqual(self.state()["other"],(1,))
        self.assertTrue(self.video.exists())

    def test_partial_database_commit_is_restored_from_journal(self):
        original=self.worker._commit_all
        seen=[False]
        def fail_once(databases):
            if not seen[0]:
                seen[0]=True
                databases[0].commit()
                raise OSError("Simulated crash after Maker DB commit")
            return original(databases)
        with patch.object(self.worker,"_commit_all",side_effect=fail_once):
            with self.assertRaises(OSError):
                self.retire()
        self.assertTrue(self.video.exists())
        self.assertEqual(self.state()["maker"][0:2],(1,"Owner Folder"))
        self.assertEqual(self.state()["engine"],("Owner Folder",))
        self.assertEqual(self.state()["app"],(1,1,"09:00"))
        journals=list((self.root/"journals").glob("*.json"))
        self.assertEqual(len(journals),1)
        self.assertEqual(json.loads(journals[0].read_text())["phase"],"recovered")

    def test_committed_partial_cleanup_recovery_is_idempotent(self):
        self.worker.journal_root.mkdir()
        filename="f"*32
        name=".short-retirement-"+filename
        hidden=self.folders/name
        self.target.rename(hidden)
        # Simulate a crash after first MP4 unlink.
        (hidden/self.video.name).unlink()
        journal={"id":filename,"phase":"committed","folder":"Owner Folder",
                 "owner":"alice","quarantine":name,
                 "media":{self.video.name:self.sha},"snapshots":{
                   "maker_profiles":[],"uploader_profiles":[],
                   "uploader_sources":[],"indexed":[]}}
        j=self.worker.journal_root/(filename+".json")
        j.write_text(json.dumps(journal))
        result=self.worker.recover(hash_file)
        self.assertEqual(result,[{"id":filename,"phase":"complete"}])
        self.assertFalse(hidden.exists())
        self.assertEqual(self.worker.recover(hash_file),[])

    def test_uncommitted_quarantine_is_recovered_without_media_loss(self):
        self.worker.journal_root.mkdir()
        filename="e"*32
        name=".short-retirement-"+filename
        self.target.rename(self.folders/name)
        originals={"maker_profiles":[{
            "id":self.profile["id"],"owner":"alice","enabled":1,
            "output_folder":"Owner Folder"}],
            "uploader_profiles":[{"id":7,"enabled":1}],
            "uploader_sources":[{"profile_id":7,"short_folder_name":"Owner Folder"}],
            "indexed":[{"id":300,"present":1}]}
        with self.profiles._connect() as maker, sqlite3.connect(self.app) as app, \
                sqlite3.connect(self.engine) as engine:
            maker.execute("UPDATE short_profiles SET output_folder='',enabled=0")
            app.execute("UPDATE auto_engine_profiles SET enabled=0 WHERE id=7")
            engine.execute("UPDATE profile_sources SET short_folder_name='' WHERE profile_id=7")
            engine.execute("UPDATE drive_videos SET present=0 WHERE id=300")
        j=self.worker.journal_root/(filename+".json")
        j.write_text(json.dumps({
            "id":filename,"phase":"quarantined","owner":"alice",
            "folder":"Owner Folder","quarantine":name,
            "media":{self.video.name:self.sha},"snapshots":originals}))
        result=self.worker.recover(hash_file)
        self.assertEqual(result,[{"id":filename,"phase":"recovered"}])
        self.assertTrue(self.video.exists())
        self.assertEqual(self.state()["maker"][0:2],(1,"Owner Folder"))
        self.assertEqual(self.state()["engine"],("Owner Folder",))
        self.assertEqual(self.state()["app"],(1,1,"09:00"))


if __name__=="__main__":
    unittest.main()

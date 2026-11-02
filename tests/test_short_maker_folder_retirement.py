"""Unused-only folder retirement: no profile/index/queue mutation, no live calls."""
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

ROOT=Path(__file__).resolve().parents[1]/"apps/short-maker/part01"


def load(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/(name+".py"))
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def hash_file(path):
    h=hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda:source.read(1024*1024),b""):
            h.update(block)
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
        self.profiles=load("short_profiles").ShortProfileStore(self.root/"store")
        self.profile=self.profiles.create("alice",{
            "name":"Generator","source_import_id":"a"*32,
            "output_folder":"Owner Folder","videos_per_day":1,
            "schedule_hhmm":"02:15"},lambda owner,sid:{
                "audio_count":1,"image_count":1},lambda name:name)
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
                   (7,'Paused Short Profile',0,1,'09:00'),
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
                INSERT INTO profile_sources VALUES (7,'Owner Folder'),(8,'Another Folder');
                INSERT INTO drive_videos VALUES(
                  300,7,'Owner Folder 001_ff0000.mp4','shortstudio-local:Owner Folder',1);
            """)
        self.jobs=[{
            "job_id":"b"*16,"owner":"alice","status":"completed",
            "config":{"short_vps_folder":"Owner Folder"},
            "outputs":[{"short_vps_folder":"Owner Folder",
                        "short_vps_filename":self.video.name,
                        "short_vps_sha256":self.sha}]
        }]
        self.inspector=load("short_folder_dependencies").ShortFolderPreflight(
            self.profiles,lambda:self.jobs,self.root/"ready",
            uploader_app_db=str(self.app),uploader_engine_db=str(self.engine))
        self.worker_module=load("short_folder_retirement")
        self.leases=0
        @contextmanager
        def exclusive_lease():
            self.leases+=1
            yield
        klass=self.worker_module.ShortFolderRetirement
        self.worker=klass(root=self.folders,journal_root=self.root/"journals",
            profile_store=self.profiles,inspector=self.inspector,
            lease_factory=exclusive_lease)
        self.disabled=klass(root=self.folders,journal_root=self.root/"no-lease",
            profile_store=self.profiles,inspector=self.inspector)

    def tearDown(self):
        self.temp.cleanup()

    def clean_dependencies(self):
        # Represents separately approved owner-scoped profile management,
        # not a side effect of the retirement action.
        with self.profiles._connect() as db:
            db.execute("UPDATE short_profiles SET output_folder='' WHERE id=?",
                       (self.profile["id"],))
        with sqlite3.connect(self.engine) as db:
            db.execute("UPDATE profile_sources SET short_folder_name='' WHERE profile_id=7")
            db.execute("UPDATE drive_videos SET present=0 WHERE id=300")

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

    def retire(self,confirmation="Owner Folder"):
        return self.worker.retire(owner="alice",folder_name="Owner Folder",
            confirmation=confirmation,digest=hash_file,
            preserve_defaults=("Default","My_reserved"))

    def assert_denied_unchanged(self):
        before=self.state()
        with self.assertRaises(HTTPException) as denied:
            self.retire()
        self.assertEqual(denied.exception.status_code,409)
        self.assertTrue(self.video.is_file())
        self.assertEqual(self.state(),before)
        self.assertEqual(list((self.root/"journals").glob("*.json")),[])

    def test_profile_links_block_even_when_stopped_and_preserve_all_records(self):
        self.assert_denied_unchanged()
        with self.profiles._connect() as db:
            db.execute("UPDATE short_profiles SET enabled=0 WHERE id=?",(self.profile["id"],))
        self.assert_denied_unchanged()
        with self.profiles._connect() as db:
            db.execute("UPDATE short_profiles SET output_folder='' WHERE id=?",
                       (self.profile["id"],))
        # Even disabled/paused Short Uploader profile links block deletion.
        self.assert_denied_unchanged()

    def test_clean_unused_folder_deletes_only_verified_mp4_and_preserves_dbs(self):
        self.clean_dependencies()
        before=self.state()
        report=self.inspector.inspect(owner="alice",folder_name="Owner Folder",
                                      folder=self.target)
        self.assertEqual(report["issues"],[])
        result=self.retire()
        self.assertTrue(result["ok"])
        self.assertEqual(result["videos_deleted"],1)
        self.assertEqual(result["linked_profiles_detached"],0)
        self.assertFalse(result["profiles_modified"])
        self.assertTrue(result["history_preserved"])
        self.assertEqual(self.state(),before)
        self.assertFalse(self.target.exists())
        self.assertEqual(self.leases,1)
        self.assertEqual(list(self.folders.glob(".short-retirement-*")),[])
        journals=list((self.root/"journals").glob("*.json"))
        self.assertEqual(len(journals),1)
        self.assertEqual(json.loads(journals[0].read_text())["phase"],"complete")
        self.assertEqual(self.worker.recover(hash_file),[])

    def test_active_maker_shared_lease_blocks_unused_retirement(self):
        self.clean_dependencies()
        lease=load("short_folder_cooperation").ShortStudioFolderLease(
            self.folders,enabled=True,timeout_seconds=0.12)
        self.worker.lease_factory=lease.retire
        with lease.producer():
            with self.assertRaises(HTTPException) as denied:
                self.retire()
            self.assertEqual(denied.exception.status_code,503)
            self.assertTrue(self.video.is_file())
        self.assertTrue(self.retire()["ok"])
        self.assertFalse(self.target.exists())

    def test_missing_verified_exclusive_lease_refuses_even_empty_folder(self):
        self.clean_dependencies()
        with self.assertRaises(HTTPException) as denied:
            self.disabled.retire(owner="alice",folder_name="Owner Folder",
                confirmation="Owner Folder",digest=hash_file)
        self.assertEqual(denied.exception.status_code,503)
        self.assertTrue(self.video.is_file())

    def test_confirmation_and_default_are_protected(self):
        self.clean_dependencies()
        with self.assertRaises(HTTPException) as denied:
            self.retire(confirmation="Wrong")
        self.assertEqual(denied.exception.status_code,409)
        with self.assertRaises(HTTPException) as denied:
            self.worker.retire(owner="alice",folder_name="Owner Folder",
               confirmation="Owner Folder",digest=hash_file,
               preserve_defaults=("Owner Folder",))
        self.assertEqual(denied.exception.status_code,403)
        self.assertTrue(self.video.is_file())

    def test_unresolved_or_even_completed_source_claim_blocks(self):
        self.clean_dependencies()
        with sqlite3.connect(self.engine) as db:
            db.execute("INSERT INTO job_sources VALUES(123,7,?,?,?)",
                       ("shortstudio-local:Owner Folder",self.video.name,"QUEUED"))
        self.assert_denied_unchanged()
        with sqlite3.connect(self.engine) as db:
            db.execute("UPDATE job_sources SET state='COMPLETE' WHERE video_id=123")
        # Completely unused means *no* residual claim still referencing MP4.
        self.assert_denied_unchanged()

    def test_indexed_video_without_profile_reference_blocks(self):
        self.clean_dependencies()
        with sqlite3.connect(self.engine) as db:
            db.execute("UPDATE drive_videos SET present=1 WHERE id=300")
        self.assert_denied_unchanged()

    def test_foreign_or_unassigned_reference_never_exposed_or_mutated(self):
        self.clean_dependencies()
        with sqlite3.connect(self.engine) as db:
            db.execute("UPDATE profile_sources SET short_folder_name='Owner Folder' WHERE profile_id=8")
        before=self.state()
        with self.assertRaises(HTTPException) as denied:
            self.retire()
        self.assertEqual(denied.exception.status_code,409)
        self.assertNotIn("Foreign Unchanged",str(denied.exception.detail))
        self.assertEqual(self.state(),before)
        self.assertTrue(self.video.exists())

    def test_active_maker_job_still_blocks(self):
        self.clean_dependencies()
        self.jobs[0]["status"]="running"
        self.assert_denied_unchanged()

    def test_unverified_or_replaced_mp4_never_removed(self):
        self.clean_dependencies()
        self.video.write_bytes(b"modified"*1500)
        self.assert_denied_unchanged()
        self.video.write_bytes(b"mp4-source"*1024)
        (self.target/"UNKNOWN.txt").write_text("preserve")
        self.assert_denied_unchanged()

    def test_missing_uploader_db_refuses(self):
        self.clean_dependencies()
        self.inspector.uploader_engine_db=""
        self.assert_denied_unchanged()

    def test_interrupted_quarantine_restores_original_without_any_db_write(self):
        self.clean_dependencies()
        before=self.state()
        original_sync=self.worker_module._sync_json
        first=[True]
        def break_during_quarantine(path,payload):
            if payload.get("phase")=="quarantined" and first[0]:
                first[0]=False
                raise OSError("Simulated crash before durable commit")
            return original_sync(path,payload)
        with patch.object(self.worker_module,"_sync_json",side_effect=break_during_quarantine):
            with self.assertRaises(OSError):
                self.retire()
        self.assertTrue(self.video.exists())
        self.assertEqual(self.state(),before)
        journal=list((self.root/"journals").glob("*.json"))
        self.assertEqual(len(journal),1)
        self.assertEqual(json.loads(journal[0].read_text())["phase"],"recovered")

    def test_committed_partial_cleanup_recovery_is_idempotent(self):
        self.worker.journal_root.mkdir()
        identifier="f"*32
        name=".short-retirement-"+identifier
        hidden=self.folders/name
        self.target.rename(hidden)
        (hidden/self.video.name).unlink()
        journal={"id":identifier,"phase":"committed","folder":"Owner Folder",
                 "owner":"alice","quarantine":name,
                 "media":{self.video.name:self.sha}}
        j=self.worker.journal_root/(identifier+".json")
        j.write_text(json.dumps(journal))
        original=self.state()
        self.assertEqual(self.worker.recover(hash_file),
                         [{"id":identifier,"phase":"complete"}])
        self.assertFalse(hidden.exists())
        self.assertEqual(self.state(),original)
        self.assertEqual(self.worker.recover(hash_file),[])

    def test_incomplete_quarantine_recovery_restores_media_without_db_writes(self):
        self.worker.journal_root.mkdir()
        identifier="e"*32
        name=".short-retirement-"+identifier
        self.target.rename(self.folders/name)
        journal={"id":identifier,"phase":"quarantined","folder":"Owner Folder",
                 "owner":"alice","quarantine":name,
                 "media":{self.video.name:self.sha}}
        (self.worker.journal_root/(identifier+".json")).write_text(json.dumps(journal))
        before=self.state()
        self.assertEqual(self.worker.recover(hash_file),
                         [{"id":identifier,"phase":"recovered"}])
        self.assertTrue(self.video.exists())
        self.assertEqual(self.state(),before)

    def test_pre_rename_recovery_refuses_replaced_original(self):
        self.clean_dependencies()
        self.worker.journal_root.mkdir()
        identifier = "d" * 32
        record = {
            "id": identifier, "phase": "preparing",
            "folder": "Owner Folder", "owner": "alice",
            "quarantine": ".short-retirement-" + identifier,
            "media": {self.video.name: self.sha},
        }
        journal_file = self.worker.journal_root / (identifier + ".json")
        journal_file.write_text(json.dumps(record))
        original_state = self.state()
        # The original folder still exists, but the receipt no longer matches:
        # the recovery path must never silently bless a replacement.
        self.video.write_bytes(b"unverified replacement video")
        with self.assertRaises(HTTPException) as blocked:
            self.worker.recover(hash_file)
        self.assertEqual(blocked.exception.status_code, 409)
        self.assertEqual(json.loads(journal_file.read_text())["phase"],
                         "preparing")
        self.assertEqual(self.state(), original_state)
        self.assertTrue(self.video.exists())
        # Once the original verified media is restored, recovery can finish.
        self.video.write_bytes(b"mp4-source" * 1024)
        self.assertEqual(self.worker.recover(hash_file),
                         [{"id": identifier, "phase": "recovered"}])
        self.assertEqual(self.state(), original_state)
        self.assertTrue(self.video.exists())

    def test_malicious_journal_path_traversal_is_rejected(self):
        self.clean_dependencies()
        self.worker.journal_root.mkdir()
        filename="e"*32
        journal={"id":filename,"phase":"quarantined",
                 "folder":"../outside","owner":"alice",
                 "quarantine":".short-retirement-"+filename,"media":{}}
        (self.worker.journal_root/(filename+".json")).write_text(json.dumps(journal))
        with self.assertRaises(HTTPException) as denied:
            self.retire()
        self.assertEqual(denied.exception.status_code,409)
        self.assertTrue(self.video.exists())


if __name__=="__main__":
    unittest.main()

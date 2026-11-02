"""Short Maker deletion preflight: only owner-visible dependencies; no mutation."""
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

SOURCE = Path(__file__).resolve().parents[1] / "apps/short-maker/part01"


def module(name):
    spec = importlib.util.spec_from_file_location(name, SOURCE/(name+".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FolderDeletePreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.folder = self.root/"Owner Folder"
        self.folder.mkdir()
        (self.folder/".studio_owner").write_text("alice", encoding="utf-8")
        self.ready = self.root/"ready_video_uploads"
        self.ready.mkdir()
        self.app_db = self.root/"uploader.db"
        self.engine_db = self.root/"auto_engine_v6.sqlite3"
        with sqlite3.connect(self.app_db) as db:
            db.executescript("""
                CREATE TABLE auto_engine_profiles (
                    id INTEGER PRIMARY KEY,category_name TEXT,
                    enabled INTEGER,schedule_enabled INTEGER);
                CREATE TABLE studio_entity_owners (
                    entity_kind TEXT,entity_id INTEGER,owner_username TEXT);
                CREATE TABLE videos (
                    id INTEGER PRIMARY KEY,status TEXT,auto_engine_profile_id INTEGER);
                INSERT INTO auto_engine_profiles VALUES (11,'Paused Owning Profile',0,0);
                INSERT INTO auto_engine_profiles VALUES (12,'Foreign Profile',1,1);
                INSERT INTO studio_entity_owners VALUES('profile',11,'alice');
                INSERT INTO studio_entity_owners VALUES('profile',12,'bob');
                INSERT INTO videos VALUES (91,'CLAIMED',11);
            """)
        with sqlite3.connect(self.engine_db) as db:
            db.executescript("""
                CREATE TABLE profile_sources (
                    profile_id INTEGER PRIMARY KEY,short_folder_name TEXT);
                CREATE TABLE job_sources (
                    video_id INTEGER,profile_id INTEGER,
                    source_folder_id TEXT,filename TEXT,state TEXT);
                CREATE TABLE drive_videos (
                    profile_id INTEGER, filename TEXT,
                    source_folder_id TEXT, present INTEGER);
                INSERT INTO profile_sources VALUES (11,'Owner Folder');
                INSERT INTO job_sources VALUES (
                  91,11,'shortstudio-local:Owner Folder','source.mp4','QUEUED');
            """)
        self.profile_store = module("short_profiles").ShortProfileStore(self.root/"data")
        self.jobs = []
        self.inspector = module("short_folder_dependencies").ShortFolderPreflight(
            self.profile_store, lambda: self.jobs, self.ready,
            uploader_app_db=str(self.app_db), uploader_engine_db=str(self.engine_db))

    def tearDown(self):
        self.tmp.cleanup()

    def inspect(self):
        return self.inspector.inspect(owner="alice",folder_name="Owner Folder",
                                      folder=self.folder)

    def test_paused_uploader_profile_and_claim_are_blockers(self):
        result = self.inspect()
        self.assertTrue(result["uploader_dependency_verified"])
        self.assertEqual([x["name"] for x in result["linked_profiles"]],
                         ["Paused Owning Profile"])
        self.assertFalse(result["linked_profiles"][0]["enabled"])
        self.assertGreater(result["active_uploader_claims"],0)
        self.assertIn("Short Uploader profiles still link folder",result["issues"])
        self.assertIn("Short Uploader has unresolved video claims",result["issues"])
        self.assertFalse(result["deletion_enabled"])
        self.assertFalse(result["delete_preflight_ready"])

    def test_foreign_and_unassigned_profile_links_are_private_blockers(self):
        with sqlite3.connect(self.engine_db) as db:
            db.execute("INSERT INTO profile_sources VALUES(12,'Owner Folder')")
            db.execute("INSERT INTO profile_sources VALUES(99,'Owner Folder')")
        result = self.inspect()
        self.assertEqual(len(result["linked_profiles"]),1)
        self.assertNotIn("Foreign Profile",json.dumps(result))
        self.assertGreaterEqual(result["foreign_or_unassigned_references"],2)
        self.assertIn("Foreign or unassigned uploader dependency",result["issues"])

    def test_stopped_short_profile_still_blocks(self):
        profile = self.profile_store.create("alice", {
            "name":"Stopped Generator","source_import_id":"a"*32,
            "output_folder":"Owner Folder","videos_per_day":5,
            "schedule_hhmm":"00:00",
        }, lambda owner,receipt:{"audio_count":2,"image_count":2},
           lambda name:name)
        result = self.inspect()
        self.assertEqual([x["id"] for x in result["linked_short_profiles"]],
                         [profile["id"]])
        self.assertFalse(result["linked_short_profiles"][0]["enabled"])
        self.assertIn("Short Profiles still link this folder",result["issues"])

    def test_foreign_maker_profile_link_is_not_exposed(self):
        self.profile_store.create("bob", {
            "name":"Hidden Foreign Generator","source_import_id":"a"*32,
            "output_folder":"Owner Folder","videos_per_day":1,
            "schedule_hhmm":"00:00",
        }, lambda owner,receipt:{"audio_count":1,"image_count":1},
           lambda name:name)
        report=self.inspect()
        self.assertIn("Foreign or unassigned Short Profile references folder",
                      report["issues"])
        self.assertNotIn("Hidden Foreign Generator",json.dumps(report))

    def test_reserved_or_orphaned_daily_claim_blocks_deletion(self):
        daily=module("short_profile_daily")
        profile=self.profile_store.create("alice",{
            "name":"Queued","source_import_id":"a"*32,
            "output_folder":"Owner Folder","videos_per_day":1,
            "schedule_hhmm":"00:00",
        }, lambda owner,receipt:{"audio_count":1,"image_count":1},
           lambda name:name)
        with self.profile_store._connect() as db:
            db.execute("UPDATE short_profiles SET enabled=1 WHERE id=?",
                       (profile["id"],))
        planner=daily.DailyBatchPlanner(
            self.profile_store,
            lambda owner,sid:{"audio":[{"sha256":"a"*64,"path":"/source/a.mp3"}],
                              "picture":[{"sha256":"f"*64,"path":"/source/a.png"}]},
            lambda owner,name:None)
        from datetime import datetime,timezone
        when=datetime(2026,11,1,19,0,tzinfo=timezone.utc)
        result=planner.plan("alice",profile["id"],when)
        self.assertEqual(len(result["jobs"]),1)
        report=self.inspect()
        self.assertIn("Undispatched daily profile batch still reserves folder",
                      report["issues"])
        with self.profile_store._connect() as db:
            db.execute("UPDATE short_profile_jobs SET status='enqueued'")
        report=self.inspect()
        self.assertIn("Unreconciled daily render job claim",report["issues"])

    def test_busy_maker_job_and_foreign_job_block(self):
        self.jobs.extend([
            {"owner":"alice","status":"running",
             "config":{"short_vps_folder":"Owner Folder"}},
            {"owner":"bob","status":"completed",
             "config":{"short_vps_folder":"Owner Folder"}},
        ])
        result=self.inspect()
        self.assertIn("Active Short Maker render or queue job",result["issues"])
        self.assertIn("Maker job has foreign/unassigned owner",result["issues"])

    def test_incomplete_import_and_unknown_files_block(self):
        folder=self.ready/"session1"
        folder.mkdir()
        (folder/"manifest.json").write_text(json.dumps(
            {"owner":"alice","folder":"Owner Folder","status":"paused",
             "files":[{"name":"file.mp4","size":5000}]}))
        (self.folder/"unknown.txt").write_text("unsupported")
        result = self.inspect()
        self.assertIn("Ready-made video upload is in progress or paused",result["issues"])
        self.assertIn("Folder has unverified or non-MP4 entries",result["issues"])

    def test_foreign_import_and_invalid_manifest_block(self):
        foreign=self.ready/"another"
        foreign.mkdir()
        (foreign/"manifest.json").write_text(json.dumps({
            "owner":"bob","folder":"Owner Folder","status":"publishing"}))
        broken=self.ready/"bad"
        broken.mkdir()
        (broken/"manifest.json").write_text("{not-json")
        result=self.inspect()
        self.assertIn("Unowned ready-made import references folder",result["issues"])
        self.assertIn("Unreadable ready-made upload session",result["issues"])

    def test_untracked_mp4_never_authorized_by_extension(self):
        with sqlite3.connect(self.engine_db) as db:
            db.execute("DELETE FROM job_sources")
            db.execute("DELETE FROM profile_sources")
        (self.folder/"untracked.mp4").write_bytes(b"mp4-content"*1000)
        result=self.inspect()
        self.assertIn("Folder contains unverified or changed MP4s",result["issues"])
        self.assertEqual(result["unverified_media_count"],1)
        self.assertFalse(result["delete_preflight_ready"])

    def test_verified_receipt_must_match_mp4_digest(self):
        import hashlib
        with sqlite3.connect(self.engine_db) as db:
            db.execute("DELETE FROM job_sources")
            db.execute("DELETE FROM profile_sources")
        video=self.folder/"My Short 001_aa22bb.mp4"
        content=b"verified-video-test"*300
        video.write_bytes(content)
        sha=hashlib.sha256(content).hexdigest()
        self.jobs.append({
            "owner":"alice", "status":"completed",
            "config":{"short_vps_folder":"Owner Folder"},
            "outputs":[{
                "short_vps_folder":"Owner Folder",
                "short_vps_filename":video.name,
                "short_vps_sha256":sha
            }]
        })
        good=self.inspect()
        self.assertEqual(good["verified_media_count"],1)
        self.assertEqual(good["unverified_media_count"],0)
        self.assertNotIn("Folder contains unverified or changed MP4s",good["issues"])
        video.write_bytes(b"replacement"*1000)
        bad=self.inspect()
        self.assertEqual(bad["unverified_media_count"],1)
        self.assertIn("Folder contains unverified or changed MP4s",bad["issues"])
        self.assertFalse(bad["deletion_enabled"])

    def test_stale_uploader_video_index_blocks_even_without_claim(self):
        with sqlite3.connect(self.engine_db) as db:
            db.execute("DELETE FROM job_sources")
            db.execute("DELETE FROM profile_sources")
            db.execute("""INSERT INTO drive_videos VALUES
                (11,'previous.mp4','shortstudio-local:Owner Folder',1)""")
        report=self.inspect()
        self.assertEqual(report["indexed_uploader_videos"],1)
        self.assertIn("Short Uploader still indexes source MP4s",report["issues"])
        with sqlite3.connect(self.engine_db) as db:
            db.execute("UPDATE drive_videos SET present=0")
        self.assertEqual(self.inspect()["indexed_uploader_videos"],0)

    def test_missing_or_schema_incomplete_uploader_db_fails_closed(self):
        self.inspector.uploader_engine_db=""
        result=self.inspect()
        self.assertFalse(result["uploader_dependency_verified"])
        self.assertIsNone(result["active_uploader_claims"])
        self.assertIn("Short Uploader dependency verification unavailable",result["issues"])
        self.assertFalse(result["delete_preflight_ready"])
        self.inspector.uploader_engine_db=str(self.engine_db)
        with sqlite3.connect(self.engine_db) as db:
            db.execute("DROP TABLE job_sources")
        result=self.inspect()
        self.assertFalse(result["uploader_dependency_verified"])

    def test_invalid_owner_and_symlink_target_rejected(self):
        (self.folder/".studio_owner").write_text("bob")
        with self.assertRaises(HTTPException) as denied:
            self.inspect()
        self.assertEqual(denied.exception.status_code,403)
        (self.folder/".studio_owner").write_text("alice")
        (self.folder/"unsafe.mp4").symlink_to(self.app_db)
        result=self.inspect()
        self.assertIn("Folder has unverified or non-MP4 entries",result["issues"])

    def test_clean_fixture_still_cannot_delete_until_transactional_gate(self):
        with sqlite3.connect(self.engine_db) as db:
            db.execute("DELETE FROM job_sources")
            db.execute("DELETE FROM profile_sources")
        result=self.inspect()
        self.assertTrue(result["uploader_dependency_verified"])
        self.assertEqual(result["issues"],[])
        self.assertFalse(result["deletion_enabled"])
        self.assertFalse(result["delete_preflight_ready"])
        self.assertTrue(result["requires_confirmation"])
        self.assertTrue(self.folder.exists())


if __name__=="__main__":
    unittest.main()

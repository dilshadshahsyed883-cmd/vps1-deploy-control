"""Actual FastAPI unused-folder deletion, owner isolation and disabled default."""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT=Path(__file__).resolve().parents[1]/"apps/short-maker/part01"


class UnusedFolderRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.media=self.root/"media"
        self.media.mkdir()
        self.data=self.root/"maker"
        self.app_db=self.root/"uploader.db"
        self.engine_db=self.root/"engine.db"
        with sqlite3.connect(self.app_db) as db:
            db.executescript("""
                CREATE TABLE auto_engine_profiles(
                    id INTEGER PRIMARY KEY,category_name TEXT,enabled INTEGER,schedule_enabled INTEGER);
                CREATE TABLE studio_entity_owners(
                    entity_kind TEXT,entity_id INTEGER,owner_username TEXT);
                CREATE TABLE videos(
                    id INTEGER PRIMARY KEY,status TEXT,auto_engine_profile_id INTEGER);
            """)
        with sqlite3.connect(self.engine_db) as db:
            db.executescript("""
                CREATE TABLE profile_sources(profile_id INTEGER,short_folder_name TEXT);
                CREATE TABLE job_sources(video_id INTEGER,profile_id INTEGER,
                   source_folder_id TEXT,filename TEXT,state TEXT);
                CREATE TABLE drive_videos(id INTEGER PRIMARY KEY,profile_id INTEGER,
                   filename TEXT,source_folder_id TEXT,present INTEGER);
            """)
        self.apps=[]

    def tearDown(self):
        for app in self.apps:
            app.executor.shutdown(wait=True)
        self.temp.cleanup()

    def app(self,enabled):
        config={
            "SHORT_MAKER_DATA":str(self.data),
            "SHORT_STUDIO_VPS_FOLDER_ROOT":str(self.media),
            "SHORT_UPLOADER_APP_DB":str(self.app_db),
            "SHORT_UPLOADER_ENGINE_DB":str(self.engine_db),
            "SHORT_MAKER_COOPERATIVE_LOCK_ENABLED":"1" if enabled else "0",
            "SHORT_MAKER_UNUSED_FOLDER_DELETE_ENABLED":"1" if enabled else "0",
            "SHORT_MAKER_DAILY_PROFILES_ENABLED":"0",
        }
        with patch.dict(os.environ,config):
            spec=importlib.util.spec_from_file_location(
                "unused_delete_api_"+str(len(self.apps)),ROOT/"app.py")
            app=importlib.util.module_from_spec(spec)
            spec.loader.exec_module(app)
        self.apps.append(app)
        return app

    @staticmethod
    def headers(username):
        return {"x-auth-user":username}

    def create(self,client):
        response=client.post("/api/vps-folders",json={"name":"Unused Demo"},
                             headers=self.headers("alice"))
        self.assertEqual(response.status_code,200,response.text)
        self.assertTrue((self.media/"Unused Demo"/".studio_owner").is_file())

    def test_default_off_never_performs_deletion(self):
        maker=self.app(False)
        with TestClient(maker.app) as client:
            self.create(client)
            result=client.get("/api/vps-folders/Unused Demo/delete-preflight",
                              headers=self.headers("alice"))
            self.assertEqual(result.status_code,200,result.text)
            self.assertFalse(result.json()["deletion_enabled"])
            denied=client.post("/api/vps-folders/Unused Demo/retire-unused",
                headers=self.headers("alice"),
                json={"confirm_name":"Unused Demo"})
            self.assertEqual(denied.status_code,503)
            self.assertTrue((self.media/"Unused Demo").is_dir())

    def test_owner_only_clean_folder_and_confirmation(self):
        maker=self.app(True)
        with TestClient(maker.app) as client:
            self.create(client)
            foreign=client.get("/api/vps-folders/Unused Demo/delete-preflight",
                               headers=self.headers("bob"))
            self.assertEqual(foreign.status_code,403)
            denied=client.post("/api/vps-folders/Unused Demo/retire-unused",
                headers=self.headers("bob"),
                json={"confirm_name":"Unused Demo"})
            self.assertEqual(denied.status_code,403)
            report=client.get("/api/vps-folders/Unused Demo/delete-preflight",
                              headers=self.headers("alice"))
            self.assertEqual(report.status_code,200,report.text)
            self.assertTrue(report.json()["deletion_enabled"])
            self.assertEqual(report.json()["deletion_policy"],"unused_only")
            wrong=client.post("/api/vps-folders/Unused Demo/retire-unused",
                headers=self.headers("alice"),json={"confirm_name":"wrong"})
            self.assertEqual(wrong.status_code,409)
            done=client.post("/api/vps-folders/Unused Demo/retire-unused",
                headers=self.headers("alice"),json={"confirm_name":"Unused Demo"})
            self.assertEqual(done.status_code,200,done.text)
            self.assertEqual(done.json()["videos_deleted"],0)
            self.assertEqual(done.json()["linked_profiles_detached"],0)
            self.assertFalse(done.json()["profiles_modified"])
            self.assertFalse((self.media/"Unused Demo").exists())
            self.assertEqual(list(self.media.glob(".short-retirement-*")),[])

    def test_stopped_profile_blocks_undeclared_detachment(self):
        maker=self.app(True)
        with TestClient(maker.app) as client:
            self.create(client)
            profile=maker._sm_profile_store.create("alice",{
                "name":"Never detach","source_import_id":"a"*32,
                "output_folder":"Unused Demo","videos_per_day":1,
                "schedule_hhmm":"02:15"},
                lambda owner,sid:{"audio_count":1,"image_count":1},
                lambda folder:folder)
            denied=client.get("/api/vps-folders/Unused Demo/delete-preflight",
                              headers=self.headers("alice"))
            self.assertEqual(denied.status_code,200,denied.text)
            self.assertFalse(denied.json()["deletion_enabled"])
            self.assertIn("Short Profiles still link this folder",denied.json()["issues"])
            refused=client.post("/api/vps-folders/Unused Demo/retire-unused",
                headers=self.headers("alice"),json={"confirm_name":"Unused Demo"})
            self.assertEqual(refused.status_code,409,refused.text)
            self.assertEqual(maker._sm_profile_store.get("alice",profile["id"])["output_folder"],
                             "Unused Demo")
            self.assertTrue((self.media/"Unused Demo").is_dir())

    def test_uploader_cached_video_index_blocks_deletion(self):
        maker=self.app(True)
        with TestClient(maker.app) as client:
            self.create(client)
            with sqlite3.connect(self.engine_db) as db:
                db.execute("INSERT INTO drive_videos VALUES(1,7,?,?,1)",
                           ("a.mp4","shortstudio-local:Unused Demo"))
            report=client.get("/api/vps-folders/Unused Demo/delete-preflight",
                              headers=self.headers("alice"))
            self.assertFalse(report.json()["deletion_enabled"])
            refused=client.post("/api/vps-folders/Unused Demo/retire-unused",
                headers=self.headers("alice"),json={"confirm_name":"Unused Demo"})
            self.assertEqual(refused.status_code,409)
            with sqlite3.connect(self.engine_db) as db:
                self.assertEqual(db.execute(
                    "SELECT present FROM drive_videos WHERE id=1").fetchone(),(1,))
            self.assertTrue((self.media/"Unused Demo").is_dir())


if __name__=="__main__":
    unittest.main()

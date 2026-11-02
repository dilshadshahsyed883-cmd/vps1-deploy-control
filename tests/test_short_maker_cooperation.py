"""Guarded opt-in Short Maker cross-process lease; NEVER a production approval."""
import importlib.util
import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

ROOT=Path(__file__).resolve().parents[1]/"apps/short-maker/part01"
SOURCE=ROOT/"short_folder_cooperation.py"


def loaded(name="short_folder_cooperation"):
    spec=importlib.util.spec_from_file_location(name,SOURCE)
    mod=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.ShortStudioFolderLease


def attempt(root,kind,results):
    try:
        cls=loaded("short_folder_cooperation_worker")
        lock=cls(root,enabled=True,timeout_seconds=0.28)
        with (lock.retire() if kind=="exclusive" else lock.producer()):
            results.put("acquired")
    except HTTPException as e:
        results.put("denied-"+str(e.status_code))
    except Exception as e:
        results.put("unexpected-"+type(e).__name__)


class CooperationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)/"short-folders"
        self.root.mkdir()
        self.Lease=loaded()

    def tearDown(self):
        self.temp.cleanup()

    def child(self,kind):
        queue=multiprocessing.get_context("spawn").Queue()
        process=multiprocessing.get_context("spawn").Process(
            target=attempt,args=(str(self.root),kind,queue))
        process.start()
        process.join(timeout=8)
        self.assertFalse(process.is_alive(),"Leased helper hung")
        self.assertEqual(process.exitcode,0)
        return queue.get(timeout=2)

    def test_disabled_producer_never_creates_a_lock_file(self):
        lease=self.Lease(self.root,enabled=False)
        with lease.producer():
            self.assertFalse((self.root/".short-studio-folder-cooperation.lock").exists())
        with self.assertRaises(HTTPException) as e:
            with lease.retire():
                pass
        self.assertEqual(e.exception.status_code,503)

    def test_active_maker_shared_lock_blocks_other_process_retirement(self):
        lease=self.Lease(self.root,enabled=True)
        with lease.producer():
            self.assertEqual(self.child("exclusive"),"denied-503")
            self.assertEqual(self.child("shared"),"acquired")
        self.assertEqual(self.child("exclusive"),"acquired")

    def test_exclusive_retirement_blocks_new_producer(self):
        lease=self.Lease(self.root,enabled=True)
        with lease.retire():
            self.assertEqual(self.child("shared"),"denied-503")
        self.assertEqual(self.child("shared"),"acquired")

    def test_symlinked_lock_file_is_denied(self):
        victim=Path(self.temp.name)/"do-not-touch.txt"
        victim.write_text("protected")
        (self.root/".short-studio-folder-cooperation.lock").symlink_to(victim)
        lease=self.Lease(self.root,enabled=True)
        with self.assertRaises(OSError):
            with lease.producer(): pass
        self.assertEqual(victim.read_text(),"protected")

    def test_no_cooperation_when_root_is_missing_or_symlinked(self):
        victim=Path(self.temp.name)/"other-root"
        victim.mkdir()
        fake=Path(self.temp.name)/"linked-root"
        fake.symlink_to(victim,target_is_directory=True)
        for candidate in (Path(self.temp.name)/"missing-root",fake):
            with self.assertRaises(HTTPException):
                with self.Lease(candidate,enabled=True).producer():
                    pass

    def test_opted_in_maker_post_returns_503_not_500_when_exclusive_held(self):
        source=ROOT/"app.py"
        with patch.dict(os.environ, {
            "SHORT_MAKER_DATA": str(Path(self.temp.name)/"lock-enabled-data"),
            "SHORT_STUDIO_VPS_FOLDER_ROOT": str(self.root),
            "SHORT_MAKER_COOPERATIVE_LOCK_ENABLED":"1",
            "SHORT_MAKER_DAILY_PROFILES_ENABLED":"0",
        }):
            spec=importlib.util.spec_from_file_location("maker_lease_enabled",source)
            maker=importlib.util.module_from_spec(spec)
            spec.loader.exec_module(maker)
        try:
            self.assertTrue(maker._sm_cooperation_enabled)
            self.assertIsNot(maker.render_job,maker._sm_original_render_job)
            maker._sm_folder_cooperation.timeout=0.2
            with TestClient(maker.app) as client:
                with maker._sm_folder_cooperation.retire():
                    response=client.post("/api/vps-folders",
                        headers={"x-auth-user":"alice"},json={"name":"Blocked"})
                self.assertEqual(response.status_code,503,response.text)
                self.assertFalse((self.root/"Blocked").exists())
                allowed=client.post("/api/vps-folders",
                    headers={"x-auth-user":"alice"},json={"name":"Allowed"})
                self.assertEqual(allowed.status_code,200,allowed.text)
                self.assertTrue((self.root/"Allowed").is_dir())
        finally:
            maker.executor.shutdown(wait=True)

    def test_real_app_has_cooperation_disabled_by_default(self):
        source=ROOT/"app.py"
        with patch.dict(os.environ, {
            "SHORT_MAKER_DATA": str(Path(self.temp.name)/"data"),
            "SHORT_STUDIO_VPS_FOLDER_ROOT": str(self.root),
            "SHORT_MAKER_COOPERATIVE_LOCK_ENABLED":"0",
            "SHORT_MAKER_DAILY_PROFILES_ENABLED":"0",
        }):
            spec=importlib.util.spec_from_file_location("maker_lease_disabled",source)
            maker=importlib.util.module_from_spec(spec)
            spec.loader.exec_module(maker)
        try:
            self.assertFalse(maker._sm_cooperation_enabled)
            self.assertIs(maker.render_job,maker._sm_original_render_job
                if hasattr(maker,"_sm_original_render_job") else maker.render_job)
            with TestClient(maker.app) as client:
                result=client.get("/api/vps-folders",headers={"x-auth-user":"alice"})
                self.assertEqual(result.status_code,200)
        finally:
            maker.executor.shutdown(wait=True)


if __name__=="__main__":
    unittest.main()

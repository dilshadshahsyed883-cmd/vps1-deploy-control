"""Existing Short Maker folders: never count unverified files or foreign assets."""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None

SOURCE = Path(__file__).resolve().parents[1] / "apps/short-maker/part01/app.py"


@unittest.skipUnless(TestClient is not None, "FastAPI test dependencies not installed")
class VerifiedShortFolderInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        data = Path(self.temp.name)
        with patch.dict(os.environ, {"SHORT_MAKER_DATA":str(data/"data"),
                                   "SHORT_STUDIO_VPS_FOLDER_ROOT":str(data/"folders")}):
            spec = importlib.util.spec_from_file_location("short_inventory_" + str(id(self)), SOURCE)
            self.maker = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.maker)
        self.client = TestClient(self.maker.app)
        self.client.__enter__()
        self.owner = {"x-auth-user":"alice"}
        self.foreign = {"x-auth-user":"bob"}
        self.folder = self.maker.short_personal_default("alice")
        self.path = self.maker.short_vps_folder(self.folder)

    def tearDown(self):
        self.client.__exit__(None,None,None)
        self.maker.executor.shutdown(wait=True)
        self.temp.cleanup()

    def inventory(self, headers=None):
        return self.client.get("/api/vps-folders/verified-inventory",
                               headers=headers or self.owner)

    def test_only_recorded_published_outputs_count(self):
        (self.path/"unverified.mp4").write_bytes(b"x"*4096)
        (self.path/"partial.mp4.part").write_bytes(b"x"*4096)
        (self.path/"published.mp4").write_bytes(b"x"*4096)
        (self.path/"unsafe.mp4").symlink_to(self.path/"published.mp4")
        self.maker.set_state("a"*16, owner="alice", status="completed",
                             config={"short_vps_folder":self.folder},
                             published_outputs=["published.mp4","unsafe.mp4"])
        row = next(f for f in self.inventory().json()["folders"]
                   if f["name"]==self.folder)
        self.assertEqual(row["available_videos"],1)
        self.assertIsNone(row["linked_profiles"])
        self.assertFalse(row["delete_preflight_ready"])

    def test_completed_ready_upload_is_counted_but_incomplete_not(self):
        (self.path/"verified.mp4").write_bytes(b"x"*4096)
        (self.path/"waiting.mp4").write_bytes(b"x"*4096)
        for sid,status,filename in (("a"*32,"completed","verified.mp4"),
                                    ("b"*32,"uploading","waiting.mp4")):
            directory=self.maker.DATA_ROOT/"ready_video_uploads"/sid
            directory.mkdir(parents=True,exist_ok=True)
            (directory/"manifest.json").write_text(json.dumps({
                "owner":"alice","folder":self.folder,"status":status,
                "files":[{"name":"original.mp4","size":4096,"verified_name":filename}]
            }))
        row=next(f for f in self.inventory().json()["folders"]
                 if f["name"]==self.folder)
        self.assertEqual(row["available_videos"],1)
        self.assertTrue(row["has_active_maker_jobs_or_uploads"])

    def test_authenticated_dependency_preflight_never_deletes_or_exposes_foreign_data(self):
        created=self.client.post("/api/vps-folders",headers=self.owner,
            json={"name":"Owned Archive"})
        self.assertEqual(created.status_code,200)
        saved=self.maker.short_vps_folder("Owned Archive")
        (saved/"video.mp4").write_bytes(b"v"*4096)
        response=self.client.get("/api/vps-folders/Owned%20Archive/delete-preflight",
                                 headers=self.owner)
        self.assertEqual(response.status_code,200)
        result=response.json()
        self.assertEqual(result["folder"],"Owned Archive")
        self.assertFalse(result["deletion_enabled"])
        self.assertFalse(result["delete_preflight_ready"])
        self.assertFalse(result["uploader_dependency_verified"])
        self.assertIn("Short Uploader dependency verification unavailable",result["issues"])
        self.assertTrue(saved.exists())
        foreign=self.client.get("/api/vps-folders/Owned%20Archive/delete-preflight",
                                 headers=self.foreign)
        self.assertEqual(foreign.status_code,403)
        protected=self.client.get(
            "/api/vps-folders/"+self.folder+"/delete-preflight",headers=self.owner)
        self.assertEqual(protected.status_code,403)
        self.assertTrue((saved/"video.mp4").exists())

    def test_cross_user_sees_only_own_names(self):
        ours=self.inventory().json()
        theirs=self.inventory(self.foreign).json()
        self.assertIn(self.folder,[p["name"] for p in ours["folders"]])
        self.assertNotIn(self.folder,[p["name"] for p in theirs["folders"]])
        self.assertTrue(ours["verified_only"])
        self.assertFalse(ours["profile_dependencies_checked"])


if __name__=="__main__":
    unittest.main()

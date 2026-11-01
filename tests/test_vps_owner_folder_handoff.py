"""Offline two-identity VPS folder handoff, admission and verified-only cleanup."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
VM = ROOT / "apps/video-merger/part01/app.py"
COPY = ROOT / "apps/copyright-uploader/part01"

class VPSFolderHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        with patch.dict(os.environ, {
            "VIDEO_MERGER_ROOT": str(self.root),
            "VIDEO_MERGER_COPYRIGHT_ROOT": str(self.root / "copyright_handoff"),
            "VIDEO_MERGER_PROXY_CONFIG_DIR": str(self.root / "proxy"),
        }):
            spec = importlib.util.spec_from_file_location("vm_handoff_test", VM)
            self.vm = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.vm)
        self.vm.init_db()
        now = self.vm.utcnow()
        with self.vm.db() as db:
            for owner in ("alice", "bob"):
                db.execute("INSERT INTO library_items(id,name,filename,owner_username,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                           (owner+"lib",owner+" library",owner+".mp4",owner,now,now))

    def payload(self, owner, folder="", key=None):
        return {
            "videos":[{"kind":"library","library_id":owner+"lib"},
                      {"kind":"url","url":"https://example.org/file.mp4"}],
            "aspect":"9:16","quality":"720p","filename_mode":"auto",
            "folder":"","vps_output_folder":folder,"upload_to_drive":False,
            "request_key":key if key is not None else "x"*24,
        }

    def test_owner_folders_and_queue_admission_are_atomic_and_network_free(self):
        vm=self.vm
        own=vm.create_vps_folder("alice","First/Output")
        other=vm.create_vps_folder("bob","First/Output")
        self.assertEqual(own,other)
        self.assertEqual(vm.list_vps_folders("alice"),["","First/Output"])
        with self.assertRaises(ValueError):
            vm.resolve_vps_folder("bob","Alice Secret")
        for value in ("../../bob","foo//bar","/tmp", ".hidden"):
            with self.subTest(folder=value), self.assertRaises(ValueError):
                vm.create_vps_folder("alice",value)
        with patch.object(vm,"source_display_filename",side_effect=AssertionError("No external network during admission")):
            first=vm.add_job(self.payload("alice",own),owner="alice")
            retry=vm.add_job(self.payload("alice",own),owner="alice")
        self.assertEqual(first,retry)
        self.assertEqual(len(vm.list_jobs("alice")),1)
        self.assertEqual(vm.get_job(first[0])["filename_mode"],"auto")
        self.assertEqual(vm.get_job(first[0])["vps_output_folder"],own)
        # Even an administrator cannot assign an output directory owned by another account.
        with self.assertRaises(ValueError):
            vm.add_job(self.payload("bob",own,key="y"*24),owner="alice")
        # Attempt to remove a folder referenced by a waiting job must be denied.
        with self.assertRaises(ValueError):
            vm.delete_vps_folder("alice",own)
        bob=vm.add_job(self.payload("bob",other),owner="bob")
        self.assertNotEqual(first[0],bob[0])
        self.assertEqual(len(vm.list_jobs("bob")),1)
        vm.cancel_job(first[0],owner="alice")
        folder=vm.resolve_vps_folder("alice",own)
        (folder/"important.mp4").write_bytes(b"retain")
        with self.assertRaises(OSError):
            vm.delete_vps_folder("alice",own)
        self.assertTrue((folder/"important.mp4").exists())

    def test_verified_cleanup_requires_owner_sha_and_complete_job(self):
        program = r"""
import hashlib, json, os, sqlite3
from pathlib import Path
from lum_engine.copyright_handoff import (owner_folder, available_folders,
    available_videos, delete_verified_source, delete_verified_merger_output)
owner="alice"
root=owner_folder(owner,"My Output")
root.mkdir(parents=True,exist_ok=True)
jobid="a"*12
filename=jobid+"__merged.mp4"
source=root/filename
source.write_bytes(b"verified-fixture")
digest=hashlib.sha256(source.read_bytes()).hexdigest()
other=owner_folder("bob","My Output")
other.mkdir(parents=True,exist_ok=True)
(other/filename).write_bytes(b"other-owner-video")
assert "My Output" in available_folders(owner)
assert available_videos(owner,set(),"My Output")==[filename]
base=Path(os.environ["VIDEO_MERGER_ROOT"])
master=base/"verified_outputs"/jobid
master.mkdir(parents=True,exist_ok=True)
(master/"merged.mp4").write_bytes(source.read_bytes())
database=base/"data"/"video_merger.db"
with sqlite3.connect(database) as c:
 c.execute("INSERT INTO jobs(id,name,videos_json,aspect,quality,fit_mode,folder,filename,status,order_idx,owner_username,local_path,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
  (jobid,"merged.mp4","[]","9:16","720p","crop","","merged.mp4","complete",100,owner,str(master/"merged.mp4"),"now","now"))
try:
 delete_verified_source(owner,filename,"0"*64,"My Output")
 raise AssertionError("Wrong checksum accepted")
except RuntimeError: pass
assert source.exists()
try:
 delete_verified_merger_output("bob",filename,digest)
 raise AssertionError("Cross-user master deletion allowed")
except RuntimeError: pass
assert (master/"merged.mp4").exists()
assert delete_verified_source(owner,filename,digest,"My Output")
assert delete_verified_merger_output(owner,filename,digest)
assert not source.exists() and not (master/"merged.mp4").exists()
assert (other/filename).exists()
assert delete_verified_source(owner,filename,digest,"My Output") is False
print("CLEANUP_OWNER_SHA=PASS")
"""
        env={**os.environ,
             "VIDEO_MERGER_ROOT":str(self.root),
             "COPYRIGHT_MEDIA_ROOT":str(self.root/"copyright_handoff"),
             "LUM_DATA_DIR":str(self.root/"copyright_data"),
             "YOUTUBE_UPLOAD_MANAGER_DATA_DIR":str(self.root/"core"),
             "LUM_ENGINE_INSTANCE":"copyright",
             "PYTHONDONTWRITEBYTECODE":"1"}
        p=subprocess.run([sys.executable,"-c",program],cwd=str(COPY),env=env,text=True,capture_output=True)
        self.assertEqual(p.returncode,0,p.stdout+p.stderr)
        self.assertIn("CLEANUP_OWNER_SHA=PASS",p.stdout)

if __name__=="__main__":
    unittest.main()

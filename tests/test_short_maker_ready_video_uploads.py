"""Offline tests for owned ready-made video sessions and atomic handoff."""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

PATH = Path(__file__).resolve().parents[1] / "apps/short-maker/part01/ready_video_uploads.py"


class ReadyVideoUploadsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.folders = self.root / "folders"
        self.folders.mkdir()
        self.owned = self.folders / "Own Output"
        self.owned.mkdir()
        self.owned.joinpath(".studio_owner").write_text("alice", encoding="utf-8")
        spec = importlib.util.spec_from_file_location("ready_upload_tests", PATH)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.seq = 0
        self.app = FastAPI()

        def identity(request: Request):
            owner = request.headers.get("x-auth-user")
            if not owner:
                raise HTTPException(401, detail="Login required")
            return owner, False

        def folder(name, request):
            owner, _ = identity(request)
            p = self.folders / name
            if not p.is_dir() or p.is_symlink():
                raise HTTPException(404, detail="Folder missing")
            marker = p / ".studio_owner"
            if marker.is_symlink() or not marker.is_file() or marker.read_text().strip() != owner:
                raise HTTPException(403, detail="Foreign folder")
            return p

        def reserve(foldername, count):
            result = []
            for _ in range(count):
                self.seq += 1
                result.append("%s %03d_abcdef.mp4" % (foldername, self.seq))
            return result

        self.module.register_ready_video_uploads(
            self.app, data_root=self.root, short_identity=identity,
            short_folder_owned=folder, short_vps_folder=lambda n:self.folders/n,
            reserve_short_output_names=reserve)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.headers = {"x-auth-user": "alice"}
        self.other = {"x-auth-user": "bob"}
        self.space = patch.object(self.module.shutil, "disk_usage",
                                  return_value=SimpleNamespace(free=20_000_000_000))
        self.space.start()

    def tearDown(self):
        self.space.stop()
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def begin(self, names=("one.mp4",), size=4096, request_id=""):
        return self.client.post("/api/ready-videos/sessions", headers=self.headers,
            json={"folder":"Own Output", "files":[{"name":n,"size":size} for n in names],
                  "client_request_id":request_id})

    def endpoint(self, session_id):
        return "/api/ready-videos/sessions/" + session_id

    def put(self, session_id, index, value, offset=0, user=None):
        return self.client.put(self.endpoint(session_id)+"/files/"+str(index),
            headers={**(user or self.headers), "x-upload-offset":str(offset)}, content=value)

    def test_two_distinct_same_name_files_publish_once_without_overwrite(self):
        begin = self.begin(("same.mp4", "same.mp4"))
        self.assertEqual(begin.status_code, 200, begin.text)
        sid = begin.json()["session_id"]
        self.assertEqual(self.put(sid, 0, b"a"*4096).status_code, 200)
        self.assertEqual(self.put(sid, 1, b"b"*4096).status_code, 200)
        with patch.object(self.module.subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stdout="video\n")):
            done = self.client.post(self.endpoint(sid)+"/finalize", headers=self.headers)
        self.assertEqual(done.status_code, 200, done.text)
        files = done.json()["files"]
        self.assertEqual(len({r["verified_name"] for r in files}), 2)
        self.assertEqual([r["uploaded"] for r in files], [4096, 4096])
        self.assertEqual((self.owned / files[0]["verified_name"]).read_bytes(), b"a"*4096)
        self.assertEqual((self.owned / files[1]["verified_name"]).read_bytes(), b"b"*4096)
        again = self.client.post(self.endpoint(sid)+"/finalize", headers=self.headers)
        self.assertEqual(again.status_code, 200)
        self.assertEqual(self.seq, 2)

    def test_foreign_user_cannot_read_transfer_or_publish(self):
        sid = self.begin().json()["session_id"]
        self.assertEqual(self.client.get(self.endpoint(sid), headers=self.other).status_code, 403)
        self.assertEqual(self.put(sid, 0, b"a"*4096, user=self.other).status_code, 403)
        self.assertEqual(self.client.post(self.endpoint(sid)+"/finalize", headers=self.other).status_code, 403)
        self.assertEqual(self.client.post("/api/ready-videos/sessions", headers=self.other,
            json={"folder":"Own Output","files":[{"name":"new.mp4","size":4096}]}).status_code, 403)

    def test_offset_pause_resume_and_incomplete_finalize(self):
        sid = self.begin().json()["session_id"]
        self.assertEqual(self.put(sid, 0, b"a"*1024, offset=23).status_code, 409)
        self.assertEqual(self.put(sid, 0, b"a"*1024).status_code, 200)
        self.assertEqual(self.client.post(self.endpoint(sid)+"/pause", headers=self.headers).json()["status"],"paused")
        self.assertEqual(self.put(sid, 0, b"a"*1024, offset=1024).status_code, 409)
        self.assertEqual(self.client.post(self.endpoint(sid)+"/resume", headers=self.headers).json()["status"],"uploading")
        self.assertEqual(self.client.post(self.endpoint(sid)+"/finalize", headers=self.headers).status_code, 409)
        self.assertEqual(self.put(sid, 0, b"a"*3072, offset=1024).status_code, 200)
        snap = self.client.get(self.endpoint(sid), headers=self.headers).json()
        self.assertEqual(snap["uploaded_bytes"],4096)

    def test_request_id_idempotence_and_no_arbitrary_file_count_cap(self):
        key = "123e4567-e89b-12d3-a456-426614174000"
        first = self.begin(request_id=key)
        self.assertEqual(first.status_code, 200)
        second = self.begin(request_id=key)
        self.assertEqual(first.json()["session_id"], second.json()["session_id"])
        conflict = self.begin(names=("other.mp4",),request_id=key)
        self.assertEqual(conflict.status_code, 409)
        many = self.begin(names=tuple("clip_%04d.mp4"%i for i in range(1001)),size=1024)
        self.assertEqual(many.status_code, 200, many.text)
        self.assertEqual(len(many.json()["files"]),1001)

    def test_invalid_file_and_cancel_protects_owned_output(self):
        bad = self.begin(names=("../bad.mp4",))
        self.assertEqual(bad.status_code, 400)
        sid = self.begin().json()["session_id"]
        self.assertEqual(self.put(sid, 0, b"a"*1024).status_code, 200)
        stopped = self.client.delete(self.endpoint(sid), headers=self.headers)
        self.assertEqual(stopped.status_code, 200)
        self.assertEqual(self.put(sid, 0, b"a"*1024, offset=1024).status_code, 409)
        self.assertFalse((self.owned / "one.mp4").exists())


if __name__ == "__main__":
    unittest.main()

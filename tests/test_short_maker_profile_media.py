"""Resumable Short Profile voice/picture import and owner isolation tests."""
from __future__ import annotations

import importlib.util
import io
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from PIL import Image

MEDIA = Path(__file__).resolve().parents[1] / "apps/short-maker/part01/short_profile_media.py"


class ShortProfileMediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        spec = importlib.util.spec_from_file_location("short_profile_media_test", MEDIA)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.root = Path(self.temp.name)
        self.app = FastAPI()

        def identity(request: Request):
            owner = request.headers.get("x-auth-user")
            if not owner:
                raise HTTPException(401, detail="Login required")
            return owner, False

        self.verify = self.module.register_short_profile_media_uploads(
            self.app, self.root, identity)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.alice = {"x-auth-user": "Alice"}
        self.bob = {"x-auth-user": "bob"}
        with io.BytesIO() as image:
            Image.new("RGB", (200, 200)).save(image, format="PNG")
            self.picture = image.getvalue()
        self.voice = b"x" * 1024
        self.space = patch.object(self.module.shutil, "disk_usage",
            return_value=SimpleNamespace(free=10_000_000_000))
        self.space.start()

    def tearDown(self):
        self.space.stop()
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def begin(self, headers=None, request_id=None):
        return self.client.post("/api/short-profile-media/sessions",
            headers=headers or self.alice, json={
                "client_request_id": request_id or str(uuid.uuid4()),
                "files": [
                    {"name": "voice.mp3", "size":len(self.voice)},
                    {"name": "image.png", "size":len(self.picture)},
                ]
            })

    def route(self, sid):
        return "/api/short-profile-media/sessions/" + sid

    def put(self, sid, i, data, offset=0, headers=None):
        return self.client.put(self.route(sid)+"/files/"+str(i),
            headers={**(headers or self.alice),"x-upload-offset":str(offset)},
            content=data)

    def test_verified_receipt_only_after_completed_audio_picture_import(self):
        started = self.begin()
        self.assertEqual(started.status_code, 200, started.text)
        sid = started.json()["session_id"]
        self.assertIsNone(self.verify("alice", sid))
        self.assertEqual(self.put(sid,0,self.voice[:512]).json()["uploaded"],512)
        self.assertEqual(self.put(sid,0,self.voice[512:],offset=512).status_code,200)
        self.assertEqual(self.put(sid,1,self.picture).status_code,200)
        with patch.object(self.module.subprocess,"run",
                          return_value=SimpleNamespace(returncode=0,stdout="audio\n")):
            done=self.client.post(self.route(sid)+"/finalize",headers=self.alice)
        self.assertEqual(done.status_code,200,done.text)
        self.assertEqual(done.json()["verified_audio_count"],1)
        self.assertEqual(done.json()["verified_picture_count"],1)
        self.assertEqual(self.verify("alice",sid),{"audio_count":1,"image_count":1})
        self.assertIsNone(self.verify("bob",sid))
        self.assertEqual(self.client.post(self.route(sid)+"/finalize",
                         headers=self.alice).status_code,200)
        self.assertEqual(self.client.request("DELETE", self.route(sid),
                         headers=self.alice).status_code,409)
        self.assertEqual(list((self.root/"short_profile_imports"/sid).glob("*.part")),[])

    def test_owner_isolation_prevents_foreign_session_lock_and_media_access(self):
        sid=self.begin().json()["session_id"]
        for method, url in (
            ("GET",self.route(sid)), ("POST",self.route(sid)+"/finalize"),
            ("PUT",self.route(sid)+"/files/0"),
            ("POST",self.route(sid)+"/pause"),
            ("POST",self.route(sid)+"/resume"),
            ("DELETE",self.route(sid)),
        ):
            response=self.client.request(method,url,headers={
                **self.bob,"x-upload-offset":"0"},content=b"x"*128
                if method=="PUT" else None)
            self.assertEqual(response.status_code,404,(method,response.text))
        self.assertIsNone(self.verify("bob",sid))
        self.assertEqual(self.client.get(self.route("f"*32),
                         headers=self.alice).status_code,404)

    def test_resumable_chunks_pause_idempotency_and_cancel(self):
        request_id=str(uuid.uuid4())
        first=self.begin(request_id=request_id)
        sid=first.json()["session_id"]
        self.assertEqual(self.begin(request_id=request_id).json()["session_id"],sid)
        self.assertEqual(self.put(sid,0,self.voice[:200]).status_code,200)
        self.assertEqual(self.put(sid,0,self.voice[:200]).status_code,409)
        self.assertEqual(self.client.post(self.route(sid)+"/pause",
                         headers=self.alice).json()["status"],"paused")
        self.assertEqual(self.put(sid,0,self.voice[200:],offset=200).status_code,409)
        self.assertEqual(self.client.post(self.route(sid)+"/resume",
                         headers=self.alice).json()["status"],"uploading")
        progress=self.client.get(self.route(sid),headers=self.alice).json()
        self.assertEqual(progress["files"][0]["uploaded"],200)
        self.assertEqual(self.client.request("DELETE",self.route(sid),
                         headers=self.alice).json()["status"],"cancelled")
        self.assertIsNone(self.verify("alice",sid))
        self.assertEqual(list((self.root/"short_profile_imports"/sid).glob("*.part")),[])

    def test_invalid_media_kept_unverified_without_publishing(self):
        sid=self.begin().json()["session_id"]
        self.put(sid,0,self.voice)
        self.put(sid,1,b"x"*len(self.picture))
        with patch.object(self.module.subprocess,"run",
                          return_value=SimpleNamespace(returncode=0,stdout="audio\n")):
            response=self.client.post(self.route(sid)+"/finalize",headers=self.alice)
        self.assertEqual(response.status_code,422,response.text)
        self.assertIsNone(self.verify("alice",sid))
        self.assertEqual(list((self.root/"short_profile_library").rglob("*.png")),[])

    def test_disk_reserve_accounts_for_publish_copy(self):
        # Peak usage includes source staging plus output bytes.
        total = len(self.voice) + len(self.picture)
        free = self.module.DISK_RESERVE_BYTES + total + 512
        with patch.object(self.module.shutil, "disk_usage",
                          return_value=SimpleNamespace(free=free)):
            response = self.begin()
        self.assertEqual(response.status_code, 507, response.text)

    def test_source_filename_traversal_and_missing_types_rejected(self):
        bad=self.client.post("/api/short-profile-media/sessions",headers=self.alice,
            json={"files":[{"name":"../audio.mp3","size":1024},
                           {"name":"image.png","size":1024}]})
        self.assertEqual(bad.status_code,400)
        missing=self.client.post("/api/short-profile-media/sessions",headers=self.alice,
            json={"files":[{"name":"audio.mp3","size":1024}]})
        self.assertEqual(missing.status_code,400)
        self.assertEqual(self.client.post("/api/short-profile-media/sessions",
                         json={"files":[]}).status_code,401)


if __name__ == "__main__":
    unittest.main()

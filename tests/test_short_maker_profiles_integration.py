"""Integrated Short Maker source media -> verified receipt -> owner profile save."""
from __future__ import annotations

import importlib.util
import io
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

SOURCE = Path(__file__).resolve().parents[1] / "apps/short-maker/part01/app.py"


class ShortProfileEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        with patch.dict(os.environ, {
            "SHORT_MAKER_DATA": str(root / "data"),
            "SHORT_STUDIO_VPS_FOLDER_ROOT": str(root / "folders"),
        }):
            spec = importlib.util.spec_from_file_location("short_profile_integration_"+uuid.uuid4().hex, SOURCE)
            self.maker = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.maker)
        self.maker.executor.shutdown(wait=True)  # no new jobs; avoid startup workers from existing tests
        self.alice = {"x-auth-user": "alice"}
        self.bob = {"x-auth-user": "bob", "x-is-admin": "1"}
        with io.BytesIO() as f:
            Image.new("RGB", (160, 160)).save(f, "PNG")
            self.picture = f.getvalue()
        self.voice = b"voice-data"*150
        self.client = TestClient(self.maker.app)
        self.client.__enter__()
        self.folder = self.maker.short_personal_default("alice")

    def tearDown(self):
        self.client.__exit__(None,None,None)
        self.tmp.cleanup()

    def test_owned_verified_import_can_create_profile_but_unverified_cannot(self):
        base = "/api/short-profile-media/sessions"
        files = [
            {"name": "narration.mp3", "size": len(self.voice)},
            {"name": "cover.png", "size": len(self.picture)},
        ]
        with patch.object(self.maker._sm_profile_media_module.shutil, "disk_usage",
                          return_value=SimpleNamespace(free=10_000_000_000)):
            begun = self.client.post(base, headers=self.alice, json={
                "files": files, "client_request_id": str(uuid.uuid4())})
        self.assertEqual(begun.status_code, 200, begun.text)
        sid = begun.json()["session_id"]
        payload = {"name":"Verified from PC folder", "source_import_id": sid,
                   "output_folder": self.folder, "videos_per_day":23,
                   "schedule_hhmm":"00:00"}
        endpoint = "/api/short-profiles"
        self.assertEqual(self.client.post(endpoint,headers=self.alice,
                         json=payload).status_code, 409)
        self.assertEqual(self.client.put(base+"/"+sid+"/files/0",
                         headers={**self.alice, "x-upload-offset":"0"},
                         content=self.voice).status_code,200)
        self.assertEqual(self.client.put(base+"/"+sid+"/files/1",
                         headers={**self.alice, "x-upload-offset":"0"},
                         content=self.picture).status_code,200)
        with patch.object(self.maker._sm_profile_media_module.subprocess, "run",
                          return_value=SimpleNamespace(returncode=0, stdout="audio\n")):
            verified = self.client.post(base+"/"+sid+"/finalize",headers=self.alice)
        self.assertEqual(verified.status_code,200,verified.text)
        self.assertEqual(verified.json()["verified_audio_count"],1)
        created=self.client.post(endpoint,headers=self.alice,json=payload)
        self.assertEqual(created.status_code,200,created.text)
        self.assertEqual(created.json()["source_import_id"],sid)
        self.assertEqual(created.json()["enabled"],0)
        self.assertEqual(self.client.get(endpoint,headers=self.bob).json()["profiles"],[])
        self.assertEqual(self.client.post(endpoint,headers=self.bob,
                         json=payload).status_code,409)
        self.assertEqual(self.client.post(endpoint+"/"+created.json()["id"]+"/start",
                         headers=self.alice).status_code,503)
        self.assertEqual(self.client.get("/health").status_code,200)


if __name__=="__main__":
    unittest.main()

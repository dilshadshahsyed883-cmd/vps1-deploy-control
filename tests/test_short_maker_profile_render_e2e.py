"""Real Short Profile path: authenticated source import -> PKT Start -> FFmpeg MP4.

Runs only in a temporary, isolated TestClient app with the release flag
temporarily enabled. No production or user media, uploader worker, YouTube
publish, OAuth, Drive, or legacy job is accessed.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid
import wave
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

ROOT = Path(__file__).resolve().parents[1] / "apps/short-maker/part01/app.py"


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "Real FFmpeg and FFprobe must be installed")
class ProfileRenderedMp4Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        with patch.dict(os.environ, {
            "SHORT_MAKER_DATA": str(self.root / "data"),
            "SHORT_STUDIO_VPS_FOLDER_ROOT": str(self.root / "folders"),
            "SHORT_MAKER_DAILY_PROFILES_ENABLED": "1",
        }):
            spec=importlib.util.spec_from_file_location(
                "short_profile_render_"+uuid.uuid4().hex, ROOT)
            self.maker=importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.maker)
        self.client=TestClient(self.maker.app)
        self.client.__enter__()
        self.alice={"x-auth-user":"alice"}
        self.bob={"x-auth-user":"bob","x-is-admin":"1"}
        self.folder=self.client.get("/api/vps-folders",headers=self.alice).json()["folders"][0]
        with io.BytesIO() as output:
            image=Image.new("RGB",(320,512),(30,84,140))
            image.save(output,format="PNG")
            self.picture=output.getvalue()
        with io.BytesIO() as output:
            with wave.open(output,"wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(24000)
                wav.writeframes(b"\x00\x00" * (24000 * 2))
            self.voice=output.getvalue()

    def tearDown(self):
        self.client.__exit__(None,None,None)
        self.maker.executor.shutdown(wait=True)
        self.temp.cleanup()

    def _import_source(self):
        base="/api/short-profile-media/sessions"
        payload={"client_request_id":str(uuid.uuid4()),"files":[
            {"name":"voice.wav","size":len(self.voice)},
            {"name":"cover.png","size":len(self.picture)},
        ]}
        create=self.client.post(base,json=payload,headers=self.alice)
        self.assertEqual(create.status_code,200,create.text)
        sid=create.json()["session_id"]
        for idx,raw in enumerate((self.voice,self.picture)):
            response=self.client.put(
                f"{base}/{sid}/files/{idx}",
                headers={**self.alice,"x-upload-offset":"0"},content=raw)
            self.assertEqual(response.status_code,200,response.text)
        completed=self.client.post(base+"/"+sid+"/finalize",headers=self.alice)
        self.assertEqual(completed.status_code,200,completed.text)
        self.assertEqual(completed.json()["verified_audio_count"],1)
        self.assertEqual(completed.json()["verified_picture_count"],1)
        return sid

    def test_real_pcm_voice_image_profile_renders_verified_mp4_only_once(self):
        receipt=self._import_source()
        profile_payload={
            "name":"Verified PKT Render",
            "source_import_id":receipt,
            "output_folder":self.folder,
            "videos_per_day":1,
            "schedule_hhmm":"00:00",
        }
        created=self.client.post(
            "/api/short-profiles",json=profile_payload,headers=self.alice)
        self.assertEqual(created.status_code,200,created.text)
        pid=created.json()["id"]
        self.assertEqual(created.json()["enabled"],0)
        self.assertEqual(
            self.client.post("/api/short-profiles/"+pid+"/start",headers=self.bob).status_code,404)

        start=self.client.post("/api/short-profiles/"+pid+"/start",headers=self.alice)
        self.assertEqual(start.status_code,200,start.text)
        self.assertEqual(start.json()["target"],1)
        self.assertEqual(start.json()["queued_or_existing"],1)
        self.assertEqual(start.json()["shortage"],0)

        deadline=time.monotonic()+80
        job=None
        while time.monotonic()<deadline:
            jobs=self.client.get("/api/jobs",headers=self.alice).json()["jobs"]
            profile_jobs=[j for j in jobs if j.get("short_profile_id")==pid]
            if profile_jobs:
                job=profile_jobs[0]
                if job["status"] in ("completed","failed","cancelled"):
                    break
            time.sleep(0.25)
        self.assertIsNotNone(job,"Profile job was not persisted to existing Maker queue")
        self.assertEqual(job["status"],"completed",str(job.get("error") or job.get("message")))
        outputs=job["outputs"]
        self.assertEqual(len(outputs),1)
        self.assertEqual(outputs[0]["short_vps_folder"],self.folder)
        name=outputs[0]["short_vps_filename"]
        target=self.maker.short_vps_folder(self.folder)/name
        self.assertTrue(target.is_file())
        self.assertGreater(target.stat().st_size,1024)
        self.assertEqual(outputs[0]["short_vps_sha256"],
                         hashlib.sha256(target.read_bytes()).hexdigest())
        info=subprocess.run([
            shutil.which("ffprobe"),"-v","error","-show_entries",
            "stream=codec_type","-of","default=nokey=1:noprint_wrappers=1",
            str(target)],capture_output=True,text=True,timeout=15)
        self.assertEqual(info.returncode,0,info.stderr)
        self.assertIn("video",info.stdout.splitlines())
        self.assertIn("audio",info.stdout.splitlines())

        same_day=self.client.post("/api/short-profiles/"+pid+"/start",headers=self.alice)
        self.assertEqual(same_day.status_code,200,same_day.text)
        self.assertEqual(same_day.json()["queued_or_existing"],1)
        owned=self.client.get("/api/jobs",headers=self.alice).json()["jobs"]
        self.assertEqual(len([j for j in owned if j.get("short_profile_id")==pid]),1)
        self.assertEqual(self.client.get("/api/jobs",headers=self.bob).json()["jobs"],[])
        history=self.client.get("/api/short-profiles/"+pid+"/history",headers=self.alice)
        self.assertEqual(history.status_code,200)
        self.assertEqual(len(history.json()["history"]),1)
        owned_summary=self.client.get("/api/short-profiles",headers=self.alice).json()
        self.assertTrue(owned_summary["scheduler_active"])
        self.assertEqual(owned_summary["profiles"][0]["daily_summary"]["today_completed"],1)


if __name__=="__main__":
    unittest.main()

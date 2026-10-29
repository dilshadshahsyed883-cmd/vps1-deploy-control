"""Optional Audio Downloader thumbnail pairing, naming, and safety contracts."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "apps/voice-studio/part01/audio_downloader_api.py"
TEMPLATE = ROOT / "apps/voice-studio/part01/templates/voice_studio.html"
VIDEO = "BaW_jenozKc"
JPG = b"\xff\xd8\xff" + b"x" * 2048
MP3 = b"ID3" + b"x" * 2048


class AudioThumbnailTests(unittest.TestCase):
    def setUp(self):
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("fastapi/httpx required")
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"AUDIO_DOWNLOADER_ROOT": self.tmp.name})
        self.env.start()
        spec = importlib.util.spec_from_file_location("thumbnail_test_audio", MODULE)
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)
        self.m.init_storage()
        app = FastAPI()
        app.include_router(self.m.router)
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.env.stop()
        self.tmp.cleanup()

    def job(self, thumb=False, upload=False, folder="YouTube Audio", project="Audio Batch"):
        return dict(job_id="ADJ-" + "B" * 16, video_id=VIDEO,
                    url="https://www.youtube.com/watch?v=" + VIDEO,
                    format="mp3", quality=192, keep_metadata=False,
                    auto_upload=upload, download_thumbnail=thumb,
                    folder=folder, project=project)

    def fake_download(self, args, **kw):
        directory = Path(kw["cwd"])
        audio = directory / ("Title [" + VIDEO + "].mp3")
        audio.write_bytes(MP3)
        if "--write-thumbnail" in args:
            self.assertIn("--convert-thumbnails", args)
            (directory / ("Title [" + VIDEO + "].jpg")).write_bytes(JPG)
        return subprocess.CompletedProcess(args, 0, str(audio) + "\n", "")

    def test_opt_in_defaults_off_and_db_migrates_without_losing_jobs(self):
        self.m.init_storage()
        with self.m.db() as db:
            columns = {r[1] for r in db.execute("PRAGMA table_info(jobs)")}
        self.assertTrue({"download_thumbnail", "local_thumbnail",
                         "thumbnail_drive_path", "thumbnail_drive_url"}.issubset(columns))
        self.m.BatchRequest.model_rebuild(_types_namespace=vars(self.m))
        with patch.object(self.m, "deps", return_value={"ready": True, "upload_ready": True}):
            response = self.m.enqueue(self.m.BatchRequest(
                links=["https://youtu.be/" + VIDEO], auto_upload=False,
                permission_confirmed=True))
        self.assertEqual(response["created"], 1)
        jobs = self.client.get("/tts/api/audio-downloader/jobs").json()["jobs"]
        self.assertFalse(jobs[0]["download_thumbnail"])
        self.assertIsNone(jobs[0]["thumbnail_url"])
        self.assertNotIn("local_thumbnail", jobs[0])
        self.m.init_storage()
        self.assertEqual(len(self.client.get("/tts/api/audio-downloader/jobs").json()["jobs"]), 1)

    def test_checkbox_on_persists_through_queue_and_retry(self):
        self.m.BatchRequest.model_rebuild(_types_namespace=vars(self.m))
        with patch.object(self.m, "deps", return_value={"ready": True, "upload_ready": True}):
            response = self.m.enqueue(self.m.BatchRequest(
                links=["https://youtu.be/" + VIDEO], auto_upload=False,
                download_thumbnail=True, permission_confirmed=True))
        self.assertEqual(response["created"], 1)
        jid = response["job_ids"][0]
        self.assertTrue(self.client.get("/tts/api/audio-downloader/jobs/" + jid).json()["download_thumbnail"])
        self.assertEqual(self.client.post("/tts/api/audio-downloader/jobs/" + jid + "/cancel").status_code, 200)
        self.assertEqual(self.client.post("/tts/api/audio-downloader/jobs/" + jid + "/retry").status_code, 200)
        self.assertTrue(self.client.get("/tts/api/audio-downloader/jobs/" + jid).json()["download_thumbnail"])

    def test_local_only_audio_and_thumbnail_are_same_video_id(self):
        updates = []
        with patch.object(self.m, "deps", return_value={"ytdlp": "/fake/yt-dlp", "ffmpeg": "/fake/ffmpeg"}), \
             patch.object(self.m.subprocess, "run", side_effect=self.fake_download), \
             patch.object(self.m, "_update", side_effect=lambda jid, state, **kw: updates.append((state, kw))):
            self.m.run_job(self.job(thumb=True))
        self.assertEqual([x[0] for x in updates], ["downloaded", "completed"])
        self.assertEqual(Path(updates[0][1]["local_file"]).name, VIDEO + ".mp3")
        self.assertEqual(Path(updates[0][1]["local_thumbnail"]).name, VIDEO + ".jpg")
        self.assertTrue(Path(updates[0][1]["local_thumbnail"]).exists())

    def test_opt_out_does_not_fetch_or_create_thumbnail(self):
        updates, calls = [], []
        def execute(args, **kw):
            calls.append(args)
            return self.fake_download(args, **kw)
        with patch.object(self.m, "deps", return_value={"ytdlp": "/fake/yt-dlp", "ffmpeg": "/fake/ffmpeg"}), \
             patch.object(self.m.subprocess, "run", side_effect=execute), \
             patch.object(self.m, "_update", side_effect=lambda jid, state, **kw: updates.append((state, kw))):
            self.m.run_job(self.job())
        self.assertNotIn("--write-thumbnail", calls[0])
        self.assertIsNone(updates[0][1]["local_thumbnail"])

    def test_missing_requested_thumbnail_fails_not_false_success(self):
        def execute(args, **kw):
            out = Path(kw["cwd"]) / "Test.mp3"
            out.write_bytes(MP3)
            return subprocess.CompletedProcess(args, 0, str(out) + "\n", "")
        with patch.object(self.m, "deps", return_value={"ytdlp": "/fake/yt-dlp", "ffmpeg": "/fake/ffmpeg"}), \
             patch.object(self.m.subprocess, "run", side_effect=execute):
            with self.assertRaisesRegex(RuntimeError, "thumbnail unavailable"):
                self.m.run_job(self.job(thumb=True))

    def test_drive_pair_is_verified_and_immutable_before_local_cleanup(self):
        copies, verified = [], []
        def execute(args, **kw):
            if args[0] == "/fake/yt-dlp":
                return self.fake_download(args, **kw)
            if "copyto" in args:
                copies.append(args)
                return subprocess.CompletedProcess(args, 0, "", "")
            if "lsjson" in args:
                remote = args[-1]
                size = len(JPG) if remote.endswith(".jpg") else len(MP3)
                return subprocess.CompletedProcess(args, 0, json.dumps({"Size": size}), "")
            self.fail("Unexpected process: " + repr(args))
        def verify(remote, local, fmt):
            verified.append((remote, Path(local).name, fmt))
            self.assertTrue(Path(local).exists())
            return "https://drive.google.com/file/d/123456789012/view?usp=sharing"
        updates = []
        state = {"ytdlp": "/fake/yt-dlp", "ffmpeg": "/fake/ffmpeg",
                 "rclone": "/fake/rclone", "upload_ready": True}
        with patch.object(self.m, "deps", return_value=state), \
             patch.object(self.m.subprocess, "run", side_effect=execute), \
             patch.object(self.m, "verified_drive_public_link", side_effect=verify), \
             patch.object(self.m, "_update", side_effect=lambda jid, st, **kw: updates.append((st, kw))):
            self.m.run_job(self.job(thumb=True, upload=True))
        self.assertEqual([args[args.index("copyto") + 2] for args in copies], [
            "gdrive:Song Studio/Audio downloader/" + VIDEO + ".mp3",
            "gdrive:Song Studio/Audio downloader/" + VIDEO + ".jpg"])
        self.assertTrue(all("--immutable" in args for args in copies))
        self.assertEqual([x[2] for x in verified], ["mp3", "jpg"])
        self.assertEqual(next(st for st, kw in updates if st == "delivered"), "delivered")
        self.assertEqual(next(kw for st, kw in updates if st == "delivered" and "thumbnail_drive_path" in kw)["thumbnail_drive_path"], "gdrive:Song Studio/Audio downloader/" + VIDEO + ".jpg")
        self.assertFalse((self.m.JOBS / self.job()["job_id"] / (VIDEO + ".jpg")).exists())

    def test_qa_upload_stays_nonce_scoped_for_safe_cleanup(self):
        job = self.job(folder="__Audio_Proxy_QA__", project="C96CA984DDF24F5D", upload=True)
        copies = []
        def execute(args, **kw):
            if args[0] == "/fake/yt-dlp":
                return self.fake_download(args, **kw)
            if "copyto" in args:
                copies.append(args)
                return subprocess.CompletedProcess(args, 0, "", "")
            if "lsjson" in args:
                return subprocess.CompletedProcess(args, 0, json.dumps({"Size": len(MP3)}), "")
            self.fail(repr(args))
        state = {"ytdlp": "/fake/yt-dlp", "ffmpeg": "/fake/ffmpeg",
                 "rclone": "/fake/rclone", "upload_ready": True}
        with patch.object(self.m, "deps", return_value=state), \
             patch.object(self.m.subprocess, "run", side_effect=execute), \
             patch.object(self.m, "verified_drive_public_link", return_value="https://drive.google.com/file/d/123456789012/view"), \
             patch.object(self.m, "_update"):
            self.m.run_job(job)
        self.assertIn("Audio_Proxy_QA/C96CA984DDF24F5D/", copies[0][copies[0].index("copyto") + 2])

    def test_jpeg_route_cannot_read_outside_owned_job(self):
        jid = "ADJ-" + "A" * 16
        with self.m.db() as db:
            db.execute("""INSERT INTO jobs
                (job_id,batch_id,video_id,url,project,folder,format,quality,
                 auto_upload,keep_metadata,download_thumbnail,status,created_at,updated_at)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (jid, "ADB-TEST", VIDEO, "https://youtu.be/" + VIDEO, "Audio Batch",
                 "YouTube Audio", "mp3", 192, 0, 0, 1, "completed", "now", "now"))
        outside = Path(self.tmp.name) / (VIDEO + ".jpg")
        outside.write_bytes(JPG)
        with self.m.db() as db:
            db.execute("UPDATE jobs SET local_thumbnail=? WHERE job_id=?", (str(outside), jid))
        self.assertEqual(self.client.get("/tts/api/audio-downloader/jobs/" + jid + "/thumbnail").status_code, 404)
        owned = self.m.JOBS / jid
        owned.mkdir(parents=True)
        thumb = owned / (VIDEO + ".jpg")
        thumb.write_bytes(JPG)
        with self.m.db() as db:
            db.execute("UPDATE jobs SET local_thumbnail=? WHERE job_id=?", (str(thumb), jid))
        self.assertEqual(self.client.get("/tts/api/audio-downloader/jobs/" + jid + "/thumbnail").status_code, 200)


class AudioThumbnailUiTests(unittest.TestCase):
    def test_checkbox_and_browser_request_payload(self):
        html = TEMPLATE.read_text()
        self.assertEqual(html.count('id="vsaThumbnail"'), 1)
        self.assertIn("download_thumbnail:$('vsaThumbnail').checked", html)
        self.assertIn("downloadThumbnailViaSession", html)
        self.assertIn("String(job.video_id)+'.jpg'", html)
        self.assertNotIn('id="vsaFolder"', html)
        self.assertNotIn('id="vsaProject"', html)


if __name__ == "__main__":
    unittest.main()

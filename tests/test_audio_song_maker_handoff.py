"""Audio Downloader local-only auto-queue to existing Song Maker multipart API.

No Google Drive or real network traffic is permitted in this suite.
"""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except ImportError:
    raise unittest.SkipTest('FastAPI/httpx available in isolated Audio Downloader CI only')

SOURCE = Path(__file__).resolve().parents[1]
MODULE = SOURCE / "apps/voice-studio/part01/audio_downloader_api.py"
TEMPLATE = SOURCE / "apps/voice-studio/part01/templates/voice_studio.html"
VIDEO = "BaW_jenozKc"
MP3 = b"ID3" + b"x" * 2048
JPG = b"\xff\xd8\xff" + b"y" * 2048


class FakeResponse:
    status = 200

    def read(self, amount):
        return json.dumps({"id": "a" * 32, "stage": "Queued"}).encode("ascii")


class FakeConnection:
    calls = []

    def __init__(self, host, port, timeout):
        self.host, self.port, self.timeout = host, port, timeout
        self.headers = {}
        self.data = bytearray()
        FakeConnection.calls.append(self)

    def putrequest(self, method, path):
        self.method, self.path = method, path

    def putheader(self, name, value):
        self.headers[name] = value

    def endheaders(self):
        pass

    def send(self, data):
        self.data.extend(data)

    def getresponse(self):
        return FakeResponse()

    def close(self):
        pass


class AudioSongMakerAutoQueueTests(unittest.TestCase):
    def setUp(self):
        FakeConnection.calls = []
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"AUDIO_DOWNLOADER_ROOT": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        spec = importlib.util.spec_from_file_location("audio_songmaker_handoff_test", MODULE)
        self.m = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.m
        self.addCleanup(lambda: sys.modules.pop(spec.name, None))
        spec.loader.exec_module(self.m)
        self.m.BatchRequest.model_rebuild(_types_namespace=vars(self.m))
        self.m.init_storage()
        app = FastAPI()
        app.include_router(self.m.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def submit(self, auto_song_maker=True, thumbnail=True, auto_upload=False, owner="alice"):
        headers = {"X-Auth-User": owner} if owner else {}
        with patch.object(self.m, "deps", return_value={"ready": True, "upload_ready": False}):
            return self.client.post("/tts/api/audio-downloader/jobs", headers=headers, json={
                "links": ["https://youtu.be/" + VIDEO],
                "permission_confirmed": True, "download_thumbnail": thumbnail,
                "auto_upload": auto_upload, "auto_song_maker": auto_song_maker,
            })

    def downloaded(self, jid):
        folder = self.m.JOBS / jid
        folder.mkdir(parents=True, exist_ok=True)
        audio = folder / (VIDEO + ".mp3")
        image = folder / (VIDEO + ".jpg")
        audio.write_bytes(MP3)
        image.write_bytes(JPG)
        self.m._update(jid, "completed", local_file=str(audio),
                       local_thumbnail=str(image), completed_at=self.m.utc())

    def test_opt_in_switches_drive_off_and_thumbnail_on(self):
        self.assertEqual(self.submit(auto_song_maker=True, thumbnail=False).status_code, 422)
        self.assertEqual(self.submit(auto_song_maker=True, auto_upload=True).status_code, 422)
        self.assertEqual(self.submit(auto_song_maker=True, owner="").status_code, 403)
        allowed = self.submit()
        self.assertEqual(allowed.status_code, 201, allowed.text)
        job = self.client.get("/tts/api/audio-downloader/jobs/" + allowed.json()["job_ids"][0]).json()
        self.assertTrue(job["auto_song_maker"])
        self.assertFalse(job["auto_upload"])
        self.assertTrue(job["download_thumbnail"])
        self.assertEqual(job["song_maker_status"], "pending")
        self.assertNotIn("song_maker_owner", job)
        html = TEMPLATE.read_text()
        self.assertIn('id="vsaSongMaker"', html)
        self.assertIn("auto_song_maker:$('vsaSongMaker').checked", html)
        self.assertIn("if($('vsaSongMaker').checked){$('vsaUpload').checked=false;$('vsaThumbnail').checked=true;}", html)
        self.assertIn("if($('vsaUpload').checked||!$('vsaThumbnail').checked)$('vsaSongMaker').checked=false;", html)

    def test_local_handoff_streams_exact_pair_and_replays_idempotently(self):
        jid = self.submit().json()["job_ids"][0]
        self.downloaded(jid)
        with patch.object(self.m.http.client, "HTTPConnection", FakeConnection):
            self.m._handoff_song_maker(jid)
            self.m._handoff_song_maker(jid)
        self.assertEqual(len(FakeConnection.calls), 1)
        connection = FakeConnection.calls[0]
        self.assertEqual((connection.host, connection.port), ("127.0.0.1", 8150))
        self.assertEqual((connection.method, connection.path), ("POST", "/song-maker/api/jobs"))
        self.assertEqual(connection.headers["X-Auth-User"], "alice")
        self.assertEqual(connection.headers["X-Song-Maker"], "1")
        self.assertEqual(connection.headers["X-Audio-Downloader-Handoff"], "1")
        self.assertEqual(int(connection.headers["Content-Length"]), len(connection.data))
        self.assertIn(('filename="' + VIDEO + '.jpg"').encode(), connection.data)
        self.assertIn(('filename="' + VIDEO + '.mp3"').encode(), connection.data)
        self.assertIn(JPG, connection.data)
        self.assertIn(MP3, connection.data)
        job = self.client.get("/tts/api/audio-downloader/jobs/" + jid).json()
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["song_maker_status"], "queued")
        self.assertEqual(job["song_maker_job_id"], "a" * 32)
        self.assertIsNone(job["drive_path"])

    def test_failed_local_handoff_retries_without_drive_or_youtube(self):
        jid = self.submit().json()["job_ids"][0]
        self.downloaded(jid)
        with patch.object(self.m.http.client, "HTTPConnection", side_effect=ConnectionError):
            self.m._handoff_song_maker(jid)
        row = self.client.get("/tts/api/audio-downloader/jobs/" + jid).json()
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["song_maker_status"], "pending")
        self.assertIsNone(row["drive_path"])
        self.assertIsNotNone(row["song_maker_next_retry_at"])
        self.assertIsNone(self.m._due_song_maker_handoff())
        with self.m.db() as db:
            db.execute("UPDATE jobs SET song_maker_next_retry_at=NULL WHERE job_id=?", (jid,))
        self.assertEqual(self.m._due_song_maker_handoff(), jid)

    def test_download_only_never_calls_drive_upload(self):
        jid = self.submit().json()["job_ids"][0]
        row = dict(self.m.row_for(jid))
        def fake_download(args, **kwargs):
            folder = Path(kwargs["cwd"])
            source = folder / ("Title [" + VIDEO + "].mp3")
            source.write_bytes(MP3)
            (folder / ("Title [" + VIDEO + "].jpg")).write_bytes(JPG)
            return subprocess.CompletedProcess(args, 0, str(source) + "\n", "")
        with patch.object(self.m, "deps", return_value={
            "ytdlp": "/fake/yt-dlp", "ffmpeg": "/fake/ffmpeg", "progress_streaming": False,
            "upload_ready": False
        }), patch.object(self.m.subprocess, "run", side_effect=fake_download), \
             patch.object(self.m, "upload_downloaded") as drive:
            self.m.run_job(row)
        drive.assert_not_called()
        job = self.m.row_for(jid)
        self.assertEqual(job["status"], "completed")
        self.assertIsNone(job["drive_path"])
        self.assertTrue(Path(job["local_file"]).is_file())
        self.assertTrue(Path(job["local_thumbnail"]).is_file())


if __name__ == "__main__":
    unittest.main()

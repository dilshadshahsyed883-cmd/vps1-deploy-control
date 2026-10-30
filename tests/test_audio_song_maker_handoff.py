"""Opt-in Audio Downloader -> Song Maker handoff; no live YouTube or Drive calls."""
import importlib.util
import io
import json
import os
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

SOURCE = Path(__file__).resolve().parents[1]
MODULE = SOURCE / "apps/voice-studio/part01/audio_downloader_api.py"
TEMPLATE = SOURCE / "apps/voice-studio/part01/templates/voice_studio.html"
VIDEO = "BaW_jenozKc"


class AutoSongMakerHandoffTests(unittest.TestCase):
    def setUp(self):
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

    def submit(self, auto_song_maker=True, thumbnail=True, auto_upload=True, owner="alice"):
        headers = {"X-Auth-User": owner} if owner else {}
        with patch.object(self.m, "deps", return_value={"ready": True, "upload_ready": True}):
            return self.client.post("/tts/api/audio-downloader/jobs", headers=headers, json={
                "links": ["https://youtu.be/" + VIDEO],
                "permission_confirmed": True,
                "download_thumbnail": thumbnail,
                "auto_upload": auto_upload,
                "auto_song_maker": auto_song_maker,
            })

    def delivered(self, jid, with_thumbnail=True):
        self.m._update(jid, "delivered",
                       drive_path="gdrive:Song Studio/Audio downloader/" + VIDEO + ".mp3",
                       thumbnail_drive_path=("gdrive:Song Studio/Audio downloader/" + VIDEO + ".jpg"
                                             if with_thumbnail else None))

    def test_checkbox_off_by_default_and_requires_complete_pair(self):
        self.assertEqual(self.submit(auto_song_maker=True, thumbnail=False).status_code, 422)
        self.assertEqual(self.submit(auto_song_maker=True, auto_upload=False).status_code, 422)
        self.assertEqual(self.submit(auto_song_maker=True, owner="").status_code, 403)
        response = self.submit(auto_song_maker=False, owner="")
        self.assertEqual(response.status_code, 201, response.text)
        job = self.client.get("/tts/api/audio-downloader/jobs/" + response.json()["job_ids"][0]).json()
        self.assertFalse(job["auto_song_maker"])
        self.assertEqual(job["song_maker_status"], "off")
        self.assertNotIn("song_maker_owner", job)
        html = TEMPLATE.read_text()
        self.assertIn('id="vsaSongMaker"', html)
        self.assertIn("auto_song_maker:$('vsaSongMaker').checked", html)

    def test_handoff_exact_pair_stays_delivered_and_is_idempotent(self):
        response = self.submit()
        self.assertEqual(response.status_code, 201, response.text)
        jid = response.json()["job_ids"][0]
        self.delivered(jid)
        ack = io.BytesIO(json.dumps({"job_id": "a" * 32, "stage": "Queued"}).encode())
        with patch.object(self.m.urllib_request, "urlopen", return_value=ack) as send:
            self.m._handoff_song_maker(jid)
            self.m._handoff_song_maker(jid)
        send.assert_called_once()
        args, kwargs = send.call_args
        self.assertEqual(kwargs["timeout"], 25)
        self.assertEqual(json.loads(args[0].data), {"video_id": VIDEO, "format": "mp3"})
        self.assertEqual(args[0].get_header("X-auth-user"), "alice")
        job = self.client.get("/tts/api/audio-downloader/jobs/" + jid).json()
        self.assertEqual(job["status"], "delivered")
        self.assertEqual(job["song_maker_status"], "queued")
        self.assertEqual(job["song_maker_job_id"], "a" * 32)
        self.assertNotIn("song_maker_owner", job)

    def test_failed_handoff_only_retries_queue_not_upload(self):
        jid = self.submit().json()["job_ids"][0]
        self.delivered(jid)
        with patch.object(self.m.urllib_request, "urlopen", side_effect=TimeoutError):
            self.m._handoff_song_maker(jid)
        row = self.client.get("/tts/api/audio-downloader/jobs/" + jid).json()
        self.assertEqual(row["status"], "delivered")
        self.assertEqual(row["song_maker_status"], "pending")
        self.assertIsNotNone(row["song_maker_next_retry_at"])
        self.assertIsNone(self.m._due_song_maker_handoff())
        with self.m.db() as db:
            db.execute("UPDATE jobs SET song_maker_next_retry_at=NULL WHERE job_id=?", (jid,))
        self.assertEqual(self.m._due_song_maker_handoff(), jid)


if __name__ == "__main__":
    unittest.main()

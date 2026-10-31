"""Audio Downloader owner-scoped preference persistence and opt-in safety."""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / "apps/voice-studio/part01/audio_downloader_api.py"


class AudioPreferencesTests(unittest.TestCase):
    def setUp(self):
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("fastapi/httpx required; exercised in Audio Downloader CI")
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"AUDIO_DOWNLOADER_ROOT": self.tmp.name})
        self.env.start()
        spec = importlib.util.spec_from_file_location("audio_preferences_test", MODULE)
        self.m = importlib.util.module_from_spec(spec)
        # Dynamic test imports must be registered for Pydantic forward-reference resolution.
        sys.modules[spec.name] = self.m
        spec.loader.exec_module(self.m)
        self.m.AudioPreferences.model_rebuild(_types_namespace=vars(self.m))
        self.m.init_storage()
        app = FastAPI()
        app.include_router(self.m.router)
        self.client = TestClient(app,headers={'X-Auth-User':'owner-a'})
        self.owner_a = {"X-Auth-User": "owner-a"}
        self.owner_b = {"X-Auth-User": "owner-b"}

    def tearDown(self):
        self.client.close()
        self.env.stop()
        self.tmp.cleanup()

    def snapshot(self, **changes):
        result = {"format": "wav", "quality": 256, "auto_upload": False,
                  "keep_metadata": True, "download_thumbnail": True,
                  "auto_song_maker": True,"song_folder_id":""}
        result.update(changes)
        return result

    def test_saved_options_survive_new_db_connections_and_do_not_change_from_status(self):
        initial = self.client.get("/tts/api/audio-downloader/preferences", headers=self.owner_a)
        self.assertEqual(initial.status_code, 200)
        self.assertFalse(initial.json()["saved"])
        self.assertEqual(initial.json()["preferences"]["quality"], 192)
        expected = self.snapshot()
        saved = self.client.put("/tts/api/audio-downloader/preferences",
                                headers=self.owner_a, json=expected)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["preferences"], expected)
        # Background status/queue activity does not overwrite saved defaults.
        with patch.object(self.m, "deps", return_value={"ready": True, "upload_ready": True}):
            self.client.get("/tts/api/audio-downloader/status")
        self.m.init_storage()  # mimics a second server startup/migration.
        restored = self.client.get("/tts/api/audio-downloader/preferences", headers=self.owner_a)
        self.assertEqual(restored.status_code, 200)
        self.assertTrue(restored.json()["saved"])
        self.assertEqual(restored.json()["preferences"], expected)

    def test_owner_isolation_and_manual_update_only(self):
        expected = self.snapshot()
        self.assertEqual(self.client.put("/tts/api/audio-downloader/preferences",
                                         headers=self.owner_a, json=expected).status_code, 200)
        other = self.client.get("/tts/api/audio-downloader/preferences", headers=self.owner_b)
        self.assertFalse(other.json()["saved"])
        self.assertEqual(other.json()["preferences"]["auto_upload"], True)
        self.assertEqual(other.json()["preferences"]["auto_song_maker"], False)
        updated = self.snapshot(format="mp3", quality=128, auto_song_maker=False)
        self.assertEqual(self.client.put("/tts/api/audio-downloader/preferences",
                                         headers=self.owner_b, json=updated).status_code, 200)
        self.assertEqual(self.client.get("/tts/api/audio-downloader/preferences",
                                         headers=self.owner_a).json()["preferences"], expected)
        self.assertEqual(self.client.get("/tts/api/audio-downloader/preferences",
                                         headers=self.owner_b).json()["preferences"], updated)

    def test_unauthenticated_preferences_cannot_leak_or_overwrite(self):
        url = "/tts/api/audio-downloader/preferences"
        # Explicit empty header overrides the test client's default owner.
        self.assertEqual(self.client.get(url, headers={"X-Auth-User": ""}).status_code, 403)
        self.assertEqual(self.client.put(url, headers={"X-Auth-User": ""}, json=self.snapshot()).status_code, 403)
        self.assertFalse(self.client.get(url, headers=self.owner_a).json()["saved"])

    def test_incompatible_auto_queue_options_and_invalid_quality_are_rejected(self):
        url = "/tts/api/audio-downloader/preferences"
        for bad in (self.snapshot(auto_upload=True),
                    self.snapshot(download_thumbnail=False),
                    self.snapshot(quality=42),
                    self.snapshot(format="flac")):
            result = self.client.put(url, headers=self.owner_a, json=bad)
            self.assertEqual(result.status_code, 422, result.text)
        self.assertFalse(self.client.get(url, headers=self.owner_a).json()["saved"])

    def test_preexisting_queue_table_kept_intact(self):
        with self.m.db() as db:
            db.execute("""INSERT INTO jobs
                (job_id,batch_id,video_id,url,project,folder,format,quality,
                 auto_upload,keep_metadata,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("ADJ-" + "F" * 16, "ADB-TEST", "BaW_jenozKc",
                 "https://youtu.be/BaW_jenozKc", "", "", "mp3", 192,
                 0, 0, "completed", self.m.utc(), self.m.utc()))
        self.client.put("/tts/api/audio-downloader/preferences",
                        headers=self.owner_a, json=self.snapshot())
        with self.m.db() as db:
            row = db.execute("SELECT status,format,quality FROM jobs").fetchone()
        self.assertEqual(tuple(row), ("completed", "mp3", 192))


if __name__ == "__main__":
    unittest.main()

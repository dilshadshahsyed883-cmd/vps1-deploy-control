"""Pre-cutover Song/Voice backend isolation and exclusive ownership regression."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1] / "apps/voice-studio/part01"
sys.path.insert(0, str(ROOT))
import audio_downloader_api as audio
import song_audio_app as song
import voice_tenant


class StandaloneSongSessionTests(unittest.TestCase):
    def test_api_boundary_contains_no_voice_synthesis_routes(self):
        routes = {r.path for r in song.app.routes}
        self.assertIn("/tts/api/audio-downloader/jobs", routes)
        self.assertIn("/audio-downloader/app", routes)
        self.assertNotIn("/tts/api/clone/jobs", routes)
        self.assertNotIn("/tts/api/kokoro/jobs", routes)
        self.assertNotIn("/tts/api/edge/jobs", routes)

    def test_real_scene_identity_not_forged_headers_selects_grant(self):
        claims = {"username": "alpha", "is_admin": False, "allowed_studios": ["song"]}
        with patch.object(song, "scene_identity", return_value=claims) as auth:
            # TestClient without context never executes startup/starts a worker.
            client = TestClient(song.app)
            try:
                resp = client.get("/audio-downloader/app",
                                  headers={"cookie": "valid", "x-auth-user": "admin",
                                           "x-is-admin": "1"})
                self.assertEqual(resp.status_code, 200)
                self.assertIn("text/html", resp.headers["content-type"])
                self.assertTrue(auth.called)
                self.assertEqual(auth.call_args.args, ("valid",))
                # Same account loses Song grant; Voice grant cannot replace it.
                claims["allowed_studios"] = ["voice"]
                denied = client.get("/audio-downloader/app",
                                    headers={"cookie": "valid", "x-auth-user": "admin",
                                             "x-is-admin": "1"})
                self.assertEqual(denied.status_code, 403)
            finally:
                client.close()
        with self.assertRaises(HTTPException) as exc:
            voice_tenant.owner()
        self.assertEqual(exc.exception.status_code, 401)

    def test_anonymous_and_invalid_session_deny_all_song_routes(self):
        client = TestClient(song.app)
        try:
            with patch.object(song, "scene_identity", side_effect=HTTPException(401, "Login required")):
                denied = client.get("/tts/api/audio-downloader/jobs",
                                    headers={"x-auth-user": "spoofed", "x-is-admin": "1"})
            self.assertEqual(denied.status_code, 401)
            self.assertEqual(client.get("/healthz").json(),
                             {"ok": True, "app": "song-audio-downloader"})
        finally:
            client.close()

    def test_standalone_worker_fails_closed_without_explicit_cutover(self):
        with patch.dict(os.environ, {"SONG_AUDIO_CUTOVER_ACTIVE": "0"}):
            with patch.object(audio, "start_worker") as worker:
                with self.assertRaisesRegex(RuntimeError, "guarded cutover"):
                    song.song_audio_startup()
                worker.assert_not_called()

    def test_smoke_mode_never_starts_external_worker(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {"SONG_AUDIO_CUTOVER_ACTIVE": "1",
                                          "AUDIO_DOWNLOADER_ROOT": td,
                                          "FRESH_WEBSITE_SMOKE_TEST": "1"}):
                with patch.object(audio, "ROOT", Path(td)), patch.object(audio, "init_storage") as init:
                    with patch.object(audio, "start_worker") as worker:
                        song.song_audio_startup()
                        init.assert_called_once_with()
                        worker.assert_not_called()


class ExclusiveWorkerTests(unittest.TestCase):
    def test_second_worker_lock_fails_closed_until_owner_releases(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = root / "data"
            with patch.object(audio, "ROOT", root), patch.object(audio, "DATA", data):
                first = audio._acquire_exclusive_worker_lock()
                try:
                    with self.assertRaisesRegex(RuntimeError, "active worker"):
                        audio._acquire_exclusive_worker_lock()
                finally:
                    os.close(first)
                new_owner = audio._acquire_exclusive_worker_lock()
                os.close(new_owner)

    def test_symlinked_lock_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = root / "data"
            data.mkdir()
            target = root / "sentinel"
            target.write_text("do-not-change")
            (data / ".audio-downloader-worker.lock").symlink_to(target)
            with patch.object(audio, "ROOT", root), patch.object(audio, "DATA", data):
                with self.assertRaisesRegex(RuntimeError, "worker lock unavailable"):
                    audio._acquire_exclusive_worker_lock()
            self.assertEqual(target.read_text(), "do-not-change")


class LegacyVoiceSplitGuardTests(unittest.TestCase):
    def test_voice_default_remains_legacy_until_explicit_split(self):
        source = (ROOT / "vps1_omnivoice_app.py").read_text()
        self.assertIn('os.getenv("SONG_AUDIO_BACKEND_SPLIT") == "1"', source)
        self.assertIn("if not SONG_AUDIO_BACKEND_SPLIT:", source)
        self.assertIn("app.include_router(ad.router)", source)
        self.assertIn("ad.start_worker()", source)
        self.assertIn('downloader = ad.status() if not SONG_AUDIO_BACKEND_SPLIT', source)

    def test_release_does_not_change_song_data_or_authorization_defaults(self):
        source = (ROOT / "audio_downloader_api.py").read_text()
        self.assertIn("AUDIO_DOWNLOADER_ROOT", source)
        self.assertIn("Song Studio/Audio downloader", source)
        self.assertIn("fcntl.LOCK_EX | fcntl.LOCK_NB", source)
        self.assertIn("os.O_NOFOLLOW", source)


if __name__ == "__main__":
    unittest.main()

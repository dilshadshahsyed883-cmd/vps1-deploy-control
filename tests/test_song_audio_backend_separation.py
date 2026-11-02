"""Pre-cutover Song/Voice backend isolation and exclusive ownership regression."""
from __future__ import annotations

import os
import ast
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1] / "apps/song-studio/part01"
VOICE_ROOT = Path(__file__).resolve().parents[1] / "apps/voice-studio/part01"
sys.path.insert(0, str(ROOT))
import song_audio_downloader_api as audio
import song_audio_app as song
import song_tenant


class StandaloneSongSessionTests(unittest.TestCase):
    def test_api_boundary_contains_no_voice_synthesis_routes(self):
        routes = {r.path for r in audio.router.routes}
        # Included router behavior may be lazy in modern FastAPI; the endpoint
        # definitions themselves must remain Song-only and in this module.
        self.assertTrue(any(getattr(r, "router", None) is audio.router or
                            getattr(r, "original_router", None) is audio.router
                            for r in song.app.router.routes) or
                        any(getattr(r, "path", "") == "/tts/api/audio-downloader/jobs"
                            for r in song.app.routes))
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
            song_tenant.owner()
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


class SceneSongProxySwitchTests(unittest.TestCase):
    def test_exact_backend_only_and_fail_closed_on_bad_configuration(self):
        scene_path = Path(__file__).resolve().parents[1] / "apps/scene-builder/part01/app.py"
        tree = ast.parse(scene_path.read_text())
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name == "_song_audio_upstream")
        module = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
        env = {"os": os, "HTTPException": HTTPException,
               "_VS_UPSTREAM": "http://127.0.0.1:8143",
               "_SONG_AUDIO_DEDICATED_UPSTREAM": "http://127.0.0.1:8152"}
        exec(compile(module, str(scene_path), "exec"), env)
        switch = env["_song_audio_upstream"]
        with patch.dict(os.environ, {"SONG_AUDIO_SPLIT_UPSTREAM": ""}):
            self.assertEqual(switch(), "http://127.0.0.1:8143")
        with patch.dict(os.environ, {"SONG_AUDIO_SPLIT_UPSTREAM": "http://127.0.0.1:8152"}):
            self.assertEqual(switch(), "http://127.0.0.1:8152")
        with patch.dict(os.environ, {"SONG_AUDIO_SPLIT_UPSTREAM": "http://evil.invalid"}):
            with self.assertRaises(HTTPException) as exc:
                switch()
            self.assertEqual(exc.exception.status_code, 503)

    def test_both_song_facades_use_dedicated_backend_without_voice_reroute(self):
        source = (Path(__file__).resolve().parents[1] /
                  "apps/scene-builder/part01/app.py").read_text()
        self.assertIn('_song_audio_upstream() + "/audio-downloader/app"', source)
        self.assertIn('(_song_audio_upstream() if studio == "song" else _VS_UPSTREAM)',
                      source)


class LegacyVoiceSplitGuardTests(unittest.TestCase):
    def test_voice_default_remains_legacy_until_explicit_split(self):
        source = (VOICE_ROOT / "vps1_omnivoice_app.py").read_text()
        self.assertIn('os.getenv("SONG_AUDIO_BACKEND_SPLIT") == "1"', source)
        self.assertIn("if not SONG_AUDIO_BACKEND_SPLIT:", source)
        self.assertIn("app.include_router(ad.router)", source)
        self.assertIn("ad.start_worker()", source)
        self.assertIn('downloader = ad.status() if not SONG_AUDIO_BACKEND_SPLIT', source)

    def test_release_does_not_change_song_data_or_authorization_defaults(self):
        source = (ROOT / "song_audio_downloader_api.py").read_text()
        self.assertIn("AUDIO_DOWNLOADER_ROOT", source)
        self.assertIn("Song Studio/Audio downloader", source)
        self.assertIn("fcntl.LOCK_EX | fcntl.LOCK_NB", source)
        self.assertIn("os.O_NOFOLLOW", source)


if __name__ == "__main__":
    unittest.main()

"""Approved Voice/Song routing and login contract; strictly offline."""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
SCENE = ROOT / "apps/scene-builder/part01/app.py"
VOICE = ROOT / "apps/voice-studio/part01"
sys.path.insert(0, str(VOICE))
import voice_tenant


def load_scene_function(name, names=None):
    tree = ast.parse(SCENE.read_text())
    node = next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    node.decorator_list = []
    env = {"HTTPException": HTTPException, "__name__": "voice_song_contract"}
    if names:
        env.update(names)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), str(SCENE), "exec"), env)
    return env[name]


class SsoIdentityBoundary(unittest.TestCase):
    def test_live_scene_port_and_authenticated_grants(self):
        self.assertEqual(voice_tenant._IDENTITY_URL, "http://127.0.0.1:18080/api/session/identity")
        class Reply:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, amount):
                return json.dumps({"authenticated": True, "username": "alpha",
                                   "is_admin": False, "allowed_studios": ["song"]}).encode()
        def fake_open(request, timeout):
            self.assertEqual(request.full_url, voice_tenant._IDENTITY_URL)
            self.assertEqual(request.get_header("Cookie"), "test-scene-session")
            return Reply()
        with patch.object(voice_tenant.urllib.request, "urlopen", fake_open):
            result = voice_tenant.scene_identity("test-scene-session")
        self.assertEqual(result["username"], "alpha")
        self.assertEqual(result["allowed_studios"], ["song"])
        self.assertFalse(result["is_admin"])

    def test_unverified_session_fails_closed(self):
        with self.assertRaises(HTTPException) as ctx:
            voice_tenant.scene_identity("")
        self.assertEqual(ctx.exception.status_code, 401)


class OwnerRoutes(unittest.IsolatedAsyncioTestCase):
    async def test_song_only_vs_voice_grants_preserve_existing_audio(self):
        classify = load_scene_function("studio_for_path")
        self.assertEqual(classify("/audio-downloader/"), "song")
        self.assertEqual(classify("/audio-downloader/tts/api/audio-downloader/jobs"), "song")
        self.assertEqual(classify("/voice-studio/service/tts/api/audio-downloader/jobs"), "song")
        self.assertEqual(classify("/voice-studio/app"), "voice")
        self.assertEqual(classify("/voice-studio/service/tts/api/clone/jobs"), "voice")

    async def test_song_proxy_only_forwards_downloader_api(self):
        called = []
        async def forwarded(request, upstream_path):
            called.append(upstream_path)
            return {"upstream": upstream_path}
        fn = load_scene_function("song_audio_downloader_api",
                                 {"re": __import__("re"), "_vps1_voice_studio_proxy": forwarded})
        result = await fn(object(), "jobs/KOJ-1")
        self.assertEqual(result["upstream"], "tts/api/audio-downloader/jobs/KOJ-1")
        self.assertEqual(called, ["tts/api/audio-downloader/jobs/KOJ-1"])
        with self.assertRaises(HTTPException) as err:
            await fn(object(), "../api/clone/jobs")
        self.assertEqual(err.exception.status_code, 404)
        self.assertEqual(called, ["tts/api/audio-downloader/jobs/KOJ-1"])


if __name__ == "__main__":
    unittest.main()

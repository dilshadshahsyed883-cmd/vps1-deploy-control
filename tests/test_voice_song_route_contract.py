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


class LiveVoiceMiddlewareContract(unittest.IsolatedAsyncioTestCase):
    """Exercise the actual Voice middleware function without starting any worker."""

    @staticmethod
    def middleware():
        from types import SimpleNamespace
        from fastapi.responses import JSONResponse

        source = VOICE / "vps1_omnivoice_app.py"
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        node = next(
            n for n in tree.body
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "voice_session_and_permissions"
        )
        node.decorator_list = []
        env = {
            "asyncio": asyncio,
            "HTTPException": HTTPException,
            "JSONResponse": JSONResponse,
            "scene_identity": voice_tenant.scene_identity,
            "set_identity": voice_tenant.set_identity,
            "reset_identity": voice_tenant.reset_identity,
            "__name__": "voice_middleware_offline_contract",
        }
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])),
                     str(source), "exec"), env)
        return env["voice_session_and_permissions"]

    @staticmethod
    def request(path, cookie="valid-scene-cookie", method="GET"):
        from types import SimpleNamespace
        return SimpleNamespace(
            url=SimpleNamespace(path=path),
            headers={"cookie": cookie, "X-Auth-User": "forged-admin", "X-Is-Admin": "1"},
            method=method,
            state=SimpleNamespace(),
        )

    async def test_verified_session_overrides_spoofed_headers_and_revalidates_grants(self):
        middleware = self.middleware()
        current = {"username": "alpha", "is_admin": False, "allowed_studios": ["voice"]}
        seen = []

        def identity(cookie):
            self.assertEqual(cookie, "valid-scene-cookie")
            return current.copy()

        async def next_handler(request):
            seen.append((request.state.scene_owner, voice_tenant.owner()))
            return {"ok": True}

        with patch.dict(middleware.__globals__, {"scene_identity": identity}):
            owned = await middleware(self.request("/tts/api/clone/jobs"), next_handler)
            self.assertEqual(owned, {"ok": True})
            self.assertEqual(seen, [("alpha", "alpha")])
            with self.assertRaises(HTTPException) as exc:
                voice_tenant.owner()
            self.assertEqual(exc.exception.status_code, 401)

            # Scene grants are checked on EVERY request, not cached at login.
            current["allowed_studios"] = []
            revoked = await middleware(self.request("/tts/api/clone/jobs"), next_handler)
            self.assertEqual(revoked.status_code, 403)
            self.assertEqual(len(seen), 1)
            current["username"] = "beta"
            current["allowed_studios"] = ["voice"]
            restored = await middleware(self.request("/tts/api/clone/jobs"), next_handler)
            self.assertEqual(restored, {"ok": True})
            self.assertEqual(seen[-1], ("beta", "beta"))

    async def test_expired_login_and_mutating_shared_provider_controls_fail_closed(self):
        middleware = self.middleware()

        def identity(cookie):
            if cookie != "valid-scene-cookie":
                raise HTTPException(401, "Scene login required")
            return {"username": "alpha", "is_admin": False, "allowed_studios": ["voice"]}

        async def next_handler(request):
            return {"accepted": request.url.path}

        with patch.dict(middleware.__globals__, {"scene_identity": identity}):
            expired = await middleware(self.request("/tts/api/clone/jobs", cookie=""), next_handler)
            self.assertEqual(expired.status_code, 401)
            for path in (
                "/tts/api/clone/worker/config",
                "/tts/api/clone/worker/connect",
                "/tts/api/clone/omnivoice/config",
                "/tts/api/clone/omnivoice/disconnect",
            ):
                with self.subTest(path=path):
                    denied = await middleware(self.request(path, method="POST"), next_handler)
                    self.assertEqual(denied.status_code, 403)

            # Non-admin keeps existing self-owned generation controls; these
            # endpoints are not shared worker configuration.
            for path in (
                "/tts/api/clone/worker/test-generation",
                "/tts/api/clone/omnivoice/test-generation",
            ):
                with self.subTest(path=path):
                    allowed = await middleware(self.request(path, method="POST"), next_handler)
                    self.assertEqual(allowed, {"accepted": path})
            for path in (
                "/tts/api/clone/worker/config",
                "/tts/api/clone/omnivoice/config",
            ):
                with self.subTest(path=path):
                    denied = await middleware(self.request(path, method="GET"), next_handler)
                    self.assertEqual(denied.status_code, 403)

            # Admin can manage shared worker connections.
            with patch.dict(middleware.__globals__, {
                "scene_identity": lambda cookie: {
                    "username": "adminuser", "is_admin": True,
                    "allowed_studios": ["voice"],
                }
            }):
                for path in (
                    "/tts/api/clone/worker/config",
                    "/tts/api/clone/omnivoice/config",
                ):
                    allowed = await middleware(self.request(path, method="GET"), next_handler)
                    self.assertEqual(allowed, {"accepted": path})

            # Song-only route survives without the additional Voice grant.
            with patch.dict(middleware.__globals__, {
                "scene_identity": lambda cookie: {
                    "username": "songuser", "is_admin": False,
                    "allowed_studios": ["song"],
                }
            }):
                allowed = await middleware(self.request("/tts/api/audio-downloader/jobs"), next_handler)
                self.assertEqual(allowed, {"accepted": "/tts/api/audio-downloader/jobs"})
                blocked = await middleware(self.request("/tts/api/clone/jobs"), next_handler)
                self.assertEqual(blocked.status_code, 403)



    async def test_parallel_sessions_do_not_cross_leak_owners_or_shared_admin(self):
        """Two authenticated sessions are never allowed to inherit each other's context."""
        middleware = self.middleware()
        identities = {
            "admin-cookie": {"username": "adminuser", "is_admin": True,
                             "allowed_studios": ["song", "short", "copyright", "voice"]},
            "alpha-cookie": {"username": "alpha", "is_admin": False,
                             "allowed_studios": ["voice"]},
            "beta-cookie": {"username": "beta", "is_admin": False,
                            "allowed_studios": ["voice"]},
            "revoked-cookie": {"username": "revoked", "is_admin": False,
                               "allowed_studios": []},
        }
        observed = []

        def lookup(cookie):
            if cookie not in identities:
                raise HTTPException(status_code=401, detail="Session unavailable")
            return identities[cookie].copy()

        async def next_handler(request):
            before = voice_tenant.owner()
            await asyncio.sleep(0)
            after = voice_tenant.owner()
            self.assertEqual(before, after)
            self.assertEqual(request.state.scene_owner, after)
            observed.append(after)
            return {"owner": after}

        with patch.dict(middleware.__globals__, {"scene_identity": lookup}):
            responses = await asyncio.gather(*[
                middleware(self.request("/tts/api/clone/jobs", cookie=cookie), next_handler)
                for cookie in ("admin-cookie", "alpha-cookie", "beta-cookie", "revoked-cookie")
            ])
            self.assertEqual([result["owner"] for result in responses[:3]],
                             ["adminuser", "alpha", "beta"])
            self.assertEqual(responses[3].status_code, 403)
            self.assertCountEqual(observed, ["adminuser", "alpha", "beta"])

            # Only the real administrator can read/change shared worker configuration.
            responses = await asyncio.gather(*[
                middleware(self.request("/tts/api/clone/worker/config",
                                        cookie=cookie, method="GET"), next_handler)
                for cookie in ("admin-cookie", "alpha-cookie", "beta-cookie")
            ])
            self.assertEqual(responses[0], {"owner": "adminuser"})
            self.assertEqual([r.status_code for r in responses[1:]], [403, 403])

        with self.assertRaises(HTTPException) as exc:
            voice_tenant.owner()
        self.assertEqual(exc.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()

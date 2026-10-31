"""Offline regression: protected Audio release verifies with a real Scene SSO identity."""
import ast
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "production/audio_downloader_release.py"


def isolated_verify():
    tree = ast.parse(SOURCE.read_text())
    target = next(x for x in tree.body if isinstance(x, ast.FunctionDef) and x.name == "verify")
    def require(value, message):
        if not value:
            raise RuntimeError(message)
    ns = {"require": require, "json": json}
    exec(compile(ast.Module(body=[target], type_ignores=[]), str(SOURCE), "exec"), ns)
    return ns["verify"]


class Reply:
    def __init__(self, status, body=b"", headers=None):
        self.status = status
        self.body = body
        self.headers = headers or {}
    def __enter__(self):
        return self
    def __exit__(self, *_):
        return False
    def read(self, *_):
        return self.body


class AudioTenantReleaseVerification(unittest.TestCase):
    def test_verified_scene_session_identity_is_used_for_every_audio_probe(self):
        calls = []
        def open_url(req, timeout=None):
            url = req.full_url
            headers = {key.lower(): value for key, value in req.header_items()}
            calls.append((url, headers))
            if "/internal/workspace-auth" in url:
                self.assertEqual(headers["cookie"], "session-cookie")
                return Reply(204, headers={"X-Auth-User": "haseeb1", "X-Is-Admin": "1"})
            self.assertEqual(headers.get("x-auth-user"), "haseeb1")
            self.assertEqual(headers.get("x-is-admin"), "1")
            state = {"ready": True, "download_contract": "explicit-proxy-private-session-v2",
                     "proxy": {"valid_selection": True}} if url.endswith("/status") else {"jobs": []}
            return Reply(200, json.dumps(state).encode())
        def https_get(path, cookie=None):
            if not cookie:
                return (401, "text/plain", b"")
            if path.endswith("/status"):
                return (200, "application/json", json.dumps({"download_contract": "explicit-proxy-private-session-v2"}).encode())
            return (200, "application/json", b"{}")
        pkg = types.ModuleType("production")
        pkg.__path__ = []
        mock_route = types.ModuleType("production.route_uploader_scene_https")
        mock_route.scene_cookie = lambda: "session-cookie"
        mock_route.https_get = https_get
        with patch.dict(sys.modules, {"production": pkg, "production.route_uploader_scene_https": mock_route}), \
                patch("urllib.request.build_opener") as build:
            build.return_value.open.side_effect = open_url
            isolated_verify()()
        self.assertEqual(len(calls), 4)
        self.assertIn("workspace-auth", calls[0][0])

    def test_denies_unverified_or_nonadmin_scene_identity(self):
        pkg = types.ModuleType("production")
        pkg.__path__ = []
        mock_route = types.ModuleType("production.route_uploader_scene_https")
        mock_route.scene_cookie = lambda: "session-cookie"
        mock_route.https_get = lambda *args: (200, "application/json", b"{}")
        for identity in ({"X-Auth-User": "haseeb1", "X-Is-Admin": "0"},
                         {"X-Auth-User": "", "X-Is-Admin": "1"}):
            with self.subTest(identity=identity), \
                    patch.dict(sys.modules, {"production": pkg, "production.route_uploader_scene_https": mock_route}), \
                    patch("urllib.request.build_opener") as build:
                build.return_value.open.return_value = Reply(204, headers=identity)
                with self.assertRaisesRegex(RuntimeError, "Scene admin"):
                    isolated_verify()()
                self.assertEqual(build.return_value.open.call_count, 1)


if __name__ == "__main__":
    unittest.main()

"""No-data migration tests for independent uploader engines and authenticated proxy."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1] / "apps/local-upload-manager/part01"
sys.path.insert(0, str(APP))
try:
    from lum_engine.mode_gateway import MODE_PORTS, proxy_target, trusted_headers
    GATEWAY_DEPS_READY = True
except ModuleNotFoundError as exc:
    # Shared production CI also tests lean non-uploader applications whose
    # isolated venv intentionally has no FastAPI/httpx. The actual uploader
    # lane installs and executes the full gateway contract.
    if exc.name not in {"fastapi", "starlette", "httpx"}:
        raise
    GATEWAY_DEPS_READY = False
    MODE_PORTS = {}
    proxy_target = None
    trusted_headers = None


class RequestStub:
    headers = {
        "x-auth-user": "fake-admin", "x-is-admin": "1",
        "authorization": "Bearer evil", "host": "evil.tld",
        "cookie": "lum_session_engine2=xyz", "content-type": "application/x-www-form-urlencoded",
    }


@unittest.skipUnless(GATEWAY_DEPS_READY, "Gateway requires uploader FastAPI/httpx dependencies")
class LUMModeProxyTests(unittest.TestCase):
    def test_only_two_fixed_loopback_targets(self):
        self.assertEqual(MODE_PORTS, {"engine2": 8016, "shorts": 8017})
        self.assertEqual(
            proxy_target("engine2", "/local-upload-manager/modes/engine2/app", "tab=auto_engine"),
            "http://127.0.0.1:8016/local-upload-manager/modes/engine2/app?tab=auto_engine",
        )
        self.assertEqual(
            proxy_target("shorts", "/local-upload-manager/modes/shorts/health"),
            "http://127.0.0.1:8017/local-upload-manager/modes/shorts/health",
        )

    def test_rejects_cross_mode_and_unknown_paths(self):
        for mode, path in [
            ("engine2", "/local-upload-manager/modes/shorts/app"),
            ("engine1", "/local-upload-manager/modes/engine1/app"),
            ("shorts", "https://example.org/"),
            ("shorts", "/etc/passwd"),
        ]:
            with self.subTest(mode=mode, path=path):
                with self.assertRaises(ValueError):
                    proxy_target(mode, path)

    def test_browser_cannot_impersonate_an_admin(self):
        headers = trusted_headers(
            {"username": "verified", "is_admin": False},
            RequestStub(),
        )
        self.assertEqual(headers["x-auth-user"], "verified")
        self.assertEqual(headers["x-is-admin"], "0")
        self.assertEqual(headers["x-app-access"], "lum")
        self.assertNotIn("host", headers)
        self.assertNotIn("authorization", headers)

    def test_admin_identity_is_server_derived(self):
        headers = trusted_headers(
            {"username": "verified-admin", "is_admin": True},
            RequestStub(),
        )
        self.assertEqual(headers["x-auth-user"], "verified-admin")
        self.assertEqual(headers["x-is-admin"], "1")


if __name__ == "__main__":
    unittest.main()

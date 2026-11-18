"""Offline Scene SSO issuer contract and negative security regressions."""
import base64
import hashlib
import hmac
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from starlette.datastructures import Headers, QueryParams

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/scene-builder/part01"))
import concat_sso_issuer as issuer

KEY = "concat-only-contract-signing-secret-32-bytes-minimum"
USERS = {
    "alice": {"username": "Alice", "is_active": 1, "is_admin": 0},
    "bob": {"username": "Bob", "is_active": 1, "is_admin": 0},
    "admin": {"username": "Admin", "is_active": 1, "is_admin": 1},
}


class FakeRequest:
    def __init__(self, session, *, host="app.scenebuilder.online", state="s" * 43,
                 client="concat-web", callback=issuer.CALLBACK, extra=""):
        self.session = session
        self.headers = Headers({"host": host, "x-auth-user": "Admin", "x-is-admin": "true"})
        query = {"client_id": client, "redirect_uri": callback, "state": state}
        self.query_params = QueryParams(list(query.items()) + ([("evil", extra)] if extra else []))


class SceneSSOTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "scene.sqlite3"
        self.account = dict(USERS["alice"])
        def factory():
            conn = sqlite3.connect(self.path)
            conn.row_factory = sqlite3.Row
            return conn
        self.database = factory
        with self.database() as conn:
            issuer.init_schema(conn)
            issuer.init_schema(conn)
        self.session = {"authenticated": True, "username": "Alice"}
        with self.database() as conn:
            self.sid = issuer.start_session(conn, self.session, "Alice", now=1000)

    def account_lookup(self, username):
        return dict(self.account) if username.casefold() == "alice" else None

    def handoff(self, request=None, now=1001):
        return issuer.authorize_handoff(request or FakeRequest(self.session),
                                       self.database, self.account_lookup,
                                       scene_session_secret="other-session-secret",
                                       key_override=KEY, now=now)

    def test_contract_signature_and_one_time_sid_reference(self):
        url = self.handoff()
        self.assertEqual(urlsplit(url).scheme, "https")
        self.assertEqual(urlsplit(url).path, "/auth/scene/callback")
        q = parse_qs(urlsplit(url).query)
        self.assertEqual(q["state"], ["s" * 43])
        encoded, signature = q["ticket"][0].split(".")
        expected = hmac.new(KEY.encode(), ("scene-concat-v1." + encoded).encode(),
                            hashlib.sha256).digest()
        actual = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
        self.assertTrue(hmac.compare_digest(expected, actual))
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        self.assertEqual((claims["v"], claims["iss"], claims["aud"], claims["sub"]),
                         (1, issuer.ISSUER, "concat-web", "Alice"))
        self.assertEqual((claims["iat"], claims["exp"]), (1001, 1061))
        self.assertEqual(claims["sid"], self.sid)
        with self.database() as conn:
            row = conn.execute("SELECT * FROM concat_sso_sessions").fetchone()
        self.assertEqual(row["sid_hash"], hashlib.sha256(self.sid.encode()).hexdigest())
        self.assertNotIn(self.sid, str(tuple(row)))

    def test_forged_host_callback_client_state_and_duplicate_parameter(self):
        cases = [
            FakeRequest(self.session, host="evil.example"),
            FakeRequest(self.session, callback="https://evil.example/callback"),
            FakeRequest(self.session, client="evil"),
            FakeRequest(self.session, state="small"),
            FakeRequest(self.session, extra="1"),
        ]
        for request in cases:
            with self.subTest(query=str(request.query_params)):
                with self.assertRaises(issuer.SSORejected):
                    self.handoff(request)

    def test_revocation_logout_and_sid_expiry(self):
        with self.database() as conn:
            self.assertIsNotNone(issuer.active_sid(conn, self.session, "Alice", now=1001))
            issuer.revoke_session(conn, self.session, now=1002)
            self.assertIsNone(issuer.active_sid(conn, self.session, "Alice", now=1003))
        with self.assertRaises(issuer.SSORejected):
            self.handoff(now=1003)
        self.assertNotIn(issuer.SID_KEY, self.session)

    def test_inactive_user_and_canonical_mismatch(self):
        self.account["is_active"] = 0
        with self.assertRaises(issuer.SSORejected):
            self.handoff()
        self.account["is_active"] = 1
        self.account["username"] = "Bob"
        with self.assertRaises(issuer.SSORejected):
            self.handoff()

    def test_ownerless_and_untrusted_identity_rejected(self):
        for name in ("__default__", "", "bad/name"):
            with self.database() as conn:
                with self.assertRaises(issuer.SSORejected):
                    issuer.start_session(conn, {}, name)
        untrusted = FakeRequest({"authenticated": False, "username": "Alice"})
        with self.assertRaises(issuer.SSORejected):
            self.handoff(untrusted)

    def test_expired_and_revoked_account(self):
        with self.database() as conn:
            self.assertIsNone(issuer.active_sid(conn, self.session, "Alice", now=1000 + issuer.SID_SECONDS))
            issuer.revoke_username(conn, "Alice", now=1050)
            self.assertIsNone(issuer.active_sid(conn, self.session, "Alice", now=1051))
        with self.assertRaises(issuer.SSORejected):
            self.handoff(now=1051)

    def test_separate_signing_key_required(self):
        for raw in ("short", "scene-secret", ""):
            with self.assertRaises(issuer.SSOUnavailable):
                issuer.signing_key("scene-secret", "web-key", raw)
        self.assertEqual(issuer.signing_key("scene-secret", "web-key", KEY), KEY.encode())


if __name__ == "__main__":
    unittest.main()

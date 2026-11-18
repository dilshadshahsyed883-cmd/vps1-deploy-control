"""Focused offline contract/security regression for VPS1 #772.

Only a temporary Scene database and simulated signed-cookie state are used.
The Concat consumer's atomic JTI replay ledger is tested in its own PR #6.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/scene-builder/part01"))
import concat_sso_issuer as issuer

KEY = "concat-test-only-" + "x" * 48
NOW = 1_800_000_000
STATE = "state_" + "s" * 40


class Params(dict):
    def __init__(self, overrides=None):
        data = {
            "client_id": ["concat-web"],
            "redirect_uri": [issuer.CALLBACK],
            "state": [STATE],
        }
        if overrides:
            data.update(overrides)
        super().__init__(data)

    def getlist(self, key):
        return self[key]

    def get(self, key, default=None):
        values = super().get(key)
        return values[-1] if values else default


class Request:
    def __init__(self, session, host="app.scenebuilder.online", params=None):
        self.session = session
        self.headers = {"host": host}
        self.query_params = Params(params)


def decode_ticket(redirect):
    parsed = urlsplit(redirect)
    assert parsed.scheme + "://" + parsed.netloc + parsed.path == issuer.CALLBACK
    params = parse_qs(parsed.query)
    assert set(params) == {"ticket", "state"}
    token = params["ticket"][0]
    encoded, mac = token.split(".")
    check = hmac.new(KEY.encode(), ("scene-concat-v1." + encoded).encode(), hashlib.sha256).digest()
    signature = base64.urlsafe_b64decode(mac + "=" * (-len(mac) % 4))
    if not hmac.compare_digest(check, signature):
        raise ValueError("forged ticket")
    payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    if payload["state"] != params["state"][0]:
        raise ValueError("wrong state")
    return payload


class ConcatIssuerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dbfile = Path(self.tmp.name) / "web.sqlite3"
        with self.db() as conn:
            conn.execute(
                "CREATE TABLE users(username TEXT PRIMARY KEY COLLATE NOCASE, "
                "is_active INTEGER NOT NULL, is_admin INTEGER NOT NULL)"
            )
            conn.executemany("INSERT INTO users VALUES (?,?,?)",
                             [("Alice", 1, 1), ("Bob", 1, 0), ("__default__", 1, 0)])
            issuer.init_schema(conn)
            issuer.init_schema(conn)
        self.session = {"authenticated": True, "username": "Alice"}
        with self.db() as conn:
            issuer.start_session(conn, self.session, "Alice", now=NOW)

    def db(self):
        return sqlite3.connect(str(self.dbfile))

    def account(self, username):
        with self.db() as conn:
            row = conn.execute(
                "SELECT username,is_active,is_admin FROM users WHERE username=? COLLATE NOCASE",
                (username,),
            ).fetchone()
        return dict(zip(("username", "is_active", "is_admin"), row)) if row else None

    def authorize(self, session=None, params=None, host="app.scenebuilder.online", now=NOW,
                  key=KEY):
        req = Request(self.session if session is None else session, host, params)
        return issuer.authorize_handoff(
            req, self.db, self.account, scene_session_secret="existing-scene-session-key",
            web_access_key="legacy-web-key", key_override=key, now=now,
        )

    def assert_rejected(self, **kwargs):
        with self.assertRaises((issuer.SSORejected, issuer.SSOUnavailable)):
            self.authorize(**kwargs)

    def test_hmac_claims_and_read_only_live_table(self):
        redirect = self.authorize()
        payload = decode_ticket(redirect)
        self.assertEqual(payload["v"], 1)
        self.assertEqual(payload["iss"], issuer.ISSUER)
        self.assertEqual(payload["aud"], issuer.CLIENT)
        self.assertEqual(payload["sub"], "Alice")
        self.assertEqual(payload["state"], STATE)
        self.assertEqual(payload["iat"], NOW)
        self.assertEqual(payload["exp"] - payload["iat"], 60)
        self.assertTrue(re.fullmatch(r"[A-Za-z0-9_-]{32,128}", payload["jti"]))
        self.assertEqual(payload["sid"], self.session["scene_sid"])
        self.assertEqual(len(base64.urlsafe_b64decode(
            payload["sid"] + "=" * (-len(payload["sid"]) % 4))), 32)
        with sqlite3.connect(f"file:{self.dbfile}?mode=ro", uri=True) as ro:
            row = ro.execute(
                "SELECT username_key,expires_at,revoked_at FROM concat_sso_sessions WHERE sid_hash=?",
                (issuer.sid_hash(payload["sid"]),),
            ).fetchone()
            self.assertEqual(row, ("alice", NOW + 12 * 60 * 60, None))
            with self.assertRaises(sqlite3.OperationalError):
                ro.execute("DELETE FROM concat_sso_sessions")

    def test_sid_stored_hashed_only_and_session_rotation(self):
        first = self.session["scene_sid"]
        with self.db() as conn:
            second = issuer.start_session(conn, self.session, "Alice", now=NOW + 1)
            self.assertNotEqual(first, second)
            self.assertEqual(conn.execute(
                "SELECT revoked_at FROM concat_sso_sessions WHERE sid_hash=?",
                (issuer.sid_hash(first),),
            ).fetchone()[0], NOW + 1)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM concat_sso_sessions WHERE sid_hash=?",
                (first,),
            ).fetchone()[0], 0)

    def test_logout_revokes_immediately_even_if_user_reenabled(self):
        copy = dict(self.session)
        with self.db() as conn:
            issuer.revoke_session(conn, self.session, now=NOW + 2)
            self.assertIsNone(issuer.active_sid(conn, copy, "Alice", now=NOW + 2))
        self.assert_rejected(session=copy, now=NOW + 2)

    def test_expired_sid_and_cross_user_mismatch(self):
        self.assert_rejected(now=NOW + 12 * 60 * 60)
        other = dict(self.session, username="Bob")
        self.assert_rejected(session=other)
        with self.db() as conn:
            self.assertIsNone(issuer.active_sid(conn, other, "Bob", now=NOW))

    def test_inactive_deleted_demoted_and_ownerless_accounts(self):
        with self.db() as conn:
            conn.execute("UPDATE users SET is_active=0 WHERE username='Alice'")
        self.assert_rejected()
        with self.db() as conn:
            issuer.revoke_username(conn, "Alice", now=NOW + 2)
            conn.execute("UPDATE users SET is_active=1 WHERE username='Alice'")
        self.assert_rejected(now=NOW + 2)
        with self.db() as conn:
            issuer.start_session(conn, self.session, "Alice", now=NOW + 3)
            conn.execute("UPDATE users SET is_admin=0 WHERE username='Alice'")
        claims = decode_ticket(self.authorize(now=NOW + 3))
        self.assertNotIn("is_admin", claims)
        self.assertNotIn("concat_granted", claims)
        self.assertEqual(self.account("Alice")["is_admin"], 0)
        self.assert_rejected(session={"authenticated": True, "username": "__default__"})
        self.assert_rejected(session={"authenticated": True, "username": ""})

    def test_forged_state_host_client_redirect_and_duplicate_params(self):
        self.assert_rejected(host="attacker.example")
        self.assert_rejected(host="app.scenebuilder.online.evil.test")
        self.assert_rejected(params={"client_id": ["unknown"]})
        self.assert_rejected(params={"redirect_uri": ["https://attacker.test/callback"]})
        self.assert_rejected(params={"state": ["short"]})
        self.assert_rejected(params={"state": [STATE + "/x"]})
        self.assert_rejected(params={"state": [STATE, STATE]})
        self.assert_rejected(params={"client_id": ["concat-web", "concat-web"]})
        self.assert_rejected(params={"redirect_uri": [issuer.CALLBACK, issuer.CALLBACK]})
        self.assert_rejected(params={"next": ["/"]})
        self.assert_rejected(session={"username": "Alice", "authenticated": False})

    def test_secret_independence_and_forged_ticket(self):
        self.assert_rejected(key="")
        self.assert_rejected(key="short")
        self.assert_rejected(key="existing-scene-session-key")
        self.assert_rejected(key="legacy-web-key")
        url = self.authorize()
        tampered = url.replace("ticket=", "ticket=forged", 1)
        with self.assertRaises(ValueError):
            decode_ticket(tampered)
        payload = decode_ticket(url)
        self.assertEqual(payload["state"], STATE)
        self.assertNotIn("secret_hash", payload)
        self.assertNotIn("password", payload)

    def test_migrate_signed_preissuer_cookie_and_unique_jtis(self):
        old = {"authenticated": True, "username": "Alice"}
        one = decode_ticket(self.authorize(session=old))
        self.assertIn("scene_sid", old)
        two = decode_ticket(self.authorize(session=old))
        self.assertEqual(one["sid"], two["sid"])
        self.assertNotEqual(one["jti"], two["jti"])  # consumer consumes JTI exactly once

    def test_additive_schema_and_login_logout_wiring_contract(self):
        source = (ROOT / "apps/scene-builder/part01/app.py").read_text()
        self.assertIn("_concat_sso.init_schema(conn)", source)
        self.assertIn("_concat_sso.start_session(conn, request.session", source)
        self.assertIn("_concat_sso.revoke_session(conn, request.session)", source)
        self.assertIn("_concat_sso.register_scene_sso_issuer", source)
        self.assertIn("max_age=60 * 60 * 12", source)
        self.assertIn('request.url.path.startswith("/api/cloud-render/")', source)
        self.assertIn('\"UPDATE concat_sso_sessions SET revoked_at=', source)
        self.assertNotIn("CREATE TABLE IF NOT EXISTS grants", source)


if __name__ == "__main__":
    unittest.main()

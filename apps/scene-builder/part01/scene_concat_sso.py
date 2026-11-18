"""Scene-side issuer for Concat Web, matching its strictly verified SSO consumer.

The Scene signed browser session is authoritative; never accept a username,
role, service identity or callback destination from an untrusted caller.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from urllib.parse import urlencode

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse

ISSUER = "https://app.scenebuilder.online"
CALLBACK = "https://concat.scenebuilder.online/auth/scene/callback"
CLIENT = "concat-web"
OPAQUE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
PRIVATE = {
    "Cache-Control": "private, no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


def _digest(value):
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _encoded(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def ensure_schema(conn):
    """Additive, idempotent migration. No changes to legacy user/session tables."""
    conn.execute("""CREATE TABLE IF NOT EXISTS concat_sso_sessions (
        sid_hash TEXT PRIMARY KEY,
        username_key TEXT NOT NULL,
        expires_at INTEGER NOT NULL,
        revoked_at INTEGER NULL
    )""")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_concat_sso_username ON "
        "concat_sso_sessions(username_key, revoked_at, expires_at)"
    )


def revoke_scene_sid(conn, session):
    sid = session.get("concat_scene_sid", "")
    if isinstance(sid, str) and OPAQUE.fullmatch(sid):
        conn.execute(
            "UPDATE concat_sso_sessions SET revoked_at=? "
            "WHERE sid_hash=? AND revoked_at IS NULL",
            (int(time.time()), _digest(sid)),
        )


def create_scene_sid(conn, session, username):
    """Rotate SID after successful password login and bind to canonical Scene user."""
    revoke_scene_sid(conn, session)
    sid = secrets.token_urlsafe(32)
    now = int(time.time())
    conn.execute(
        "INSERT INTO concat_sso_sessions(sid_hash,username_key,expires_at,revoked_at) "
        "VALUES(?,?,?,NULL)",
        (_digest(sid), username.casefold(), now + 12 * 3600),
    )
    session["concat_scene_sid"] = sid
    return sid


def active_scene_sid(conn, session, canonical_username):
    sid = session.get("concat_scene_sid", "")
    if not isinstance(sid, str) or not OPAQUE.fullmatch(sid):
        return None
    row = conn.execute(
        "SELECT username_key, expires_at, revoked_at FROM concat_sso_sessions WHERE sid_hash=?",
        (_digest(sid),),
    ).fetchone()
    if (row is None or row["username_key"] != canonical_username.casefold()
            or row["revoked_at"] is not None or int(row["expires_at"]) <= int(time.time())):
        return None
    return sid


def sign_ticket(username, sid, state, *, secret, now=None):
    if len(secret.encode("utf-8")) < 32:
        raise HTTPException(503, "Concat SSO unavailable")
    if not OPAQUE.fullmatch(sid) or not OPAQUE.fullmatch(state):
        raise HTTPException(400, "Invalid SSO input")
    now = int(time.time()) if now is None else int(now)
    claims = {
        "v": 1, "iss": ISSUER, "aud": CLIENT, "sub": username,
        "sid": sid, "state": state, "jti": secrets.token_urlsafe(32),
        "iat": now, "exp": now + 60,
    }
    payload = _encoded(json.dumps(claims, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    mac = hmac.new(secret.encode("utf-8"), ("scene-concat-v1." + payload).encode("ascii"), hashlib.sha256).digest()
    return payload + "." + _encoded(mac)


def register_scene_concat_sso(app, database, user_account, database_path=None):
    """Register a single pinned browser handoff route; called after session middleware."""
    @app.get("/concat-sso/authorize")
    def authorize(request: Request, client_id: str = "", redirect_uri: str = "", state: str = ""):
        # Do not trust forwarded identity, arbitrary Host, redirects or JSON claims.
        if request.headers.get("host", "").lower() != "app.scenebuilder.online":
            raise HTTPException(400, "Untrusted Scene host")
        if client_id != CLIENT or redirect_uri != CALLBACK:
            raise HTTPException(400, "Unknown SSO client or callback")
        if not isinstance(state, str) or not OPAQUE.fullmatch(state):
            raise HTTPException(400, "Invalid SSO state")
        if not request.session.get("authenticated"):
            from login_next import login_redirect_url
            target = "/concat-sso/authorize?" + urlencode({
                "client_id": CLIENT, "redirect_uri": CALLBACK, "state": state,
            })
            return RedirectResponse(login_redirect_url(target), status_code=303, headers=PRIVATE)
        username = request.session.get("username")
        account = user_account(username) if isinstance(username, str) else None
        if not account or not int(account.get("is_active") or 0):
            raise HTTPException(401, "Active Scene login required")
        canonical = account["username"]
        if not isinstance(canonical, str) or not canonical or canonical != canonical.strip():
            raise HTTPException(401, "Invalid Scene account")
        secret = os.environ.get("CONCAT_SCENE_SSO_SECRET", "")
        if len(secret.encode("utf-8")) < 32:
            raise HTTPException(503, "Concat SSO unavailable")
        with database() as conn:
            sid = active_scene_sid(conn, request.session, canonical)
            if not sid:
                raise HTTPException(401, "Scene SSO session expired or revoked")
        ticket = sign_ticket(canonical, sid, state, secret=secret)
        response = RedirectResponse(CALLBACK + "?" + urlencode({
            "ticket": ticket, "state": state,
        }), status_code=303, headers=PRIVATE)
        return response

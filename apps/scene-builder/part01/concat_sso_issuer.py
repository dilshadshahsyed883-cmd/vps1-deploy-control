"""Scene-only issuer for the independent Concat Web consumer (VPS1 #772).

Scene is the sole account authority. Concat grants and callback ticket replay
protection live in Concat Web. This module never reads Concat credentials or
changes legacy Scene/Cloud Render authorization.
"""
from __future__ import annotations

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

ISSUER = "https://app.scenebuilder.online"
CLIENT = "concat-web"
CALLBACK = "https://concat.scenebuilder.online/auth/scene/callback"
SID_SECONDS = 12 * 60 * 60
TICKET_SECONDS = 60
SID_KEY = "scene_sid"
URLSAFE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
STATE = re.compile(r"^[A-Za-z0-9_-]{32,200}$")
USERNAME = re.compile(r"^[^\s/\\:\x00-\x1f\x7f]{1,128}$")
PRIVATE_HEADERS = {
    "Cache-Control": "private, no-store",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Vary": "Cookie",
}


class SSORejected(ValueError):
    """Invalid browser state, client, account, host or signed Scene session."""


class SSOUnavailable(RuntimeError):
    """The separate Concat signing key or Scene session storage is unavailable."""


def sid_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


def init_schema(conn: sqlite3.Connection) -> None:
    """Additive, idempotent migration of only the Scene-side handoff table."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS concat_sso_sessions (
            sid_hash TEXT PRIMARY KEY,
            username_key TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            revoked_at INTEGER NULL
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_concat_sso_user ON "
        "concat_sso_sessions(username_key, revoked_at)"
    )


def revoke_session(conn: sqlite3.Connection, scene_session: dict,
                   *, now: int | None = None) -> None:
    """Revoke before deleting a Scene cookie, including account invalidation."""
    raw = scene_session.pop(SID_KEY, "")
    if isinstance(raw, str) and URLSAFE.fullmatch(raw):
        conn.execute(
            "UPDATE concat_sso_sessions SET revoked_at=? "
            "WHERE sid_hash=? AND revoked_at IS NULL",
            (int(time.time()) if now is None else now, sid_hash(raw)),
        )


def revoke_username(conn: sqlite3.Connection, username: str,
                    *, now: int | None = None) -> None:
    """Account disable/delete invalidates prior handoffs even after re-enable."""
    if not isinstance(username, str) or not username:
        return
    conn.execute(
        "UPDATE concat_sso_sessions SET revoked_at=? "
        "WHERE username_key=? AND revoked_at IS NULL",
        (int(time.time()) if now is None else now, username.casefold()),
    )


def start_session(conn: sqlite3.Connection, scene_session: dict, username: str,
                  *, now: int | None = None) -> str:
    """Allocate a CSPRNG 256-bit SID; persist *only its SHA-256* server-side.

    Plain SID is stored only inside Scene's existing signed, Secure, HttpOnly,
    host-only Starlette session cookie, never in the SQLite row or logs.
    """
    if not isinstance(username, str) or not USERNAME.fullmatch(username) or username == "__default__":
        raise SSORejected("Invalid Scene account")
    current = int(time.time()) if now is None else now
    raw = secrets.token_urlsafe(32)
    revoke_session(conn, scene_session, now=current)
    conn.execute(
        "INSERT INTO concat_sso_sessions(sid_hash,username_key,expires_at,revoked_at) "
        "VALUES(?,?,?,NULL)",
        (sid_hash(raw), username.casefold(), current + SID_SECONDS),
    )
    scene_session[SID_KEY] = raw
    return raw


def active_sid(conn: sqlite3.Connection, scene_session: dict, username: str,
               *, now: int | None = None) -> str | None:
    """DB-backed, owner-bound and time-bounded validation; never trust a SID alone."""
    raw = scene_session.get(SID_KEY)
    if not isinstance(raw, str) or not URLSAFE.fullmatch(raw):
        return None
    row = conn.execute(
        "SELECT username_key,expires_at,revoked_at FROM concat_sso_sessions WHERE sid_hash=?",
        (sid_hash(raw),),
    ).fetchone()
    if not row:
        return None
    return raw if (row[0] == username.casefold() and row[2] is None
                   and int(row[1]) > (int(time.time()) if now is None else now)) else None


def signing_key(session_secret: str, web_access_key: str, raw_key: str | None = None) -> bytes:
    key = os.getenv("CONCAT_SCENE_SSO_SECRET", "") if raw_key is None else raw_key
    if not key or len(key.encode("utf-8")) < 32 or key in (session_secret, web_access_key):
        raise SSOUnavailable("Separate strong Concat SSO key not configured")
    return key.encode("utf-8")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def ticket_for(username: str, sid: str, state: str, key: bytes, *, now: int | None = None) -> str:
    current = int(time.time()) if now is None else now
    payload = {
        "v": 1, "iss": ISSUER, "aud": CLIENT, "sub": username,
        "sid": sid, "state": state, "jti": secrets.token_urlsafe(32),
        "iat": current, "exp": current + TICKET_SECONDS,
    }
    encoded = _b64(json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8"))
    mac = hmac.new(key, ("scene-concat-v1." + encoded).encode("ascii"), hashlib.sha256).digest()
    return encoded + "." + _b64(mac)


def authorize_handoff(request, db_factory, get_account, *,
                      scene_session_secret: str, web_access_key: str = "",
                      key_override: str | None = None, now: int | None = None) -> str:
    """Return one pinned callback URL after validating the REAL Scene cookie.

    The caller must use existing Scene SessionMiddleware and authenticated
    middleware. Browser-supplied identity, forwarded headers and Concat grants
    never elevate the account.
    """
    host = request.headers.get("host", "").lower()
    if host not in ("app.scenebuilder.online", "app.scenebuilder.online:443"):
        raise SSORejected("Untrusted Scene host")
    params = request.query_params
    if set(params.keys()) != {"client_id", "redirect_uri", "state"}:
        raise SSORejected("Unexpected SSO parameter")
    if any(len(params.getlist(name)) != 1 for name in ("client_id", "redirect_uri", "state")):
        raise SSORejected("Duplicated SSO parameter")
    client = params.get("client_id")
    redirect_uri = params.get("redirect_uri")
    state = params.get("state")
    if client != CLIENT or redirect_uri != CALLBACK or not isinstance(state, str) or not STATE.fullmatch(state):
        raise SSORejected("Invalid Concat SSO client or state")
    scene = request.session
    if scene.get("authenticated") is not True:
        raise SSORejected("Scene login required")
    name = scene.get("username")
    if not isinstance(name, str) or not USERNAME.fullmatch(name) or name == "__default__":
        raise SSORejected("Invalid Scene identity")
    account = get_account(name)
    if (not account or not int(account.get("is_active") or 0)
            or account.get("username") != name):
        raise SSORejected("Inactive or noncanonical Scene account")
    # Fail closed if disabled. No fallback to SESSION_SECRET or WEB_ACCESS_KEY.
    key = signing_key(scene_session_secret, web_access_key, key_override)
    current = int(time.time()) if now is None else now
    try:
        with db_factory() as conn:
            sid = active_sid(conn, scene, name, now=current)
            if sid is None:
                if SID_KEY in scene:
                    raise SSORejected("Revoked or expired Scene session")
                # Compatibility with an already-authenticated pre-issuer Scene
                # cookie. It is signed by the same existing Scene middleware.
                sid = start_session(conn, scene, name, now=current)
    except sqlite3.Error as exc:
        raise SSOUnavailable("Scene SSO session storage unavailable") from exc
    ticket = ticket_for(name, sid, state, key, now=current)
    return CALLBACK + "?" + urlencode({"ticket": ticket, "state": state})


def register_scene_sso_issuer(app, db_factory, get_account, scene_session_secret: str,
                              web_access_key: str = "") -> None:
    """One opt-in endpoint; existing auth, cookies and Cloud Render remain intact."""
    from fastapi import HTTPException, Request
    from fastapi.responses import RedirectResponse

    async def authorize(request: Request):
        try:
            target = authorize_handoff(
                request, db_factory, get_account,
                scene_session_secret=scene_session_secret,
                web_access_key=web_access_key,
            )
        except SSORejected:
            raise HTTPException(401, "Invalid Scene SSO handoff") from None
        except SSOUnavailable:
            raise HTTPException(503, "Scene SSO unavailable") from None
        return RedirectResponse(target, status_code=303, headers=PRIVATE_HEADERS)

    app.add_api_route("/concat-sso/authorize", authorize, methods=["GET"],
                      include_in_schema=False)

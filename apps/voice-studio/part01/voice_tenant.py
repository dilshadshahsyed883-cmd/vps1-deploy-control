"""Voice Studio identity context, always resolved from the live Scene session.

Do not trust browser-provided usernames, role headers, or local storage.
Voice is an independent upstream, so we introspect the same-origin Scene
session over the loopback-only application listener.
"""
from __future__ import annotations

from contextvars import ContextVar
import json
import re
import urllib.error
import urllib.request

from fastapi import HTTPException

_USER: ContextVar[str] = ContextVar("voice_owner", default="")
_ADMIN: ContextVar[bool] = ContextVar("voice_admin", default=False)
_IDENTITY_URL = "http://127.0.0.1:8888/api/session/identity"
_OWNER_PATTERN = re.compile(r"[A-Za-z0-9_.-]{3,40}")


def scene_identity(cookie: str) -> dict:
    """Revalidate an authenticated Scene cookie against its current DB role."""
    if not cookie:
        raise HTTPException(status_code=401, detail="Scene login required")
    request = urllib.request.Request(
        _IDENTITY_URL,
        headers={"Cookie": cookie, "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=4) as response:
            if response.status != 200:
                raise HTTPException(status_code=401, detail="Scene login required")
            payload = json.loads(response.read(8192))
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="Scene session verification unavailable") from exc
    if not isinstance(payload, dict) or payload.get("authenticated") is not True:
        raise HTTPException(status_code=401, detail="Scene login required")
    username = str(payload.get("username") or "")
    if not _OWNER_PATTERN.fullmatch(username):
        raise HTTPException(status_code=403, detail="Unrecognized account identity")
    return {
        "username": username,
        "is_admin": payload.get("is_admin") is True,
        "allowed_studios": payload.get("allowed_studios") if isinstance(payload.get("allowed_studios"), list) else [],
    }


def set_identity(username: str, is_admin: bool):
    return _USER.set(username), _ADMIN.set(bool(is_admin))


def reset_identity(tokens) -> None:
    user, admin = tokens
    _USER.reset(user)
    _ADMIN.reset(admin)


def owner() -> str:
    """Use only the verified, per-request owner; fail closed for direct calls."""
    username = _USER.get()
    if not username:
        raise HTTPException(status_code=401, detail="Verified Voice Studio identity required")
    return username


def require_admin() -> None:
    owner()
    if not _ADMIN.get():
        raise HTTPException(status_code=403, detail="Shared Voice worker settings require Admin")

"""Admin-only, browser-managed Google OAuth web client for Auto Stream.

Stores the Google OAuth client secret encrypted under Auto Stream's own
persistent data directory. Does not read or change Auto Uploader credentials.
Browser writes require Scene's trusted admin headers and same-site CSRF proof.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
from threading import RLock

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from auto_stream_engine import service

REDIRECT = "https://app.scenebuilder.online/short-stream/engine/oauth/callback"
ORIGIN = "https://app.scenebuilder.online"
ID_PATTERN = re.compile(r"^[a-zA-Z0-9._-]{10,245}\.apps\.googleusercontent\.com$")
SECRET_PATTERN = re.compile(r"^[\x21-\x7e]{6,512}$")
router = APIRouter(prefix="/engine", tags=["Auto Stream OAuth client admin"])


class OAuthClientStore:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else service().root
        if self.root.is_symlink():
            raise ValueError("Untrusted Auto Stream storage location")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "oauth-client.enc"
        self.lock = RLock()

    @staticmethod
    def _fernet():
        try:
            from cryptography.fernet import Fernet
            return Fernet(os.environ["AUTO_STREAM_FERNET_KEY"].encode("ascii"))
        except (KeyError, ValueError, ImportError) as exc:
            raise ValueError("Auto Stream encryption vault is not ready") from exc

    def _read(self):
        if self.path.is_symlink():
            raise ValueError("Untrusted Auto Stream client file")
        if not self.path.exists():
            return None
        if not self.path.is_file():
            raise ValueError("Untrusted Auto Stream client file")
        mode = self.path.stat().st_mode
        if not stat.S_ISREG(mode) or mode & 0o077:
            raise ValueError("Auto Stream client file permissions are unsafe")
        try:
            from cryptography.fernet import InvalidToken
            data = json.loads(self._fernet().decrypt(self.path.read_bytes()))
        except (InvalidToken, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("Auto Stream client configuration cannot be decrypted") from exc
        if (not isinstance(data, dict) or
                not ID_PATTERN.fullmatch(str(data.get("client_id",""))) or
                not SECRET_PATTERN.fullmatch(str(data.get("client_secret","")))):
            raise ValueError("Invalid stored Google OAuth client")
        return data

    def read_client(self):
        """Used only inside the backend OAuth code. Never return to the browser."""
        with self.lock:
            item = self._read()
            if item is None:
                return None
            return (item["client_id"], item["client_secret"], REDIRECT)

    def _protected_credentials_exist(self):
        db = self.root / "integrations.sqlite3"
        if not db.exists():
            return False
        if db.is_symlink() or not db.is_file():
            raise ValueError("Untrusted Auto Stream integration database")
        try:
            with sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=6) as c:
                tables = {r[0] for r in c.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                if "channels" in tables and c.execute(
                    "SELECT 1 FROM channels WHERE authorized=1 AND token IS NOT NULL LIMIT 1"
                ).fetchone():
                    return True
                if "oauth_state" in tables and c.execute(
                    "SELECT 1 FROM oauth_state WHERE expires>? LIMIT 1",
                    (datetime.now(timezone.utc).isoformat(),)
                ).fetchone():
                    return True
        except sqlite3.DatabaseError as exc:
            raise ValueError("Cannot safely check existing OAuth grants") from exc
        return False

    def public(self):
        with self.lock:
            item = self._read()
            if item:
                return {"configured": True, "source": "studio",
                        "client_id": item["client_id"],
                        "redirect_uri": REDIRECT, "updated_at": item["updated_at"],
                        "secret_stored": True}
            legacy = (os.environ.get("AUTO_STREAM_GOOGLE_CLIENT_ID") and
                      os.environ.get("AUTO_STREAM_GOOGLE_CLIENT_SECRET"))
            return {"configured": bool(legacy), "source": "environment" if legacy else "none",
                    "client_id": os.environ.get("AUTO_STREAM_GOOGLE_CLIENT_ID","") if legacy else "",
                    "redirect_uri": REDIRECT, "updated_at": None,
                    "secret_stored": bool(legacy)}

    def save(self, client_id: str, client_secret: str):
        client_id, client_secret = str(client_id).strip(), str(client_secret).strip()
        if not ID_PATTERN.fullmatch(client_id):
            raise ValueError("Enter a valid Google OAuth Web application Client ID")
        if not SECRET_PATTERN.fullmatch(client_secret):
            raise ValueError("Enter the complete Google OAuth Client Secret")
        with self.lock:
            if self._protected_credentials_exist():
                raise ValueError("Existing channel consent/pending OAuth must be disconnected or completed before changing client")
            doc = {"client_id": client_id, "client_secret": client_secret,
                   "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            token = self._fernet().encrypt(json.dumps(doc).encode("utf-8"))
            if self.path.is_symlink():
                raise ValueError("Untrusted Auto Stream client file")
            temp = self.root / (".oauth-client-" + secrets.token_hex(10))
            try:
                fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(token)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp, self.path)
            finally:
                if temp.exists():
                    temp.unlink()
            return self.public()

    def delete(self):
        with self.lock:
            if self._protected_credentials_exist():
                raise ValueError("Disconnect authorized channels before removing OAuth client")
            if self.path.is_symlink():
                raise ValueError("Untrusted Auto Stream client file")
            if self.path.exists():
                self.path.unlink()
            return self.public()


_instance = None
_global_lock = RLock()


def store():
    global _instance
    with _global_lock:
        if _instance is None:
            _instance = OAuthClientStore()
        return _instance


def require_admin(request: Request, *, write: bool = False):
    if (request.headers.get("x-is-admin") != "1" or
            not request.headers.get("x-auth-user")):
        raise HTTPException(403, "Auto Stream OAuth setup requires an admin login")
    if write and (request.headers.get("origin") != ORIGIN or
                  request.headers.get("x-auto-stream-config-action") != "1" or
                  request.headers.get("host") != "app.scenebuilder.online"):
        raise HTTPException(403, "Same-origin admin action required")


def _safe_result(fn):
    try:
        return fn()
    except ValueError as exc:
        # Static, credential-independent error messages only.
        raise HTTPException(409, str(exc)) from None


@router.get("/oauth/client")
def client_status(request: Request):
    require_admin(request)
    return JSONResponse(_safe_result(lambda: store().public()),
                        headers={"Cache-Control":"no-store"})


@router.post("/oauth/client")
async def client_save(request: Request):
    require_admin(request, write=True)
    if request.headers.get("content-type","").split(";",1)[0].strip().lower() != "application/json":
        raise HTTPException(415, "JSON required")
    if len(await request.body()) > 4096:
        raise HTTPException(413, "Configuration is too large")
    try:
        payload = await request.json()
    except (ValueError, UnicodeError):
        raise HTTPException(400, "Invalid configuration JSON") from None
    if not isinstance(payload, dict):
        raise HTTPException(400, "Invalid configuration form")
    if set(payload) != {"client_id","client_secret"}:
        raise HTTPException(400, "Client ID and Client Secret are required")
    if not isinstance(payload["client_id"], str) or not isinstance(payload["client_secret"], str):
        raise HTTPException(400, "Invalid configuration values")
    return JSONResponse(_safe_result(lambda: store().save(
        payload["client_id"], payload["client_secret"])),
        headers={"Cache-Control":"no-store"})


@router.delete("/oauth/client")
def client_remove(request: Request):
    require_admin(request, write=True)
    return JSONResponse(_safe_result(lambda: store().delete()),
                        headers={"Cache-Control":"no-store"})


@router.post("/oauth/client/json")
async def client_json_upload(request: Request):
    require_admin(request, write=True)
    raw = await request.body()
    if len(raw) > 32768:
        raise HTTPException(413, "OAuth JSON must be smaller than 32 KB")
    try:
        doc = json.loads(raw)
    except (ValueError, UnicodeError):
        raise HTTPException(400, "Malformed Google OAuth JSON") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("web"), dict):
        raise HTTPException(422, "Upload Google OAuth Web application JSON; desktop and service-account credentials cannot use this HTTPS callback")
    web = doc["web"]
    if REDIRECT not in web.get("redirect_uris", []):
        raise HTTPException(422, "Add the Auto Stream HTTPS callback to Authorized redirect URIs in Google Cloud, then download the JSON again")
    return JSONResponse(_safe_result(lambda: store().save(
        web.get("client_id", ""), web.get("client_secret", ""))),
        headers={"Cache-Control": "no-store"})

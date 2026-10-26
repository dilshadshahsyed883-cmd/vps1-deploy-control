"""Explicit, scoped SML library reset. Never runs without an owner confirmation.

Media/catalog data only: app settings, tokens, proxy profiles, installed AI models,
and everything outside SML_DATA_ROOT are intentionally retained.
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
from pathlib import Path

from fastapi import HTTPException, Request
from pydantic import BaseModel

CONFIRMATION = "CLEAN SML LIBRARY"
MEDIA_DIRS = (
    "sources", "clips", "thumbs", "metadata", "temp", "images",
    "image_previews", "media_cache", "ai_previews", "ai_references",
)
_active_clean = threading.Lock()


class CleanRequest(BaseModel):
    confirmation: str
    preview_token: str
    acknowledge_editor_references: bool = False


def _secret(root: Path) -> bytes:
    path = root / ".library-clean-secret"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(secrets.token_bytes(32))
    except FileExistsError:
        pass
    key = path.read_bytes()
    if len(key) != 32:
        raise HTTPException(503, "Library confirmation key is unavailable")
    return key


def _count_files(root: Path):
    total = files = 0
    for name in MEDIA_DIRS:
        folder = root / name
        if folder.is_symlink():
            raise HTTPException(409, "Unsafe storage link in SML: " + name)
        if not folder.exists():
            continue
        for parent, dirs, filenames in os.walk(folder, followlinks=False):
            # Symlinks are counted as entries but never dereferenced.
            for filename in filenames:
                path = Path(parent) / filename
                files += 1
                if not path.is_symlink():
                    try:
                        total += path.stat().st_size
                    except FileNotFoundError:
                        pass
            for dirname in dirs:
                if (Path(parent) / dirname).is_symlink():
                    files += 1
    return files, total


def _snapshot(conn: sqlite3.Connection, root: Path) -> dict:
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )}
    names = (
        "clips", "image_assets", "sources", "media_imports", "ai_jobs",
        "media_reservations", "people", "sml_person_collections",
    )
    counts = {name: conn.execute('SELECT COUNT(*) FROM "' + name + '"').fetchone()[0]
              if name in tables else 0 for name in names}
    trashed_clips = conn.execute(
        "SELECT COUNT(*) FROM clips WHERE deleted_at IS NOT NULL"
    ).fetchone()[0] if "clips" in tables else 0
    trashed_images = conn.execute(
        "SELECT COUNT(*) FROM image_assets WHERE deleted_at IS NOT NULL"
    ).fetchone()[0] if "image_assets" in tables else 0
    committed = conn.execute(
        "SELECT COUNT(*) FROM media_reservations WHERE state='committed'"
    ).fetchone()[0] if "media_reservations" in tables else 0
    files, size = _count_files(root)
    return {
        "videos": counts["clips"] - trashed_clips,
        "images": counts["image_assets"] - trashed_images,
        "trash_videos": trashed_clips, "trash_images": trashed_images,
        "sources": counts["sources"], "imports": counts["media_imports"],
        "ai_jobs": counts["ai_jobs"],
        "named_people": counts["people"] + counts["sml_person_collections"],
        "committed_editor_references": committed,
        "files": files, "bytes": size,
    }


def _blockers(conn: sqlite3.Connection, process_lock) -> list[str]:
    issues = []
    if process_lock.locked():
        issues.append("Video processing is running")
    active = (
        ("sources", "status IN ('queued','processing')", "Video sources are pending or processing"),
        ("media_imports", "status IN ('queued','processing')", "Imports are queued or running"),
        ("ai_jobs", "state IN ('queued','running')", "AI jobs are queued or running"),
        ("people_scans", "status IN ('queued','running','processing')", "Image face scans are running"),
        ("person_video_scans", "status IN ('queued','running','processing')", "Video face scans are running"),
        ("media_reservations", "state='reserved' AND expires_at>" + str(int(time.time())),
         "Scene Builder is using active media reservations"),
    )
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, where, label in active:
        if table in tables and conn.execute('SELECT 1 FROM "' + table + '" WHERE ' + where + ' LIMIT 1').fetchone():
            issues.append(label)
    return issues


def _fingerprint(snapshot: dict) -> str:
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _issue_token(root: Path, snapshot: dict) -> str:
    payload = json.dumps({
        "exp": int(time.time()) + 300, "fingerprint": _fingerprint(snapshot)
    }, separators=(",", ":")).encode()
    body = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
    signature = hmac.new(_secret(root), body.encode(), hashlib.sha256).hexdigest()
    return body + "." + signature


def _verify_token(root: Path, token: str, snapshot: dict):
    try:
        body, signature = token.split(".", 1)
        expected = hmac.new(_secret(root), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError()
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if payload["exp"] < time.time() or payload["fingerprint"] != _fingerprint(snapshot):
            raise ValueError()
    except (TypeError, ValueError, KeyError, UnicodeError):
        raise HTTPException(409, "Library changed or confirmation expired. Refresh Clean Library preview.") from None


def _clear_media_files(root: Path):
    for name in MEDIA_DIRS:
        folder = root / name
        if folder.is_symlink():
            raise RuntimeError("Refusing linked library folder: " + name)
        if folder.exists():
            shutil.rmtree(folder)
        folder.mkdir(parents=True, exist_ok=True)


def register(app, root, db, process_lock):
    root = Path(root).resolve()
    if not root.is_dir():
        raise RuntimeError("SML root is not a directory")

    @app.middleware("http")
    async def block_writes_during_clean(request: Request, call_next):
        if request.method in ("POST", "PATCH", "PUT", "DELETE") and not request.url.path.startswith("/api/library/clean"):
            try:
                with db() as conn:
                    flag = conn.execute("SELECT value FROM settings WHERE key='library_cleaning'").fetchone()
                    if flag and flag[0] == "true":
                        from fastapi.responses import JSONResponse
                        return JSONResponse({"detail": "SML library cleanup in progress"}, status_code=503)
            except sqlite3.OperationalError:
                pass  # Allow app startup to initialize the schema.
        return await call_next(request)

    @app.get("/api/library/clean/preview")
    def clean_preview():
        with db() as conn:
            snapshot = _snapshot(conn, root)
            blockers = _blockers(conn, process_lock)
            maintenance = conn.execute(
                "SELECT value FROM settings WHERE key='library_cleaning'"
            ).fetchone()
            if maintenance and maintenance[0] == "true":
                blockers.append("Another library cleanup is in progress")
        return {
            **snapshot, "ready": not blockers, "blockers": blockers,
            "confirmation_phrase": CONFIRMATION,
            "preview_token": _issue_token(root, snapshot),
            "preserved": ["App configuration", "Proxy credentials", "AI models", "Other VPS apps"],
        }

    @app.post("/api/library/clean")
    def clean_all(body: CleanRequest, request: Request):
        if request.headers.get("x-sml-clean-intent") != "confirmed":
            raise HTTPException(403, "Clean Library requires the confirmation dialog")
        if request.headers.get("sec-fetch-site") not in (None, "none", "same-origin"):
            raise HTTPException(403, "Clean Library must be opened from the SML website")
        if body.confirmation != CONFIRMATION or not body.acknowledge_editor_references:
            raise HTTPException(400, "Enter the exact confirmation and acknowledge project references")
        if not _active_clean.acquire(blocking=False):
            raise HTTPException(409, "Library cleanup already running")
        lock_path = root / ".library-clean.lock"
        try:
            with lock_path.open("a+b") as lock_file:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise HTTPException(409, "Library cleanup already running") from None
                with db() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    snapshot = _snapshot(conn, root)
                    _verify_token(root, body.preview_token, snapshot)
                    issues = _blockers(conn, process_lock)
                    if issues:
                        raise HTTPException(409, "; ".join(issues))
                    conn.execute(
                        "INSERT INTO settings(key,value) VALUES('library_cleaning','true') "
                        "ON CONFLICT(key) DO UPDATE SET value='true'"
                    )
                try:
                    with db() as conn:
                        conn.execute("BEGIN IMMEDIATE")
                        issues = _blockers(conn, process_lock)
                        if issues:
                            raise HTTPException(409, "; ".join(issues))
                        # SML_DATA_ROOT/library.db belongs to SML only; retain settings,
                        # table definitions, indexes and migrations for instant reuse.
                        table_names = [row[0] for row in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                        )]
                        for table in table_names:
                            if table == "settings":
                                continue
                            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
                                raise RuntimeError("Unexpected SQLite table name")
                            conn.execute('DELETE FROM "' + table + '"')
                    _clear_media_files(root)
                finally:
                    with db() as conn:
                        conn.execute(
                            "UPDATE settings SET value='false' WHERE key='library_cleaning'"
                        )
                return {
                    "ok": True, "status": "cleaned", "deleted": snapshot,
                    "message": "SML media, Trash, sources, AI data and media history cleared",
                }
        finally:
            _active_clean.release()

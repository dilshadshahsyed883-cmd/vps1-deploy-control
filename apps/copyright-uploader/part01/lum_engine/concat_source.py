"""Owner-verified Cloud Render MP4 handoff for the Concat Uploader.

The browser URL is solely an identifier. Never request it over HTTP or use a
user-supplied filesystem path. Legacy ownerless media is deliberately denied.
Agent 4 must add verified owner_username to Cloud Render's files table before
this source can be admitted.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import sqlite3
import uuid
from pathlib import Path
from urllib.parse import urlsplit, unquote

LINK_RE = re.compile(r"^/api/cloud-render/media/([a-zA-Z0-9_-]{1,128})/file$")
SHA_RE = re.compile(r"^[a-fA-F0-9]{64}$")
MAX_MP4 = 12 * 1024 ** 3


class ConcatSourceError(ValueError):
    """Safe admission failure, never a reason to fall back to remote download."""


def parse_concat_link(link: str, *, origin: str = "https://app.scenebuilder.online") -> str:
    raw = str(link or "").strip()
    if not raw or len(raw) > 2048 or any(ord(c) < 32 for c in raw) or "\\" in raw:
        raise ConcatSourceError("A valid Cloud Render Copy Video Link is required")
    url = urlsplit(raw)
    trusted = urlsplit(origin)
    if (url.scheme != "https" or url.netloc.lower() != trusted.netloc.lower()
            or url.username or url.password or url.fragment or url.query):
        raise ConcatSourceError("Only the current website's Cloud Render video links are accepted")
    if "%" in url.path or unquote(url.path) != url.path:
        raise ConcatSourceError("Encoded Cloud Render paths are not accepted")
    match = LINK_RE.fullmatch(url.path)
    if not match:
        raise ConcatSourceError("Link must point to a completed Cloud Render MP4")
    return match.group(1)


def _owned_verified_file(con: sqlite3.Connection, job: str, owner: str) -> dict:
    columns = {str(row[1]) for row in con.execute("PRAGMA table_info(files)")}
    if "owner_username" not in columns:
        raise ConcatSourceError("Cloud Render per-user isolation is not enabled yet")
    row = con.execute(
        "SELECT id,name,size,sha256,state,owner_username FROM files WHERE id=?", (job,)
    ).fetchone()
    if (not row or not row["owner_username"]
            or str(row["owner_username"]).strip().casefold() != str(owner).strip().casefold()):
        raise ConcatSourceError("Video is unavailable in this account")
    info = dict(row)
    if (info["state"] != "ready" or not 0 < int(info["size"]) <= MAX_MP4
            or not SHA_RE.fullmatch(str(info["sha256"]))):
        raise ConcatSourceError("Cloud Render video is not a verified ready MP4")
    return info


def stage_concat_link(link: str, owner: str, staging_root: Path, *,
                      cloud_root: Path, origin: str = "https://app.scenebuilder.online") -> tuple[str, str, int, str]:
    """Return a *private copy* (path, media_name, byte_count, SHA-256).

    Never hardlink Cloud Render media into a YouTube worker. A hardlink shares
    a mutable inode: even after a checksum passes, renderer/retention changes
    to the original could silently change uploaded bytes. Copy, fsync, verify,
    and leave the canonical Cloud Render / Drive original untouched.
    """
    job = parse_concat_link(link, origin=origin)
    if not owner or not str(owner).strip():
        raise ConcatSourceError("Login is required")
    root = Path(cloud_root)
    destination_root = Path(staging_root)
    if (not root.is_absolute() or root.is_symlink() or
            not destination_root.is_absolute() or destination_root.is_symlink()):
        raise ConcatSourceError("Verified storage configuration is unavailable")
    database = root / "cloud_render.sqlite3"
    media_root = root / "media"
    if not database.is_file() or database.is_symlink() or not media_root.is_dir() or media_root.is_symlink():
        raise ConcatSourceError("Cloud Render verified storage is unavailable")
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        info = _owned_verified_file(con, job, owner)
    source = media_root / f"{job}.mp4"
    if (not source.is_file() or source.is_symlink()
            or source.resolve().parent != media_root.resolve()):
        # Never treat a Drive URL/path as permission to fetch arbitrary content.
        raise ConcatSourceError("Local MP4 is unavailable; authorized Drive handoff is required")
    destination_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    expected_size = int(info["size"])
    # Guard precious VPS storage. No staging job may exhaust the filesystem.
    if shutil.disk_usage(destination_root).free < expected_size + 1024 * 1024 * 1024:
        raise ConcatSourceError("Insufficient safe VPS staging capacity for this MP4")
    destination = destination_root / ("concat_" + uuid.uuid4().hex + ".mp4")
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        source_fd = os.open(source, flags)
        with os.fdopen(source_fd, "rb") as inp:
            stat_info = os.fstat(inp.fileno())
            if not stat.S_ISREG(stat_info.st_mode) or stat_info.st_size != expected_size:
                raise ConcatSourceError("Cloud Render media changed before secure handoff")
            if inp.read(8)[4:8] != b"ftyp":
                raise ConcatSourceError("Cloud Render video is not an MP4")
            inp.seek(0)
            fd = os.open(destination,
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                         0o600)
            digest = hashlib.sha256()
            total = 0
            with os.fdopen(fd, "wb") as out:
                while True:
                    chunk = inp.read(4 * 1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > expected_size:
                        raise ConcatSourceError("Cloud Render media size changed")
                    digest.update(chunk)
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
        if total != expected_size or destination.stat().st_size != expected_size:
            raise ConcatSourceError("Cloud Render media size changed")
        if digest.hexdigest().lower() != str(info["sha256"]).lower():
            raise ConcatSourceError("Cloud Render media checksum changed")
        return str(destination), str(info["name"]), total, digest.hexdigest()
    except BaseException:
        destination.unlink(missing_ok=True)
        raise

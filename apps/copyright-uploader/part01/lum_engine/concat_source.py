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
import subprocess
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
    extra = (",drive_path" if "drive_path" in columns else ",'' AS drive_path")
    extra += (",md5" if "md5" in columns else ",'' AS md5")
    row = con.execute(
        "SELECT id,name,size,sha256,state,owner_username" + extra +
        " FROM files WHERE id=? AND owner_username=?", (job, str(owner).strip().casefold())
    ).fetchone()
    if not row or not row["owner_username"]:
        raise ConcatSourceError("Video is unavailable in this account")
    info = dict(row)
    if (info["state"] not in {"ready", "verified", "delivered"}
            or not 0 < int(info["size"]) <= MAX_MP4
            or not SHA_RE.fullmatch(str(info["sha256"]))):
        raise ConcatSourceError("Cloud Render video is not a verified MP4")
    if info["state"] in {"verified", "delivered"} and not info["drive_path"]:
        raise ConcatSourceError("Drive-backed video has no verified delivery record")
    return info


def _canonical_drive_source(remote_path: str, owner: str, job: str) -> str:
    """Require the exact recorded canonical destination, not a user-provided URL."""
    remote = os.getenv("CLOUD_RENDER_DRIVE_REMOTE", "gdrive")
    folder = os.getenv("CLOUD_RENDER_DRIVE_FOLDER", "Cloud Render")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", remote) or not re.fullmatch(r"[A-Za-z0-9_ /-]{1,120}", folder):
        raise ConcatSourceError("Canonical Drive Bridge settings are invalid")
    prefix = remote + ":" + folder.strip("/") + "/"
    if not remote_path.startswith(prefix):
        raise ConcatSourceError("Video Drive delivery is outside the canonical bridge")
    name = remote_path[len(prefix):]
    # The existing verified legacy folder layout is supported only after the
    # record has been independently attributed to this authenticated owner.
    allowed = name.startswith(owner + "/" + job + "_") or name.startswith(job + "_")
    if (not allowed or not name.endswith(".mp4") or
            ".." in name or "\\" in name or any(ord(c) < 32 for c in name)):
        raise ConcatSourceError("Video Drive path is not a verified Concat delivery")
    return remote_path


def _canonical_drive_env() -> dict[str, str]:
    """Same HOME/XDG rclone identity as Scene's canonical Drive Bridge.

    No OAuth files are copied, regenerated or returned to the caller.
    """
    home = Path("/var/lib/fresh-website/scene-builder/worker-home")
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    configured = env.get("RCLONE_CONFIG", "").strip()
    if configured and (not Path(configured).is_file() or not os.access(configured, os.R_OK)):
        env.pop("RCLONE_CONFIG", None)
    return env


def _stage_drive(remote: str, destination: Path) -> None:
    if not shutil.which("rclone"):
        raise ConcatSourceError("Canonical Drive Bridge is unavailable")
    try:
        proc = subprocess.run(
            ["rclone", "copyto", remote, str(destination), "--immutable"],
            env=_canonical_drive_env(), capture_output=True, timeout=3600, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ConcatSourceError("Canonical Drive Bridge retrieval failed") from None
    if proc.returncode:
        raise ConcatSourceError("Canonical Drive Bridge retrieval failed")


def stage_concat_link(link: str, owner: str, staging_root: Path, *,
                      cloud_root: Path, origin: str = "https://app.scenebuilder.online") -> tuple[str, str, int, str]:
    """Stage a private verified copy from VPS or canonical Drive Bridge.

    The caller's URL is only an opaque identifier. It is never fetched.
    Owner, readiness and digest are verified from the Scene-owned SQLite row.
    """
    job = parse_concat_link(link, origin=origin)
    owner = str(owner or "").strip().casefold()
    if not owner:
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
    if source.is_symlink():
        raise ConcatSourceError("Unsafe local Concat media path")
    local = source.is_file() and source.resolve().parent == media_root.resolve()
    if not local and not info["drive_path"]:
        raise ConcatSourceError("Local MP4 unavailable; no verified Drive delivery exists")
    destination_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    expected_size = int(info["size"])
    if shutil.disk_usage(destination_root).free < expected_size + 1024 * 1024 * 1024:
        raise ConcatSourceError("Insufficient safe VPS staging capacity for this MP4")
    destination = destination_root / ("concat_" + uuid.uuid4().hex + ".mp4")
    try:
        if local:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            source_fd = os.open(source, flags)
            with os.fdopen(source_fd, "rb") as inp:
                stat_info = os.fstat(inp.fileno())
                if not stat.S_ISREG(stat_info.st_mode) or stat_info.st_size != expected_size:
                    raise ConcatSourceError("Cloud Render media changed before secure handoff")
                fd = os.open(destination,
                             os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                             0o600)
                with os.fdopen(fd, "wb") as out:
                    shutil.copyfileobj(inp, out, 4 * 1024 * 1024)
                    out.flush()
                    os.fsync(out.fileno())
        else:
            verified = _canonical_drive_source(str(info["drive_path"]), owner, job)
            _stage_drive(verified, destination)
        if destination.is_symlink() or not destination.is_file():
            raise ConcatSourceError("Staged MP4 is not a regular file")
        digest = hashlib.sha256()
        total = 0
        with destination.open("rb") as inp:
            if inp.read(12)[4:8] != b"ftyp":
                raise ConcatSourceError("Cloud Render video is not an MP4")
            inp.seek(0)
            for chunk in iter(lambda: inp.read(4 * 1024 * 1024), b""):
                total += len(chunk)
                if total > expected_size:
                    raise ConcatSourceError("Cloud Render source grew during handoff")
                digest.update(chunk)
        if total != expected_size or digest.hexdigest().lower() != str(info["sha256"]).lower():
            raise ConcatSourceError("Cloud Render MP4 size or checksum mismatch")
        destination.chmod(0o600)
        return str(destination), str(info["name"]), total, digest.hexdigest()
    except BaseException:
        destination.unlink(missing_ok=True)
        raise

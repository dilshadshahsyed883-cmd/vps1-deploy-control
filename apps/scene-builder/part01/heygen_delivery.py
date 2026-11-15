"""Owner-scoped, checksum-verified VPS MP4 mirror for HeyGen worker outputs.

Unlike Concat's push receiver, HeyGen exposes a protected download API. Pulling
the completed MP4 is the appropriate transport; never reuse Concat's job IDs,
Drive token, or storage folders. GPU media is never deleted by this mirroring.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from fastapi.responses import FileResponse

MAX_MP4_BYTES = 2 * 1024 ** 3
CHUNK_BYTES = 1024 * 1024
JOB_ID = re.compile(r"^[0-9a-f]{32}$")


def verify_mp4(path: Path) -> dict:
    """Require decodable video and audio, not merely a plausible filename."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-count_frames", "-show_streams",
             "-show_format", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=180, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("VPS ffprobe is unavailable or timed out") from exc
    if result.returncode:
        raise ValueError("VPS ffprobe rejected the MP4")
    try:
        data = json.loads(result.stdout)
        tracks = data["streams"]
        video = next(s for s in tracks if s.get("codec_type") == "video")
        next(s for s in tracks if s.get("codec_type") == "audio")
        frames = int(video["nb_read_frames"])
        duration = float(data["format"]["duration"])
        width, height = int(video["width"]), int(video["height"])
        if min(frames, width, height) <= 0 or not 0 < duration < 86400:
            raise ValueError("Invalid frames, dimensions or duration")
    except (KeyError, TypeError, StopIteration, ValueError) as exc:
        raise ValueError("MP4 is missing valid video/audio tracks or duration") from exc
    return {"duration_seconds": round(duration, 3), "video_frames": frames,
            "width": width, "height": height}


class VerifiedMedia:
    """Mirror only completed owner-mapped jobs; retry safely after restarts."""

    def __init__(self, root: Path, connection, client_factory, validate=verify_mp4):
        self.root = Path(root) / "media"
        if self.root.is_symlink():
            raise RuntimeError("Unsafe HeyGen media directory")
        self.root.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.connection, self.client_factory, self.validate = connection, client_factory, validate
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="heygen-vps-mp4")
        self.lock = threading.RLock()
        self.inflight = set()
        self.next_attempt = {}
        with self.connection() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS heygen_media(
                owner TEXT NOT NULL, id TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',
                size INTEGER NOT NULL DEFAULT 0, sha256 TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(owner,id))""")

    def file(self, owner: str, job_id: str) -> Path:
        if not JOB_ID.fullmatch(str(job_id)) or not owner:
            raise ValueError("Invalid job ID or owner")
        # Two independent GPU accounts can generate the same worker-side job ID.
        # Never share a local cache namespace across owners.
        namespace = hashlib.sha256(owner.encode("utf-8")).hexdigest()
        folder = self.root / namespace
        if folder.is_symlink():
            raise ValueError("Unsafe owner media directory")
        folder.mkdir(mode=0o700, exist_ok=True)
        return folder / (job_id + ".mp4")

    def snapshot(self, owner: str, job_id: str) -> dict:
        path = self.file(owner, job_id)
        with self.connection() as con:
            row = con.execute("SELECT state,size,sha256,detail FROM heygen_media "
                              "WHERE owner=? AND id=?", (owner, job_id)).fetchone()
        if row and row["state"] == "ready" and path.is_file() and not path.is_symlink() and path.stat().st_size == row["size"]:
            return {"state": "ready", "size": row["size"], "sha256": row["sha256"]}
        return {"state": row["state"] if row and row["state"] != "ready" else "pending",
                "detail": row["detail"] if row and row["state"] == "failed" else ""}

    def schedule(self, owner: str, job_id: str, *, force: bool = False) -> dict:
        current = self.snapshot(owner, job_id)
        if current["state"] == "ready":
            return current
        key = (owner, job_id)
        with self.lock:
            if key in self.inflight or (not force and self.next_attempt.get(key, 0) > time.monotonic()):
                return current
            self.inflight.add(key)
            self.next_attempt[key] = time.monotonic() + 60
        self.pool.submit(self._mirror, owner, job_id)
        return {"state": "copying"}

    def _mirror(self, owner: str, job_id: str) -> None:
        key = (owner, job_id)
        path = self.file(owner, job_id)
        staging = path.with_suffix(".part")
        lockfile = path.with_suffix(".lock")
        try:
            with lockfile.open("a+b") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                # Another web process may have finished the same job already.
                if self.snapshot(owner, job_id)["state"] == "ready":
                    return
                with self.connection() as con:
                    found = con.execute("SELECT 1 FROM jobs WHERE owner=? AND id=?",
                                        (owner, job_id)).fetchone()
                if not found:
                    return
                # A stale incomplete part must never be mistaken for a valid MP4.
                if staging.is_symlink() or path.is_symlink():
                    raise ValueError("Unsafe destination")
                staging.unlink(missing_ok=True)
                total, sha = 0, hashlib.sha256()
                with self.client_factory(owner, timeout=300) as client:
                    with client.stream("GET", "api/v1/jobs/" + job_id + "/video") as response:
                        if response.status_code != 200 or "video/mp4" not in response.headers.get("content-type", ""):
                            raise ValueError("GPU video not ready for mirroring")
                        header = response.headers.get("content-length")
                        if header and (not header.isdigit() or int(header) > MAX_MP4_BYTES):
                            raise ValueError("GPU video exceeds storage limit")
                        with staging.open("xb") as output:
                            magic = bytearray()
                            for chunk in response.iter_bytes(CHUNK_BYTES):
                                total += len(chunk)
                                if total > MAX_MP4_BYTES:
                                    raise ValueError("GPU video exceeds storage limit")
                                if len(magic) < 12:
                                    magic.extend(chunk[:12 - len(magic)])
                                sha.update(chunk)
                                output.write(chunk)
                            output.flush()
                            os.fsync(output.fileno())
                        if len(magic) < 12 or magic[4:8] != b"ftyp" or total < 1024:
                            raise ValueError("GPU output has invalid MP4 header or is empty")
                        if header and int(header) != total:
                            raise ValueError("GPU output length mismatch")
                info = self.validate(staging)
                # Owner-mapped output is immutable once verified.
                if path.exists():
                    raise ValueError("Existing HeyGen output cannot be overwritten")
                os.replace(staging, path)
                with self.connection() as con:
                    con.execute("""INSERT INTO heygen_media(owner,id,state,size,sha256,detail,created_at)
                        VALUES(?,?,'ready',?,?,?,?)
                        ON CONFLICT(owner,id) DO UPDATE SET
                          state='ready',size=excluded.size,sha256=excluded.sha256,detail=excluded.detail""",
                        (owner, job_id, total, sha.hexdigest(), json.dumps(info),
                         datetime.now(timezone.utc).isoformat()))
        except Exception:
            # Keep the remote file for a later retry; do not leak URLs, tokens, paths or raw stderr.
            with self.connection() as con:
                con.execute("""INSERT INTO heygen_media(owner,id,state,detail)
                    VALUES(?,?,'failed','MP4 verification or transfer failed; retry available')
                    ON CONFLICT(owner,id) DO UPDATE SET
                        state='failed',detail=excluded.detail""", (owner, job_id))
        finally:
            staging.unlink(missing_ok=True)
            with self.lock:
                self.inflight.discard(key)

    def response(self, owner: str, job_id: str, download: bool = False):
        if self.snapshot(owner, job_id)["state"] != "ready":
            return None
        path = self.file(owner, job_id)
        return FileResponse(
            path, media_type="video/mp4",
            filename=f"heygen-{job_id}.mp4",
            content_disposition_type="attachment" if download else "inline",
            headers={"Cache-Control": "private, no-store",
                     "X-Content-Type-Options": "nosniff"})

    def delete(self, owner: str, job_id: str):
        # Caller must already have authorized owner and completed remote delete.
        path = self.file(owner, job_id)
        if path.is_symlink():
            raise ValueError("Unsafe MP4")
        path.unlink(missing_ok=True)
        with self.connection() as con:
            con.execute("DELETE FROM heygen_media WHERE owner=? AND id=?", (owner, job_id))

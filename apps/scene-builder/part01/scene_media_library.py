"""Scene Builder-only local media catalog (separate from the SML service).

Files and SQLite live under WEB_DATA_DIR/scene_media_library by default.
There is no Drive sync, no SML catalog mutation, and no cross-app exposure.
"""
from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import sqlite3
import tempfile
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ALLOWED = {
    ".jpg": "image", ".jpeg": "image", ".png": "image",
    ".webp": "image", ".gif": "image", ".bmp": "image",
    ".mp4": "video", ".mov": "video", ".m4v": "video",
    ".webm": "video", ".mkv": "video",
}
MEDIA_ID = re.compile(r"[0-9a-f]{32}\Z")
WORD = re.compile(r"[^\W_]+", re.UNICODE)
STOP = {"the", "and", "with", "scene", "clip", "video", "image", "from", "that", "this"}


class InvalidMedia(ValueError):
    pass


def _terms(value: object) -> set[str]:
    return {x for x in WORD.findall(str(value or "").casefold())
            if len(x) >= 3 and x not in STOP}


def _signature_ok(ext: str, data: bytes) -> bool:
    if ext in {".jpg", ".jpeg"}:
        return data.startswith(b"\xff\xd8\xff")
    if ext == ".png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if ext == ".webp":
        return data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    if ext == ".gif":
        return data.startswith((b"GIF87a", b"GIF89a"))
    if ext == ".bmp":
        return data.startswith(b"BM")
    if ext in {".mp4", ".m4v", ".mov"}:
        return len(data) >= 12 and data[4:8] == b"ftyp"
    if ext in {".webm", ".mkv"}:
        return data.startswith(b"\x1a\x45\xdf\xa3")
    return False


class SceneMediaLibrary:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.assets = self.root / "assets"
        self.database = self.root / "catalog.sqlite3"
        self.lock = threading.RLock()
        self.assets.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._db() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS media (
                id TEXT PRIMARY KEY, original_name TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('image','video')),
                extension TEXT NOT NULL, bytes INTEGER NOT NULL,
                sha256 TEXT NOT NULL, approved INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_scene_media_kind ON media(kind)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_scene_media_hash ON media(sha256,bytes)")

    @contextmanager
    def _db(self):
        conn = sqlite3.connect(str(self.database), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _row(self, row: sqlite3.Row) -> dict:
        entry = dict(row)
        entry["approved"] = bool(entry["approved"])
        entry["url"] = f"/api/scene-media-library/media/{entry['id']}"
        return entry

    def list(self, kind: str = "all", query: str = "", limit: int = 500) -> list[dict]:
        if kind not in {"all", "image", "video"}:
            raise InvalidMedia("Unsupported media category")
        query = str(query or "").strip().casefold()[:200]
        with self.lock, self._db() as conn:
            rows = conn.execute("SELECT * FROM media ORDER BY created_at DESC,id DESC").fetchall()
        entries = [self._row(row) for row in rows
                   if (kind == "all" or row["kind"] == kind)
                   and (not query or query in str(row["original_name"]).casefold())]
        return entries[:max(1, min(100000, limit))]

    def summary(self) -> dict:
        with self.lock, self._db() as conn:
            rows = conn.execute("SELECT kind,COUNT(*) AS total,COALESCE(SUM(bytes),0) AS size FROM media GROUP BY kind").fetchall()
        groups = {row["kind"]: {"count": row["total"], "bytes": row["size"]} for row in rows}
        images = groups.get("image", {"count": 0, "bytes": 0})
        videos = groups.get("video", {"count": 0, "bytes": 0})
        return {"images": images["count"], "videos": videos["count"],
                "approved": images["count"] + videos["count"],
                "bytes": images["bytes"] + videos["bytes"]}

    def file(self, media_id: str) -> tuple[Path, dict]:
        if not MEDIA_ID.fullmatch(str(media_id or "")):
            raise InvalidMedia("Invalid media ID")
        with self.lock, self._db() as conn:
            row = conn.execute("SELECT * FROM media WHERE id=?", (media_id,)).fetchone()
        if row is None:
            raise FileNotFoundError(media_id)
        info = self._row(row)
        path = self.assets / (media_id + info["extension"])
        if path.is_symlink() or not path.is_file():
            raise FileNotFoundError(media_id)
        return path, info

    async def import_upload(self, upload, max_bytes: int) -> dict:
        name = str(upload.filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()[:180]
        ext = Path(name).suffix.casefold()
        kind = ALLOWED.get(ext)
        if not name or not kind:
            await upload.close()
            raise InvalidMedia("Unsupported image/video filename or format")
        ident = uuid.uuid4().hex
        hasher = hashlib.sha256()
        total = 0
        header = b""
        fd, tmp = tempfile.mkstemp(prefix=".scene-upload-", dir=str(self.assets))
        try:
            with os.fdopen(fd, "wb") as stream:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise InvalidMedia("Media file exceeds upload limit")
                    if len(header) < 32:
                        header = (header + chunk)[:32]
                    hasher.update(chunk)
                    stream.write(chunk)
            if total < 12 or not _signature_ok(ext, header):
                raise InvalidMedia("File content does not match its image/video extension")
            with self.lock:
                with self._db() as conn:
                    existing = conn.execute("SELECT * FROM media WHERE sha256=? AND bytes=? LIMIT 1",
                                            (hasher.hexdigest(), total)).fetchone()
                    if existing:
                        return {**self._row(existing), "duplicate": True}
                    target = self.assets / (ident + ext)
                    os.replace(tmp, target)
                    try:
                        conn.execute("""INSERT INTO media(id, original_name, kind, extension, bytes, sha256, approved, created_at)
                            VALUES(?,?,?,?,?,?,1,?)""", (
                            ident, name, kind, ext, total, hasher.hexdigest(),
                            datetime.now(timezone.utc).isoformat()))
                    except Exception:
                        target.unlink(missing_ok=True)
                        raise
                return {**self._row(self._one(ident)), "duplicate": False}
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            await upload.close()

    def _one(self, media_id: str):
        with self._db() as conn:
            return conn.execute("SELECT * FROM media WHERE id=?", (media_id,)).fetchone()

    def delete(self, media_id: str) -> None:
        with self.lock:
            path, _ = self.file(media_id)
            path.unlink(missing_ok=True)
            with self._db() as conn:
                conn.execute("DELETE FROM media WHERE id=?", (media_id,))

    def clean(self) -> int:
        """Delete only tracked Scene-local assets; never traverse another library."""
        with self.lock:
            with self._db() as conn:
                rows = conn.execute("SELECT id,extension FROM media").fetchall()
                for row in rows:
                    path = self.assets / (row["id"] + row["extension"])
                    if path.is_symlink():
                        raise InvalidMedia("Unsafe library asset symlink")
                    path.unlink(missing_ok=True)
                conn.execute("DELETE FROM media")
            return len(rows)

    def report(self) -> str:
        items = self.list(limit=100000)
        counts = self.summary()
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines = ["SCENE BUILDER | MEDIA LIBRARY SUMMARY", "=" * 40,
                 f"Generated: {stamp}", "Access: Scene Builder only",
                 "Approval: Automatic after successful import", "",
                 f"Total images: {counts['images']}", f"Total videos: {counts['videos']}",
                 f"Approved: {counts['approved']}", f"Total size: {counts['bytes']:,} bytes"]
        for kind in ("image", "video"):
            selected = sorted((x for x in items if x["kind"] == kind),
                              key=lambda x: (x["original_name"].casefold(), x["id"]))
            lines.extend(("", kind.upper() + "S", "-" * 40))
            lines.extend(f"{idx:04d}. {x['original_name']} | {x['bytes']:,} bytes | Auto approved"
                         for idx, x in enumerate(selected, 1))
            if not selected:
                lines.append("(none)")
        return "\n".join(lines) + "\n"

    def ranked(self, scene: dict, used: set[str] | None = None, kind: str = "all") -> list[dict]:
        query = scene.get("query_override") or scene.get("keyword_used") or scene.get("original_query") or scene.get("narration")
        needle = _terms(query)
        if not needle:
            return []
        matches = []
        for row in self.list(kind=kind, limit=2000):
            if row["id"] in (used or set()):
                continue
            overlap = len(needle & _terms(Path(row["original_name"]).stem))
            if overlap:
                matches.append((overlap / len(needle), overlap, row["original_name"].casefold(), row))
        matches.sort(key=lambda x: (-x[0], -x[1], x[2]))
        return [x[3] for x in matches]

"""Resumable verified PC voice/picture imports for owner-scoped Short Profiles.

This module stores media in a persistent VPS library. It neither starts the
Short Maker renderer nor accesses any client PC folder without a browser upload.
Media are never exposed as verified until all requested files have been checked.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
from contextlib import contextmanager
from pathlib import Path

from fastapi import HTTPException, Request
from PIL import Image

AUDIO = frozenset((".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"))
PICTURES = frozenset((".jpg", ".jpeg", ".png", ".webp", ".jfif", ".heic", ".heif"))
CHUNK_BYTES = 8 * 1024 * 1024
DISK_RESERVE_BYTES = 1024 * 1024 * 1024


def register_short_profile_media_uploads(app, data_root, short_identity):
    data_root = Path(data_root)
    sessions = data_root / "short_profile_imports"
    library = data_root / "short_profile_library"
    for directory in (sessions, library):
        if directory.is_symlink():
            raise RuntimeError("Unsafe Short Profile media root")
        directory.mkdir(parents=True, exist_ok=True)

    def identity(request):
        return short_identity(request)[0].strip().casefold()

    def sid_dir(sid):
        if not re.fullmatch(r"[0-9a-f]{32}", sid):
            raise HTTPException(404, detail="Source import not found")
        folder = sessions / sid
        if folder.is_symlink():
            raise HTTPException(409, detail="Unsafe source import directory")
        return folder

    @contextmanager
    def locked(path):
        if path.is_symlink():
            raise HTTPException(409, detail="Unsafe source import lock")
        with path.open("a+b") as fd:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)

    def load(sid, owner):
        path = sid_dir(sid) / "manifest.json"
        if path.is_symlink() or not path.is_file():
            raise HTTPException(404, detail="Source import not found")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise HTTPException(409, detail="Source import manifest corrupted")
        if not isinstance(value, dict) or value.get("owner") != owner:
            raise HTTPException(404, detail="Source import not found")
        return value

    def save(sid, value):
        path = sid_dir(sid) / "manifest.json"
        temporary = path.with_suffix(".new")
        if temporary.is_symlink() or path.is_symlink():
            raise HTTPException(409, detail="Unsafe source import manifest")
        with temporary.open("w", encoding="utf-8") as fd:
            json.dump(value, fd, sort_keys=True)
            fd.flush()
            os.fsync(fd.fileno())
        os.replace(temporary, path)

    def staging(sid, index):
        return sid_dir(sid) / ("media_%010d.part" % index)

    def user_library(owner):
        # The owner never becomes an arbitrary filesystem path component.
        digest = hashlib.sha256(owner.encode("utf-8")).hexdigest()[:32]
        root = library / digest
        if root.is_symlink():
            raise HTTPException(409, detail="Unsafe owner media directory")
        root.mkdir(mode=0o700, exist_ok=True)
        for name in ("audio", "picture"):
            directory = root / name
            if directory.is_symlink():
                raise HTTPException(409, detail="Unsafe media library directory")
            directory.mkdir(mode=0o700, exist_ok=True)
        return root

    def file_digest(path):
        digest = hashlib.sha256()
        with path.open("rb") as fd:
            for block in iter(lambda: fd.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def verify_media(path, kind):
        if path.is_symlink() or not path.is_file() or path.stat().st_size < 128:
            return False
        if kind == "picture":
            try:
                with Image.open(path) as image:
                    image.verify()
                return True
            except (OSError, ValueError, SyntaxError):
                return False
        try:
            result = subprocess.run(
                [shutil.which("ffprobe") or "/usr/bin/ffprobe", "-v", "error",
                 "-select_streams", "a:0", "-show_entries",
                 "stream=codec_type", "-of",
                 "default=nokey=1:noprint_wrappers=1", str(path)],
                capture_output=True, text=True, timeout=30, check=False,
            )
            return result.returncode == 0 and "audio" in result.stdout.splitlines()
        except (OSError, subprocess.TimeoutExpired):
            return False

    def target_for(owner, asset):
        root = user_library(owner)
        kind = asset["kind"]
        ext = asset["ext"]
        digest = asset["sha256"]
        if kind not in ("audio", "picture") or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise HTTPException(409, detail="Unsafe source media receipt")
        if ext not in (AUDIO if kind == "audio" else PICTURES):
            raise HTTPException(409, detail="Invalid source media extension")
        return root / kind / (digest + ext)

    def publish(source, destination, digest):
        if destination.is_symlink():
            raise HTTPException(409, detail="Unsafe destination")
        if destination.exists():
            if destination.is_file() and file_digest(destination) == digest:
                return
            raise HTTPException(409, detail="Conflicting source-media digest")
        scratch = destination.parent / ("." + uuid.uuid4().hex + ".part")
        try:
            shutil.copyfile(source, scratch)
            if file_digest(scratch) != digest:
                raise HTTPException(409, detail="Media copy verification failed")
            try:
                os.link(scratch, destination)  # atomic publish, no overwrite
            except FileExistsError:
                if destination.is_symlink() or file_digest(destination) != digest:
                    raise HTTPException(409, detail="Media publish collision")
        finally:
            scratch.unlink(missing_ok=True)

    def snapshot(sid, value):
        items = []
        total = transferred = 0
        for index, file in enumerate(value["files"]):
            path = staging(sid, index)
            if path.is_symlink():
                raise HTTPException(409, detail="Unsafe source staging file")
            received = file["size"] if value["status"] == "completed" else (
                path.stat().st_size if path.is_file() else 0
            )
            if received > file["size"]:
                raise HTTPException(409, detail="Invalid source staging offset")
            items.append({"index": index, "name": file["name"],
                          "size": file["size"], "uploaded": received,
                          "kind": file["kind"]})
            total += file["size"]
            transferred += received
        return {"session_id": sid, "status": value["status"],
                "files": items, "total_bytes": total,
                "uploaded_bytes": transferred,
                "verified_audio_count": sum(
                    a["kind"] == "audio" for a in value.get("verified_assets", []))
                if value["status"] == "completed" else 0,
                "verified_picture_count": sum(
                    a["kind"] == "picture" for a in value.get("verified_assets", []))
                if value["status"] == "completed" else 0}

    def verified_import(owner, sid):
        """Fail-closed receipt accessor wired into Short Profile saving."""
        owner = str(owner).strip().casefold()
        try:
            manifest = load(sid, owner)
            if manifest["status"] != "completed":
                return None
            assets = manifest["verified_assets"]
            if len(assets) != len(manifest["files"]):
                return None
            counts = {"audio_count": 0, "image_count": 0}
            unique_media = set()
            for asset in assets:
                path = target_for(owner, asset)
                if (path.is_symlink() or not path.is_file() or
                        path.stat().st_size != asset["size"]):
                    return None
                media_key = (asset["kind"], asset["sha256"])
                if media_key not in unique_media:
                    counts["audio_count" if asset["kind"] == "audio" else "image_count"] += 1
                    unique_media.add(media_key)
            return counts
        except (HTTPException, ValueError, OSError, KeyError, TypeError):
            return None

    @app.post("/api/short-profile-media/sessions")
    async def begin_import(request: Request):
        owner = identity(request)
        try:
            data = await request.json()
        except ValueError:
            raise HTTPException(400, detail="Invalid media import request")
        if not isinstance(data, dict) or not isinstance(data.get("files"), list):
            raise HTTPException(400, detail="Missing source files")
        clean = []
        for item in data["files"]:
            if not isinstance(item, dict):
                raise HTTPException(400, detail="Invalid source media entry")
            name = str(item.get("name") or "")
            if (not name or len(name) > 180 or name in (".", "..") or
                    "/" in name or "\\" in name or
                    any(ord(c) < 32 or ord(c) == 127 for c in name)):
                raise HTTPException(400, detail="Unsafe source filename")
            extension = Path(name).suffix.lower()
            if extension in AUDIO:
                kind = "audio"
            elif extension in PICTURES:
                kind = "picture"
            else:
                raise HTTPException(400, detail="Unsupported source voice/picture format")
            size = item.get("size")
            if type(size) is not int or size < 128:
                raise HTTPException(400, detail="Source media is too small")
            clean.append({"name": name, "size": size, "kind": kind,
                          "ext": extension})
        if not any(f["kind"] == "audio" for f in clean) or not any(
                f["kind"] == "picture" for f in clean):
            raise HTTPException(400, detail="Import must contain voice audio and pictures")
        total = sum(f["size"] for f in clean)
        if total > max(0, shutil.disk_usage(sessions).free - DISK_RESERVE_BYTES):
            raise HTTPException(507, detail="Insufficient space for source import")
        request_id = str(data.get("client_request_id") or "")
        if request_id and not re.fullmatch(r"[0-9a-f-]{36}", request_id):
            raise HTTPException(400, detail="Invalid import idempotency ID")
        sid = (hashlib.sha256((owner + ":" + request_id).encode()).hexdigest()[:32]
               if request_id else uuid.uuid4().hex)
        with locked(sessions / ".create.lock"):
            directory = sid_dir(sid)
            if directory.exists():
                manifest = load(sid, owner)
                if manifest["files"] != clean:
                    raise HTTPException(409, detail="Import key reused with different files")
                return snapshot(sid, manifest)
            directory.mkdir(mode=0o700)
            manifest = {"owner": owner, "status": "uploading",
                        "files": clean, "verified_assets": []}
            save(sid, manifest)
            return snapshot(sid, manifest)

    @app.get("/api/short-profile-media/sessions/{sid}")
    def import_progress(sid: str, request: Request):
        return snapshot(sid, load(sid, identity(request)))

    @app.put("/api/short-profile-media/sessions/{sid}/files/{index}")
    async def import_chunk(sid: str, index: int, request: Request):
        owner = identity(request)
        if index < 0:
            raise HTTPException(404, detail="Media index not found")
        try:
            offset = int(request.headers.get("x-upload-offset", "-1"))
        except ValueError:
            raise HTTPException(400, detail="Invalid media chunk offset")
        body = bytearray()
        async for piece in request.stream():
            body.extend(piece)
            if len(body) > CHUNK_BYTES:
                raise HTTPException(413, detail="Source chunk exceeds 8 MiB")
        if not body:
            raise HTTPException(400, detail="Empty media chunk")
        load(sid, owner)  # authorize before touching another session directory
        with locked(sid_dir(sid) / ".lock"):
            manifest = load(sid, owner)
            if manifest["status"] != "uploading" or index >= len(manifest["files"]):
                raise HTTPException(409, detail="Source media cannot accept chunk")
            target = staging(sid, index)
            if target.is_symlink():
                raise HTTPException(409, detail="Unsafe staging file")
            existing = target.stat().st_size if target.is_file() else 0
            size = manifest["files"][index]["size"]
            if offset != existing or offset + len(body) > size:
                raise HTTPException(409, detail="Source media offset mismatch")
            with target.open("ab") as fd:
                fd.write(body)
                fd.flush()
                os.fsync(fd.fileno())
            return {"index": index, "uploaded": existing + len(body),
                    "complete": existing + len(body) == size}

    @app.post("/api/short-profile-media/sessions/{sid}/finalize")
    def finish_import(sid: str, request: Request):
        owner = identity(request)
        load(sid, owner)  # authorize before touching another session directory
        with locked(sid_dir(sid) / ".lock"):
            manifest = load(sid, owner)
            if manifest["status"] == "completed":
                return snapshot(sid, manifest)
            if manifest["status"] not in ("uploading", "publishing"):
                raise HTTPException(409, detail="Source import is not finalizable")
            if manifest["status"] == "uploading":
                assets = []
                for index, entry in enumerate(manifest["files"]):
                    source = staging(sid, index)
                    if (source.is_symlink() or not source.is_file() or
                            source.stat().st_size != entry["size"]):
                        raise HTTPException(409, detail="Source media upload incomplete")
                    if not verify_media(source, entry["kind"]):
                        raise HTTPException(422, detail="Invalid media: " + entry["name"])
                    assets.append({"kind": entry["kind"], "ext": entry["ext"],
                                   "sha256": file_digest(source), "size": entry["size"]})
                manifest["verified_assets"] = assets
                manifest["status"] = "publishing"
                save(sid, manifest)
            for index, asset in enumerate(manifest["verified_assets"]):
                publish(staging(sid, index), target_for(owner, asset), asset["sha256"])
            manifest["status"] = "completed"
            save(sid, manifest)
            for index in range(len(manifest["files"])):
                staging(sid, index).unlink(missing_ok=True)
            return snapshot(sid, manifest)

    @app.post("/api/short-profile-media/sessions/{sid}/pause")
    def pause_import(sid: str, request: Request):
        owner = identity(request)
        load(sid, owner)  # authorize before touching another session directory
        with locked(sid_dir(sid) / ".lock"):
            manifest = load(sid, owner)
            if manifest["status"] == "uploading":
                manifest["status"] = "paused"
                save(sid, manifest)
            return snapshot(sid, manifest)

    @app.post("/api/short-profile-media/sessions/{sid}/resume")
    def resume_import(sid: str, request: Request):
        owner = identity(request)
        load(sid, owner)  # authorize before touching another session directory
        with locked(sid_dir(sid) / ".lock"):
            manifest = load(sid, owner)
            if manifest["status"] == "paused":
                manifest["status"] = "uploading"
                save(sid, manifest)
            return snapshot(sid, manifest)

    @app.delete("/api/short-profile-media/sessions/{sid}")
    def cancel_import(sid: str, request: Request):
        owner = identity(request)
        load(sid, owner)  # authorize before touching another session directory
        with locked(sid_dir(sid) / ".lock"):
            manifest = load(sid, owner)
            if manifest["status"] in ("publishing", "completed"):
                raise HTTPException(409, detail="Published media cannot be cancelled")
            manifest["status"] = "cancelled"
            save(sid, manifest)
            for index in range(len(manifest["files"])):
                staging(sid, index).unlink(missing_ok=True)
            return {"ok": True, "status": "cancelled"}

    return verified_import

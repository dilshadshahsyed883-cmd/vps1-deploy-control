"""Owner-scoped, refresh-recoverable ready-made MP4 transfer and verified handoff.

A bounded-chunk HTTP transfer: no hard cap on the number of selected files.
Feature remains gated in the Short Maker UI until the full approved workflow,
including cross-service folder deletion checks, passes its release tests.
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

CHUNK_BYTES = 8 * 1024 * 1024
DISK_RESERVE_BYTES = 5_000_000_000
MP4_SUFFIXES = {".mp4"}


def register_ready_video_uploads(app, *, data_root, short_identity,
                                 short_folder_owned, short_vps_folder,
                                 reserve_short_output_names):
    root = Path(data_root) / "ready_video_uploads"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink():
        raise RuntimeError("Unsafe ready-made upload root")

    def session_dir(session_id):
        if not re.fullmatch(r"[0-9a-f]{32}", session_id or ""):
            raise HTTPException(404, detail="Ready-made upload session not found")
        path = root / session_id
        if path.is_symlink():
            raise HTTPException(409, detail="Unsafe upload session path")
        return path

    @contextmanager
    def lock(session_id):
        directory = session_dir(session_id)
        if not directory.is_dir():
            raise HTTPException(404, detail="Upload session not found")
        lockfile = directory / ".lock"
        if lockfile.is_symlink():
            raise HTTPException(409, detail="Unsafe upload lock")
        with lockfile.open("a+b") as descriptor:
            fcntl.flock(descriptor.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(descriptor.fileno(), fcntl.LOCK_UN)

    def read(session_id, request):
        owner, _ = short_identity(request)
        path = session_dir(session_id) / "manifest.json"
        if path.is_symlink() or not path.is_file():
            raise HTTPException(404, detail="Upload session not found")
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise HTTPException(409, detail="Unreadable upload manifest")
        if manifest.get("owner") != owner.casefold():
            raise HTTPException(403, detail="Upload session belongs to another user")
        return manifest

    def save(session_id, manifest):
        path = session_dir(session_id) / "manifest.json"
        temp = path.with_suffix(".new")
        if temp.is_symlink() or path.is_symlink():
            raise HTTPException(409, detail="Unsafe manifest")
        with temp.open("w", encoding="utf-8") as output:
            json.dump(manifest, output, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)

    def staged(session_id, index):
        return session_dir(session_id) / ("file_%010d.part" % index)

    def snapshot(session_id, manifest):
        files = []
        total = uploaded = 0
        for index, record in enumerate(manifest["files"]):
            path = staged(session_id, index)
            if path.is_symlink():
                raise HTTPException(409, detail="Unsafe staged file")
            size = int(record["size"])
            received = size if manifest["status"] == "completed" else (
                path.stat().st_size if path.is_file() else 0)
            if received > size:
                raise HTTPException(409, detail="Staged file exceeds manifest size")
            uploaded += received
            total += size
            files.append(dict(index=index, name=record["name"], size=size,
                              uploaded=received, verified_name=record.get("verified_name")))
        return {"session_id": session_id, "status": manifest["status"],
                "folder": manifest["folder"], "files": files,
                "uploaded_bytes": uploaded, "total_bytes": total,
                "progress": round(100 * uploaded / total, 2) if total else 100}

    @contextmanager
    def create_lock():
        file = root / ".create.lock"
        if file.is_symlink():
            raise HTTPException(409, detail="Unsafe upload-create lock")
        with file.open("a+b") as descriptor:
            fcntl.flock(descriptor.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(descriptor.fileno(), fcntl.LOCK_UN)

    def verify_mp4(path):
        if path.is_symlink() or not path.is_file() or path.stat().st_size < 1024:
            return False
        try:
            result = subprocess.run(
                [shutil.which("ffprobe") or "/usr/bin/ffprobe", "-v", "error",
                 "-select_streams", "v:0", "-show_entries", "stream=codec_type",
                 "-of", "default=nokey=1:noprint_wrappers=1", str(path)],
                capture_output=True, text=True, timeout=30, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0 and "video" in result.stdout.splitlines()

    def file_digest(path):
        digest = hashlib.sha256()
        with path.open("rb") as infile:
            for chunk in iter(lambda: infile.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def publish(source, destination, expected_hash):
        if destination.is_symlink():
            raise HTTPException(409, detail="Conflicting output symlink")
        if destination.exists():
            if destination.is_file() and file_digest(destination) == expected_hash:
                return
            raise HTTPException(409, detail="Output name is already in use")
        scratch = destination.parent / ("." + uuid.uuid4().hex + ".partial")
        if scratch.is_symlink():
            raise HTTPException(409, detail="Unsafe publish staging path")
        try:
            shutil.copyfile(source, scratch)
            if file_digest(scratch) != expected_hash:
                raise HTTPException(409, detail="Verified MP4 copy hash mismatch")
            os.link(scratch, destination)  # atomic no-clobber, within output filesystem
        except FileExistsError:
            if not destination.is_file() or file_digest(destination) != expected_hash:
                raise HTTPException(409, detail="Output already published by another job")
        finally:
            scratch.unlink(missing_ok=True)

    @app.post("/api/ready-videos/sessions")
    async def create_ready_video_upload(request: Request):
        owner, _ = short_identity(request)
        try:
            payload = await request.json()
        except ValueError:
            raise HTTPException(400, detail="Invalid upload request")
        if not isinstance(payload, dict) or not isinstance(payload.get("files"), list):
            raise HTTPException(400, detail="Missing files")
        folder = str(payload.get("folder") or "")
        short_folder_owned(folder, request)
        files = payload["files"]
        if not files:
            raise HTTPException(400, detail="Select at least one MP4")
        clean = []
        for item in files:
            if not isinstance(item, dict):
                raise HTTPException(400, detail="Invalid upload file")
            name = str(item.get("name") or "")
            if len(name) > 180 or not re.fullmatch(r"[^/\\\x00-\x1f]+\.mp4", name, re.I):
                raise HTTPException(400, detail="Only direct MP4 video files are supported")
            try:
                size = int(item["size"])
            except (KeyError, ValueError, TypeError):
                raise HTTPException(400, detail="Invalid MP4 size")
            if size < 1024:
                raise HTTPException(400, detail="MP4 is too small to be valid")
            clean.append({"name": name, "size": size})
        total = sum(item["size"] for item in clean)
        if total > max(0, shutil.disk_usage(root).free - DISK_RESERVE_BYTES):
            raise HTTPException(507, detail="Insufficient VPS disk space; source files retained")
        key = str(payload.get("client_request_id") or "")
        if key and not re.fullmatch(r"[0-9a-f-]{36}", key):
            raise HTTPException(400, detail="Invalid idempotency request ID")
        session_id = hashlib.sha256((owner.casefold() + ":" + key).encode()).hexdigest()[:32] if key else uuid.uuid4().hex
        with create_lock():
            directory = session_dir(session_id)
            if directory.exists():
                manifest = read(session_id, request)
                if (manifest["owner"] != owner.casefold() or manifest["folder"] != folder
                        or [(v["name"],v["size"]) for v in manifest["files"]]
                        != [(v["name"],v["size"]) for v in clean]):
                    raise HTTPException(409, detail="Idempotency key already used differently")
                return snapshot(session_id, manifest)
            directory.mkdir(mode=0o700)
            manifest = {"owner": owner.casefold(), "folder": folder, "files": clean,
                        "status": "uploading", "created_at": uuid.uuid4().hex}
            save(session_id, manifest)
            return snapshot(session_id, manifest)

    @app.get("/api/ready-videos/sessions/{session_id}")
    def read_ready_video_upload(session_id: str, request: Request):
        return snapshot(session_id, read(session_id, request))

    @app.put("/api/ready-videos/sessions/{session_id}/files/{index}")
    async def upload_ready_chunk(session_id: str, index: int, request: Request):
        short_identity(request)
        if index < 0:
            raise HTTPException(404, detail="File index not found")
        try:
            offset = int(request.headers.get("x-upload-offset", "-1"))
        except ValueError:
            raise HTTPException(400, detail="Invalid upload offset")
        body = bytearray()
        async for piece in request.stream():
            body.extend(piece)
            if len(body) > CHUNK_BYTES:
                raise HTTPException(413, detail="Chunk exceeds 8 MiB")
        if not body:
            raise HTTPException(400, detail="Empty upload chunk")
        with lock(session_id):
            manifest = read(session_id, request)
            if manifest["status"] != "uploading":
                raise HTTPException(409, detail="Upload is not accepting chunks")
            if index >= len(manifest["files"]):
                raise HTTPException(404, detail="File not found")
            target = staged(session_id, index)
            if target.is_symlink():
                raise HTTPException(409, detail="Unsafe upload file")
            size = manifest["files"][index]["size"]
            written = target.stat().st_size if target.is_file() else 0
            if offset != written or offset + len(body) > size:
                raise HTTPException(409, detail="Offset mismatch; refresh session")
            with target.open("ab") as outfile:
                outfile.write(body)
                outfile.flush()
                os.fsync(outfile.fileno())
            return {"index": index, "uploaded": offset + len(body),
                    "complete": offset + len(body) == size}

    @app.post("/api/ready-videos/sessions/{session_id}/finalize")
    def finalize_ready_video_upload(session_id: str, request: Request):
        with lock(session_id):
            manifest = read(session_id, request)
            if manifest["status"] == "completed":
                return snapshot(session_id, manifest)
            if manifest["status"] not in ("uploading", "publishing"):
                raise HTTPException(409, detail="Upload is not finalizable")
            folder = short_folder_owned(manifest["folder"], request)
            for index, record in enumerate(manifest["files"]):
                source = staged(session_id, index)
                if source.is_symlink() or not source.is_file() or source.stat().st_size != record["size"]:
                    raise HTTPException(409, detail="Upload incomplete; retry missing chunks")
                if not verify_mp4(source):
                    raise HTTPException(422, detail="Uploaded file is not a playable MP4: " + record["name"])
            if manifest["status"] == "uploading":
                names = []
                # Existing sequence allocator supports batches of 999; larger
                # user batches use consecutive reservations, with no file-count cap.
                for start in range(0, len(manifest["files"]), 999):
                    names.extend(reserve_short_output_names(
                        manifest["folder"], min(999, len(manifest["files"]) - start)))
                for index, (record, name) in enumerate(zip(manifest["files"], names)):
                    record["verified_name"] = name
                    record["sha256"] = file_digest(staged(session_id, index))
                manifest["status"] = "publishing"
                save(session_id, manifest)
            for index, record in enumerate(manifest["files"]):
                # Continue idempotently after an interrupted multi-video publish.
                destination = folder / record["verified_name"]
                publish(staged(session_id, index), destination, record["sha256"])
            manifest["status"] = "completed"
            save(session_id, manifest)
            # Final MP4s have independent verified copies; avoid retaining an
            # identical redundant staging copy after durable publish receipt.
            for index in range(len(manifest["files"])):
                staged(session_id, index).unlink(missing_ok=True)
            return snapshot(session_id, manifest)

    @app.post("/api/ready-videos/sessions/{session_id}/pause")
    def pause_ready_upload(session_id: str, request: Request):
        with lock(session_id):
            manifest = read(session_id, request)
            if manifest["status"] == "uploading":
                manifest["status"] = "paused"
                save(session_id, manifest)
            return snapshot(session_id, manifest)

    @app.post("/api/ready-videos/sessions/{session_id}/resume")
    def resume_ready_upload(session_id: str, request: Request):
        with lock(session_id):
            manifest = read(session_id, request)
            if manifest["status"] == "paused":
                manifest["status"] = "uploading"
                save(session_id, manifest)
            return snapshot(session_id, manifest)

    @app.delete("/api/ready-videos/sessions/{session_id}")
    def cancel_ready_upload(session_id: str, request: Request):
        with lock(session_id):
            manifest = read(session_id, request)
            if manifest["status"] in ("completed", "publishing"):
                raise HTTPException(409, detail="Published videos cannot be discarded as incomplete uploads")
            manifest["status"] = "cancelled"
            save(session_id, manifest)
        directory = session_dir(session_id)
        for index in range(len(manifest["files"])):
            staged(session_id, index).unlink(missing_ok=True)
        return {"ok": True, "status": "cancelled", "session_id": session_id}

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import uuid
from typing import Any, Iterator

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from .auth import Principal, get_current_principal
from .config import app_root


router = APIRouter(prefix="/fg-upload", tags=["acs-fg-pc-upload"])

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_SESSION_FILES = 20000
DATA_ROOT = app_root() / "data" / "web_flow"
SESSION_ROOT = DATA_ROOT / "fg_upload_sessions"
DEFAULT_FG_ROOT = app_root() / "data" / "fg_libraries"
FG_CONFIG = app_root() / "data" / "config" / "web_flow_fg_libraries.json"
AUDIT_FILE = app_root() / "logs" / "web_flow_fg_upload_audit.jsonl"


class FgPcUploadSessionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    total_files: int = Field(ge=1, le=MAX_SESSION_FILES)
    resume_session_id: str = Field(default="", max_length=80)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_admin(principal: Principal) -> None:
    if not principal.is_admin:
        raise HTTPException(403, "Administrator access required to manage shared FG libraries")


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip()).strip("._-")
    return slug[:80] or "fg_library"


def _normalize_relpath(value: str) -> str:
    text = str(value or "").strip().replace("\\", "/").lstrip("/")
    path = PurePosixPath(text)
    parts = [part for part in path.parts if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise HTTPException(400, "Invalid folder-relative image path")
    normalized = PurePosixPath(*parts)
    if normalized.suffix.lower() not in IMAGE_EXTENSIONS:
        raise HTTPException(400, "Unsupported FG image type")
    return normalized.as_posix()


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _audit(principal: Principal, action: str, payload: dict[str, Any]) -> None:
    AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "time": now_iso(),
        "user": principal.username,
        "action": action,
        **payload,
    }
    with AUDIT_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def _session_dir(session_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", str(session_id or "")):
        raise HTTPException(400, "Invalid FG upload session")
    return SESSION_ROOT / session_id


def _session_file(session_id: str) -> Path:
    return _session_dir(session_id) / "session.json"


@contextmanager
def _session_lock(session_id: str) -> Iterator[None]:
    root = _session_dir(session_id)
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".lock"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _load_session(session_id: str, principal: Principal) -> dict[str, Any]:
    path = _session_file(session_id)
    data = _load_json(path, None)
    if not isinstance(data, dict):
        raise HTTPException(404, "FG upload session not found")
    if str(data.get("owner") or "") != principal.username:
        raise HTTPException(403, "FG upload session access denied")
    return data


def _session_payload(data: dict[str, Any]) -> dict[str, Any]:
    files = data.get("files") if isinstance(data.get("files"), dict) else {}
    uploaded = len(files)
    unique_stored = sum(1 for item in files.values() if isinstance(item, dict) and bool(item.get("stored", True)))
    return {
        "ok": True,
        "session_id": data.get("id"),
        "name": data.get("name"),
        "status": data.get("status"),
        "total_files": int(data.get("total_files") or 0),
        "uploaded_count": uploaded,
        "unique_stored_count": unique_stored,
        "duplicate_count": max(0, uploaded - unique_stored),
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
        "library_id": data.get("library_id") or "",
        "library_path": data.get("library_path") or "",
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _registry_append(item: dict[str, Any]) -> None:
    FG_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    lock_path = FG_CONFIG.with_suffix(FG_CONFIG.suffix + ".lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            raw = _load_json(FG_CONFIG, [])
            registry = [dict(x) for x in raw if isinstance(x, dict)] if isinstance(raw, list) else []
            resolved = Path(str(item["path"])).resolve()
            for existing in registry:
                existing_path = str(existing.get("path") or "")
                if existing_path and Path(existing_path).resolve() == resolved:
                    raise HTTPException(409, "Uploaded FG folder is already registered")
            registry.append(item)
            _atomic_json(FG_CONFIG, registry)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


@router.post("/session")
def create_fg_upload_session(
    payload: FgPcUploadSessionCreate,
    principal: Principal = Depends(get_current_principal),
) -> dict[str, Any]:
    _require_admin(principal)
    SESSION_ROOT.mkdir(parents=True, exist_ok=True)
    DEFAULT_FG_ROOT.mkdir(parents=True, exist_ok=True)

    resume_id = str(payload.resume_session_id or "").strip()
    if resume_id:
        try:
            with _session_lock(resume_id):
                existing = _load_session(resume_id, principal)
                if str(existing.get("status")) == "uploading" and str(existing.get("name")) == payload.name.strip():
                    return _session_payload(existing)
        except HTTPException:
            pass

    session_id = uuid.uuid4().hex
    root = _session_dir(session_id)
    (root / "files").mkdir(parents=True, exist_ok=False)
    data = {
        "id": session_id,
        "owner": principal.username,
        "name": payload.name.strip(),
        "total_files": int(payload.total_files),
        "status": "uploading",
        "files": {},
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    _atomic_json(_session_file(session_id), data)
    _audit(principal, "fg_pc_upload_session_created", {"session_id": session_id, "name": data["name"], "total_files": data["total_files"]})
    return _session_payload(data)


@router.get("/session/{session_id}")
def fg_upload_session_status(
    session_id: str,
    principal: Principal = Depends(get_current_principal),
) -> dict[str, Any]:
    _require_admin(principal)
    with _session_lock(session_id):
        data = _load_session(session_id, principal)
        return _session_payload(data)


@router.post("/session/{session_id}/file")
def upload_fg_file(
    session_id: str,
    relative_path: str = Form(...),
    file: UploadFile = File(...),
    principal: Principal = Depends(get_current_principal),
) -> dict[str, Any]:
    _require_admin(principal)
    rel = _normalize_relpath(relative_path)
    root = _session_dir(session_id)
    tmp = root / f".{uuid.uuid4().hex}.part"
    size = 0
    digest = hashlib.sha256()

    try:
        with tmp.open("wb") as handle:
            while True:
                block = file.file.read(1024 * 1024)
                if not block:
                    break
                size += len(block)
                if size > MAX_FILE_BYTES:
                    raise HTTPException(413, "FG image exceeds 100 MB limit")
                digest.update(block)
                handle.write(block)
        sha = digest.hexdigest()

        with _session_lock(session_id):
            data = _load_session(session_id, principal)
            if str(data.get("status")) != "uploading":
                raise HTTPException(409, "FG upload session is not accepting files")
            files = data.setdefault("files", {})
            if len(files) >= int(data.get("total_files") or 0) and rel not in files:
                raise HTTPException(409, "FG upload session already received its expected files")

            existing = files.get(rel)
            if isinstance(existing, dict):
                if str(existing.get("sha256")) == sha and int(existing.get("size") or -1) == size:
                    return {"ok": True, "relative_path": rel, "size": size, "sha256": sha, "duplicate": True, "already_received": True, **_session_payload(data)}
                raise HTTPException(409, f"A different file was already uploaded for {rel}")

            duplicate_of = ""
            for other_rel, other in files.items():
                if isinstance(other, dict) and str(other.get("sha256")) == sha:
                    duplicate_of = str(other_rel)
                    break

            stored = not bool(duplicate_of)
            if stored:
                target = (root / "files" / rel).resolve()
                files_root = (root / "files").resolve()
                try:
                    target.relative_to(files_root)
                except ValueError as exc:
                    raise HTTPException(400, "FG image path escaped upload root") from exc
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(tmp, target)
            else:
                tmp.unlink(missing_ok=True)

            files[rel] = {
                "size": size,
                "sha256": sha,
                "stored": stored,
                "duplicate_of": duplicate_of,
                "uploaded_at": now_iso(),
            }
            data["updated_at"] = now_iso()
            _atomic_json(_session_file(session_id), data)
            return {
                "ok": True,
                "relative_path": rel,
                "size": size,
                "sha256": sha,
                "duplicate": bool(duplicate_of),
                "duplicate_of": duplicate_of,
                **_session_payload(data),
            }
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


@router.post("/session/{session_id}/finalize")
def finalize_fg_upload_session(
    session_id: str,
    principal: Principal = Depends(get_current_principal),
) -> dict[str, Any]:
    _require_admin(principal)
    with _session_lock(session_id):
        data = _load_session(session_id, principal)
        if str(data.get("status")) == "completed":
            return _session_payload(data)
        if str(data.get("status")) != "uploading":
            raise HTTPException(409, "FG upload session cannot be finalized")
        files = data.get("files") if isinstance(data.get("files"), dict) else {}
        expected = int(data.get("total_files") or 0)
        if len(files) != expected:
            raise HTTPException(409, f"FG folder is incomplete: received {len(files)} of {expected} selected image(s)")
        unique_count = sum(1 for item in files.values() if isinstance(item, dict) and bool(item.get("stored", True)))
        if unique_count < 1:
            raise HTTPException(409, "FG folder contains no unique usable images")

        DEFAULT_FG_ROOT.mkdir(parents=True, exist_ok=True)
        base = _safe_slug(str(data.get("name") or "FG Library"))
        final = DEFAULT_FG_ROOT / base
        if final.exists():
            final = DEFAULT_FG_ROOT / f"{base}_{session_id[:8]}"
        files_dir = _session_dir(session_id) / "files"
        if not files_dir.is_dir():
            raise HTTPException(409, "FG staging folder is missing")
        os.replace(files_dir, final)
        (final / ".cleanup_protected").write_text("Protected reusable ACS FG library. Do not remove during project cleanup.\n", encoding="utf-8")
        library_id = "pcfg_" + uuid.uuid4().hex
        metadata = {
            "id": library_id,
            "name": str(data.get("name") or base),
            "path": str(final.resolve()),
            "protected": True,
            "source": "pc_folder_upload",
            "created_by": principal.username,
            "created_at": now_iso(),
        }
        (final / ".acs_fg_library.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
        try:
            _registry_append(metadata)
        except Exception:
            # Put files back into the session if registration fails so retry is safe.
            os.replace(final, files_dir)
            raise

        data["status"] = "completed"
        data["library_id"] = library_id
        data["library_path"] = str(final.resolve())
        data["completed_at"] = now_iso()
        data["updated_at"] = now_iso()
        _atomic_json(_session_file(session_id), data)
        _audit(principal, "fg_pc_upload_finalized", {
            "session_id": session_id,
            "library_id": library_id,
            "name": metadata["name"],
            "path": metadata["path"],
            "selected_files": expected,
            "unique_files": unique_count,
        })
        return {**_session_payload(data), "library": metadata}


@router.post("/session/{session_id}/abort")
def abort_fg_upload_session(
    session_id: str,
    principal: Principal = Depends(get_current_principal),
) -> dict[str, Any]:
    _require_admin(principal)
    with _session_lock(session_id):
        data = _load_session(session_id, principal)
        if str(data.get("status")) == "completed":
            raise HTTPException(409, "Completed FG library cannot be aborted")
        data["status"] = "aborted"
        data["updated_at"] = now_iso()
        _atomic_json(_session_file(session_id), data)
        shutil.rmtree(_session_dir(session_id) / "files", ignore_errors=True)
        _audit(principal, "fg_pc_upload_aborted", {"session_id": session_id, "name": data.get("name")})
        return {"ok": True, "session_id": session_id, "status": "aborted"}

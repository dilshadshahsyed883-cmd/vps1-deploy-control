from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import threading

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from PySide6.QtGui import QImageReader

from smart_scroll.local_ai_tools import download_pexels_backgrounds, list_background_images
from .auth import Principal, get_current_principal
from .database import connect
from .shared_pexels_library import CONFIG_FILE, KEYS_FILE, SHARED_LIBRARY, server_pexels_keys
from .pexels_keyword_batch_v1 import new_batch as start_smart_pexels_batch, run_batch as run_smart_pexels_batch, get_batch_status as smart_pexels_status, _slug as smart_pexels_slug

router = APIRouter(prefix="/api/pexels-library", tags=["pexels-library"])
STATE_FILE = CONFIG_FILE.with_name("pexels_library_state.json")
AUDIT_FILE = CONFIG_FILE.parent.parent / "logs" / "pexels_library_audit.jsonl"
LOCK = threading.Lock()


class FillRequest(BaseModel):
    keywords: str = Field(min_length=1, max_length=5000)
    target_count: int = Field(ge=1, le=500)


class ConfirmRequest(BaseModel):
    confirm: str


def _active_render_count() -> int:
    with connect() as db:
        return int(db.execute("SELECT COUNT(*) FROM jobs WHERE status='running'").fetchone()[0])


def _media() -> list[Path]:
    SHARED_LIBRARY.mkdir(parents=True, exist_ok=True)
    return [p for p in SHARED_LIBRARY.rglob("*") if p.is_file()]


def _size(paths: list[Path]) -> int:
    return sum(p.stat().st_size for p in paths if p.exists())


def _state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {"status": "idle", "progress": 0, "message": "Ready"}


def _write_state(**values) -> None:
    current = _state(); current.update(values); current["updated_at"] = datetime.now(timezone.utc).isoformat()
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.chmod(0o600); temp.replace(STATE_FILE)


def _audit(principal: Principal, action: str, detail: dict) -> None:
    AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    row = {"at": datetime.now(timezone.utc).isoformat(), "actor": principal.username, "admin": principal.is_admin, "action": action, **detail}
    with AUDIT_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def _require_admin(principal: Principal) -> None:
    if not principal.is_admin:
        raise HTTPException(403, "Administrator access required")


def _fill_worker(keywords: str, target: int, actor: str) -> None:
    with LOCK:
        try:
            keys = server_pexels_keys()
            if not keys:
                raise RuntimeError("No server Pexels API keys are configured")
            queries = [x.strip() for x in keywords.replace("\n", ",").split(",") if x.strip()]
            last_error = ""
            for index, key in enumerate(keys, 1):
                try:
                    before = len(list_background_images(SHARED_LIBRARY))
                    _write_state(status="running", progress=5, message=f"Searching Pexels with server key {index}/{len(keys)}")
                    paths = download_pexels_backgrounds(
                        api_key=key, queries=queries, needed=target,
                        cache_dir=SHARED_LIBRARY, use_existing=True,
                        randomize=True, min_library_size=target,
                        progress_callback=lambda p, m: _write_state(status="running", progress=int(p), message=str(m)),
                    )
                    after = len(list_background_images(SHARED_LIBRARY))
                    if before < target and after <= before:
                        last_error = f"Server Pexels key {index} downloaded no new media"
                        continue
                    _write_state(status="completed", progress=100, message=f"Library ready: {len(paths)} reusable image(s)", actor=actor)
                    return
                except Exception as exc:
                    last_error = str(exc)
            raise RuntimeError(last_error or "Every configured Pexels key failed")
        except Exception as exc:
            _write_state(status="failed", progress=0, message=str(exc), actor=actor)


@router.get("/status")
def status(principal: Principal = Depends(get_current_principal)) -> dict:
    files = _media()
    return {"ok": True, "file_count": len(files), "size_bytes": _size(files), "key_count": len(server_pexels_keys()), "active_renders": _active_render_count(), "is_admin": principal.is_admin, **_state()}


@router.post("/fill")
def fill(payload: FillRequest, tasks: BackgroundTasks, principal: Principal = Depends(get_current_principal)) -> dict:
    if smart_pexels_status(CONFIG_FILE).get("status") in ("queued", "running"):
        raise HTTPException(409, "Smart Pexels Fill is active; wait for it to finish")
    state = _state()
    if state.get("status") in ("queued", "running"):
        raise HTTPException(409, "Library fill is already running")
    _write_state(status="queued", progress=0, message="Fill request queued", actor=principal.username)
    _audit(principal, "fill", {"keywords": payload.keywords, "target_count": payload.target_count})
    tasks.add_task(_fill_worker, payload.keywords, payload.target_count, principal.username)
    return {"ok": True, "status": "queued"}


class SmartPexelsFillRequest(BaseModel):
    keywords: str = Field(default="", max_length=30000)
    images_per_keyword: int = Field(default=3, ge=1, le=20)
    mode: str = "new_only"
    retry_incomplete: bool = False


@router.get("/fill-batch/status")
def smart_fill_batch_status(principal: Principal = Depends(get_current_principal)) -> dict:
    return {"ok": True, **smart_pexels_status(CONFIG_FILE)}


@router.post("/fill-batch")
def smart_fill_batch(payload: SmartPexelsFillRequest, tasks: BackgroundTasks, principal: Principal = Depends(get_current_principal)) -> dict:
    if _state().get("status") in ("queued", "running"):
        raise HTTPException(409, "Legacy Pexels Fill is active; wait for it to finish")
    keys = server_pexels_keys()
    if not keys:
        raise HTTPException(409, "No server Pexels API key is configured")
    try:
        batch = start_smart_pexels_batch(config_path=CONFIG_FILE, keywords=payload.keywords,
            images_per_keyword=payload.images_per_keyword, mode=payload.mode,
            actor=principal.username, retry_incomplete=payload.retry_incomplete)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    tasks.add_task(run_smart_pexels_batch, config_path=CONFIG_FILE, library=SHARED_LIBRARY,
        keys=keys, job_id=batch["job_id"])
    _audit(principal, "smart_pexels_fill", {"job_id": batch["job_id"],
        "keywords": batch["total_keywords"], "images_per_keyword": payload.images_per_keyword,
        "mode": batch["mode"], "retry_incomplete": payload.retry_incomplete})
    return {"ok": True, "status": "queued", "job_id": batch["job_id"],
        "total_keywords": batch["total_keywords"], "requested_total": batch["requested_total"]}


# ACS_PEXELS_GALLERY_V2 — read-only, authenticated and confined to protected library.

def _smart_gallery_directory(keyword: str) -> Path:
    word = " ".join(str(keyword or "").strip().split())
    if not word or len(word) > 180:
        raise HTTPException(422, "Choose a keyword from the batch")
    root = SHARED_LIBRARY.resolve()
    folder = (root / smart_pexels_slug(word)).resolve()
    if folder.parent != root:
        raise HTTPException(400, "Invalid keyword")
    return folder


@router.get("/fill-batch/gallery")
def smart_fill_gallery(keyword: str, principal: Principal = Depends(get_current_principal)) -> dict:
    folder = _smart_gallery_directory(keyword)
    if not folder.is_dir():
        return {"ok": True, "keyword": keyword, "images": [], "total": 0}
    valid_ext = {".jpg", ".jpeg", ".png", ".webp"}
    pictures = []
    for path in folder.iterdir():
        if path.is_symlink() or not path.is_file() or path.suffix.lower() not in valid_ext or path.name.startswith("."):
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        if st.st_size < 10000:
            continue
        pictures.append((st.st_mtime, path.name, st.st_size))
    pictures.sort(reverse=True)
    return {"ok": True, "keyword": keyword, "total": len(pictures),
            "images": [{"name": name, "size_bytes": size} for _, name, size in pictures[:80]]}


@router.get("/fill-batch/image")
def smart_fill_image(keyword: str, name: str, principal: Principal = Depends(get_current_principal)):
    folder = _smart_gallery_directory(keyword)
    if not name or len(name) > 240 or Path(name).name != name or name.startswith("."):
        raise HTTPException(400, "Invalid image filename")
    if Path(name).suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise HTTPException(400, "Unsupported image format")
    if not folder.is_dir():
        raise HTTPException(404, "Keyword images not found")
    candidate = folder / name
    if candidate.is_symlink() or not candidate.is_file() or candidate.resolve().parent != folder:
        raise HTTPException(404, "Image not found")
    if candidate.stat().st_size < 10000:
        raise HTTPException(404, "Invalid image")
    return FileResponse(candidate, headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"})


@router.post("/clear")
def clear(payload: ConfirmRequest, principal: Principal = Depends(get_current_principal)) -> dict:
    _require_admin(principal)
    if payload.confirm != "CLEAR ENTIRE LIBRARY":
        raise HTTPException(400, "Exact confirmation required")
    if _active_render_count():
        raise HTTPException(409, "Active render detected; library was not changed")
    files = _media(); removed_bytes = _size(files)
    for path in files: path.unlink(missing_ok=True)
    _audit(principal, "clear", {"removed": len(files), "bytes": removed_bytes})
    return {"ok": True, "removed": len(files), "bytes": removed_bytes}


@router.post("/cleanup")
def cleanup(principal: Principal = Depends(get_current_principal)) -> dict:
    _require_admin(principal)
    if _active_render_count():
        raise HTTPException(409, "Active render detected; library was not changed")
    broken: list[Path] = []; duplicates: list[Path] = []; seen: dict[str, Path] = {}
    for path in _media():
        reader = QImageReader(str(path))
        if not reader.canRead():
            broken.append(path); continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest in seen: duplicates.append(path)
        else: seen[digest] = path
    targets = broken + duplicates; removed_bytes = _size(targets)
    for path in targets: path.unlink(missing_ok=True)
    _audit(principal, "cleanup", {"broken": len(broken), "duplicates": len(duplicates), "bytes": removed_bytes})
    return {"ok": True, "broken": len(broken), "duplicates": len(duplicates), "removed": len(targets), "bytes": removed_bytes}


@router.post("/reset-work-history")
def reset_work_history(payload: ConfirmRequest, principal: Principal = Depends(get_current_principal)) -> dict:
    _require_admin(principal)
    if payload.confirm != "RESET ALL HISTORY":
        raise HTTPException(400, "Exact confirmation required")
    terminal = ("completed", "failed", "cancelled", "interrupted")
    data_root = CONFIG_FILE.parent.parent
    backup_dir = data_root / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"auto_caption.before_history_reset_{stamp}.db"
    with connect() as db:
        placeholders = ",".join("?" for _ in terminal)
        active_jobs = int(db.execute(
            f"SELECT COUNT(*) FROM jobs WHERE status NOT IN ({placeholders})", terminal
        ).fetchone()[0])
        active_ingress = int(db.execute(
            "SELECT COUNT(*) FROM drive_ingress WHERE status IN ('pending','downloading')"
        ).fetchone()[0])
        if active_jobs or active_ingress:
            raise HTTPException(409, f"Reset refused: {active_jobs} active job(s), {active_ingress} active upload(s)")
        counts = {
            "jobs": int(db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]),
            "batches": int(db.execute("SELECT COUNT(*) FROM upload_batches").fetchone()[0]),
            "ingress": int(db.execute("SELECT COUNT(*) FROM drive_ingress").fetchone()[0]),
        }
        backup = sqlite3.connect(str(backup_path))
        try:
            db.backup(backup)
        finally:
            backup.close()
        db.execute("BEGIN IMMEDIATE")
        for table in ("job_events", "job_attempts", "drive_ingress", "batch_items", "jobs", "upload_batches", "scheduler_state"):
            db.execute(f"DELETE FROM {table}")
        db.commit()
    _audit(principal, "reset_work_history", {**counts, "backup": backup_path.name})
    return {"ok": True, "removed": counts, "backup": backup_path.name}

from __future__ import annotations

import json
import copy
import math
import hashlib
import hmac
import html
import os
import re
import shutil
import sqlite3
import subprocess
import signal
import threading
import time
import traceback
import uuid
import zipfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from contextvars import ContextVar
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, JSONResponse

from smart_scroll.caption_sync import build_caption_segments, detect_first_speech_start
from smart_scroll.audio_mixer import prepare_final_audio
from smart_scroll.audio_library_v174 import resolve_music_playlist, resolve_sfx_plan
from smart_scroll.ffmpeg_utils import get_audio_duration
from vps_engine.internal_audio_engine import VERSION as INTERNAL_AUDIO_ENGINE_VERSION, process_voice as internal_audio_engine_process, presets_payload as internal_audio_engine_presets, resolve_settings as internal_audio_engine_resolve_settings
from smart_scroll.project_model import ProjectSettings
from smart_scroll.reliability_engine import candidate_score, sha256_file, validate_media, should_continue_previous_visual, build_working_video_clip, working_clip_target_duration
from smart_scroll.scene_txt_pipeline import align_scenes_to_captions, normalize_scene_timing
from smart_scroll.transition_engine import assign_auto_transitions
from smart_scroll.v2_renderer import render_mixed_media
from smart_scroll.v3_engine import download_result, search_multi_provider, optimize_video_candidate, find_cached_provider_media
from vps_engine.worker_registry import WorkerRegistry, env_capacity
from vps_engine.sml_worker_bridge import SMLWorkerClient, SMLMediaUnavailable

WORKER_API_VERSION = 26
WORKER_BUILD = "2026.09.06-professional-editor-v17.5d-r6-internal-audio-studio-v1"

BASE = Path(os.getenv("SCENE_WORKER_DATA", "/data")).resolve()
JOBS = BASE / "jobs"
JOBS.mkdir(parents=True, exist_ok=True)
DB_PATH = BASE / "queue.sqlite3"
TOKEN = os.getenv("SCENE_WORKER_TOKEN", "").strip()
WORKER_ADMISSION_ONLY = os.getenv("SCENE_WORKER_ADMISSION_ONLY", "0") == "1"
if WORKER_ADMISSION_ONLY:
    # Never attach admission to any existing production job queue.
    if (len(TOKEN) < 32 or "scene-worker-admission" not in str(BASE)
            or not BASE.is_absolute() or
            os.getenv("DRIVE_UPLOAD_ENABLED", "0") != "0" or
            os.getenv("AUTO_CLEAN_MEDIA_AFTER_RENDER", "0") != "0"):
        raise RuntimeError("admission requires isolated state, long token and disabled drive/cleanup")

MAX_UPLOAD = int(os.getenv("MAX_UPLOAD_MB", "5000")) * 1024 * 1024
PERMANENT_ROOT = (BASE / "permanent").resolve()
PERMANENT_MUSIC_DIR = PERMANENT_ROOT / "music"
PERMANENT_SFX_DIR = PERMANENT_ROOT / "sfx"
PERMANENT_MUSIC_DIR.mkdir(parents=True, exist_ok=True)
PERMANENT_SFX_DIR.mkdir(parents=True, exist_ok=True)
# V17.4 deliberately has no shared Pexels media cache. Every provider download
# belongs to its job and disappears when that project is cleaned.
# V17 policy: job failures are never retried automatically. Recovery is an
# explicit operator action: Resume, Restart Stage, or Restart Job. Legacy env
# values are intentionally ignored so a stale deployment cannot re-enable retry.
AUTO_RETRY_INTERRUPTED = False
MAX_RETRIES = 0
DRIVE_UPLOAD_ENABLED = os.getenv("DRIVE_UPLOAD_ENABLED", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
DRIVE_REMOTE = os.getenv("DRIVE_REMOTE", "gdrive").strip() or "gdrive"
DRIVE_FOLDER = os.getenv("DRIVE_FOLDER", "SceneBuilder-Renders").strip() or "SceneBuilder-Renders"
# SCENE_CHANNEL_DRIVE_FOLDERS_V1
def _safe_drive_channel(value: str) -> str:
    compact = " ".join(str(value or "").strip().split())
    compact = "".join(ch for ch in compact if ch.isalnum() or ch in " ._-").strip(" .")
    return compact[:80]

def _drive_folder_for_channel(value: str) -> str:
    channel = _safe_drive_channel(value)
    return f"{DRIVE_FOLDER}/{channel}" if channel else DRIVE_FOLDER
DRIVE_TRANSFER_MODE = os.getenv("DRIVE_TRANSFER_MODE", "move").strip().lower() or "move"
DRIVE_SCRIPT = Path(
    os.getenv("DRIVE_SCRIPT", "/app/vps_scripts/move_output_to_drive.sh")
).resolve()
COMPLETED_OUTPUT_RETENTION_DAYS = max(
    0, int(os.getenv("COMPLETED_OUTPUT_RETENTION_DAYS", "7"))
)
FAILED_JOB_RETENTION_DAYS = max(0, int(os.getenv("FAILED_JOB_RETENTION_DAYS", "14")))
QUEUE_HISTORY_RETENTION_DAYS = max(1, int(os.getenv("QUEUE_HISTORY_RETENTION_DAYS", "90")))
DELETE_OUTPUT_ONLY_AFTER_DRIVE_UPLOAD = os.getenv(
    "DELETE_OUTPUT_ONLY_AFTER_DRIVE_UPLOAD", "1"
).strip().lower() not in {"0", "false", "no", "off"}

# V17.2+ unattended-batch hardening. These guards are intentionally conservative:
# they pause/retain work rather than risk storage exhaustion or silent delivery loss.
MIN_FREE_SPACE_GB = max(1.0, float(os.getenv("MIN_FREE_SPACE_GB", "8")))
MIN_FREE_SPACE_BYTES = int(MIN_FREE_SPACE_GB * 1024 * 1024 * 1024)
RETENTION_SWEEP_SECONDS = max(300, int(os.getenv("RETENTION_SWEEP_SECONDS", "1800")))
FAILURE_CIRCUIT_BREAKER_THRESHOLD = max(2, int(os.getenv("FAILURE_CIRCUIT_BREAKER_THRESHOLD", "3")))
DRIVE_UPLOAD_TIMEOUT_SECONDS = max(300, int(os.getenv("DRIVE_UPLOAD_TIMEOUT_SECONDS", str(24 * 60 * 60))))
DRIVE_UPLOAD_STALL_SECONDS = max(120, int(os.getenv("DRIVE_UPLOAD_STALL_SECONDS", "1800")))
DRIVE_VERIFY_HASH = os.getenv("DRIVE_VERIFY_HASH", "1").strip().lower() not in {"0", "false", "no", "off"}

# Independent execution lanes. Multiple project pipeline threads may overlap, while
# each heavy resource keeps its own explicit capacity. Defaults are conservative for
# a 4-vCore / 8-GB CPU VPS and can be tuned without changing code.
PIPELINE_WORKERS = env_capacity("PIPELINE_WORKERS", 3)
AUTO_CLEAN_MEDIA_AFTER_RENDER = os.getenv("AUTO_CLEAN_MEDIA_AFTER_RENDER", "1").strip().lower() not in {"0","false","no","off"}
COMPRESSOR_ENABLED = os.getenv("COMPRESSOR_ENABLED", "0").strip().lower() in {"1","true","yes","on"}
COMPRESS_CRF = max(18, min(32, int(os.getenv("COMPRESS_CRF", "25"))))
WORKERS = WorkerRegistry({
    "transcribe": env_capacity("TRANSCRIBE_WORKERS", 1),
    "pexels": env_capacity("PEXELS_WORKERS", 3),
    "render": env_capacity("RENDER_WORKERS", 1),
    "compressor": env_capacity("COMPRESS_WORKERS", 1),
    "drive_uploader": env_capacity("DRIVE_UPLOAD_WORKERS", 1),
    "cleanup": env_capacity("CLEANUP_WORKERS", 1),
})


def _split_keys(*values: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        for key in re.split(r"[,;\s]+", str(value or "")):
            key = key.strip()
            if key and key not in seen:
                seen.add(key)
                result.append(key)
    return result


PEXELS_ENV_KEYS = _split_keys(
    os.getenv("PEXELS_API_KEY", ""),
    os.getenv("PEXELS_API_KEYS", ""),
)
PEXELS_KEYS_FILE = PERMANENT_ROOT / "pexels_keys.txt"


def _current_pexels_keys() -> list[str]:
    """Return env + web-managed supplemental keys without worker recreation.

    The file lives in the permanent VPS area and is intentionally outside every
    production-clean path. It is read when media resolution/health needs it so
    administrators may add more keys from Storage Manager immediately.
    """
    supplemental = ""
    try:
        supplemental = PEXELS_KEYS_FILE.read_text(encoding="utf-8")
    except OSError:
        pass
    return _split_keys(*PEXELS_ENV_KEYS, supplemental)

app = FastAPI(
    title="Scene Builder Persistent Queue Worker",
    version=str(WORKER_API_VERSION),
)
_db_lock = threading.RLock()
_wake = threading.Event()
_stop = threading.Event()
_worker_header_token: ContextVar[str] = ContextVar("scene_worker_header_token", default="")
_audit_locks_guard = threading.RLock()
_audit_locks: dict[str, threading.RLock] = {}


def _audit_lock(job_id: str) -> threading.RLock:
    with _audit_locks_guard:
        return _audit_locks.setdefault(str(job_id), threading.RLock())


@app.middleware("http")
async def admission_read_only_gate(request: Request, call_next):
    if WORKER_ADMISSION_ONLY:
        if request.method != "GET" or request.url.path not in ("/health", "/jobs"):
            return JSONResponse({"ok": False, "error": "worker admission: mutations disabled"}, status_code=403)
        if request.url.path == "/jobs":
            supplied = str(request.headers.get("x-scene-worker-token") or "").strip()
            if not hmac.compare_digest(supplied, TOKEN):
                return JSONResponse({"ok": False, "error": "worker authorization required"}, status_code=401)
    return await call_next(request)


@app.middleware("http")
async def capture_scene_worker_proxy_token(request: Request, call_next):
    """Accept a worker token injected by the authenticated VPS-1 /SB2 proxy."""
    marker = _worker_header_token.set(str(request.headers.get("x-scene-worker-token") or "").strip())
    try:
        return await call_next(request)
    finally:
        _worker_header_token.reset(marker)


PATH_KEYS = {
    "audio_path",
    "background_image_path",
    "foreground_image_path",
    "music_assigned_path",
    "render_audio_path",
    "lut_custom_path",
    "piper_model_path",
    "media_path",
    "media_source_path",
    "image_path",
    "video_path",
    "video_candidate_path",
    "source_path",
    "file_path",
    "path",
}
PATH_LIST_KEYS = {
    "music_paths",
    "background_video_paths",
    "pexels_cache_paths",
}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
ACTIVE_STATUSES = {"queued", "preparing", "downloading_media", "waiting_for_audit", "ready_to_render", "rendering", "ready_to_upload", "drive_uploading", "running"}
TERMINAL_STATUSES = {"completed", "cleaned", "failed", "cancelled"}

# V17.5d preserves the V17.5c persistent queue policy; media timing and Pexels selection are project-owned settings.
# Normal preserves the V17.5b overlapping pipeline. Verified Serial allows exactly
# one project to own the end-to-end production pipeline until Drive verification
# and verified local project cleanup are both complete.
QUEUE_MODE_NORMAL = "normal"
QUEUE_MODE_VERIFIED_SERIAL = "verified_serial"
SERIAL_BUSY_STATUSES = {
    "preparing", "downloading_media", "waiting_for_audit", "ready_to_render",
    "rendering", "ready_to_upload", "drive_uploading", "running",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _runtime_state_get(key: str, default: str = "") -> str:
    try:
        with _db_lock, db() as connection:
            row = connection.execute("SELECT value FROM runtime_state WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else default
    except Exception:
        return default


def _runtime_state_set(key: str, value: str) -> None:
    with _db_lock, db() as connection:
        connection.execute(
            "INSERT INTO runtime_state(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            (key, str(value), now_iso()),
        )


def _pipeline_pause_state() -> dict[str, Any]:
    raw = _runtime_state_get("pipeline_pause", "")
    if not raw:
        return {"paused": False, "reason": ""}
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return {
                "paused": bool(data.get("paused")),
                "reason": str(data.get("reason") or ""),
                "paused_at": str(data.get("paused_at") or ""),
            }
    except Exception:
        pass
    return {"paused": False, "reason": ""}


def _pipeline_paused() -> bool:
    return bool(_pipeline_pause_state().get("paused"))


def _queue_mode() -> str:
    mode = _runtime_state_get("queue_mode", QUEUE_MODE_NORMAL).strip().lower()
    return mode if mode in {QUEUE_MODE_NORMAL, QUEUE_MODE_VERIFIED_SERIAL} else QUEUE_MODE_NORMAL


def _serial_active_job_id() -> str:
    return _runtime_state_get("serial_active_job_id", "").strip()


def _serial_job_release_ready(job: dict[str, Any]) -> bool:
    return (
        str(job.get("status") or "") == "cleaned"
        and str(job.get("drive_status") or "") == "completed"
        and bool(str(job.get("files_cleaned_at") or "").strip())
    )


def _runtime_value_conn(connection: sqlite3.Connection, key: str, default: str = "") -> str:
    row = connection.execute("SELECT value FROM runtime_state WHERE key=?", (key,)).fetchone()
    return str(row["value"]) if row else default


def _runtime_set_conn(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute(
        "INSERT INTO runtime_state(key,value,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
        (key, str(value), now_iso()),
    )


def _serial_reconcile_conn(connection: sqlite3.Connection, *, adopt_single_existing: bool = True) -> str:
    """Return the persistent serial owner after reconciling stale/completed state.

    Existing concurrent work is never killed. When Verified Serial is enabled while
    Normal mode has work in flight, new claims stop. If exactly one existing project
    remains, it is adopted as the serial owner; otherwise the old work drains first.
    """
    mode = _runtime_value_conn(connection, "queue_mode", QUEUE_MODE_NORMAL).strip().lower()
    if mode != QUEUE_MODE_VERIFIED_SERIAL:
        if _runtime_value_conn(connection, "serial_active_job_id", ""):
            _runtime_set_conn(connection, "serial_active_job_id", "")
        return ""

    active_id = _runtime_value_conn(connection, "serial_active_job_id", "").strip()
    if active_id:
        row = connection.execute("SELECT * FROM jobs WHERE id=?", (active_id,)).fetchone()
        if row is None:
            _runtime_set_conn(connection, "serial_active_job_id", "")
            active_id = ""
        else:
            job = dict(row)
            if _serial_job_release_ready(job):
                _runtime_set_conn(connection, "serial_active_job_id", "")
                active_id = ""
            else:
                return active_id

    if not adopt_single_existing:
        return ""

    placeholders = ",".join("?" for _ in SERIAL_BUSY_STATUSES)
    rows = connection.execute(
        f"SELECT id,status FROM jobs WHERE cancel_requested=0 AND status IN ({placeholders}) "
        "ORDER BY priority ASC, created_at ASC",
        tuple(sorted(SERIAL_BUSY_STATUSES)),
    ).fetchall()
    if len(rows) == 1:
        active_id = str(rows[0]["id"])
        _runtime_set_conn(connection, "serial_active_job_id", active_id)
        return active_id
    return ""


def _queue_mode_state() -> dict[str, Any]:
    mode = _queue_mode()
    if mode == QUEUE_MODE_NORMAL:
        return {
            "queue_mode": mode, "serial_active_job_id": "",
            "serial_state": "normal", "serial_reason": "Normal V17.5b pipeline overlap is enabled.",
        }

    with _db_lock, db() as connection:
        active_id = _serial_reconcile_conn(connection)
        placeholders = ",".join("?" for _ in SERIAL_BUSY_STATUSES)
        busy = connection.execute(
            f"SELECT id,status,project_name FROM jobs WHERE cancel_requested=0 AND status IN ({placeholders}) "
            "ORDER BY priority ASC, created_at ASC", tuple(sorted(SERIAL_BUSY_STATUSES)),
        ).fetchall()
        active_row = connection.execute("SELECT * FROM jobs WHERE id=?", (active_id,)).fetchone() if active_id else None

    if not DRIVE_UPLOAD_ENABLED:
        return {
            "queue_mode": mode, "serial_active_job_id": active_id, "serial_state": "blocked",
            "serial_reason": "Verified Serial requires Google Drive delivery verification before the next project can start.",
        }
    if active_row is not None:
        job = dict(active_row)
        status = str(job.get("status") or "")
        if status in {"failed", "cancelled"}:
            state, reason = "blocked", f"{status.title()} project is holding the serial queue. Resume/restart it or Skip & Continue."
        elif status == "completed" and not _serial_job_release_ready(job):
            if str(job.get("drive_status") or "") != "completed":
                state, reason = "blocked", "Current project is waiting for verified Google Drive delivery."
            else:
                state, reason = "blocked", "Drive is verified but local cleanup is pending. Use Clean Now to continue."
        elif status == "waiting_for_audit":
            state, reason = "active", "Current project is waiting for First 2-Minute Visual Audit approval."
        else:
            state, reason = "active", "One project owns the full production pipeline."
        return {
            "queue_mode": mode, "serial_active_job_id": active_id, "serial_state": state,
            "serial_reason": reason, "serial_active_status": status,
            "serial_active_project": str(job.get("project_name") or "Untitled"),
        }
    if len(busy) > 1:
        return {
            "queue_mode": mode, "serial_active_job_id": "", "serial_state": "activating",
            "serial_reason": f"Verified Serial is activating after {len(busy)} existing in-flight projects clear.",
        }
    return {
        "queue_mode": mode, "serial_active_job_id": "", "serial_state": "ready",
        "serial_reason": "Verified Serial is ready to claim the next queued project.",
    }


def _verified_serial_owns(job_id: str) -> bool:
    return _queue_mode() == QUEUE_MODE_VERIFIED_SERIAL and _serial_active_job_id() == str(job_id)


def _pause_pipeline(reason: str) -> None:
    _runtime_state_set(
        "pipeline_pause",
        json.dumps({"paused": True, "reason": str(reason), "paused_at": now_iso()}, separators=(",", ":")),
    )
    _wake.set()


def _resume_pipeline_state() -> None:
    _runtime_state_set("pipeline_pause", json.dumps({"paused": False, "reason": "", "paused_at": ""}, separators=(",", ":")))
    _runtime_state_set("failure_circuit", json.dumps({"stage": "", "count": 0, "updated_at": now_iso()}, separators=(",", ":")))
    _wake.set()


def _failure_stage_key(job: dict[str, Any]) -> str:
    stage = str(job.get("exact_stage") or job.get("stage") or "unknown").strip().lower()
    if "drive" in stage or str(job.get("status") or "") in {"ready_to_upload", "drive_uploading"}: return "drive_upload"
    if "render" in stage: return "render"
    if "pexels" in stage or "media" in stage or str(job.get("status") or "") == "downloading_media": return "media"
    if "whisper" in stage or "caption" in stage or "transcrib" in stage: return "whisper"
    if "audio" in stage or "music" in stage: return "audio_mix"
    if "edit" in stage or "transition" in stage or "resolved" in stage: return "resolve"
    return re.sub(r"[^a-z0-9]+", "_", stage).strip("_") or "unknown"


def _record_pipeline_failure(job: dict[str, Any]) -> tuple[int, bool]:
    stage = _failure_stage_key(job)
    try:
        current = json.loads(_runtime_state_get("failure_circuit", "{}") or "{}")
    except Exception:
        current = {}
    count = int(current.get("count") or 0) + 1 if str(current.get("stage") or "") == stage else 1
    state = {"stage": stage, "count": count, "updated_at": now_iso()}
    _runtime_state_set("failure_circuit", json.dumps(state, separators=(",", ":")))
    paused = count >= FAILURE_CIRCUIT_BREAKER_THRESHOLD
    if paused:
        _pause_pipeline(f"Pipeline paused after {count} consecutive failures at stage: {stage}")
    return count, paused


def _record_pipeline_success() -> None:
    _runtime_state_set("failure_circuit", json.dumps({"stage": "", "count": 0, "updated_at": now_iso()}, separators=(",", ":")))


def _ensure_disk_space(stage: str, extra_required_bytes: int = 0) -> None:
    usage = shutil.disk_usage(BASE)
    required = MIN_FREE_SPACE_BYTES + max(0, int(extra_required_bytes or 0))
    if usage.free < required:
        raise RuntimeError(
            f"Storage safety guard blocked {stage}: {usage.free / (1024**3):.1f} GB free; "
            f"requires at least {required / (1024**3):.1f} GB free."
        )


def db(timeout: float = 30.0) -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=max(0.1, float(timeout)), check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    return connection


def init_db() -> None:
    with _db_lock, db() as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            project_name TEXT NOT NULL DEFAULT 'Untitled',
            status TEXT NOT NULL,
            stage TEXT NOT NULL DEFAULT '',
            progress INTEGER NOT NULL DEFAULT 0,
            message TEXT NOT NULL DEFAULT '',
            priority INTEGER NOT NULL DEFAULT 100,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT NOT NULL DEFAULT '',
            completed_at TEXT NOT NULL DEFAULT '',
            retry_count INTEGER NOT NULL DEFAULT 0,
            max_retries INTEGER NOT NULL DEFAULT 0,
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            input_zip TEXT NOT NULL DEFAULT '',
            output_file TEXT NOT NULL DEFAULT '',
            size_bytes INTEGER NOT NULL DEFAULT 0,
            client_job_key TEXT NOT NULL DEFAULT '',
            error_log TEXT NOT NULL DEFAULT '',
            drive_status TEXT NOT NULL DEFAULT '',
            drive_destination TEXT NOT NULL DEFAULT '',
            drive_view_url TEXT NOT NULL DEFAULT '',
            output_filename TEXT NOT NULL DEFAULT '',
            downloaded_at TEXT NOT NULL DEFAULT '',
            files_cleaned_at TEXT NOT NULL DEFAULT '',
            submission_version TEXT NOT NULL DEFAULT '',
            exact_stage TEXT NOT NULL DEFAULT '',
            stage_state_json TEXT NOT NULL DEFAULT '',
            cancelled_at TEXT NOT NULL DEFAULT '',
            cancel_source TEXT NOT NULL DEFAULT '',
            resume_mode TEXT NOT NULL DEFAULT '',
            audit_approved_at TEXT NOT NULL DEFAULT '',
            manual_start_required INTEGER NOT NULL DEFAULT 0
        )"""
        )
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
        }
        for name in (
            "drive_status",
            "drive_destination",
            "drive_view_url",
            "output_filename",
            "downloaded_at",
            "files_cleaned_at",
            "submission_version",
            "exact_stage",
            "stage_state_json",
            "cancelled_at",
            "cancel_source",
            "resume_mode",
            "audit_approved_at",
        ):
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE jobs ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
                )
        if "manual_start_required" not in columns:
            connection.execute("ALTER TABLE jobs ADD COLUMN manual_start_required INTEGER NOT NULL DEFAULT 0")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_status_priority ON jobs(status, priority, created_at)"
        )
        connection.execute(
            'CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_client_key ON jobs(client_job_key) WHERE client_job_key <> ""'
        )
        connection.execute("UPDATE jobs SET max_retries=0")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS runtime_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS job_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT '',
            stage TEXT NOT NULL DEFAULT '',
            progress INTEGER NOT NULL DEFAULT 0,
            message TEXT NOT NULL DEFAULT ''
        )"""
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_job_events_job_time ON job_events(job_id, id)"
        )
        connection.execute(
            "INSERT OR IGNORE INTO runtime_state(key,value,updated_at) VALUES('queue_mode',?,?)",
            (QUEUE_MODE_NORMAL, now_iso()),
        )
        connection.execute(
            "INSERT OR IGNORE INTO runtime_state(key,value,updated_at) VALUES('serial_active_job_id','',?)",
            (now_iso(),),
        )

        interrupted = connection.execute(
            "SELECT id,retry_count,max_retries FROM jobs "
            "WHERE status IN ('running','preparing','downloading_media','ready_to_render','rendering','ready_to_upload','drive_uploading')"
        ).fetchall()
        for row in interrupted:
            stamp = now_iso()
            connection.execute(
                "UPDATE jobs SET status='failed',stage='Interrupted',max_retries=0,"
                "message='Worker restarted while job was active; choose Resume, Restart Stage, or Restart Job',updated_at=? WHERE id=?",
                (stamp, row["id"]),
            )
            connection.execute(
                "INSERT INTO job_events(job_id,created_at,status,stage,progress,message) "
                "SELECT id,?,'failed','Interrupted',progress,'Worker restarted while job was active; choose Resume, Restart Stage, or Restart Job' "
                "FROM jobs WHERE id=?",
                (stamp, row["id"]),
            )


def auth(token: str | None) -> None:
    provided = str(token or _worker_header_token.get() or "").strip()
    if TOKEN and not hmac.compare_digest(provided, TOKEN):
        raise HTTPException(401, "Invalid worker token")


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        raise HTTPException(404, "Job not found")
    return dict(row)


def get_job(job_id: str) -> dict[str, Any]:
    with _db_lock, db() as connection:
        return row_to_dict(
            connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        )


def _update_job_once(job_id: str, values: dict[str, Any], db_timeout: float = 30.0) -> None:
    if not values:
        return
    payload = dict(values)
    event_relevant = any(key in payload for key in ("status", "stage", "progress", "message"))
    stamp = now_iso()
    payload["updated_at"] = stamp
    columns = ",".join(f"{key}=?" for key in payload)
    with _db_lock, db(timeout=db_timeout) as connection:
        before = connection.execute(
            "SELECT status,stage,progress,message FROM jobs WHERE id=?", (job_id,)
        ).fetchone() if event_relevant else None
        connection.execute(
            f"UPDATE jobs SET {columns} WHERE id=?",
            (*payload.values(), job_id),
        )
        if event_relevant:
            after = connection.execute(
                "SELECT status,stage,progress,message FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            before_state = tuple(before) if before is not None else None
            after_state = tuple(after) if after is not None else None
            if after is not None and before_state != after_state:
                connection.execute(
                    "INSERT INTO job_events(job_id,created_at,status,stage,progress,message) VALUES(?,?,?,?,?,?)",
                    (job_id, stamp, str(after["status"] or ""), str(after["stage"] or ""),
                     int(after["progress"] or 0), str(after["message"] or "")),
                )


def update_job(job_id: str, **values: Any) -> None:
    _update_job_once(job_id, values, db_timeout=30.0)


# R5 render reliability: renderer progress/checkpoint callbacks are telemetry, not
# render correctness. A transient SQLite path/lock failure must never terminate
# FFmpeg after expensive media resolution and completed render chunks already exist.
_RENDER_DB_RETRY_DELAYS = (0.0, 0.20, 0.60)
_render_db_pending_lock = threading.RLock()
_render_db_pending: dict[str, dict[str, Any]] = {}


def _append_render_db_warning(job_id: str, message: str) -> None:
    """Best-effort local breadcrumb when SQLite telemetry is temporarily unavailable."""
    try:
        path = JOBS / job_id / "render_status_fallback.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{now_iso()} {message}\n")
    except OSError:
        pass


def _render_db_update(job_id: str, **values: Any) -> bool:
    """Persist render telemetry without allowing a transient DB error to abort FFmpeg.

    The newest unsaved state is retained in memory and merged into the next callback.
    The renderer's own on-disk checkpoint.json is written before this callback runs,
    so render-resume authority remains durable even while SQLite is unavailable.
    """
    with _render_db_pending_lock:
        merged = dict(_render_db_pending.get(job_id) or {})
        merged.update(values)
    last_error: BaseException | None = None
    for delay in _RENDER_DB_RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            _update_job_once(job_id, merged, db_timeout=1.0)
        except (sqlite3.OperationalError, OSError) as exc:
            last_error = exc
            continue
        with _render_db_pending_lock:
            _render_db_pending.pop(job_id, None)
        return True
    with _render_db_pending_lock:
        _render_db_pending[job_id] = merged
    _append_render_db_warning(
        job_id,
        "SQLite render telemetry deferred; renderer continued: " + str(last_error or "unknown database error"),
    )
    return False


def _flush_render_db_update(job_id: str) -> bool:
    with _render_db_pending_lock:
        pending = dict(_render_db_pending.get(job_id) or {})
    if not pending:
        return True
    return _render_db_update(job_id, **pending)


def safe_extract(archive: zipfile.ZipFile, destination: Path) -> None:
    base = destination.resolve()
    for member in archive.infolist():
        target = (destination / member.filename).resolve()
        if not str(target).startswith(str(base)):
            raise ValueError("Unsafe ZIP path")
    archive.extractall(destination)


def _resolve_file_reference(
    value: Any,
    files: dict[str, list[Path]],
    source_root: Path,
) -> Any:
    if not isinstance(value, str) or not value:
        return value
    if len(value) > 1024 or "\n" in value or "\r" in value:
        return value
    try:
        candidate = Path(value)
        candidates: list[Path] = []
        if candidate.is_absolute():
            candidates.append(candidate)
        else:
            candidates.append(source_root / candidate)
        for item in candidates:
            if item.is_file():
                return str(item.resolve())
        hits = files.get(candidate.name.lower(), [])
        if len(hits) == 1:
            return str(hits[0].resolve())
    except (OSError, ValueError):
        return value
    return value


def remap_paths(
    value: Any,
    files: dict[str, list[Path]],
    source_root: Path,
    key: str = "",
) -> Any:
    """Remap only fields that are explicitly file paths.

    The old worker attempted Path.exists() on every string, including narration text
    and JSON signatures. Long narration therefore raised Linux Errno 36. This
    function never treats normal text as a path.
    """

    if isinstance(value, dict):
        return {
            child_key: remap_paths(child_value, files, source_root, child_key)
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        if key in PATH_LIST_KEYS or key.endswith("_paths"):
            return [
                _resolve_file_reference(item, files, source_root)
                for item in value
            ]
        return [remap_paths(item, files, source_root, "") for item in value]
    if isinstance(value, str) and (
        key in PATH_KEYS or key.endswith("_path") or key.endswith("_file")
    ):
        return _resolve_file_reference(value, files, source_root)
    return value


def cancelled(job_id: str) -> bool:
    try:
        return bool(get_job(job_id).get("cancel_requested"))
    except Exception:
        return False



def _manual_start_required(job_id: str) -> bool:
    try:
        return bool(int(get_job(job_id).get("manual_start_required") or 0))
    except Exception:
        return False


def _mark_next_job_manual_start(exclude_job_id: str) -> str:
    """After an operator cancellation, stop the next project at Ready to Render."""
    with _db_lock, db() as connection:
        row = connection.execute(
            "SELECT id FROM jobs WHERE id<>? AND cancel_requested=0 "
            "AND status IN ('ready_to_render','preparing','downloading_media','queued') "
            "ORDER BY CASE status WHEN 'ready_to_render' THEN 0 WHEN 'preparing' THEN 1 WHEN 'downloading_media' THEN 1 ELSE 2 END, priority ASC, created_at ASC LIMIT 1",
            (exclude_job_id,),
        ).fetchone()
        if not row:
            return ""
        next_id=str(row["id"])
        connection.execute("UPDATE jobs SET manual_start_required=1,updated_at=? WHERE id=?",(now_iso(),next_id))
        return next_id


def _wait_for_manual_start(job_id: str) -> None:
    while _manual_start_required(job_id) or _pipeline_paused():
        if cancelled(job_id):
            raise RuntimeError("Remote render cancelled.")
        current=get_job(job_id)
        if _pipeline_paused():
            reason = str(_pipeline_pause_state().get("reason") or "Pipeline paused")
            if current.get("status") != "ready_to_render" or "Pipeline paused" not in str(current.get("message") or ""):
                update_job(job_id,status="ready_to_render",stage="Ready to Render",progress=35,message=reason + " · resume pipeline to continue")
        elif current.get("status") != "ready_to_render":
            update_job(job_id,status="ready_to_render",stage="Ready to Render",progress=35,message="Ready to render · Start required after previous cancellation")
        time.sleep(1.0)


def _preparation_slot_available(connection: sqlite3.Connection) -> bool:
    row=connection.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE cancel_requested=0 AND status IN ('preparing','downloading_media','ready_to_render')"
    ).fetchone()
    return int(row["n"] or 0)==0

def safe_project_name(value: str) -> str:
    cleaned = "".join(
        character if character.isalnum() or character in "-_ " else "_"
        for character in value
    ).strip()
    return cleaned or "Untitled"


def drive_output_filename(project_name: str) -> str:
    """Return one safe, direct Drive filename for a completed project."""
    value = str(project_name or "").strip()
    if value.lower().endswith(".mp4"):
        value = value[:-4]
    cleaned = safe_project_name(value)
    return f"{cleaned}.mp4"


def _read_script_text(data: dict[str, Any], source_root: Path) -> str:
    metadata = dict(data.get("metadata") or {})
    raw_text = str(data.get("text") or "").strip()
    marked_as_file = bool(metadata.get("text_field_is_relative_file"))

    if raw_text and not marked_as_file:
        return raw_text

    candidates: list[Path] = []
    for value in (
        str(metadata.get("project_text_file") or "").strip(),
        raw_text if marked_as_file else "",
        "project.txt",
    ):
        if not value or len(value) > 240 or "\n" in value or "\r" in value:
            continue
        candidate = Path(value)
        if candidate.is_absolute():
            candidates.append(candidate)
        else:
            candidates.append(source_root / candidate)
            candidates.extend(source_root.rglob(candidate.name))

    seen: set[str] = set()
    for candidate in candidates:
        identity = str(candidate)
        if identity in seen:
            continue
        seen.add(identity)
        try:
            if candidate.is_file():
                text = candidate.read_text(encoding="utf-8-sig").strip()
                if text:
                    return text
        except (OSError, UnicodeError):
            continue
    return ""


def _scene_keywords(scene: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in (
        "primary_keyword",
        "keyword",
        "query",
        "tag",
        "fallback_keyword",
    ):
        value = str(scene.get(key, "") or "").strip()
        if value and not value.upper().startswith("SCENE_"):
            values.append(value)

    fallback = scene.get("fallback_keywords", [])
    if isinstance(fallback, str):
        fallback = [
            part.strip()
            for part in re.split(r"[,;|\n]+", fallback)
            if part.strip()
        ]
    if isinstance(fallback, list):
        values.extend(str(item or "").strip() for item in fallback)

    text = " ".join(str(scene.get("text", "") or "").split())
    if text:
        values.append(" ".join(text.split()[:10]))

    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = " ".join(value.split()).strip()
        identity = normalized.casefold()
        if normalized and identity not in seen:
            seen.add(identity)
            result.append(normalized)
    return result[:4]


def _scene_duration(scene: dict[str, Any]) -> float:
    return max(
        0.05,
        float(scene.get("end", 0.0) or 0.0)
        - float(scene.get("start", 0.0) or 0.0),
    )


def _assigned_media_type(path: Path) -> str:
    return "video" if path.suffix.lower() in VIDEO_EXTENSIONS else "image"


def _scene_media_type_order(scene: dict[str, Any]) -> list[str]:
    """Return the real Pexels media-type preference order for a scene.

    Prefer Video is intentionally authoritative when video is allowed: it tries
    video before an authored image hint, then falls back to the other allowed
    type. With Prefer Video off, the authored scene media_type stays first.
    """
    authored = str(scene.get("media_type") or "").strip().lower()
    allow_video = bool(scene.get("pexels_allow_video", True))
    allow_image = bool(scene.get("pexels_allow_image", True))
    prefer_video = bool(scene.get("pexels_prefer_video", True))
    order: list[str] = []

    def add(kind: str) -> None:
        if kind == "video" and not allow_video:
            return
        if kind == "image" and not allow_image:
            return
        if kind in {"video", "image"} and kind not in order:
            order.append(kind)

    if prefer_video:
        add("video")
        add(authored)
    else:
        add(authored)
    add("video")
    add("image")
    if not order:
        order.append("video" if prefer_video else (authored if authored in {"video", "image"} else "image"))
    return order


def _download_scene_candidate(
    *,
    scene: dict[str, Any],
    scene_code: str,
    requested_type: str,
    keyword: str,
    provider_cache: Path,
    assignment_root: Path,
    used_asset_ids: set[str],
    used_hashes: set[str],
    target_width: int = 1280,
    target_height: int = 720,
    used_authors: set[str] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    errors: list[str] = []
    credentials = {"pexels": list(_current_pexels_keys()), "pixabay": []}
    duration = _scene_duration(scene)
    selection_mode = str(scene.get("pexels_selection_mode") or "best_match_variety").strip().lower()
    if selection_mode not in {"first_result", "best_match", "top_n_random", "popularity", "best_match_variety"}:
        selection_mode = "best_match_variety"
    search_pool = max(1, min(40, int(scene.get("pexels_search_pool") or 10)))
    avoid_used_media = bool(scene.get("pexels_avoid_used_media", True))
    avoid_same_creator = bool(scene.get("pexels_avoid_same_creator", True))
    prefer_landscape = bool(scene.get("pexels_prefer_landscape", True))
    author_history = used_authors if used_authors is not None else set()

    # Library second: before any network request, reuse an exact-keyword provider
    # cache hit. For video, still create a short project-local working clip.
    cached_path, cached_meta = find_cached_provider_media(provider_cache, keyword, requested_type)
    cached_resolution_ok = (
        requested_type != "video"
        or (int(cached_meta.get("width", 0) or 0) >= int(target_width) and int(cached_meta.get("height", 0) or 0) >= int(target_height))
    )
    cached_asset_key = f"{cached_meta.get('provider','pexels')}:{cached_meta.get('asset_id','')}" if isinstance(cached_meta, dict) else ""
    cached_author = str((cached_meta or {}).get("author") or "").strip().casefold() if isinstance(cached_meta, dict) else ""
    cached_not_used = (not avoid_used_media) or (not cached_asset_key) or cached_asset_key not in used_asset_ids
    cached_creator_ok = (not avoid_same_creator) or (not cached_author) or cached_author not in author_history
    if cached_path is not None and cached_path.is_file() and cached_resolution_ok and cached_not_used and cached_creator_ok:
        try:
            validation = validate_media(cached_path, requested_type, duration, require_landscape=prefer_landscape)
            if validation.get("handling") == "reject_short":
                raise ValueError("Cached video is less than 50% of scene duration")
            source_digest = sha256_file(cached_path)
            suffix = ".mp4" if requested_type == "video" else cached_path.suffix.lower()
            destination = assignment_root / f"{scene_code}{suffix}"
            temporary = assignment_root / f".{scene_code}.{uuid.uuid4().hex}{suffix}.tmp"
            clip_info = {"trimmed": False, "working_duration": float(cached_meta.get("duration", 0.0) or 0.0), "source_duration": float(cached_meta.get("duration", 0.0) or 0.0)}
            if requested_type == "video":
                clip_info = build_working_video_clip(cached_path, temporary, duration, source_duration=float(validation.get("duration", 0.0) or cached_meta.get("duration", 0.0) or 0.0), require_landscape=prefer_landscape)
            else:
                shutil.copy2(cached_path, temporary)
            temporary.replace(destination)
            digest = sha256_file(destination)
            result = dict(scene)
            result.update(
                media_path=str(destination), media_source_path=str(cached_path), media_hash=digest,
                image_hash=digest if requested_type == "image" else "",
                selected_keyword=keyword, keyword_used=keyword,
                provider=str(cached_meta.get("provider", "pexels")), asset_id=str(cached_meta.get("asset_id", "")),
                media_provider=str(cached_meta.get("provider", "pexels")), provider_asset_id=str(cached_meta.get("asset_id", "")),
                assigned_media_type=requested_type, automation_status="ready_library_reuse", status="matched_stock",
                media_locked=False, last_media_error="", validation_handling=str(validation.get("handling", "")),
                provider_duration=float(cached_meta.get("duration", 0.0) or 0.0),
                source_media_duration=float(clip_info.get("source_duration", cached_meta.get("duration", 0.0)) or 0.0),
                media_duration=float(clip_info.get("working_duration", cached_meta.get("duration", 0.0)) or 0.0),
                media_trimmed=bool(clip_info.get("trimmed")),
                working_clip_target=float(working_clip_target_duration(duration)) if requested_type == "video" else 0.0,
                visual_action="library_reuse", previously_used=True, acquisition_variant="cached_provider",
                media_author=str(cached_meta.get("author", "")), pexels_selection_mode=selection_mode,
            )
            asset_key = f"{cached_meta.get('provider','pexels')}:{cached_meta.get('asset_id','')}"
            if asset_key != "pexels:" and avoid_used_media:
                used_asset_ids.add(asset_key)
            if avoid_used_media:
                used_hashes.add(source_digest)
            if cached_author:
                author_history.add(cached_author)
            return result, ""
        except Exception as exc:
            errors.append(f"cached {keyword}: {exc}")

    try:
        candidates = search_multi_provider(
            keyword,
            requested_type,
            credentials,
            ["pexels"],
            per_provider=search_pool,
            target_width=target_width,
            target_height=target_height,
            prefer_landscape=prefer_landscape,
            preserve_provider_order=selection_mode in {"first_result", "popularity", "top_n_random"},
        )
    except Exception as exc:
        return None, f"{keyword}: search failed: {exc}"

    ranked: list[dict[str, Any]] = []
    for provider_index, candidate in enumerate(candidates):
        item = dict(candidate)
        reliability_used = used_asset_ids if avoid_used_media else set()
        item["reliability_score"] = candidate_score(item, requested_type, duration, reliability_used, require_landscape=prefer_landscape, target_width=target_width, target_height=target_height)
        author_key = str(item.get("author") or "").strip().casefold()
        variety_penalty = 18.0 if avoid_same_creator and author_key and author_key in author_history else 0.0
        item["selection_score"] = float(item.get("reliability_score", 0.0)) - variety_penalty
        item["provider_order"] = provider_index + 1
        ranked.append(item)
    if selection_mode == "best_match":
        ranked.sort(key=lambda item: float(item.get("reliability_score", 0)), reverse=True)
    elif selection_mode == "best_match_variety":
        ranked.sort(key=lambda item: float(item.get("selection_score", 0)), reverse=True)
    elif selection_mode == "top_n_random":
        top = ranked[:max(1, min(len(ranked), search_pool))]
        ranked = sorted(
            top,
            key=lambda item: hashlib.sha256(
                f"{scene_code}|{keyword}|{item.get('provider')}|{item.get('asset_id')}".encode("utf-8")
            ).hexdigest(),
        )
    # first_result and popularity intentionally keep Pexels/provider order.

    for candidate in ranked:
        if requested_type == "video":
            candidate = optimize_video_candidate(candidate, duration, target_width, target_height, prefer_landscape)
        candidate["search_keyword"] = keyword
        asset_key = f"{candidate.get('provider')}:{candidate.get('asset_id')}"
        author_key = str(candidate.get("author") or "").strip().casefold()
        if float(candidate.get("reliability_score", 0)) < 0:
            continue
        if avoid_used_media and asset_key in used_asset_ids:
            continue
        if avoid_same_creator and author_key and author_key in author_history and any(str(x.get("author") or "").strip().casefold() not in author_history for x in ranked):
            continue
        try:
            downloaded = download_result(candidate, provider_cache, scene_code)
            validation = validate_media(downloaded, requested_type, duration, require_landscape=prefer_landscape)
            if validation.get("handling") == "reject_short":
                raise ValueError("Video is less than 50% of scene duration")
            digest = sha256_file(downloaded)
            if avoid_used_media and digest in used_hashes:
                raise ValueError("Duplicate media file")

            suffix = ".mp4" if requested_type == "video" else downloaded.suffix.lower()
            destination = assignment_root / f"{scene_code}{suffix}"
            temporary = assignment_root / (
                f".{scene_code}.{uuid.uuid4().hex}{suffix}.tmp"
            )
            clip_info = {"trimmed": False, "working_duration": float(candidate.get("duration", 0.0) or 0.0), "source_duration": float(candidate.get("duration", 0.0) or 0.0)}
            if requested_type == "video":
                clip_info = build_working_video_clip(
                    downloaded, temporary, duration,
                    source_duration=float(validation.get("duration", 0.0) or candidate.get("duration", 0.0) or 0.0),
                    require_landscape=prefer_landscape,
                )
            else:
                shutil.copy2(downloaded, temporary)
            temporary.replace(destination)

            result = dict(scene)
            result.update(
                media_path=str(destination),
                media_source_path=str(downloaded),
                media_hash=digest,
                image_hash=digest if requested_type == "image" else "",
                selected_keyword=keyword,
                keyword_used=keyword,
                provider=str(candidate.get("provider", "")),
                asset_id=str(candidate.get("asset_id", "")),
                assigned_media_type=requested_type,
                automation_status="ready",
                status="matched_stock",
                media_locked=False,
                last_media_error="",
                validation_handling=str(validation.get("handling", "")),
                provider_duration=float(candidate.get("duration", 0.0) or 0.0),
                source_media_duration=float(clip_info.get("source_duration", candidate.get("duration", 0.0)) or 0.0),
                media_duration=float(clip_info.get("working_duration", candidate.get("duration", 0.0)) or 0.0),
                media_trimmed=bool(clip_info.get("trimmed")),
                working_clip_target=float(working_clip_target_duration(duration)) if requested_type == "video" else 0.0,
                visual_action="new_media",
                acquisition_variant=str(candidate.get("acquisition_variant", "provider_default")),
                previously_used=False, media_author=str(candidate.get("author") or ""),
                pexels_selection_mode=selection_mode, pexels_provider_order=int(candidate.get("provider_order") or candidate.get("rank") or 0),
                pexels_selection_score=round(float(candidate.get("selection_score", candidate.get("reliability_score", 0.0)) or 0.0), 3),
            )
            if avoid_used_media:
                used_asset_ids.add(asset_key)
                used_hashes.add(digest)
            if author_key:
                author_history.add(author_key)
            return result, ""
        except Exception as exc:
            errors.append(str(exc))
    return None, f"{keyword}: " + ("; ".join(errors[-3:]) or "no usable result")



def _try_sml_first_scene(
    *,
    client: SMLWorkerClient,
    job_id: str,
    scene: dict[str, Any],
    scene_code: str,
    keywords: list[str],
    assignment_root: Path,
    source_root: Path,
    used_sml_ids: set[str],
    used_hashes: set[str],
) -> dict[str, Any] | None:
    """Resolve SML *before* any Pexels lookup, preserving independent lifecycles.

    Only previously-approved SML video clips may be reserved. Failed integrity,
    short-duration, missing Drive restore or unusable dimensions fall through
    to the existing Pexels acquisition. Reviewed/locked scenes bypass this.
    """
    if (not bool(scene.get("_sml_first")) or not client.enabled or
            not bool(scene.get("pexels_allow_video", True))):
        return None
    scene_duration = _scene_duration(scene)
    for query in keywords[:4]:
        try:
            candidates = client.ranked(query, used_ids=used_sml_ids)
        except SMLMediaUnavailable:
            return None  # SML offline: keep Pexels operating.
        for candidate in candidates[:5]:
            clip_id = str(candidate["id"])
            reservation = None
            source = source_root / f"{scene_code}_{uuid.uuid4().hex}.mp4"
            temporary = assignment_root / f".{scene_code}_{uuid.uuid4().hex}.mp4"
            destination = assignment_root / f"{scene_code}.mp4"
            finalized = False
            try:
                reservation = client.reserve(job_id, scene_code, clip_id, used_sml_ids)
                if not reservation:
                    continue
                client.download(candidate, source)
                source_hash = sha256_file(source)
                if source_hash in used_hashes:
                    raise SMLMediaUnavailable("SML duplicate media checksum")
                validation = validate_media(
                    source, "video", scene_duration,
                    require_landscape=bool(scene.get("pexels_prefer_landscape", True)),
                )
                if validation.get("handling") == "reject_short":
                    raise SMLMediaUnavailable("SML clip too short")
                clip_info = build_working_video_clip(
                    source, temporary, scene_duration,
                    source_duration=float(validation.get("duration") or candidate.get("duration") or 0),
                    require_landscape=bool(scene.get("pexels_prefer_landscape", True)),
                )
                if not temporary.is_file():
                    raise SMLMediaUnavailable("SML working clip was not generated")
                temporary.replace(destination)
                finalized = True
                # Commit only after the job-scoped working media has been created.
                client.transition(reservation, "commit")
                digest = sha256_file(destination)
                used_sml_ids.add(clip_id)
                used_hashes.add(source_hash)
                resolved = dict(scene)
                resolved.update(
                    media_path=str(destination), media_source_path=str(source),
                    media_hash=digest, image_hash="", selected_keyword=query,
                    keyword_used=query, provider="sml", asset_id=clip_id,
                    media_provider="sml", provider_asset_id=clip_id,
                    source_type="SML", assigned_media_type="video",
                    automation_status="ready_sml", status="matched_stock",
                    media_locked=False, last_media_error="",
                    validation_handling=str(validation.get("handling") or ""),
                    provider_duration=float(candidate.get("duration") or 0),
                    source_media_duration=float(clip_info.get("source_duration") or 0),
                    media_duration=float(clip_info.get("working_duration") or 0),
                    media_trimmed=bool(clip_info.get("trimmed")),
                    working_clip_target=float(working_clip_target_duration(scene_duration)),
                    visual_action="sml_library", previously_used=False,
                    acquisition_variant="sml_v2_editor_reserved",
                    sml_reservation_id=reservation,
                )
                return resolved
            except (SMLMediaUnavailable, ValueError, OSError, RuntimeError):
                if finalized:
                    destination.unlink(missing_ok=True)
                source.unlink(missing_ok=True)
            finally:
                temporary.unlink(missing_ok=True)
                if reservation and clip_id not in used_sml_ids:
                    try:
                        client.transition(reservation, "release")
                    except SMLMediaUnavailable:
                        pass  # TTL expires on the SML side; do not block Pexels.
    return None


def _resolve_scene_stock_media(
    job_id: str,
    scenes: list[dict[str, Any]],
    job_dir: Path,
    target_width: int = 1280,
    target_height: int = 720,
) -> list[dict[str, Any]]:
    if not scenes:
        raise RuntimeError("The uploaded project contains no scene plan.")
    needs_stock_search = any(
        not (
            bool(scene.get("review_approved"))
            and bool(scene.get("review_locked"))
            and Path(str(scene.get("media_path", "") or "")).is_file()
        )
        for scene in scenes
    )
    sml_client = SMLWorkerClient()
    has_sml_first = any(bool(scene.get("_sml_first")) for scene in scenes) and sml_client.enabled
    if needs_stock_search and not has_sml_first and not any(bool(scene.get("pexels_enabled", True)) for scene in scenes):
        raise RuntimeError("No SML media integration or Pexels source is enabled for unresolved scenes.")
    if needs_stock_search and not has_sml_first and not _current_pexels_keys():
        raise RuntimeError(
            "The VPS has no Pexels API key for unresolved scenes. Set PEXELS_API_KEY or PEXELS_API_KEYS in the VPS environment and restart the worker."
        )

    assignment_root = job_dir / "resolved_media"
    assignment_root.mkdir(parents=True, exist_ok=True)
    provider_cache = job_dir / "pexels_provider_cache"
    provider_cache.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any] | None] = [None] * len(scenes)
    errors: dict[str, str] = {}
    used_asset_ids: set[str] = set()
    used_hashes: set[str] = set()
    used_authors: set[str] = set()
    used_sml_ids: set[str] = set()
    sml_source_root = job_dir / "sml_sources"
    if has_sml_first:
        sml_source_root.mkdir(parents=True, exist_ok=True)

    for index, original_scene in enumerate(scenes):
        if cancelled(job_id):
            raise RuntimeError("Remote render cancelled.")
        scene = dict(original_scene)
        scene_code = str(scene.get("scene_code") or f"SCENE_{index + 1:03d}")
        reviewed_locked = bool(scene.get("review_approved")) and bool(scene.get("review_locked"))
        reviewed_path = Path(str(scene.get("media_path", "") or ""))
        if reviewed_locked and reviewed_path.is_file():
            requested_type = _assigned_media_type(reviewed_path)
            validation = validate_media(reviewed_path, requested_type, _scene_duration(scene), require_landscape=bool(scene.get("pexels_prefer_landscape", True)))
            if validation.get("handling") == "reject_short":
                raise RuntimeError(
                    f"{scene_code}: approved reviewed video is too short for its scene duration. Unlock and replace it."
                )
            digest = sha256_file(reviewed_path)
            asset_key = f"{scene.get('media_provider') or scene.get('provider') or 'review'}:{scene.get('provider_asset_id') or scene.get('asset_id') or digest[:16]}"
            destination = assignment_root / f"{scene_code}{reviewed_path.suffix.lower()}"
            temporary = assignment_root / (
                f".{scene_code}.{uuid.uuid4().hex}{reviewed_path.suffix.lower()}.tmp"
            )
            shutil.copy2(reviewed_path, temporary)
            temporary.replace(destination)
            scene.update(
                media_path=str(destination),
                media_source_path=str(reviewed_path),
                media_hash=digest,
                image_hash=digest if requested_type == "image" else "",
                assigned_media_type=requested_type,
                automation_status="ready_review_locked",
                status="review_locked",
                media_locked=True,
                review_approved=True,
                review_locked=True,
                remote_review_media_preserved=True,
                last_media_error="",
                validation_handling=str(validation.get("handling", "")),
            )
            results[index] = scene
            used_asset_ids.add(asset_key)
            used_hashes.add(digest)
            reviewed_author = str(scene.get("media_author") or scene.get("author") or "").strip().casefold()
            if reviewed_author:
                used_authors.add(reviewed_author)
            continue

        scene_duration = _scene_duration(scene)
        # Tiny-scene efficiency rule: keep narration timing but continue the prior
        # visual. This prevents an entire Pexels video download for 0.02-0.74 sec.
        if should_continue_previous_visual(scene_duration) and index > 0:
            previous = results[index - 1]
            previous_path = Path(str((previous or {}).get("media_path", "") or ""))
            if previous and previous_path.is_file():
                scene.update(
                    media_path=str(previous_path),
                    media_source_path=str((previous or {}).get("media_source_path") or previous_path),
                    media_hash=str((previous or {}).get("media_hash") or sha256_file(previous_path)),
                    image_hash=str((previous or {}).get("image_hash") or ""),
                    assigned_media_type=str((previous or {}).get("assigned_media_type") or _assigned_media_type(previous_path)),
                    media_provider=str((previous or {}).get("media_provider") or (previous or {}).get("provider") or "previous_scene"),
                    provider_asset_id=str((previous or {}).get("provider_asset_id") or (previous or {}).get("asset_id") or ""),
                    provider_duration=float((previous or {}).get("provider_duration", 0.0) or 0.0),
                    source_media_duration=float((previous or {}).get("source_media_duration", (previous or {}).get("provider_duration", 0.0)) or 0.0),
                    media_duration=float((previous or {}).get("media_duration", 0.0) or 0.0),
                    media_trimmed=bool((previous or {}).get("media_trimmed", False)),
                    visual_action="continue_previous", previously_used=True,
                    automation_status="ready_tiny_reuse", status="matched_stock",
                    fallback_type="tiny_scene_previous",
                    fallback_source_scene_id=str((previous or {}).get("scene_code") or f"SCENE_{index:03d}"),
                    fallback_reason=f"Scene is {scene_duration:.2f}s; previous visual reused to avoid a wasteful provider download",
                    media_locked=False, last_media_error="",
                )
                results[index] = scene
                update_job(
                    job_id, status="downloading_media", stage="Pexels",
                    progress=15 + int(index / max(1, len(scenes)) * 20),
                    message=f"{scene_code}: {scene_duration:.2f}s tiny scene → previous visual reused; no provider download",
                )
                continue

        requested_types = _scene_media_type_order(scene)
        keywords = _scene_keywords(scene)
        if not keywords:
            keywords = ["documentary background"]

        if has_sml_first:
            update_job(job_id, status="downloading_media", stage="SML Media",
                       progress=15 + int(index / max(1, len(scenes)) * 20),
                       message=f"{scene_code}: searching approved SML clips before Pexels")
            matched = _try_sml_first_scene(
                client=sml_client, job_id=job_id, scene=scene,
                scene_code=scene_code, keywords=keywords,
                assignment_root=assignment_root, source_root=sml_source_root,
                used_sml_ids=used_sml_ids, used_hashes=used_hashes,
            )
            if matched is not None:
                used_asset_ids.add(f"sml:{matched['asset_id']}")
                results[index] = matched
                continue

        if not bool(scene.get("pexels_enabled", True)):
            errors[scene_code] = "SML clip not found; Pexels disabled for unresolved scene"
            results[index] = {**scene, "status": "stock_failed", "last_media_error": errors[scene_code]}
            continue

        update_job(
            job_id,
            status="downloading_media",
            stage="Pexels",
            progress=15 + int(index / max(1, len(scenes)) * 20),
            message=f"{scene_code}: searching {' → '.join(requested_types)} media",
        )

        resolved: dict[str, Any] | None = None
        last_error = "No usable Pexels result"
        for requested_type in requested_types:
            for keyword in keywords:
                resolved, last_error = _download_scene_candidate(
                    scene=scene,
                    scene_code=scene_code,
                    requested_type=requested_type,
                    keyword=keyword,
                    provider_cache=provider_cache,
                    assignment_root=assignment_root,
                    used_asset_ids=used_asset_ids,
                    used_hashes=used_hashes,
                    target_width=target_width,
                    target_height=target_height,
                    used_authors=used_authors,
                )
                if resolved is not None:
                    break
            if resolved is not None:
                break

        if (
            resolved is None
            and requested_type == "video"
            and bool(scene.get("allow_media_fallback", True))
        ):
            for keyword in keywords:
                resolved, last_error = _download_scene_candidate(
                    scene=scene,
                    scene_code=scene_code,
                    requested_type="image",
                    keyword=keyword,
                    provider_cache=provider_cache,
                    assignment_root=assignment_root,
                    used_asset_ids=used_asset_ids,
                    used_hashes=used_hashes,
                    target_width=target_width,
                    target_height=target_height,
                    used_authors=used_authors,
                )
                if resolved is not None:
                    resolved["fallback_type"] = "image_fallback"
                    resolved["fallback_reason"] = "No valid Pexels video was available"
                    break

        if resolved is not None:
            results[index] = resolved
        else:
            failed = dict(scene)
            failed.update(
                automation_status="retry_pending",
                status="stock_failed",
                last_media_error=last_error,
                attempted_keywords=keywords,
            )
            results[index] = failed
            errors[scene_code] = last_error

    resolved_indices = [
        index
        for index, scene in enumerate(results)
        if scene is not None and Path(str(scene.get("media_path", "") or "")).is_file()
    ]
    if not resolved_indices:
        report = job_dir / "media_resolution.json"
        report.write_text(
            json.dumps({"worker_build": WORKER_BUILD, "errors": errors}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        raise RuntimeError(
            "Pexels could not resolve media for any scene. Check the VPS Pexels key, internet access, and media queries. "
            f"Details: {report.name}"
        )

    # Last-resort continuity fallback: reuse the closest successfully resolved scene.
    # This prevents a single weak query from crashing the entire render while still
    # preserving every scene's timing and creative settings.
    for index, scene in enumerate(results):
        assert scene is not None
        media_path = Path(str(scene.get("media_path", "") or ""))
        if media_path.is_file():
            continue
        source_index = min(resolved_indices, key=lambda candidate: abs(candidate - index))
        source_scene = results[source_index]
        assert source_scene is not None
        source_path = Path(str(source_scene.get("media_path", "") or ""))
        scene_code = str(scene.get("scene_code") or f"SCENE_{index + 1:03d}")
        destination = assignment_root / f"{scene_code}{source_path.suffix.lower()}"
        temporary = assignment_root / (
            f".{scene_code}.{uuid.uuid4().hex}{source_path.suffix.lower()}.tmp"
        )
        shutil.copy2(source_path, temporary)
        temporary.replace(destination)
        source_code = str(
            source_scene.get("scene_code") or f"SCENE_{source_index + 1:03d}"
        )
        scene.update(
            media_path=str(destination),
            media_source_path=str(source_path),
            media_hash=sha256_file(destination),
            assigned_media_type=_assigned_media_type(destination),
            automation_status="ready_neighbor_fallback",
            status="matched_stock",
            fallback_type="neighbor_media",
            fallback_source_scene_id=source_code,
            fallback_reason=errors.get(scene_code, "Pexels query exhausted"),
            media_locked=False,
        )
        results[index] = scene

    final_scenes = [dict(scene) for scene in results if scene is not None]
    unresolved = [
        str(scene.get("scene_code", "UNKNOWN"))
        for scene in final_scenes
        if not Path(str(scene.get("media_path", "") or "")).is_file()
    ]
    report = job_dir / "media_resolution.json"
    report.write_text(
        json.dumps(
            {
                "worker_build": WORKER_BUILD,
                "scene_count": len(final_scenes),
                "fallback_count": sum(
                    1 for scene in final_scenes if scene.get("fallback_type")
                ),
                "errors": errors,
                "scenes": final_scenes,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    if unresolved:
        raise RuntimeError(
            "Stock-media validation failed before rendering. Missing: "
            + ", ".join(unresolved[:20])
        )
    return final_scenes



def _load_saved_intro_media(job_dir: Path, intro_scenes: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    report_path = job_dir / "intro_media_resolution.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        saved = [dict(item) for item in report.get("scenes", []) if isinstance(item, dict)]
    except Exception:
        return None
    if len(saved) != len(intro_scenes) or not saved:
        return None
    merged: list[dict[str, Any]] = []
    for index, (old, fresh) in enumerate(zip(saved, intro_scenes)):
        old_code = str(old.get("scene_code") or f"INTRO_{index + 1:03d}")
        new_code = str(fresh.get("scene_code") or f"INTRO_{index + 1:03d}")
        media_path = Path(str(old.get("media_path") or ""))
        if old_code != new_code or not media_path.is_file() or media_path.stat().st_size < 1024:
            return None
        try:
            validate_media(media_path, str(old.get("media_type") or "video").lower(), max(0.05, _scene_duration(fresh)), require_landscape=bool(old.get("pexels_prefer_landscape", True)))
        except Exception:
            return None
        item = dict(old)
        for key in ("start", "end", "duration", "start_seconds", "end_seconds", "duration_seconds"):
            if key in fresh:
                item[key] = fresh[key]
        merged.append(item)
    return merged


def _resolve_intro_stock_media(
    job_id: str,
    intro_scenes: list[dict[str, Any]],
    job_dir: Path,
    *,
    target_width: int,
    target_height: int,
    intro_queries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Resolve intro footage using ordered intro queries only.

    A failed query consumes its backups and then advances to the next unused intro
    query.  Documentary scenes are never used as a fallback for the intro.
    """
    if not intro_scenes:
        return []
    sml_client = SMLWorkerClient()
    sml_intro_first = sml_client.enabled and any(bool(x.get("_sml_first")) for x in intro_scenes)
    if not _current_pexels_keys() and not sml_intro_first:
        raise RuntimeError("Opening Intro needs Pexels media, but no Pexels API key is configured.")
    queries = [dict(x) for x in intro_queries if isinstance(x, dict) and str(x.get("query") or "").strip()]
    if not queries:
        raise RuntimeError("Opening Intro is enabled, but no usable INTRO_QUERY rows were supplied.")

    assignment_root = job_dir / "resolved_intro_media"
    assignment_root.mkdir(parents=True, exist_ok=True)
    provider_cache = job_dir / "pexels_provider_cache"
    provider_cache.mkdir(parents=True, exist_ok=True)
    used_asset_ids: set[str] = set()
    used_hashes: set[str] = set()
    used_authors: set[str] = set()
    resolved_rows: list[dict[str, Any]] = []
    errors: dict[str, list[str]] = {}
    next_query_index = 0
    used_sml_ids: set[str] = set()
    sml_source_root = job_dir / "sml_intro_sources"
    if sml_intro_first:
        sml_source_root.mkdir(parents=True, exist_ok=True)

    for scene_index, source_scene in enumerate(intro_scenes):
        if cancelled(job_id):
            raise RuntimeError("Remote render cancelled.")
        scene = dict(source_scene)
        scene_code = str(scene.get("scene_code") or f"INTRO_{scene_index + 1:03d}")
        requested_type = str(scene.get("media_type") or "video").lower()
        if requested_type not in {"image", "video"}:
            requested_type = "video"
        reviewed_path = Path(str(scene.get("media_path") or ""))
        if bool(scene.get("review_approved")) and bool(scene.get("review_locked")) and reviewed_path.is_file():
            requested_type = _assigned_media_type(reviewed_path)
            validation = validate_media(reviewed_path, requested_type, _scene_duration(scene), require_landscape=bool(scene.get("pexels_prefer_landscape", True)))
            if validation.get("handling") == "reject_short":
                raise RuntimeError(f"{scene_code}: approved audited intro video is too short")
            destination = assignment_root / f"{scene_code}{reviewed_path.suffix.lower()}"
            temporary = assignment_root / f".{scene_code}.{uuid.uuid4().hex}{reviewed_path.suffix.lower()}.tmp"
            shutil.copy2(reviewed_path, temporary); temporary.replace(destination)
            preserved = dict(scene)
            preserved.update(
                media_path=str(destination), media_source_path=str(reviewed_path),
                media_hash=sha256_file(destination), assigned_media_type=requested_type,
                automation_status="ready_audit_locked", status="review_locked", media_locked=True,
                review_approved=True, review_locked=True, last_media_error="",
            )
            resolved_rows.append(preserved)
            next_query_index += 1
            errors[scene_code] = []
            update_job(job_id, status="downloading_media", stage="Intro Media", progress=31 + int((scene_index + 1) / max(1, len(intro_scenes)) * 3), message=f"{scene_code}: reusing approved Visual Audit media")
            continue
        chosen = None
        local_errors: list[str] = []
        attempts = 0
        # Consume new query rows first. Only after all rows have been considered may
        # the pool wrap for an unusually long intro.
        while attempts < max(len(queries), 1):
            qindex = next_query_index % len(queries)
            query_row = queries[qindex]
            next_query_index += 1
            attempts += 1
            primary = str(query_row.get("query") or "").strip()
            backups = [str(x).strip() for x in query_row.get("backup_keywords", []) if str(x).strip()][:3]
            candidate_scene = dict(scene)
            candidate_scene.update(
                primary_keyword=primary,
                keyword=primary,
                visual_prompt=primary,
                backup_keywords=backups,
                fallback_keywords=backups,
                intro_query_order=int(query_row.get("order") or (qindex + 1)),
                intro_query_reused=next_query_index > len(queries),
                environment_sfx=str(query_row.get("environment_sfx") or scene.get("environment_sfx") or "none"),
            )
            keywords = _scene_keywords(candidate_scene) or [primary]
            last_error = "no usable result"
            if sml_intro_first:
                chosen = _try_sml_first_scene(
                    client=sml_client, job_id=job_id, scene=candidate_scene,
                    scene_code=scene_code, keywords=keywords,
                    assignment_root=assignment_root, source_root=sml_source_root,
                    used_sml_ids=used_sml_ids, used_hashes=used_hashes,
                )
            for keyword in ([] if chosen is not None else keywords):
                chosen, last_error = _download_scene_candidate(
                    scene=candidate_scene,
                    scene_code=scene_code,
                    requested_type=requested_type,
                    keyword=keyword,
                    provider_cache=provider_cache,
                    assignment_root=assignment_root,
                    used_asset_ids=used_asset_ids,
                    used_hashes=used_hashes,
                    target_width=target_width,
                    target_height=target_height,
                    used_authors=used_authors,
                )
                if chosen is not None:
                    break
            if chosen is None and requested_type == "video" and bool(candidate_scene.get("allow_media_fallback", True)):
                for keyword in keywords:
                    chosen, last_error = _download_scene_candidate(
                        scene=candidate_scene,
                        scene_code=scene_code,
                        requested_type="image",
                        keyword=keyword,
                        provider_cache=provider_cache,
                        assignment_root=assignment_root,
                        used_asset_ids=used_asset_ids,
                        used_hashes=used_hashes,
                        target_width=target_width,
                        target_height=target_height,
                        used_authors=used_authors,
                    )
                    if chosen is not None:
                        chosen["fallback_type"] = "image_fallback"
                        chosen["fallback_reason"] = "No valid Pexels intro video was available"
                        break
            if chosen is not None:
                chosen["intro_query_order"] = int(query_row.get("order") or (qindex + 1))
                chosen["intro_query_reused"] = next_query_index > len(queries)
                chosen["intro_query_advanced_after_failure"] = attempts > 1
                break
            local_errors.append(f"query {qindex + 1}: {last_error}")

        if chosen is None:
            # Intro-only continuity fallback is allowed only after ordered intro
            # queries have been exhausted. Never borrow a documentary scene.
            if resolved_rows:
                previous = resolved_rows[-1]
                source_path = Path(str(previous.get("media_path") or ""))
                destination = assignment_root / f"{scene_code}{source_path.suffix.lower()}"
                shutil.copy2(source_path, destination)
                chosen = dict(scene)
                chosen.update(
                    media_path=str(destination),
                    media_source_path=str(source_path),
                    media_hash=sha256_file(destination),
                    assigned_media_type=_assigned_media_type(destination),
                    automation_status="ready_intro_continuity_fallback",
                    status="matched_stock",
                    fallback_type="intro_previous_only",
                    fallback_source_scene_id=str(previous.get("scene_code") or ""),
                    fallback_reason="All remaining ordered intro queries failed; previous intro visual reused",
                    media_locked=False,
                )
            else:
                errors[scene_code] = local_errors
                raise RuntimeError(
                    f"{scene_code}: no intro query produced usable media. Documentary media was not reused. "
                    + "; ".join(local_errors[-4:])
                )
        resolved_rows.append(dict(chosen))
        errors[scene_code] = local_errors
        update_job(
            job_id, status="downloading_media", stage="Intro Media",
            progress=31 + int((scene_index + 1) / max(1, len(intro_scenes)) * 3),
            message=f"{scene_code}: resolved from ordered intro query {chosen.get('intro_query_order', '?')}",
        )

    report_path = job_dir / "intro_media_resolution.json"
    report_path.write_text(json.dumps({"worker_build": WORKER_BUILD, "scenes": resolved_rows, "errors": errors}, ensure_ascii=False, indent=2), encoding="utf-8")
    return resolved_rows



def _probe_video_duration(path: Path) -> float:
    probe = shutil.which("ffprobe")
    if not probe or not path.is_file():
        return 0.0
    try:
        result = subprocess.run(
            [probe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60, check=False,
        )
        return max(0.0, float((result.stdout or "0").strip() or 0.0)) if result.returncode == 0 else 0.0
    except Exception:
        return 0.0


def _intro_render_signature(settings: ProjectSettings, resolved_intro_scenes: list[dict[str, Any]]) -> str:
    return _json_hash({
        "profile": _technical_render_profile(settings),
        "intro_plan": dict((settings.metadata or {}).get("intro_plan") or {}),
        "scenes": [
            {**scene, "media_stat": _file_signature(str(scene.get("media_path") or ""))}
            for scene in resolved_intro_scenes
        ],
    })


def _intro_render_checkpoint_valid(job_dir: Path, signature: str, intro_path: Path) -> bool:
    path = _checkpoint_dir(job_dir) / "intro_rendered.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return bool(
        int(payload.get("version") or 0) == PIPELINE_CHECKPOINT_VERSION
        and payload.get("intro_render_signature") == signature
        and intro_path.is_file()
        and intro_path.stat().st_size >= 1024
        and _file_signature(str(intro_path)) == dict(payload.get("file_signature") or {})
    )


def _save_intro_render_checkpoint(job_dir: Path, signature: str, intro_path: Path) -> None:
    _atomic_json(_checkpoint_dir(job_dir) / "intro_rendered.json", {
        "version": PIPELINE_CHECKPOINT_VERSION,
        "stage": "intro_rendered",
        "intro_render_signature": signature,
        "file": str(intro_path),
        "file_signature": _file_signature(str(intro_path)),
        "completed_at": now_iso(),
    })


def _render_true_separate_intro(
    *,
    job_id: str,
    job_dir: Path,
    settings: ProjectSettings,
    resolved_main_scenes: list[dict[str, Any]],
    resolved_intro_scenes: list[dict[str, Any]],
    render_progress_callback,
    render_checkpoint_callback,
    reuse_checkpoints: bool,
) -> None:
    """Render intro and documentary as separate visual segments, then prepend.

    Documentary scene/caption timing remains unmodified in the authoritative plan.
    Final audio already contains exact intro silence before narration and a continuous
    music timeline.  The final two-segment pass shifts caption copies only for output.
    """
    output_root = job_dir / "output"
    intro_path = output_root / "intro_segment" / "intro.mp4"
    main_path = output_root / "main_segment" / "main.mp4"
    intro_path.parent.mkdir(parents=True, exist_ok=True)
    main_path.parent.mkdir(parents=True, exist_ok=True)

    intro_signature = _intro_render_signature(settings, resolved_intro_scenes)
    if not (reuse_checkpoints and _intro_render_checkpoint_valid(job_dir, intro_signature, intro_path)):
        intro_settings = copy.deepcopy(settings)
        intro_settings.output_path = str(intro_path)
        intro_settings.audio_path = ""
        intro_settings.render_audio_path = ""
        intro_settings.captions_enabled = False
        intro_settings.caption_mode = False
        intro_meta = dict(intro_settings.metadata or {})
        intro_visual_scenes = []
        for source in resolved_intro_scenes:
            row = dict(source); row["transition_sfx"] = "none"; row["sfx_volume"] = 0.0
            intro_visual_scenes.append(row)
        intro_meta["scene_sync_plan"] = intro_visual_scenes
        intro_meta["caption_segments"] = []
        intro_meta["resolved_sfx_plan"] = {"environment": [], "transition": [], "warnings": []}
        intro_settings.metadata = intro_meta
        render_mixed_media(
            intro_settings,
            progress_callback=lambda p, m: render_progress_callback(int(max(0, min(100, p)) * 0.20), "Intro: " + str(m)),
            cancel_callback=lambda: cancelled(job_id),
            checkpoint_callback=None,
        )
        _save_intro_render_checkpoint(job_dir, intro_signature, intro_path)
    else:
        render_progress_callback(20, "Reusing separately rendered intro checkpoint")

    main_settings = copy.deepcopy(settings)
    main_settings.output_path = str(main_path)
    main_settings.audio_path = ""
    main_settings.render_audio_path = ""
    main_settings.captions_enabled = False
    main_settings.caption_mode = False
    main_meta = dict(main_settings.metadata or {})
    main_visual_scenes = []
    for source in resolved_main_scenes:
        row = dict(source); row["transition_sfx"] = "none"; row["sfx_volume"] = 0.0
        main_visual_scenes.append(row)
    main_meta["scene_sync_plan"] = main_visual_scenes
    main_meta["caption_segments"] = []
    main_meta["resolved_sfx_plan"] = {"environment": [], "transition": [], "warnings": []}
    main_settings.metadata = main_meta
    render_mixed_media(
        main_settings,
        progress_callback=lambda p, m: render_progress_callback(20 + int(max(0, min(100, p)) * 0.60), "Documentary: " + str(m)),
        cancel_callback=lambda: cancelled(job_id),
        checkpoint_callback=render_checkpoint_callback,
    )

    intro_duration = _probe_video_duration(intro_path) or float((settings.metadata or {}).get("intro_plan", {}).get("duration_seconds") or getattr(settings, "intro_duration_seconds", 0) or 0)
    main_duration = _probe_video_duration(main_path) or sum(max(0.05, _scene_duration(x)) for x in resolved_main_scenes)
    if intro_duration <= 0 or main_duration <= 0:
        raise RuntimeError("Separate intro/main render produced invalid segment duration.")

    final_settings = copy.deepcopy(settings)
    final_settings.lut_enabled = False  # Color is already baked into both segment renders.
    final_settings.output_path = str(settings.output_path)
    final_meta = dict(final_settings.metadata or {})
    final_meta["scene_sync_plan"] = [
        {
            "scene_code": "FINAL_INTRO_SEGMENT", "media_type": "video", "media_path": str(intro_path),
            "start": 0.0, "end": intro_duration, "duration": intro_duration,
            "start_seconds": 0.0, "end_seconds": intro_duration, "duration_seconds": intro_duration,
            "transition_out": "hard_cut", "transition_duration_ms": 0, "transition_sfx": "none", "sfx_volume": 0.0,
            "filter": "none", "filter_intensity": 0.0, "effect": "none", "effect_intensity": 0.0,
            "motion": "none", "motion_intensity": 0.0, "overlay": "none", "overlay_intensity": 0.0,
        },
        {
            "scene_code": "FINAL_DOCUMENTARY_SEGMENT", "media_type": "video", "media_path": str(main_path),
            "start": intro_duration, "end": intro_duration + main_duration, "duration": main_duration,
            "start_seconds": intro_duration, "end_seconds": intro_duration + main_duration, "duration_seconds": main_duration,
            "transition_out": "none", "transition_duration_ms": 0, "transition_sfx": "none", "sfx_volume": 0.0,
            "filter": "none", "filter_intensity": 0.0, "effect": "none", "effect_intensity": 0.0,
            "motion": "none", "motion_intensity": 0.0, "overlay": "none", "overlay_intensity": 0.0,
        },
    ]
    # Caption source remains untouched in the authoritative metadata; only this
    # final-output copy is offset by the already-rendered intro duration.
    original_captions = [dict(x) for x in (settings.metadata or {}).get("caption_segments", []) if isinstance(x, dict)]
    final_meta["caption_segments"] = [_shift_time_fields(row, intro_duration) for row in original_captions]
    final_settings.metadata = final_meta
    render_mixed_media(
        final_settings,
        progress_callback=lambda p, m: render_progress_callback(80 + int(max(0, min(100, p)) * 0.20), "Final prepend/mux: " + str(m)),
        cancel_callback=lambda: cancelled(job_id),
        checkpoint_callback=None,
    )


def _resolved_narration_start(
    *,
    settings: ProjectSettings,
    audio_path: Path,
    audio_duration: float,
    caption_segments: list[dict[str, Any]],
    sync_engine: str,
    progress_callback=None,
    cancel_callback=None,
) -> tuple[float, str]:
    """Resolve the absolute speech start on the uploaded-audio timeline."""
    total = max(0.1, float(audio_duration or 0.1))
    mode = str(getattr(settings, "narration_start_mode", "auto") or "auto").strip().lower()
    if mode not in {"auto", "manual"}:
        mode = "auto"
    if mode == "manual":
        requested = max(0.0, float(getattr(settings, "narration_start_manual_seconds", 0.0) or 0.0))
        resolved = min(max(0.0, total - 0.05), requested)
        return (0.0 if resolved < 0.25 else resolved), "manual"

    whisper_based = "whisper" in str(sync_engine or "").lower() and "fallback" not in str(sync_engine or "").lower()
    starts = [
        max(0.0, float(row.get("start", 0.0) or 0.0))
        for row in caption_segments
        if isinstance(row, dict) and str(row.get("text", "") or "").strip()
    ]
    if whisper_based and starts:
        resolved = min(starts)
        return (0.0 if resolved < 0.25 else min(total - 0.05, resolved)), "whisper-caption"

    if audio_path.is_file():
        detected, source = detect_first_speech_start(
            audio_path=audio_path,
            language=str(getattr(settings, "caption_language", "auto") or "auto"),
            model_size=str(getattr(settings, "caption_whisper_model", "base") or "base"),
            progress_callback=progress_callback,
            cancel_callback=cancel_callback,
        )
        if detected is not None:
            resolved = max(0.0, min(total - 0.05, float(detected)))
            return (0.0 if resolved < 0.25 else resolved), source
        return 0.0, source
    return 0.0, "no-audio"


def _trim_captions_before_manual_narration_start(
    caption_segments: list[dict[str, Any]],
    narration_start: float,
) -> list[dict[str, Any]]:
    """Drop intro/music ASR before a deliberate manual speech boundary."""
    start_at = max(0.0, float(narration_start or 0.0))
    if start_at <= 0.0:
        return [dict(row) for row in caption_segments]
    cleaned: list[dict[str, Any]] = []
    for source in caption_segments:
        row = dict(source)
        start = max(0.0, float(row.get("start", 0.0) or 0.0))
        end = max(start, float(row.get("end", start) or start))
        if end <= start_at:
            continue
        row["start"] = max(start_at, start)
        if row["end"] > row["start"] and str(row.get("text", "") or "").strip():
            cleaned.append(row)
    return cleaned


def _embedded_audio_preroll_scenes(
    settings: ProjectSettings,
    narration_scenes: list[dict[str, Any]],
    narration_start: float,
) -> list[dict[str, Any]]:
    """Fill music-only uploaded-audio lead-in with intro-paced stock visuals.

    These scenes live on the *same* audio timeline; unlike the existing V17.4
    Opening Intro they do not prepend silence or shift the uploaded audio.
    """
    duration = max(0.0, float(narration_start or 0.0))
    metadata = dict(settings.metadata or {})
    if duration < 0.25:
        metadata["embedded_audio_preroll_seconds"] = 0.0
        metadata["embedded_audio_preroll_scene_count"] = 0
        settings.metadata = metadata
        return [dict(row) for row in narration_scenes]

    minimum = max(0.25, min(10.0, float(getattr(settings, "intro_media_min_seconds", 1.5) or 1.5)))
    maximum = max(minimum, min(15.0, float(getattr(settings, "intro_media_max_seconds", 3.0) or 3.0)))
    part_count = max(1, int(math.ceil(duration / maximum)))
    while part_count > 1 and duration / part_count < minimum:
        part_count -= 1

    query_rows = [dict(row) for row in metadata.get("intro_queries", []) if isinstance(row, dict)]
    fallback_queries: list[str] = []
    for row in narration_scenes[:6]:
        for key in ("primary_keyword", "keyword", "query"):
            value = " ".join(str(row.get(key, "") or "").split()).strip()
            if value and not value.upper().startswith("SCENE_") and value.casefold() not in {x.casefold() for x in fallback_queries}:
                fallback_queries.append(value)
        if not fallback_queries:
            text = " ".join(str(row.get("text", "") or "").split()).strip()
            if text:
                fallback_queries.append(" ".join(text.split()[:8]))
    if not fallback_queries:
        fallback_queries = ["cinematic opening establishing footage"]

    preroll: list[dict[str, Any]] = []
    cursor = 0.0
    for index in range(part_count):
        end = duration if index == part_count - 1 else duration * (index + 1) / part_count
        query_row = query_rows[index % len(query_rows)] if query_rows else {}
        query = " ".join(str(query_row.get("query") or query_row.get("keyword") or "").split()).strip()
        if not query:
            query = fallback_queries[index % len(fallback_queries)]
        row = {
            "scene_code": f"AUDIO_INTRO_{index + 1:03d}",
            "text": query,
            "visual_prompt": query,
            "primary_keyword": query,
            "keyword": query,
            "media_type": str(query_row.get("media_type") or "video").lower(),
            "allow_media_fallback": True,
            "start": round(cursor, 3), "end": round(end, 3), "duration": round(end - cursor, 3),
            "start_seconds": round(cursor, 3), "end_seconds": round(end, 3), "duration_seconds": round(end - cursor, 3),
            "matched_end": round(end, 3), "timing_validated": True,
            "timing_source": "uploaded_audio_preroll",
            "intro_scene": True, "embedded_audio_preroll": True,
            "media_duration_stage": "intro",
            "media_duration_min_seconds": round(minimum, 3),
            "media_duration_max_seconds": round(maximum, 3),
            "media_duration_min_unavoidable": bool((end - cursor) + 0.002 < minimum),
            "status": "missing_image", "approval_status": "approved",
            "tag": f"audio_intro_{index + 1:03d}",
            "pexels_selection_mode": str(getattr(settings, "pexels_selection_mode", "best_match_variety") or "best_match_variety"),
            "pexels_search_pool": int(getattr(settings, "pexels_search_pool", 10) or 10),
            "pexels_avoid_used_media": bool(getattr(settings, "pexels_avoid_used_media", True)),
            "pexels_avoid_same_creator": bool(getattr(settings, "pexels_avoid_same_creator", True)),
            "pexels_prefer_landscape": bool(getattr(settings, "pexels_prefer_landscape", True)),
            "pexels_prefer_video": bool(getattr(settings, "pexels_prefer_video", True)),
            "pexels_allow_video": bool(getattr(settings, "use_video_backgrounds", True)),
            "pexels_allow_image": bool(getattr(settings, "use_image_backgrounds", True)),
            "pexels_enabled": bool(getattr(settings, "pexels_enabled", True)),
            "transition_out": "hard_cut", "transition_duration_ms": 0,
            "transition_sfx": "none", "sfx_volume": 0.0,
        }
        preroll.append(row)
        cursor = end

    metadata["embedded_audio_preroll_seconds"] = round(duration, 3)
    metadata["embedded_audio_preroll_scene_count"] = len(preroll)
    metadata["uploaded_audio_master_timeline"] = True
    settings.metadata = metadata
    return preroll + [dict(row) for row in narration_scenes]


def _prepare_captions_and_timing(
    job_id: str,
    settings: ProjectSettings,
    scenes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    metadata = dict(settings.metadata or {})
    audio_path = Path(str(settings.audio_path or ""))
    audio_duration = get_audio_duration(str(audio_path)) if audio_path.is_file() else float(
        settings.duration_seconds or 0.0
    )
    caption_rows: list[dict[str, Any]] = []
    detected_language = "auto"
    sync_engine = "captions-disabled"

    if bool(settings.captions_enabled) and bool(settings.caption_mode):
        update_job(
            job_id,
            status="preparing",
            stage="Whisper",
            progress=3,
            message="Generating captions from narration audio",
        )

        def caption_progress(progress: int, message: str) -> None:
            if cancelled(job_id):
                raise RuntimeError("Remote render cancelled.")
            mapped = 3 + int(max(0, min(100, int(progress))) * 0.11)
            update_job(
                job_id,
                status="preparing",
                stage="Whisper",
                progress=min(14, mapped),
                message=str(message),
            )

        try:
            segments, detected_language, sync_engine = build_caption_segments(
                text=settings.text,
                audio_path=str(audio_path) if audio_path.is_file() else None,
                duration=max(0.1, audio_duration),
                sync_mode=str(settings.caption_sync_mode or "better"),
                language=str(settings.caption_language or "auto"),
                max_words=int(settings.caption_max_words or 8),
                offset=float(settings.caption_timing_offset or 0.0),
                text_source=str(settings.caption_text_source or "whisper"),
                prefer_script_language=bool(settings.caption_prefer_script_language),
                model_size=str(settings.caption_whisper_model or "base"),
                min_duration=(
                    float(settings.caption_min_duration or 0.0)
                    if bool(settings.caption_heavy_enabled)
                    else 0.0
                ),
                tighten_gaps=(
                    bool(settings.caption_gap_tighten)
                    if bool(settings.caption_heavy_enabled)
                    else False
                ),
                progress_callback=caption_progress,
                cancel_callback=lambda: cancelled(job_id),
            )
        except Exception as exc:
            raise RuntimeError(f"Caption generation failed: {exc}") from exc
        if not segments:
            raise RuntimeError(
                "Caption generation returned no usable segments; render was stopped."
            )
        caption_rows = [
            segment.to_dict() if hasattr(segment, "to_dict") else dict(segment)
            for segment in segments
        ]
    else:
        def caption_progress(progress: int, message: str) -> None:
            if cancelled(job_id):
                raise RuntimeError("Remote render cancelled.")
            mapped = 3 + int(max(0, min(100, int(progress))) * 0.05)
            update_job(job_id, status="preparing", stage="Narration Start", progress=min(8, mapped), message=str(message))

    narration_start, narration_start_source = _resolved_narration_start(
        settings=settings,
        audio_path=audio_path,
        audio_duration=max(0.1, audio_duration),
        caption_segments=caption_rows,
        sync_engine=sync_engine,
        progress_callback=caption_progress,
        cancel_callback=lambda: cancelled(job_id),
    )
    if str(getattr(settings, "narration_start_mode", "auto") or "auto").lower() == "manual" and caption_rows:
        caption_rows = _trim_captions_before_manual_narration_start(caption_rows, narration_start)
        if bool(settings.captions_enabled) and bool(settings.caption_mode) and not caption_rows:
            raise RuntimeError("Manual Narration Start is after all detected speech. Reduce the manual seconds value.")

    metadata["caption_segments"] = caption_rows if bool(settings.captions_enabled) and bool(settings.caption_mode) else []
    if caption_rows:
        metadata["caption_detected_language"] = str(detected_language)
        metadata["caption_sync_engine"] = str(sync_engine)
    metadata["narration_start_mode"] = str(getattr(settings, "narration_start_mode", "auto") or "auto").lower()
    metadata["narration_start_seconds"] = round(float(narration_start), 3)
    metadata["narration_start_source"] = str(narration_start_source)
    metadata["uploaded_audio_master_timeline"] = True
    settings.metadata = metadata

    if caption_rows:
        scenes = align_scenes_to_captions(
            scenes,
            caption_rows,
            max(0.1, audio_duration),
            timeline_start=narration_start,
        )
    elif narration_start > 0.0:
        scenes = align_scenes_to_captions(
            scenes,
            [],
            max(0.1, audio_duration),
            timeline_start=narration_start,
        )

    intro_duration_for_hook = float(getattr(settings, "intro_duration_seconds", 0) or 0) if bool(getattr(settings, "intro_enabled", False)) else 0.0
    narration_hook_seconds = max(0.0, AUDIT_WINDOW_SECONDS - intro_duration_for_hook)
    scenes = normalize_scene_timing(
        scenes,
        max(0.1, audio_duration),
        max_scene_seconds=float(getattr(settings, "general_media_max_seconds", settings.max_scene_seconds) or 4.0),
        min_scene_seconds=float(getattr(settings, "general_media_min_seconds", metadata.get("minimum_scene_seconds", 0.50)) or 0.50),
        hook_seconds=narration_hook_seconds,
        hook_min_scene_seconds=float(getattr(settings, "hook_media_min_seconds", 1.5) or 1.5),
        hook_max_scene_seconds=float(getattr(settings, "hook_media_max_seconds", 3.0) or 3.0),
        timeline_start=narration_start,
    )
    # A music-only lead-in belongs to the uploaded audio itself. Fill it with
    # Intro Media paced visuals on the same absolute timeline; do not prepend audio.
    scenes = _embedded_audio_preroll_scenes(settings, scenes, narration_start)
    metadata = dict(settings.metadata or {})
    metadata["caption_segments"] = caption_rows if bool(settings.captions_enabled) and bool(settings.caption_mode) else []
    metadata["narration_start_mode"] = str(getattr(settings, "narration_start_mode", "auto") or "auto").lower()
    metadata["narration_start_seconds"] = round(float(narration_start), 3)
    metadata["narration_start_source"] = str(narration_start_source)
    metadata["uploaded_audio_master_timeline"] = True

    invalid_timing = [
        str(scene.get("scene_code") or "unknown")
        for scene in scenes
        if not bool(scene.get("timing_validated"))
        or float(scene.get("end", 0.0) or 0.0) <= float(scene.get("start", 0.0) or 0.0)
    ]
    if invalid_timing:
        raise RuntimeError(
            "Scene timing preflight failed before media acquisition: "
            + ", ".join(invalid_timing[:20])
        )

    metadata["scene_sync_plan"] = [dict(scene) for scene in scenes]
    metadata["scene_sync_plan_authoritative"] = True
    metadata["worker_build"] = WORKER_BUILD
    settings.metadata = metadata
    return scenes


def _drive_remote_entries(destination: str) -> list[dict[str, Any]]:
    process = subprocess.run(
        ["rclone", "lsjson", destination, "--files-only", "--hash"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=120,
        check=False,
    )
    if process.returncode != 0:
        return []
    try:
        payload = json.loads(process.stdout or "[]")
    except ValueError:
        return []
    if isinstance(payload, dict):
        return [payload]
    return [dict(item) for item in payload if isinstance(item, dict)]


def _file_md5(path: Path) -> str:
    digest = hashlib.md5()  # nosec - compatibility check against Google Drive/rclone metadata
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest().lower()


def _drive_entry_matches_local(entry: dict[str, Any], output_file: Path) -> bool:
    try:
        if int(entry.get("Size") or -1) != int(output_file.stat().st_size):
            return False
    except OSError:
        return False
    if DRIVE_VERIFY_HASH:
        hashes = dict(entry.get("Hashes") or {})
        remote_md5 = str(hashes.get("MD5") or hashes.get("md5") or "").strip().lower()
        if remote_md5 and remote_md5 != _file_md5(output_file):
            return False
    return True


def _drive_view_url(entry: dict[str, Any]) -> str:
    file_id = str(entry.get("ID") or "")
    return f"https://drive.google.com/file/d/{file_id}/view" if file_id else ""


def _stop_upload_process(process: subprocess.Popen, force: bool = False) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name != "nt":
            os.killpg(os.getpgid(process.pid), signal.SIGKILL if force else signal.SIGTERM)
        elif force:
            process.kill()
        else:
            process.terminate()
    except Exception:
        try:
            process.kill() if force else process.terminate()
        except Exception:
            pass


def _run_cancellable_drive_upload(job_id: str, output_file: Path, output_filename: str, drive_folder: str, log_path: Path) -> None:
    environment = os.environ.copy()
    environment.update({"DRIVE_REMOTE": DRIVE_REMOTE, "DRIVE_FOLDER": drive_folder, "DRIVE_TRANSFER_MODE":"copy", "OUTPUT_FILENAME": output_filename})
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log_handle:
        process = subprocess.Popen(
            [str(DRIVE_SCRIPT), str(output_file)],
            env=environment,
            text=True,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=(os.name != "nt"),
        )
        started = time.monotonic()
        last_activity = started
        last_size = -1
        while process.poll() is None:
            if cancelled(job_id):
                _stop_upload_process(process, force=False)
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    _stop_upload_process(process, force=True)
                    process.wait(timeout=10)
                raise RuntimeError("Remote render cancelled.")
            elapsed = time.monotonic() - started
            if elapsed > DRIVE_UPLOAD_TIMEOUT_SECONDS:
                _stop_upload_process(process, force=False)
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    _stop_upload_process(process, force=True)
                    process.wait(timeout=10)
                raise RuntimeError("Google Drive upload exceeded the safety timeout; local render preserved")
            try:
                current_size = log_path.stat().st_size
            except OSError:
                current_size = last_size
            if current_size != last_size:
                last_size = current_size
                last_activity = time.monotonic()
            elif time.monotonic() - last_activity > DRIVE_UPLOAD_STALL_SECONDS:
                _stop_upload_process(process, force=False)
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    _stop_upload_process(process, force=True)
                    process.wait(timeout=10)
                raise RuntimeError("Google Drive upload stalled; local render preserved")
            time.sleep(1.0)
        if process.returncode != 0:
            raise RuntimeError(f"Google Drive upload failed; local render preserved. See {log_path.name}.")


def upload_output_to_drive(
    job_id: str,
    project_name: str,
    output_file: Path,
    channel_name: str = "",
) -> tuple[str, str, str]:
    if not DRIVE_UPLOAD_ENABLED:
        return "", "", ""
    if not DRIVE_SCRIPT.exists():
        raise RuntimeError(f"Drive transfer script is missing: {DRIVE_SCRIPT}")
    output_filename = drive_output_filename(project_name)
    drive_folder = _drive_folder_for_channel(channel_name)
    destination = f"{DRIVE_REMOTE}:{drive_folder}/{output_filename}"

    # Crash-safe reconciliation: if a previous worker run already uploaded this
    # exact file but crashed before SQLite reached Completed, accept the verified
    # remote copy instead of treating it as a name collision.
    existing_entries = _drive_remote_entries(destination)
    if existing_entries:
        entry = existing_entries[0]
        if _drive_entry_matches_local(entry, output_file):
            drive_view_url = _drive_view_url(entry)
            update_job(
                job_id,
                status="drive_uploading",
                stage="Drive Verification",
                progress=99,
                message=f"Verified existing Drive copy after resume: {destination}",
                drive_status="completed",
                drive_destination=destination,
                drive_view_url=drive_view_url,
                output_filename=output_filename,
            )
            if DRIVE_TRANSFER_MODE == "move" and DELETE_OUTPUT_ONLY_AFTER_DRIVE_UPLOAD and not cancelled(job_id):
                try:
                    output_file.unlink(missing_ok=True)
                except OSError:
                    pass
            return destination, drive_view_url, output_filename
        raise RuntimeError("A Google Drive output with this project name already exists but does not match the local render: " + destination)

    update_job(
        job_id,
        status="drive_uploading",
        stage="Drive Uploading",
        progress=99,
        message=f"Uploading completed video to {destination}",
        drive_status="uploading",
        drive_destination=destination,
        output_filename=output_filename,
    )
    log_path = JOBS / job_id / "drive_upload.log"
    try:
        _run_cancellable_drive_upload(job_id, output_file, output_filename, drive_folder, log_path)
    except Exception:
        if cancelled(job_id):
            update_job(job_id, drive_status="cancelled", message="Drive upload cancelled; local render preserved")
        else:
            update_job(job_id, drive_status="failed", message=f"Google Drive upload failed; local render preserved. See {log_path.name}.")
        raise

    entries = _drive_remote_entries(destination)
    if not entries:
        update_job(job_id, drive_status="failed", message="Drive upload returned success but remote verification failed; local render preserved")
        raise RuntimeError("Google Drive verification failed; local render preserved")
    entry = entries[0]
    if not _drive_entry_matches_local(entry, output_file):
        update_job(job_id, drive_status="failed", message="Drive verification size/hash mismatch; local render preserved")
        raise RuntimeError("Google Drive verification size/hash mismatch; local render preserved")
    drive_view_url = _drive_view_url(entry)
    update_job(
        job_id,
        drive_status="completed",
        drive_destination=destination,
        drive_view_url=drive_view_url,
        output_filename=output_filename,
    )
    if DRIVE_TRANSFER_MODE == "move" and DELETE_OUTPUT_ONLY_AFTER_DRIVE_UPLOAD and not cancelled(job_id):
        try:
            output_file.unlink(missing_ok=True)
        except OSError:
            pass
    return destination, drive_view_url, output_filename


def _attempt_public_drive_viewer(job_id: str, destination: str, current_url: str) -> tuple[str, str]:
    """Best-effort `Anyone with the link / Viewer` request.

    For Google Drive, `rclone link` creates/returns an unauthenticated public link
    when the remote policy permits it.  V17.4 intentionally does not perform a
    second viewer-access verification: a permission failure is logged and never
    blocks storage-first per-project cleanup after the verified upload.
    """
    if not destination:
        return current_url, ""
    try:
        result = subprocess.run(
            ["rclone", "link", destination],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            timeout=120, check=False,
        )
        link = (result.stdout or "").strip().splitlines()
        if result.returncode == 0 and link:
            candidate = link[-1].strip()
            if candidate.startswith("http://") or candidate.startswith("https://"):
                update_job(job_id, drive_view_url=candidate)
                return candidate, ""
        warning = "Drive viewer permission/link request was not applied: " + ((result.stdout or "unknown rclone error").strip()[-700:])
        return current_url, warning
    except Exception as exc:
        return current_url, f"Drive viewer permission/link request failed: {exc}"


def _auto_clean_delivered_project(job_id: str, *, viewer_warning: str = "") -> bool:
    """Delete every project-owned local byte after a verified Drive delivery.

    The SQLite queue/web record lives outside the job directory and survives.
    Permanent Music/SFX libraries also live outside JOBS and are never touched.
    """
    job_dir = JOBS / job_id
    try:
        if job_dir.exists():
            shutil.rmtree(job_dir)
        if job_dir.exists():
            raise RuntimeError(f"project directory still exists after cleanup: {job_dir}")
        message = "Drive delivery verified; all local project data removed"
        if viewer_warning:
            message += " · " + viewer_warning
        update_job(
            job_id, status="cleaned", stage="Cleaned", progress=100, message=message,
            output_file="", size_bytes=0, files_cleaned_at=now_iso(),
            resume_mode="", manual_start_required=0,
        )
        return True
    except Exception as exc:
        # Keep the completed state when deletion cannot be verified so operators can
        # see it. Normal mode may continue; Verified Serial deliberately retains its
        # persistent owner until an operator retries cleanup or resolves the block.
        message = f"Drive delivery verified; automatic local cleanup failed: {exc}"
        if viewer_warning:
            message += " · " + viewer_warning
        update_job(job_id, status="completed", stage="Completed", progress=100, message=message)
        return False


def _compress_output_if_enabled(job_id: str, source: Path) -> Path:
    if not COMPRESSOR_ENABLED:
        return source
    compressed = source.with_name(source.stem + ".compressed.mp4")
    update_job(job_id, stage="Compressing", progress=99, message=f"Compressing final video (CRF {COMPRESS_CRF})")
    process = subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(source), "-c:v", "libx264", "-preset", "veryfast",
            "-crf", str(COMPRESS_CRF), "-c:a", "aac", "-b:a", "160k",
            "-movflags", "+faststart", str(compressed),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=24 * 60 * 60,
        check=False,
    )
    if process.returncode != 0 or not compressed.exists() or compressed.stat().st_size < 1024:
        try:
            compressed.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError("Compressor failed: " + (process.stdout or "unknown ffmpeg error")[-1000:])
    if compressed.stat().st_size < source.stat().st_size:
        source.unlink(missing_ok=True)
        compressed.replace(source)
    else:
        compressed.unlink(missing_ok=True)
    return source


def _cleanup_transient_job_media(job_id: str) -> None:
    """Delete reconstructable per-project media after a successful render.

    Final output, queue DB, error/audit logs and Drive state are retained. V17.4
    Pexels downloads are project-scoped; there is no reusable shared Pexels cache.
    """
    if not AUTO_CLEAN_MEDIA_AFTER_RENDER:
        return
    job_dir = JOBS / job_id
    for folder_name in ("resolved_media", "resolved_intro_media", "pexels_provider_cache", "audit_media", "audit_upload_tmp", "cache", "temp", "tmp", "output/render_checkpoint", "output/intro_segment", "output/main_segment", "output/_acs_temp"):
        shutil.rmtree(job_dir / folder_name, ignore_errors=True)
    # Uploaded package/extracted assets are no longer needed once final.mp4 is safe.
    shutil.rmtree(job_dir / "input", ignore_errors=True)
    try:
        (job_dir / "input.zip").unlink(missing_ok=True)
        (job_dir / "project.zip").unlink(missing_ok=True)
        (job_dir / "audit_first2.json").unlink(missing_ok=True)
    except OSError:
        pass
    update_job(job_id, files_cleaned_at=now_iso())


def _load_saved_media(job_dir: Path, timed_scenes: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """Return a validated saved media plan while accepting freshly generated timing."""
    report_path = job_dir / "media_resolution.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        saved = [dict(item) for item in report.get("scenes", []) if isinstance(item, dict)]
    except Exception:
        return None
    if len(saved) != len(timed_scenes) or not saved:
        return None
    timing_keys = {
        "start", "end", "duration", "start_time", "end_time", "start_seconds",
        "end_seconds", "duration_seconds", "caption_start", "caption_end",
    }
    merged: list[dict[str, Any]] = []
    for index, (old, fresh) in enumerate(zip(saved, timed_scenes)):
        old_code = str(old.get("scene_code") or f"SCENE_{index + 1:03d}")
        new_code = str(fresh.get("scene_code") or f"SCENE_{index + 1:03d}")
        media_path = Path(str(old.get("media_path") or ""))
        if old_code != new_code or not media_path.is_file() or media_path.stat().st_size < 1024:
            return None
        try:
            validate_media(media_path, str(old.get("media_type") or "video").lower(), max(0.05, _scene_duration(fresh)), require_landscape=bool(old.get("pexels_prefer_landscape", True)))
        except Exception:
            return None
        item = dict(old)
        for key in timing_keys:
            if key in fresh:
                item[key] = fresh[key]
        merged.append(item)
    return merged



PIPELINE_CHECKPOINT_VERSION = 20
PIPELINE_CHECKPOINT_DIR = "pipeline_checkpoints"


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


def _json_hash(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


def _file_signature(path_value: str) -> dict[str, Any]:
    path = Path(str(path_value or ""))
    try:
        stat = path.stat()
        return {"path": str(path), "size": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns)}
    except OSError:
        return {"path": str(path), "size": 0, "mtime_ns": 0}


def _pipeline_source_signature(settings: ProjectSettings, scenes: list[dict[str, Any]]) -> str:
    keys = (
        "captions_enabled", "caption_mode", "caption_sync_mode", "caption_language",
        "caption_max_words", "caption_timing_offset", "caption_text_source",
        "caption_prefer_script_language", "caption_whisper_model", "caption_heavy_enabled",
        "caption_min_duration", "caption_gap_tighten", "max_scene_seconds",
        "intro_media_min_seconds", "intro_media_max_seconds", "hook_media_min_seconds", "hook_media_max_seconds",
        "general_media_min_seconds", "general_media_max_seconds", "narration_start_mode", "narration_start_manual_seconds", "pexels_selection_mode", "pexels_search_pool",
        "pexels_avoid_used_media", "pexels_avoid_same_creator", "pexels_prefer_landscape", "pexels_prefer_video",
        "transition_engine_enabled", "transition_engine_mode", "transition_preset",
        "transition_intensity_percent", "transition_base_seconds", "transition_hook_cut_bias",
        "opening_hook_seconds", "transition_no_repeat", "intro_enabled", "intro_duration_seconds",
        "first_two_minute_visual_audit_enabled",
        "music_enabled", "music_assigned_path", "music_playlist", "music_ai_select_from_library",
        "music_ai_allowed_categories", "music_use_track1_for_intro", "music_allow_intro_track_in_main",
        "music_playlist_loop", "music_track_crossfade_seconds",
        "music_volume_percent", "music_ducking_enabled", "music_ducking_strength",
        "sfx_enabled", "environment_sfx_enabled", "transition_sfx_library_enabled", "sfx_amount",
        "environment_sfx_volume_percent", "transition_sfx_volume_percent", "auto_clean_project_after_drive",
        "voice_processing_preset", "voice_volume_percent", "voice_speed_percent", "extra_end_seconds",
        "width", "height", "fps", "quality_crf", "video_quality_preset", "lut_enabled",
        "lut_mode", "lut_pack", "lut_preset", "lut_strength_percent", "lut_scene_aware_enabled",
        "lut_continuity_enabled", "render_chunk_scenes",
    )
    payload = {
        "version": PIPELINE_CHECKPOINT_VERSION,
        "scenes": scenes,
        "settings": {key: getattr(settings, key, None) for key in keys},
        "audio": _file_signature(str(getattr(settings, "audio_path", "") or "")),
        "music": _file_signature(str(getattr(settings, "music_assigned_path", "") or "")),
        "intro_queries": list((settings.metadata or {}).get("intro_queries", []) or []),
        "permanent_library": _file_signature(str(PERMANENT_ROOT / "library.json")),
    }
    return _json_hash(payload)


def _checkpoint_dir(job_dir: Path) -> Path:
    root = job_dir / PIPELINE_CHECKPOINT_DIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def _caption_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        key: metadata.get(key)
        for key in (
            "caption_segments", "caption_detected_language", "caption_sync_engine",
            "narration_start_mode", "narration_start_seconds", "narration_start_source",
            "embedded_audio_preroll_seconds", "embedded_audio_preroll_scene_count", "uploaded_audio_master_timeline",
            "scene_sync_plan_authoritative", "worker_build",
        )
        if key in metadata
    }


def _save_timing_checkpoint(job_dir: Path, source_signature: str, settings: ProjectSettings, scenes: list[dict[str, Any]]) -> None:
    _atomic_json(
        _checkpoint_dir(job_dir) / "timing.json",
        {
            "version": PIPELINE_CHECKPOINT_VERSION,
            "source_signature": source_signature,
            "stage": "whisper_timing",
            "scenes": scenes,
            "metadata": _caption_metadata(dict(settings.metadata or {})),
            "completed_at": now_iso(),
        },
    )


def _load_timing_checkpoint(job_dir: Path, source_signature: str, settings: ProjectSettings) -> list[dict[str, Any]] | None:
    path = _checkpoint_dir(job_dir) / "timing.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if int(payload.get("version") or 0) != PIPELINE_CHECKPOINT_VERSION or payload.get("source_signature") != source_signature:
        return None
    scenes = [dict(item) for item in payload.get("scenes", []) if isinstance(item, dict)]
    if not scenes or any(float(scene.get("end", 0) or 0) <= float(scene.get("start", 0) or 0) for scene in scenes):
        return None
    metadata = dict(settings.metadata or {})
    metadata.update(dict(payload.get("metadata") or {}))
    metadata["scene_sync_plan"] = scenes
    metadata["scene_sync_plan_authoritative"] = True
    settings.metadata = metadata
    return scenes


def _edit_signature(source_signature: str, settings: ProjectSettings, scenes: list[dict[str, Any]]) -> str:
    return _json_hash({
        "source_signature": source_signature,
        "scenes": scenes,
        "transition": {
            "enabled": bool(settings.transition_engine_enabled),
            "mode": str(settings.transition_engine_mode or "auto_editorial"),
            "preset": str(settings.transition_preset or "documentary"),
            "intensity": int(settings.transition_intensity_percent or 0),
            "base_seconds": float(settings.transition_base_seconds or 0.28),
            "hook": bool(settings.transition_hook_cut_bias),
            "hook_seconds": float(settings.opening_hook_seconds or 0.0),
            "no_repeat": bool(settings.transition_no_repeat),
        },
        "motion_style": str(getattr(settings, "visual_motion_style", "cinematic") or "cinematic"),
        "video_quality_preset": str(getattr(settings, "video_quality_preset", "enhanced") or "enhanced"),
    })


def _save_edit_checkpoint(job_dir: Path, signature: str, scenes: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    _atomic_json(
        _checkpoint_dir(job_dir) / "edit_direction.json",
        {
            "version": PIPELINE_CHECKPOINT_VERSION,
            "edit_signature": signature,
            "stage": "edit_direction",
            "scenes": scenes,
            "transition_summary": summary,
            "completed_at": now_iso(),
        },
    )


def _load_edit_checkpoint(job_dir: Path, signature: str) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
    path = _checkpoint_dir(job_dir) / "edit_direction.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if int(payload.get("version") or 0) != PIPELINE_CHECKPOINT_VERSION or payload.get("edit_signature") != signature:
        return None
    scenes = [dict(item) for item in payload.get("scenes", []) if isinstance(item, dict)]
    if not scenes:
        return None
    return scenes, dict(payload.get("transition_summary") or {})


def _audio_signature(source_signature: str, settings: ProjectSettings) -> str:
    playlist = [dict(x) for x in (settings.metadata or {}).get("resolved_music_playlist", []) if isinstance(x, dict)]
    return _json_hash({
        "source_signature": source_signature,
        "audio": _file_signature(str(settings.audio_path or "")),
        "music": [_file_signature(str(x.get("path") or "")) for x in playlist],
        "music_playlist": playlist,
        "music_enabled": bool(settings.music_enabled),
        "intro_enabled": bool(getattr(settings, "intro_enabled", False)),
        "intro_duration_seconds": int(getattr(settings, "intro_duration_seconds", 0) or 0),
        "music_use_track1_for_intro": bool(getattr(settings, "music_use_track1_for_intro", True)),
        "music_allow_intro_track_in_main": bool(getattr(settings, "music_allow_intro_track_in_main", False)),
        "music_playlist_loop": bool(getattr(settings, "music_playlist_loop", True)),
        "music_track_crossfade_seconds": float(getattr(settings, "music_track_crossfade_seconds", 1.5) or 0.0),
        "music_volume_percent": int(settings.music_volume_percent or 0),
        "ducking": bool(settings.music_ducking_enabled),
        "ducking_strength": str(settings.music_ducking_strength or "medium"),
        "voice_processing": str(settings.voice_processing_preset or "off"),
        "voice_volume": int(settings.voice_volume_percent or 0),
        "voice_speed": int(settings.voice_speed_percent or 100),
        "extra_end_seconds": float(settings.extra_end_seconds or 0.0),
    })


def _save_audio_checkpoint(job_dir: Path, signature: str, settings: ProjectSettings) -> None:
    render_audio = str(settings.render_audio_path or "")
    _atomic_json(
        _checkpoint_dir(job_dir) / "audio_mix.json",
        {
            "version": PIPELINE_CHECKPOINT_VERSION,
            "audio_signature": signature,
            "stage": "audio_mix",
            "render_audio_path": render_audio,
            "audio_mix": dict((settings.metadata or {}).get("audio_mix") or {}),
            "completed_at": now_iso(),
        },
    )


def _load_audio_checkpoint(job_dir: Path, signature: str, settings: ProjectSettings, needs_audio_mix: bool) -> bool:
    if not needs_audio_mix:
        settings.render_audio_path = ""
        return True
    path = _checkpoint_dir(job_dir) / "audio_mix.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if int(payload.get("version") or 0) != PIPELINE_CHECKPOINT_VERSION or payload.get("audio_signature") != signature:
        return False
    render_audio = Path(str(payload.get("render_audio_path") or ""))
    if not render_audio.is_file() or render_audio.stat().st_size < 1024:
        return False
    settings.render_audio_path = str(render_audio)
    metadata = dict(settings.metadata or {})
    metadata["audio_mix"] = dict(payload.get("audio_mix") or {})
    settings.metadata = metadata
    return True



def _resolve_editorial_scene_technical_fields(scenes: list[dict[str, Any]], settings: ProjectSettings) -> list[dict[str, Any]]:
    """Resolve V17 editorial intent into bounded technical scene instructions.

    Legacy technical plans remain compatible. V17 editorial plans intentionally do
    not carry FFmpeg-level filter/motion/overlay values; those come from settings.
    """
    style = str(getattr(settings, "visual_motion_style", "cinematic") or "cinematic").lower()
    motion_map = {
        "very_slow": ("zoom_in", 0.18),
        "cinematic": ("ken_burns", 0.32),
        "dynamic": ("zoom_in", 0.52),
    }
    resolved: list[dict[str, Any]] = []
    for index, original in enumerate(scenes):
        scene = dict(original)
        if str(scene.get("outer_ai_source") or "") == "structured_txt_v17_editorial":
            requested_motion, intensity = motion_map.get(style, motion_map["cinematic"])
            if str(scene.get("media_type") or "image").lower() == "image":
                # Alternate direction deterministically to avoid repetitive push-in motion.
                if style == "dynamic" and index % 2:
                    requested_motion = "zoom_out"
                scene["motion"] = requested_motion
                scene["motion_intensity"] = intensity
            else:
                scene["motion"] = "none"
                scene["motion_intensity"] = 0.0
            # Color is owned by the preset/LUT policy; Outer AI supplies visual_mood.
            scene["filter"] = "none"
            scene["filter_intensity"] = 0.0
            # Keep effect_intent as editorial metadata. Heavy scene effects are not
            # injected automatically unless a future preset explicitly enables them.
            scene["effect"] = "none"
            scene["effect_intensity"] = 0.0
            scene["overlay"] = "none"
            scene["overlay_intensity"] = 0.0
            scene["technical_resolution_source"] = "preset_settings_v17"
        resolved.append(scene)
    return resolved


def _technical_render_profile(settings: ProjectSettings) -> dict[str, Any]:
    keys = (
        "width", "height", "fps", "quality_crf", "video_encoder", "video_quality_preset",
        "render_chunk_scenes", "lut_enabled", "lut_mode", "lut_pack", "lut_preset",
        "lut_strength_percent", "lut_scene_aware_enabled", "lut_continuity_enabled",
        "captions_enabled", "caption_style", "caption_position", "font_family", "font_size",
        "transition_engine_mode", "transition_preset", "transition_intensity_percent",
    )
    return {key: getattr(settings, key, None) for key in keys}


def _save_resolved_job_plan(job_dir: Path, source_signature: str, settings: ProjectSettings, scenes: list[dict[str, Any]]) -> Path:
    metadata = dict(settings.metadata or {})
    path = job_dir / "resolved_job_plan.json"
    payload = {
        "schema": "scene-builder-resolved-plan-v17.4",
        "version": PIPELINE_CHECKPOINT_VERSION,
        "worker_build": WORKER_BUILD,
        "source_signature": source_signature,
        "authority": {
            "outer_ai": "editorial_intent_and_media_planning",
            "preset_settings": "technical_editing_rules",
            "resolver": "single_resolution_before_render",
            "renderer": "resolved_job_plan_only",
        },
        "render_audio_path": str(settings.render_audio_path or ""),
        "technical_profile": _technical_render_profile(settings),
        "metadata": {
            **_caption_metadata(metadata),
            "audio_mix": dict(metadata.get("audio_mix") or {}),
            "intro_plan": dict(metadata.get("intro_plan") or {}),
            "intro_scenes": list(metadata.get("intro_scenes", []) or []),
            "resolved_intro_scenes": list(metadata.get("resolved_intro_scenes", []) or []),
            "resolved_music_playlist": list(metadata.get("resolved_music_playlist", []) or []),
            "resolved_sfx_plan": dict(metadata.get("resolved_sfx_plan") or {}),
            "audio_warnings": list(metadata.get("audio_warnings", []) or []),
            "transition_direction": dict(metadata.get("transition_direction") or {}),
        },
        "scenes": scenes,
        "frozen_at": now_iso(),
    }
    payload["plan_fingerprint"] = _json_hash({
        "source_signature": source_signature,
        "technical_profile": payload["technical_profile"],
        "render_audio": _file_signature(payload["render_audio_path"]),
        "scenes": [
            {**scene, "media_stat": _file_signature(str(scene.get("media_path") or ""))}
            for scene in scenes
        ],
        "intro_scenes": [
            {**scene, "media_stat": _file_signature(str(scene.get("media_path") or ""))}
            for scene in metadata.get("resolved_intro_scenes", []) if isinstance(scene, dict)
        ],
    })
    _atomic_json(path, payload)
    return path


def _load_resolved_job_plan(job_dir: Path, source_signature: str, settings: ProjectSettings) -> list[dict[str, Any]] | None:
    path = job_dir / "resolved_job_plan.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if int(payload.get("version") or 0) != PIPELINE_CHECKPOINT_VERSION or payload.get("source_signature") != source_signature:
        return None
    scenes = [dict(item) for item in payload.get("scenes", []) if isinstance(item, dict)]
    if not scenes:
        return None
    expected_fingerprint = str(payload.get("plan_fingerprint") or "")
    actual_fingerprint = _json_hash({
        "source_signature": source_signature,
        "technical_profile": dict(payload.get("technical_profile") or {}),
        "render_audio": _file_signature(str(payload.get("render_audio_path") or "")),
        "scenes": [
            {**scene, "media_stat": _file_signature(str(scene.get("media_path") or ""))}
            for scene in scenes
        ],
        "intro_scenes": [
            {**scene, "media_stat": _file_signature(str(scene.get("media_path") or ""))}
            for scene in (payload.get("metadata") or {}).get("resolved_intro_scenes", []) if isinstance(scene, dict)
        ],
    })
    if not expected_fingerprint or not hmac.compare_digest(expected_fingerprint, actual_fingerprint):
        return None
    for scene in scenes:
        media_path = Path(str(scene.get("media_path") or ""))
        if not media_path.is_file() or media_path.stat().st_size < 1024:
            return None
        try:
            validate_media(media_path, str(scene.get("media_type") or "video").lower(), max(0.05, _scene_duration(scene)), require_landscape=bool(scene.get("pexels_prefer_landscape", True)))
        except Exception:
            return None
        logical = str(scene.get("transition_out") or "none").strip().lower()
        if logical in {"none", "hard_cut", "cut"}:
            scene["transition_duration_ms"] = 0
            scene["transition_xfade"] = None
            scene["transition_variant"] = None
            scene["transition_engine"] = "cut"
    for intro_scene in (payload.get("metadata") or {}).get("resolved_intro_scenes", []):
        if not isinstance(intro_scene, dict):
            continue
        media_path = Path(str(intro_scene.get("media_path") or ""))
        if not media_path.is_file() or media_path.stat().st_size < 1024:
            return None
        try:
            validate_media(media_path, str(intro_scene.get("media_type") or "video").lower(), max(0.05, _scene_duration(intro_scene)), require_landscape=bool(intro_scene.get("pexels_prefer_landscape", True)))
        except Exception:
            return None

    render_audio = str(payload.get("render_audio_path") or "")
    if render_audio and not Path(render_audio).is_file():
        return None
    settings.render_audio_path = render_audio
    metadata = dict(settings.metadata or {})
    metadata.update(dict(payload.get("metadata") or {}))
    metadata["scene_sync_plan"] = scenes
    metadata["scene_sync_plan_authoritative"] = True
    metadata["resolved_job_plan"] = str(path)
    metadata["resolved_job_plan_frozen"] = True
    settings.metadata = metadata
    return scenes



def _intro_plan_signature(source_signature: str, settings: ProjectSettings) -> str:
    metadata = dict(settings.metadata or {})
    return _json_hash({
        "source_signature": source_signature,
        "enabled": bool(getattr(settings, "intro_enabled", False)),
        "duration": int(getattr(settings, "intro_duration_seconds", 0) or 0),
        "queries": list(metadata.get("intro_queries", []) or []),
        "intro_media_min_seconds": float(getattr(settings, "intro_media_min_seconds", 1.5) or 1.5),
        "intro_media_max_seconds": float(getattr(settings, "intro_media_max_seconds", 3.0) or 3.0),
    })


def _shift_time_fields(row: dict[str, Any], offset: float) -> dict[str, Any]:
    """Return a shifted COPY for final-output timing without mutating source timing."""
    result = dict(row)
    for key in ("start", "end", "start_time", "end_time", "start_seconds", "end_seconds", "caption_start", "caption_end"):
        if key in result:
            try:
                result[key] = float(result[key] or 0.0) + offset
            except Exception:
                pass
    return result


def _planned_intro_scenes(settings: ProjectSettings) -> list[dict[str, Any]]:
    """Create only the pre-narration intro segment.

    V17.4 final-compliance rule: documentary/Whisper timing remains untouched.
    Intro shots live in metadata and are rendered/checkpointed separately.
    """
    if not bool(getattr(settings, "intro_enabled", False)):
        metadata = dict(settings.metadata or {})
        metadata["intro_scenes"] = []
        metadata["intro_plan"] = {"enabled": False, "duration_seconds": 0.0, "scene_count": 0}
        settings.metadata = metadata
        return []
    duration = float(max(1, min(600, int(getattr(settings, "intro_duration_seconds", 15) or 15))))
    metadata = dict(settings.metadata or {})
    queries = [dict(x) for x in metadata.get("intro_queries", []) if isinstance(x, dict) and str(x.get("query") or "").strip()]
    if not queries:
        raise RuntimeError("Opening Intro is enabled, but the Scene Plan contains no INTRO_QUERY rows.")

    min_shot = max(0.25, min(10.0, float(getattr(settings, "intro_media_min_seconds", 1.5) or 1.5)))
    max_shot = max(min_shot, min(15.0, float(getattr(settings, "intro_media_max_seconds", 3.0) or 3.0)))
    # Choose a count that keeps every intro shot inside the configured range whenever mathematically possible.
    shot_count = max(1, int((duration + max_shot - 1e-9) // max_shot))
    if shot_count * max_shot < duration - 1e-6:
        shot_count += 1
    while shot_count > 1 and duration / shot_count < min_shot:
        shot_count -= 1
    target_shot = duration / max(1, shot_count)
    intro: list[dict[str, Any]] = []
    cursor = 0.0
    index = 0
    while cursor < duration - 0.001:
        remaining = duration - cursor
        remaining_slots = max(1, shot_count - index)
        shot = min(max_shot, max(0.05, remaining / remaining_slots if remaining_slots else target_shot))
        query_row = queries[index % len(queries)]
        query = str(query_row.get("query") or "").strip()
        end = min(duration, cursor + max(0.05, shot))
        backups = [str(x).strip() for x in query_row.get("backup_keywords", []) if str(x).strip()][:3]
        intro.append({
            "scene_code": f"INTRO_{index + 1:03d}",
            "text": "", "visual_prompt": query,
            "media_type": str(query_row.get("media_type") or "video").lower() if str(query_row.get("media_type") or "video").lower() in {"image","video"} else "video",
            "primary_keyword": query, "keyword": query,
            "backup_keywords": backups, "fallback_keywords": backups,
            "visual_direction": "opening intro cinematic establishing footage",
            "allow_media_fallback": True,
            "start": round(cursor, 6), "end": round(end, 6), "duration": round(end - cursor, 6),
            "start_seconds": round(cursor, 6), "end_seconds": round(end, 6), "duration_seconds": round(end - cursor, 6),
            "timing_validated": True, "intro_scene": True,
            "media_duration_stage": "intro",
            "media_duration_min_seconds": round(min_shot, 3),
            "media_duration_max_seconds": round(max_shot, 3),
            "pexels_selection_mode": str(getattr(settings, "pexels_selection_mode", "best_match_variety") or "best_match_variety"),
            "pexels_search_pool": int(getattr(settings, "pexels_search_pool", 10) or 10),
            "pexels_avoid_used_media": bool(getattr(settings, "pexels_avoid_used_media", True)),
            "pexels_avoid_same_creator": bool(getattr(settings, "pexels_avoid_same_creator", True)),
            "pexels_prefer_landscape": bool(getattr(settings, "pexels_prefer_landscape", True)),
            "pexels_prefer_video": bool(getattr(settings, "pexels_prefer_video", True)),
            "intro_query_order": int(query_row.get("order") or (index + 1)),
            "intro_query_reused": index >= len(queries),
            "environment_sfx": str(query_row.get("environment_sfx") or "none"),
            "transition_out": "hard_cut", "transition_duration_ms": 0,
            "filter": "none", "filter_intensity": 0.0, "effect": "none", "effect_intensity": 0.0,
            "motion": "none", "motion_intensity": 0.0, "overlay": "none", "overlay_intensity": 0.0,
            "transition_sfx": "none", "sfx_volume": 0.0,
            "outer_ai_source": "intro_query_v174",
        })
        cursor = end
        index += 1
        if index > 500:
            raise RuntimeError("Intro planner exceeded its safety limit.")

    metadata["intro_scenes"] = intro
    metadata["intro_plan"] = {
        "enabled": True, "duration_seconds": duration,
        "scene_count": len(intro), "queries_consumed": min(len(queries), len(intro)),
        "query_rows_available": len(queries),
        "architecture": "separate_pre_narration_segment",
        "documentary_timing_shifted": False,
    }
    settings.metadata = metadata
    return intro


def _build_intro_scenes(settings: ProjectSettings, narration_scenes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Backward-compatible wrapper; documentary scenes are intentionally unchanged."""
    _planned_intro_scenes(settings)
    return [dict(scene) for scene in narration_scenes]


def _final_timeline_scenes(settings: ProjectSettings, narration_scenes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build a render-only final-timeline copy for SFX/caption event timing."""
    metadata = dict(settings.metadata or {})
    intro_source = metadata.get("resolved_intro_scenes") or metadata.get("intro_scenes") or []
    intro = [dict(x) for x in intro_source if isinstance(x, dict)]
    if not intro:
        return [dict(x) for x in narration_scenes]
    duration = float((metadata.get("intro_plan") or {}).get("duration_seconds") or getattr(settings, "intro_duration_seconds", 0) or 0)
    return intro + [_shift_time_fields(scene, duration) for scene in narration_scenes]


def _prepare_intro_checkpoint(job_dir: Path, source_signature: str, settings: ProjectSettings, narration_scenes: list[dict[str, Any]], reuse: bool) -> list[dict[str, Any]]:
    if not bool(getattr(settings, "intro_enabled", False)):
        _planned_intro_scenes(settings)
        return narration_scenes
    signature = _intro_plan_signature(source_signature, settings)
    path = _checkpoint_dir(job_dir) / "intro_plan.json"
    if reuse:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if int(payload.get("version") or 0) == PIPELINE_CHECKPOINT_VERSION and payload.get("intro_signature") == signature:
                intro = [dict(x) for x in payload.get("intro_scenes", []) if isinstance(x, dict)]
                if intro:
                    metadata = dict(settings.metadata or {})
                    metadata.update(dict(payload.get("metadata") or {}))
                    metadata["intro_scenes"] = intro
                    settings.metadata = metadata
                    return narration_scenes
        except Exception:
            pass
    intro = _planned_intro_scenes(settings)
    _atomic_json(path, {
        "version": PIPELINE_CHECKPOINT_VERSION, "intro_signature": signature,
        "stage": "intro_resolved", "intro_scenes": intro,
        "documentary_scenes_shifted": False,
        "metadata": {"intro_plan": dict((settings.metadata or {}).get("intro_plan") or {})},
        "completed_at": now_iso(),
    })
    return narration_scenes


def _music_plan_signature(source_signature: str, settings: ProjectSettings) -> str:
    return _json_hash({
        "source_signature": source_signature,
        "playlist": list(getattr(settings, "music_playlist", []) or []),
        "ai": bool(getattr(settings, "music_ai_select_from_library", False)),
        "categories": list(getattr(settings, "music_ai_allowed_categories", []) or []),
        "library_manifest": _file_signature(str(PERMANENT_ROOT / "library.json")),
    })


def _resolve_music_plan_checkpoint(job_dir: Path, source_signature: str, settings: ProjectSettings, project_label: str, reuse: bool) -> list[dict[str, Any]]:
    metadata = dict(settings.metadata or {})
    legacy = Path(str(settings.music_assigned_path or ""))
    signature = _music_plan_signature(source_signature, settings)
    path = _checkpoint_dir(job_dir) / "music_plan.json"
    resolved: list[dict[str, Any]] = []
    if reuse:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            candidates = [dict(x) for x in payload.get("playlist", []) if isinstance(x, dict)]
            if int(payload.get("version") or 0) == PIPELINE_CHECKPOINT_VERSION and payload.get("music_signature") == signature and all(Path(str(x.get("path") or "")).is_file() for x in candidates):
                resolved = candidates
        except Exception:
            pass
    if not resolved and bool(settings.music_enabled):
        if legacy.is_file():
            resolved = [{"asset_id": "legacy_project_music", "path": str(legacy), "display_name": legacy.stem, "category": "legacy", "role": "general", "order": 1}]
        else:
            resolved = resolve_music_playlist(
                PERMANENT_ROOT, list(getattr(settings, "music_playlist", []) or []),
                ai_select=bool(getattr(settings, "music_ai_select_from_library", False)),
                allowed_categories=list(getattr(settings, "music_ai_allowed_categories", []) or []),
                project_name=project_label, script_text=str(settings.text or ""),
                intro_queries=[str(x.get("query") or "") for x in metadata.get("intro_queries", []) if isinstance(x, dict)],
                intro_enabled=bool(getattr(settings, "intro_enabled", False)),
            )
        _atomic_json(path, {"version": PIPELINE_CHECKPOINT_VERSION, "music_signature": signature, "stage": "music_playlist_resolved", "playlist": resolved, "completed_at": now_iso()})
    if bool(settings.music_enabled) and not resolved:
        metadata.setdefault("audio_warnings", []).append("Music was enabled but no valid library track resolved; rendering continues without music.")
        settings.music_enabled = False
    metadata["resolved_music_playlist"] = resolved
    if resolved and str(resolved[0].get("selection_method") or "") == "smart_fallback":
        warning = str(resolved[0].get("selection_warning") or "AI Music Selection fell back to deterministic Smart Music Selection.")
        metadata.setdefault("audio_warnings", []).append(warning)
    metadata["music_selection_method"] = str(resolved[0].get("selection_method") or ("manual" if resolved else "none")) if resolved else "none"
    settings.metadata = metadata
    settings.music_assigned_path = str(resolved[0].get("path") or "") if resolved else ""
    return resolved


def _sfx_plan_signature(source_signature: str, settings: ProjectSettings, scenes: list[dict[str, Any]]) -> str:
    return _json_hash({
        "source_signature": source_signature, "scenes": scenes,
        "enabled": bool(getattr(settings, "sfx_enabled", False)),
        "environment": bool(getattr(settings, "environment_sfx_enabled", True)),
        "transition": bool(getattr(settings, "transition_sfx_library_enabled", True)),
        "amount": str(getattr(settings, "sfx_amount", "medium")),
        "env_volume": int(getattr(settings, "environment_sfx_volume_percent", 18) or 18),
        "transition_volume": int(getattr(settings, "transition_sfx_volume_percent", 24) or 24),
        "library_manifest": _file_signature(str(PERMANENT_ROOT / "library.json")),
    })


def _resolve_sfx_plan_checkpoint(job_dir: Path, source_signature: str, settings: ProjectSettings, scenes: list[dict[str, Any]], reuse: bool) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    signature = _sfx_plan_signature(source_signature, settings, scenes)
    path = _checkpoint_dir(job_dir) / "sfx_plan.json"
    plan: dict[str, Any] | None = None
    if reuse:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            candidate = dict(payload.get("plan") or {})
            events = [*candidate.get("environment", []), *candidate.get("transition", [])]
            if int(payload.get("version") or 0) == PIPELINE_CHECKPOINT_VERSION and payload.get("sfx_signature") == signature and all(Path(str(x.get("path") or "")).is_file() for x in events if isinstance(x, dict)):
                plan = candidate
        except Exception:
            pass
    if plan is None:
        plan = resolve_sfx_plan(PERMANENT_ROOT, scenes, settings)
        _atomic_json(path, {"version": PIPELINE_CHECKPOINT_VERSION, "sfx_signature": signature, "stage": "sfx_plan_resolved", "plan": plan, "completed_at": now_iso()})
    local_transition_codes = {code for event in plan.get("transition", []) if isinstance(event, dict) for code in event.get("scene_codes", [])}
    adjusted = []
    for scene in scenes:
        row = dict(scene)
        if str(row.get("scene_code") or "") in local_transition_codes:
            row["transition_sfx"] = "none"
            row["sfx_volume"] = 0.0
        adjusted.append(row)
    metadata = dict(settings.metadata or {})
    metadata["resolved_sfx_plan"] = plan
    if plan.get("warnings"):
        metadata.setdefault("audio_warnings", []).extend([str(x) for x in plan.get("warnings", [])][:100])
    settings.metadata = metadata
    return adjusted, plan


def _failure_stage(job: dict[str, Any]) -> str:
    try:
        state = json.loads(str(job.get("stage_state_json") or "{}"))
        failure = dict(state.get("failure") or {})
        if failure.get("stage"):
            return str(failure["stage"])
    except Exception:
        pass
    exact = str(job.get("exact_stage") or "")
    if exact.startswith("render_checkpoint"):
        return "Rendering"
    return str(job.get("stage") or "Failed")


def _clear_stage_and_downstream(job_dir: Path, stage: str) -> str:
    checkpoint_root = job_dir / PIPELINE_CHECKPOINT_DIR
    stage_key = str(stage or "").lower()
    def unlink(name: str) -> None:
        try:
            (checkpoint_root / name).unlink(missing_ok=True)
        except OSError:
            pass
    def clear_render() -> None:
        shutil.rmtree(job_dir / "output" / "render_checkpoint", ignore_errors=True)
        shutil.rmtree(job_dir / "output" / "intro_segment", ignore_errors=True)
        shutil.rmtree(job_dir / "output" / "main_segment", ignore_errors=True)
        try:
            (checkpoint_root / "intro_rendered.json").unlink(missing_ok=True)
        except OSError:
            pass
        for name in ("final.mp4", "final.partial.mp4"):
            try:
                (job_dir / "output" / name).unlink(missing_ok=True)
            except OSError:
                pass

    if "whisper" in stage_key or "prepar" in stage_key:
        for name in ("timing.json", "intro_plan.json", "intro_rendered.json", "edit_direction.json", "music_plan.json", "sfx_plan.json", "audio_mix.json"):
            unlink(name)
        shutil.rmtree(job_dir / "resolved_media", ignore_errors=True)
        shutil.rmtree(job_dir / "resolved_intro_media", ignore_errors=True)
        (job_dir / "media_resolution.json").unlink(missing_ok=True)
        (job_dir / "intro_media_resolution.json").unlink(missing_ok=True)
        (job_dir / "resolved_job_plan.json").unlink(missing_ok=True)
        clear_render()
        return "Whisper / Timing"
    if "edit" in stage_key or "direction" in stage_key:
        for name in ("edit_direction.json", "music_plan.json", "sfx_plan.json", "audio_mix.json"):
            unlink(name)
        shutil.rmtree(job_dir / "resolved_media", ignore_errors=True)
        shutil.rmtree(job_dir / "resolved_intro_media", ignore_errors=True)
        (job_dir / "media_resolution.json").unlink(missing_ok=True)
        (job_dir / "intro_media_resolution.json").unlink(missing_ok=True)
        (job_dir / "resolved_job_plan.json").unlink(missing_ok=True)
        clear_render()
        return "Edit Direction"
    if "music" in stage_key:
        for name in ("music_plan.json", "audio_mix.json"):
            unlink(name)
        (job_dir / "resolved_job_plan.json").unlink(missing_ok=True)
        clear_render()
        return "Music Resolution"
    if "sfx" in stage_key or "sound" in stage_key:
        for name in ("sfx_plan.json", "audio_mix.json"):
            unlink(name)
        (job_dir / "resolved_job_plan.json").unlink(missing_ok=True)
        clear_render()
        return "SFX Resolution"
    if "intro" in stage_key:
        for name in ("intro_plan.json", "intro_rendered.json", "edit_direction.json", "music_plan.json", "sfx_plan.json", "audio_mix.json"):
            unlink(name)
        shutil.rmtree(job_dir / "resolved_media", ignore_errors=True)
        shutil.rmtree(job_dir / "resolved_intro_media", ignore_errors=True)
        (job_dir / "media_resolution.json").unlink(missing_ok=True)
        (job_dir / "intro_media_resolution.json").unlink(missing_ok=True)
        (job_dir / "resolved_job_plan.json").unlink(missing_ok=True)
        clear_render()
        return "Opening Intro"
    if "audio" in stage_key:
        unlink("audio_mix.json")
        (job_dir / "resolved_job_plan.json").unlink(missing_ok=True)
        clear_render()
        return "Audio Mix"
    if "pexels" in stage_key or "media" in stage_key or "download" in stage_key:
        shutil.rmtree(job_dir / "resolved_media", ignore_errors=True)
        shutil.rmtree(job_dir / "resolved_intro_media", ignore_errors=True)
        (job_dir / "media_resolution.json").unlink(missing_ok=True)
        (job_dir / "intro_media_resolution.json").unlink(missing_ok=True)
        (job_dir / "resolved_job_plan.json").unlink(missing_ok=True)
        clear_render()
        return "Media"
    if "render" in stage_key:
        clear_render()
        return "Rendering"
    if "compress" in stage_key:
        # The validated render remains reusable; only compression/delivery is retried.
        return "Compression"
    if "drive" in stage_key or "upload" in stage_key:
        return "Google Drive"
    clear_render()
    return "Rendering"


def _resume_snapshot(job_id: str) -> dict[str, Any]:
    job_dir = JOBS / job_id
    report_path = job_dir / "media_resolution.json"
    manifest_path = job_dir / "output" / "render_checkpoint" / "checkpoint.json"
    cached_media_count = 0
    report_scene_count = 0
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report_scenes = [item for item in report.get("scenes", []) if isinstance(item, dict)]
        report_scene_count = len(report_scenes)
        cached_media_count = sum(
            1 for item in report_scenes
            if Path(str(item.get("media_path") or "")).is_file()
        )
    except Exception:
        pass
    checkpoint: dict[str, Any] = {}
    try:
        checkpoint = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        pass
    scene_count = int(checkpoint.get("scene_count") or report_scene_count or 0)
    pipeline_root = job_dir / PIPELINE_CHECKPOINT_DIR
    timing_path = pipeline_root / "timing.json"
    timing_ready = timing_path.is_file()
    if scene_count <= 0 and timing_ready:
        try:
            timing_payload = json.loads(timing_path.read_text(encoding="utf-8"))
            scene_count = len([item for item in timing_payload.get("scenes", []) if isinstance(item, dict)])
        except Exception:
            pass
    intro_ready = (pipeline_root / "intro_plan.json").is_file()
    intro_rendered_ready = (pipeline_root / "intro_rendered.json").is_file()
    edit_ready = (pipeline_root / "edit_direction.json").is_file()
    music_ready = (pipeline_root / "music_plan.json").is_file()
    sfx_ready = (pipeline_root / "sfx_plan.json").is_file()
    audio_ready = (pipeline_root / "audio_mix.json").is_file()
    resolved_plan_ready = (job_dir / "resolved_job_plan.json").is_file()
    audit_ready = (job_dir / "audit_first2.json").is_file()
    media_ready = bool(report_scene_count and cached_media_count == report_scene_count and report_path.is_file())
    any_checkpoint = timing_ready or intro_ready or intro_rendered_ready or edit_ready or music_ready or sfx_ready or audio_ready or media_ready or audit_ready or bool(checkpoint) or resolved_plan_ready
    completed_stages = []
    if timing_ready: completed_stages.append("whisper")
    if intro_ready: completed_stages.append("intro_resolved")
    if intro_rendered_ready: completed_stages.append("intro_rendered")
    if edit_ready: completed_stages.append("edit_direction")
    if music_ready: completed_stages.append("music_playlist")
    if sfx_ready: completed_stages.append("sfx_plan")
    if audio_ready: completed_stages.append("audio_mix")
    if audit_ready: completed_stages.append("visual_audit")
    if media_ready: completed_stages.append("media")
    if checkpoint.get("completed_chunks"): completed_stages.append("render_chunks")
    if resolved_plan_ready: completed_stages.append("resolved_plan")
    return {
        "resume_available": bool(any_checkpoint),
        "cached_media_count": cached_media_count,
        "scene_count": scene_count,
        "last_completed_scene": int(checkpoint.get("last_completed_scene") or 0),
        "completed_render_chunks": len(checkpoint.get("completed_chunks") or []),
        "total_render_chunks": int(checkpoint.get("total_chunks") or 0),
        "timing_checkpoint_ready": timing_ready,
        "intro_checkpoint_ready": intro_ready,
        "intro_rendered_checkpoint_ready": intro_rendered_ready,
        "edit_checkpoint_ready": edit_ready,
        "music_checkpoint_ready": music_ready,
        "sfx_checkpoint_ready": sfx_ready,
        "audio_checkpoint_ready": audio_ready,
        "audit_checkpoint_ready": audit_ready,
        "media_checkpoint_ready": media_ready,
        "resolved_plan_ready": resolved_plan_ready,
        "completed_stages": completed_stages,
    }


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    result = dict(job)
    if str(result.get("status") or "").lower() in {"failed", "cancelled"}:
        result.update(_resume_snapshot(str(result.get("id") or "")))
    else:
        result.update(
            {
                "resume_available": False,
                "cached_media_count": 0,
                "scene_count": 0,
                "last_completed_scene": 0,
                "completed_render_chunks": 0,
                "total_render_chunks": 0,
                "timing_checkpoint_ready": False,
                "intro_checkpoint_ready": False,
                "intro_rendered_checkpoint_ready": False,
                "edit_checkpoint_ready": False,
                "audio_checkpoint_ready": False,
                "audit_checkpoint_ready": False,
                "media_checkpoint_ready": False,
                "resolved_plan_ready": False,
                "completed_stages": [],
            }
        )
    return result



AUDIT_CHECKPOINT_VERSION = 1
AUDIT_WINDOW_SECONDS = 120.0


def _audit_checkpoint_path(job_dir: Path) -> Path:
    return job_dir / "audit_first2.json"


def _load_audit_checkpoint(job_dir: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(_audit_checkpoint_path(job_dir).read_text(encoding="utf-8"))
    except Exception:
        return None
    if int(payload.get("version") or 0) != AUDIT_CHECKPOINT_VERSION:
        return None
    if not isinstance(payload.get("items"), list):
        return None
    return payload


def _save_audit_checkpoint(job_dir: Path, payload: dict[str, Any]) -> None:
    data = dict(payload)
    data["version"] = AUDIT_CHECKPOINT_VERSION
    data["updated_at"] = now_iso()
    _atomic_json(_audit_checkpoint_path(job_dir), data)


def _audit_final_timeline_rows(settings: ProjectSettings, narration_scenes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only scene references intersecting final timeline 00:00-02:00."""
    metadata = dict(settings.metadata or {})
    intro_duration = 0.0
    rows: list[dict[str, Any]] = []
    if bool(getattr(settings, "intro_enabled", False)):
        intro_duration = float((metadata.get("intro_plan") or {}).get("duration_seconds") or getattr(settings, "intro_duration_seconds", 0) or 0)
        for index, source in enumerate(metadata.get("intro_scenes", []) or []):
            if not isinstance(source, dict):
                continue
            start = float(source.get("start", source.get("start_seconds", 0.0)) or 0.0)
            end = float(source.get("end", source.get("end_seconds", start + _scene_duration(source))) or (start + _scene_duration(source)))
            if start >= AUDIT_WINDOW_SECONDS or end <= 0:
                continue
            rows.append({
                "item_id": str(source.get("scene_code") or f"INTRO_{index + 1:03d}"),
                "kind": "intro",
                "scene_code": str(source.get("scene_code") or f"INTRO_{index + 1:03d}"),
                "final_start": max(0.0, start),
                "final_end": min(AUDIT_WINDOW_SECONDS, end),
                "scene": dict(source),
            })
    for index, source in enumerate(narration_scenes):
        if not isinstance(source, dict):
            continue
        local_start = float(source.get("start", source.get("start_seconds", 0.0)) or 0.0)
        local_end = float(source.get("end", source.get("end_seconds", local_start + _scene_duration(source))) or (local_start + _scene_duration(source)))
        start = local_start + intro_duration
        end = local_end + intro_duration
        if start >= AUDIT_WINDOW_SECONDS or end <= 0:
            continue
        rows.append({
            "item_id": str(source.get("scene_code") or f"SCENE_{index + 1:03d}"),
            "kind": "documentary",
            "scene_code": str(source.get("scene_code") or f"SCENE_{index + 1:03d}"),
            "final_start": max(0.0, start),
            "final_end": min(AUDIT_WINDOW_SECONDS, end),
            "scene": dict(source),
        })
    rows.sort(key=lambda row: (float(row.get("final_start") or 0), 0 if row.get("kind") == "intro" else 1, str(row.get("scene_code") or "")))
    return rows


def _audit_safe_query(scene: dict[str, Any]) -> str:
    keywords = _scene_keywords(scene)
    return keywords[0] if keywords else "documentary background"


def _audit_asset_key(item: dict[str, Any]) -> str:
    provider = str(item.get("provider") or "").strip()
    asset_id = str(item.get("asset_id") or "").strip()
    return f"{provider}:{asset_id}" if provider and asset_id else ""


def _copy_review_media_for_audit(scene: dict[str, Any], item_id: str, assignment_root: Path) -> dict[str, Any] | None:
    if not (bool(scene.get("review_approved")) and bool(scene.get("review_locked"))):
        return None
    source = Path(str(scene.get("media_path") or ""))
    if not source.is_file():
        return None
    requested_type = _assigned_media_type(source)
    validate_media(source, requested_type, _scene_duration(scene), require_landscape=bool(scene.get("pexels_prefer_landscape", True)))
    destination = assignment_root / f"{item_id}_review{source.suffix.lower()}"
    shutil.copy2(source, destination)
    return {
        **dict(scene),
        "media_path": str(destination),
        "media_source_path": str(source),
        "assigned_media_type": requested_type,
        "provider": str(scene.get("provider") or scene.get("media_provider") or "review"),
        "asset_id": str(scene.get("asset_id") or scene.get("provider_asset_id") or sha256_file(source)[:16]),
        "keyword_used": _audit_safe_query(scene),
        "selected_keyword": _audit_safe_query(scene),
        "source_type": "UPLOADED" if str(scene.get("provider") or "").lower() == "uploaded" else "REVIEWED",
    }


def _resolve_audit_candidate(
    *,
    job_id: str,
    job_dir: Path,
    item_id: str,
    scene: dict[str, Any],
    keyword: str,
    revision: int,
    rejected_asset_keys: set[str],
    used_asset_keys: set[str],
    used_hashes: set[str],
    target_width: int,
    target_height: int,
) -> tuple[dict[str, Any] | None, str]:
    if not _current_pexels_keys():
        return None, "No Pexels API key is configured"
    assignment_root = job_dir / "audit_media"
    assignment_root.mkdir(parents=True, exist_ok=True)
    provider_cache = job_dir / "pexels_provider_cache"
    provider_cache.mkdir(parents=True, exist_ok=True)
    requested_type = str(scene.get("media_type") or "video").lower()
    if requested_type not in {"image", "video"}:
        requested_type = "video"
    candidate_scene = dict(scene)
    candidate_scene["primary_keyword"] = keyword
    candidate_scene["keyword"] = keyword
    candidate_scene["visual_prompt"] = keyword
    exclusions = set(used_asset_keys) | set(rejected_asset_keys)
    code = f"{item_id}_r{max(1, int(revision))}"
    resolved, error = _download_scene_candidate(
        scene=candidate_scene,
        scene_code=code,
        requested_type=requested_type,
        keyword=keyword,
        provider_cache=provider_cache,
        assignment_root=assignment_root,
        used_asset_ids=exclusions,
        used_hashes=set(used_hashes),
        target_width=target_width,
        target_height=target_height,
    )
    if resolved is None and requested_type == "video" and bool(candidate_scene.get("allow_media_fallback", True)):
        resolved, error = _download_scene_candidate(
            scene=candidate_scene,
            scene_code=code,
            requested_type="image",
            keyword=keyword,
            provider_cache=provider_cache,
            assignment_root=assignment_root,
            used_asset_ids=exclusions,
            used_hashes=set(used_hashes),
            target_width=target_width,
            target_height=target_height,
        )
        if resolved is not None:
            resolved["fallback_type"] = "image_fallback"
            resolved["fallback_reason"] = "No valid Pexels video was available for this audited scene"
    return resolved, error


def _audit_item_from_result(base: dict[str, Any], result: dict[str, Any] | None, error: str, *, revision: int = 1) -> dict[str, Any]:
    scene = dict(base.get("scene") or {})
    original_query = _audit_safe_query(scene)
    item = {
        "item_id": str(base.get("item_id") or base.get("scene_code") or ""),
        "kind": str(base.get("kind") or "documentary"),
        "scene_code": str(base.get("scene_code") or ""),
        "final_start": round(float(base.get("final_start") or 0.0), 3),
        "final_end": round(float(base.get("final_end") or 0.0), 3),
        "narration": " ".join(str(scene.get("text") or "").split())[:360],
        "original_query": original_query,
        "query_override": "",
        "keyword_used": str((result or {}).get("keyword_used") or (result or {}).get("selected_keyword") or original_query),
        "scene_snapshot": scene,
        "source_type": "PEXELS" if result else "MISSING",
        "media_path": str((result or {}).get("media_path") or ""),
        "media_source_path": str((result or {}).get("media_source_path") or ""),
        "assigned_media_type": str((result or {}).get("assigned_media_type") or scene.get("media_type") or "video"),
        "provider": str((result or {}).get("provider") or (result or {}).get("media_provider") or ""),
        "asset_id": str((result or {}).get("asset_id") or (result or {}).get("provider_asset_id") or ""),
        "error": str(error or "") if not result else "",
        "rejected_asset_ids": [],
        "approved": False,
        "changed": False,
        "revision": int(revision),
    }
    if result and str(result.get("source_type") or ""):
        item["source_type"] = str(result.get("source_type"))
    return item


def _prepare_first2_visual_audit(
    job_id: str,
    job_dir: Path,
    source_signature: str,
    settings: ProjectSettings,
    narration_scenes: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    """Create or reuse the opening visual audit. Returns (scenes, should_stop)."""
    if not bool(getattr(settings, "first_two_minute_visual_audit_enabled", False)):
        return narration_scenes, False
    existing = _load_audit_checkpoint(job_dir)
    if existing and str(existing.get("source_signature") or "") == source_signature:
        if bool(existing.get("approved")):
            return _apply_approved_visual_audit(job_dir, settings, narration_scenes, existing), False
        missing = sum(1 for item in existing.get("items", []) if not Path(str(item.get("media_path") or "")).is_file())
        update_job(
            job_id, status="waiting_for_audit", stage="Needs Visual Audit", progress=34,
            message=f"First 2-minute visuals ready for review · {len(existing.get('items', []))} items · {missing} missing",
        )
        return narration_scenes, True

    rows = _audit_final_timeline_rows(settings, narration_scenes)
    if not rows:
        # No opening rows is a configuration failure, not an audit-media failure.
        raise RuntimeError("First 2-Minute Visual Audit is enabled, but no scenes intersect 00:00-02:00.")
    assignment_root = job_dir / "audit_media"
    shutil.rmtree(assignment_root, ignore_errors=True)
    assignment_root.mkdir(parents=True, exist_ok=True)
    items: list[dict[str, Any]] = []
    used_asset_keys: set[str] = set()
    used_hashes: set[str] = set()
    for index, row in enumerate(rows):
        if cancelled(job_id):
            raise RuntimeError("Remote render cancelled.")
        scene = dict(row.get("scene") or {})
        item_id = str(row.get("item_id") or row.get("scene_code") or f"AUDIT_{index + 1:03d}")
        update_job(
            job_id, status="downloading_media", stage="Preparing Visual Audit", progress=30 + int((index / max(1, len(rows))) * 4),
            message=f"{item_id}: resolving opening visual for audit",
        )
        try:
            result = _copy_review_media_for_audit(scene, item_id, assignment_root)
        except Exception as exc:
            result = None
            error = f"Reviewed media is invalid: {exc}"
        else:
            error = ""
        if result is None:
            # Initial audit resolution follows the Scene Plan's ordered keyword
            # fallbacks, but stops at the first usable asset. The UI exposes the
            # actual keyword used so a generic fallback is immediately visible.
            keywords = _scene_keywords(scene) or [_audit_safe_query(scene)]
            for keyword in keywords:
                result, error = _resolve_audit_candidate(
                    job_id=job_id, job_dir=job_dir, item_id=item_id, scene=scene, keyword=keyword, revision=1,
                    rejected_asset_keys=set(), used_asset_keys=used_asset_keys, used_hashes=used_hashes,
                    target_width=int(settings.width), target_height=int(settings.height),
                )
                if result is not None:
                    break
        item = _audit_item_from_result(row, result, error, revision=1)
        if result:
            key = _audit_asset_key(item)
            if key:
                used_asset_keys.add(key)
            path = Path(str(item.get("media_path") or ""))
            if path.is_file():
                try: used_hashes.add(sha256_file(path))
                except Exception: pass
        items.append(item)
    payload = {
        "version": AUDIT_CHECKPOINT_VERSION,
        "source_signature": source_signature,
        "window_seconds": AUDIT_WINDOW_SECONDS,
        "target_width": int(settings.width),
        "target_height": int(settings.height),
        "approved": False,
        "approved_at": "",
        "items": items,
        "created_at": now_iso(),
    }
    _save_audit_checkpoint(job_dir, payload)
    missing = sum(1 for item in items if not Path(str(item.get("media_path") or "")).is_file())
    update_job(
        job_id, status="waiting_for_audit", stage="Needs Visual Audit", progress=34,
        message=f"First 2-minute visuals ready for review · {len(items)} items · {missing} missing",
    )
    return narration_scenes, True


def _apply_approved_visual_audit(job_dir: Path, settings: ProjectSettings, narration_scenes: list[dict[str, Any]], checkpoint: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    payload = checkpoint or _load_audit_checkpoint(job_dir)
    if not payload or not bool(payload.get("approved")):
        return narration_scenes
    by_code = {str(item.get("scene_code") or ""): item for item in payload.get("items", []) if isinstance(item, dict)}

    def merge(source: dict[str, Any]) -> dict[str, Any]:
        row = dict(source)
        item = by_code.get(str(row.get("scene_code") or ""))
        if not item:
            return row
        media_path = Path(str(item.get("media_path") or ""))
        if not bool(item.get("approved")) or not media_path.is_file():
            raise RuntimeError(f"Approved visual audit is incomplete for {row.get('scene_code') or 'scene'}")
        row.update(
            media_path=str(media_path), media_source_path=str(item.get("media_source_path") or media_path),
            review_approved=True, review_locked=True, media_locked=True,
            provider=str(item.get("provider") or ("uploaded" if item.get("source_type") == "UPLOADED" else "pexels")),
            media_provider=str(item.get("provider") or ("uploaded" if item.get("source_type") == "UPLOADED" else "pexels")),
            asset_id=str(item.get("asset_id") or ""), provider_asset_id=str(item.get("asset_id") or ""),
            selected_keyword=str(item.get("query_override") or item.get("keyword_used") or item.get("original_query") or ""),
            keyword_used=str(item.get("query_override") or item.get("keyword_used") or item.get("original_query") or ""),
            assigned_media_type=str(item.get("assigned_media_type") or _assigned_media_type(media_path)),
            audit_first2_approved=True, audit_revision=int(item.get("revision") or 1),
        )
        return row

    merged_main = [merge(scene) for scene in narration_scenes]
    metadata = dict(settings.metadata or {})
    metadata["intro_scenes"] = [merge(scene) for scene in metadata.get("intro_scenes", []) if isinstance(scene, dict)]
    metadata["visual_audit_first2"] = {
        "approved": True, "approved_at": str(payload.get("approved_at") or ""),
        "window_seconds": AUDIT_WINDOW_SECONDS, "checkpoint": str(_audit_checkpoint_path(job_dir)),
    }
    settings.metadata = metadata
    return merged_main


def _public_audit_payload(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    items = []
    for raw in payload.get("items", []):
        if not isinstance(raw, dict):
            continue
        row = {key: value for key, value in raw.items() if key not in {"media_path", "media_source_path", "scene_snapshot"}}
        row["has_media"] = Path(str(raw.get("media_path") or "")).is_file()
        row["preview_url"] = f"/jobs/{job_id}/audit/media/{raw.get('item_id')}/preview" if row["has_media"] else ""
        items.append(row)
    return {
        "job_id": job_id,
        "window_seconds": float(payload.get("window_seconds") or AUDIT_WINDOW_SECONDS),
        "approved": bool(payload.get("approved")),
        "approved_at": str(payload.get("approved_at") or ""),
        "created_at": str(payload.get("created_at") or ""),
        "updated_at": str(payload.get("updated_at") or ""),
        "items": items,
        "scene_count": len(items),
        "changed_count": sum(1 for item in items if bool(item.get("changed"))),
        "missing_count": sum(1 for item in items if not bool(item.get("has_media"))),
        "approved_count": sum(1 for item in items if bool(item.get("approved"))),
    }


def _find_audit_item(payload: dict[str, Any], item_id: str) -> tuple[int, dict[str, Any]]:
    for index, item in enumerate(payload.get("items", [])):
        if isinstance(item, dict) and str(item.get("item_id") or "") == str(item_id):
            return index, item
    raise HTTPException(404, "Audit item not found")


def _validate_audit_revision(item: dict[str, Any], expected: Any) -> None:
    if expected in (None, ""):
        return
    try:
        expected_int = int(expected)
    except Exception as exc:
        raise HTTPException(400, "Invalid audit revision") from exc
    if expected_int != int(item.get("revision") or 0):
        raise HTTPException(409, "This audit card changed in another browser session. Refresh and try again.")


def _delete_audit_owned_if_unused(job_dir: Path, payload: dict[str, Any], path_value: str, *, excluding_item_id: str) -> None:
    path = Path(str(path_value or ""))
    if not path.is_file():
        return
    allowed_roots = [(job_dir / "audit_media").resolve(), (job_dir / "pexels_provider_cache").resolve()]
    try:
        resolved = path.resolve()
        if not any(resolved.is_relative_to(root) for root in allowed_roots):
            return
    except Exception:
        return
    for other in payload.get("items", []):
        if not isinstance(other, dict) or str(other.get("item_id") or "") == excluding_item_id:
            continue
        for key in ("media_path", "media_source_path"):
            try:
                if Path(str(other.get(key) or "")).resolve() == resolved:
                    return
            except Exception:
                pass
    try: path.unlink(missing_ok=True)
    except OSError: pass


def _replace_audit_item_media(job_id: str, item_id: str, *, keyword: str | None, expected_revision: Any = None) -> dict[str, Any]:
    job = get_job(job_id)
    if str(job.get("status") or "") != "waiting_for_audit":
        raise HTTPException(409, "Project is not waiting for Visual Audit")
    job_dir = JOBS / job_id
    with _audit_lock(job_id):
        payload = _load_audit_checkpoint(job_dir)
        if not payload or bool(payload.get("approved")):
            raise HTTPException(409, "Visual Audit is not editable")
        index, item = _find_audit_item(payload, item_id)
        _validate_audit_revision(item, expected_revision)
        scene = dict(item.get("scene_snapshot") or {})
        chosen_keyword = " ".join(str(keyword if keyword is not None else item.get("query_override") or item.get("keyword_used") or item.get("original_query") or "").split()).strip()
        if not chosen_keyword:
            raise HTTPException(400, "Search keyword cannot be empty")
        rejected = {str(value) for value in item.get("rejected_asset_ids", []) if str(value)}
        current_key = _audit_asset_key(item)
        if current_key:
            rejected.add(current_key)
        used_keys = {_audit_asset_key(other) for other in payload.get("items", []) if isinstance(other, dict) and str(other.get("item_id") or "") != item_id}
        used_keys.discard("")
        used_hashes: set[str] = set()
        for other in payload.get("items", []):
            if not isinstance(other, dict) or str(other.get("item_id") or "") == item_id:
                continue
            other_path = Path(str(other.get("media_path") or ""))
            if other_path.is_file():
                try: used_hashes.add(sha256_file(other_path))
                except Exception: pass
        revision = int(item.get("revision") or 0) + 1
        result, error = _resolve_audit_candidate(
            job_id=job_id, job_dir=job_dir, item_id=item_id, scene=scene, keyword=chosen_keyword, revision=revision,
            rejected_asset_keys=rejected, used_asset_keys=used_keys, used_hashes=used_hashes,
            target_width=int(payload.get("target_width") or 1280), target_height=int(payload.get("target_height") or 720),
        )
        if result is None:
            # Keep the current usable visual on a failed replacement. Keyword edits
            # are still saved so the user can retry without losing the previous clip.
            item["query_override"] = chosen_keyword if keyword is not None else str(item.get("query_override") or "")
            item["error"] = str(error or "No usable replacement found")
            item["revision"] = revision
            item["changed"] = True
            payload["items"][index] = item
            _save_audit_checkpoint(job_dir, payload)
            return _public_audit_payload(job_id, payload)
        old_media = str(item.get("media_path") or "")
        old_source = str(item.get("media_source_path") or "")
        item.update(
            query_override=chosen_keyword if keyword is not None else str(item.get("query_override") or ""),
            keyword_used=str(result.get("keyword_used") or result.get("selected_keyword") or chosen_keyword),
            source_type="PEXELS", media_path=str(result.get("media_path") or ""), media_source_path=str(result.get("media_source_path") or ""),
            assigned_media_type=str(result.get("assigned_media_type") or scene.get("media_type") or "video"),
            provider=str(result.get("provider") or "pexels"), asset_id=str(result.get("asset_id") or ""),
            rejected_asset_ids=sorted(rejected), error="", approved=True, changed=True, revision=revision,
        )
        payload["items"][index] = item
        _save_audit_checkpoint(job_dir, payload)
        _delete_audit_owned_if_unused(job_dir, payload, old_media, excluding_item_id=item_id)
        _delete_audit_owned_if_unused(job_dir, payload, old_source, excluding_item_id=item_id)
        return _public_audit_payload(job_id, payload)

# SCENE_INTERNAL_AUDIO_ENGINE_WORKER_V1
# Voice DSP is now an internal worker stage. Scene Builder Timing and native
# Music/SFX mixer remain the sole owners of timeline and background audio.
def _scene_internal_audio_config(settings: ProjectSettings) -> dict[str, Any]:
    metadata = dict(settings.metadata or {})
    raw = metadata.get("internal_audio_engine")
    if not isinstance(raw, dict):
        raw = {}
    value = raw.get("enabled", False)
    enabled = value if isinstance(value, bool) else str(value).strip().lower() in {"1","true","yes","on","enabled"}
    config = dict(raw)
    config["enabled"] = bool(enabled)
    config["preset"] = str(config.get("preset") or "documentary").strip() or "documentary"
    return config


def _scene_internal_audio_checkpoint(job_dir: Path) -> Path:
    root = _checkpoint_dir(job_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root / "voice_engine.json"


def _prepare_scene_internal_audio(job_id: str, job_dir: Path, settings: ProjectSettings, project_label: str) -> bool:
    config = _scene_internal_audio_config(settings)
    if not bool(config.get("enabled")):
        return False
    original = Path(str(settings.audio_path or ""))
    if not original.is_file() or original.stat().st_size <= 0:
        raise RuntimeError("Internal Audio Engine: original narration is missing")
    resolved = internal_audio_engine_resolve_settings(config)
    signature = _json_hash({
        "engine_version": INTERNAL_AUDIO_ENGINE_VERSION,
        "source": _file_signature(str(original)),
        "settings": {k: resolved[k] for k in sorted(resolved)},
    })
    checkpoint = _scene_internal_audio_checkpoint(job_dir)
    processed = checkpoint.parent / "narration_internal_audio_engine.mp3"
    try:
        payload = json.loads(checkpoint.read_text("utf-8")) if checkpoint.is_file() else {}
    except Exception:
        payload = {}
    if (
        isinstance(payload, dict)
        and payload.get("version") == 1
        and payload.get("signature") == signature
        and processed.is_file()
        and processed.stat().st_size >= 1024
    ):
        settings.audio_path = str(processed)
        metadata = dict(settings.metadata or {})
        metadata["internal_audio_engine_result"] = dict(payload.get("result") or {})
        metadata["internal_audio_engine_result"]["checkpoint_reused"] = True
        metadata.setdefault("pre_internal_voice_processing_preset", str(getattr(settings, "voice_processing_preset", "") or ""))
        settings.metadata = metadata
        settings.voice_processing_preset = "off"
        update_job(job_id, status="preparing", stage="Audio Engine Reuse", progress=5, message="Reusing validated internal narration-processing checkpoint")
        return True

    update_job(job_id, status="preparing", stage="Audio Engine", progress=2, message="Processing narration inside Scene Builder worker")
    def on_progress(pct: int, message: str) -> None:
        mapped = 2.0 + max(0, min(100, int(pct))) * 0.06
        update_job(job_id, status="preparing", stage="Audio Engine", progress=round(mapped, 1), message=str(message))
    result = internal_audio_engine_process(original, processed, config=config, progress_callback=on_progress)
    if not processed.is_file() or processed.stat().st_size < 1024:
        raise RuntimeError("Internal Audio Engine did not produce valid narration")
    result = dict(result or {})
    result.update({
        "enabled": True,
        "architecture": "scene_builder_worker_internal",
        "source_name": original.name,
        "processed_name": processed.name,
        "checkpoint_reused": False,
    })
    settings.audio_path = str(processed)
    metadata = dict(settings.metadata or {})
    metadata.setdefault("pre_internal_voice_processing_preset", str(getattr(settings, "voice_processing_preset", "") or ""))
    metadata["internal_audio_engine_result"] = result
    qc = result.get("qc") if isinstance(result.get("qc"), dict) else {}
    if qc and not bool(qc.get("pass", True)):
        metadata.setdefault("audio_warnings", []).append("Internal Audio Engine QC is outside target tolerance; processed narration was preserved for review.")
    settings.metadata = metadata
    settings.voice_processing_preset = "off"
    _atomic_json(checkpoint, {"version": 1, "signature": signature, "result": result, "processed_path": str(processed), "completed_at": now_iso()})
    return True

def process_job(job_id: str) -> None:
    job_dir = JOBS / job_id
    source_root = job_dir / "input"
    output_root = job_dir / "output"
    output_root.mkdir(parents=True, exist_ok=True)
    try:
        resume_mode = str(get_job(job_id).get("resume_mode") or "")
        update_job(
            job_id,
            status="preparing",
            stage="Preparing",
            progress=1,
            message=f"Worker {WORKER_BUILD}: validating uploaded project",
            started_at=now_iso(),
        )
        project_files = list(source_root.rglob("project.json")) + list(
            source_root.rglob("*.scene-project.json")
        )
        if not project_files:
            raise RuntimeError("project.json is missing from uploaded ZIP")
        data = json.loads(project_files[0].read_text("utf-8"))

        files: dict[str, list[Path]] = {}
        for item in source_root.rglob("*"):
            if item.is_file():
                files.setdefault(item.name.lower(), []).append(item)

        script_text = _read_script_text(data, source_root)
        if not script_text:
            raise RuntimeError("Uploaded package contains no usable project script text.")
        data["text"] = script_text

        metadata = dict(data.get("metadata") or {})
        metadata.pop("caption_segments", None)
        metadata.pop("caption_generation_signature", None)
        metadata["vps_generate_whisper"] = True
        metadata["remote_worker_fetches_stock_media"] = True
        data["metadata"] = metadata

        settings = ProjectSettings.from_dict(remap_paths(data, files, source_root))
        settings.text = script_text
        if bool(settings.voice_review_required) and not (
            bool(settings.voice_approved) and bool(settings.voice_locked)
        ):
            raise RuntimeError(
                "Voice Review is required. The uploaded project voice is not approved + locked."
            )
        settings.video_encoder = "cpu_x264"
        settings.output_folder = str(output_root)
        settings.output_path = str(output_root / "final.mp4")
        settings.pexels_cache_paths = []
        settings.pexels_download_enabled = bool(settings.pexels_enabled)
        settings.pexels_use_library = False
        settings.pexels_library_folder = ""
        settings.ai_media_enabled = False
        settings.auto_prepare_backgrounds = False

        scenes = [
            dict(scene)
            for scene in list((settings.metadata or {}).get("scene_sync_plan", []) or [])
            if isinstance(scene, dict)
        ]
        if not scenes:
            raise RuntimeError("Uploaded project contains no authoritative scene plan.")

        # Freeze Pexels selection policy into each scene so normal acquisition and
        # First-2-Minute Visual Audit use the same project-owned behavior.
        for scene in scenes:
            scene.setdefault("pexels_selection_mode", str(getattr(settings, "pexels_selection_mode", "best_match_variety") or "best_match_variety"))
            scene.setdefault("pexels_search_pool", int(getattr(settings, "pexels_search_pool", 10) or 10))
            scene.setdefault("pexels_avoid_used_media", bool(getattr(settings, "pexels_avoid_used_media", True)))
            scene.setdefault("pexels_avoid_same_creator", bool(getattr(settings, "pexels_avoid_same_creator", True)))
            scene.setdefault("pexels_prefer_landscape", bool(getattr(settings, "pexels_prefer_landscape", True)))
            scene.setdefault("pexels_prefer_video", bool(getattr(settings, "pexels_prefer_video", True)))
            scene.setdefault("pexels_allow_video", bool(getattr(settings, "use_video_backgrounds", True)))
            scene.setdefault("pexels_allow_image", bool(getattr(settings, "use_image_backgrounds", True)))
            scene.setdefault("pexels_enabled", bool(getattr(settings, "pexels_enabled", True)))
            scene["_sml_first"] = (
                str((settings.metadata or {}).get("media_source_mode") or "") == "sml_first"
                and bool((settings.metadata or {}).get("sml_auto_scene_matching", False))
            )

        # Unreviewed local PC paths are never trusted on the VPS. Approved + locked
        # review media may be bundled by the desktop remote package and is preserved
        # only when the remapped file lives inside this job's extracted input root.
        path_fields = (
            "media_path",
            "media_source_path",
            "image_path",
            "video_path",
            "video_candidate_path",
            "source_path",
            "file_path",
            "path",
        )
        cleaned_scenes: list[dict[str, Any]] = []
        review_missing: list[str] = []
        source_resolved = source_root.resolve()
        for index, scene in enumerate(scenes):
            cleaned = dict(scene)
            reviewed_locked = bool(cleaned.get("review_approved")) and bool(cleaned.get("review_locked"))
            preserved = ""
            if reviewed_locked:
                candidate = Path(str(cleaned.get("media_path") or ""))
                try:
                    resolved = candidate.resolve()
                    if resolved.is_file() and resolved.is_relative_to(source_resolved):
                        preserved = str(resolved)
                except (OSError, ValueError):
                    preserved = ""
            for field in path_fields:
                cleaned[field] = ""
            if preserved:
                cleaned["media_path"] = preserved
                cleaned["media_source_path"] = preserved
                cleaned["remote_review_media_bundled"] = True
            elif bool(settings.scene_review_required):
                review_missing.append(
                    str(cleaned.get("scene_code") or f"SCENE_{index + 1:03d}")
                )
            cleaned_scenes.append(cleaned)
        if bool(settings.scene_review_required):
            unapproved = [
                str(scene.get("scene_code") or f"SCENE_{index + 1:03d}")
                for index, scene in enumerate(cleaned_scenes)
                if not (bool(scene.get("review_approved")) and bool(scene.get("review_locked")))
            ]
            failures = list(dict.fromkeys(unapproved + review_missing))
            if failures:
                raise RuntimeError(
                    "Scene Review is required. Every scene must be approved + locked and its exact reviewed media must be bundled. Missing: "
                    + ", ".join(failures[:20])
                )

        project_label = str(get_job(job_id).get("project_name") or settings.project_name or "Untitled")
        _prepare_scene_internal_audio(job_id, job_dir, settings, project_label)
        source_signature = _pipeline_source_signature(settings, cleaned_scenes)
        resume_snapshot = _resume_snapshot(job_id)
        reuse_checkpoints = (
            resume_mode in {"resume", "restart_stage", "audit_approved"}
            or bool(resume_snapshot.get("resume_available"))
        )
        if reuse_checkpoints and resume_mode not in {"resume", "restart_stage", "audit_approved"}:
            update_job(
                job_id, status="preparing", stage="Checkpoint Recovery", progress=1,
                message="Reusable pipeline checkpoints detected; resuming from the first invalid stage",
            )

        # Fastest and safest path: if the exact frozen renderer plan is valid, use
        # it as the sole authority and skip all earlier planning/download stages.
        resolved_scenes = _load_resolved_job_plan(job_dir, source_signature, settings) if reuse_checkpoints else None
        if resolved_scenes is not None:
            update_job(
                job_id,
                status="preparing",
                stage="Resolved Plan Reuse",
                progress=35,
                message=f"Reusing frozen resolved_job_plan.json with {len(resolved_scenes)} validated scene(s)",
            )
        else:
            timed_scenes = _load_timing_checkpoint(job_dir, source_signature, settings) if reuse_checkpoints else None
            if timed_scenes is not None:
                cleaned_scenes = timed_scenes
                update_job(
                    job_id, status="preparing", stage="Whisper Reuse", progress=14,
                    message=f"Reusing validated Whisper/timing checkpoint for {len(cleaned_scenes)} render shot(s)",
                )
            else:
                with WORKERS.lane("transcribe", job_id, project_label, "Whisper / caption timing"):
                    cleaned_scenes = _prepare_captions_and_timing(
                        job_id,
                        settings,
                        cleaned_scenes,
                    )
                _save_timing_checkpoint(job_dir, source_signature, settings, cleaned_scenes)

            # V17.4 true pre-narration intro is a separate deterministic stage.
            if bool(getattr(settings, "intro_enabled", False)):
                update_job(job_id, status="preparing", stage="Intro Plan", progress=16, message="Building exact pre-narration intro timeline from ordered Scene Plan queries")
            cleaned_scenes = _prepare_intro_checkpoint(job_dir, source_signature, settings, cleaned_scenes, reuse_checkpoints)

            edit_signature = _edit_signature(source_signature, settings, cleaned_scenes)
            saved_edit = _load_edit_checkpoint(job_dir, edit_signature) if reuse_checkpoints else None
            if saved_edit is not None:
                cleaned_scenes, transition_summary = saved_edit
                update_job(
                    job_id, status="preparing", stage="Edit Direction Reuse", progress=29,
                    message="Reusing frozen transition/edit-direction checkpoint",
                )
            else:
                update_job(
                    job_id,
                    status="preparing",
                    stage="Edit Direction",
                    progress=29,
                    message="Resolving Outer-AI editorial intent through the selected technical preset",
                )
                transition_mode = (
                    str(settings.transition_engine_mode or "auto_editorial")
                    if bool(settings.transition_engine_enabled)
                    else "off"
                )
                cleaned_scenes, transition_summary = assign_auto_transitions(
                    cleaned_scenes,
                    mode=transition_mode,
                    preset=str(settings.transition_preset or "documentary"),
                    intensity=int(settings.transition_intensity_percent or 0),
                    base_seconds=float(settings.transition_base_seconds or 0.28),
                    project_seed=f"{job_id}:{project_label}",
                    hook_seconds=(
                        float(settings.opening_hook_seconds or 0.0)
                        if bool(settings.transition_hook_cut_bias)
                        else 0.0
                    ),
                    no_repeat=bool(settings.transition_no_repeat),
                )
                cleaned_scenes = _resolve_editorial_scene_technical_fields(cleaned_scenes, settings)
                _save_edit_checkpoint(job_dir, edit_signature, cleaned_scenes, transition_summary)

            metadata = dict(settings.metadata or {})
            metadata["scene_sync_plan"] = [dict(scene) for scene in cleaned_scenes]
            metadata["transition_direction"] = transition_summary
            metadata["transition_direction_authoritative"] = True
            metadata["editing_authority"] = {
                "outer_ai": "editorial_intent_and_media_planning",
                "preset_settings": "technical_editing_rules",
                "resolver": "single_resolution_before_render",
            }
            settings.metadata = metadata

            # V17.5 retention gate: resolve only visuals intersecting final 00:00-02:00
            # and release this pipeline worker while the project waits for human review.
            cleaned_scenes, audit_should_stop = _prepare_first2_visual_audit(
                job_id, job_dir, source_signature, settings, cleaned_scenes
            )
            if audit_should_stop:
                return

            # Resolve permanent-library audio choices once, freeze them, then let
            # FFmpeg execute the plan. Missing optional assets never fail video.
            if bool(settings.music_enabled):
                update_job(job_id, status="preparing", stage="Music Plan", progress=29, message="Resolving preset music playlist from permanent VPS library")
            _resolve_music_plan_checkpoint(job_dir, source_signature, settings, project_label, reuse_checkpoints)
            settings.render_audio_path = ""
            needs_audio_mix = (
                bool(settings.music_enabled)
                or bool(getattr(settings, "intro_enabled", False))
                or int(settings.voice_volume_percent or 0) != 100
                or str(settings.voice_processing_preset or "off").lower() != "off"
            )
            audio_signature = _audio_signature(source_signature, settings)
            reused_audio = _load_audio_checkpoint(job_dir, audio_signature, settings, needs_audio_mix) if reuse_checkpoints else False
            if reused_audio and needs_audio_mix:
                update_job(
                    job_id, status="preparing", stage="Audio Mix Reuse", progress=34,
                    message="Reusing validated final audio mix checkpoint",
                )
            elif needs_audio_mix:
                update_job(
                    job_id,
                    status="preparing",
                    stage="Audio Mix",
                    progress=30,
                    message=(
                        "Building intro-safe narration and ordered background-music layers"
                        if bool(settings.music_enabled)
                        else "Preparing narration audio with intro timing"
                    ),
                )

                def audio_mix_progress(progress: int, message: str) -> None:
                    if cancelled(job_id):
                        raise RuntimeError("Remote render cancelled.")
                    mapped = 30 + int(max(0, min(100, int(progress))) * 0.04)
                    update_job(
                        job_id,
                        status="preparing",
                        stage="Audio Mix",
                        progress=min(34, mapped),
                        message=str(message),
                    )

                mixed_audio = prepare_final_audio(
                    settings,
                    progress_callback=audio_mix_progress,
                    cancel_callback=lambda: cancelled(job_id),
                    output_tag="v17_deterministic_mix",
                )
                if mixed_audio is not None:
                    settings.render_audio_path = mixed_audio.path
                    metadata = dict(settings.metadata or {})
                    metadata["audio_mix"] = {
                        "music_enabled": bool(mixed_audio.used_music),
                        "continuous_second_layer": bool(mixed_audio.used_music),
                        "music_volume_percent": int(settings.music_volume_percent),
                        "ducking_enabled": bool(settings.music_ducking_enabled),
                        "duration": float(mixed_audio.duration),
                        "intro_silence_seconds": float(getattr(settings, "intro_duration_seconds", 0) or 0) if bool(getattr(settings, "intro_enabled", False)) else 0.0,
                        "resolved_playlist": list((settings.metadata or {}).get("resolved_music_playlist", []) or []),
                    }
                    settings.metadata = metadata
                _save_audio_checkpoint(job_dir, audio_signature, settings)

            # Resolve intro footage independently from documentary footage.
            # This prevents any documentary neighbor fallback from leaking into the
            # intro and keeps all narration/Whisper scene timing untouched.
            metadata = dict(settings.metadata or {})
            planned_intro = [dict(x) for x in metadata.get("intro_scenes", []) if isinstance(x, dict)]
            resolved_intro_scenes: list[dict[str, Any]] = []
            if planned_intro:
                sml_intro_enabled = (
                    str(metadata.get("media_source_mode") or "") == "sml_first"
                    and bool(metadata.get("sml_auto_scene_matching", False))
                )
                for intro_scene in planned_intro:
                    intro_scene["_sml_first"] = sml_intro_enabled
                    intro_scene.setdefault("pexels_allow_video", bool(getattr(settings, "use_video_backgrounds", True)))
                    intro_scene.setdefault("pexels_prefer_landscape", bool(getattr(settings, "pexels_prefer_landscape", True)))
                resolved_intro_scenes = _load_saved_intro_media(job_dir, planned_intro) if reuse_checkpoints else None
                if resolved_intro_scenes is None:
                    _ensure_disk_space("intro media acquisition")
                    with WORKERS.lane("pexels", job_id, project_label, "Ordered intro Pexels search / download"):
                        resolved_intro_scenes = _resolve_intro_stock_media(
                            job_id, planned_intro, job_dir,
                            target_width=int(settings.width), target_height=int(settings.height),
                            intro_queries=[dict(x) for x in metadata.get("intro_queries", []) if isinstance(x, dict)],
                        )
                else:
                    update_job(job_id, status="preparing", stage="Intro Media Reuse", progress=34, message=f"Reusing {len(resolved_intro_scenes)} validated intro media files")

            resolved_scenes = _load_saved_media(job_dir, cleaned_scenes) if reuse_checkpoints else None
            if resolved_scenes is not None:
                update_job(
                    job_id, status="preparing", stage="Media Reuse", progress=35,
                    message=f"Reusing {len(resolved_scenes)} validated documentary media files",
                )
            else:
                _ensure_disk_space("media acquisition")
                with WORKERS.lane("pexels", job_id, project_label, "Pexels search / download"):
                    resolved_scenes = _resolve_scene_stock_media(
                        job_id,
                        cleaned_scenes,
                        job_dir,
                        target_width=int(settings.width),
                        target_height=int(settings.height),
                    )

            metadata = dict(settings.metadata or {})
            metadata["resolved_intro_scenes"] = [dict(scene) for scene in (resolved_intro_scenes or [])]
            metadata["scene_sync_plan"] = [dict(scene) for scene in resolved_scenes]
            metadata["scene_sync_plan_authoritative"] = True
            metadata["documentary_timing_shifted_for_intro"] = False
            metadata["vps_stock_media_resolution"] = True
            settings.metadata = metadata

            # Resolve SFX after actual intro media/query progression is known so
            # intro ambience follows the query that really won, not a failed one.
            if bool(getattr(settings, "sfx_enabled", False)):
                update_job(job_id, status="preparing", stage="SFX Plan", progress=35, message="Resolving keyword-driven SFX from permanent VPS library")
            sfx_timeline = _final_timeline_scenes(settings, resolved_scenes)
            _adjusted_timeline, _resolved_sfx = _resolve_sfx_plan_checkpoint(job_dir, source_signature, settings, sfx_timeline, reuse_checkpoints)
            local_transition_codes = {
                str(code) for event in _resolved_sfx.get("transition", []) if isinstance(event, dict)
                for code in event.get("scene_codes", [])
            }
            if local_transition_codes:
                cleaned_main=[]
                for scene in resolved_scenes:
                    row=dict(scene)
                    if str(row.get("scene_code") or "") in local_transition_codes:
                        row["transition_sfx"]="none"; row["sfx_volume"]=0.0
                    cleaned_main.append(row)
                resolved_scenes=cleaned_main
                metadata=dict(settings.metadata or {})
                intro_fixed=[]
                for scene in metadata.get("resolved_intro_scenes", []) or []:
                    if not isinstance(scene, dict):
                        continue
                    row=dict(scene)
                    if str(row.get("scene_code") or "") in local_transition_codes:
                        row["transition_sfx"]="none"; row["sfx_volume"]=0.0
                    intro_fixed.append(row)
                metadata["resolved_intro_scenes"]=intro_fixed
                metadata["scene_sync_plan"]=[dict(scene) for scene in resolved_scenes]
                settings.metadata=metadata

            resolved_plan_path = _save_resolved_job_plan(job_dir, source_signature, settings, resolved_scenes)
            metadata = dict(settings.metadata or {})
            metadata["resolved_job_plan"] = str(resolved_plan_path)
            metadata["resolved_job_plan_frozen"] = True
            settings.metadata = metadata

        render_started = time.monotonic()

        def render_progress(progress: int, message: str) -> None:
            if cancelled(job_id): raise RuntimeError("Remote render cancelled.")
            mapped = 35 + int(max(0, min(100, int(progress))) * 0.64)
            elapsed = max(0.0, time.monotonic() - render_started); eta_text = ""
            if 2 <= int(progress) < 100 and elapsed >= 5:
                eta_seconds = int(elapsed * (100 - int(progress)) / max(1, int(progress)))
                eta_text = f" · estimated {max(1, round(eta_seconds / 60))} min remaining" if eta_seconds >= 60 else f" · estimated {max(1, eta_seconds)} sec remaining"
            _render_db_update(job_id,status="rendering",stage="Rendering",progress=min(99,mapped),message=str(message)+eta_text)

        def render_checkpoint(state: dict[str, Any]) -> None:
            last_scene=int(state.get("last_completed_scene") or 0); total_scenes=int(state.get("scene_count") or len(resolved_scenes))
            completed_chunks=len(state.get("completed_chunks") or []); total_chunks=int(state.get("total_chunks") or 0)
            _render_db_update(job_id,exact_stage=f"render_checkpoint_scene_{last_scene}",stage_state_json=json.dumps({"resume":{"last_completed_scene":last_scene,"scene_count":total_scenes,"completed_chunks":completed_chunks,"total_chunks":total_chunks,"final_ready":bool(state.get("final_ready")),"assembly_mode":str(state.get("assembly_mode") or ""),"final_video_mode":str(state.get("final_video_mode") or ""),"render_profile":"chunk_only_resume"}},separators=(",",":")))

        _ensure_disk_space("render start")
        update_job(job_id,status="ready_to_render",stage="Ready to Render",progress=35,message=f"Ready to render {len(resolved_scenes)} resolved scene(s)")
        render_lane=None; render_lane_open=False
        while True:
            _wait_for_manual_start(job_id)
            render_lane = WORKERS.lane("render", job_id, project_label, "FFmpeg video render")
            render_lane.__enter__(); render_lane_open=True
            # A cancellation can arm manual Start while this project is already
            # blocked on the render semaphore. Recheck after acquisition so it
            # cannot slip into FFmpeg automatically.
            if _manual_start_required(job_id):
                render_lane.__exit__(None,None,None); render_lane_open=False
                update_job(job_id,status="ready_to_render",stage="Ready to Render",progress=35,message="Ready to render · Start required after previous cancellation")
                continue
            break
        drive_lane=None; drive_lane_open=False
        try:
            if cancelled(job_id): raise RuntimeError("Remote render cancelled.")
            resolved_intro_scenes = [dict(x) for x in (settings.metadata or {}).get("resolved_intro_scenes", []) if isinstance(x, dict)]
            if bool(getattr(settings, "intro_enabled", False)) and resolved_intro_scenes:
                update_job(job_id,status="rendering",stage="Rendering Intro + Documentary",progress=35,message=f"Rendering separate intro plus {len(resolved_scenes)} documentary scene(s)")
                _render_true_separate_intro(
                    job_id=job_id, job_dir=job_dir, settings=settings,
                    resolved_main_scenes=resolved_scenes, resolved_intro_scenes=resolved_intro_scenes,
                    render_progress_callback=render_progress, render_checkpoint_callback=render_checkpoint,
                    reuse_checkpoints=reuse_checkpoints,
                )
            else:
                update_job(job_id,status="rendering",stage="Rendering",progress=35,message=f"Rendering {len(resolved_scenes)} resolved scene(s)")
                render_mixed_media(settings,progress_callback=render_progress,cancel_callback=lambda: cancelled(job_id),checkpoint_callback=render_checkpoint)
            _flush_render_db_update(job_id)
            result=Path(settings.output_path)
            if not result.exists() or result.stat().st_size < 1024: raise RuntimeError("Renderer did not create a valid MP4")
            if COMPRESSOR_ENABLED:
                with WORKERS.lane("compressor",job_id,project_label,"Final video compression"):
                    result=_compress_output_if_enabled(job_id,result)
            result_size=result.stat().st_size
            project_name=str(get_job(job_id).get("project_name") or settings.project_name or "Untitled")
            channel_name=str((settings.metadata or {}).get("web_channel") or "").strip()
            update_job(job_id,output_file=str(result),size_bytes=result_size)
            drive_destination=""; drive_view_url=""; output_filename=drive_output_filename(project_name); drive_status=""
            # Presets may disable Drive delivery in Normal mode. Verified Serial
            # always forces it because its release contract is Drive Verified + Cleanup Verified.
            drive_required = bool(DRIVE_UPLOAD_ENABLED) and (
                bool(getattr(settings, "drive_upload_enabled", True)) or _verified_serial_owns(job_id)
            )
            if drive_required:
                # Backpressure: a completed render keeps the single render slot until
                # the single Drive uploader becomes available. This prevents a fourth
                # storage-heavy project from accumulating a second finished MP4.
                if cancelled(job_id): raise RuntimeError("Remote render cancelled.")
                update_job(job_id,status="ready_to_upload",stage="Waiting for Drive",progress=99,message="Render complete · waiting for Drive uploader")
                drive_lane=WORKERS.lane("drive_uploader",job_id,project_label,"Google Drive upload")
                drive_lane.__enter__(); drive_lane_open=True
                render_lane.__exit__(None,None,None); render_lane_open=False
                drive_destination,drive_view_url,output_filename=upload_output_to_drive(job_id,project_name,result,channel_name)
                drive_status="completed"
                if cancelled(job_id): raise RuntimeError("Remote render cancelled.")
            else:
                render_lane.__exit__(None,None,None); render_lane_open=False
            message="Render completed" if not drive_destination else f"Render completed and transferred to {drive_destination}"
            update_job(job_id,status="completed",stage="Completed",progress=100,message=message,output_file=str(result),size_bytes=result_size,completed_at=now_iso(),drive_status=drive_status,drive_destination=drive_destination,drive_view_url=drive_view_url,output_filename=output_filename,resume_mode="",manual_start_required=0)
            _record_pipeline_success()
            cleanup_delivery_safe=(not drive_required) or drive_status=="completed"
            # Auto Clean behavior is identical in Normal and Verified Serial.
            # Queue mode changes scheduling only; an intentionally confirmed OFF
            # leaves the delivered project local until the operator uses Clean Now.
            auto_clean_delivered=bool(getattr(settings,"auto_clean_project_after_drive",True)) and drive_status=="completed"
        finally:
            if render_lane_open: render_lane.__exit__(None,None,None)
            if drive_lane_open and drive_lane is not None: drive_lane.__exit__(None,None,None)
        if auto_clean_delivered:
            # Storage-first policy: once the existing Drive size/hash verification has
            # succeeded, delete project-owned local bytes immediately. Public-link
            # creation is best-effort and runs only after space has already been freed.
            cleaned=False
            with WORKERS.lane("cleanup", job_id, project_label, "Verified Drive project cleanup"):
                cleaned=_auto_clean_delivered_project(job_id)
            if cleaned:
                drive_view_url, viewer_warning = _attempt_public_drive_viewer(job_id, drive_destination, drive_view_url)
                if viewer_warning:
                    current=get_job(job_id)
                    update_job(job_id, message=(str(current.get("message") or "Drive delivery verified; all local project data removed") + " · " + viewer_warning)[-1800:])
        elif AUTO_CLEAN_MEDIA_AFTER_RENDER and cleanup_delivery_safe:
            with WORKERS.lane("cleanup", job_id, project_label, "Project media cleanup"):
                _cleanup_transient_job_media(job_id)
    except Exception as exc:
        error_path = job_dir / "error.log"
        error_path.write_text(traceback.format_exc(), "utf-8")
        if cancelled(job_id) or "cancel" in str(exc).lower():
            update_job(
                job_id,
                status="cancelled",
                stage="Cancelled",
                message="Remote render cancelled.",
                error_log=str(error_path),
                cancelled_at=now_iso(),
            )
            _mark_next_job_manual_start(job_id)
        else:
            failed_job = get_job(job_id)
            previous_state: dict[str, Any] = {}
            try:
                previous_state = json.loads(str(failed_job.get("stage_state_json") or "{}"))
                if not isinstance(previous_state, dict):
                    previous_state = {}
            except Exception:
                previous_state = {}
            previous_state["failure"] = {
                "stage": str(failed_job.get("stage") or ""),
                "exact_stage": str(failed_job.get("exact_stage") or ""),
                "message": str(exc),
                "failed_at": now_iso(),
            }
            failure_count, circuit_paused = _record_pipeline_failure(failed_job)
            failure_message = str(exc)
            if circuit_paused:
                failure_message += f" · pipeline paused after {failure_count} consecutive failures at {_failure_stage_key(failed_job)}"
            update_job(
                job_id,
                status="failed",
                stage="Failed",
                message=failure_message,
                error_log=str(error_path),
                max_retries=0,
                stage_state_json=json.dumps(previous_state, separators=(",", ":")),
            )


def worker_loop() -> None:
    while not _stop.is_set():
        job_id = None
        with _db_lock, db() as connection:
            row = None
            if not _pipeline_paused():
                mode = _runtime_value_conn(connection, "queue_mode", QUEUE_MODE_NORMAL).strip().lower()
                if mode == QUEUE_MODE_VERIFIED_SERIAL:
                    # Verified Serial never begins new work without Drive verification
                    # available, because successful Drive + cleanup is the release gate.
                    if DRIVE_UPLOAD_ENABLED:
                        owner = _serial_reconcile_conn(connection)
                        if owner:
                            current = connection.execute("SELECT id,status,cancel_requested FROM jobs WHERE id=?", (owner,)).fetchone()
                            if current and str(current["status"]) == "queued" and not int(current["cancel_requested"] or 0) and _preparation_slot_available(connection):
                                row = current
                        else:
                            placeholders = ",".join("?" for _ in SERIAL_BUSY_STATUSES)
                            busy_count = int(connection.execute(
                                f"SELECT COUNT(*) AS n FROM jobs WHERE cancel_requested=0 AND status IN ({placeholders})",
                                tuple(sorted(SERIAL_BUSY_STATUSES)),
                            ).fetchone()["n"] or 0)
                            if busy_count == 0 and _preparation_slot_available(connection):
                                row = connection.execute(
                                    "SELECT id,status,cancel_requested FROM jobs WHERE status='queued' AND cancel_requested=0 "
                                    "ORDER BY priority ASC, CASE WHEN audit_approved_at<>'' THEN audit_approved_at ELSE created_at END ASC LIMIT 1"
                                ).fetchone()
                                if row:
                                    _runtime_set_conn(connection, "serial_active_job_id", str(row["id"]))
                elif _preparation_slot_available(connection):
                    row = connection.execute(
                        "SELECT id,status,cancel_requested FROM jobs WHERE status='queued' AND cancel_requested=0 "
                        "ORDER BY priority ASC, CASE WHEN audit_approved_at<>'' THEN audit_approved_at ELSE created_at END ASC LIMIT 1"
                    ).fetchone()
            if row:
                job_id = str(row["id"])
                claimed = connection.execute(
                    "UPDATE jobs SET status='preparing',stage='Claimed',"
                    "message='Worker claimed job',updated_at=? "
                    "WHERE id=? AND status='queued'",
                    (now_iso(), job_id),
                )
                if claimed.rowcount != 1:
                    job_id = None
        if job_id:
            process_job(job_id)
            continue
        _wake.wait(3)
        _wake.clear()


def _sb2_web_html(view: str, forwarded_prefix: str) -> str:
    view = (view or "dashboard").strip("/").lower() or "dashboard"
    if view == "live":
        view = "dashboard"
    if view not in {"dashboard", "queue", "settings", "admin"}:
        view = "dashboard"
    raw_prefix = (forwarded_prefix or "").strip().rstrip("/")
    public_prefix = raw_prefix if raw_prefix.startswith("/") and "<" not in raw_prefix and ">" not in raw_prefix else ""
    ui_prefix = public_prefix or "/web"
    api_prefix = (public_prefix + "/api") if public_prefix else ""
    esc_ui = html.escape(ui_prefix, quote=True)
    esc_api = html.escape(api_prefix, quote=True)
    nav = [
        ("dashboard", "SB2"),
        ("queue", "SB2 Queue"),
        ("settings", "SB2 Settings"),
        ("admin", "SB2 Admin"),
    ]
    nav_html = "".join(
        f'<a class="{"active" if key == view else ""}" href="{esc_ui}/{"" if key == "dashboard" else key}">{label}</a>'
        for key, label in nav
    )
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Scene Builder 2</title>
<style>
:root{{--bg:#f5f7fb;--panel:#fff;--ink:#101828;--muted:#667085;--line:#e4e7ec;--nav:#101828;--good:#067647;--bad:#b42318}}
*{{box-sizing:border-box}}body{{margin:0;font:14px Inter,system-ui,-apple-system,Segoe UI,sans-serif;background:var(--bg);color:var(--ink)}}
.shell{{display:grid;grid-template-columns:220px 1fr;min-height:100vh}}aside{{background:var(--nav);color:#fff;padding:22px 14px}}.brand{{font-size:18px;font-weight:800;padding:4px 8px 20px}}.brand small{{display:block;color:#98a2b3;font-size:11px;margin-top:4px}}nav a{{display:block;color:#d0d5dd;text-decoration:none;padding:10px 11px;border-radius:8px;margin:3px 0;font-weight:650}}nav a:hover,nav a.active{{background:#344054;color:#fff}}
main{{padding:24px;min-width:0}}header{{display:flex;justify-content:space-between;gap:16px;align-items:flex-start;margin-bottom:18px}}h1{{margin:0;font-size:25px}}.sub{{color:var(--muted);margin-top:5px}}.pill{{border:1px solid var(--line);background:#fff;border-radius:999px;padding:7px 10px;color:var(--muted)}}
.grid{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}}.card{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;box-shadow:0 1px 2px #1018280a}}.wide{{grid-column:1/-1}}.metric strong{{display:block;font-size:25px;margin-top:8px}}.muted{{color:var(--muted)}}
table{{width:100%;border-collapse:collapse;font-size:12px}}th,td{{padding:9px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}th{{color:#475467;font-size:10px;text-transform:uppercase;letter-spacing:.35px}}.status{{font-weight:750}}button{{border:1px solid #d0d5dd;border-radius:7px;background:#fff;padding:6px 9px;cursor:pointer;font-weight:650}}button:hover{{background:#f9fafb}}button.danger{{color:var(--bad)}}pre{{white-space:pre-wrap;overflow:auto;background:#101828;color:#e4e7ec;padding:14px;border-radius:9px;max-height:430px}}.ok{{color:var(--good)}}.bad{{color:var(--bad)}}
@media(max-width:900px){{.shell{{grid-template-columns:1fr}}aside{{padding:10px}}.brand{{padding-bottom:8px}}nav{{display:flex;gap:4px;overflow:auto}}nav a{{white-space:nowrap}}.grid{{grid-template-columns:repeat(2,1fr)}}main{{padding:14px}}}}@media(max-width:560px){{.grid{{grid-template-columns:1fr}}}}
</style></head>
<body data-api="{esc_api}" data-view="{html.escape(view, quote=True)}"><div class="shell"><aside><div class="brand">Scene Builder 2<small>VPS-2 production engine</small></div><nav>{nav_html}</nav></aside><main>
<header><div><h1>Scene Builder 2</h1><div class="sub">Dedicated editing and rendering execution engine</div></div><div class="pill" id="health-pill">Checking VPS-2…</div></header>
<section class="grid"><div class="card metric"><span class="muted">Worker</span><strong id="worker">—</strong></div><div class="card metric"><span class="muted">Queued</span><strong id="queued">—</strong></div><div class="card metric"><span class="muted">Running</span><strong id="running">—</strong></div><div class="card metric"><span class="muted">Completed</span><strong id="completed">—</strong></div></section>
<section class="card wide" style="margin-top:12px" id="content"><div class="muted">Loading…</div></section>
</main></div>
<script>
const API=document.body.dataset.api||''; const VIEW=document.body.dataset.view||'dashboard';
async function req(path,opts={{}}){{const r=await fetch(API+path,{{cache:'no-store',...opts}});if(!r.ok)throw new Error(`HTTP ${{r.status}}: ${{await r.text()}}`);const ct=r.headers.get('content-type')||'';return ct.includes('json')?r.json():r.text();}}
function esc(v){{return String(v??'').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}}[c]));}}
function counts(h){{const c=h.queue_counts||{{}};document.querySelector('#worker').textContent=h.worker_api_version?`API v${{h.worker_api_version}}`:'Online';document.querySelector('#queued').textContent=c.queued||0;document.querySelector('#running').textContent=(c.running||0)+(c.rendering||0)+(c.preparing||0)+(c.downloading_media||0)+(c.ready_to_render||0)+(c.ready_to_upload||0)+(c.drive_uploading||0);document.querySelector('#completed').textContent=c.completed||0;}}
async function health(){{try{{const h=await req('/health');counts(h);const p=document.querySelector('#health-pill');p.textContent=`VPS-2 Online • ${{h.worker_build||''}}`;p.classList.add('ok');return h;}}catch(e){{const p=document.querySelector('#health-pill');p.textContent='VPS-2 Offline / Route Error';p.classList.add('bad');throw e;}}}}
function actionButton(id,status,manualStart){{let a=[];if(status==='ready_to_render'&&Number(manualStart||0)===1)a.push(`<button onclick="act('${{id}}','start')">Start</button>`);else if(!['completed','cleaned','failed','cancelled'].includes(status))a.push(`<button class="danger" onclick="act('${{id}}','cancel')">Cancel</button>`);if(['failed','cancelled'].includes(status)){{a.push(`<button onclick="act('${{id}}','resume')">Resume</button>`);a.push(`<button onclick="act('${{id}}','restart-stage')">Restart Stage</button>`);a.push(`<button class="danger" onclick="act('${{id}}','restart')">Restart Job</button>`);}}return a.join(' ');}}
async function act(id,op){{if(!confirm(`${{op}} job ${{id.slice(0,8)}}?`))return;await req(`/jobs/${{id}}/${{op}}`,{{method:'POST'}});await render();}}
async function queueView(){{const d=await req('/jobs?limit=200');const jobs=d.jobs||[];return `<h2>VPS-2 Render Queue</h2><div class="muted" style="margin-bottom:10px">Persistent Scene Builder queue on VPS-2. ACS queue on VPS-1 is untouched.</div><div style="overflow:auto"><table><thead><tr><th>Project</th><th>Status</th><th>Stage</th><th>Progress</th><th>Message</th><th>Action</th></tr></thead><tbody>${{jobs.map(j=>`<tr><td><b>${{esc(j.project_name)}}</b><br><span class="muted">${{esc(j.id).slice(0,12)}}</span></td><td class="status">${{esc(j.status)}}</td><td>${{esc(j.stage)}}</td><td>${{esc(j.progress)}}%</td><td>${{esc(j.message)}}</td><td>${{actionButton(j.id,j.status,j.manual_start_required)}}</td></tr>`).join('')||'<tr><td colspan="6" class="muted">No jobs.</td></tr>'}}</tbody></table></div>`;}}
async function settingsView(h){{return `<h2>SB2 Runtime Settings</h2><table><tr><th>Worker build</th><td>${{esc(h.worker_build)}}</td></tr><tr><th>API version</th><td>${{esc(h.worker_api_version)}}</td></tr><tr><th>Pexels keys</th><td>${{esc(h.pexels_key_count)}}</td></tr><tr><th>Drive upload</th><td>${{h.drive_upload_enabled?'Enabled':'Disabled'}}</td></tr><tr><th>Drive remote</th><td>${{esc(h.drive_remote||'—')}}</td></tr></table><p class="muted">Secrets are never displayed. Persistent configuration remains under /etc/scene-builder on VPS-2.</p>`;}}
async function adminView(h){{const q=await req('/jobs?limit=20');return `<h2>SB2 Admin / Audit</h2><p><b>VPS-2 worker:</b> <span class="ok">online</span></p><p><b>Recent jobs:</b> ${{(q.jobs||[]).length}}</p><p class="muted">This page controls only the Scene Builder editing engine.</p>`;}}
async function dashboardView(h){{return `<h2>SB2 Production</h2><p>The Scene Builder editing engine handles transcription, media acquisition, rendering, compression, Drive delivery and cleanup.</p><table><tr><th>Queue state</th><td>${{esc(JSON.stringify(h.queue_counts||{{}}))}}</td></tr><tr><th>Creative engine</th><td>LUT grading, Transition Director, captions, quality pipeline and renderer preserved</td></tr></table>`;}}
async function render(){{const box=document.querySelector('#content');try{{const h=await health();let out='';if(VIEW==='queue')out=await queueView();else if(VIEW==='settings')out=await settingsView(h);else if(VIEW==='admin')out=await adminView(h);else out=await dashboardView(h);box.innerHTML=out;}}catch(e){{box.innerHTML=`<h2>SB2 unavailable</h2><p class="bad">${{esc(e.message)}}</p><p>Check the Scene Builder editing engine route and container.</p>`;}}}}
render(); setInterval(()=>{{if(VIEW!=='queue')health().catch(()=>{{}});}},10000);
</script></body></html>'''


@app.get("/web", response_class=HTMLResponse)
@app.get("/web/", response_class=HTMLResponse)
@app.get("/web/{view:path}", response_class=HTMLResponse)
def sb2_web(request: Request, view: str = "dashboard"):
    auth(None)
    clean_view = (view or "dashboard").strip("/").lower() or "dashboard"
    if clean_view == "admin" and str(request.headers.get("x-app-access") or "").strip().lower() != "both":
        raise HTTPException(403, "Administrator access required")
    return HTMLResponse(_sb2_web_html(clean_view, str(request.headers.get("x-forwarded-prefix") or "")))


@app.on_event("startup")
def startup() -> None:
    init_db()
    if WORKER_ADMISSION_ONLY:
        # Create isolated schema; do not start queue-processing or cleanup loops.
        return
    for index in range(PIPELINE_WORKERS):
        threading.Thread(
            target=worker_loop,
            daemon=True,
            name=f"project-pipeline-{index + 1}",
        ).start()
    threading.Thread(target=_retention_loop, daemon=True, name="retention-sweeper").start()
    _wake.set()


@app.get("/health")
def health():
    with _db_lock, db() as connection:
        counts = {
            row["status"]: row["count"]
            for row in connection.execute(
                "SELECT status,COUNT(*) count FROM jobs GROUP BY status"
            )
        }
    return {
        "ok": not WORKER_ADMISSION_ONLY,
        "admission_only": WORKER_ADMISSION_ONLY,
        "worker": "scene-builder-persistent-queue",
        "worker_api_version": WORKER_API_VERSION,
        "worker_build": WORKER_BUILD,
        "queue_counts": counts,
        "pexels_api_configured": bool(_current_pexels_keys()),
        "pexels_key_count": len(_current_pexels_keys()),
        "drive_upload_enabled": DRIVE_UPLOAD_ENABLED,
        "drive_remote": DRIVE_REMOTE if DRIVE_UPLOAD_ENABLED else "",
        "pipeline_workers": PIPELINE_WORKERS,
        "auto_clean_media_after_render": AUTO_CLEAN_MEDIA_AFTER_RENDER,
        "compressor_enabled": COMPRESSOR_ENABLED,
        "render_profile": "chunk_only_resume",
        "chunk_assembly": "stream_copy",
        "internal_audio_engine": True,
        "internal_audio_engine_version": INTERNAL_AUDIO_ENGINE_VERSION,
        "internal_audio_engine_mode": "worker_module",
        "internal_audio_studio_api": True,
        "optional_continuous_music": True,
        "intro_pre_narration": True,
        "separate_intro_render": True,
        "intro_render_checkpoint": True,
        "documentary_timing_preserved": True,
        "ordered_intro_query_failover": True,
        "ai_music_optional": True,
        "environment_sfx_narration_ducking": True,
        "permanent_audio_library": True,
        "project_scoped_pexels_media": True,
        "dynamic_pexels_key_file": str(PEXELS_KEYS_FILE),
        "keyword_sfx_resolver": True,
        "verified_drive_project_autoclean": True,
        "first2_visual_audit": True,
        "audit_waiting_releases_slots": True,
        "professional_transition_director": True,
        "caption_preview_contract": "renderer-matched-v17",
        "automatic_job_retries": 0,
        "recovery_actions": ["resume", "restart_stage", "restart_job", "start_ready"],
        "true_cut_engine": True,
        "resolved_plan_authority": "resolved_job_plan.json",
        "english_caption_fonts": 10,
        "caption_styles": ["karaoke", "boxed", "minimal", "bold", "documentary", "social_pop"],
        "lut_count": 23,
        "engine_stats_endpoint": "/engine/stats",
        "pipeline_paused": _pipeline_paused(),
        "pipeline_pause": _pipeline_pause_state(),
        **_queue_mode_state(),
        "verified_serial_queue": True,
        "serial_release_contract": "drive_verified+cleanup_verified",
        "failure_circuit": json.loads(_runtime_state_get("failure_circuit", "{}") or "{}"),
        "failure_circuit_breaker_threshold": FAILURE_CIRCUIT_BREAKER_THRESHOLD,
        "minimum_free_space_gb": MIN_FREE_SPACE_GB,
    }


# SCENE_INTERNAL_AUDIO_STUDIO_API_V1
INTERNAL_AUDIO_STUDIO_ROOT = BASE / "internal_audio_engine" / "studio"
INTERNAL_AUDIO_STUDIO_TTL_SECONDS = 24 * 60 * 60


def _internal_audio_studio_cleanup() -> None:
    INTERNAL_AUDIO_STUDIO_ROOT.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - INTERNAL_AUDIO_STUDIO_TTL_SECONDS
    for child in INTERNAL_AUDIO_STUDIO_ROOT.iterdir():
        try:
            if child.is_dir() and child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            continue


@app.get("/internal-audio/presets")
def internal_audio_presets_api(token: str | None = None):
    auth(token)
    return {"ok": True, "version": INTERNAL_AUDIO_ENGINE_VERSION, "presets": internal_audio_engine_presets()}


@app.post("/internal-audio/studio/process")
async def internal_audio_studio_process_api(request: Request, token: str | None = None):
    auth(token)
    _internal_audio_studio_cleanup()
    form = await request.form()
    upload = form.get("file") or form.get("files")
    if upload is None or not hasattr(upload, "read"):
        raise HTTPException(400, "Narration file is required")
    original_name = str(getattr(upload, "filename", "") or "narration.bin")
    suffix = Path(original_name).suffix.lower()
    allowed = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".aiff", ".aif", ".amr", ".ac3", ".mka", ".mp2", ".webm", ".mp4", ".mov", ".mkv", ".avi", ".mpeg", ".mpg", ".mts", ".m2ts"}
    if suffix not in allowed:
        suffix = ".bin"
    studio_id = uuid.uuid4().hex
    root = INTERNAL_AUDIO_STUDIO_ROOT / studio_id
    root.mkdir(parents=True, exist_ok=False)
    source = root / ("input" + suffix)
    output = root / "output.mp3"
    total = 0
    try:
        with source.open("wb") as handle:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD:
                    raise HTTPException(413, "Studio narration exceeds upload limit")
                handle.write(chunk)
        if total <= 0:
            raise HTTPException(400, "Narration file is empty")
        config = {
            "preset": str(form.get("preset") or "documentary"),
            "mode": str(form.get("mode") or "manual"),
            "noise_enabled": str(form.get("noise_enabled") or "0"),
        }
        for key in (
            "narration_start", "music_start", "music_fade_in", "narration_fade_in", "music_fade_out",
            "noise_floor", "noise_reduction", "highpass", "bass_gain", "mud_gain", "presence_gain", "air_gain",
            "deesser", "comp_threshold", "comp_ratio", "comp_attack", "comp_release", "comp_makeup",
            "target_lufs", "lra", "true_peak", "output_gain_db", "duck_db", "duck_attack", "duck_release",
            "music_level_db", "music_base_lufs",
        ):
            value = form.get(key)
            if value is not None and str(value).strip() != "":
                config[key] = str(value)
        result = internal_audio_engine_process(source, output, config=config)
        if not output.is_file() or output.stat().st_size < 1024:
            raise RuntimeError("Internal Audio Studio produced no valid output")
        meta = {
            "id": studio_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_name": original_name,
            "bytes": output.stat().st_size,
            "result": result,
        }
        (root / "result.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return {
            "ok": True,
            "id": studio_id,
            "version": INTERNAL_AUDIO_ENGINE_VERSION,
            "result": result,
            "output_url": f"/internal-audio/studio/{studio_id}/output",
        }
    except HTTPException:
        shutil.rmtree(root, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(root, ignore_errors=True)
        raise HTTPException(500, f"Internal Audio Studio failed: {exc}") from exc
    finally:
        try:
            await upload.close()
        except Exception:
            pass


@app.get("/internal-audio/studio/{studio_id}/output")
def internal_audio_studio_output_api(studio_id: str, token: str | None = None):
    auth(token)
    if not re.fullmatch(r"[0-9a-f]{32}", str(studio_id or "")):
        raise HTTPException(404, "Studio output not found")
    output = INTERNAL_AUDIO_STUDIO_ROOT / studio_id / "output.mp3"
    if not output.is_file() or output.stat().st_size < 1024:
        raise HTTPException(404, "Studio output not found")
    return FileResponse(output, media_type="audio/mpeg", filename=f"scene-builder-studio-{studio_id[:8]}.mp3")


@app.get("/engine/stats")
def engine_stats(token: str | None = None):
    auth(token)
    with _db_lock, db() as connection:
        counts = {row["status"]: row["count"] for row in connection.execute(
            "SELECT status,COUNT(*) count FROM jobs GROUP BY status"
        )}
    return {
        "ok": True,
        "engine": "scene-builder-vps-engine",
        "worker_api_version": WORKER_API_VERSION,
        "worker_build": WORKER_BUILD,
        "queue_counts": counts,
        "pipeline_workers": PIPELINE_WORKERS,
        "workers": WORKERS.snapshot(),
        "policies": {
            "auto_clean_media_after_render": AUTO_CLEAN_MEDIA_AFTER_RENDER,
            "compressor_enabled": COMPRESSOR_ENABLED,
            "drive_upload_enabled": DRIVE_UPLOAD_ENABLED,
            "render_profile": "chunk_only_resume",
            "chunk_assembly": "stream_copy",
            "optional_continuous_music": True,
            "minimum_free_space_gb": MIN_FREE_SPACE_GB,
            "failure_circuit_breaker_threshold": FAILURE_CIRCUIT_BREAKER_THRESHOLD,
        },
        "pipeline_pause": _pipeline_pause_state(),
        "queue_behavior": _queue_mode_state(),
        "failure_circuit": json.loads(_runtime_state_get("failure_circuit", "{}") or "{}"),
    }


@app.get("/queue/mode")
def get_queue_mode(token: str | None = None):
    auth(token)
    return {"ok": True, **_queue_mode_state()}


@app.post("/queue/mode")
async def set_queue_mode(request: Request, token: str | None = None):
    auth(token)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    mode = str((payload or {}).get("mode") or "").strip().lower()
    if mode not in {QUEUE_MODE_NORMAL, QUEUE_MODE_VERIFIED_SERIAL}:
        raise HTTPException(400, "Queue mode must be normal or verified_serial")
    if mode == QUEUE_MODE_VERIFIED_SERIAL and not DRIVE_UPLOAD_ENABLED:
        raise HTTPException(409, "Verified Serial requires Google Drive delivery verification to be enabled")
    with _db_lock, db() as connection:
        _runtime_set_conn(connection, "queue_mode", mode)
        if mode == QUEUE_MODE_NORMAL:
            _runtime_set_conn(connection, "serial_active_job_id", "")
        else:
            _serial_reconcile_conn(connection, adopt_single_existing=True)
    _wake.set()
    return {"ok": True, **_queue_mode_state()}


@app.post("/pipeline/resume")
def resume_pipeline(token: str | None = None):
    auth(token)
    _resume_pipeline_state()
    return {"ok": True, "pipeline_paused": False, "message": "Pipeline resumed; failure circuit reset"}


def _reset_job_from_zero(job_id: str, message: str) -> dict[str, Any]:
    job_dir = JOBS / job_id
    shutil.rmtree(job_dir / "output", ignore_errors=True)
    shutil.rmtree(job_dir / "resolved_media", ignore_errors=True)
    shutil.rmtree(job_dir / PIPELINE_CHECKPOINT_DIR, ignore_errors=True)
    for name in ("error.log", "media_resolution.json", "drive_upload.log", "resolved_job_plan.json"):

        try:
            (job_dir / name).unlink(missing_ok=True)
        except OSError:
            pass
    update_job(
        job_id,
        status="queued",
        stage="Queued",
        progress=0,
        message=message,
        started_at="",
        completed_at="",
        retry_count=0,
        max_retries=MAX_RETRIES,
        cancel_requested=0,
        output_file="",
        size_bytes=0,
        error_log="",
        drive_status="",
        drive_destination="",
        drive_view_url="",
        output_filename="",
        downloaded_at="",
        files_cleaned_at="",
        exact_stage="",
        stage_state_json="",
        cancelled_at="",
        cancel_source="",
        resume_mode="restart",
    )
    _wake.set()
    return get_job(job_id)


@app.post("/jobs")
async def create_job(
    file: UploadFile = File(...),
    token: str | None = None,
    priority: int = 100,
    client_job_key: str = "",
    submission_version: str = "",
    retry_failed: bool = False,
):
    auth(token)
    _ensure_disk_space("project submission")
    if not (file.filename or "").lower().endswith(".zip"):
        raise HTTPException(400, "Upload a ZIP file")

    if client_job_key:
        with _db_lock, db() as connection:
            existing_row = connection.execute(
                "SELECT * FROM jobs WHERE client_job_key=?",
                (client_job_key,),
            ).fetchone()
        if existing_row:
            existing = row_to_dict(existing_row)
            # Duplicate submission is deduplicated only. V17 never turns a submit
            # request into a hidden restart of a failed/cancelled job.
            response = existing
            response["deduplicated"] = True
            response["worker_build"] = WORKER_BUILD
            return response

    job_id = uuid.uuid4().hex
    job_dir = JOBS / job_id
    source_root = job_dir / "input"
    source_root.mkdir(parents=True)
    archive = job_dir / "project.zip"
    total = 0
    with archive.open("wb") as handle:
        while chunk := await file.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_UPLOAD:
                shutil.rmtree(job_dir, ignore_errors=True)
                raise HTTPException(413, "Upload too large")
            handle.write(chunk)
    try:
        _ensure_disk_space("project submission after upload")
    except RuntimeError as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(507, str(exc)) from exc
    try:
        with zipfile.ZipFile(archive) as package:
            safe_extract(package, source_root)
        candidates = list(source_root.rglob("project.json"))
        data = json.loads(candidates[0].read_text("utf-8")) if candidates else {}
        project_name = str(data.get("project_name") or "Untitled")[:200]
    except Exception as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(400, f"Invalid ZIP: {exc}") from exc

    created = now_iso()
    with _db_lock, db() as connection:
        connection.execute(
            "INSERT INTO jobs(id,project_name,status,stage,progress,message,priority,"
            "created_at,updated_at,max_retries,input_zip,client_job_key,submission_version) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                job_id,
                project_name,
                "queued",
                "Queued",
                0,
                f"Stored safely on VPS queue (worker {WORKER_BUILD})",
                int(priority),
                created,
                created,
                MAX_RETRIES,
                str(archive),
                client_job_key,
                submission_version,
            ),
        )
        connection.execute(
            "INSERT INTO job_events(job_id,created_at,status,stage,progress,message) VALUES(?,?,?,?,?,?)",
            (job_id, created, "queued", "Queued", 0, f"Stored safely on VPS queue (worker {WORKER_BUILD})"),
        )
    _wake.set()
    response = get_job(job_id)
    response["worker_build"] = WORKER_BUILD
    return response


@app.get("/jobs")
def list_jobs(
    token: str | None = None,
    limit: int = 200,
    status: str = "",
):
    auth(token)
    limit = max(1, min(1000, int(limit)))
    with _db_lock, db() as connection:
        if status:
            rows = connection.execute(
                "SELECT * FROM jobs WHERE status=? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return {
        "jobs": [_public_job(dict(row)) for row in rows],
        "worker_build": WORKER_BUILD,
        "worker_api_version": WORKER_API_VERSION,
    }


@app.get("/jobs/{job_id}")
def status(job_id: str, token: str | None = None):
    auth(token)
    return _public_job(get_job(job_id))


def _worker_job_log_text(job_id: str) -> str:
    job = get_job(job_id)
    lines = [
        "SCENE BUILDER JOB LOG", "=" * 72,
        f"Project: {job.get('project_name') or 'Untitled'}",
        f"Job ID: {job_id}",
        f"Status: {job.get('status') or ''}",
        f"Stage: {job.get('stage') or ''}",
        f"Progress: {job.get('progress') or 0}%",
        f"Message: {job.get('message') or ''}",
        f"Created: {job.get('created_at') or ''}",
        f"Started: {job.get('started_at') or ''}",
        f"Updated: {job.get('updated_at') or ''}",
        f"Completed: {job.get('completed_at') or ''}",
        f"Drive status: {job.get('drive_status') or ''}",
        f"Drive destination: {job.get('drive_destination') or ''}",
        f"Cleanup verified: {'yes' if job.get('files_cleaned_at') else 'no'}",
        f"Cleanup time: {job.get('files_cleaned_at') or ''}",
        "",
    ]
    with _db_lock, db() as connection:
        events = connection.execute(
            "SELECT created_at,status,stage,progress,message FROM job_events WHERE job_id=? ORDER BY id ASC LIMIT 1000",
            (job_id,),
        ).fetchall()
    if events:
        lines.extend(["EXECUTION TIMELINE", "-" * 72])
        for event in events:
            stamp = str(event["created_at"] or "")
            status = str(event["status"] or "")
            stage = str(event["stage"] or "")
            progress = int(event["progress"] or 0)
            message = str(event["message"] or "")
            detail = " · ".join(part for part in (status, stage, f"{progress}%", message) if part)
            lines.append(f"{stamp}  {detail}")
        lines.append("")

    job_dir = JOBS / job_id
    for label, name in (("ERROR / TRACEBACK", "error.log"), ("DRIVE UPLOAD", "drive_upload.log")):
        path = job_dir / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            text = f"Could not read {name}: {exc}"
        if len(text) > 200_000:
            text = text[-200_000:]
            text = "[tail only; log exceeded 200 KB]\n" + text
        lines.extend([label, "-" * 72, text.rstrip(), ""])
    if not events and not any((job_dir / name).is_file() for name in ("error.log", "drive_upload.log")):
        lines.extend(["No additional error or Drive-upload log file is present for this job.", ""])
    return "\n".join(lines)


@app.get("/jobs/{job_id}/log.txt")
def worker_job_log(job_id: str, token: str | None = None):
    auth(token)
    return PlainTextResponse(_worker_job_log_text(job_id), media_type="text/plain; charset=utf-8")


@app.post("/jobs/{job_id}/clean-now")
def clean_delivered_job(job_id: str, token: str | None = None):
    auth(token)
    current = get_job(job_id)
    if str(current.get("drive_status") or "") != "completed":
        raise HTTPException(409, "Drive verification is not complete")
    if str(current.get("files_cleaned_at") or "").strip() or str(current.get("status") or "") == "cleaned":
        return {"ok": True, "already_cleaned": True, **_queue_mode_state()}
    if str(current.get("status") or "") != "completed":
        raise HTTPException(409, "Only a completed Drive-verified project can be cleaned")
    project_label = str(current.get("project_name") or "Untitled")
    with WORKERS.lane("cleanup", job_id, project_label, "Manual verified Drive project cleanup"):
        cleaned = _auto_clean_delivered_project(job_id)
    _wake.set()
    if not cleaned:
        raise HTTPException(500, "Local project cleanup could not be verified")
    return {"ok": True, "already_cleaned": False, **_queue_mode_state()}


@app.post("/jobs/{job_id}/serial-retry-cleanup")
def serial_retry_cleanup(job_id: str, token: str | None = None):
    auth(token)
    if _queue_mode() != QUEUE_MODE_VERIFIED_SERIAL or _serial_active_job_id() != job_id:
        raise HTTPException(409, "This job does not own the Verified Serial queue")
    current = get_job(job_id)
    if str(current.get("drive_status") or "") != "completed":
        raise HTTPException(409, "Drive verification is not complete")
    if _serial_job_release_ready(current):
        return {"ok": True, **_queue_mode_state()}
    project_label = str(current.get("project_name") or "Untitled")
    with WORKERS.lane("cleanup", job_id, project_label, "Verified Serial cleanup retry"):
        cleaned = _auto_clean_delivered_project(job_id)
    _wake.set()
    if not cleaned:
        raise HTTPException(500, "Local project cleanup could not be verified; the serial queue remains blocked")
    return {"ok": True, **_queue_mode_state()}


@app.post("/jobs/{job_id}/serial-skip")
def serial_skip(job_id: str, token: str | None = None):
    auth(token)
    if _queue_mode() != QUEUE_MODE_VERIFIED_SERIAL:
        raise HTTPException(409, "Verified Serial is not enabled")
    current = get_job(job_id)
    if _serial_active_job_id() != job_id:
        raise HTTPException(409, "This job does not own the Verified Serial queue")
    if str(current.get("status") or "") not in {"failed", "cancelled"}:
        raise HTTPException(409, "Only a failed or cancelled serial owner can be skipped")
    with _db_lock, db() as connection:
        _runtime_set_conn(connection, "serial_active_job_id", "")
    update_job(job_id, message=(str(current.get("message") or "") + " · Serial queue skip approved").strip(" ·"))
    _wake.set()
    return {"ok": True, **_queue_mode_state()}


@app.post("/jobs/{job_id}/cancel")
def cancel(job_id: str, token: str | None = None):
    auth(token)
    current = get_job(job_id)
    if current["status"] in TERMINAL_STATUSES:
        return current
    update_job(
        job_id,
        cancel_requested=1,
        cancel_source="web",
        message="Cancellation requested; validated render checkpoints will be preserved",
    )
    if current["status"] in {"queued", "waiting_for_audit"}:
        update_job(job_id, status="cancelled", stage="Cancelled", cancelled_at=now_iso())
    else:
        _mark_next_job_manual_start(job_id)
    _wake.set()
    return get_job(job_id)


@app.post("/jobs/{job_id}/start")
def start_ready_job(job_id: str, token: str | None = None):
    auth(token)
    current=get_job(job_id)
    if current["status"] != "ready_to_render" or not bool(int(current.get("manual_start_required") or 0)):
        raise HTTPException(409,"Job is not waiting for manual Start")
    update_job(job_id,manual_start_required=0,message="Start approved · waiting for renderer")
    _wake.set()
    return _public_job(get_job(job_id))


@app.post("/jobs/{job_id}/retry")
def retry(job_id: str, token: str | None = None):
    auth(token)
    raise HTTPException(410, "Retry is disabled in V17. Use Resume, Restart Stage, or Restart Job.")


@app.post("/jobs/{job_id}/restart-stage")
def restart_stage(job_id: str, token: str | None = None):
    auth(token)
    current = get_job(job_id)
    if current["status"] not in {"failed", "cancelled"}:
        raise HTTPException(409, "Only failed or cancelled jobs can restart a stage")
    failed_from = _failure_stage(current)
    stage_label = _clear_stage_and_downstream(JOBS / job_id, failed_from)
    update_job(
        job_id,
        status="queued",
        stage="Restart Stage Queued",
        progress=0,
        message=f"Restarting {stage_label}; earlier validated checkpoints are preserved",
        started_at="",
        completed_at="",
        cancel_requested=0,
        cancelled_at="",
        cancel_source="",
        resume_mode="restart_stage",
        error_log="",
        drive_status="",
        drive_destination="",
        drive_view_url="",
        output_filename="",
        files_cleaned_at="",
        max_retries=0,
        manual_start_required=0,
    )
    _wake.set()
    return _public_job(get_job(job_id))


@app.post("/jobs/{job_id}/restart")
def restart(job_id: str, token: str | None = None):
    auth(token)
    current = get_job(job_id)
    if current["status"] not in {"failed", "cancelled"}:
        raise HTTPException(409, "Only failed or cancelled jobs can be restarted")
    return _public_job(_reset_job_from_zero(job_id, "Queued for full restart"))


@app.post("/jobs/{job_id}/resume")
def resume(job_id: str, token: str | None = None):
    auth(token)
    current = get_job(job_id)
    if current["status"] not in {"failed", "cancelled"}:
        raise HTTPException(409, "Only failed or cancelled jobs can be resumed")
    snapshot = _resume_snapshot(job_id)
    if not snapshot["resume_available"]:
        raise HTTPException(409, "No reusable pipeline checkpoint is available; use Restart Job")
    update_job(
        job_id,
        status="queued",
        stage="Resume Queued",
        progress=0,
        message=(
            "Resume queued from first invalid checkpoint; preserved stages: "
            + (", ".join(snapshot.get("completed_stages") or []) or "none")
        ),
        started_at="",
        completed_at="",
        cancel_requested=0,
        cancelled_at="",
        cancel_source="",
        resume_mode="resume",
        error_log="",
        drive_status="",
        drive_destination="",
        drive_view_url="",
        output_filename="",
        files_cleaned_at="",
        manual_start_required=0,
    )
    _wake.set()
    return _public_job(get_job(job_id))


@app.delete("/jobs/{job_id}")
def delete(job_id: str, token: str | None = None):
    auth(token)
    current = get_job(job_id)
    if current["status"] in {"preparing", "downloading_media", "ready_to_render", "rendering", "ready_to_upload", "drive_uploading", "running"}:
        raise HTTPException(409, "Cannot delete an active job")
    with _db_lock, db() as connection:
        connection.execute("DELETE FROM jobs WHERE id=?", (job_id,))
    shutil.rmtree(JOBS / job_id, ignore_errors=True)
    return {"deleted": job_id}



@app.get("/jobs/{job_id}/audit/media")
def get_visual_audit(job_id: str, token: str | None = None):
    auth(token)
    current = get_job(job_id)
    payload = _load_audit_checkpoint(JOBS / job_id)
    if payload is None:
        raise HTTPException(404, "Visual Audit has not been prepared for this project")
    result = _public_audit_payload(job_id, payload)
    result["project_name"] = str(current.get("project_name") or "Untitled")
    result["status"] = str(current.get("status") or "")
    return result


@app.get("/jobs/{job_id}/audit/media/{item_id}/preview")
def visual_audit_preview(job_id: str, item_id: str, token: str | None = None):
    auth(token)
    payload = _load_audit_checkpoint(JOBS / job_id)
    if payload is None:
        raise HTTPException(404, "Visual Audit is unavailable")
    _, item = _find_audit_item(payload, item_id)
    path = Path(str(item.get("media_path") or ""))
    if not path.is_file():
        raise HTTPException(404, "Audit media is missing")
    try:
        resolved = path.resolve()
        job_root = (JOBS / job_id).resolve()
        if not resolved.is_relative_to(job_root):
            raise HTTPException(403, "Audit preview path is outside this project")
    except OSError as exc:
        raise HTTPException(404, "Audit media is unavailable") from exc
    return FileResponse(path, filename=path.name)


@app.post("/jobs/{job_id}/audit/media/{item_id}/replace")
async def visual_audit_replace(job_id: str, item_id: str, request: Request, token: str | None = None):
    auth(token)
    try:
        body = await request.json()
    except Exception:
        body = {}
    return _replace_audit_item_media(job_id, item_id, keyword=None, expected_revision=(body or {}).get("revision"))


@app.post("/jobs/{job_id}/audit/media/{item_id}/keyword")
async def visual_audit_keyword(job_id: str, item_id: str, request: Request, token: str | None = None):
    auth(token)
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(400, "Keyword request must be JSON") from exc
    keyword = " ".join(str((body or {}).get("keyword") or "").split()).strip()
    if not keyword:
        raise HTTPException(400, "Search keyword cannot be empty")
    if len(keyword) > 240:
        raise HTTPException(400, "Search keyword is too long")
    return _replace_audit_item_media(job_id, item_id, keyword=keyword, expected_revision=(body or {}).get("revision"))


@app.post("/jobs/{job_id}/audit/media/{item_id}/upload")
async def visual_audit_upload(job_id: str, item_id: str, file: UploadFile = File(...), revision: int | None = None, token: str | None = None):
    auth(token)
    current = get_job(job_id)
    if str(current.get("status") or "") != "waiting_for_audit":
        raise HTTPException(409, "Project is not waiting for Visual Audit")
    suffix = Path(file.filename or "replacement.mp4").suffix.lower()
    if suffix not in VIDEO_EXTENSIONS:
        raise HTTPException(400, "Audit replacement must be a supported video file")
    job_dir = JOBS / job_id
    with _audit_lock(job_id):
        payload = _load_audit_checkpoint(job_dir)
        if not payload or bool(payload.get("approved")):
            raise HTTPException(409, "Visual Audit is not editable")
        index, item = _find_audit_item(payload, item_id)
        _validate_audit_revision(item, revision)
        next_revision = int(item.get("revision") or 0) + 1
        temp_root = job_dir / "audit_upload_tmp"
        temp_root.mkdir(parents=True, exist_ok=True)
        temp_path = temp_root / f"{uuid.uuid4().hex}{suffix}"
        total = 0
        try:
            with temp_path.open("wb") as handle:
                while chunk := await file.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_UPLOAD:
                        raise HTTPException(413, "Audit replacement is too large")
                    handle.write(chunk)
            if total < 1024:
                raise HTTPException(400, "Audit replacement is empty")
            duration = max(0.05, float(item.get("final_end") or 0) - float(item.get("final_start") or 0))
            validation = validate_media(temp_path, "video", duration, require_landscape=bool(item.get("pexels_prefer_landscape", True)))
            if validation.get("handling") == "reject_short":
                raise HTTPException(400, "Uploaded replacement is too short for this scene")
            destination_root = job_dir / "audit_media"
            destination_root.mkdir(parents=True, exist_ok=True)
            destination = destination_root / f"{item_id}_upload_r{next_revision}{suffix}"
            temp_path.replace(destination)
            digest = sha256_file(destination)
            old_media = str(item.get("media_path") or "")
            old_source = str(item.get("media_source_path") or "")
            rejected = {str(v) for v in item.get("rejected_asset_ids", []) if str(v)}
            current_key = _audit_asset_key(item)
            if current_key:
                rejected.add(current_key)
            item.update(
                source_type="UPLOADED", media_path=str(destination), media_source_path=str(destination),
                assigned_media_type="video", provider="uploaded", asset_id=digest[:24],
                keyword_used=str(item.get("query_override") or item.get("keyword_used") or item.get("original_query") or ""),
                rejected_asset_ids=sorted(rejected), error="", approved=True, changed=True, revision=next_revision,
            )
            payload["items"][index] = item
            _save_audit_checkpoint(job_dir, payload)
            _delete_audit_owned_if_unused(job_dir, payload, old_media, excluding_item_id=item_id)
            _delete_audit_owned_if_unused(job_dir, payload, old_source, excluding_item_id=item_id)
            return _public_audit_payload(job_id, payload)
        finally:
            try: temp_path.unlink(missing_ok=True)
            except OSError: pass
            try:
                if temp_root.exists() and not any(temp_root.iterdir()): temp_root.rmdir()
            except OSError: pass


@app.post("/jobs/{job_id}/audit/media/{item_id}/approve")
def visual_audit_approve_item(job_id: str, item_id: str, revision: int | None = None, token: str | None = None):
    auth(token)
    current = get_job(job_id)
    if str(current.get("status") or "") != "waiting_for_audit":
        raise HTTPException(409, "Project is not waiting for Visual Audit")
    job_dir = JOBS / job_id
    with _audit_lock(job_id):
        payload = _load_audit_checkpoint(job_dir)
        if not payload or bool(payload.get("approved")):
            raise HTTPException(409, "Visual Audit is not editable")
        index, item = _find_audit_item(payload, item_id)
        _validate_audit_revision(item, revision)
        if not Path(str(item.get("media_path") or "")).is_file():
            raise HTTPException(409, "This scene has no usable media")
        item["approved"] = True
        item["revision"] = int(item.get("revision") or 0) + 1
        payload["items"][index] = item
        _save_audit_checkpoint(job_dir, payload)
        return _public_audit_payload(job_id, payload)


@app.post("/jobs/{job_id}/audit/media/approve-all")
def visual_audit_approve_all(job_id: str, token: str | None = None):
    auth(token)
    current = get_job(job_id)
    if str(current.get("status") or "") != "waiting_for_audit":
        raise HTTPException(409, "Project is not waiting for Visual Audit")
    job_dir = JOBS / job_id
    with _audit_lock(job_id):
        payload = _load_audit_checkpoint(job_dir)
        if not payload or bool(payload.get("approved")):
            raise HTTPException(409, "Visual Audit is not editable")
        for item in payload.get("items", []):
            if not isinstance(item, dict):
                continue
            if Path(str(item.get("media_path") or "")).is_file():
                item["approved"] = True
                item["revision"] = int(item.get("revision") or 0) + 1
        _save_audit_checkpoint(job_dir, payload)
        return _public_audit_payload(job_id, payload)


@app.post("/jobs/{job_id}/audit/continue")
def visual_audit_continue(job_id: str, token: str | None = None):
    auth(token)
    current = get_job(job_id)
    if str(current.get("status") or "") != "waiting_for_audit":
        raise HTTPException(409, "Project is not waiting for Visual Audit")
    job_dir = JOBS / job_id
    with _audit_lock(job_id):
        payload = _load_audit_checkpoint(job_dir)
        if not payload or bool(payload.get("approved")):
            raise HTTPException(409, "Visual Audit is not waiting for approval")
        missing = [str(item.get("item_id") or "") for item in payload.get("items", []) if isinstance(item, dict) and not Path(str(item.get("media_path") or "")).is_file()]
        unapproved = [str(item.get("item_id") or "") for item in payload.get("items", []) if isinstance(item, dict) and not bool(item.get("approved"))]
        if missing:
            raise HTTPException(409, "Missing visual media: " + ", ".join(missing[:20]))
        if unapproved:
            raise HTTPException(409, "Unapproved audit scenes remain: " + ", ".join(unapproved[:20]))
        approved_at = now_iso()
        payload["approved"] = True
        payload["approved_at"] = approved_at
        _save_audit_checkpoint(job_dir, payload)
        update_job(
            job_id, status="queued", stage="Audit Approved", progress=34,
            message="First 2-minute Visual Audit approved · queued for remaining media and render",
            resume_mode="audit_approved", audit_approved_at=approved_at, cancel_requested=0,
        )
        _wake.set()
        return {"ok": True, "job": _public_job(get_job(job_id)), "audit": _public_audit_payload(job_id, payload)}


@app.get("/jobs/{job_id}/download")
def download(job_id: str, token: str | None = None):
    auth(token)
    current = get_job(job_id)
    if current["status"] != "completed":
        raise HTTPException(409, "Job is not completed")
    path = Path(current["output_file"])
    if not path.exists() and current.get("drive_destination"):
        path.parent.mkdir(parents=True, exist_ok=True)
        destination = str(current["drive_destination"])
        remote_file = (
            destination
            if destination.lower().endswith(".mp4")
            else f"{destination}/final.mp4"
        )
        process = subprocess.run(
            [
                "rclone",
                "copyto",
                remote_file,
                str(path),
                "--retries",
                "5",
                "--low-level-retries",
                "10",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=24 * 60 * 60,
            check=False,
        )
        if process.returncode != 0:
            raise HTTPException(
                502,
                "Could not restore rendered file from Google Drive: "
                + (process.stdout or "").strip()[-500:],
            )
    if not path.exists():
        raise HTTPException(404, "Rendered file is missing")
    safe = safe_project_name(current["project_name"]) or job_id
    return FileResponse(path, media_type="video/mp4", filename=f"{safe}.mp4")


@app.post("/jobs/{job_id}/download-confirmed")
def download_confirmed(job_id: str, token: str | None = None):
    auth(token)
    current = get_job(job_id)
    if current["status"] != "completed":
        raise HTTPException(409, "Job is not completed")
    update_job(
        job_id,
        downloaded_at=now_iso(),
        message="Completed video downloaded to Windows",
    )
    return get_job(job_id)


def _directory_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for item in path.rglob("*"):
        try:
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
        except OSError:
            pass
    return total


def _cache_paths() -> list[Path]:
    candidates = [BASE / "cache", BASE / "temp", BASE / "tmp"]
    candidates.extend([path for path in BASE.rglob("pexels_live") if path.is_dir()])
    return candidates


def _parse_utc(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _remove_job_heavy_files(job_id: str, keep_logs: bool = True) -> int:
    job_dir = JOBS / job_id
    before = _directory_size(job_dir)
    if not job_dir.exists():
        return 0
    preserve_names = {"error.log", "drive_upload.log"} if keep_logs else set()
    for child in list(job_dir.iterdir()):
        if child.name in preserve_names:
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            try:
                child.unlink(missing_ok=True)
            except OSError:
                pass
    after = _directory_size(job_dir)
    return max(0, before - after)


def _remove_failed_job_heavy_files(job_id: str) -> int:
    """Reclaim failed/cancelled render media while preserving original input for Restart Job."""
    job_dir = JOBS / job_id
    before = _directory_size(job_dir)
    if not job_dir.exists():
        return 0
    for name in ("resolved_media", "resolved_intro_media", "output", PIPELINE_CHECKPOINT_DIR, "cache", "temp", "tmp"):
        shutil.rmtree(job_dir / name, ignore_errors=True)
    for name in ("project.zip", "input.zip", "media_resolution.json", "intro_media_resolution.json", "resolved_job_plan.json"):
        try:
            (job_dir / name).unlink(missing_ok=True)
        except OSError:
            pass
    return max(0, before - _directory_size(job_dir))


def _prune_pexels_library() -> dict[str, int]:
    # V17.4 has no shared Pexels library. Provider media is job-scoped and is
    # removed with the project. Keep this compatibility hook as a no-op.
    return {"removed_files": 0, "removed_bytes": 0}


def _retention_sweep_once() -> dict[str, int]:
    now = datetime.now(timezone.utc)
    removed_jobs = 0
    removed_history = 0
    freed_bytes = 0
    with _db_lock, db() as connection:
        rows = [dict(row) for row in connection.execute(
            "SELECT id,status,completed_at,updated_at,drive_status,downloaded_at,files_cleaned_at FROM jobs "
            "WHERE status IN ('completed','failed','cancelled')"
        ).fetchall()]
    for row in rows:
        stamp = _parse_utc(row.get("completed_at") or row.get("updated_at") or "")
        if not stamp:
            continue
        age_days = (now - stamp).total_seconds() / 86400.0
        status = str(row.get("status") or "")
        if status == "completed":
            safe = bool(row.get("downloaded_at")) or str(row.get("drive_status") or "") == "completed" or not DRIVE_UPLOAD_ENABLED
            if safe and not row.get("files_cleaned_at") and age_days >= COMPLETED_OUTPUT_RETENTION_DAYS:
                freed_bytes += _remove_job_heavy_files(str(row["id"]), keep_logs=True)
                update_job(str(row["id"]), files_cleaned_at=now_iso(), message="Completed project retention cleanup applied")
                removed_jobs += 1
        elif not row.get("files_cleaned_at") and age_days >= FAILED_JOB_RETENTION_DAYS:
            freed_bytes += _remove_failed_job_heavy_files(str(row["id"]))
            update_job(str(row["id"]), files_cleaned_at=now_iso(), message=f"{status.title()} project heavy media expired; original input preserved for Restart Job")
            removed_jobs += 1

    # Queue history is separate from file retention. Only terminal rows old enough
    # are removed, and completed rows must already be delivery-safe.
    history_cutoff = now - timedelta(days=QUEUE_HISTORY_RETENTION_DAYS)
    with _db_lock, db() as connection:
        rows = [dict(row) for row in connection.execute(
            "SELECT id,status,completed_at,updated_at,drive_status,downloaded_at FROM jobs "
            "WHERE status IN ('completed','failed','cancelled')"
        ).fetchall()]
        for row in rows:
            stamp = _parse_utc(row.get("completed_at") or row.get("updated_at") or "")
            if not stamp or stamp > history_cutoff:
                continue
            if row["status"] == "completed" and DRIVE_UPLOAD_ENABLED and not (row.get("downloaded_at") or row.get("drive_status") == "completed"):
                continue
            shutil.rmtree(JOBS / str(row["id"]), ignore_errors=True)
            connection.execute("DELETE FROM job_events WHERE job_id=?", (row["id"],))
            connection.execute("DELETE FROM jobs WHERE id=?", (row["id"],))
            removed_history += 1
    with _db_lock, db() as connection:
        media_active = int(connection.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE status IN ('preparing','downloading_media')"
        ).fetchone()["n"] or 0)
    pexels = {"removed_files": 0, "removed_bytes": 0} if media_active else _prune_pexels_library()
    return {
        "cleaned_terminal_jobs": removed_jobs,
        "removed_history_rows": removed_history,
        "freed_bytes": freed_bytes + int(pexels.get("removed_bytes") or 0),
        "pexels_removed_files": int(pexels.get("removed_files") or 0),
    }


def _retention_loop() -> None:
    while not _stop.wait(RETENTION_SWEEP_SECONDS):
        try:
            with WORKERS.lane("cleanup", "retention", "system", "Retention cleanup"):
                _retention_sweep_once()
        except Exception:
            pass


def _storage_snapshot() -> dict[str, Any]:
    usage = shutil.disk_usage(BASE)
    jobs_bytes = _directory_size(JOBS)
    pexels_bytes = 0
    cache_bytes = sum(_directory_size(path) for path in _cache_paths())
    completed_bytes = 0
    completed_jobs = 0
    with _db_lock, db() as connection:
        rows = connection.execute(
            "SELECT id FROM jobs WHERE status='completed'"
        ).fetchall()
    for row in rows:
        completed_jobs += 1
        completed_bytes += _directory_size(JOBS / row["id"])
    return {
        "disk_total_bytes": usage.total,
        "disk_used_bytes": usage.used,
        "disk_free_bytes": usage.free,
        "disk_used_percent": round(
            (usage.used / usage.total * 100) if usage.total else 0,
            1,
        ),
        "jobs_bytes": jobs_bytes,
        "completed_projects_bytes": completed_bytes,
        "completed_projects_count": completed_jobs,
        "pexels_media_bytes": pexels_bytes,
        "cache_bytes": cache_bytes,
    }


@app.get("/maintenance/storage")
def maintenance_storage(token: str | None = None):
    auth(token)
    return _storage_snapshot()


@app.post("/maintenance/retention-sweep")
def maintenance_retention_sweep(token: str | None = None):
    auth(token)
    with WORKERS.lane("cleanup", "retention-manual", "system", "Manual retention cleanup"):
        result = _retention_sweep_once()
    return {"ok": True, **result, "storage": _storage_snapshot()}


@app.post("/maintenance/cleanup")
def maintenance_cleanup(
    token: str | None = None,
    completed_projects: bool = False,
    pexels_media: bool = False,
    cache: bool = False,
):
    auth(token)
    if not any((completed_projects, pexels_media, cache)):
        raise HTTPException(400, "Select at least one cleanup target")

    if pexels_media or cache:
        with _db_lock, db() as connection:
            active = connection.execute(
                "SELECT COUNT(*) count FROM jobs WHERE status IN "
                "('preparing','downloading_media','rendering','running')"
            ).fetchone()["count"]
        if active:
            raise HTTPException(
                409,
                "Pexels media/cache cannot be cleaned while a VPS job is active",
            )

    before = _storage_snapshot()
    removed_jobs = 0
    removed_paths = 0
    skipped_unsafe = 0

    if completed_projects:
        with _db_lock, db() as connection:
            rows = connection.execute(
                "SELECT id,drive_status,downloaded_at,output_file FROM jobs "
                "WHERE status='completed'"
            ).fetchall()
        for row in rows:
            safe_to_remove_output = bool(row["downloaded_at"]) or row["drive_status"] == "completed"
            if DELETE_OUTPUT_ONLY_AFTER_DRIVE_UPLOAD and not safe_to_remove_output:
                skipped_unsafe += 1
                continue
            job_dir = JOBS / row["id"]
            if job_dir.exists():
                archive_dir = job_dir / "archive"
                archive_dir.mkdir(parents=True, exist_ok=True)
                candidates = (
                    list((job_dir / "input").rglob("project.json"))
                    + list((job_dir / "input").rglob("project.txt"))
                    + list((job_dir / "input").rglob("*.wav"))
                    + list((job_dir / "input").rglob("*.mp3"))
                    + list(job_dir.glob("*.log"))
                )
                for candidate in candidates:
                    try:
                        target = archive_dir / candidate.name
                        if candidate.is_file() and not target.exists():
                            shutil.copy2(candidate, target)
                    except OSError:
                        pass
                for child in list(job_dir.iterdir()):
                    if child.name != "archive":
                        if child.is_dir():
                            shutil.rmtree(child, ignore_errors=True)
                        else:
                            try:
                                child.unlink()
                            except OSError:
                                pass
                removed_jobs += 1
            update_job(
                row["id"],
                stage="Archived",
                files_cleaned_at=now_iso(),
                message="Completed project archived; heavy local files removed safely",
            )

    # V17.4: no shared Pexels cache exists. Project-scoped media is cleaned with jobs.

    if cache:
        for path in _cache_paths():
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
                removed_paths += 1

    after = _storage_snapshot()
    return {
        "ok": True,
        "removed_completed_projects": removed_jobs,
        "skipped_unsafe_completed_projects": skipped_unsafe,
        "removed_storage_paths": removed_paths,
        "freed_bytes": max(0, before["disk_used_bytes"] - after["disk_used_bytes"]),
        "before": before,
        "after": after,
    }

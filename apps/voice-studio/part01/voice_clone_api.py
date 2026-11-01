from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import FileResponse
from voice_tenant import owner
from pydantic import BaseModel, Field

ROOT = Path(os.environ.get("VOICE_CLONE_ROOT", "/home/ubuntu/apps/kokoro_tts_test/data/voice_clone"))
DB_PATH = ROOT / "clone.db"
SAMPLES_DIR = ROOT / "samples"
AUDIO_DIR = ROOT / "audio"
TEXT_DIR = ROOT / "text"
TMP_DIR = ROOT / "tmp"
MAX_SAMPLE_BYTES = 12 * 1024 * 1024
MAX_TEXT = 60000
MAX_BATCH = 100
MAX_SOURCE_FILENAME = 180
ALLOWED_SAMPLE_EXTS = {"wav", "mp3", "m4a", "flac", "ogg", "webm", "aac", "mp4"}
ALLOWED_JOB_STATUSES = {"waiting_worker", "processing", "completed", "failed", "cancelled"}
DELIVERY_STATUSES = {
    "waiting_generation", "pending", "uploading", "upload_failed",
    "drive_verified", "cleaned", "legacy_local", "not_applicable",
}
GPU_ENGINES = {"auto", "chatterbox", "indicf5", "omnivoice"}
INDICF5_LANGUAGES = {"as", "bn", "gu", "hi", "kn", "ml", "mr", "or", "pa", "ta", "te"}
CHATTERBOX_LANGUAGES = {"ar", "da", "de", "el", "en", "es", "fi", "fr", "he", "hi", "it", "ja", "ko", "ms", "nl", "no", "pl", "pt", "ru", "sv", "sw", "tr", "zh"}
OMNIVOICE_LANGUAGES = {"ar", "bn", "gu", "hi", "mr"}
GPU_PRESETS = {"fast", "narration", "expressive"}
WORKER_CONFIG_FILE = ROOT / "worker_config.json"
DEFAULT_TOKEN_FILE = "/home/ubuntu/.config/voice_clone/worker_token"
OMNIVOICE_CONFIG_FILE = ROOT / "omnivoice_worker_config.json"
DEFAULT_OMNIVOICE_SALAD_KEY_FILE = "/home/ubuntu/.config/voice_clone/omnivoice_salad_api_key"
DEFAULT_OMNIVOICE_API_TOKEN_FILE = "/home/ubuntu/.config/voice_clone/omnivoice_api_token"


def _clean_worker_path(value: str, default: str) -> str:
    value = str(value or default).strip()
    if not value.startswith("/"):
        value = "/" + value
    if "://" in value or ".." in value or "?" in value or "#" in value:
        raise ValueError("Worker path must be a simple absolute API path")
    return value.rstrip("/") or "/"


def _validate_salad_endpoint(value: str) -> str:
    value = str(value or "").strip().rstrip("/")
    if not value:
        raise ValueError("Salad endpoint URL is required")
    u = urlsplit(value)
    host = (u.hostname or "").lower().rstrip(".")
    if u.scheme.lower() != "https":
        raise ValueError("Salad endpoint must use HTTPS")
    if not (host == "salad.cloud" or host.endswith(".salad.cloud")):
        raise ValueError("Endpoint host must be salad.cloud")
    if u.username or u.password or u.query or u.fragment:
        raise ValueError("Endpoint URL cannot contain credentials, query, or fragment")
    if u.port not in (None, 443):
        raise ValueError("Salad endpoint must use standard HTTPS port 443")
    base_path = (u.path or "").rstrip("/")
    if ".." in base_path:
        raise ValueError("Endpoint path is invalid")
    return f"https://{host}{base_path}"


def _read_managed_worker_config() -> dict[str, Any] | None:
    if not WORKER_CONFIG_FILE.is_file():
        return None
    try:
        data = json.loads(WORKER_CONFIG_FILE.read_text("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _token_file_path() -> Path:
    return Path(os.environ.get("VOICE_CLONE_WORKER_TOKEN_FILE", DEFAULT_TOKEN_FILE))


def effective_worker_config() -> dict[str, Any]:
    managed = _read_managed_worker_config()
    if managed is not None:
        endpoint = str(managed.get("endpoint_url") or "").strip().rstrip("/")
        enabled = bool(managed.get("enabled", False))
        return {
            "source": "managed",
            "provider": "SaladCloud",
            "endpoint_url": endpoint if enabled else "",
            "saved_endpoint_url": endpoint,
            "health_path": _clean_worker_path(str(managed.get("health_path") or "/healthz"), "/healthz"),
            "generation_path": _clean_worker_path(str(managed.get("generation_path") or "/v1/clone/generate"), "/v1/clone/generate"),
            "request_timeout": max(10, min(1800, int(managed.get("request_timeout") or 300))),
            "auth_mode": (lambda m: m if m in {"none", "bearer", "salad_api_key"} else "none")(str(managed.get("auth_mode") or "none").lower()),
            "enabled": enabled,
        }
    legacy = os.environ.get("VOICE_CLONE_WORKER_URL", "").strip().rstrip("/")
    token_exists = _token_file_path().is_file() and bool(_token_file_path().read_text("utf-8").strip())
    return {
        "source": "legacy-env",
        "provider": "SaladCloud",
        "endpoint_url": legacy,
        "saved_endpoint_url": legacy,
        "health_path": "/healthz",
        "generation_path": "/v1/clone/generate",
        "request_timeout": 300,
        "auth_mode": "bearer" if token_exists else "none",
        "enabled": bool(legacy),
    }


WORKER_URL = lambda: str(effective_worker_config().get("endpoint_url") or "")
TOKEN_FILE = lambda: str(_token_file_path())
WORKER_HEALTH_PATH = lambda: str(effective_worker_config().get("health_path") or "/healthz")
WORKER_GENERATION_PATH = lambda: str(effective_worker_config().get("generation_path") or "/v1/clone/generate")
WORKER_REQUEST_TIMEOUT = lambda: int(effective_worker_config().get("request_timeout") or 300)
WORKER_AUTH_MODE = lambda: str(effective_worker_config().get("auth_mode") or "none")


def _read_json_object(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _secret_file(path_value: str, env_name: str) -> Path:
    return Path(os.environ.get(env_name, path_value))


def omnivoice_salad_key_file() -> Path:
    return _secret_file(DEFAULT_OMNIVOICE_SALAD_KEY_FILE, "OMNIVOICE_SALAD_KEY_FILE")


def omnivoice_api_token_file() -> Path:
    return _secret_file(DEFAULT_OMNIVOICE_API_TOKEN_FILE, "OMNIVOICE_API_TOKEN_FILE")


def _read_secret(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        return path.read_text("utf-8").strip()
    except Exception:
        return ""


def effective_omnivoice_config() -> dict[str, Any]:
    managed = _read_json_object(OMNIVOICE_CONFIG_FILE) or {}
    endpoint = str(managed.get("endpoint_url") or "").strip().rstrip("/")
    enabled = bool(managed.get("enabled", False))
    return {
        "source": "managed" if managed else "default",
        "provider": "SaladCloud",
        "engine": "omnivoice",
        "endpoint_url": endpoint if enabled else "",
        "saved_endpoint_url": endpoint,
        "health_path": _clean_worker_path(str(managed.get("health_path") or "/healthz"), "/healthz"),
        "ready_path": _clean_worker_path(str(managed.get("ready_path") or "/readyz"), "/readyz"),
        "generation_path": "/v1/jobs",
        "request_timeout": max(10, min(1800, int(managed.get("request_timeout") or 300))),
        "enabled": enabled,
    }


def omnivoice_auth_headers(salad_key_override: str = "", api_token_override: str = "") -> dict[str, str]:
    headers: dict[str, str] = {}
    salad_key = str(salad_key_override or "").strip() or _read_secret(omnivoice_salad_key_file())
    api_token = str(api_token_override or "").strip() or _read_secret(omnivoice_api_token_file())
    if salad_key:
        headers["Salad-Api-Key"] = salad_key
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"
    return headers


def _probe_omnivoice_config(
    config: dict[str, Any],
    salad_key_override: str = "",
    api_token_override: str = "",
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    url = str(config.get("endpoint_url") or config.get("saved_endpoint_url") or "").strip().rstrip("/")
    if not url:
        return {"ok": False, "reachable": False, "http_status": None, "error": "not_configured", "latency_ms": None}
    health_path = _clean_worker_path(str(config.get("health_path") or "/healthz"), "/healthz")
    timeout = float(timeout_seconds if timeout_seconds is not None else min(15, int(config.get("request_timeout") or 300)))
    started = time.monotonic()
    try:
        r = requests.get(
            url + health_path,
            headers=omnivoice_auth_headers(salad_key_override, api_token_override),
            timeout=timeout,
            allow_redirects=False,
        )
        latency = int((time.monotonic() - started) * 1000)
        return {
            "ok": 200 <= r.status_code < 300,
            "reachable": True,
            "http_status": int(r.status_code),
            "error": "" if 200 <= r.status_code < 300 else f"HTTP {r.status_code}",
            "latency_ms": latency,
        }
    except requests.RequestException as exc:
        return {
            "ok": False,
            "reachable": False,
            "http_status": None,
            "error": str(exc)[:180],
            "latency_ms": int((time.monotonic() - started) * 1000),
        }


def omnivoice_worker_health(timeout_seconds: float = 2.0) -> tuple[bool, str]:
    config = effective_omnivoice_config()
    if not config.get("endpoint_url"):
        return False, "not_configured"
    result = _probe_omnivoice_config(config, timeout_seconds=timeout_seconds)
    return bool(result["ok"]), str(result.get("error") or "")


router = APIRouter()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    try:
        with con:
            yield con
    finally:
        con.close()


def init_storage() -> None:
    for p in (ROOT, SAMPLES_DIR, AUDIO_DIR, TEXT_DIR, TMP_DIR):
        p.mkdir(parents=True, exist_ok=True)
    with db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS profiles (
              profile_id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              source_filename TEXT NOT NULL,
              sample_file TEXT NOT NULL,
              sample_sha256 TEXT NOT NULL,
              duration_seconds REAL NOT NULL,
              provenance_note TEXT NOT NULL DEFAULT '',
              consent_confirmed INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
              job_id TEXT PRIMARY KEY,
              profile_id TEXT NOT NULL,
              text TEXT NOT NULL,
              language TEXT NOT NULL DEFAULT 'auto',
              status TEXT NOT NULL DEFAULT 'waiting_worker',
              created_at TEXT NOT NULL,
              started_at TEXT,
              finished_at TEXT,
              audio_file TEXT,
              error TEXT NOT NULL DEFAULT '',
              retries INTEGER NOT NULL DEFAULT 0,
              FOREIGN KEY(profile_id) REFERENCES profiles(profile_id) ON DELETE RESTRICT
            );
            CREATE INDEX IF NOT EXISTS idx_clone_jobs_status_created ON jobs(status, created_at);
            CREATE TABLE IF NOT EXISTS categories (
              name TEXT PRIMARY KEY,
              created_at TEXT NOT NULL
            );
            """
        )
        con.execute("CREATE TABLE IF NOT EXISTS voice_owner_categories (owner_username TEXT NOT NULL COLLATE NOCASE, name TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(owner_username,name))")
        con.execute("CREATE TABLE IF NOT EXISTS voice_design_preview_owners (preview_id TEXT PRIMARY KEY, owner_username TEXT NOT NULL COLLATE NOCASE)")
        profile_cols = {str(r["name"]) for r in con.execute("PRAGMA table_info(profiles)").fetchall()}
        had_engine_pref = "engine_pref" in profile_cols
        for name, ddl in {
            "reference_language": "TEXT NOT NULL DEFAULT 'auto'",
            "reference_text": "TEXT NOT NULL DEFAULT ''",
            "engine_pref": "TEXT NOT NULL DEFAULT 'auto'",
            "profile_type": "TEXT NOT NULL DEFAULT 'cloned'",
            "design_prompt": "TEXT NOT NULL DEFAULT ''",
            "owner_username": "TEXT NOT NULL DEFAULT ''",
        }.items():
            if name not in profile_cols:
                con.execute(f"ALTER TABLE profiles ADD COLUMN {name} {ddl}")
        # Pre-V6 profiles do not have a transcript. Keep their known-working lane honest.
        if not had_engine_pref:
            con.execute("UPDATE profiles SET engine_pref='chatterbox' WHERE TRIM(COALESCE(reference_text,''))='' ")

        job_cols = {str(r["name"]) for r in con.execute("PRAGMA table_info(jobs)").fetchall()}
        had_delivery = "delivery_status" in job_cols
        migrations = {
            "engine": "TEXT NOT NULL DEFAULT 'auto'",
            "preset": "TEXT NOT NULL DEFAULT 'narration'",
            "speed": "REAL NOT NULL DEFAULT 1.0",
            "settings_json": "TEXT NOT NULL DEFAULT '{}'",
            "batch_id": "TEXT NOT NULL DEFAULT ''",
            "batch_name": "TEXT NOT NULL DEFAULT ''",
            "title": "TEXT NOT NULL DEFAULT ''",
            "source_filename": "TEXT NOT NULL DEFAULT ''",
            "output_basename": "TEXT NOT NULL DEFAULT ''",
            "category": "TEXT NOT NULL DEFAULT 'General'",
            "drive_folder": "TEXT NOT NULL DEFAULT ''",
            "text_file": "TEXT NOT NULL DEFAULT ''",
            "delivery_status": "TEXT NOT NULL DEFAULT 'waiting_generation'",
            "drive_remote": "TEXT NOT NULL DEFAULT ''",
            "drive_text_remote": "TEXT NOT NULL DEFAULT ''",
            "drive_error": "TEXT NOT NULL DEFAULT ''",
            "drive_verified_at": "TEXT",
            "cleaned_at": "TEXT",
            "delivery_retries": "INTEGER NOT NULL DEFAULT 0",
            "delivery_next_retry": "REAL NOT NULL DEFAULT 0",
            # VOICE_STUDIO_BATCH_CONTROLS_API_V1
            "queue_paused": "INTEGER NOT NULL DEFAULT 0",
            "queue_order_at": "TEXT NOT NULL DEFAULT ''",
            "owner_username": "TEXT NOT NULL DEFAULT ''",
        }
        for name, ddl in migrations.items():
            if name not in job_cols:
                con.execute(f"ALTER TABLE jobs ADD COLUMN {name} {ddl}")
        if not had_delivery:
            # Never auto-deliver historical output merely because the feature was installed.
            con.execute(
                "UPDATE jobs SET delivery_status=CASE "
                "WHEN status='completed' THEN 'legacy_local' "
                "WHEN status IN ('failed','cancelled') THEN 'not_applicable' "
                "ELSE 'waiting_generation' END"
            )
        con.execute("INSERT OR IGNORE INTO categories(name,created_at) VALUES('General',?)", (utc_now(),))
        # Restart recovery: a process interrupted mid-dispatch can safely be retried.
        con.execute(
            "UPDATE jobs SET status='waiting_worker',started_at=NULL,error='Recovered after dispatcher restart' "
            "WHERE status='processing'"
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_clone_jobs_delivery_retry ON jobs(delivery_status,delivery_next_retry,created_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_clone_jobs_batch ON jobs(batch_id,created_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_clone_jobs_queue_control ON jobs(status,queue_paused,queue_order_at,created_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_clone_jobs_owner ON jobs(owner_username,created_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_clone_profiles_owner ON profiles(owner_username,created_at)")


def _safe_name(v: str, fallback: str = "reference") -> str:
    v = re.sub(r"[^A-Za-z0-9._-]+", "-", (v or "").strip()).strip("-._")
    return (v[:120] or fallback)


def _probe_duration(path: Path) -> float:
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            text=True, stderr=subprocess.DEVNULL, timeout=20
        ).strip()
        return max(0.0, float(out))
    except Exception:
        return 0.0


def _normalize_reference(raw: bytes, source_filename: str, profile_id: str) -> tuple[Path, float, str]:
    suffix = Path(source_filename or "").suffix.lower().lstrip(".")
    if suffix not in ALLOWED_SAMPLE_EXTS:
        raise HTTPException(400, "Unsupported reference-audio format")
    if not raw or len(raw) > MAX_SAMPLE_BYTES:
        raise HTTPException(400, "Reference audio must be between 1 byte and 12 MB")
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmpname = tempfile.mkstemp(prefix="clone-upload-", suffix="." + suffix, dir=str(TMP_DIR))
    os.close(fd)
    src = Path(tmpname)
    out = SAMPLES_DIR / f"{profile_id}.wav"
    try:
        src.write_bytes(raw)
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
             "-vn", "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", str(out)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=90
        )
        if proc.returncode != 0:
            out.unlink(missing_ok=True)
            raise HTTPException(400, "Reference audio could not be decoded")
        duration = _probe_duration(out)
        if duration < 2.0 or duration > 120.0:
            out.unlink(missing_ok=True)
            raise HTTPException(400, "Reference audio must be between 2 and 120 seconds")
        if out.stat().st_size < 2000:
            out.unlink(missing_ok=True)
            raise HTTPException(400, "Reference audio produced no usable speech file")
        sha = hashlib.sha256(out.read_bytes()).hexdigest()
        return out, duration, sha
    finally:
        src.unlink(missing_ok=True)


def _profile_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "profile_id": str(row["profile_id"]),
        "name": str(row["name"]),
        "source_filename": str(row["source_filename"]),
        "sample_sha256": str(row["sample_sha256"]),
        "duration_seconds": round(float(row["duration_seconds"]), 3),
        "provenance_note": str(row["provenance_note"] or ""),
        "reference_language": str(row["reference_language"] or "auto"),
        "reference_text": str(row["reference_text"] or ""),
        "engine_pref": str(row["engine_pref"] or "auto"),
        "profile_type": str(row["profile_type"] or "cloned"),
        "design_prompt": str(row["design_prompt"] or ""),
        "transcript_ready": bool(str(row["reference_text"] or "").strip()),
        "consent_confirmed": bool(row["consent_confirmed"]),
        "created_at": str(row["created_at"]),
    }


def _json_settings(value: Any) -> dict[str, Any]:
    try:
        data = json.loads(str(value or "{}"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _job_payload(row: sqlite3.Row, profile_name: str | None = None) -> dict[str, Any]:
    audio = str(row["audio_file"] or "")
    return {
        "job_id": str(row["job_id"]),
        "batch_id": str(row["batch_id"] or ""),
        "batch_name": str(row["batch_name"] or ""),
        "title": str(row["title"] or ""),
        "source_filename": str(row["source_filename"] or ""),
        "output_basename": str(row["output_basename"] or ""),
        "category": str(row["category"] or ""),
        "drive_folder": str(row["drive_folder"] or ""),
        "profile_id": str(row["profile_id"]),
        "profile_name": profile_name or "",
        "language": str(row["language"] or "auto"),
        "engine": str(row["engine"] or "auto"),
        "preset": str(row["preset"] or "narration"),
        "speed": float(row["speed"] or 1.0),
        "settings": _json_settings(row["settings_json"]),
        "status": str(row["status"]),
        "delivery_status": str(row["delivery_status"] or "waiting_generation"),
        "drive_remote": str(row["drive_remote"] or ""),
        "drive_text_remote": str(row["drive_text_remote"] or ""),
        "drive_error": str(row["drive_error"] or ""),
        "drive_verified_at": str(row["drive_verified_at"] or ""),
        "cleaned_at": str(row["cleaned_at"] or ""),
        "created_at": str(row["created_at"]),
        "started_at": str(row["started_at"] or ""),
        "finished_at": str(row["finished_at"] or ""),
        "error": str(row["error"] or ""),
        "retries": int(row["retries"] or 0),
        "delivery_retries": int(row["delivery_retries"] or 0),
        "text_preview": str(row["text"])[:160],
        "audio_url": f"/tts/clone/audio/{audio}" if audio and (AUDIO_DIR / audio).is_file() else "",
    }


def _saved_token() -> str:
    token_path = _token_file_path()
    if token_path.is_file():
        try:
            return token_path.read_text("utf-8").strip()
        except Exception:
            return ""
    return ""


def _worker_auth_headers(token_override: str = "", auth_mode: str | None = None) -> dict[str, str]:
    mode = (auth_mode or WORKER_AUTH_MODE()).strip().lower()
    token = str(token_override or "").strip() or _saved_token()
    if not token:
        return {}
    if mode == "bearer":
        return {"Authorization": f"Bearer {token}"}
    if mode == "salad_api_key":
        return {"Salad-Api-Key": token}
    return {}


def _probe_worker_config(config: dict[str, Any], token_override: str = "", timeout_seconds: float | None = None) -> dict[str, Any]:
    url = str(config.get("endpoint_url") or "").strip().rstrip("/")
    if not url:
        return {"ok": False, "reachable": False, "http_status": None, "error": "not_configured", "latency_ms": None}
    health_path = _clean_worker_path(str(config.get("health_path") or "/healthz"), "/healthz")
    timeout = float(timeout_seconds if timeout_seconds is not None else min(15, int(config.get("request_timeout") or 300)))
    started = time.monotonic()
    try:
        r = requests.get(url + health_path, headers=_worker_auth_headers(token_override, str(config.get("auth_mode") or "none")), timeout=timeout, allow_redirects=False)
        latency = int((time.monotonic() - started) * 1000)
        return {
            "ok": 200 <= r.status_code < 300,
            "reachable": True,
            "http_status": int(r.status_code),
            "error": "" if 200 <= r.status_code < 300 else f"HTTP {r.status_code}",
            "latency_ms": latency,
        }
    except requests.RequestException as exc:
        return {"ok": False, "reachable": False, "http_status": None, "error": str(exc)[:180], "latency_ms": int((time.monotonic() - started) * 1000)}


def worker_health(timeout_seconds: float = 2.0) -> tuple[bool, str]:
    config = effective_worker_config()
    if not config.get("endpoint_url"):
        return False, "not_configured"
    result = _probe_worker_config(config, timeout_seconds=timeout_seconds)
    return bool(result["ok"]), str(result.get("error") or "")


def _atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{uuid.uuid4().hex[:8]}")
    tmp.write_text(content, encoding="utf-8")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _public_worker_config() -> dict[str, Any]:
    cfg = effective_worker_config()
    online, error = worker_health(timeout_seconds=3.0) if cfg.get("endpoint_url") else (False, "not_configured")
    return {
        "ok": True,
        "provider": "SaladCloud",
        "source": cfg.get("source"),
        "endpoint_url": cfg.get("saved_endpoint_url") or "",
        "health_path": cfg.get("health_path") or "/healthz",
        "generation_path": cfg.get("generation_path") or "/v1/clone/generate",
        "request_timeout": int(cfg.get("request_timeout") or 300),
        "auth_mode": cfg.get("auth_mode") or "none",
        "enabled": bool(cfg.get("enabled")),
        "token_configured": bool(_saved_token()),
        "worker_online": online,
        "worker_error": error,
    }


class WorkerConnectionPayload(BaseModel):
    endpoint_url: str = Field(min_length=8, max_length=500)
    health_path: str = Field(default="/healthz", min_length=1, max_length=120)
    generation_path: str = Field(default="/v1/clone/generate", min_length=1, max_length=160)
    request_timeout: int = Field(default=300, ge=10, le=1800)
    auth_mode: str = Field(default="none", max_length=32)
    token: str = Field(default="", max_length=4096)
    enabled: bool = True


def _validated_worker_payload(payload: WorkerConnectionPayload, enabled: bool | None = None) -> dict[str, Any]:
    mode = payload.auth_mode.strip().lower()
    if mode not in {"none", "bearer", "salad_api_key"}:
        raise HTTPException(status_code=400, detail="Connection mode must be public, Salad API Key, or bearer token")
    try:
        endpoint = _validate_salad_endpoint(payload.endpoint_url)
        health = _clean_worker_path(payload.health_path, "/healthz")
        generation = _clean_worker_path(payload.generation_path, "/v1/clone/generate")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "provider": "SaladCloud",
        "endpoint_url": endpoint,
        "health_path": health,
        "generation_path": generation,
        "request_timeout": int(payload.request_timeout),
        "auth_mode": mode,
        "enabled": bool(payload.enabled if enabled is None else enabled),
        "updated_at": utc_now(),
    }


def _save_worker_connection(config: dict[str, Any], token: str = "") -> None:
    init_storage()
    if str(token or "").strip():
        _atomic_write(_token_file_path(), str(token).strip() + "\n")
    _atomic_write(WORKER_CONFIG_FILE, json.dumps(config, indent=2, sort_keys=True) + "\n")


class CloneProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    source_filename: str = Field(min_length=1, max_length=180)
    audio_base64: str = Field(min_length=4, max_length=18_000_000)
    consent: bool
    provenance_note: str = Field(default="", max_length=240)
    reference_language: str = Field(default="auto", min_length=1, max_length=24)
    reference_text: str = Field(default="", max_length=3000)
    engine_pref: str = Field(default="auto", min_length=3, max_length=24)


class CategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class CloneGenerateRequest(BaseModel):
    profile_id: str = Field(min_length=8, max_length=64)
    text: str = Field(min_length=1, max_length=MAX_TEXT)
    language: str = Field(default="auto", min_length=1, max_length=24)
    engine: str = Field(default="auto", min_length=3, max_length=24)
    preset: str = Field(default="narration", min_length=3, max_length=24)
    speed: float = Field(default=1.0, ge=0.7, le=1.3)
    settings: dict[str, Any] = Field(default_factory=dict)
    title: str = Field(default="", max_length=180)
    source_filename: str = Field(default="", max_length=MAX_SOURCE_FILENAME)
    category: str = Field(default="", max_length=80)
    drive_folder: str = Field(default="", max_length=180)
    batch_id: str = Field(default="", max_length=64)


class CloneBatchItem(BaseModel):
    source_filename: str = Field(min_length=5, max_length=MAX_SOURCE_FILENAME)
    text: str = Field(min_length=1, max_length=MAX_TEXT)
    title: str = Field(default="", max_length=180)


class CloneBatchRequest(BaseModel):
    profile_id: str = Field(min_length=8, max_length=64)
    batch_name: str = Field(default="", max_length=180)
    category: str = Field(default="", max_length=80)
    drive_folder: str = Field(default="", max_length=180)
    items: list[CloneBatchItem] = Field(min_length=1, max_length=MAX_BATCH)
    language: str = Field(default="auto", min_length=1, max_length=24)
    engine: str = Field(default="auto", min_length=3, max_length=24)
    preset: str = Field(default="narration", min_length=3, max_length=24)
    speed: float = Field(default=1.0, ge=0.7, le=1.3)
    settings: dict[str, Any] = Field(default_factory=dict)


def _language_base(value: str) -> str:
    return (value or "auto").strip().lower().replace("_", "-").split("-", 1)[0]


def _resolve_engine(requested: str, language: str) -> str:
    requested = (requested or "auto").strip().lower()
    lang = _language_base(language)
    if requested not in GPU_ENGINES:
        raise HTTPException(400, "Unknown GPU engine")
    if requested == "auto":
        return "indicf5" if lang in INDICF5_LANGUAGES else "chatterbox"
    if requested == "indicf5" and lang != "auto" and lang not in INDICF5_LANGUAGES:
        raise HTTPException(400, "IndicF5 does not support the selected language")
    if requested == "omnivoice" and lang != "auto" and lang not in OMNIVOICE_LANGUAGES:
        raise HTTPException(400, "OmniVoice supports Hindi, Marathi, Gujarati, Bengali and Arabic in this deployment")
    if requested == "chatterbox" and lang != "auto" and lang not in CHATTERBOX_LANGUAGES:
        raise HTTPException(400, "Chatterbox does not officially support the selected language")
    return requested


def _sanitize_settings(engine: str, raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise HTTPException(400, "GPU settings must be an object")
    if engine == "chatterbox":
        allowed = {"exaggeration", "cfg_weight", "temperature", "seed", "min_p", "top_p", "repetition_penalty"}
        defaults = {"exaggeration": 0.5, "cfg_weight": 0.5, "temperature": 0.8, "seed": 0, "min_p": 0.05, "top_p": 1.0, "repetition_penalty": 1.2}
    elif engine == "omnivoice":
        allowed = {"num_step"}
        defaults = {"num_step": 32}
    else:
        allowed = {"cfg_strength", "nfe_step", "cross_fade_duration", "seed", "remove_silence"}
        defaults = {"cfg_strength": 2.0, "nfe_step": 32, "cross_fade_duration": 0.15, "seed": -1, "remove_silence": False}
    unknown = set(raw) - allowed
    if unknown:
        raise HTTPException(400, "Unsupported GPU setting: " + sorted(unknown)[0])
    out = {**defaults, **raw}
    if engine == "chatterbox":
        out["exaggeration"] = min(2.0, max(0.25, float(out["exaggeration"])))
        out["cfg_weight"] = min(1.0, max(0.0, float(out["cfg_weight"])))
        out["temperature"] = min(5.0, max(0.05, float(out["temperature"])))
        out["min_p"] = min(1.0, max(0.0, float(out["min_p"])))
        out["top_p"] = min(1.0, max(0.0, float(out["top_p"])))
        out["repetition_penalty"] = min(2.0, max(1.0, float(out["repetition_penalty"])))
        out["seed"] = int(out["seed"])
    elif engine == "omnivoice":
        out["num_step"] = min(64, max(8, int(out["num_step"])))
    else:
        out["cfg_strength"] = min(5.0, max(0.0, float(out["cfg_strength"])))
        out["nfe_step"] = min(64, max(8, int(out["nfe_step"])))
        out["cross_fade_duration"] = min(1.0, max(0.0, float(out["cross_fade_duration"])))
        out["seed"] = int(out["seed"])
        out["remove_silence"] = bool(out["remove_silence"])
    return out


def _validate_category_name(value: str) -> str:
    value = (value or "").strip()
    if not value or len(value) > 80 or any(ord(ch) < 32 for ch in value) or "/" in value or "\\" in value:
        raise HTTPException(400, "Category name is invalid")
    return value


def _resolve_category(con: sqlite3.Connection, value: str) -> str:
    username = owner()
    con.execute(
        "INSERT OR IGNORE INTO voice_owner_categories(owner_username,name,created_at) VALUES(?,?,?)",
        (username, "General", utc_now()),
    )
    name = _validate_category_name(value) if (value or "").strip() else "General"
    row = con.execute(
        "SELECT name FROM voice_owner_categories WHERE owner_username=? AND name=? COLLATE NOCASE",
        (username, name),
    ).fetchone()
    if not row:
        raise HTTPException(400, "Selected category no longer exists")
    return str(row["name"])


def _validate_drive_folder(value: str) -> str:
    """New Voice outputs go to a tenant-specific Drive folder; old jobs stay unchanged."""
    value = (value or "").strip()
    if len(value) > 150 or value in {".", ".."} or "/" in value or chr(92) in value or any(ord(ch) < 32 for ch in value):
        raise HTTPException(400, "Output folder is invalid")
    prefix = "vs_" + owner() + "__"
    if not value:
        return prefix + "default"
    if value.casefold().startswith(prefix.casefold()):
        return value
    if value.casefold().startswith("vs_"):
        raise HTTPException(403, "Another Voice user's folder is unavailable")
    return prefix + value


def _validate_txt_filename(value: str) -> tuple[str, str]:
    value = (value or "").strip()
    if not value or len(value) > MAX_SOURCE_FILENAME:
        raise HTTPException(400, "TXT filename is invalid")
    if value in {".", ".."} or "/" in value or "\\" in value or any(ord(ch) < 32 for ch in value):
        raise HTTPException(400, "TXT filename must be a plain filename")
    p = Path(value)
    if p.suffix.lower() != ".txt":
        raise HTTPException(400, "Batch files must use the .txt extension")
    stem = p.stem
    if not stem or not stem.strip(" ."):
        raise HTTPException(400, "TXT filename needs a usable base name")
    return value, stem


def _direct_filename(title: str, job_id: str) -> str:
    value = (title or "").strip()
    value = re.sub(r"[\\/\x00-\x1f]+", "-", value).strip(" .")
    if not value:
        value = job_id
    value = value[:140]
    return f"{value}.txt"


def _load_profile_for_job(con: sqlite3.Connection, profile_id: str, engine: str) -> sqlite3.Row:
    profile = con.execute("SELECT * FROM profiles WHERE profile_id=? AND owner_username=?", (profile_id, owner())).fetchone()
    if not profile:
        raise HTTPException(404, "Clone profile not found")
    profile_engine = str(profile["engine_pref"] or "auto").strip().lower()
    if profile_engine == "chatterbox" and engine != "chatterbox":
        raise HTTPException(400, "This profile is configured for Chatterbox only")
    if profile_engine == "indicf5" and engine != "indicf5":
        raise HTTPException(400, "This profile is configured for IndicF5 only")
    if profile_engine == "omnivoice" and engine != "omnivoice":
        raise HTTPException(400, "This profile is configured for OmniVoice only")
    if engine in {"indicf5", "omnivoice"} and not str(profile["reference_text"] or "").strip():
        raise HTTPException(400, "This profile needs a reference transcript before it can use this engine")
    return profile


def _write_text_sidecar(job_id: str, source_filename: str, text: str) -> str:
    rel = Path(job_id) / source_filename
    folder = TEXT_DIR / job_id
    folder.mkdir(parents=True, exist_ok=False)
    path = TEXT_DIR / rel
    path.write_text(text, encoding="utf-8")
    if path.stat().st_size <= 0:
        shutil.rmtree(folder, ignore_errors=True)
        raise HTTPException(400, "TXT script is empty")
    return rel.as_posix()


def _cleanup_text_rel(rel: str) -> None:
    if not rel:
        return
    path = (TEXT_DIR / rel).resolve()
    root = TEXT_DIR.resolve()
    try:
        path.relative_to(root)
    except Exception:
        return
    path.unlink(missing_ok=True)
    try:
        path.parent.rmdir()
    except OSError:
        pass


@router.get("/tts/api/clone/worker/config")
def get_worker_connection():
    init_storage()
    return _public_worker_config()


@router.post("/tts/api/clone/worker/test")
def test_worker_connection(payload: WorkerConnectionPayload):
    config = _validated_worker_payload(payload, enabled=True)
    result = _probe_worker_config(config, token_override=payload.token, timeout_seconds=min(15, payload.request_timeout))
    return {"ok": bool(result["ok"]), "saved": False, "provider": "SaladCloud", **result}


@router.post("/tts/api/clone/worker/config")
def save_worker_connection(payload: WorkerConnectionPayload):
    config = _validated_worker_payload(payload)
    _save_worker_connection(config, payload.token)
    return {"ok": True, "saved": True, **_public_worker_config()}


@router.post("/tts/api/clone/worker/connect")
def connect_worker(payload: WorkerConnectionPayload):
    config = _validated_worker_payload(payload, enabled=True)
    result = _probe_worker_config(config, token_override=payload.token, timeout_seconds=min(15, payload.request_timeout))
    if not result["ok"]:
        return {"ok": False, "saved": False, "provider": "SaladCloud", **result}
    _save_worker_connection(config, payload.token)
    return {"ok": True, "saved": True, "provider": "SaladCloud", **result, "config": _public_worker_config()}


@router.post("/tts/api/clone/worker/disconnect")
def disconnect_worker():
    current = effective_worker_config()
    endpoint = str(current.get("saved_endpoint_url") or current.get("endpoint_url") or "").strip()
    config = {
        "provider": "SaladCloud",
        "endpoint_url": endpoint,
        "health_path": current.get("health_path") or "/healthz",
        "generation_path": current.get("generation_path") or "/v1/clone/generate",
        "request_timeout": int(current.get("request_timeout") or 300),
        "auth_mode": current.get("auth_mode") or "none",
        "enabled": False,
        "updated_at": utc_now(),
    }
    _save_worker_connection(config)
    return {"ok": True, "disconnected": True, **_public_worker_config()}


@router.post("/tts/api/clone/worker/test-generation")
def test_worker_generation(profile_id: str = Query(min_length=8, max_length=64)):
    init_storage()
    config = effective_worker_config()
    worker = str(config.get("endpoint_url") or "")
    if not worker:
        return {"ok": False, "error": "worker_not_connected"}
    probe = _probe_worker_config(config, timeout_seconds=min(15, int(config.get("request_timeout") or 300)))
    if not probe["ok"]:
        return {"ok": False, "error": probe.get("error") or "worker_offline", **probe}
    with db() as con:
        row = con.execute("SELECT * FROM profiles WHERE profile_id=?", (profile_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Voice profile not found")
    sample = SAMPLES_DIR / str(row["sample_file"])
    if not sample.is_file():
        raise HTTPException(status_code=409, detail="Reference sample is missing")
    lang = str(row["reference_language"] or "auto")
    engine = str(row["engine_pref"] or "auto")
    if engine == "auto":
        base_lang = lang.lower().replace("_", "-").split("-", 1)[0]
        engine = "indicf5" if base_lang in INDICF5_LANGUAGES else "chatterbox"
    payload = {
        "job_id": "connection-test-" + uuid.uuid4().hex[:12],
        "profile_id": profile_id,
        "profile_name": str(row["name"]),
        "text": "GPU connection test successful.",
        "language": lang,
        "engine": engine,
        "preset": "narration",
        "speed": 1.0,
        "settings": {},
        "reference_language": lang,
        "reference_text": str(row["reference_text"] or ""),
        "sample_wav_base64": base64.b64encode(sample.read_bytes()).decode("ascii"),
        "output_format": "wav",
    }
    started = time.monotonic()
    try:
        resp = requests.post(
            worker + str(config.get("generation_path") or "/v1/clone/generate"),
            headers={"Content-Type": "application/json", **_worker_auth_headers()},
            data=json.dumps(payload),
            timeout=int(config.get("request_timeout") or 300),
        )
    except requests.RequestException as exc:
        return {"ok": False, "error": str(exc)[:180], "latency_ms": int((time.monotonic() - started) * 1000)}
    latency = int((time.monotonic() - started) * 1000)
    if resp.status_code >= 400:
        return {"ok": False, "error": f"HTTP {resp.status_code}", "http_status": resp.status_code, "latency_ms": latency}
    try:
        ctype = (resp.headers.get("content-type") or "").lower()
        if ctype.startswith("audio/") or ctype == "application/octet-stream":
            audio = resp.content
        else:
            data = resp.json()
            raw_b64 = data.get("audio_base64") or data.get("audio_b64")
            if not raw_b64:
                raise ValueError("GPU worker response did not include audio")
            audio = base64.b64decode(raw_b64)
        if len(audio) < 1000 or len(audio) > 100 * 1024 * 1024:
            raise ValueError("GPU worker returned invalid audio size")
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:180], "http_status": resp.status_code, "latency_ms": latency}
    return {"ok": True, "http_status": resp.status_code, "latency_ms": latency, "audio_bytes": len(audio), "profile_id": profile_id}



class OmniVoiceConnectionPayload(BaseModel):
    endpoint_url: str = Field(min_length=8, max_length=500)
    health_path: str = Field(default="/healthz", min_length=1, max_length=120)
    ready_path: str = Field(default="/readyz", min_length=1, max_length=120)
    request_timeout: int = Field(default=300, ge=10, le=1800)
    salad_api_key: str = Field(default="", max_length=4096)
    api_token: str = Field(default="", max_length=4096)
    enabled: bool = True


class OmniVoiceDesignPreviewPayload(BaseModel):
    instruct: str = Field(min_length=1, max_length=1200)
    sample_text: str = Field(min_length=1, max_length=500)
    language: str = Field(default="hi", min_length=2, max_length=8)
    num_step: int = Field(default=32, ge=8, le=64)
    speed: float = Field(default=1.0, ge=0.5, le=2.0)


class OmniVoiceDesignApprovePayload(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    provenance_note: str = Field(default="", max_length=240)


def _validated_omnivoice_payload(payload: OmniVoiceConnectionPayload, enabled: bool | None = None) -> dict[str, Any]:
    try:
        endpoint = _validate_salad_endpoint(payload.endpoint_url)
        health = _clean_worker_path(payload.health_path, "/healthz")
        ready = _clean_worker_path(payload.ready_path, "/readyz")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "provider": "SaladCloud",
        "engine": "omnivoice",
        "endpoint_url": endpoint,
        "health_path": health,
        "ready_path": ready,
        "generation_path": "/v1/jobs",
        "request_timeout": int(payload.request_timeout),
        "enabled": bool(payload.enabled if enabled is None else enabled),
        "updated_at": utc_now(),
    }


def _save_omnivoice_connection(config: dict[str, Any], salad_api_key: str = "", api_token: str = "") -> None:
    init_storage()
    if str(salad_api_key or "").strip():
        _atomic_write(omnivoice_salad_key_file(), str(salad_api_key).strip() + "\n")
    if str(api_token or "").strip():
        _atomic_write(omnivoice_api_token_file(), str(api_token).strip() + "\n")
    _atomic_write(OMNIVOICE_CONFIG_FILE, json.dumps(config, indent=2, sort_keys=True) + "\n")


def _public_omnivoice_config() -> dict[str, Any]:
    cfg = effective_omnivoice_config()
    online, error = omnivoice_worker_health(timeout_seconds=3.0) if cfg.get("endpoint_url") else (False, "not_configured")
    return {
        "ok": True,
        "provider": "SaladCloud",
        "engine": "omnivoice",
        "source": cfg.get("source"),
        "endpoint_url": cfg.get("saved_endpoint_url") or "",
        "health_path": cfg.get("health_path") or "/healthz",
        "ready_path": cfg.get("ready_path") or "/readyz",
        "generation_path": "/v1/jobs",
        "request_timeout": int(cfg.get("request_timeout") or 300),
        "enabled": bool(cfg.get("enabled")),
        "salad_api_key_configured": bool(_read_secret(omnivoice_salad_key_file())),
        "api_token_configured": bool(_read_secret(omnivoice_api_token_file())),
        "worker_online": online,
        "worker_error": error,
    }


@router.get("/tts/api/clone/omnivoice/config")
def get_omnivoice_connection():
    init_storage()
    return _public_omnivoice_config()


@router.post("/tts/api/clone/omnivoice/test")
def test_omnivoice_connection(payload: OmniVoiceConnectionPayload):
    config = _validated_omnivoice_payload(payload, enabled=True)
    result = _probe_omnivoice_config(
        config,
        salad_key_override=payload.salad_api_key,
        api_token_override=payload.api_token,
        timeout_seconds=min(15, payload.request_timeout),
    )
    return {"ok": bool(result["ok"]), "saved": False, "provider": "SaladCloud", "engine": "omnivoice", **result}


@router.post("/tts/api/clone/omnivoice/config")
def save_omnivoice_connection(payload: OmniVoiceConnectionPayload):
    config = _validated_omnivoice_payload(payload)
    _save_omnivoice_connection(config, payload.salad_api_key, payload.api_token)
    return {"ok": True, "saved": True, **_public_omnivoice_config()}


@router.post("/tts/api/clone/omnivoice/connect")
def connect_omnivoice_worker(payload: OmniVoiceConnectionPayload):
    config = _validated_omnivoice_payload(payload, enabled=True)
    result = _probe_omnivoice_config(
        config,
        salad_key_override=payload.salad_api_key,
        api_token_override=payload.api_token,
        timeout_seconds=min(15, payload.request_timeout),
    )
    if not result["ok"]:
        return {"ok": False, "saved": False, "provider": "SaladCloud", "engine": "omnivoice", **result}
    _save_omnivoice_connection(config, payload.salad_api_key, payload.api_token)
    return {"ok": True, "saved": True, "provider": "SaladCloud", "engine": "omnivoice", **result, "config": _public_omnivoice_config()}


@router.post("/tts/api/clone/omnivoice/disconnect")
def disconnect_omnivoice_worker():
    current = effective_omnivoice_config()
    config = {
        "provider": "SaladCloud",
        "engine": "omnivoice",
        "endpoint_url": str(current.get("saved_endpoint_url") or current.get("endpoint_url") or "").strip(),
        "health_path": current.get("health_path") or "/healthz",
        "ready_path": current.get("ready_path") or "/readyz",
        "generation_path": "/v1/jobs",
        "request_timeout": int(current.get("request_timeout") or 300),
        "enabled": False,
        "updated_at": utc_now(),
    }
    _save_omnivoice_connection(config)
    return {"ok": True, "disconnected": True, **_public_omnivoice_config()}


def _omnivoice_language(value: str) -> str:
    lang = _language_base(value)
    return "arb" if lang == "ar" else lang


def _omnivoice_wait_audio(worker: str, remote_job_id: str, timeout: int, headers: dict[str, str]) -> bytes:
    deadline = time.monotonic() + max(60, timeout)
    while time.monotonic() < deadline:
        r = requests.get(worker + f"/v1/jobs/{remote_job_id}", headers=headers, timeout=min(20, timeout))
        if r.status_code >= 400:
            raise RuntimeError(f"OmniVoice status HTTP {r.status_code}")
        data = r.json()
        state = str(data.get("state") or "").strip().lower()
        if state == "completed":
            a = requests.get(worker + f"/v1/jobs/{remote_job_id}/audio", headers=headers, timeout=min(60, timeout))
            if a.status_code >= 400:
                raise RuntimeError(f"OmniVoice audio HTTP {a.status_code}")
            return a.content
        if state == "failed":
            raise RuntimeError(str(data.get("error") or "OmniVoice generation failed")[:350])
        if state not in {"queued", "running"}:
            raise RuntimeError(f"OmniVoice returned unknown job state: {state or 'empty'}")
        time.sleep(1.0)
    raise RuntimeError("OmniVoice generation test timed out")



# VPS3_OMNIVOICE_REUSABLE_PROFILES_DESIGN_V1
def _omnivoice_profile_value(profile: Any, key: str, default: Any = "") -> Any:
    try:
        value = profile[key]
    except Exception:
        try:
            value = profile.get(key, default)
        except Exception:
            value = default
    return default if value is None else value


def _omnivoice_remote_detail(resp: requests.Response) -> str:
    try:
        data = resp.json()
        if isinstance(data, dict):
            detail = data.get("detail") or data.get("error") or data.get("message")
            if detail:
                return str(detail)[:300]
    except Exception:
        pass
    return (resp.text or "").strip().replace("\x00", "")[:300]


def ensure_omnivoice_remote_profile(profile: Any, sample: Path, worker: str = "") -> dict[str, Any]:
    # VPS3 is source-of-truth. Upload reference only when the remote reusable profile is absent/stale.
    cfg = effective_omnivoice_config()
    endpoint = str(worker or cfg.get("endpoint_url") or "").strip().rstrip("/")
    if not endpoint:
        raise RuntimeError("OmniVoice worker is not connected")
    voice_id = str(_omnivoice_profile_value(profile, "profile_id")).strip()
    ref_text = str(_omnivoice_profile_value(profile, "reference_text")).strip()
    if not voice_id:
        raise RuntimeError("OmniVoice profile has no stable profile_id")
    if not ref_text:
        raise RuntimeError("OmniVoice profile requires the exact reference transcript")
    if not sample.is_file():
        raise RuntimeError("OmniVoice reference sample is missing on VPS3")

    headers = {"Content-Type": "application/json", **omnivoice_auth_headers()}
    source_sha = str(_omnivoice_profile_value(profile, "sample_sha256")).strip()
    check = requests.get(endpoint + f"/v1/voices/{voice_id}", headers=headers, timeout=20)
    replace_remote = False
    if check.status_code == 200:
        try:
            remote = check.json()
        except Exception as exc:
            raise RuntimeError("OmniVoice reusable profile lookup returned invalid JSON") from exc
        remote_sha = str(remote.get("source_sample_sha256") or "")
        if not remote_sha or not source_sha or remote_sha == source_sha:
            return {"voice_id": voice_id, "created": False, "remote": remote}
        replace_remote = True
    elif check.status_code != 404:
        detail = _omnivoice_remote_detail(check)
        raise RuntimeError(
            f"OmniVoice reusable profile lookup HTTP {check.status_code}" +
            (f": {detail}" if detail else "")
        )

    metadata = {
        "source_profile_id": voice_id,
        "source_profile_name": str(_omnivoice_profile_value(profile, "name")),
        "source_sample_sha256": source_sha,
        "source_filename": str(_omnivoice_profile_value(profile, "source_filename")),
        "reference_language": str(_omnivoice_profile_value(profile, "reference_language", "auto")),
        "profile_type": str(_omnivoice_profile_value(profile, "profile_type", "cloned")),
        "design_prompt": str(_omnivoice_profile_value(profile, "design_prompt", "")),
        "vps3_master": True,
    }
    create = requests.post(
        endpoint + "/v1/voices",
        headers=headers,
        json={
            "voice_id": voice_id,
            "ref_audio_b64": base64.b64encode(sample.read_bytes()).decode("ascii"),
            "ref_text": ref_text,
            "metadata": metadata,
            "replace": replace_remote,
        },
        timeout=max(60, min(300, int(cfg.get("request_timeout") or 300))),
    )
    if create.status_code >= 400:
        detail = _omnivoice_remote_detail(create)
        if create.status_code == 404:
            raise RuntimeError(
                "OmniVoice GPU runtime does not expose reusable voice profiles yet; deploy the updated runtime image"
            )
        raise RuntimeError(
            f"OmniVoice reusable profile sync HTTP {create.status_code}" +
            (f": {detail}" if detail else "")
        )
    try:
        remote = create.json()
    except Exception as exc:
        raise RuntimeError("OmniVoice reusable profile sync returned invalid JSON") from exc
    return {"voice_id": voice_id, "created": True, "remote": remote}


def _safe_omnivoice_preview_id(preview_id: str) -> str:
    value = str(preview_id or "").strip().lower()
    if not re.fullmatch(r"[a-f0-9]{32}", value):
        raise HTTPException(status_code=400, detail="Invalid OmniVoice preview id")
    return value


def _omnivoice_design_endpoint() -> tuple[str, dict[str, str], int]:
    cfg = effective_omnivoice_config()
    worker = str(cfg.get("endpoint_url") or "").strip().rstrip("/")
    if not worker:
        raise HTTPException(status_code=409, detail="OmniVoice worker is not connected")
    return worker, {"Content-Type": "application/json", **omnivoice_auth_headers()}, int(cfg.get("request_timeout") or 300)


def _require_design_preview_owner(preview_id: str) -> str:
    preview_id = _safe_omnivoice_preview_id(preview_id)
    init_storage()
    with db() as con:
        row = con.execute(
            "SELECT 1 FROM voice_design_preview_owners WHERE preview_id=? AND owner_username=?",
            (preview_id, owner()),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Voice design preview not found")
    return preview_id


@router.post("/tts/api/clone/omnivoice/design/preview")
def create_omnivoice_design_preview(payload: OmniVoiceDesignPreviewPayload):
    init_storage()
    worker, headers, timeout = _omnivoice_design_endpoint()
    lang = _language_base(payload.language)
    if lang not in OMNIVOICE_LANGUAGES:
        raise HTTPException(status_code=400, detail="Voice Design supports Hindi, Marathi, Gujarati, Bengali and Arabic")
    try:
        resp = requests.post(
            worker + "/v1/voice-design/previews",
            headers=headers,
            json={
                "instruct": payload.instruct.strip(),
                "sample_text": payload.sample_text.strip(),
                "language": _omnivoice_language(lang),
                "num_step": payload.num_step,
                "speed": payload.speed,
            },
            timeout=max(30, min(timeout, 300)),
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"OmniVoice design preview unavailable: {exc.__class__.__name__}") from exc
    if resp.status_code >= 400:
        detail = _omnivoice_remote_detail(resp)
        if resp.status_code == 404:
            detail = "Updated OmniVoice runtime with Voice Design API is not deployed yet"
        raise HTTPException(status_code=502, detail=detail or f"OmniVoice preview HTTP {resp.status_code}")
    result = resp.json()
    candidate = result.get("preview") if isinstance(result, dict) else None
    candidate = candidate if isinstance(candidate, dict) else {}
    preview_id = str(result.get("preview_id") or candidate.get("preview_id") or candidate.get("id") or "") if isinstance(result, dict) else ""
    preview_id = _safe_omnivoice_preview_id(preview_id)
    with db() as con:
        con.execute(
            "INSERT INTO voice_design_preview_owners(preview_id,owner_username) VALUES(?,?)",
            (preview_id,owner()),
        )
    return result


@router.get("/tts/api/clone/omnivoice/design/previews/{preview_id}")
def get_omnivoice_design_preview(preview_id: str):
    preview_id = _require_design_preview_owner(preview_id)
    worker, headers, _ = _omnivoice_design_endpoint()
    resp = requests.get(worker + f"/v1/voice-design/previews/{preview_id}", headers=headers, timeout=20)
    if resp.status_code >= 400:
        raise HTTPException(status_code=404 if resp.status_code == 404 else 502, detail=_omnivoice_remote_detail(resp) or "Preview unavailable")
    return resp.json()


@router.get("/tts/api/clone/omnivoice/design/previews/{preview_id}/audio")
def get_omnivoice_design_preview_audio(preview_id: str):
    preview_id = _require_design_preview_owner(preview_id)
    worker, headers, _ = _omnivoice_design_endpoint()
    resp = requests.get(worker + f"/v1/voice-design/previews/{preview_id}/audio", headers=headers, timeout=60)
    if resp.status_code >= 400:
        raise HTTPException(status_code=404 if resp.status_code == 404 else 502, detail=_omnivoice_remote_detail(resp) or "Preview audio unavailable")
    return Response(content=resp.content, media_type="audio/wav", headers={"Cache-Control": "no-store"})


@router.delete("/tts/api/clone/omnivoice/design/previews/{preview_id}")
def delete_omnivoice_design_preview(preview_id: str):
    preview_id = _require_design_preview_owner(preview_id)
    worker, headers, _ = _omnivoice_design_endpoint()
    resp = requests.delete(worker + f"/v1/voice-design/previews/{preview_id}", headers=headers, timeout=20)
    if resp.status_code not in {200, 204, 404}:
        raise HTTPException(status_code=502, detail=_omnivoice_remote_detail(resp) or "Preview cleanup failed")
    with db() as con:
        con.execute("DELETE FROM voice_design_preview_owners WHERE preview_id=? AND owner_username=?", (preview_id,owner()))
    return {"ok": True, "deleted": resp.status_code != 404, "preview_id": preview_id}


@router.post("/tts/api/clone/omnivoice/design/previews/{preview_id}/approve")
def approve_omnivoice_design_preview(preview_id: str, payload: OmniVoiceDesignApprovePayload):
    init_storage()
    preview_id = _require_design_preview_owner(preview_id)
    worker, headers, timeout = _omnivoice_design_endpoint()

    meta_resp = requests.get(worker + f"/v1/voice-design/previews/{preview_id}", headers=headers, timeout=20)
    if meta_resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=_omnivoice_remote_detail(meta_resp) or "Preview metadata unavailable")
    meta_payload = meta_resp.json()
    preview = meta_payload.get("preview") if isinstance(meta_payload, dict) else None
    if not isinstance(preview, dict):
        raise HTTPException(status_code=502, detail="OmniVoice preview metadata is invalid")

    audio_resp = requests.get(worker + f"/v1/voice-design/previews/{preview_id}/audio", headers=headers, timeout=60)
    if audio_resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=_omnivoice_remote_detail(audio_resp) or "Preview audio unavailable")
    if len(audio_resp.content) < 1000 or len(audio_resp.content) > MAX_SAMPLE_BYTES:
        raise HTTPException(status_code=502, detail="OmniVoice preview audio size is invalid")

    profile_id = "CLV-" + uuid.uuid4().hex[:12].upper()
    source_filename = f"omnivoice-design-{preview_id}.wav"
    sample_path, duration, sample_sha = _normalize_reference(audio_resp.content, source_filename, profile_id)
    created = utc_now()
    remote_language = str(preview.get("language") or "hi")
    reference_language = "ar" if remote_language == "arb" else _language_base(remote_language)
    reference_text = str(preview.get("sample_text") or "").strip()
    design_prompt = str(preview.get("instruct") or "").strip()
    if not reference_text or not design_prompt:
        sample_path.unlink(missing_ok=True)
        raise HTTPException(status_code=502, detail="OmniVoice preview metadata is missing prompt or sample text")

    try:
        with db() as con:
            con.execute(
                "INSERT INTO profiles(profile_id,name,source_filename,sample_file,sample_sha256,duration_seconds,"
                "provenance_note,consent_confirmed,created_at,reference_language,reference_text,engine_pref,profile_type,design_prompt,owner_username) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    profile_id,
                    payload.name.strip(),
                    source_filename,
                    sample_path.name,
                    sample_sha,
                    duration,
                    payload.provenance_note.strip() or "OmniVoice prompt-designed voice",
                    1,
                    created,
                    reference_language,
                    reference_text,
                    "omnivoice",
                    "designed",
                    design_prompt,
                    owner(),
                ),
            )
            row = con.execute("SELECT * FROM profiles WHERE profile_id=?", (profile_id,)).fetchone()
    except Exception:
        sample_path.unlink(missing_ok=True)
        raise

    remote_synced = False
    remote_error = ""
    try:
        approved = requests.post(
            worker + f"/v1/voice-design/previews/{preview_id}/approve",
            headers=headers,
            json={"voice_id": profile_id, "replace": False},
            timeout=max(60, min(timeout, 300)),
        )
        if approved.status_code < 400:
            remote_synced = True
            try:
                requests.delete(worker + f"/v1/voice-design/previews/{preview_id}", headers=headers, timeout=20)
            except requests.RequestException:
                pass
        else:
            remote_error = _omnivoice_remote_detail(approved) or f"HTTP {approved.status_code}"
    except requests.RequestException as exc:
        remote_error = f"{exc.__class__.__name__}"

    return {
        "ok": True,
        "profile": _profile_payload(row),
        "remote_synced": remote_synced,
        "remote_error": remote_error,
        "note": "VPS3 master profile is safe; remote profile will be recreated from VPS3 automatically if needed.",
    }


@router.post("/tts/api/clone/omnivoice/test-generation")
def test_omnivoice_generation(profile_id: str = Query(min_length=8, max_length=64)):
    init_storage()
    config = effective_omnivoice_config()
    worker = str(config.get("endpoint_url") or "")
    if not worker:
        return {"ok": False, "error": "worker_not_connected"}
    probe = _probe_omnivoice_config(config, timeout_seconds=min(15, int(config.get("request_timeout") or 300)))
    if not probe["ok"]:
        return {"ok": False, "error": probe.get("error") or "worker_offline", **probe}
    with db() as con:
        row = con.execute("SELECT * FROM profiles WHERE profile_id=?", (profile_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Voice profile not found")
    sample = SAMPLES_DIR / str(row["sample_file"])
    if not sample.is_file():
        raise HTTPException(status_code=409, detail="Reference sample is missing")
    ref_text = str(row["reference_text"] or "").strip()
    if not ref_text:
        raise HTTPException(status_code=409, detail="OmniVoice requires the exact reference transcript")
    lang = _language_base(str(row["reference_language"] or "hi"))
    if lang not in OMNIVOICE_LANGUAGES:
        lang = "hi"
    payload = {
        "text": "नमस्ते, यह ओम्नीवॉइस कनेक्शन परीक्षण है।" if lang == "hi" else "OmniVoice connection test.",
        "language": _omnivoice_language(lang),
        "mode": "clone",
        "voice_id": profile_id,
        "num_step": 32,
        "speed": 1.0,
    }
    headers = {"Content-Type": "application/json", **omnivoice_auth_headers()}
    started = time.monotonic()
    try:
        sync = ensure_omnivoice_remote_profile(row, sample, worker)
        r = requests.post(worker + "/v1/jobs", headers=headers, json=payload, timeout=20)
        if r.status_code >= 400:
            return {"ok": False, "error": f"HTTP {r.status_code}", "http_status": r.status_code}
        remote_job_id = str(r.json().get("job_id") or "")
        if not remote_job_id:
            raise RuntimeError("OmniVoice submit response did not include job_id")
        audio = _omnivoice_wait_audio(worker, remote_job_id, int(config.get("request_timeout") or 300), headers)
        if len(audio) < 1000 or len(audio) > 100 * 1024 * 1024:
            raise RuntimeError("OmniVoice returned invalid audio size")
        latency = int((time.monotonic() - started) * 1000)
        return {"ok": True, "http_status": 200, "latency_ms": latency, "audio_bytes": len(audio), "profile_id": profile_id, "reusable_profile_created": bool(sync.get("created"))}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:180], "latency_ms": int((time.monotonic() - started) * 1000)}


@router.get("/tts/api/clone/status")
def clone_status():
    init_storage()
    configured = bool(WORKER_URL())
    online, worker_error = worker_health() if configured else (False, "not_configured")
    counts = {s: 0 for s in ALLOWED_JOB_STATUSES}
    delivery_counts = {s: 0 for s in DELIVERY_STATUSES}
    with db() as con:
        for row in con.execute("SELECT status,COUNT(*) n FROM jobs GROUP BY status"):
            if row["status"] in counts:
                counts[str(row["status"])] = int(row["n"])
        for row in con.execute("SELECT delivery_status,COUNT(*) n FROM jobs GROUP BY delivery_status"):
            if row["delivery_status"] in delivery_counts:
                delivery_counts[str(row["delivery_status"])] = int(row["n"])
        profiles = int(con.execute("SELECT COUNT(*) FROM profiles").fetchone()[0])
        categories = int(con.execute("SELECT COUNT(*) FROM categories").fetchone()[0])
    return {
        "ok": True,
        "mode": "remote-gpu-worker",
        "worker_configured": configured,
        "worker_online": online,
        "worker_error": worker_error,
        "profiles": profiles,
        "categories": categories,
        "counts": counts,
        "delivery_counts": delivery_counts,
        "max_text": MAX_TEXT,
        "max_batch": MAX_BATCH,
        "sample_limit_mb": 12,
        "reference_seconds": {"min": 2, "max": 120, "recommended": "10-15 for GPU profiles"},
        "engines": {
            "chatterbox": {"label": "Chatterbox Multilingual", "languages": sorted(CHATTERBOX_LANGUAGES), "reference_text_required": False},
            "indicf5": {"label": "IndicF5", "languages": sorted(INDICF5_LANGUAGES), "reference_text_required": True},
            "omnivoice": {"label": "OmniVoice", "languages": sorted(OMNIVOICE_LANGUAGES), "reference_text_required": True},
        },
        "omnivoice": _public_omnivoice_config(),
        "auto_route": {"indicf5": sorted(INDICF5_LANGUAGES), "fallback": "chatterbox"},
        "batch_delivery": {"txt_audio_pair": True, "exact_basename": True, "drive_verified_before_cleanup": True},
    }


@router.get("/tts/api/clone/categories")
def list_categories():
    init_storage()
    username = owner()
    with db() as con:
        con.execute(
            "INSERT OR IGNORE INTO voice_owner_categories(owner_username,name,created_at) VALUES(?,?,?)",
            (username, "General", utc_now()),
        )
        rows = con.execute(
            "SELECT name,created_at FROM voice_owner_categories WHERE owner_username=? ORDER BY created_at,name COLLATE NOCASE",
            (username,),
        ).fetchall()
    return {"ok": True, "categories": [{"name": str(r["name"]), "created_at": str(r["created_at"])} for r in rows]}


@router.post("/tts/api/clone/categories")
def create_category(payload: CategoryCreate):
    init_storage()
    name = _validate_category_name(payload.name)
    username = owner()
    with db() as con:
        exists = con.execute("SELECT name FROM voice_owner_categories WHERE owner_username=? AND name=? COLLATE NOCASE",(username,name)).fetchone()
        if exists:
            return {"ok": True, "category": str(exists["name"]), "created": False}
        con.execute("INSERT INTO voice_owner_categories(owner_username,name,created_at) VALUES(?,?,?)", (username, name, utc_now()))
    return {"ok": True, "category": name, "created": True}


@router.delete("/tts/api/clone/categories/{name}")
def delete_category(name: str):
    init_storage()
    name = _validate_category_name(name)
    username = owner()
    with db() as con:
        row = con.execute("SELECT name FROM voice_owner_categories WHERE owner_username=? AND name=? COLLATE NOCASE",(username,name)).fetchone()
        if not row:
            raise HTTPException(404, "Category not found")
        total = int(con.execute("SELECT COUNT(*) FROM voice_owner_categories WHERE owner_username=?", (username,)).fetchone()[0])
        if total <= 1:
            raise HTTPException(409, "At least one category must remain")
        canonical = str(row["name"])
        con.execute("DELETE FROM voice_owner_categories WHERE owner_username=? AND name=?", (username, canonical))
    return {"ok": True, "deleted": canonical, "note": "Existing jobs keep their saved category"}


@router.get("/tts/api/clone/profiles")
def list_profiles():
    init_storage()
    with db() as con:
        rows = con.execute("SELECT * FROM profiles WHERE owner_username=? ORDER BY created_at DESC",(owner(),)).fetchall()
    return {"ok": True, "profiles": [_profile_payload(r) for r in rows]}


@router.post("/tts/api/clone/profiles")
def create_profile(payload: CloneProfileCreate):
    init_storage()
    if payload.consent is not True:
        raise HTTPException(400, "Permission confirmation is required")
    try:
        raw = base64.b64decode(payload.audio_base64, validate=True)
    except Exception:
        raise HTTPException(400, "Reference audio is not valid base64")
    engine_pref = payload.engine_pref.strip().lower()
    if engine_pref not in GPU_ENGINES:
        raise HTTPException(400, "Unknown profile engine preference")
    reference_text = payload.reference_text.strip()
    if engine_pref in {"auto", "indicf5", "omnivoice"} and not reference_text:
        raise HTTPException(400, "Reference transcript is required for IndicF5/OmniVoice-compatible profiles")
    profile_id = "CLV-" + uuid.uuid4().hex[:12].upper()
    sample_path, duration, sha = _normalize_reference(raw, payload.source_filename, profile_id)
    created = utc_now()
    try:
        with db() as con:
            con.execute(
                "INSERT INTO profiles(profile_id,name,source_filename,sample_file,sample_sha256,duration_seconds,"
                "provenance_note,consent_confirmed,created_at,reference_language,reference_text,engine_pref,owner_username) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    profile_id, payload.name.strip(), _safe_name(payload.source_filename),
                    sample_path.name, sha, duration, payload.provenance_note.strip(), 1, created,
                    payload.reference_language.strip() or "auto", reference_text, engine_pref, owner()
                )
            )
            row = con.execute("SELECT * FROM profiles WHERE profile_id=?", (profile_id,)).fetchone()
        return {"ok": True, "profile": _profile_payload(row)}
    except Exception:
        sample_path.unlink(missing_ok=True)
        raise


@router.delete("/tts/api/clone/profiles/{profile_id}")
def delete_profile(profile_id: str):
    init_storage()
    with db() as con:
        row = con.execute("SELECT * FROM profiles WHERE profile_id=? AND owner_username=?", (profile_id,owner())).fetchone()
        if not row:
            raise HTTPException(404, "Clone profile not found")
        active = int(con.execute(
            "SELECT COUNT(*) FROM jobs WHERE profile_id=? AND (status IN ('waiting_worker','processing') "
            "OR delivery_status IN ('pending','uploading','upload_failed','drive_verified'))",
            (profile_id,)
        ).fetchone()[0])
        if active:
            raise HTTPException(409, "Finish/delete active generation and Drive-delivery jobs before deleting this profile")
        stale = con.execute("SELECT audio_file,text_file FROM jobs WHERE profile_id=?", (profile_id,)).fetchall()
        con.execute("DELETE FROM jobs WHERE profile_id=?", (profile_id,))
        con.execute("DELETE FROM profiles WHERE profile_id=?", (profile_id,))
    for j in stale:
        if str(j["audio_file"] or ""):
            (AUDIO_DIR / str(j["audio_file"])).unlink(missing_ok=True)
        _cleanup_text_rel(str(j["text_file"] or ""))
    (SAMPLES_DIR / str(row["sample_file"])).unlink(missing_ok=True)
    return {"ok": True, "deleted": profile_id}


def _insert_job(
    con: sqlite3.Connection,
    *,
    profile_id: str,
    profile_name: str,
    text: str,
    language: str,
    engine: str,
    preset: str,
    speed: float,
    settings: dict[str, Any],
    batch_id: str,
    batch_name: str,
    title: str,
    source_filename: str,
    output_basename: str,
    category: str,
    drive_folder: str,
    text_file: str,
    job_id: str,
) -> sqlite3.Row:
    con.execute(
        "INSERT INTO jobs(job_id,profile_id,text,language,status,created_at,engine,preset,speed,settings_json,"
        "batch_id,batch_name,title,source_filename,output_basename,category,drive_folder,text_file,delivery_status,owner_username) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'waiting_generation',?)",
        (
            job_id, profile_id, text, language, "waiting_worker", utc_now(), engine, preset, float(speed),
            json.dumps(settings, separators=(",", ":"), sort_keys=True), batch_id, batch_name, title, source_filename,
            output_basename, category, drive_folder, text_file, owner(),
        )
    )
    return con.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()


@router.post("/tts/api/clone/generate")
def queue_clone(payload: CloneGenerateRequest):
    init_storage()
    text = payload.text.strip()
    if not text:
        raise HTTPException(400, "Text is required")
    language = payload.language.strip() or "auto"
    engine = _resolve_engine(payload.engine, language)
    preset = payload.preset.strip().lower()
    if preset not in GPU_PRESETS:
        raise HTTPException(400, "Unknown GPU preset")
    settings = _sanitize_settings(engine, payload.settings)
    drive_folder = _validate_drive_folder(payload.drive_folder)
    job_id = "CLJ-" + uuid.uuid4().hex[:12].upper()
    source_filename = payload.source_filename.strip() if payload.source_filename.strip() else _direct_filename(payload.title, job_id)
    source_filename, output_basename = _validate_txt_filename(source_filename)
    text_rel = ""
    try:
        text_rel = _write_text_sidecar(job_id, source_filename, text)
        with db() as con:
            profile = _load_profile_for_job(con, payload.profile_id, engine)
            category = _resolve_category(con, payload.category)
            row = _insert_job(
                con, profile_id=payload.profile_id, profile_name=str(profile["name"]), text=text,
                language=language, engine=engine, preset=preset, speed=float(payload.speed), settings=settings,
                batch_id=payload.batch_id.strip(), batch_name=(payload.title.strip() or output_basename), title=(payload.title.strip() or output_basename),
                source_filename=source_filename, output_basename=output_basename, category=category,
                drive_folder=drive_folder, text_file=text_rel, job_id=job_id,
            )
        return {"ok": True, "job": _job_payload(row, str(profile["name"]))}
    except Exception:
        _cleanup_text_rel(text_rel)
        raise


@router.post("/tts/api/clone/batch")
def queue_clone_batch(payload: CloneBatchRequest):
    init_storage()
    language = payload.language.strip() or "auto"
    engine = _resolve_engine(payload.engine, language)
    preset = payload.preset.strip().lower()
    if preset not in GPU_PRESETS:
        raise HTTPException(400, "Unknown GPU preset")
    settings = _sanitize_settings(engine, payload.settings)
    drive_folder = _validate_drive_folder(payload.drive_folder)

    prepared: list[tuple[str, str, str, str]] = []
    seen: set[str] = set()
    for item in payload.items:
        text = item.text.strip()
        if not text:
            raise HTTPException(400, f"{item.source_filename}: TXT script is empty")
        source_filename, stem = _validate_txt_filename(item.source_filename)
        key = stem.casefold()
        if key in seen:
            raise HTTPException(400, f"Duplicate TXT base name in batch: {stem}")
        seen.add(key)
        prepared.append((source_filename, stem, text, item.title.strip() or stem))

    batch_id = "CLB-" + uuid.uuid4().hex[:12].upper()
    created_text: list[str] = []
    rows: list[sqlite3.Row] = []
    try:
        with db() as con:
            profile = _load_profile_for_job(con, payload.profile_id, engine)
            category = _resolve_category(con, payload.category)
            for source_filename, stem, text, title in prepared:
                job_id = "CLJ-" + uuid.uuid4().hex[:12].upper()
                text_rel = _write_text_sidecar(job_id, source_filename, text)
                created_text.append(text_rel)
                rows.append(_insert_job(
                    con, profile_id=payload.profile_id, profile_name=str(profile["name"]), text=text,
                    language=language, engine=engine, preset=preset, speed=float(payload.speed), settings=settings,
                    batch_id=batch_id, batch_name=payload.batch_name.strip(), title=title, source_filename=source_filename, output_basename=stem,
                    category=category, drive_folder=drive_folder, text_file=text_rel, job_id=job_id,
                ))
        return {
            "ok": True,
            "batch_id": batch_id,
            "batch_name": payload.batch_name.strip(),
            "created": len(rows),
            "jobs": [_job_payload(r, str(profile["name"])) for r in rows],
            "naming_rule": "TXT base name is preserved exactly for generated audio",
        }
    except Exception:
        for rel in created_text:
            _cleanup_text_rel(rel)
        raise


@router.get("/tts/api/clone/jobs")
def list_clone_jobs(limit: int = Query(default=100, ge=0)):
    init_storage()
    with db() as con:
        base_sql = (
            "SELECT j.*,p.name profile_name FROM jobs j JOIN profiles p ON p.profile_id=j.profile_id "
            "WHERE j.owner_username=? AND p.owner_username=? ORDER BY j.created_at DESC"
        )
        if int(limit) == 0:
            rows = con.execute(base_sql,(owner(),owner())).fetchall()
        else:
            rows = con.execute(base_sql + " LIMIT ?", (owner(),owner(),int(limit))).fetchall()
    return {
        "ok": True,
        "jobs": [
            {
                **_job_payload(r, str(r["profile_name"])),
                "queue_paused": bool(r["queue_paused"]),
                "queue_order_at": str(r["queue_order_at"] or ""),
            }
            for r in rows
        ],
        "display_limit": "all" if int(limit) == 0 else int(limit),
    }



# VOICE_STUDIO_BATCH_CONTROLS_API_V1
def _batch_rows(con, batch_id: str):
    bid = str(batch_id or "").strip()
    if not bid:
        raise HTTPException(400, "Batch id is required")
    rows = con.execute(
        "SELECT * FROM jobs WHERE batch_id=? AND owner_username=? ORDER BY created_at, rowid",
        (bid,owner()),
    ).fetchall()
    if not rows:
        raise HTTPException(404, "Clone batch not found")
    return bid, rows


@router.post("/tts/api/clone/batches/{batch_id}/pause")
def pause_clone_batch(batch_id: str):
    init_storage()
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        bid, rows = _batch_rows(con, batch_id)
        queued = sum(1 for r in rows if str(r["status"] or "") in {"waiting_worker", "queued"})
        if queued < 1:
            raise HTTPException(409, "This batch has no queued jobs to pause")
        con.execute("UPDATE jobs SET queue_paused=1 WHERE batch_id=? AND owner_username=?", (bid,owner()))
    return {"ok": True, "batch_id": bid, "paused": True, "queued_jobs": queued}


@router.post("/tts/api/clone/batches/{batch_id}/resume")
def resume_clone_batch(batch_id: str):
    init_storage()
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        bid, rows = _batch_rows(con, batch_id)
        queued = sum(1 for r in rows if str(r["status"] or "") in {"waiting_worker", "queued"})
        if queued < 1:
            raise HTTPException(409, "This batch has no queued jobs to resume")
        con.execute("UPDATE jobs SET queue_paused=0 WHERE batch_id=? AND owner_username=?", (bid,owner()))
    return {"ok": True, "batch_id": bid, "paused": False, "queued_jobs": queued}


@router.post("/tts/api/clone/batches/{batch_id}/skip")
def skip_clone_batch(batch_id: str):
    init_storage()
    moved_at = utc_now()
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        bid, rows = _batch_rows(con, batch_id)
        queued = sum(1 for r in rows if str(r["status"] or "") in {"waiting_worker", "queued"})
        if queued < 1:
            raise HTTPException(409, "This batch has no queued jobs to skip")
        con.execute(
            "UPDATE jobs SET queue_paused=0,queue_order_at=? "
            "WHERE batch_id=? AND owner_username=? AND status IN ('waiting_worker','queued')",
            (moved_at, bid, owner()),
        )
    return {"ok": True, "batch_id": bid, "skipped_to_end_at": moved_at, "queued_jobs": queued}


@router.delete("/tts/api/clone/batches/{batch_id}")
def delete_clone_batch(batch_id: str):
    # Guarded delete: only batches whose jobs have never started generation/delivery.
    init_storage()
    text_files = []
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        bid, rows = _batch_rows(con, batch_id)
        unsafe = []
        for r in rows:
            status = str(r["status"] or "")
            delivery = str(r["delivery_status"] or "")
            started = str(r["started_at"] or "")
            audio = str(r["audio_file"] or "")
            drive_remote = str(r["drive_remote"] or "")
            drive_text_remote = str(r["drive_text_remote"] or "")
            if status not in {"waiting_worker", "queued"}:
                unsafe.append(f"{r['job_id']}:status={status}")
            elif delivery not in {"", "waiting_generation", "not_applicable"}:
                unsafe.append(f"{r['job_id']}:delivery={delivery}")
            elif started or audio or drive_remote or drive_text_remote:
                unsafe.append(f"{r['job_id']}:already_started_or_delivered")
            text_files.append(str(r["text_file"] or ""))
        if unsafe:
            raise HTTPException(
                409,
                "Batch delete is allowed only before any job starts. " + "; ".join(unsafe[:5]),
            )
        deleted = con.execute("DELETE FROM jobs WHERE batch_id=? AND owner_username=?", (bid,owner())).rowcount
    for rel in text_files:
        if rel:
            _cleanup_text_rel(rel)
    return {"ok": True, "batch_id": bid, "deleted_jobs": int(deleted)}


@router.delete("/tts/api/clone/jobs/{job_id}")
def delete_clone_job(job_id: str):
    init_storage()
    with db() as con:
        row = con.execute("SELECT * FROM jobs WHERE job_id=? AND owner_username=?", (job_id,owner())).fetchone()
        if not row:
            raise HTTPException(404, "Clone job not found")
        if str(row["status"]) == "processing":
            raise HTTPException(409, "A processing clone job cannot be deleted")
        delivery = str(row["delivery_status"] or "")
        if delivery in {"pending", "uploading", "upload_failed", "drive_verified"}:
            raise HTTPException(409, "Drive delivery is not safely cleaned yet")
        audio = str(row["audio_file"] or "")
        text_rel = str(row["text_file"] or "")
        con.execute("DELETE FROM jobs WHERE job_id=?", (job_id,))
    if audio:
        (AUDIO_DIR / audio).unlink(missing_ok=True)
    _cleanup_text_rel(text_rel)
    return {"ok": True, "deleted": job_id}



@router.post("/tts/api/clone/jobs/{job_id}/discard")
def discard_clone_job(job_id: str):
    """Explicitly discard a non-active GPU queue job and its local files.

    This is intentionally separate from normal delete semantics. It is used by
    the UI only after an explicit confirmation when Drive delivery is stuck or
    no longer wanted. Active generation/upload and Drive-verified jobs remain
    protected.
    """
    init_storage()
    audio = ""
    text_rel = ""
    with db() as con:
        # Serialize against the Drive spooler. If the spooler already owns the
        # row it will be 'uploading' and this request is rejected.
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM jobs WHERE job_id=? AND owner_username=?", (job_id,owner())).fetchone()
        if not row:
            raise HTTPException(404, "Clone job not found")
        status = str(row["status"] or "")
        delivery = str(row["delivery_status"] or "")
        if status == "processing":
            raise HTTPException(409, "A processing clone job cannot be discarded")
        if delivery == "uploading":
            raise HTTPException(409, "Drive upload is currently active; try again after it finishes")
        if delivery == "drive_verified":
            raise HTTPException(409, "Drive-verified delivery must finish local cleanup before deletion")
        audio = str(row["audio_file"] or "")
        text_rel = str(row["text_file"] or "")
        # Deleting the row under an IMMEDIATE transaction prevents a pending
        # upload_failed retry from claiming it after confirmation.
        con.execute("DELETE FROM jobs WHERE job_id=?", (job_id,))
    if audio:
        (AUDIO_DIR / audio).unlink(missing_ok=True)
    _cleanup_text_rel(text_rel)
    return {"ok": True, "discarded": job_id, "local_audio_removed": bool(audio), "local_text_removed": bool(text_rel)}


@router.get("/tts/clone/audio/{filename}")
def clone_audio(filename: str):
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,180}\.(?:wav|mp3|flac|ogg)", filename):
        raise HTTPException(404)
    path = AUDIO_DIR / filename
    with db() as con:
        permitted = con.execute("SELECT 1 FROM jobs WHERE audio_file=? AND owner_username=?", (filename,owner())).fetchone()
    if not permitted or not path.is_file():
        raise HTTPException(404)
    media = {
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".flac": "audio/flac",
        ".ogg": "audio/ogg",
    }.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=media, filename=path.name)

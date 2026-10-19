from __future__ import annotations
import asyncio

import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import subprocess
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from smart_scroll.ai_scene_txt import extract_intro_queries, parse_outer_ai_scene_txt
from storage_manager_client import storage_manager_request

RUNTIME_CANDIDATE = os.getenv("VPS1_RUNTIME_CANDIDATE") == "1"
SCENE_PRECUTOVER_EDITING = os.getenv("VPS1_SCENE_PRECUTOVER_EDITING", "0") == "1"
if SCENE_PRECUTOVER_EDITING and not RUNTIME_CANDIDATE:
    raise RuntimeError("precutover editing cannot run outside guarded candidate mode")
if RUNTIME_CANDIDATE:
    from runtime_guard import validate_candidate_environment
    from precutover_policy import mutation_allowed as candidate_mutation_allowed
    validate_candidate_environment()
from audio_library_v174 import (
    MANIFEST_PATH as AUDIO_LIBRARY_MANIFEST, MUSIC_ROOT, PERMANENT_ROOT, SFX_ROOT,
    asset_path as audio_asset_path, create_asset_record, delete_asset, ensure_library,
    list_assets, load_manifest as load_audio_manifest, update_asset,
)

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("WEB_DATA_DIR", str(APP_DIR / "data"))).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "web.sqlite3"
WORKER_URL = os.getenv("WORKER_URL", "http://127.0.0.1:8000").rstrip("/")
WORKER_TOKEN = os.getenv("WORKER_TOKEN", "").strip()
WEB_USERNAME = os.getenv("WEB_USERNAME", "team").strip() or "team"
WEB_ACCESS_KEY = os.getenv("WEB_ACCESS_KEY", "").strip()
SESSION_SECRET = os.getenv("SESSION_SECRET", WEB_ACCESS_KEY or "change-this-session-secret")
MAX_UPLOAD = int(os.getenv("MAX_UPLOAD_MB", "5000")) * 1024 * 1024
PEXELS_KEYS_FILE = PERMANENT_ROOT / "pexels_keys.txt"


def _split_managed_pexels_keys(text: str) -> list[str]:
    rows: list[str] = []
    seen: set[str] = set()
    for key in re.split(r"[,;\s]+", str(text or "")):
        value = key.strip()
        if value and value not in seen:
            seen.add(value); rows.append(value)
    return rows


def managed_pexels_keys() -> list[str]:
    try:
        return _split_managed_pexels_keys(PEXELS_KEYS_FILE.read_text(encoding="utf-8"))
    except OSError:
        return []


def save_managed_pexels_keys(keys: list[str]) -> None:
    PERMANENT_ROOT.mkdir(parents=True, exist_ok=True)
    clean = _split_managed_pexels_keys("\n".join(keys))
    tmp = PEXELS_KEYS_FILE.with_suffix(".txt.tmp")
    tmp.write_text("\n".join(clean) + ("\n" if clean else ""), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(PEXELS_KEYS_FILE)


def masked_managed_pexels_keys() -> list[dict[str, str]]:
    return [{
        "key_id": hashlib.sha256(key.encode("utf-8", errors="replace")).hexdigest()[:16],
        "masked": ("••••" + key[-4:]) if len(key) >= 4 else "••••",
    } for key in managed_pexels_keys()]

app = FastAPI(title="Scene Builder Team Web", version="7.7.11-professional-editor-v17.5d-r6-internal-audio-studio-v1")
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=APP_DIR / "templates")

# SCRIPT_WORK_FUNCTIONAL_V1_IMPORT
from script_extractor_service import register_script_work_api
register_script_work_api(app, templates)
from script_processor_service import register_script_processor_api
register_script_processor_api(app)
# SCRIPT_WORK_PROCESSOR_REGISTER
# SCRIPT_WORK_FUNCTIONAL_V1_REGISTER

# In a clean rebuild Algrow Voice is completely excluded; existing VPS
# behavior stays unchanged unless explicitly launched with fresh mode.
if os.getenv("FRESH_WEBSITE_INSTALL", "0") != "1":
    from algrow_voice_web import register_algrow_voice_web
    register_algrow_voice_web(app, templates)

# MASTER_CONTROLLER_V1_IMPORT
from master_controller import register_master_controller
register_master_controller(app, templates)
# Auto Script remains owned by the parallel GitHub branch.
if not RUNTIME_CANDIDATE and (os.getenv("FRESH_WEBSITE_INSTALL", "0") != "1" or os.getenv("FRESH_AUTO_SCRIPT_READY", "0") == "1"):
    from auto_script import register_auto_script
    register_auto_script(app, templates)

CAPTION_FONTS = {
    "inter_semibold": {"label": "Inter SemiBold", "filename": "Inter-SemiBold.ttf", "family": "Inter"},
    "roboto_medium": {"label": "Roboto Medium", "filename": "Roboto-Medium.ttf", "family": "Roboto"},
    "poppins_semibold": {"label": "Poppins SemiBold", "filename": "Poppins-SemiBold.ttf", "family": "Poppins"},
    "montserrat_bold": {"label": "Montserrat Bold", "filename": "Montserrat-Bold.ttf", "family": "Montserrat"},
    "roboto_condensed_bold": {"label": "Roboto Condensed Bold", "filename": "RobotoCondensed-Bold.ttf", "family": "Roboto Condensed"},
    "nimbus_sans_bold": {"label": "Nimbus Sans Bold", "filename": "NimbusSans-Bold.otf", "family": "Nimbus Sans"},
    "nimbus_sans_narrow_bold": {"label": "Nimbus Sans Narrow Bold", "filename": "NimbusSansNarrow-Bold.otf", "family": "Nimbus Sans Narrow"},
    "nimbus_roman_bold": {"label": "Nimbus Roman Bold", "filename": "NimbusRoman-Bold.otf", "family": "Nimbus Roman"},
    "dejavu_sans_bold": {"label": "DejaVu Sans Bold", "filename": "DejaVuSans-Bold.ttf", "family": "DejaVu Sans"},
    "dejavu_serif_bold": {"label": "DejaVu Serif Bold", "filename": "DejaVuSerif-Bold.ttf", "family": "DejaVu Serif"},
}
DEFAULT_CAPTION_FONT = "inter_semibold"
FONT_DIR = APP_DIR / "static" / "fonts"

LUT_PACKS: dict[str, dict[str, Any]] = {
    "cinematic": {"label": "Cinematic", "presets": ["blockbuster", "filmic", "soft_cinema", "teal_orange"]},
    "dark_moody": {"label": "Dark & Moody", "presets": ["desaturated_cold", "dramatic", "foggy"]},
    "nature": {"label": "Nature", "presets": ["forest", "lush_green", "tropical", "warm_landscape"]},
    "neutral_correction": {"label": "Neutral Correction", "presets": ["normalization", "subtle_contrast"]},
    "romantic_story": {"label": "Romantic Story", "presets": ["dreamy", "soft_contrast", "warm_skin"]},
    "travel": {"label": "Travel", "presets": ["beach", "drone", "golden_hour", "vibrant_city"]},
    "urban_modern": {"label": "Urban Modern", "presets": ["clean_contrast", "cool_city", "luxury"]},
}

CAPTION_STYLE_PRESETS: dict[str, dict[str, Any]] = {
    "karaoke": {"label": "Karaoke Highlight", "karaoke": True, "bold": True, "background": False, "outline": 3, "shadow": True},
    "boxed": {"label": "Boxed Subtitle", "karaoke": False, "bold": True, "background": True, "outline": 1, "shadow": False},
    "minimal": {"label": "Minimal Clean", "karaoke": False, "bold": False, "background": False, "outline": 2, "shadow": True},
    "bold": {"label": "Bold Impact", "karaoke": False, "bold": True, "background": False, "outline": 4, "shadow": True},
    "documentary": {"label": "Documentary", "karaoke": False, "bold": True, "background": True, "outline": 2, "shadow": True},
    "social_pop": {"label": "Social Pop", "karaoke": True, "bold": True, "background": False, "outline": 4, "shadow": True},
}

SCENE_PRESETS: dict[str, dict[str, Any]] = {
    "cinematic_documentary": {
        "label": "Cinematic Documentary",
        "description": "Premium 16:9 storytelling with restrained motion and clean captions.",
        "quality": "1080p", "aspect_ratio": "16:9", "fps": 24,
        "captions_enabled": True, "caption_style": "minimal", "caption_position": "bottom", "caption_karaoke_enabled": False,
        "timing_mode": "auto_repair", "max_scene_seconds": 4.0, "min_scene_seconds": 0.65,
        "intro_media_min_seconds": 1.5, "intro_media_max_seconds": 3.0,
        "hook_media_min_seconds": 1.5, "hook_media_max_seconds": 3.0,
        "general_media_min_seconds": 2.0, "general_media_max_seconds": 4.0,
        "narration_start_mode": "auto", "narration_start_manual_seconds": 0.0,
        "pexels_enabled": True, "pexels_selection_mode": "best_match_variety", "pexels_search_pool": 10,
        "pexels_avoid_used_media": True, "pexels_avoid_same_creator": True,
        "pexels_prefer_landscape": True, "pexels_prefer_video": True,
        "media_mix": "video_first", "use_images": True, "use_videos": True,
        "motion_style": "cinematic", "transition_engine_enabled": True, "transition_preset": "storytelling",
        "transition_intensity": 38, "lut_enabled": True, "lut_pack": "cinematic",
        "lut_preset": "filmic", "lut_strength": 38, "quality_crf": 20,
        "ffmpeg_preset": "veryfast", "compress": False, "drive_upload_enabled": True,
    },
    "storytelling": {
        "label": "Emotional Storytelling",
        "description": "Smooth visual pacing, warm grading and readable karaoke captions.",
        "quality": "1080p", "aspect_ratio": "16:9", "fps": 24,
        "captions_enabled": True, "caption_style": "karaoke", "caption_position": "bottom", "caption_karaoke_enabled": True,
        "timing_mode": "auto_repair", "max_scene_seconds": 4.0, "min_scene_seconds": 0.65,
        "intro_media_min_seconds": 1.5, "intro_media_max_seconds": 3.0,
        "hook_media_min_seconds": 1.5, "hook_media_max_seconds": 3.0,
        "general_media_min_seconds": 2.0, "general_media_max_seconds": 4.0,
        "narration_start_mode": "auto", "narration_start_manual_seconds": 0.0,
        "pexels_enabled": True, "pexels_selection_mode": "best_match_variety", "pexels_search_pool": 10,
        "pexels_avoid_used_media": True, "pexels_avoid_same_creator": True,
        "pexels_prefer_landscape": True, "pexels_prefer_video": True,
        "media_mix": "alternate", "use_images": True, "use_videos": True,
        "motion_style": "very_slow", "transition_engine_enabled": True, "transition_preset": "smooth",
        "transition_intensity": 32, "lut_enabled": True, "lut_pack": "romantic_story",
        "lut_preset": "warm_skin", "lut_strength": 34, "quality_crf": 20,
        "ffmpeg_preset": "veryfast", "compress": False, "drive_upload_enabled": True,
    },
}


DEFAULT_PRESET_MEDIA_CONFIG: dict[str, Any] = {
    "intro_enabled": False,
    "intro_duration_seconds": 15,
    "music_enabled": False,
    "music_playlist": [],
    "music_ai_select_from_library": False,
    "music_ai_allowed_categories": [],
    "music_volume_percent": 12,
    "music_ducking_enabled": True,
    "music_ducking_strength": "medium",
    "music_normalize": True,
    "music_playlist_loop": True,
    "music_track_crossfade_seconds": 1.5,
    "music_use_track1_for_intro": True,
    "music_allow_intro_track_in_main": False,
    "sfx_enabled": False,
    "environment_sfx_enabled": True,
    "transition_sfx_library_enabled": True,
    "sfx_amount": "medium",
    "environment_sfx_volume_percent": 18,
    "transition_sfx_volume_percent": 24,
    "auto_clean_project_after_drive": True,
    "auto_clean_disable_confirmed": False,
    "first_two_minute_visual_audit_enabled": False,
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    ensure_library()
    with db() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY COLLATE NOCASE,
                secret_hash TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                is_admin INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        user_columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        user_additions = {
            "daily_render_limit": "INTEGER NOT NULL DEFAULT 25",
            "subscription_plan_days": "INTEGER NOT NULL DEFAULT 30",
            "subscription_start_at": "TEXT NOT NULL DEFAULT ''",
            "subscription_end_at": "TEXT NOT NULL DEFAULT ''",
            "last_login_at": "TEXT NOT NULL DEFAULT ''",
            "app_access": "TEXT NOT NULL DEFAULT 'scene_builder'",
        }
        for column, definition in user_additions.items():
            if column not in user_columns:
                conn.execute(f"ALTER TABLE users ADD COLUMN {column} {definition}")
        migration_now = datetime.now(timezone.utc)
        migration_end = (migration_now + timedelta(days=30)).isoformat()
        quota_row = conn.execute("SELECT value FROM settings WHERE key='quota_limit'").fetchone()
        old_quota = int(quota_row[0]) if quota_row else 25
        conn.execute("UPDATE users SET daily_render_limit=? WHERE is_admin=0 AND daily_render_limit=25", (max(0, old_quota),))
        conn.execute("UPDATE users SET subscription_plan_days=30 WHERE is_admin=0 AND subscription_plan_days NOT IN (7,15,30)")
        conn.execute("UPDATE users SET subscription_start_at=? WHERE is_admin=0 AND subscription_start_at=''", (migration_now.isoformat(),))
        conn.execute("UPDATE users SET subscription_end_at=? WHERE is_admin=0 AND subscription_end_at=''", (migration_end,))

        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                message TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS job_ownership (
                job_id TEXT PRIMARY KEY,
                owner_username TEXT NOT NULL COLLATE NOCASE,
                project_name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(owner_username) REFERENCES users(username)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_job_ownership_owner ON job_ownership(owner_username)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_scene_presets (
                preset_id TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                profile_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_user_scene_presets_label ON user_scene_presets(label COLLATE NOCASE)")
        defaults = {
            "quota_limit": "25",
            "allowed_storage_gb": "60",
            "announcement": "Welcome to Scene Builder. Upload projects from the Queue page.",
            "quality": "720p",
            "captions_enabled": "1",
            "caption_words": "3",
            "caption_style": "karaoke",
            "caption_font_size": "48",
            "caption_font": DEFAULT_CAPTION_FONT,
            "caption_text_color": "#ffffff",
            "caption_outline_color": "#000000",
            "caption_highlight_color": "#ffd54a",
            "caption_background_color": "#000000",
            "caption_background_opacity": "55",
            "caption_position": "bottom",
            "caption_outline_width": "3",
            "caption_shadow_enabled": "1",
            "default_scene_preset": "cinematic_documentary",
            "custom_aspect_ratio": "16:9",
            "custom_fps": "24",
            "timing_mode": "auto_repair",
            "max_scene_seconds": "4.0",
            "min_scene_seconds": "0.65",
            "intro_media_min_seconds": "1.5",
            "intro_media_max_seconds": "3.0",
            "hook_media_min_seconds": "1.5",
            "hook_media_max_seconds": "3.0",
            "general_media_min_seconds": "2.0",
            "general_media_max_seconds": "4.0",
            "narration_start_mode": "auto",
            "narration_start_manual_seconds": "0.0",
            "pexels_selection_mode": "best_match_variety",
            "pexels_search_pool": "10",
            "pexels_avoid_used_media": "1",
            "pexels_avoid_same_creator": "1",
            "pexels_prefer_landscape": "1",
            "pexels_prefer_video": "1",
            "media_mix": "video_first",
            "use_image_backgrounds": "1",
            "use_video_backgrounds": "1",
            "avoid_duplicate_media": "1",
            "scene_review_required": "0",
            "motion_style": "cinematic",
            "transition_mode": "auto_editorial",
            "transition_preset": "storytelling",
            "transition_intensity": "38",
            "transition_duration": "0.28",
            "lut_enabled": "1",
            "lut_pack": "cinematic",
            "lut_preset": "filmic",
            "lut_strength": "38",
            "lut_normalize_enabled": "1",
            "output_fps": "24",
            "quality_crf": "20",
            "ffmpeg_preset": "veryfast",
            "post_render_compress_enabled": "0",
            "voice_volume_percent": "100",
            "voice_processing_preset": "off",
            "music_volume_percent": "12",
            "music_ducking_enabled": "1",
            "music_ducking_strength": "medium",
            "music_normalize": "1",
            "music_loop_if_short": "1",
            "music_loop_crossfade_seconds": "0.5",
            "music_fade_in_seconds": "1.5",
            "music_fade_out_seconds": "2.0",
            "drive_backup_enabled": "1",
            "download_preference": "vps_then_drive",
            "retention_enabled": "1",
            "retention_free_gb": "20",
            "retention_last_run": "",
        }
        for key, value in defaults.items():
            conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (key, value))
        existing = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        if existing == 0 and WEB_ACCESS_KEY:
            conn.execute(
                "INSERT INTO users(username,secret_hash,is_active,is_admin,created_at) VALUES(?,?,?,?,?)",
                (WEB_USERNAME, hash_secret(WEB_ACCESS_KEY), 1, 1, now_iso()),
            )



def hash_secret(secret: str, salt_hex: str | None = None) -> str:
    salt = bytes.fromhex(salt_hex) if salt_hex else os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, 200_000)
    return f"{salt.hex()}:{digest.hex()}"


def verify_secret(secret: str, stored: str) -> bool:
    try:
        salt_hex, expected = stored.split(":", 1)
        actual = hash_secret(secret, salt_hex).split(":", 1)[1]
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


@app.on_event("startup")
def startup() -> None:
    # STORAGE_MANAGER_INTEGRATION_V1
    # The legacy worker cleanup loop is retired.  The dedicated, allowlisted
    # Storage Manager owns scans, policies, history and cleanup scheduling.
    init_db()


def setting(key: str, default: str = "") -> str:
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return str(row["value"]) if row else default


def set_setting(key: str, value: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def preset_media_config(preset_id: str) -> dict[str, Any]:
    selected = str(preset_id or "custom").strip() or "custom"
    result = dict(DEFAULT_PRESET_MEDIA_CONFIG)
    raw = setting(f"preset_media_config::{selected}", "")
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                result.update(parsed)
        except Exception:
            pass
    result["intro_duration_seconds"] = max(1, min(600, int(result.get("intro_duration_seconds") or 15)))
    result["music_volume_percent"] = max(0, min(100, int(result.get("music_volume_percent") or 0)))
    result["environment_sfx_volume_percent"] = max(0, min(60, int(result.get("environment_sfx_volume_percent") or 0)))
    result["transition_sfx_volume_percent"] = max(0, min(60, int(result.get("transition_sfx_volume_percent") or 0)))
    result["sfx_amount"] = str(result.get("sfx_amount") or "medium") if str(result.get("sfx_amount") or "medium") in {"low","medium","high"} else "medium"
    result["music_playlist"] = [dict(x) for x in result.get("music_playlist", []) if isinstance(x, dict)]
    result["music_ai_allowed_categories"] = [str(x) for x in result.get("music_ai_allowed_categories", []) if str(x).strip()]
    # R4 safety migration: older releases could leave Auto Clean OFF without any
    # deliberate confirmation. Only an R4+ preset carrying the explicit marker
    # is allowed to stay OFF; all legacy/unconfirmed OFF values become ON.
    disable_confirmed = bool(result.get("auto_clean_disable_confirmed", False))
    if not bool(result.get("auto_clean_project_after_drive", True)) and not disable_confirmed:
        result["auto_clean_project_after_drive"] = True
    result["auto_clean_disable_confirmed"] = bool(
        not bool(result.get("auto_clean_project_after_drive", True)) and disable_confirmed
    )
    return result


def save_preset_media_config(preset_id: str, config: dict[str, Any]) -> None:
    if preset_id not in scene_preset_catalog():
        raise HTTPException(400, "Invalid preset")
    set_setting(f"preset_media_config::{preset_id}", json.dumps(config, ensure_ascii=False, separators=(",", ":")))


def all_preset_media_configs() -> dict[str, dict[str, Any]]:
    return {key: preset_media_config(key) for key in scene_preset_catalog()}


# SCENE_PARENT_CHILD_AUDIO_PRESET_V1
# The lower Audio Engine preset owns audio-processing values. The upper Scene Builder
# preset owns the binding to that lower preset, so child changes roll up to the parent.
SCENE_AUDIO_ENGINE_BINDING_PREFIX = "preset_audio_engine::"

def scene_audio_engine_binding(preset_id: str) -> dict[str, Any]:
    preset_id = str(preset_id or "").strip()
    if preset_id not in scene_preset_catalog():
        raise HTTPException(404, "Scene Builder preset not found")
    raw = setting(f"{SCENE_AUDIO_ENGINE_BINDING_PREFIX}{preset_id}", "")
    if not raw:
        return {"configured": False, "enabled": True, "audio_engine_preset": ""}
    try:
        data = json.loads(raw)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    child = str(data.get("audio_engine_preset") or data.get("preset") or "").strip()
    if child and not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", child):
        child = ""
    return {
        "configured": bool(child),
        "enabled": bool(data.get("enabled", True)),
        "audio_engine_preset": child,
    }

def save_scene_audio_engine_binding(preset_id: str, *, enabled: bool, audio_engine_preset: str) -> dict[str, Any]:
    preset_id = str(preset_id or "").strip()
    if preset_id not in scene_preset_catalog():
        raise HTTPException(404, "Scene Builder preset not found")
    child = str(audio_engine_preset or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", child):
        raise HTTPException(400, "Invalid Audio Engine preset")
    payload = {"enabled": bool(enabled), "audio_engine_preset": child}
    set_setting(f"{SCENE_AUDIO_ENGINE_BINDING_PREFIX}{preset_id}", json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return {"configured": True, **payload}


def audio_asset_usage(asset_id: str) -> list[str]:
    used: list[str] = []
    for preset_id in scene_preset_catalog():
        config = preset_media_config(preset_id)
        if any(str(row.get("asset_id") or "") == asset_id for row in config.get("music_playlist", [])):
            used.append(preset_id)
    return used



def audit_event(event_type: str, message: str, details: dict[str, Any] | None = None) -> None:
    try:
        with db() as conn:
            conn.execute(
                "INSERT INTO audit_events(event_type,message,details_json,created_at) VALUES(?,?,?,?)",
                (event_type, message[:1000], json.dumps(details or {}, ensure_ascii=False), now_iso()),
            )
    except Exception:
        pass


def wants_json(request: Request) -> bool:
    return (
        request.headers.get("x-requested-with", "").lower() == "xmlhttprequest"
        or "application/json" in request.headers.get("accept", "").lower()
    )


def submission_error(request: Request, status_code: int, message: str):
    if wants_json(request):
        return JSONResponse({"ok": False, "error": message}, status_code=status_code)
    raise HTTPException(status_code, message)


SUPPORTED_NARRATION_SUFFIXES = {
    ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma",
    ".aiff", ".aif", ".amr", ".ac3", ".mka", ".mp2", ".webm", ".mp4",
    ".mov", ".mkv", ".avi", ".mpeg", ".mpg", ".mts", ".m2ts",
}


def validate_narration_media(path: Path) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration,format_name",
                "-show_entries", "stream=index,codec_type,codec_name",
                "-of", "json", str(path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=90,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ValueError("FFprobe is not installed on the website VPS") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Narration media validation timed out") from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "FFprobe rejected the file").strip()
        raise ValueError(f"Narration media is invalid or unsupported: {detail[-400:]}")

    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("FFprobe returned an invalid response") from exc

    streams = payload.get("streams") or []
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    if not audio_streams:
        raise ValueError("Selected media contains no usable audio stream")

    duration = float((payload.get("format") or {}).get("duration") or 0)
    if duration <= 0:
        raise ValueError("Narration media has no valid duration")

    return {
        "duration": duration,
        "format_name": str((payload.get("format") or {}).get("format_name") or ""),
        "audio_codec": str(audio_streams[0].get("codec_name") or ""),
    }


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def user_account(username: str) -> dict[str, Any] | None:
    if not username:
        return None
    with db() as conn:
        row = conn.execute(
            """SELECT username,is_active,is_admin,created_at,daily_render_limit,
                      subscription_plan_days,subscription_start_at,subscription_end_at,last_login_at,
                      app_access
               FROM users WHERE username=? COLLATE NOCASE""",
            (username,),
        ).fetchone()
    if not row:
        return None
    data = dict(row)
    end_at = parse_iso(data.get("subscription_end_at"))
    data["subscription_expired"] = bool(not data.get("is_admin") and end_at and end_at <= datetime.now(timezone.utc))
    data["subscription_status"] = "Disabled" if not data.get("is_active") else ("Expired" if data["subscription_expired"] else "Active")
    return data


def completed_today_count(username: str, jobs: list[dict[str, Any]] | None = None) -> int:
    jobs = jobs if jobs is not None else jobs_for_user(username, 1000)
    today = datetime.now(timezone.utc).date()
    used = 0
    for job in jobs:
        if str(job.get("status") or "").lower() not in {"completed", "cleaned"}:
            continue
        stamp = parse_iso(str(job.get("completed_at") or job.get("updated_at") or ""))
        if stamp and stamp.astimezone(timezone.utc).date() == today:
            used += 1
    return used


def quota_snapshot_for_user(username: str, jobs: list[dict[str, Any]] | None = None) -> tuple[int, int, int]:
    account = user_account(username) or {}
    limit = max(0, int(account.get("daily_render_limit") or 0))
    used = completed_today_count(username, jobs)
    remaining = max(0, limit - used) if limit else 0
    return limit, used, remaining


def require_admin(request: Request) -> None:
    if not bool(request.session.get("is_admin")):
        raise HTTPException(403, "Administrator access required")


def app_access_allows(account: dict[str, Any] | None, app_name: str) -> bool:
    """Interpret the existing app_access model with LUM added as the third app."""
    if not account:
        return False
    if bool(account.get("is_admin")):
        return True
    access = str(account.get("app_access") or "scene_builder").strip().lower()
    matrix = {
        "scene_builder": {"scene_builder"},
        "auto_caption": {"auto_caption"},
        "lum": {"lum"},
        "both": {"scene_builder", "auto_caption"},
        "all": {"scene_builder", "auto_caption", "lum"},
    }
    return app_name in matrix.get(access, set())


def require_auto_caption_access(request: Request) -> dict[str, Any]:
    """Return the active account when it may use Auto Caption Studio."""
    require_login(request)
    account = user_account(str(request.session.get("username") or ""))
    if not account or not int(account.get("is_active") or 0) or account.get("subscription_expired"):
        raise HTTPException(401, "Active account required")
    if not app_access_allows(account, "auto_caption"):
        raise HTTPException(403, "Auto Caption access required")
    return account


def require_local_upload_manager_access(request: Request) -> dict[str, Any]:
    """Return the active account when it may use Auto Uploader."""
    require_login(request)
    account = user_account(str(request.session.get("username") or ""))
    if not account or not int(account.get("is_active") or 0) or account.get("subscription_expired"):
        raise HTTPException(401, "Active account required")
    if not app_access_allows(account, "lum"):
        raise HTTPException(403, "Auto Uploader access required")
    return account


def logged_in(request: Request) -> bool:
    return bool(request.session.get("authenticated"))


def require_login(request: Request) -> None:
    if not logged_in(request):
        raise HTTPException(401, "Login required")


@app.middleware("http")
async def authentication(request: Request, call_next):
    if RUNTIME_CANDIDATE and request.method not in ("GET", "HEAD", "OPTIONS"):
        if not candidate_mutation_allowed(request.method, request.url.path, SCENE_PRECUTOVER_EDITING):
            return JSONResponse({"ok": False, "error": "candidate: write requires separate approval"}, status_code=403)
    if RUNTIME_CANDIDATE and request.url.path == "/internal/runtime-ready":
        return await call_next(request)
    # AUTOFLOW_STAGE12_PC_EXTENSION_MIDDLEWARE_BYPASS
    # Bypass only the website redirect. master_controller._require_api still
    # enforces a normal web session or the private PC-extension bridge token.
    _af_path = request.url.path
    if (
        _af_path.startswith("/api/master-controller/chatgpt/")
        or _af_path.startswith("/api/master-controller/workers/")
        or _af_path == "/api/master-controller/bridge/heartbeat"
        or _af_path == "/api/master-controller/health"
    ):
        return await call_next(request)
    if request.url.path in {"/internal/auto-caption-auth", "/internal/local-upload-manager-auth", "/internal/audio-engine-auth", "/internal/workspace-auth"}:
        return await call_next(request)
    public_paths = {"/login", "/health", "/internal/auto-caption-auth", "/internal/local-upload-manager-auth", "/internal/audio-engine-auth", "/internal/workspace-auth"}
    if request.url.path.startswith("/static/") or request.url.path in public_paths:
        return await call_next(request)
    if not logged_in(request):
        return RedirectResponse("/login", status_code=303)
    account = user_account(str(request.session.get("username") or ""))
    if not account or not int(account.get("is_active") or 0) or account.get("subscription_expired"):
        request.session.clear()
        reason = "Subscription expired" if account and account.get("subscription_expired") else "Account unavailable"
        return RedirectResponse(f"/login?error={reason.replace(' ', '+')}", status_code=303)
    request.session["is_admin"] = bool(account.get("is_admin"))
    request.session["app_access"] = "both" if bool(account.get("is_admin")) else str(account.get("app_access") or "scene_builder")
    if request.session["app_access"] == "auto_caption" and request.url.path not in {
        "/", "/api/dashboard/v1", "/auto-caption/live", "/auto-caption/admin", "/auto-caption/studio", "/logout",
        "/internal/auto-caption-auth", "/internal/local-upload-manager-auth"
    }:
        return RedirectResponse("/auto-caption/live", status_code=303)
    if request.session["app_access"] == "lum" and request.url.path not in {
        "/", "/api/dashboard/v1", "/local-upload-manager", "/logout", "/internal/auto-caption-auth", "/internal/local-upload-manager-auth"
    }:
        return RedirectResponse("/local-upload-manager", status_code=303)
    return await call_next(request)

app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    https_only=True,
    same_site="lax",
    max_age=60 * 60 * 12,
)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str = ""):
    if logged_in(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": error},
    )


@app.post("/login")
def login(request: Request, username: str = Form(...), secret_key: str = Form(...)):
    clean_username = username.strip()
    with db() as conn:
        row = conn.execute(
            "SELECT username,secret_hash,is_active,is_admin,subscription_end_at,app_access FROM users WHERE username=? COLLATE NOCASE",
            (clean_username,),
        ).fetchone()
    if not row or not int(row["is_active"]) or not verify_secret(secret_key.strip(), row["secret_hash"]):
        return RedirectResponse("/login?error=Invalid+username+or+secret+key", status_code=303)
    end_at = parse_iso(row["subscription_end_at"])
    if not bool(row["is_admin"]) and end_at and end_at <= datetime.now(timezone.utc):
        return RedirectResponse("/login?error=Subscription+expired", status_code=303)
    with db() as conn:
        conn.execute("UPDATE users SET last_login_at=? WHERE username=? COLLATE NOCASE", (now_iso(), row["username"]))
    request.session.clear()
    request.session["authenticated"] = True
    request.session["username"] = row["username"]
    request.session["is_admin"] = bool(row["is_admin"])
    request.session["app_access"] = "both" if bool(row["is_admin"]) else str(row["app_access"] or "scene_builder")
    if request.session["app_access"] == "auto_caption":
        return RedirectResponse("/auto-caption/live", status_code=303)
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


def worker_params(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if WORKER_TOKEN:
        params["token"] = WORKER_TOKEN
    if extra:
        params.update(extra)
    return params


def worker_request(method: str, path: str, **kwargs: Any) -> requests.Response:
    params = dict(kwargs.pop("params", {}) or {})
    params.update(worker_params())
    try:
        response = requests.request(
            method,
            WORKER_URL + path,
            params=params,
            timeout=kwargs.pop("timeout", 30),
            **kwargs,
        )
    except requests.RequestException as exc:
        raise HTTPException(502, f"Worker unavailable: {exc}") from exc
    if response.status_code >= 400:
        detail = response.text[-1000:] or f"Worker returned {response.status_code}"
        raise HTTPException(response.status_code, detail)
    return response


def get_jobs(limit: int = 200) -> list[dict[str, Any]]:
    """Return raw jobs from the worker. Ownership filtering is applied separately."""
    try:
        return worker_request("GET", "/jobs", params={"limit": limit}).json().get("jobs", [])
    except HTTPException:
        return []


def primary_admin_username() -> str:
    with db() as conn:
        row = conn.execute(
            "SELECT username FROM users WHERE is_admin=1 ORDER BY created_at, username LIMIT 1"
        ).fetchone()
    return str(row["username"]) if row else WEB_USERNAME


def record_job_owner(job_id: str, username: str, project_name: str = "") -> None:
    if not job_id or not username:
        return
    with db() as conn:
        conn.execute(
            """INSERT INTO job_ownership(job_id,owner_username,project_name,created_at)
               VALUES(?,?,?,?)
               ON CONFLICT(job_id) DO UPDATE SET
                 project_name=CASE WHEN excluded.project_name<>'' THEN excluded.project_name ELSE job_ownership.project_name END""",
            (job_id, username, project_name, now_iso()),
        )


def reconcile_job_ownership(jobs: list[dict[str, Any]]) -> None:
    """Assign pre-deployment/untracked worker jobs to the primary admin account."""
    if not jobs:
        return
    admin_username = primary_admin_username()
    rows = [
        (str(job.get("id") or ""), admin_username, str(job.get("project_name") or ""), now_iso())
        for job in jobs if str(job.get("id") or "")
    ]
    with db() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO job_ownership(job_id,owner_username,project_name,created_at) VALUES(?,?,?,?)",
            rows,
        )


def ownership_map(job_ids: list[str] | None = None) -> dict[str, str]:
    with db() as conn:
        if job_ids:
            placeholders = ",".join("?" for _ in job_ids)
            rows = conn.execute(
                f"SELECT job_id,owner_username FROM job_ownership WHERE job_id IN ({placeholders})",
                job_ids,
            ).fetchall()
        else:
            rows = conn.execute("SELECT job_id,owner_username FROM job_ownership").fetchall()
    return {str(row["job_id"]): str(row["owner_username"]) for row in rows}


def jobs_for_user(username: str, limit: int = 200) -> list[dict[str, Any]]:
    jobs = get_jobs(limit)
    reconcile_job_ownership(jobs)
    owners = ownership_map([str(job.get("id") or "") for job in jobs])
    return [
        {**job, "owner_username": owners.get(str(job.get("id") or ""), "")}
        for job in jobs
        if owners.get(str(job.get("id") or ""), "").casefold() == username.casefold()
    ]


def all_jobs_with_owners(limit: int = 200) -> list[dict[str, Any]]:
    jobs = get_jobs(limit)
    reconcile_job_ownership(jobs)
    owners = ownership_map([str(job.get("id") or "") for job in jobs])
    return [{**job, "owner_username": owners.get(str(job.get("id") or ""), "")} for job in jobs]


def audit_waiting_count_for_user(username: str) -> int:
    return sum(1 for job in jobs_for_user(username, 1000) if str(job.get("status") or "") == "waiting_for_audit")


def require_job_access(request: Request, job_id: str, allow_admin: bool = True) -> str:
    username = str(request.session.get("username") or "")
    with db() as conn:
        row = conn.execute(
            "SELECT owner_username FROM job_ownership WHERE job_id=?", (job_id,)
        ).fetchone()
    if row is None:
        # An old worker job may not yet have been reconciled. Confirm it exists, then assign it to admin.
        worker_request("GET", f"/jobs/{job_id}")
        record_job_owner(job_id, primary_admin_username())
        with db() as conn:
            row = conn.execute(
                "SELECT owner_username FROM job_ownership WHERE job_id=?", (job_id,)
            ).fetchone()
    owner = str(row["owner_username"]) if row else ""
    if owner.casefold() == username.casefold():
        return owner
    if allow_admin and bool(request.session.get("is_admin")):
        return owner
    raise HTTPException(403, "You do not have access to this project")


def get_health() -> dict[str, Any]:
    try:
        return worker_request("GET", "/health").json()
    except HTTPException as exc:
        return {"ok": False, "error": exc.detail, "queue_counts": {}}


def get_storage() -> dict[str, Any]:
    try:
        return worker_request("GET", "/maintenance/storage").json()
    except HTTPException as exc:
        return {"error": exc.detail}


def bytes_text(value: int | float | None) -> str:
    size = float(value or 0)
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return "0 B"


def quality_dimensions(name: str) -> tuple[int, int]:
    return {"480p": (854, 480), "720p": (1280, 720), "1080p": (1920, 1080)}.get(name, (1280, 720))


def profile_dimensions(quality: str, aspect_ratio: str) -> tuple[int, int]:
    landscape = quality_dimensions(quality)
    if aspect_ratio == "9:16":
        return landscape[1], landscape[0]
    if aspect_ratio == "1:1":
        edge = 1080 if quality == "1080p" else (720 if quality == "720p" else 480)
        return edge, edge
    return landscape


def validated_lut_selection(pack: str, preset: str) -> tuple[str, str]:
    selected_pack = str(pack or "").strip()
    if selected_pack not in LUT_PACKS:
        selected_pack = "cinematic"
    presets = list(LUT_PACKS[selected_pack]["presets"])
    selected_preset = str(preset or "").strip()
    if selected_preset not in presets:
        selected_preset = "filmic" if selected_pack == "cinematic" else presets[0]
    return selected_pack, selected_preset


def custom_scene_profile() -> dict[str, Any]:
    lut_pack, lut_preset = validated_lut_selection(
        setting("lut_pack", "cinematic"),
        setting("lut_preset", "filmic"),
    )
    return {
        "label": "Custom Web Settings",
        "description": "Uses the advanced values saved in Scene Builder Settings.",
        "quality": setting("quality", "720p"),
        "aspect_ratio": setting("custom_aspect_ratio", "16:9"),
        "fps": max(20, min(60, int(setting("output_fps", "24") or 24))),
        "captions_enabled": setting("captions_enabled", "1") == "1",
        "caption_style": setting("caption_style", "karaoke"),
        "caption_position": setting("caption_position", "bottom"),
        "caption_karaoke_enabled": setting("caption_karaoke_enabled", "1" if CAPTION_STYLE_PRESETS.get(setting("caption_style", "karaoke"), CAPTION_STYLE_PRESETS["karaoke"])["karaoke"] else "0") == "1",
        "caption_words": max(1, min(5, int(setting("caption_words", "3") or 3))),
        "caption_font_size": max(18, min(120, int(setting("caption_font_size", "48") or 48))),
        "caption_font": setting("caption_font", DEFAULT_CAPTION_FONT),
        "caption_text_color": setting("caption_text_color", "#ffffff"),
        "caption_outline_color": setting("caption_outline_color", "#000000"),
        "caption_highlight_color": setting("caption_highlight_color", "#ffd54a"),
        "caption_background_color": setting("caption_background_color", "#000000"),
        "caption_background_opacity": max(0, min(100, int(setting("caption_background_opacity", "55") or 55))),
        "caption_outline_width": max(0, min(8, int(setting("caption_outline_width", "3") or 3))),
        "caption_shadow_enabled": setting("caption_shadow_enabled", "1") == "1",
        "timing_mode": setting("timing_mode", "auto_repair"),
        "max_scene_seconds": max(1.0, min(10.0, float(setting("general_media_max_seconds", setting("max_scene_seconds", "4.0")) or 4.0))),
        "min_scene_seconds": max(0.05, min(10.0, float(setting("general_media_min_seconds", setting("min_scene_seconds", "0.65")) or 0.65))),
        "intro_media_min_seconds": max(0.25, min(10.0, float(setting("intro_media_min_seconds", "1.5") or 1.5))),
        "intro_media_max_seconds": max(0.25, min(15.0, float(setting("intro_media_max_seconds", "3.0") or 3.0))),
        "hook_media_min_seconds": max(0.25, min(10.0, float(setting("hook_media_min_seconds", "1.5") or 1.5))),
        "hook_media_max_seconds": max(0.25, min(15.0, float(setting("hook_media_max_seconds", "3.0") or 3.0))),
        "general_media_min_seconds": max(0.25, min(10.0, float(setting("general_media_min_seconds", setting("min_scene_seconds", "2.0")) or 2.0))),
        "general_media_max_seconds": max(0.25, min(15.0, float(setting("general_media_max_seconds", setting("max_scene_seconds", "4.0")) or 4.0))),
        "narration_start_mode": setting("narration_start_mode", "auto") if setting("narration_start_mode", "auto") in {"auto", "manual"} else "auto",
        "narration_start_manual_seconds": max(0.0, min(3600.0, float(setting("narration_start_manual_seconds", "0.0") or 0.0))),
        "pexels_enabled": setting("pexels_enabled", "1") == "1",
        "pexels_selection_mode": setting("pexels_selection_mode", "best_match_variety"),
        "pexels_search_pool": max(1, min(40, int(setting("pexels_search_pool", "10") or 10))),
        "pexels_avoid_used_media": setting("pexels_avoid_used_media", "1") == "1",
        "pexels_avoid_same_creator": setting("pexels_avoid_same_creator", "1") == "1",
        "pexels_prefer_landscape": setting("pexels_prefer_landscape", "1") == "1",
        "pexels_prefer_video": setting("pexels_prefer_video", "1") == "1",
        "media_mix": setting("media_mix", "video_first"),
        "use_images": setting("use_image_backgrounds", "1") == "1",
        "use_videos": setting("use_video_backgrounds", "1") == "1",
        "motion_style": setting("motion_style", "cinematic"),
        "transition_engine_enabled": setting("transition_engine_enabled", "1") == "1",
        "transition_mode": setting("transition_mode", "auto_editorial"),
        "transition_preset": setting("transition_preset", "storytelling"),
        "transition_duration": max(0.12, min(0.50, float(setting("transition_duration", "0.28") or 0.28))),
        "transition_intensity": max(0, min(100, int(setting("transition_intensity", "38") or 38))),
        "lut_enabled": setting("lut_enabled", "1") == "1",
        "lut_pack": lut_pack,
        "lut_preset": lut_preset,
        "lut_strength": max(0, min(100, int(setting("lut_strength", "38") or 38))),
        "lut_normalize_enabled": setting("lut_normalize_enabled", "1") == "1",
        "quality_crf": max(16, min(30, int(setting("quality_crf", "20") or 20))),
        "ffmpeg_preset": setting("ffmpeg_preset", "veryfast"),
        "compress": setting("post_render_compress_enabled", "0") == "1",
        "drive_upload_enabled": setting("drive_upload_enabled", "1") == "1",
        "voice_volume_percent": max(0, min(150, int(setting("voice_volume_percent", "100") or 100))),
        "voice_processing_preset": setting("voice_processing_preset", "off"),
    }


PROFILE_FIELDS = (
    "quality", "aspect_ratio", "fps", "captions_enabled", "caption_style", "caption_position", "caption_karaoke_enabled",
    "timing_mode", "max_scene_seconds", "min_scene_seconds",
    "intro_media_min_seconds", "intro_media_max_seconds",
    "hook_media_min_seconds", "hook_media_max_seconds",
    "general_media_min_seconds", "general_media_max_seconds",
    "narration_start_mode", "narration_start_manual_seconds",
    "pexels_enabled", "pexels_selection_mode", "pexels_search_pool", "pexels_avoid_used_media",
    "pexels_avoid_same_creator", "pexels_prefer_landscape", "pexels_prefer_video",
    "media_mix", "use_images", "use_videos", "motion_style", "transition_engine_enabled", "transition_mode", "transition_preset",
    "transition_intensity", "transition_duration", "lut_enabled", "lut_pack", "lut_preset",
    "lut_strength", "lut_normalize_enabled",
    "caption_words", "caption_font_size", "caption_font", "caption_text_color", "caption_outline_color",
    "caption_highlight_color", "caption_background_color", "caption_background_opacity", "caption_outline_width", "caption_shadow_enabled",
    "voice_volume_percent", "voice_processing_preset",
    "quality_crf", "ffmpeg_preset", "compress", "drive_upload_enabled",
)


def _clean_preset_profile(raw: dict[str, Any], *, label: str, description: str) -> dict[str, Any]:
    base = custom_scene_profile()
    if isinstance(raw, dict):
        for key in PROFILE_FIELDS:
            if key in raw:
                base[key] = raw[key]
    base["label"] = str(label or "Custom Preset").strip()[:80] or "Custom Preset"
    base["description"] = str(description or "Custom production preset.").strip()[:240] or "Custom production preset."
    base["is_user_preset"] = True
    return base


def user_scene_presets() -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    with db() as conn:
        rows = conn.execute(
            "SELECT preset_id,label,description,profile_json,created_at,updated_at FROM user_scene_presets ORDER BY label COLLATE NOCASE, created_at"
        ).fetchall()
    for row in rows:
        try:
            raw = json.loads(row["profile_json"] or "{}")
        except Exception:
            raw = {}
        profile = _clean_preset_profile(raw if isinstance(raw, dict) else {}, label=row["label"], description=row["description"])
        profile["preset_id"] = row["preset_id"]
        profile["created_at"] = row["created_at"]
        profile["updated_at"] = row["updated_at"]
        result[str(row["preset_id"])] = profile
    return result


def is_user_scene_preset(preset_id: str) -> bool:
    with db() as conn:
        return conn.execute("SELECT 1 FROM user_scene_presets WHERE preset_id=?", (str(preset_id or ""),)).fetchone() is not None


def save_user_scene_preset_profile(preset_id: str, profile: dict[str, Any]) -> None:
    with db() as conn:
        row = conn.execute("SELECT label,description FROM user_scene_presets WHERE preset_id=?", (preset_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Custom preset not found")
        cleaned = _clean_preset_profile(profile, label=row["label"], description=row["description"])
        payload = {key: cleaned[key] for key in PROFILE_FIELDS if key in cleaned}
        conn.execute(
            "UPDATE user_scene_presets SET profile_json=?,updated_at=? WHERE preset_id=?",
            (json.dumps(payload, ensure_ascii=False, separators=(",", ":")), now_iso(), preset_id),
        )


def _unique_preset_label(label: str, *, exclude_id: str = "") -> str:
    cleaned = " ".join(str(label or "").split()).strip()[:80]
    if not cleaned:
        raise HTTPException(400, "Preset name is required")
    catalog = scene_preset_catalog()
    for pid, row in catalog.items():
        if pid != exclude_id and str(row.get("label") or "").strip().casefold() == cleaned.casefold():
            raise HTTPException(409, "A preset with this name already exists")
    return cleaned


def create_user_scene_preset(label: str, source_preset_id: str) -> str:
    clean_label = _unique_preset_label(label)
    source_id, source_profile = resolve_scene_profile(source_preset_id)
    preset_id = "preset_" + secrets.token_hex(8)
    description = f"Custom preset copied from {source_profile.get('label') or source_id}."[:240]
    profile = _clean_preset_profile(source_profile, label=clean_label, description=description)
    payload = {key: profile[key] for key in PROFILE_FIELDS if key in profile}
    stamp = now_iso()
    with db() as conn:
        conn.execute(
            "INSERT INTO user_scene_presets(preset_id,label,description,profile_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (preset_id, clean_label, description, json.dumps(payload, ensure_ascii=False, separators=(",", ":")), stamp, stamp),
        )
    # Copy preset-owned Intro/Music/SFX behavior by value; permanent assets stay referenced by stable IDs.
    set_setting(f"preset_media_config::{preset_id}", json.dumps(preset_media_config(source_id), ensure_ascii=False, separators=(",", ":")))
    # Parent preset also inherits its lower Audio Engine preset binding.
    source_audio_binding = scene_audio_engine_binding(source_id)
    if source_audio_binding.get("configured"):
        save_scene_audio_engine_binding(
            preset_id,
            enabled=bool(source_audio_binding.get("enabled", True)),
            audio_engine_preset=str(source_audio_binding.get("audio_engine_preset") or "documentary"),
        )
    return preset_id


def builtin_scene_preset_override(preset_id: str) -> dict[str, Any]:
    if preset_id not in SCENE_PRESETS:
        return {}
    raw = setting(f"preset_profile_override::{preset_id}", "")
    try:
        payload = json.loads(raw) if raw else {}
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        return {}
    return {key: payload[key] for key in PROFILE_FIELDS if key in payload}


def save_builtin_scene_preset_override(preset_id: str, profile: dict[str, Any]) -> None:
    if preset_id not in SCENE_PRESETS:
        raise HTTPException(404, "Built-in preset not found")
    payload = {key: profile[key] for key in PROFILE_FIELDS if key in profile}
    set_setting(f"preset_profile_override::{preset_id}", json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def scene_preset_catalog() -> dict[str, dict[str, Any]]:
    builtins: dict[str, dict[str, Any]] = {}
    for preset_id, raw in {"custom": custom_scene_profile(), **SCENE_PRESETS}.items():
        item = dict(raw)
        if preset_id in SCENE_PRESETS:
            item.update(builtin_scene_preset_override(preset_id))
        item["preset_id"] = preset_id
        item["is_user_preset"] = False
        builtins[preset_id] = item
    builtins.update(user_scene_presets())
    return builtins


def resolve_scene_profile(preset_id: str) -> tuple[str, dict[str, Any]]:
    catalog = scene_preset_catalog()
    selected = str(preset_id or "").strip()
    if selected not in catalog:
        selected = setting("default_scene_preset", "cinematic_documentary")
    if selected not in catalog:
        selected = "cinematic_documentary" if "cinematic_documentary" in catalog else next(iter(catalog))
    return selected, dict(catalog[selected])


def quota_snapshot(jobs: list[dict[str, Any]]) -> tuple[int, int, int]:
    limit = max(0, int(setting("quota_limit", "25") or 25))
    used = sum(1 for job in jobs if str(job.get("status")) in {"completed", "cleaned", "queued", "preparing", "downloading_media", "ready_to_render", "rendering", "ready_to_upload", "drive_uploading", "running"})
    remaining = max(0, limit - used) if limit else 0
    return limit, used, remaining


def storage_snapshot() -> tuple[dict[str, Any], float, int, int]:
    storage = get_storage()
    allowed_gb = max(0.0, float(setting("allowed_storage_gb", "60") or 60))
    allowed_bytes = int(allowed_gb * 1024**3)
    used_bytes = int(storage.get("disk_used_bytes") or 0)
    percent = round((used_bytes / allowed_bytes) * 100, 1) if allowed_bytes else 0
    return storage, allowed_gb, used_bytes, allowed_bytes


def parse_authoritative_scene_plan(script_text: str) -> list[dict[str, Any]]:
    """Use the exact desktop structured-TXT parser.

    Required preferred format:
      SCENE_001|Narration|media_type=...;primary_keyword=...;...

    The desktop parser also preserves the legacy 12-part format.
    """
    with tempfile.TemporaryDirectory(prefix="scene_web_txt_parse_") as temp:
        txt_path = Path(temp) / "scene_plan.txt"
        txt_path.write_text(script_text, encoding="utf-8")
        scenes = parse_outer_ai_scene_txt(txt_path)

    for scene in scenes:
        scene.setdefault("automation_status", "prompt_ready")
        scene.setdefault("status", "prompt_ready")
        scene.setdefault("media_locked", False)
    return scenes


def caption_render_settings(style_name: str, profile: dict[str, Any] | None = None) -> dict[str, Any]:
    style = CAPTION_STYLE_PRESETS.get(style_name, CAPTION_STYLE_PRESETS["karaoke"])
    profile = profile or {}
    requested_outline = max(0, min(8, int(profile.get("caption_outline_width", setting("caption_outline_width", "3")) or 3)))
    return {
        "caption_style": style_name if style_name in CAPTION_STYLE_PRESETS else "karaoke",
        "caption_karaoke_enabled": bool(style["karaoke"]),
        "bold": bool(style["bold"]),
        "italic": False,
        "underline": False,
        "outline_enabled": requested_outline > 0,
        "outline_width": requested_outline,
        "shadow_enabled": bool(style["shadow"]) and bool(profile.get("caption_shadow_enabled", setting("caption_shadow_enabled", "1") == "1")),
        "text_background_enabled": bool(style["background"]),
        "text_background_follow_text": True,
    }

# SCENE_CHANNEL_DRIVE_FOLDERS_V1
SCENE_CHANNELS_FILE = Path(os.getenv("SCENE_CHANNELS_FILE", "/var/lib/scene-builder-web/scene_channels.json"))

def sanitize_scene_channel(value: str) -> str:
    compact = " ".join(str(value or "").strip().split())
    compact = "".join(ch for ch in compact if ch.isalnum() or ch in " ._-").strip(" .")
    return compact[:80] or "General"

def load_scene_channels() -> list[str]:
    raw = []
    try:
        payload = json.loads(SCENE_CHANNELS_FILE.read_text(encoding="utf-8"))
        raw = payload.get("channels", []) if isinstance(payload, dict) else payload
    except (OSError, ValueError, TypeError):
        raw = []
    channels: list[str] = []
    seen: set[str] = set()
    for value in ["General", *(raw if isinstance(raw, list) else [])]:
        name = sanitize_scene_channel(value)
        key = name.casefold()
        if key not in seen:
            seen.add(key); channels.append(name)
    return channels[:200]

def save_scene_channels(values: list[str]) -> list[str]:
    channels: list[str] = []
    seen: set[str] = set()
    for value in ["General", *values]:
        name = sanitize_scene_channel(value)
        key = name.casefold()
        if key not in seen:
            seen.add(key); channels.append(name)
    channels = channels[:200]
    SCENE_CHANNELS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = SCENE_CHANNELS_FILE.with_suffix(SCENE_CHANNELS_FILE.suffix + ".tmp")
    temp.write_text(json.dumps({"version": 1, "channels": channels}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, SCENE_CHANNELS_FILE)
    return channels

def build_project_package(
    project_name: str,
    script_text: str,
    audio_path: Path,
    preset_id: str,
    profile: dict[str, Any],
    channel_name: str,
    destination: Path,
) -> None:
    width, height = profile_dimensions(str(profile["quality"]), str(profile["aspect_ratio"]))
    captions = bool(profile["captions_enabled"])
    safe_name = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in project_name).strip("._") or "Scene_Builder_Project"
    audio_name = audio_path.name
    caption_font_key = str(profile.get("caption_font") or setting("caption_font", DEFAULT_CAPTION_FONT))
    if caption_font_key not in CAPTION_FONTS:
        caption_font_key = DEFAULT_CAPTION_FONT
    caption_font = CAPTION_FONTS[caption_font_key]
    caption_font_source = FONT_DIR / caption_font["filename"]
    if not caption_font_source.is_file():
        raise ValueError(f"Caption font file is missing: {caption_font['filename']}")
    caption_font_relative = f"assets/fonts/{caption_font['filename']}"
    caption_style = str(profile["caption_style"])
    caption_controls = caption_render_settings(caption_style, profile)
    caption_controls["caption_karaoke_enabled"] = bool(profile.get("caption_karaoke_enabled", caption_controls.get("caption_karaoke_enabled", False)))
    media_config = preset_media_config(preset_id)
    first2_audit_enabled = bool(media_config.get("first_two_minute_visual_audit_enabled", False))
    manual_scene_review_requested = setting("scene_review_required", "0") == "1"
    # Queue never overrides production audio. The selected preset is the sole
    # authority for Music, Intro, SFX and related audio behavior.
    music_enabled = bool(media_config.get("music_enabled"))
    selected_music_volume = max(0, min(100, int(media_config.get("music_volume_percent", 12) or 12)))
    selected_ducking = bool(media_config.get("music_ducking_enabled", True))
    scene_plan = parse_authoritative_scene_plan(script_text)
    intro_queries = extract_intro_queries(script_text)
    combined_script = " ".join(str(scene.get("text", "")).strip() for scene in scene_plan).strip()
    metadata = {
        "project_text_file": "project.txt",
        "text_field_is_relative_file": False,
        "vps_generate_whisper": True,
        "remote_package_mode": "text_audio_settings_only",
        "remote_worker_fetches_stock_media": True,
        "web_scene_review_mode": "first2_visual_audit" if first2_audit_enabled else "vps_stock_media",
        "web_manual_scene_review_requested": manual_scene_review_requested,
        "web_submission_version": "7.7.11-professional-editor-v17.5d-r6-internal-audio-studio-v1",
        "web_preset_id": preset_id,
        "web_preset_label": str(profile["label"]),
        "web_channel": sanitize_scene_channel(channel_name),
        "timing_mode": str(profile["timing_mode"]),
        "minimum_scene_seconds": float(profile.get("general_media_min_seconds", profile["min_scene_seconds"])),
        "intro_media_min_seconds": float(profile.get("intro_media_min_seconds", 1.5)),
        "intro_media_max_seconds": float(profile.get("intro_media_max_seconds", 3.0)),
        "hook_media_min_seconds": float(profile.get("hook_media_min_seconds", 1.5)),
        "hook_media_max_seconds": float(profile.get("hook_media_max_seconds", 3.0)),
        "general_media_min_seconds": float(profile.get("general_media_min_seconds", profile["min_scene_seconds"])),
        "general_media_max_seconds": float(profile.get("general_media_max_seconds", profile["max_scene_seconds"])),
        "narration_start_mode": str(profile.get("narration_start_mode", "auto")),
        "narration_start_manual_seconds": float(profile.get("narration_start_manual_seconds", 0.0)),
        "uploaded_audio_master_timeline": True,
        "pexels_enabled": bool(profile.get("pexels_enabled", True)),
        "pexels_selection_mode": str(profile.get("pexels_selection_mode", "best_match_variety")),
        "pexels_search_pool": int(profile.get("pexels_search_pool", 10)),
        "pexels_avoid_used_media": bool(profile.get("pexels_avoid_used_media", True)),
        "pexels_avoid_same_creator": bool(profile.get("pexels_avoid_same_creator", True)),
        "pexels_prefer_landscape": bool(profile.get("pexels_prefer_landscape", True)),
        "pexels_prefer_video": bool(profile.get("pexels_prefer_video", True)),
        "timing_preflight_required": True,
        "avoid_duplicate_media": bool(profile.get("pexels_avoid_used_media", setting("avoid_duplicate_media", "1") == "1")),
        "scene_sync_plan": scene_plan,
        "scene_sync_plan_authoritative": True,
        "web_scene_count": len(scene_plan),
        "intro_queries": intro_queries,
        "intro_queries_authoritative": True,
        "preset_media_config": media_config,
        "internal_audio_engine": dict(profile.get("_internal_audio_engine") or {}),
    }
    data = {
        "project_id": safe_name,
        "project_name": safe_name[:80],
        "text": combined_script,
        "audio_path": f"assets/{audio_name}",
        "output_path": "final.mp4",
        "output_folder": "",
        "width": width,
        "height": height,
        "fps": int(profile["fps"]),
        "captions_enabled": captions,
        "caption_mode": captions,
        "caption_sync_mode": "better",
        "caption_language": "auto",
        "caption_text_source": "whisper",
        "caption_position": str(profile.get("caption_position") or setting("caption_position", "bottom")),
        "caption_max_words": max(1, min(5, int(profile.get("caption_words", setting("caption_words", "3")) or 3))),
        "caption_whisper_model": "base",
        **caption_controls,
        "caption_font_size": max(18, min(120, int(profile.get("caption_font_size", setting("caption_font_size", "48")) or 48))),
        "caption_font": caption_font_key,
        "caption_font_name": caption_font["label"],
        "caption_font_family": caption_font["family"],
        "caption_font_file": caption_font_relative,
        "caption_font_path": caption_font_relative,
        "caption_text_color": str(profile.get("caption_text_color") or setting("caption_text_color", "#ffffff")),
        "caption_outline_color": str(profile.get("caption_outline_color") or setting("caption_outline_color", "#000000")),
        "caption_highlight_color": str(profile.get("caption_highlight_color") or setting("caption_highlight_color", "#ffd54a")),
        "caption_background_color": str(profile.get("caption_background_color") or setting("caption_background_color", "#000000")),
        "caption_background_opacity": max(0, min(100, int(profile.get("caption_background_opacity", setting("caption_background_opacity", "55")) or 55))),
        "font_family": caption_font["family"],
        "font_size": max(18, min(120, int(profile.get("caption_font_size", setting("caption_font_size", "48")) or 48))),
        "text_color": str(profile.get("caption_text_color") or setting("caption_text_color", "#ffffff")),
        "outline_color": str(profile.get("caption_outline_color") or setting("caption_outline_color", "#000000")),
        "text_background_color": str(profile.get("caption_background_color") or setting("caption_background_color", "#000000")),
        "text_background_opacity_percent": max(0, min(100, int(profile.get("caption_background_opacity", setting("caption_background_opacity", "55")) or 55))),
        "caption_export_srt": True,
        "custom_background_enabled": False,
        "background_mode": "gradient",
        "story_visualizer_enabled": False,
        "foreground_enabled": False,
        "voice_volume_percent": max(0, min(150, int(profile.get("voice_volume_percent", setting("voice_volume_percent", "100")) or 100))),
        "voice_processing_preset": str(profile.get("voice_processing_preset") or setting("voice_processing_preset", "off")),
        "music_enabled": music_enabled,
        "music_paths": [],
        "music_assigned_path": "",
        "music_playlist": list(media_config.get("music_playlist", [])),
        "music_ai_select_from_library": bool(media_config.get("music_ai_select_from_library")),
        "music_ai_allowed_categories": list(media_config.get("music_ai_allowed_categories", [])),
        "music_use_track1_for_intro": bool(media_config.get("music_use_track1_for_intro", True)),
        "music_allow_intro_track_in_main": bool(media_config.get("music_allow_intro_track_in_main", False)),
        "music_playlist_loop": bool(media_config.get("music_playlist_loop", True)),
        "music_track_crossfade_seconds": max(0.0, min(8.0, float(media_config.get("music_track_crossfade_seconds", 1.5) or 0.0))),
        "music_volume_percent": selected_music_volume,
        "music_ducking_enabled": selected_ducking,
        "music_ducking_strength": str(media_config.get("music_ducking_strength", "medium")),
        "music_normalize": bool(media_config.get("music_normalize", True)),
        "music_loop_if_short": bool(media_config.get("music_playlist_loop", True)),
        "music_trim_if_long": True,
        "music_loop_crossfade_seconds": max(0.05, min(1.5, float(media_config.get("music_track_crossfade_seconds", 1.5) or 1.5))),
        "music_fade_in_seconds": max(0.0, min(10.0, float(setting("music_fade_in_seconds", "1.5") or 1.5))),
        "music_fade_out_seconds": max(0.0, min(10.0, float(setting("music_fade_out_seconds", "2.0") or 2.0))),
        "intro_enabled": bool(media_config.get("intro_enabled", False)),
        "intro_duration_seconds": max(1, min(600, int(media_config.get("intro_duration_seconds", 15) or 15))),
        "sfx_enabled": bool(media_config.get("sfx_enabled", False)),
        "environment_sfx_enabled": bool(media_config.get("environment_sfx_enabled", True)),
        "transition_sfx_library_enabled": bool(media_config.get("transition_sfx_library_enabled", True)),
        "sfx_amount": str(media_config.get("sfx_amount", "medium")),
        "environment_sfx_volume_percent": max(0, min(60, int(media_config.get("environment_sfx_volume_percent", 18) or 18))),
        "transition_sfx_volume_percent": max(0, min(60, int(media_config.get("transition_sfx_volume_percent", 24) or 24))),
        "auto_clean_project_after_drive": bool(media_config.get("auto_clean_project_after_drive", True)),
        "first_two_minute_visual_audit_enabled": first2_audit_enabled,
        "ai_media_enabled": True,
        "pexels_enabled": bool(profile.get("pexels_enabled", True)),
        "pexels_download_enabled": bool(profile.get("pexels_enabled", True)),
        "pexels_use_library": False,
        "use_image_backgrounds": bool(profile["use_images"]),
        "use_video_backgrounds": bool(profile["use_videos"]),
        "pexels_mix_mode": str(profile["media_mix"]),
        "auto_prepare_backgrounds": True,
        "image_timing_mode": "narration",
        "visual_motion_style": str(profile["motion_style"]),
        "quality_timing_enabled": True,
        "max_scene_seconds": float(profile.get("general_media_max_seconds", profile["max_scene_seconds"])),
        "intro_media_min_seconds": float(profile.get("intro_media_min_seconds", 1.5)),
        "intro_media_max_seconds": float(profile.get("intro_media_max_seconds", 3.0)),
        "hook_media_min_seconds": float(profile.get("hook_media_min_seconds", 1.5)),
        "hook_media_max_seconds": float(profile.get("hook_media_max_seconds", 3.0)),
        "general_media_min_seconds": float(profile.get("general_media_min_seconds", profile["min_scene_seconds"])),
        "general_media_max_seconds": float(profile.get("general_media_max_seconds", profile["max_scene_seconds"])),
        "narration_start_mode": str(profile.get("narration_start_mode", "auto")),
        "narration_start_manual_seconds": max(0.0, min(3600.0, float(profile.get("narration_start_manual_seconds", 0.0) or 0.0))),
        "pexels_selection_mode": str(profile.get("pexels_selection_mode", "best_match_variety")),
        "pexels_search_pool": int(profile.get("pexels_search_pool", 10)),
        "pexels_avoid_used_media": bool(profile.get("pexels_avoid_used_media", True)),
        "pexels_avoid_same_creator": bool(profile.get("pexels_avoid_same_creator", True)),
        "pexels_prefer_landscape": bool(profile.get("pexels_prefer_landscape", True)),
        "pexels_prefer_video": bool(profile.get("pexels_prefer_video", True)),
        "transition_engine_enabled": bool(profile.get("transition_engine_enabled", True)),
        "transition_engine_mode": str(profile.get("transition_mode") or setting("transition_mode", "auto_editorial")),
        "transition_preset": str(profile["transition_preset"]),
        "transition_intensity_percent": int(profile["transition_intensity"]),
        "transition_base_seconds": max(0.12, min(0.50, float(profile.get("transition_duration", setting("transition_duration", "0.28")) or 0.28))),
        "transition_no_repeat": True,
        "transition_hook_cut_bias": True,
        "lut_enabled": bool(profile["lut_enabled"]),
        "lut_mode": "auto",
        "lut_pack": str(profile["lut_pack"]),
        "lut_preset": str(profile["lut_preset"]),
        "lut_strength_percent": int(profile["lut_strength"]),
        "lut_normalize_enabled": bool(profile.get("lut_normalize_enabled", True)),
        # Web remote packages contain text/audio/settings only and intentionally do not
        # bundle a full set of pre-reviewed scene media. Sending the legacy/manual
        # all-scene review gate here makes the VPS reject the job before it can fetch
        # Pexels media or prepare the First 2-Minute Visual Audit. Keep the strict
        # engine gate available for genuinely pre-reviewed portable/desktop packages,
        # but never assert it for this web remote package path.
        "scene_review_required": False,
        "quality_crf": int(profile["quality_crf"]),
        "ffmpeg_preset": str(profile["ffmpeg_preset"]),
        "video_encoder": "cpu_x264",
        "post_render_compress_enabled": bool(profile["compress"]),
        "drive_upload_enabled": bool(profile.get("drive_upload_enabled", True)),
        "metadata": metadata,
    }
    with tempfile.TemporaryDirectory(prefix="scene_web_package_") as temp:
        root = Path(temp)
        assets = root / "assets"
        assets.mkdir()
        shutil.copy2(audio_path, assets / audio_name)
        font_assets = assets / "fonts"
        font_assets.mkdir()
        shutil.copy2(caption_font_source, font_assets / caption_font["filename"])
        (root / "project.txt").write_text(script_text, encoding="utf-8")
        (root / "project.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for item in root.rglob("*"):
                if item.is_file():
                    zf.write(item, item.relative_to(root).as_posix())


def _dashboard_bucket(status: str, *, stage: str = "", drive_status: str = "") -> str:
    status = str(status or "").lower()
    stage = str(stage or "").lower()
    drive_status = str(drive_status or "").lower()
    joined = f"{status} {stage} {drive_status}"
    if any(x in joined for x in ("failed", "error", "interrupted", "manual_required")):
        return "failed"
    uploading_states = {"uploading", "drive_uploading", "upload_in_progress", "uploading_to_drive", "youtube_uploading"}
    if status in uploading_states or stage in uploading_states or drive_status == "uploading":
        return "uploading"
    if status in {"running", "preparing", "downloading_media", "rendering", "generating", "processing", "working"}:
        return "active"
    if status in {"queued", "pending", "waiting_for_audit", "ready_to_render", "ready_to_upload", "ready_transcribe", "ready_render", "held", "retry_wait", "assigned"}:
        return "waiting"
    return ""


def _scene_dashboard_counts(username: str) -> dict[str, int]:
    out = {"waiting": 0, "active": 0, "uploading": 0, "failed": 0}
    for job in jobs_for_user(username):
        bucket = _dashboard_bucket(
            str(job.get("status") or ""),
            stage=str(job.get("stage") or job.get("current_stage") or ""),
            drive_status=str(job.get("drive_status") or ""),
        )
        if bucket:
            out[bucket] += 1
    return out


def _acs_queue_snapshot(username: str, is_admin: bool) -> dict[str, Any]:
    """Prefer the current isolated ACS port; keep the old worker port as fallback.

    Local-only requests provide the same session identity as the Nginx bridge.
    API reachability is service health, never a synthetic failed media job.
    """
    headers = {"X-Auth-User": username, "X-App-Access": "both" if is_admin else ""}
    for port in (8144, 8100):
        try:
            response = requests.get(
                f"http://127.0.0.1:{port}/api/web-flow/queue",
                headers=headers, timeout=2,
            )
            if response.ok:
                payload = response.json()
                if isinstance(payload, dict):
                    return payload
        except (requests.RequestException, ValueError):
            continue
    raise RuntimeError("ACS queue API unavailable")


def _acs_dashboard_counts(username: str, is_admin: bool) -> dict[str, int]:
    out = {"waiting": 0, "active": 0, "uploading": 0, "failed": 0}
    try:
        data = _acs_queue_snapshot(username, is_admin)
        jobs = data.get("jobs") if isinstance(data, dict) else []
        if isinstance(jobs, list):
            for job in jobs:
                if not isinstance(job, dict):
                    continue
                bucket = _dashboard_bucket(
                    str(job.get("status") or ""),
                    stage=str(job.get("pipeline_stage") or job.get("current_stage") or ""),
                    drive_status=str(job.get("drive_status") or ""),
                )
                if bucket:
                    out[bucket] += 1
        counts = data.get("counts") if isinstance(data, dict) else {}
        if isinstance(counts, dict):
            # The queue endpoint can return more rows than the lightweight job list.
            out["waiting"] = max(out["waiting"], int(counts.get("waiting") or 0))
            out["active"] = max(out["active"], int(counts.get("processing") or 0))
            out["failed"] = max(out["failed"], int(counts.get("attention") or 0))
    except Exception:
        pass  # A disconnected API is not a failed media project.
    return out


def _algrow_dashboard_counts(username: str, is_admin: bool) -> dict[str, int]:
    out = {"waiting": 0, "active": 0, "uploading": 0, "failed": 0}
    side = Path("/home/ubuntu/apps/algrow_voice_web/shared/data/web-sidecar/algrow_voice.sqlite3")
    spool = Path("/home/ubuntu/apps/algrow_voice_web/shared/data/delivery_spool.sqlite3")
    try:
        if side.is_file():
            with sqlite3.connect(f"file:{side}?mode=ro", uri=True, timeout=3) as db:
                if is_admin:
                    rows = db.execute("SELECT status,count(*) FROM voice_jobs GROUP BY status").fetchall()
                else:
                    rows = db.execute(
                        """SELECT v.status,count(*) FROM voice_jobs v JOIN projects p ON p.project_id=v.project_id
                           WHERE p.owner_id=? GROUP BY v.status""",
                        (username,),
                    ).fetchall()
                for status, count in rows:
                    status = str(status or "")
                    count = int(count or 0)
                    if status in {"pending", "retry_wait", "assigned"}:
                        out["waiting"] += count
                    elif status == "generating":
                        out["active"] += count
                    elif status in {"manual_required", "failed"}:
                        out["failed"] += count
        if spool.is_file():
            with sqlite3.connect(f"file:{spool}?mode=ro", uri=True, timeout=3) as db:
                if is_admin:
                    rows = db.execute("SELECT status,last_error FROM spool WHERE status IN ('pending','retry','uploading')").fetchall()
                else:
                    rows = db.execute(
                        "SELECT status,last_error FROM spool WHERE owner_id=? AND status IN ('pending','retry','uploading')",
                        (username,),
                    ).fetchall()
                for status, error in rows:
                    status = str(status or "")
                    if status == "uploading":
                        out["uploading"] += 1
                    elif status == "retry" and str(error or "").strip():
                        out["failed"] += 1
                    elif status in {"pending", "retry"}:
                        out["waiting"] += 1
    except Exception:
        pass  # New installs do not have the retired Algrow SQLite directory.
    return out


def _service_active(name: str) -> bool:
    try:
        return subprocess.run(
            ["systemctl", "is-active", "--quiet", name], timeout=2, check=False
        ).returncode == 0
    except Exception:
        return False


def _find_count_values(value: Any, wanted: set[str]) -> int:
    total = 0
    if isinstance(value, dict):
        for key, child in value.items():
            k = str(key).lower().replace("-", "_").replace(" ", "_")
            if k in wanted and isinstance(child, (int, float)):
                total += max(0, int(child))
            elif isinstance(child, (dict, list)):
                total += _find_count_values(child, wanted)
    elif isinstance(value, list):
        for child in value:
            total += _find_count_values(child, wanted)
    return total


def _lum_dashboard_counts() -> dict[str, int]:
    out = {"waiting": 0, "active": 0, "uploading": 0, "failed": 0}
    data: Any = None
    for path in ("/LUM/api/queue", "/LUM/api/dashboard", "/LUM/api/status", "/LUM/health"):
        try:
            response = requests.get(f"http://127.0.0.1:8015{path}", timeout=2)
            if response.ok:
                data = response.json()
                if isinstance(data, dict):
                    break
        except Exception:
            continue
    if isinstance(data, dict):
        out["waiting"] = _find_count_values(data, {"waiting", "pending", "queued", "queue_count"})
        out["active"] = _find_count_values(data, {"active", "processing", "running", "generating"})
        out["uploading"] = _find_count_values(data, {"uploading", "upload_active", "youtube_uploading"})
        out["failed"] = _find_count_values(data, {"failed", "failures", "error_count", "errors"})
    # A missing upload service is reported by the service-health panel,
    # not manufactured as a failed YouTube upload.
    return out


def _storage_dashboard_counts() -> dict[str, int]:
    out = {"waiting": 0, "active": 0, "uploading": 0, "failed": 0}
    try:
        metrics = storage_manager_request("GET", "/v1/metrics", timeout=4)
        # Storage Manager is operational rather than a media queue. Only surface
        # a current operation/failure when the API explicitly reports one.
        out["waiting"] = _find_count_values(metrics, {"waiting", "pending"})
        out["active"] = _find_count_values(metrics, {"active_operations", "running_operations", "processing"})
        out["failed"] = _find_count_values(metrics, {"failed", "failures", "error_count"})
    except Exception:
        pass  # Backend unconfigured does not mean a cleanup operation failed.
    return out


def _dashboard_allowed_app_names(request: Request) -> set[str]:
    if bool(request.session.get("is_admin")):
        return {"Scene Builder", "Auto Caption Studio", "Auto Uploader", "Storage Manager"}
    access = str(request.session.get("app_access") or "scene_builder")
    names: set[str] = set()
    if access in {"scene_builder", "both", "all"}: names.add("Scene Builder")
    if access in {"auto_caption", "both", "all"}: names.add("Auto Caption Studio")
    if access in {"lum", "all"}: names.add("Auto Uploader")
    return names

def _scene_dashboard_detail(username: str) -> dict[str, Any]:
    jobs = jobs_for_user(username)
    return {"completed": sum(1 for j in jobs if str(j.get("status") or "").lower() in {"completed", "cleaned"}), "service_summary": "Scene queue source"}

def _acs_dashboard_detail(username: str, is_admin: bool) -> dict[str, Any]:
    completed = 0
    try:
        data = _acs_queue_snapshot(username, is_admin)
        if isinstance(data, dict):
            counts = data.get("counts") if isinstance(data, dict) else {}
            if isinstance(counts, dict): completed = int(counts.get("completed") or counts.get("done") or 0)
            if not completed and isinstance(data, dict) and isinstance(data.get("jobs"), list):
                completed = sum(1 for j in data["jobs"] if isinstance(j, dict) and str(j.get("status") or "").lower() in {"completed", "done"})
    except Exception: pass
    return {"completed": completed, "service_summary": "ACS queue API"}

def _algrow_dashboard_detail(username: str, is_admin: bool) -> dict[str, Any]:
    completed = 0
    side = Path("/home/ubuntu/apps/algrow_voice_web/shared/data/web-sidecar/algrow_voice.sqlite3")
    try:
        if side.is_file():
            with sqlite3.connect(f"file:{side}?mode=ro", uri=True, timeout=3) as db:
                if is_admin: completed = int(db.execute("SELECT count(*) FROM voice_jobs WHERE status='completed'").fetchone()[0])
                else: completed = int(db.execute("SELECT count(*) FROM voice_jobs v JOIN projects p ON p.project_id=v.project_id WHERE p.owner_id=? AND v.status='completed'", (username,)).fetchone()[0])
    except Exception: pass
    return {"completed": completed, "service_summary": "Algrow live queue"}

def _lum_dashboard_detail() -> dict[str, Any]:
    completed = 0
    for path in ("/LUM/api/queue", "/LUM/api/dashboard", "/LUM/api/status"):
        try:
            r = requests.get(f"http://127.0.0.1:8015{path}", timeout=2)
            if r.ok:
                completed = _find_count_values(r.json(), {"completed", "done", "uploaded", "success"}); break
        except Exception: continue
    return {"completed": completed, "service_summary": "LUM live API"}

def _dashboard_apps(request: Request) -> list[dict[str, Any]]:
    username = str(request.session.get("username") or ""); is_admin = bool(request.session.get("is_admin")); allowed = _dashboard_allowed_app_names(request)
    rows = [
        {"name": "Scene Builder", "available": _service_active("vps1-scene-candidate.service"),
         **_scene_dashboard_counts(username), "detail": _scene_dashboard_detail(username)},
        {"name": "Auto Caption Studio", "available": _service_active("vps1-production-auto-caption-studio.service"),
         **_acs_dashboard_counts(username, is_admin), "detail": _acs_dashboard_detail(username, is_admin)},
        {"name": "Auto Uploader", "available": _service_active("vps1-production-local-upload-manager.service"),
         **_lum_dashboard_counts(), "detail": _lum_dashboard_detail()},
        {"name": "Storage Manager",
         "available": bool(os.getenv("STORAGE_MANAGER_TOKEN", "").strip()) and
                      (_service_active("vps1-storage-candidate.service") or _service_active("vps-storage-manager.service")),
         **_storage_dashboard_counts(), "detail": {"completed": 0, "service_summary": "Storage metrics API"}},
    ]
    return [row for row in rows if row["name"] in allowed]

def _read_cpu_percent() -> float:
    def snap() -> tuple[int, int]:
        nums = [int(x) for x in Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()[1:]]
        return sum(nums), nums[3] + (nums[4] if len(nums) > 4 else 0)
    try:
        import time as _time
        t1,i1=snap(); _time.sleep(0.06); t2,i2=snap(); dt=max(1,t2-t1)
        return round(max(0.0,min(100.0,100.0*(dt-(i2-i1))/dt)),1)
    except Exception: return 0.0

def _vps_dashboard_metrics() -> dict[str, Any]:
    mem: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if ":" in line:
                k,v=line.split(":",1); mem[k]=int(v.strip().split()[0])*1024
    except Exception: pass
    total=int(mem.get("MemTotal",0)); available=int(mem.get("MemAvailable",0)); used=max(0,total-available); disk=shutil.disk_usage("/")
    try: uptime_seconds=float(Path("/proc/uptime").read_text().split()[0])
    except Exception: uptime_seconds=0
    days=int(uptime_seconds//86400); hours=int((uptime_seconds%86400)//3600); minutes=int((uptime_seconds%3600)//60)
    try: load1,load5,load15=os.getloadavg()
    except Exception: load1=load5=load15=0.0
    os_name="Ubuntu Linux"
    try:
        m=re.search(r'^PRETTY_NAME="?([^"\\n]+)',Path("/etc/os-release").read_text(encoding="utf-8"),re.M)
        if m: os_name=m.group(1)
    except Exception: pass
    gb=1024**3
    return {"hostname":os.uname().nodename,"os":os_name,"cpu_cores":os.cpu_count() or 1,"cpu_percent":_read_cpu_percent(),"load_1m":round(load1,2),"load_5m":round(load5,2),"load_15m":round(load15,2),"uptime_seconds":int(uptime_seconds),"uptime_human":f"{days}d {hours}h {minutes}m","ram":{"total_gb":round(total/gb,1),"used_gb":round(used/gb,1),"available_gb":round(available/gb,1),"percent":round((used/total*100) if total else 0,1)},"disk":{"mount":"/","total_gb":round(disk.total/gb,1),"used_gb":round(disk.used/gb,1),"free_gb":round(disk.free/gb,1),"percent":round((disk.used/disk.total*100) if disk.total else 0,1)},"server_time":datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")}

def _dashboard_services() -> list[dict[str, Any]]:
    # Fresh-VPS systemd units, not retired pre-reinstall legacy names.
    services=[
        ("vps1-scene-candidate.service","Scene Builder Web","Shared Scene login / dashboard"),
        ("vps1-production-auto-caption-studio.service","ACS Web","Caption API / queue"),
        ("vps1-production-acs-ingress.service","ACS Ingress","Drive intake pipeline"),
        ("vps1-production-acs-render.service","ACS Renderer","Caption rendering"),
        ("vps1-production-acs-compress.service","ACS Compressor","Video compression"),
        ("vps1-production-acs-upload.service","ACS Uploader","Drive upload"),
        ("vps1-production-voice-studio.service","Voice Studio","Voice API"),
        ("vps1-production-local-upload-manager.service","Auto Uploader","YouTube upload engine"),
        ("vps1-production-lum-worker.service","Uploader Worker","Upload queue processing"),
    ]
    return [{"name":n,"label":l,"role":r,"active":_service_active(n)} for n,l,r in services]

def _dashboard_activity(request: Request) -> list[dict[str, str]]:
    username=str(request.session.get("username") or ""); items=[]; now_ts=datetime.now(timezone.utc).timestamp()
    def when(ts: float) -> str:
        d=max(0,int(now_ts-ts))
        return f"{d}s ago" if d<60 else (f"{d//60}m ago" if d<3600 else (f"{d//3600}h ago" if d<86400 else f"{d//86400}d ago"))
    try:
        for j in jobs_for_user(username)[:20]:
            raw=j.get("updated_at") or j.get("created_at") or ""; ts=0.0
            if isinstance(raw,(int,float)): ts=float(raw)
            elif raw:
                try: ts=datetime.fromisoformat(str(raw).replace("Z","+00:00")).timestamp()
                except Exception: pass
            if ts: items.append((ts,{"title":f"Scene Builder · {str(j.get('status') or 'updated').replace('_',' ')}","detail":str(j.get("project_name") or j.get("id") or "Project"),"when":when(ts)}))
    except Exception: pass
    try:
        side=Path("/home/ubuntu/apps/algrow_voice_web/shared/data/web-sidecar/algrow_voice.sqlite3")
        if side.is_file() and "Algrow Voice" in _dashboard_allowed_app_names(request):
            with sqlite3.connect(f"file:{side}?mode=ro",uri=True,timeout=3) as db:
                for jid,status,ts in db.execute("SELECT id,status,updated_at FROM voice_jobs ORDER BY updated_at DESC LIMIT 10").fetchall():
                    if ts: items.append((float(ts),{"title":f"Algrow Voice · {str(status).replace('_',' ')}","detail":str(jid),"when":when(float(ts))}))
    except Exception: pass
    items.sort(key=lambda x:x[0],reverse=True)
    return [x for _,x in items[:20]]

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request":request,"page":"dashboard","username":request.session.get("username","team")})

@app.get("/api/dashboard/v1")
def dashboard_api(request: Request):
    return {"ok":True,"generated_at":datetime.now(timezone.utc).isoformat(),"generated_at_local":datetime.now().astimezone().strftime("%H:%M:%S"),"apps":_dashboard_apps(request),"vps":_vps_dashboard_metrics(),"services":_dashboard_services(),"activity":_dashboard_activity(request)}


@app.get("/queue", response_class=HTMLResponse)
def queue_page(request: Request):
    jobs = jobs_for_user(str(request.session.get("username") or ""))
    health = get_health()
    quota_limit, quota_used, quota_remaining = quota_snapshot_for_user(str(request.session.get("username") or ""), jobs)
    default_preset, default_profile = resolve_scene_profile(
        setting("default_scene_preset", "cinematic_documentary")
    )
    return templates.TemplateResponse(
        "queue.html",
        {
            "request": request,
            "page": "queue",
            "username": request.session.get("username", "team"),
            "health": health,
            "queue_mode": str(health.get("queue_mode") or "normal"),
            "serial_state": str(health.get("serial_state") or "normal"),
            "serial_reason": str(health.get("serial_reason") or ""),
            "serial_active_job_id": str(health.get("serial_active_job_id") or ""),
            "jobs": jobs,
            "audit_waiting_count": sum(1 for job in jobs if str(job.get("status") or "") == "waiting_for_audit"),
            "quality": setting("quality", "720p"),
            "captions_enabled": setting("captions_enabled", "1") == "1",
            "quota_limit": quota_limit,
            "quota_used": quota_used,
            "quota_remaining": quota_remaining,
            "max_upload_mb": MAX_UPLOAD // (1024 * 1024),
            "caption_words": str(default_profile.get("caption_words", setting("caption_words", "3"))),
            "caption_style": setting("caption_style", "karaoke"),
            "caption_font_size": str(default_profile.get("caption_font_size", setting("caption_font_size", "48"))),
            "caption_font": setting("caption_font", DEFAULT_CAPTION_FONT),
            "caption_text_color": setting("caption_text_color", "#ffffff"),
            "caption_outline_color": setting("caption_outline_color", "#000000"),
            "caption_highlight_color": setting("caption_highlight_color", "#ffd54a"),
            "caption_background_color": setting("caption_background_color", "#000000"),
            "caption_background_opacity": str(default_profile.get("caption_background_opacity", setting("caption_background_opacity", "55"))),
            "caption_position": str(default_profile.get("caption_position") or setting("caption_position", "bottom")),
            "caption_karaoke_enabled": bool(default_profile.get("caption_karaoke_enabled", CAPTION_STYLE_PRESETS.get(str(default_profile.get("caption_style") or setting("caption_style", "karaoke")), CAPTION_STYLE_PRESETS["karaoke"])["karaoke"])),
            "caption_outline_width": str(default_profile.get("caption_outline_width", setting("caption_outline_width", "3"))),
            "caption_shadow_enabled": setting("caption_shadow_enabled", "1") == "1",
            "caption_styles": CAPTION_STYLE_PRESETS,
            "music_volume_percent": setting("music_volume_percent", "12"),
            "music_ducking_enabled": setting("music_ducking_enabled", "1") == "1",
            "scene_presets": scene_preset_catalog(),
            "default_scene_preset": default_preset,
            "default_scene_profile": default_profile,
            "preset_media_configs": all_preset_media_configs(),
            "music_library": list_assets("music"),
            "sfx_library": list_assets("sfx"),
            "preset_media_configs": all_preset_media_configs(),
            "selected_preset_media_config": preset_media_config(default_preset),
            "music_library": list_assets("music"),
            "sfx_library": list_assets("sfx"),
            "submitted_job_id": request.query_params.get("submitted", ""),
            "duplicate_submission": request.query_params.get("duplicate", "") == "1",
            "submission_error": request.query_params.get("error", ""),
            "account": user_account(str(request.session.get("username") or "")) or {},
        },
    )


@app.get("/api/scene-builder/channels")
async def scene_channel_list_api(request: Request):
    account = user_account(str(request.session.get("username") or ""))
    if not account or not int(account.get("is_active") or 0):
        return {"ok": False, "error": "Unauthorized"}
    return {"ok": True, "channels": load_scene_channels()}

@app.post("/api/scene-builder/channels/add")
async def scene_channel_add_api(request: Request):
    account = user_account(str(request.session.get("username") or ""))
    if not account or not int(account.get("is_active") or 0):
        return {"ok": False, "error": "Unauthorized"}
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    requested = str(payload.get("name") or "").strip() if isinstance(payload, dict) else ""
    if not requested:
        return {"ok": False, "error": "Channel name is required"}
    name = sanitize_scene_channel(requested)
    channels = load_scene_channels()
    if name.casefold() not in {x.casefold() for x in channels}:
        channels.append(name)
    return {"ok": True, "channels": save_scene_channels(channels), "selected": name}

@app.post("/api/scene-builder/channels/remove")
async def scene_channel_remove_api(request: Request):
    account = user_account(str(request.session.get("username") or ""))
    if not account or not int(account.get("is_active") or 0):
        return {"ok": False, "error": "Unauthorized"}
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    name = sanitize_scene_channel(str(payload.get("name") or "")) if isinstance(payload, dict) else "General"
    if name.casefold() == "general":
        return {"ok": False, "error": "General is the permanent fallback channel", "channels": load_scene_channels()}
    channels = [x for x in load_scene_channels() if x.casefold() != name.casefold()]
    return {"ok": True, "channels": save_scene_channels(channels), "selected": "General"}


# SCENE_AUDIO_ENGINE_BACKEND_PIPELINE_V2
# Audio Engine is a real Scene Builder pipeline stage. Browser code only selects
# whether/preset; the backend owns processing, completion verification and handoff.
def scene_builder_audio_engine_process(source: Path, temp_dir: Path, preset: str, external_id: str) -> tuple[Path, dict[str, Any]]:
    # SCENE_INTERNAL_AUDIO_LEGACY_HELPER_V1: compatibility path, no standalone service.
    preset = (preset or "documentary").strip() or "documentary"
    with source.open("rb") as handle:
        response = worker_request(
            "POST",
            "/internal-audio/studio/process",
            data={"preset": preset, "mode": "auto", "external_id": external_id},
            files={"file": (source.name, handle, "application/octet-stream")},
            timeout=3600,
        )
    payload = response.json()
    studio_id = str(payload.get("id") or "")
    output_url = str(payload.get("output_url") or "")
    if not studio_id or not output_url:
        raise RuntimeError("Scene Builder internal Audio Engine returned no output")
    out = temp_dir / f"{source.stem}_audio_engine.mp3"
    download = worker_request("GET", output_url, timeout=3600, stream=True)
    try:
        with out.open("wb") as target:
            for chunk in download.iter_content(1024 * 1024):
                if chunk:
                    target.write(chunk)
    finally:
        download.close()
    if not out.is_file() or out.stat().st_size < 1024:
        raise RuntimeError("Scene Builder internal Audio Engine download is empty")
    return out, {"job_id": f"internal-studio:{studio_id}", "preset": preset, "architecture": "scene_builder_worker_internal", "result": payload.get("result") or {}}


@app.post("/queue/submit")
async def submit_project(
    request: Request,
    project_name: str = Form(...),
    preset_id: str = Form(""),
    channel_name: str = Form("General"),
    script_file: UploadFile = File(...),
    audio_file: UploadFile = File(...),
    audio_engine_processed: str = Form(""),  # compatibility with pre-V2 cached clients
    audio_engine_enabled: str = Form("1"),
    audio_engine_preset: str = Form("documentary"),
):
    project_name = project_name.strip()
    if not project_name:
        return submission_error(request, 400, "Project name is required")
    channel_name = sanitize_scene_channel(channel_name)

    submitter = str(request.session.get("username") or "")
    jobs = jobs_for_user(submitter, 1000)
    account = user_account(submitter)
    if not account or not int(account.get("is_active") or 0):
        return submission_error(request, 403, "Account is disabled")
    if account.get("subscription_expired"):
        return submission_error(request, 403, "Subscription has expired")
    quota_limit, quota_used, _ = quota_snapshot_for_user(submitter, jobs)
    if quota_limit and quota_used >= quota_limit:
        return submission_error(request, 409, "Daily successful-render quota has been reached")

    _, allowed_gb, used_bytes, allowed_bytes = storage_snapshot()
    if allowed_bytes and used_bytes >= allowed_bytes:
        return submission_error(request, 507, f"Allowed storage limit of {allowed_gb:g} GB has been reached")

    script_bytes = await script_file.read()
    if len(script_bytes) > 10 * 1024 * 1024:
        return submission_error(request, 413, "Scene TXT is larger than 10 MB")
    try:
        script_text = script_bytes.decode("utf-8-sig").strip()
    except UnicodeDecodeError:
        return submission_error(request, 400, "Scene TXT must be UTF-8 encoded")
    if not script_text:
        return submission_error(request, 400, "Scene TXT is empty")

    original_audio_name = Path(audio_file.filename or "audio.mp3").name
    audio_suffix = Path(original_audio_name).suffix.lower()
    if audio_suffix not in SUPPORTED_NARRATION_SUFFIXES:
        return submission_error(
            request, 400,
            "Unsupported narration format. Use an FFmpeg-compatible audio or video file.",
        )

    with tempfile.TemporaryDirectory(prefix="scene_web_upload_") as temp:
        temp_dir = Path(temp)
        audio_path = temp_dir / original_audio_name
        total = 0
        audio_hasher = hashlib.sha256()
        with audio_path.open("wb") as handle:
            while chunk := await audio_file.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_UPLOAD:
                    return submission_error(request, 413, "Narration upload is too large")
                audio_hasher.update(chunk)
                handle.write(chunk)

        if total == 0:
            return submission_error(request, 400, "Narration media is empty")
        if allowed_bytes and used_bytes + total > allowed_bytes:
            return submission_error(request, 507, "This upload would exceed the allowed storage limit")

        try:
            media_info = validate_narration_media(audio_path)
        except ValueError as exc:
            return submission_error(request, 400, str(exc))

        preset_id, profile = resolve_scene_profile(preset_id)
        # SCENE_INTERNAL_AUDIO_WEB_HANDOFF_V1
        # Web owns validation/packaging only. Narration DSP executes inside the
        # Scene Builder worker before Whisper/timing. No localhost Audio Engine job
        # is created for new Scene Builder submissions.
        audio_engine_was_used = str(audio_engine_processed or "").strip().lower() in {"1", "true", "yes", "on"}
        audio_engine_requested = str(audio_engine_enabled or "1").strip().lower() not in {"0", "false", "no", "off"}
        selected_audio_engine_preset = str(audio_engine_preset or "documentary").strip() or "documentary"
        if selected_audio_engine_preset not in {"documentary", "clean", "natural", "deep", "youtube"}:
            selected_audio_engine_preset = "documentary"
        effective_audio_sha256 = audio_hasher.hexdigest()
        profile = dict(profile)
        if audio_engine_was_used:
            # Cached pre-V2 clients may still submit an already-processed file.
            # Preserve that compatibility without processing it a second time.
            profile["_internal_audio_engine"] = {
                "enabled": False,
                "preset": selected_audio_engine_preset,
                "architecture": "legacy_preprocessed_compat",
            }
            profile["voice_processing_preset"] = "off"
        else:
            profile["_internal_audio_engine"] = {
                "enabled": bool(audio_engine_requested),
                "preset": selected_audio_engine_preset,
                "architecture": "scene_builder_worker_internal",
                "voice_only": True,
                "timeline_owner": "scene_builder",
                "music_owner": "scene_builder",
            }
        quality = str(profile["quality"])
        captions = bool(profile["captions_enabled"])
        submission_fingerprint = ""

        package_path = temp_dir / "project.zip"
        try:
            build_project_package(
                project_name,
                script_text,
                audio_path,
                preset_id,
                profile,
                channel_name,
                package_path,
            )
        except ValueError as exc:
            return submission_error(request, 400, str(exc))

        # V17.2+ deduplication hashes the canonical package configuration, not a
        # hand-picked subset of UI settings. Any rendering/editing setting that
        # changes project.json therefore creates a different job identity.
        try:
            with zipfile.ZipFile(package_path, "r") as package:
                packaged_project = json.loads(package.read("project.json").decode("utf-8"))
            fingerprint_payload = {
                "owner_username": submitter.casefold(),
                "project": packaged_project,
                "script_sha256": hashlib.sha256(script_bytes).hexdigest(),
                "audio_sha256": effective_audio_sha256,
                "submission_version": "7.7.11-professional-editor-v17.5d-r6-internal-audio-studio-v1",
            }
            submission_fingerprint = hashlib.sha256(
                json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            ).hexdigest()
        except Exception as exc:
            return submission_error(request, 500, f"Could not fingerprint project package: {exc}")

        try:
            with package_path.open("rb") as handle:
                response = worker_request(
                    "POST",
                    "/jobs",
                    params={
                        "client_job_key": submission_fingerprint,
                        "submission_version": "web-7.7.11-professional-editor-v17.5d-r6-internal-audio-studio-v1",
                    },
                    files={"file": ("project.zip", handle, "application/zip")},
                    timeout=3600,
                )
        except HTTPException as exc:
            return submission_error(request, exc.status_code, str(exc.detail))

    payload = response.json()
    job_id = str(payload.get("id") or payload.get("job_id") or "")
    duplicate = bool(
        payload.get("duplicate")
        or payload.get("existing")
        or payload.get("reused")
        or payload.get("idempotent_replay")
        or payload.get("deduplicated")
    )
    if not job_id:
        return submission_error(request, 502, "Worker accepted the upload but returned no job ID")
    record_job_owner(job_id, submitter, project_name)
    audit_event("project_submitted", "Website project submission accepted", {
        "job_id": job_id,
        "project_name": project_name,
        "submission_fingerprint": submission_fingerprint,
        "duplicate": duplicate,
        "media_duration": media_info["duration"],
        "media_codec": media_info["audio_codec"],
        "production_audio_source": "audio_engine" if audio_engine_was_used else "scene_builder_legacy",
        "audio_engine_enabled": audio_engine_requested,
        "audio_engine_preset": selected_audio_engine_preset if audio_engine_requested else "",
        "audio_engine_job_id": str(audio_engine_meta.get("job_id") or ""),
    })

    if wants_json(request):
        return JSONResponse({
            "ok": True,
            "job_id": job_id,
            "duplicate": duplicate,
            "submission_fingerprint": submission_fingerprint,
            "audio_engine_used": audio_engine_was_used,
            "audio_engine_job_id": str(audio_engine_meta.get("job_id") or ""),
            "redirect": f"/queue?submitted={job_id}&duplicate={1 if duplicate else 0}",
        })
    return RedirectResponse(
        url=f"/queue?submitted={job_id}&duplicate={1 if duplicate else 0}",
        status_code=303,
    )



def format_project_log_txt(job: dict[str, Any]) -> str:
    """Create a safe diagnostic TXT report for one project."""
    def val(key: str, default: str = "—") -> str:
        raw = job.get(key)
        return default if raw is None or raw == "" else str(raw)

    sensitive = {
        "token", "password", "secret", "api_key", "authorization",
        "cookie", "session", "credential",
    }

    def redact(obj: Any) -> Any:
        if isinstance(obj, dict):
            result = {}
            for key, value in obj.items():
                lowered = str(key).lower()
                result[key] = "[REDACTED]" if any(item in lowered for item in sensitive) else redact(value)
            return result
        if isinstance(obj, list):
            return [redact(value) for value in obj]
        return obj

    stage_raw = job.get("stage_state_json") or job.get("stage_state") or {}
    try:
        stage_state = json.loads(stage_raw) if isinstance(stage_raw, str) else stage_raw
    except Exception:
        stage_state = {"raw": str(stage_raw)}

    lines = [
        "SCENE BUILDER PROJECT DIAGNOSTIC LOG",
        "=" * 72,
        f"Generated at: {now_iso()}",
        "",
        "PROJECT",
        "-" * 72,
        f"Project name: {val('project_name')}",
        f"Job ID: {val('id', val('job_id'))}",
        f"Status: {val('status')}",
        f"Current stage: {val('stage')}",
        f"Progress: {val('progress', '0')}%",
        f"Message: {val('message')}",
        "",
        "FAILURE INFORMATION",
        "-" * 72,
        f"Failed stage: {val('failed_stage')}",
        f"Error code: {val('error_code')}",
        f"Error message: {val('error_message', val('error'))}",
        f"Error log: {val('error_log')}",
        "",
        "TIMESTAMPS",
        "-" * 72,
        f"Created: {val('created_at')}",
        f"Started: {val('started_at')}",
        f"Updated: {val('updated_at')}",
        f"Completed: {val('completed_at')}",
        f"Failed: {val('failed_at')}",
        f"Cancelled: {val('cancelled_at')}",
        "",
        "RETRY AND RESUME",
        "-" * 72,
        f"Attempt: {val('attempt', val('attempts'))}",
        f"Retry count: {val('retry_count')}",
        f"Resume mode: {val('resume_mode')}",
        f"Resume from stage: {val('resume_from_stage')}",
        f"Last completed stage: {val('last_completed_stage')}",
        "",
        "OUTPUT AND GOOGLE DRIVE",
        "-" * 72,
        f"Output filename: {val('output_filename')}",
        f"Output path: {val('output_path')}",
        f"Output validated: {val('output_validated')}",
        f"Drive status: {val('drive_status')}",
        f"Drive destination: {val('drive_destination')}",
        f"Drive file ID: {val('drive_file_id')}",
        f"Drive error: {val('drive_error')}",
        "",
        "SUBMISSION AND DUPLICATE PROTECTION",
        "-" * 72,
        f"Client job key: {val('client_job_key')}",
        f"Submission fingerprint: {val('submission_fingerprint')}",
        f"Submission version: {val('submission_version')}",
        "",
        "STRUCTURED STAGE STATE",
        "-" * 72,
        json.dumps(redact(stage_state), ensure_ascii=False, indent=2, sort_keys=True),
        "",
        "RAW SAFE JOB DATA",
        "-" * 72,
        json.dumps(redact(dict(job)), ensure_ascii=False, indent=2, sort_keys=True),
        "",
        "=" * 72,
        "END OF PROJECT DIAGNOSTIC LOG",
        "",
    ]
    return "\n".join(lines)


@app.get("/api/jobs/{job_id}/log")
def view_project_log(request: Request, job_id: str):
    require_job_access(request, job_id)
    response = worker_request("GET", f"/jobs/{job_id}/log.txt")
    return {"ok": True, "job_id": job_id, "text": response.text}


@app.get("/queue/{job_id}/log.txt")
def download_queue_project_log(request: Request, job_id: str):
    require_job_access(request, job_id)
    response = worker_request("GET", f"/jobs/{job_id}/log.txt")
    job = worker_request("GET", f"/jobs/{job_id}").json()
    project_name = str(job.get("project_name") or "project")
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", project_name).strip("._") or "project"
    audit_event("project_log_downloaded", "Project execution log downloaded", {"job_id": job_id, "project_name": project_name})
    return PlainTextResponse(
        response.text,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}_{job_id}_LOG.txt"',
            "Cache-Control": "no-store",
        },
    )


@app.get("/admin/jobs/{job_id}/log.txt")
def download_project_log(request: Request, job_id: str):
    if not bool(request.session.get("is_admin")):
        raise HTTPException(403, "Administrator access required")
    job = worker_request("GET", f"/jobs/{job_id}").json()
    job["owner_username"] = ownership_map([job_id]).get(job_id, "")
    project_name = str(job.get("project_name") or "project")
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", project_name).strip("._") or "project"
    audit_event(
        "project_log_downloaded",
        "Project diagnostic log downloaded",
        {"job_id": job_id, "project_name": project_name},
    )
    return PlainTextResponse(
        format_project_log_txt(job),
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}_{job_id}_LOG.txt"',
            "Cache-Control": "no-store",
        },
    )


@app.get("/api/jobs")
def api_jobs(request: Request):
    username = str(request.session.get("username") or "")
    return {"jobs": jobs_for_user(username, 500), "server_time": now_iso(), "health": get_health()}


@app.get("/api/jobs/{job_id}")
def api_job_detail(request: Request, job_id: str):
    require_job_access(request, job_id)
    job = worker_request("GET", f"/jobs/{job_id}").json()
    job["owner_username"] = ownership_map([job_id]).get(job_id, "")
    return job



def _web_audit_payload(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = dict(payload or {})
    items = []
    for raw in data.get("items", []) or []:
        if not isinstance(raw, dict):
            continue
        row = dict(raw)
        if row.get("has_media"):
            row["preview_url"] = f"/audit-media/{job_id}/{row.get('item_id')}/preview"
        else:
            row["preview_url"] = ""
        items.append(row)
    data["items"] = items
    return data


@app.get("/audit-media", response_class=HTMLResponse)
def audit_media_page(request: Request):
    username = str(request.session.get("username") or "")
    waiting = [job for job in jobs_for_user(username, 1000) if str(job.get("status") or "") == "waiting_for_audit"]
    selected_job_id = str(request.query_params.get("job") or "").strip()
    selected_job: dict[str, Any] | None = None
    audit_payload: dict[str, Any] | None = None
    if selected_job_id:
        require_job_access(request, selected_job_id)
        selected_job = worker_request("GET", f"/jobs/{selected_job_id}").json()
        try:
            audit_payload = _web_audit_payload(selected_job_id, worker_request("GET", f"/jobs/{selected_job_id}/audit/media").json())
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
    return templates.TemplateResponse(
        "audit_media.html",
        {
            "request": request,
            "page": "audit_media",
            "username": request.session.get("username", "team"),
            "audit_waiting_count": len(waiting),
            "waiting_jobs": waiting,
            "selected_job": selected_job,
            "audit": audit_payload,
            "account": user_account(username) or {},
        },
    )


@app.get("/api/audit-media/{job_id}")
def api_audit_media(request: Request, job_id: str):
    require_job_access(request, job_id)
    return _web_audit_payload(job_id, worker_request("GET", f"/jobs/{job_id}/audit/media").json())


@app.post("/api/audit-media/{job_id}/{item_id}/replace")
async def api_audit_replace(request: Request, job_id: str, item_id: str):
    require_job_access(request, job_id)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    result = worker_request("POST", f"/jobs/{job_id}/audit/media/{item_id}/replace", json=payload).json()
    return _web_audit_payload(job_id, result)


@app.post("/api/audit-media/{job_id}/{item_id}/keyword")
async def api_audit_keyword(request: Request, job_id: str, item_id: str):
    require_job_access(request, job_id)
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(400, "Keyword request must be JSON") from exc
    result = worker_request("POST", f"/jobs/{job_id}/audit/media/{item_id}/keyword", json=payload).json()
    return _web_audit_payload(job_id, result)


@app.post("/api/audit-media/{job_id}/{item_id}/upload")
async def api_audit_upload(request: Request, job_id: str, item_id: str, file: UploadFile = File(...), revision: int | None = Form(None)):
    require_job_access(request, job_id)
    temp_dir = Path(tempfile.mkdtemp(prefix="sb_audit_upload_"))
    temp_path = temp_dir / (Path(file.filename or "replacement.mp4").name or "replacement.mp4")
    total = 0
    try:
        with temp_path.open("wb") as handle:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_UPLOAD:
                    raise HTTPException(413, "Audit replacement is too large")
                handle.write(chunk)
        if total <= 0:
            raise HTTPException(400, "Audit replacement is empty")
        with temp_path.open("rb") as handle:
            result = worker_request(
                "POST", f"/jobs/{job_id}/audit/media/{item_id}/upload",
                params={"revision": revision} if revision is not None else {},
                files={"file": (temp_path.name, handle, "video/mp4")}, timeout=3600,
            ).json()
        return _web_audit_payload(job_id, result)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@app.post("/api/audit-media/{job_id}/approve-all")
def api_audit_approve_all(request: Request, job_id: str):
    require_job_access(request, job_id)
    result = worker_request("POST", f"/jobs/{job_id}/audit/media/approve-all").json()
    return _web_audit_payload(job_id, result)


@app.post("/api/audit-media/{job_id}/continue")
def api_audit_continue(request: Request, job_id: str):
    require_job_access(request, job_id)
    result = worker_request("POST", f"/jobs/{job_id}/audit/continue").json()
    audit_event("visual_audit_approved", "First 2-minute Visual Audit approved", {"job_id": job_id, "username": request.session.get("username", "")})
    return result


@app.get("/audit-media/{job_id}/{item_id}/preview")
def audit_media_preview_proxy(request: Request, job_id: str, item_id: str):
    require_job_access(request, job_id)
    params = worker_params()
    headers = {}
    range_header = request.headers.get("range")
    if range_header:
        headers["Range"] = range_header
    try:
        upstream = requests.get(
            WORKER_URL + f"/jobs/{job_id}/audit/media/{item_id}/preview",
            params=params, headers=headers, stream=True, timeout=60,
        )
    except requests.RequestException as exc:
        raise HTTPException(502, f"Worker unavailable: {exc}") from exc
    if upstream.status_code >= 400:
        text = upstream.text[-1000:]
        upstream.close()
        raise HTTPException(upstream.status_code, text or "Audit preview unavailable")
    response_headers = {"Accept-Ranges": upstream.headers.get("Accept-Ranges", "bytes"), "Cache-Control": "private, max-age=60"}
    for key in ("Content-Range", "Content-Length"):
        if upstream.headers.get(key):
            response_headers[key] = upstream.headers[key]
    content_type = upstream.headers.get("Content-Type") or "application/octet-stream"
    def stream_upstream():
        try:
            for chunk in upstream.iter_content(chunk_size=1024 * 256):
                if chunk:
                    yield chunk
        finally:
            upstream.close()
    return StreamingResponse(
        stream_upstream(),
        status_code=upstream.status_code,
        media_type=content_type,
        headers=response_headers,
    )


@app.get("/api/storage")
def api_storage():
    return get_storage()


@app.post("/api/storage/cleanup")
def api_storage_cleanup_retired(request: Request):
    require_admin(request)
    raise HTTPException(410, "Legacy cleanup is retired. Use /admin/storage.")


@app.get("/api/drive/test")
def api_drive_test():
    health = get_health()
    return {
        "ok": bool(health.get("drive_upload_enabled")),
        "drive_upload_enabled": bool(health.get("drive_upload_enabled")),
        "drive_remote": health.get("drive_remote", ""),
        "drive_base_dir": health.get("drive_base_dir", ""),
        "worker_ok": bool(health.get("ok")),
    }


@app.get("/api/queue/mode")
def queue_mode_api(request: Request):
    return worker_request("GET", "/queue/mode").json()


@app.post("/api/queue/mode")
async def queue_mode_update(request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    mode = str((payload or {}).get("mode") or "").strip().lower()
    if mode not in {"normal", "verified_serial"}:
        raise HTTPException(400, "Queue mode must be normal or verified_serial")
    result = worker_request("POST", "/queue/mode", json={"mode": mode}).json()
    audit_event("queue_mode_changed", "Scene Builder queue behavior changed", {"mode": mode})
    return result


@app.post("/queue/pipeline/resume")
def resume_pipeline_queue(request: Request):
    require_admin(request)
    worker_request("POST", "/pipeline/resume")
    audit_event("pipeline_resumed", "Scene Builder failure circuit manually resumed", {"username": request.session.get("username", "")})
    return RedirectResponse("/queue", status_code=303)


@app.post("/queue/{job_id}/cancel")
def cancel_job(request: Request, job_id: str):
    require_job_access(request, job_id)
    worker_request("POST", f"/jobs/{job_id}/cancel")
    return RedirectResponse("/queue", status_code=303)


@app.post("/queue/{job_id}/serial-retry-cleanup")
def retry_serial_cleanup(request: Request, job_id: str):
    require_job_access(request, job_id)
    worker_request("POST", f"/jobs/{job_id}/serial-retry-cleanup")
    audit_event("serial_cleanup_retried", "Verified Serial cleanup retry requested", {"job_id": job_id})
    return RedirectResponse("/queue", status_code=303)


@app.post("/queue/{job_id}/clean-now")
def clean_delivered_job(request: Request, job_id: str):
    require_job_access(request, job_id)
    worker_request("POST", f"/jobs/{job_id}/clean-now")
    audit_event("delivered_job_cleaned", "Manual verified-Drive project cleanup requested", {"job_id": job_id})
    return RedirectResponse("/queue", status_code=303)


@app.post("/queue/{job_id}/serial-skip")
def skip_serial_job(request: Request, job_id: str):
    require_job_access(request, job_id)
    worker_request("POST", f"/jobs/{job_id}/serial-skip")
    audit_event("serial_job_skipped", "Verified Serial job skipped by operator", {"job_id": job_id})
    return RedirectResponse("/queue", status_code=303)


@app.post("/queue/{job_id}/start")
def start_ready_job(request: Request, job_id: str):
    require_job_access(request, job_id)
    worker_request("POST", f"/jobs/{job_id}/start")
    return RedirectResponse("/queue", status_code=303)


@app.post("/queue/{job_id}/retry")
def retry_job(request: Request, job_id: str):
    require_job_access(request, job_id)
    raise HTTPException(410, "Retry is disabled. Use Resume, Restart Stage, or Restart Job.")


@app.post("/queue/{job_id}/restart-stage")
def restart_stage_job(request: Request, job_id: str):
    require_job_access(request, job_id)
    worker_request("POST", f"/jobs/{job_id}/restart-stage")
    return RedirectResponse("/queue", status_code=303)


@app.post("/queue/{job_id}/resume")
def resume_job(request: Request, job_id: str):
    require_job_access(request, job_id)
    worker_request("POST", f"/jobs/{job_id}/resume")
    return RedirectResponse("/queue", status_code=303)


@app.post("/queue/{job_id}/restart")
def restart_job(request: Request, job_id: str):
    require_job_access(request, job_id)
    worker_request("POST", f"/jobs/{job_id}/restart")
    return RedirectResponse("/queue", status_code=303)


@app.post("/queue/{job_id}/delete")
def delete_job(request: Request, job_id: str):
    require_job_access(request, job_id)
    worker_request("DELETE", f"/jobs/{job_id}")
    with db() as conn:
        conn.execute("DELETE FROM job_ownership WHERE job_id=?", (job_id,))
    return RedirectResponse("/queue", status_code=303)


@app.get("/queue/{job_id}/download")
def download_job(request: Request, job_id: str):
    require_job_access(request, job_id)
    try:
        upstream = requests.get(
            WORKER_URL + f"/jobs/{job_id}/download",
            params=worker_params(),
            stream=True,
            timeout=24 * 60 * 60,
        )
    except requests.RequestException as exc:
        raise HTTPException(502, f"Download failed: {exc}") from exc
    if upstream.status_code >= 400:
        raise HTTPException(upstream.status_code, upstream.text[-1000:])
    headers = {}
    if upstream.headers.get("content-disposition"):
        headers["Content-Disposition"] = upstream.headers["content-disposition"]
    return StreamingResponse(upstream.iter_content(1024 * 1024), media_type="video/mp4", headers=headers)


# SCENE_INTERNAL_AUDIO_STUDIO_WEB_API_V1
@app.get("/api/internal-audio/presets")
def scene_internal_audio_presets(request: Request):
    require_login(request)
    payload = worker_request("GET", "/internal-audio/presets", timeout=20).json()
    if not isinstance(payload, dict) or not payload.get("ok"):
        raise HTTPException(502, "Internal Audio Engine preset response invalid")
    return payload


@app.post("/api/internal-audio/studio/process")
async def scene_internal_audio_studio_process(request: Request):
    require_login(request)
    form = await request.form()
    upload = form.get("file") or form.get("files")
    if upload is None or not hasattr(upload, "file"):
        raise HTTPException(400, "Narration file is required")
    data: dict[str, str] = {}
    for key, value in form.multi_items():
        if hasattr(value, "file"):
            continue
        data[str(key)] = str(value)
    try:
        upload.file.seek(0)
    except Exception:
        pass
    upstream = worker_request(
        "POST",
        "/internal-audio/studio/process",
        data=data,
        files={"file": (str(getattr(upload, "filename", "") or "narration.bin"), upload.file, str(getattr(upload, "content_type", "") or "application/octet-stream"))},
        timeout=3600,
    )
    payload = upstream.json()
    if not isinstance(payload, dict) or not payload.get("ok") or not payload.get("id"):
        raise HTTPException(502, "Internal Audio Studio response invalid")
    payload["output_url"] = f"/api/internal-audio/studio/{payload['id']}/output"
    return payload


@app.get("/api/internal-audio/studio/{studio_id}/output")
def scene_internal_audio_studio_output(request: Request, studio_id: str):
    require_login(request)
    if not re.fullmatch(r"[0-9a-f]{32}", str(studio_id or "")):
        raise HTTPException(404, "Studio output not found")
    upstream = worker_request("GET", f"/internal-audio/studio/{studio_id}/output", timeout=3600, stream=True)
    def stream_audio():
        try:
            for chunk in upstream.iter_content(1024 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()
    return StreamingResponse(stream_audio(), media_type="audio/mpeg", headers={"Cache-Control": "no-store"})


# SCENE_PARENT_CHILD_AUDIO_PRESET_V1 routes
@app.get("/api/presets/{preset_id}/audio-engine")
def preset_audio_engine_get(preset_id: str):
    return {"ok": True, "scene_preset_id": preset_id, **scene_audio_engine_binding(preset_id)}


@app.post("/api/presets/{preset_id}/audio-engine")
async def preset_audio_engine_save(request: Request, preset_id: str):
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(400, "Invalid Audio Engine preset binding request") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "Invalid Audio Engine preset binding request")
    child = str(payload.get("audio_engine_preset") or payload.get("preset") or "").strip()
    result = save_scene_audio_engine_binding(
        preset_id,
        enabled=bool(payload.get("enabled", True)),
        audio_engine_preset=child,
    )
    audit_event("preset_audio_engine_saved", "Audio Engine child preset linked to Scene Builder parent preset", {
        "scene_preset_id": preset_id,
        "audio_engine_preset": result["audio_engine_preset"],
        "enabled": result["enabled"],
    })
    return {"ok": True, "scene_preset_id": preset_id, **result}


# V17.4 custom preset management. Built-in presets are immutable templates;
# user presets can be named, copied, renamed and deleted without touching assets.
@app.post("/api/presets")
async def preset_create(request: Request):
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(400, "Invalid preset request") from exc
    source = str(payload.get("copy_from") or setting("default_scene_preset", "cinematic_documentary"))
    preset_id = create_user_scene_preset(str(payload.get("name") or ""), source)
    row = scene_preset_catalog()[preset_id]
    audit_event("preset_created", f"Created preset {row['label']}", {"preset_id": preset_id, "copied_from": source})
    return {"ok": True, "preset_id": preset_id, "preset": row}


@app.post("/api/presets/{preset_id}/duplicate")
async def preset_duplicate(request: Request, preset_id: str):
    if preset_id not in scene_preset_catalog():
        raise HTTPException(404, "Preset not found")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    source = scene_preset_catalog()[preset_id]
    name = str(payload.get("name") or f"{source.get('label') or 'Preset'} Copy")
    new_id = create_user_scene_preset(name, preset_id)
    row = scene_preset_catalog()[new_id]
    audit_event("preset_duplicated", f"Duplicated preset {source.get('label')} as {row['label']}", {"source_preset_id": preset_id, "preset_id": new_id})
    return {"ok": True, "preset_id": new_id, "preset": row}


@app.post("/api/presets/{preset_id}/rename")
async def preset_rename(request: Request, preset_id: str):
    if not is_user_scene_preset(preset_id):
        raise HTTPException(409, "Built-in presets cannot be renamed. Duplicate it first.")
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(400, "Invalid preset rename request") from exc
    label = _unique_preset_label(str(payload.get("name") or ""), exclude_id=preset_id)
    with db() as conn:
        conn.execute("UPDATE user_scene_presets SET label=?,updated_at=? WHERE preset_id=?", (label, now_iso(), preset_id))
    audit_event("preset_renamed", f"Renamed preset to {label}", {"preset_id": preset_id})
    return {"ok": True, "preset_id": preset_id, "preset": scene_preset_catalog()[preset_id]}


@app.delete("/api/presets/{preset_id}")
def preset_delete(request: Request, preset_id: str):
    if not is_user_scene_preset(preset_id):
        raise HTTPException(409, "Built-in presets cannot be deleted. Custom presets can be deleted.")
    row = scene_preset_catalog().get(preset_id)
    with db() as conn:
        conn.execute("DELETE FROM user_scene_presets WHERE preset_id=?", (preset_id,))
        conn.execute("DELETE FROM settings WHERE key=?", (f"preset_media_config::{preset_id}",))
    fallback = setting("default_scene_preset", "cinematic_documentary")
    if fallback == preset_id or fallback not in scene_preset_catalog():
        fallback = "cinematic_documentary" if "cinematic_documentary" in scene_preset_catalog() else next(iter(scene_preset_catalog()))
        set_setting("default_scene_preset", fallback)
    audit_event("preset_deleted", f"Deleted preset {row.get('label') if row else preset_id}", {"preset_id": preset_id, "fallback_default": fallback})
    return {"ok": True, "deleted": preset_id, "default_scene_preset": fallback}


# V17.4 permanent reusable Music/SFX library APIs.
def _library_kind(kind: str) -> str:
    value = str(kind or "").lower().strip()
    if value not in {"music", "sfx"}:
        raise HTTPException(404, "Unknown audio library")
    return value


@app.get("/api/audio-library/{kind}")
def audio_library_list(request: Request, kind: str):
    kind = _library_kind(kind)
    rows = list_assets(kind)
    for row in rows:
        row["used_by_presets"] = audio_asset_usage(str(row.get("asset_id") or "")) if kind == "music" else []
    return {"kind": kind, "assets": rows, "permanent": True}


@app.post("/api/audio-library/{kind}/upload")
async def audio_library_upload(request: Request, kind: str, files: list[UploadFile] = File(...), category: str = Form("custom")):
    kind = _library_kind(kind)
    if not files:
        raise HTTPException(400, "Choose at least one audio file")
    ensure_library()
    created: list[dict[str, Any]] = []
    for upload in files[:100]:
        original = Path(upload.filename or "audio").name
        suffix = Path(original).suffix.lower()
        if suffix not in SUPPORTED_NARRATION_SUFFIXES:
            raise HTTPException(400, f"Unsupported audio format: {original}")
        fd, temp_name = tempfile.mkstemp(prefix=".library-upload-", suffix=suffix, dir=str(PERMANENT_ROOT))
        os.close(fd)
        temp_path = Path(temp_name)
        total = 0
        try:
            with temp_path.open("wb") as handle:
                while chunk := await upload.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_UPLOAD:
                        raise HTTPException(413, f"Audio file is too large: {original}")
                    handle.write(chunk)
            if total < 1:
                raise HTTPException(400, f"Audio file is empty: {original}")
            media = validate_narration_media(temp_path)
            row = create_asset_record(kind, temp_path, original, category=category, duration=float(media.get("duration") or 0), created_at=now_iso())
            created.append(row)
        finally:
            temp_path.unlink(missing_ok=True)
    audit_event("audio_library_upload", f"Uploaded {len(created)} {kind} asset(s)", {"kind": kind, "asset_ids": [x.get("asset_id") for x in created]})
    return {"ok": True, "assets": created}


@app.post("/api/audio-library/{kind}/{asset_id}/update")
async def audio_library_update(request: Request, kind: str, asset_id: str):
    kind = _library_kind(kind)
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(400, "Invalid asset update") from exc
    try:
        row = update_asset(
            kind, asset_id,
            display_name=str(payload.get("display_name")) if "display_name" in payload else None,
            category=str(payload.get("category")) if "category" in payload else None,
            tags=[str(x) for x in payload.get("tags", [])] if isinstance(payload.get("tags"), list) else None,
        )
    except KeyError as exc:
        raise HTTPException(404, "Audio asset not found") from exc
    audit_event("audio_library_update", f"Updated {kind} asset", {"kind": kind, "asset_id": asset_id})
    return {"ok": True, "asset": row}


@app.delete("/api/audio-library/{kind}/{asset_id}")
def audio_library_delete(request: Request, kind: str, asset_id: str):
    kind = _library_kind(kind)
    used = audio_asset_usage(asset_id) if kind == "music" else []
    if used:
        raise HTTPException(409, "Asset is still used by preset(s): " + ", ".join(used))
    try:
        delete_asset(kind, asset_id)
    except KeyError as exc:
        raise HTTPException(404, "Audio asset not found") from exc
    audit_event("audio_library_delete", f"Deleted {kind} asset", {"kind": kind, "asset_id": asset_id})
    return {"ok": True}


@app.get("/api/audio-library/{kind}/{asset_id}/stream")
def audio_library_stream(request: Request, kind: str, asset_id: str):
    kind = _library_kind(kind)
    try:
        path = audio_asset_path(kind, asset_id)
    except KeyError as exc:
        raise HTTPException(404, "Audio asset not found") from exc
    return FileResponse(path, filename=path.name, media_type=(mimetypes.guess_type(path.name)[0] or "application/octet-stream"), headers={"Cache-Control": "private, max-age=3600"})


def require_settings_csrf(request: Request) -> None:
    expected = str(request.session.get("settings_csrf") or "")
    supplied = str(request.headers.get("x-settings-csrf") or "")
    if not expected or not supplied or not secrets.compare_digest(expected, supplied):
        raise HTTPException(403, "Invalid settings action token")


def settings_pexels_payload() -> dict[str, Any]:
    health = get_health()
    managed = masked_managed_pexels_keys()
    total = int(health.get("pexels_key_count") or len(managed))
    return {
        "ok": True,
        "keys": managed,
        "managed_count": len(managed),
        "total_key_count": total,
        "environment_key_count": max(0, total - len(managed)),
        "dynamic_reload": True,
    }


@app.get("/api/settings/pexels-keys")
def settings_pexels_keys(request: Request):
    require_admin(request)
    return settings_pexels_payload()


@app.post("/api/settings/pexels-keys")
async def settings_pexels_keys_update(request: Request):
    require_admin(request)
    require_settings_csrf(request)
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(400, "Invalid Pexels key request") from exc
    action = str(payload.get("action") or "").lower()
    keys = managed_pexels_keys()
    if action == "add":
        key = str(payload.get("key") or "").strip()
        if len(key) < 10 or len(key) > 512 or re.search(r"\s", key):
            raise HTTPException(400, "Invalid Pexels API key")
        if key not in keys:
            keys.append(key)
        save_managed_pexels_keys(keys)
        audit_event("pexels_key_added", "Administrator added a masked supplemental Pexels key from Scene Builder Settings", {"key_id": hashlib.sha256(key.encode()).hexdigest()[:16]})
    elif action == "remove":
        key_id = str(payload.get("key_id") or "")
        matched = next((key for key in keys if hashlib.sha256(key.encode()).hexdigest()[:16] == key_id), None)
        if not matched:
            raise HTTPException(404, "Managed Pexels key not found")
        save_managed_pexels_keys([key for key in keys if key != matched])
        audit_event("pexels_key_removed", "Administrator removed a masked supplemental Pexels key from Scene Builder Settings", {"key_id": key_id})
    else:
        raise HTTPException(400, "Action must be add or remove")
    return settings_pexels_payload()


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    health = get_health()
    settings_csrf = ""
    if bool(request.session.get("is_admin")):
        settings_csrf = str(request.session.get("settings_csrf") or secrets.token_urlsafe(32))
        request.session["settings_csrf"] = settings_csrf
    managed_keys = masked_managed_pexels_keys() if bool(request.session.get("is_admin")) else []
    total_pexels_keys = int(health.get("pexels_key_count") or len(managed_keys))
    lut_pack, lut_preset = validated_lut_selection(
        setting("lut_pack", "cinematic"),
        setting("lut_preset", "filmic"),
    )
    saved_default_preset, _ = resolve_scene_profile(setting("default_scene_preset", "cinematic_documentary"))
    requested_preset = str(request.query_params.get("preset") or saved_default_preset).strip()
    editing_preset, editing_profile = resolve_scene_profile(requested_preset)
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "page": "settings",
            "username": request.session.get("username", "team"),
            "audit_waiting_count": audit_waiting_count_for_user(str(request.session.get("username") or "")),
            "quality": str(editing_profile.get("quality") or setting("quality", "720p")),
            "captions_enabled": bool(editing_profile.get("captions_enabled", setting("captions_enabled", "1") == "1")),
            "caption_words": str(editing_profile.get("caption_words", setting("caption_words", "3"))),
            "caption_style": str(editing_profile.get("caption_style") or setting("caption_style", "karaoke")),
            "caption_font_size": str(editing_profile.get("caption_font_size", setting("caption_font_size", "48"))),
            "caption_font": str(editing_profile.get("caption_font") or setting("caption_font", DEFAULT_CAPTION_FONT)),
            "caption_fonts": CAPTION_FONTS,
            "caption_text_color": str(editing_profile.get("caption_text_color") or setting("caption_text_color", "#ffffff")),
            "caption_outline_color": str(editing_profile.get("caption_outline_color") or setting("caption_outline_color", "#000000")),
            "caption_highlight_color": str(editing_profile.get("caption_highlight_color") or setting("caption_highlight_color", "#ffd54a")),
            "caption_background_color": str(editing_profile.get("caption_background_color") or setting("caption_background_color", "#000000")),
            "caption_background_opacity": str(editing_profile.get("caption_background_opacity", setting("caption_background_opacity", "55"))),
            "caption_position": str(editing_profile.get("caption_position") or setting("caption_position", "bottom")),
            "caption_karaoke_enabled": bool(editing_profile.get("caption_karaoke_enabled", CAPTION_STYLE_PRESETS.get(str(editing_profile.get("caption_style") or setting("caption_style", "karaoke")), CAPTION_STYLE_PRESETS["karaoke"])["karaoke"])),
            "caption_outline_width": str(editing_profile.get("caption_outline_width", setting("caption_outline_width", "3"))),
            "caption_shadow_enabled": bool(editing_profile.get("caption_shadow_enabled", setting("caption_shadow_enabled", "1") == "1")),
            "caption_styles": CAPTION_STYLE_PRESETS,
            "quota_limit": setting("quota_limit", "25"),
            "allowed_storage_gb": setting("allowed_storage_gb", "60"),
            "retention_enabled": setting("retention_enabled", "1") == "1",
            "retention_free_gb": setting("retention_free_gb", "20"),
            "retention_last_run": setting("retention_last_run", ""),
            "announcement": setting("announcement"),
            "scene_presets": scene_preset_catalog(),
            "default_scene_preset": editing_preset,
            "default_scene_profile": editing_profile,
            "custom_aspect_ratio": str(editing_profile.get("aspect_ratio") or setting("custom_aspect_ratio", "16:9")),
            "timing_mode": str(editing_profile.get("timing_mode") or setting("timing_mode", "auto_repair")),
            "max_scene_seconds": str(editing_profile.get("general_media_max_seconds", editing_profile.get("max_scene_seconds", setting("general_media_max_seconds", setting("max_scene_seconds", "4.0"))))),
            "min_scene_seconds": str(editing_profile.get("general_media_min_seconds", editing_profile.get("min_scene_seconds", setting("general_media_min_seconds", setting("min_scene_seconds", "2.0"))))),
            "intro_media_min_seconds": str(editing_profile.get("intro_media_min_seconds", setting("intro_media_min_seconds", "1.5"))),
            "intro_media_max_seconds": str(editing_profile.get("intro_media_max_seconds", setting("intro_media_max_seconds", "3.0"))),
            "hook_media_min_seconds": str(editing_profile.get("hook_media_min_seconds", setting("hook_media_min_seconds", "1.5"))),
            "hook_media_max_seconds": str(editing_profile.get("hook_media_max_seconds", setting("hook_media_max_seconds", "3.0"))),
            "general_media_min_seconds": str(editing_profile.get("general_media_min_seconds", setting("general_media_min_seconds", "2.0"))),
            "general_media_max_seconds": str(editing_profile.get("general_media_max_seconds", setting("general_media_max_seconds", "4.0"))),
            "narration_start_mode": str(editing_profile.get("narration_start_mode", setting("narration_start_mode", "auto"))),
            "narration_start_manual_seconds": str(editing_profile.get("narration_start_manual_seconds", setting("narration_start_manual_seconds", "0.0"))),
            "pexels_enabled": bool(editing_profile.get("pexels_enabled", setting("pexels_enabled", "1") == "1")),
            "pexels_selection_mode": str(editing_profile.get("pexels_selection_mode", setting("pexels_selection_mode", "best_match_variety"))),
            "pexels_search_pool": str(editing_profile.get("pexels_search_pool", setting("pexels_search_pool", "10"))),
            "pexels_avoid_used_media": bool(editing_profile.get("pexels_avoid_used_media", setting("pexels_avoid_used_media", "1") == "1")),
            "pexels_avoid_same_creator": bool(editing_profile.get("pexels_avoid_same_creator", setting("pexels_avoid_same_creator", "1") == "1")),
            "pexels_prefer_landscape": bool(editing_profile.get("pexels_prefer_landscape", setting("pexels_prefer_landscape", "1") == "1")),
            "pexels_prefer_video": bool(editing_profile.get("pexels_prefer_video", setting("pexels_prefer_video", "1") == "1")),
            "media_mix": str(editing_profile.get("media_mix") or setting("media_mix", "video_first")),
            "use_image_backgrounds": bool(editing_profile.get("use_images", setting("use_image_backgrounds", "1") == "1")),
            "use_video_backgrounds": bool(editing_profile.get("use_videos", setting("use_video_backgrounds", "1") == "1")),
            "avoid_duplicate_media": setting("avoid_duplicate_media", "1") == "1",
            "scene_review_required": setting("scene_review_required", "0") == "1",
            "motion_style": str(editing_profile.get("motion_style") or setting("motion_style", "cinematic")),
            "transition_engine_enabled": bool(editing_profile.get("transition_engine_enabled", setting("transition_engine_enabled", "1") == "1")),
            "transition_mode": str(editing_profile.get("transition_mode") or setting("transition_mode", "auto_editorial")),
            "transition_preset": str(editing_profile.get("transition_preset") or setting("transition_preset", "storytelling")),
            "transition_intensity": str(editing_profile.get("transition_intensity", setting("transition_intensity", "38"))),
            "transition_duration": str(editing_profile.get("transition_duration", setting("transition_duration", "0.28"))),
            "lut_enabled": bool(editing_profile.get("lut_enabled", setting("lut_enabled", "1") == "1")),
            "lut_pack": str(editing_profile.get("lut_pack") or lut_pack),
            "lut_preset": str(editing_profile.get("lut_preset") or lut_preset),
            "lut_strength": str(editing_profile.get("lut_strength", setting("lut_strength", "38"))),
            "lut_normalize_enabled": bool(editing_profile.get("lut_normalize_enabled", setting("lut_normalize_enabled", "1") == "1")),
            "lut_packs": LUT_PACKS,
            "voice_volume_percent": str(editing_profile.get("voice_volume_percent", setting("voice_volume_percent", "100"))),
            "voice_processing_preset": str(editing_profile.get("voice_processing_preset") or setting("voice_processing_preset", "off")),
            "music_volume_percent": setting("music_volume_percent", "12"),
            "music_ducking_enabled": setting("music_ducking_enabled", "1") == "1",
            "music_ducking_strength": setting("music_ducking_strength", "medium"),
            "music_normalize": setting("music_normalize", "1") == "1",
            "music_loop_if_short": setting("music_loop_if_short", "1") == "1",
            "music_loop_crossfade_seconds": setting("music_loop_crossfade_seconds", "0.5"),
            "music_fade_in_seconds": setting("music_fade_in_seconds", "1.5"),
            "music_fade_out_seconds": setting("music_fade_out_seconds", "2.0"),
            "saved_default_scene_preset": saved_default_preset,
            "preset_media_configs": all_preset_media_configs(),
            "selected_preset_media_config": preset_media_config(editing_preset),
            "music_library": list_assets("music"),
            "sfx_library": list_assets("sfx"),
            "settings_csrf": settings_csrf,
            "pexels_managed_keys": managed_keys,
            "pexels_total_key_count": total_pexels_keys,
            "pexels_environment_key_count": max(0, total_pexels_keys - len(managed_keys)),
            "output_fps": str(editing_profile.get("fps", setting("output_fps", "24"))),
            "quality_crf": str(editing_profile.get("quality_crf", setting("quality_crf", "20"))),
            "ffmpeg_preset": str(editing_profile.get("ffmpeg_preset") or setting("ffmpeg_preset", "veryfast")),
            "post_render_compress_enabled": bool(editing_profile.get("compress", setting("post_render_compress_enabled", "0") == "1")),
            "drive_upload_enabled": bool(editing_profile.get("drive_upload_enabled", setting("drive_upload_enabled", "1") == "1")),
        },
    )


@app.post("/settings")
def save_settings(
    quality: str = Form(...),
    default_scene_preset: str = Form("cinematic_documentary"),
    custom_aspect_ratio: str = Form("16:9"),
    timing_mode: str = Form("auto_repair"),
    max_scene_seconds: float = Form(4.0),
    min_scene_seconds: float = Form(2.0),
    intro_media_min_seconds: float = Form(1.5),
    intro_media_max_seconds: float = Form(3.0),
    hook_media_min_seconds: float = Form(1.5),
    hook_media_max_seconds: float = Form(3.0),
    general_media_min_seconds: float = Form(2.0),
    general_media_max_seconds: float = Form(4.0),
    narration_start_mode: str = Form("auto"),
    narration_start_manual_seconds: float = Form(0.0),
    pexels_enabled: str | None = Form(None),
    pexels_selection_mode: str = Form("best_match_variety"),
    pexels_search_pool: int = Form(10),
    pexels_avoid_used_media: str | None = Form(None),
    pexels_avoid_same_creator: str | None = Form(None),
    pexels_prefer_landscape: str | None = Form(None),
    pexels_prefer_video: str | None = Form(None),
    media_mix: str = Form("video_first"),
    use_image_backgrounds: str | None = Form(None),
    use_video_backgrounds: str | None = Form(None),
    avoid_duplicate_media: str | None = Form(None),
    scene_review_required: str | None = Form(None),
    motion_style: str = Form("cinematic"),
    transition_engine_enabled: str | None = Form(None),
    transition_mode: str = Form("auto_editorial"),
    transition_preset: str = Form("storytelling"),
    transition_intensity: int = Form(38),
    transition_duration: float = Form(0.28),
    lut_enabled: str | None = Form(None),
    lut_pack: str = Form("cinematic"),
    lut_preset: str = Form("filmic"),
    lut_strength: int = Form(38),
    lut_normalize_enabled: str | None = Form(None),
    output_fps: int = Form(24),
    quality_crf: int = Form(20),
    ffmpeg_preset: str = Form("veryfast"),
    post_render_compress_enabled: str | None = Form(None),
    drive_upload_enabled: str | None = Form(None),
    captions_enabled: str | None = Form(None),
    caption_words: int = Form(3),
    caption_style: str = Form("karaoke"),
    caption_karaoke_enabled: str | None = Form(None),
    caption_font_size: int = Form(48),
    caption_font: str = Form(DEFAULT_CAPTION_FONT),
    caption_text_color: str = Form("#ffffff"),
    caption_outline_color: str = Form("#000000"),
    caption_highlight_color: str = Form("#ffd54a"),
    caption_background_color: str = Form("#000000"),
    caption_background_opacity: int = Form(55),
    caption_position: str = Form("bottom"),
    caption_outline_width: int = Form(3),
    caption_shadow_enabled: str | None = Form(None),
    voice_volume_percent: int = Form(100),
    voice_processing_preset: str = Form("off"),
    music_volume_percent: int = Form(12),
    music_ducking_enabled: str | None = Form(None),
    music_ducking_strength: str = Form("medium"),
    music_normalize: str | None = Form(None),
    music_loop_if_short: str | None = Form(None),
    music_loop_crossfade_seconds: float = Form(0.5),
    music_fade_in_seconds: float = Form(1.5),
    music_fade_out_seconds: float = Form(2.0),
    preset_intro_enabled: str | None = Form(None),
    preset_intro_duration_seconds: int = Form(15),
    preset_music_enabled: str | None = Form(None),
    preset_music_playlist_json: str = Form("[]"),
    preset_music_ai_select: str | None = Form(None),
    preset_music_ai_allowed_categories_json: str = Form("[]"),
    preset_music_volume_percent: int = Form(12),
    preset_music_ducking_enabled: str | None = Form(None),
    preset_music_ducking_strength: str = Form("medium"),
    preset_music_normalize: str | None = Form(None),
    preset_music_playlist_loop: str | None = Form(None),
    preset_music_crossfade_seconds: float = Form(1.5),
    preset_music_use_track1_for_intro: str | None = Form(None),
    preset_music_allow_intro_track_in_main: str | None = Form(None),
    preset_sfx_enabled: str | None = Form(None),
    preset_environment_sfx_enabled: str | None = Form(None),
    preset_transition_sfx_enabled: str | None = Form(None),
    preset_sfx_amount: str = Form("medium"),
    preset_environment_sfx_volume_percent: int = Form(18),
    preset_transition_sfx_volume_percent: int = Form(24),
    preset_auto_clean_project_after_drive: str | None = Form(None),
    preset_auto_clean_disable_confirmation: str = Form(""),
    preset_first_two_minute_visual_audit_enabled: str | None = Form(None),
    quota_limit: int = Form(...),
    allowed_storage_gb: float = Form(...),
    retention_enabled: str | None = Form(None),
    retention_free_gb: float = Form(20),
    announcement: str = Form(""),
):
    if quality not in {"480p", "720p", "1080p"}:
        raise HTTPException(400, "Invalid quality")
    if caption_style not in CAPTION_STYLE_PRESETS:
        raise HTTPException(400, "Invalid caption style")
    if caption_font not in CAPTION_FONTS:
        raise HTTPException(400, "Invalid caption font")
    if default_scene_preset not in scene_preset_catalog():
        raise HTTPException(400, "Invalid default preset")
    if custom_aspect_ratio not in {"16:9", "9:16", "1:1"}:
        raise HTTPException(400, "Invalid aspect ratio")
    if timing_mode not in {"auto_repair", "whisper", "narration_weighted"}:
        raise HTTPException(400, "Invalid timing mode")
    if narration_start_mode not in {"auto", "manual"}:
        raise HTTPException(400, "Invalid Narration Start mode")
    if not 0.0 <= float(narration_start_manual_seconds) <= 3600.0:
        raise HTTPException(400, "Narration Start manual seconds must be between 0 and 3600")
    duration_pairs = (
        ("Intro media", intro_media_min_seconds, intro_media_max_seconds),
        ("First 2 minutes", hook_media_min_seconds, hook_media_max_seconds),
        ("General media", general_media_min_seconds, general_media_max_seconds),
    )
    for label, minimum, maximum in duration_pairs:
        if minimum <= 0 or maximum <= 0 or maximum < minimum:
            raise HTTPException(400, f"{label}: maximum duration must be equal to or greater than minimum duration")
        if minimum > 10 or maximum > 15:
            raise HTTPException(400, f"{label}: duration is outside the supported range")
    if pexels_selection_mode not in {"first_result", "best_match", "top_n_random", "popularity", "best_match_variety"}:
        raise HTTPException(400, "Invalid Pexels selection mode")
    if not 1 <= int(pexels_search_pool) <= 40:
        raise HTTPException(400, "Pexels search pool must be between 1 and 40")
    if media_mix not in {"video_first", "alternate", "video_heavy", "image_heavy", "random"}:
        raise HTTPException(400, "Invalid media mix")
    if not (use_image_backgrounds == "on" or use_video_backgrounds == "on"):
        raise HTTPException(400, "Enable at least one stock-media type")
    if motion_style not in {"very_slow", "cinematic", "dynamic"}:
        raise HTTPException(400, "Invalid motion style")
    if transition_mode not in {"auto_editorial", "cuts_only", "off", "scene_plan"}:
        raise HTTPException(400, "Invalid transition mode")
    if transition_preset not in {"documentary", "cinematic", "smooth", "energetic", "minimal", "storytelling"}:
        raise HTTPException(400, "Invalid transition preset")
    if ffmpeg_preset not in {"ultrafast", "veryfast", "faster", "fast", "medium"}:
        raise HTTPException(400, "Invalid encoder preset")
    if caption_position not in {"top", "center", "bottom"}:
        raise HTTPException(400, "Invalid caption position")
    if voice_processing_preset not in {"off", "natural", "clear", "strong"}:
        raise HTTPException(400, "Invalid voice-processing preset")
    if music_ducking_strength not in {"light", "medium", "strong"}:
        raise HTTPException(400, "Invalid music-ducking strength")
    if lut_pack not in LUT_PACKS or lut_preset not in LUT_PACKS[lut_pack]["presets"]:
        raise HTTPException(400, "Invalid LUT pack or preset")
    if preset_music_ducking_strength not in {"light", "medium", "strong"}:
        raise HTTPException(400, "Invalid preset music ducking strength")
    if preset_sfx_amount not in {"low", "medium", "high"}:
        raise HTTPException(400, "Invalid SFX amount")
    try:
        playlist_raw = json.loads(preset_music_playlist_json or "[]")
        allowed_categories_raw = json.loads(preset_music_ai_allowed_categories_json or "[]")
    except Exception as exc:
        raise HTTPException(400, "Invalid preset music configuration") from exc
    if not isinstance(playlist_raw, list) or not isinstance(allowed_categories_raw, list):
        raise HTTPException(400, "Invalid preset music configuration")
    valid_music_ids = {str(row.get("asset_id") or "") for row in list_assets("music")}
    playlist: list[dict[str, Any]] = []
    for index, raw in enumerate(playlist_raw[:100]):
        if not isinstance(raw, dict):
            continue
        asset_id = str(raw.get("asset_id") or "")
        if asset_id not in valid_music_ids:
            continue
        role = str(raw.get("role") or "general").lower()
        if role not in {"intro", "main", "outro", "general"}:
            role = "general"
        playlist.append({"asset_id": asset_id, "order": index + 1, "role": role})
    allowed_categories = [re.sub(r"[^A-Za-z0-9 _-]+", "", str(x)).strip()[:80] for x in allowed_categories_raw if re.sub(r"[^A-Za-z0-9 _-]+", "", str(x)).strip()][:50]

    colors = {
        "caption_text_color": caption_text_color,
        "caption_outline_color": caption_outline_color,
        "caption_highlight_color": caption_highlight_color,
        "caption_background_color": caption_background_color,
    }
    for key, value in colors.items():
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", value or ""):
            raise HTTPException(400, f"Invalid color for {key}")
        set_setting(key, value.lower())

    values = {
        "quality": quality,
        "default_scene_preset": default_scene_preset,
        "custom_aspect_ratio": custom_aspect_ratio,
        "timing_mode": timing_mode,
        "max_scene_seconds": str(max(0.25, min(15.0, general_media_max_seconds))),
        "min_scene_seconds": str(max(0.25, min(10.0, general_media_min_seconds))),
        "intro_media_min_seconds": str(max(0.25, min(10.0, intro_media_min_seconds))),
        "intro_media_max_seconds": str(max(0.25, min(15.0, intro_media_max_seconds))),
        "hook_media_min_seconds": str(max(0.25, min(10.0, hook_media_min_seconds))),
        "hook_media_max_seconds": str(max(0.25, min(15.0, hook_media_max_seconds))),
        "general_media_min_seconds": str(max(0.25, min(10.0, general_media_min_seconds))),
        "general_media_max_seconds": str(max(0.25, min(15.0, general_media_max_seconds))),
        "narration_start_mode": narration_start_mode,
        "narration_start_manual_seconds": str(max(0.0, min(3600.0, narration_start_manual_seconds))),
        "pexels_enabled": "1" if pexels_enabled == "on" else "0",
        "pexels_selection_mode": pexels_selection_mode,
        "pexels_search_pool": str(max(1, min(40, int(pexels_search_pool)))),
        "pexels_avoid_used_media": "1" if pexels_avoid_used_media == "on" else "0",
        "pexels_avoid_same_creator": "1" if pexels_avoid_same_creator == "on" else "0",
        "pexels_prefer_landscape": "1" if pexels_prefer_landscape == "on" else "0",
        "pexels_prefer_video": "1" if pexels_prefer_video == "on" else "0",
        "media_mix": media_mix,
        "use_image_backgrounds": "1" if use_image_backgrounds == "on" else "0",
        "use_video_backgrounds": "1" if use_video_backgrounds == "on" else "0",
        "avoid_duplicate_media": "1" if pexels_avoid_used_media == "on" else "0",
        "scene_review_required": "1" if scene_review_required == "on" else "0",
        "motion_style": motion_style,
        "transition_engine_enabled": "1" if transition_engine_enabled == "on" else "0",
        "transition_mode": transition_mode,
        "transition_preset": transition_preset,
        "transition_intensity": str(max(0, min(100, transition_intensity))),
        "transition_duration": str(max(0.12, min(0.50, transition_duration))),
        "lut_enabled": "1" if lut_enabled == "on" else "0",
        "lut_pack": re.sub(r"[^a-zA-Z0-9_-]", "", lut_pack)[:80] or "cinematic",
        "lut_preset": re.sub(r"[^a-zA-Z0-9_-]", "", lut_preset)[:80] or "filmic",
        "lut_strength": str(max(0, min(100, lut_strength))),
        "lut_normalize_enabled": "1" if lut_normalize_enabled == "on" else "0",
        "output_fps": str(max(20, min(60, output_fps))),
        "quality_crf": str(max(16, min(30, quality_crf))),
        "ffmpeg_preset": ffmpeg_preset,
        "post_render_compress_enabled": "1" if post_render_compress_enabled == "on" else "0",
        "drive_upload_enabled": "1" if drive_upload_enabled == "on" else "0",
        "captions_enabled": "1" if captions_enabled == "on" else "0",
        "caption_words": str(max(1, min(5, caption_words))),
        "caption_style": caption_style,
        "caption_karaoke_enabled": "1" if caption_karaoke_enabled == "on" else "0",
        "caption_font_size": str(max(18, min(120, caption_font_size))),
        "caption_font": caption_font,
        "caption_background_opacity": str(max(0, min(100, caption_background_opacity))),
        "caption_position": caption_position,
        "caption_outline_width": str(max(0, min(8, caption_outline_width))),
        "caption_shadow_enabled": "1" if caption_shadow_enabled == "on" else "0",
        "voice_volume_percent": str(max(0, min(150, voice_volume_percent))),
        "voice_processing_preset": voice_processing_preset,
        "music_volume_percent": str(max(0, min(100, music_volume_percent))),
        "music_ducking_enabled": "1" if music_ducking_enabled == "on" else "0",
        "music_ducking_strength": music_ducking_strength,
        "music_normalize": "1" if music_normalize == "on" else "0",
        "music_loop_if_short": "1" if music_loop_if_short == "on" else "0",
        "music_loop_crossfade_seconds": str(max(0.05, min(1.5, music_loop_crossfade_seconds))),
        "music_fade_in_seconds": str(max(0.0, min(10.0, music_fade_in_seconds))),
        "music_fade_out_seconds": str(max(0.0, min(10.0, music_fade_out_seconds))),
        "quota_limit": str(max(0, quota_limit)),
        "allowed_storage_gb": str(max(0.0, allowed_storage_gb)),
        "retention_enabled": "1" if retention_enabled == "on" else "0",
        "retention_free_gb": str(max(1.0, retention_free_gb)),
        "announcement": announcement.strip()[:1000],
    }
    for key, value in values.items():
        set_setting(key, value)
    requested_auto_clean = preset_auto_clean_project_after_drive == "on"
    if not requested_auto_clean and preset_auto_clean_disable_confirmation.strip() != "CLEAN OFF":
        raise HTTPException(400, "Type CLEAN OFF to disable Auto Clean After Drive")
    preset_config = {
        "intro_enabled": preset_intro_enabled == "on",
        "intro_duration_seconds": max(1, min(600, int(preset_intro_duration_seconds))),
        "music_enabled": preset_music_enabled == "on",
        "music_playlist": playlist,
        "music_ai_select_from_library": preset_music_ai_select == "on",
        "music_ai_allowed_categories": allowed_categories,
        "music_volume_percent": max(0, min(100, int(preset_music_volume_percent))),
        "music_ducking_enabled": preset_music_ducking_enabled == "on",
        "music_ducking_strength": preset_music_ducking_strength,
        "music_normalize": preset_music_normalize == "on",
        "music_playlist_loop": preset_music_playlist_loop == "on",
        "music_track_crossfade_seconds": max(0.0, min(8.0, float(preset_music_crossfade_seconds))),
        "music_use_track1_for_intro": preset_music_use_track1_for_intro == "on",
        "music_allow_intro_track_in_main": preset_music_allow_intro_track_in_main == "on",
        "sfx_enabled": preset_sfx_enabled == "on",
        "environment_sfx_enabled": preset_environment_sfx_enabled == "on",
        "transition_sfx_library_enabled": preset_transition_sfx_enabled == "on",
        "sfx_amount": preset_sfx_amount,
        "environment_sfx_volume_percent": max(0, min(60, int(preset_environment_sfx_volume_percent))),
        "transition_sfx_volume_percent": max(0, min(60, int(preset_transition_sfx_volume_percent))),
        "auto_clean_project_after_drive": requested_auto_clean,
        "auto_clean_disable_confirmed": not requested_auto_clean,
        "first_two_minute_visual_audit_enabled": preset_first_two_minute_visual_audit_enabled == "on",
    }
    save_preset_media_config(default_scene_preset, preset_config)
    editable_profile = {
            "quality": quality, "aspect_ratio": custom_aspect_ratio, "fps": max(20, min(60, output_fps)),
            "captions_enabled": captions_enabled == "on", "caption_style": caption_style,
            "caption_position": caption_position, "caption_karaoke_enabled": caption_karaoke_enabled == "on",
            "caption_words": max(1, min(5, caption_words)), "caption_font_size": max(18, min(120, caption_font_size)),
            "caption_font": caption_font, "caption_text_color": caption_text_color.lower(), "caption_outline_color": caption_outline_color.lower(),
            "caption_highlight_color": caption_highlight_color.lower(), "caption_background_color": caption_background_color.lower(),
            "caption_background_opacity": max(0, min(100, caption_background_opacity)),
            "caption_outline_width": max(0, min(8, caption_outline_width)), "caption_shadow_enabled": caption_shadow_enabled == "on",
            "timing_mode": timing_mode, "max_scene_seconds": max(0.25, min(15.0, general_media_max_seconds)),
            "min_scene_seconds": max(0.25, min(10.0, general_media_min_seconds)),
            "intro_media_min_seconds": max(0.25, min(10.0, intro_media_min_seconds)),
            "intro_media_max_seconds": max(0.25, min(15.0, intro_media_max_seconds)),
            "hook_media_min_seconds": max(0.25, min(10.0, hook_media_min_seconds)),
            "hook_media_max_seconds": max(0.25, min(15.0, hook_media_max_seconds)),
            "general_media_min_seconds": max(0.25, min(10.0, general_media_min_seconds)),
            "general_media_max_seconds": max(0.25, min(15.0, general_media_max_seconds)),
            "narration_start_mode": narration_start_mode,
            "narration_start_manual_seconds": max(0.0, min(3600.0, narration_start_manual_seconds)),
            "pexels_enabled": pexels_enabled == "on",
            "pexels_selection_mode": pexels_selection_mode, "pexels_search_pool": max(1, min(40, int(pexels_search_pool))),
            "pexels_avoid_used_media": pexels_avoid_used_media == "on",
            "pexels_avoid_same_creator": pexels_avoid_same_creator == "on",
            "pexels_prefer_landscape": pexels_prefer_landscape == "on",
            "pexels_prefer_video": pexels_prefer_video == "on",
            "media_mix": media_mix,
            "use_images": use_image_backgrounds == "on", "use_videos": use_video_backgrounds == "on",
            "motion_style": motion_style, "transition_engine_enabled": transition_engine_enabled == "on", "transition_mode": transition_mode, "transition_preset": transition_preset,
            "transition_intensity": max(0, min(100, transition_intensity)), "transition_duration": max(0.12, min(0.50, transition_duration)), "lut_enabled": lut_enabled == "on",
            "lut_pack": lut_pack, "lut_preset": lut_preset, "lut_strength": max(0, min(100, lut_strength)),
            "lut_normalize_enabled": lut_normalize_enabled == "on", "quality_crf": max(16, min(30, quality_crf)),
            "ffmpeg_preset": ffmpeg_preset, "compress": post_render_compress_enabled == "on",
            "drive_upload_enabled": drive_upload_enabled == "on",
            "voice_volume_percent": max(0, min(150, voice_volume_percent)), "voice_processing_preset": voice_processing_preset,
    }
    if is_user_scene_preset(default_scene_preset):
        save_user_scene_preset_profile(default_scene_preset, editable_profile)
    elif default_scene_preset in SCENE_PRESETS:
        save_builtin_scene_preset_override(default_scene_preset, editable_profile)

    audit_event("settings_saved", "Website settings updated", {
        "quality": quality,
        "caption_words": values["caption_words"],
        "caption_style": caption_style,
        "caption_font": caption_font,
        "retention_enabled": values["retention_enabled"],
        "retention_free_gb": values["retention_free_gb"],
    })
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    require_admin(request)
    return RedirectResponse("/admin/storage", status_code=303)



@app.post("/admin/system-settings")
def save_admin_system_settings(
    request: Request,
    quota_limit: int = Form(0),
    allowed_storage_gb: float = Form(0),
    retention_enabled: str | None = Form(None),
    retention_free_gb: float = Form(20),
):
    if not bool(request.session.get("is_admin")):
        raise HTTPException(403, "Administrator access required")
    set_setting("quota_limit", str(max(0, quota_limit)))
    set_setting("allowed_storage_gb", str(max(0.0, allowed_storage_gb)))
    set_setting("retention_enabled", "1" if retention_enabled == "on" else "0")
    set_setting("retention_free_gb", str(max(1.0, retention_free_gb)))
    audit_event("admin_system_settings_saved", "Administrator updated limits and retention", {
        "quota_limit": max(0, quota_limit),
        "allowed_storage_gb": max(0.0, allowed_storage_gb),
        "retention_enabled": retention_enabled == "on",
        "retention_free_gb": max(1.0, retention_free_gb),
    })
    return RedirectResponse("/admin?saved=1", status_code=303)


@app.get("/user-management", response_class=HTMLResponse)
def user_management_page(request: Request):
    require_admin(request)
    jobs = all_jobs_with_owners(2000)
    owners = ownership_map()
    counts: dict[str, int] = {}
    for owner in owners.values():
        counts[owner.casefold()] = counts.get(owner.casefold(), 0) + 1
    with db() as conn:
        rows = [dict(row) for row in conn.execute(
            """SELECT username,is_active,is_admin,created_at,daily_render_limit,
                      subscription_plan_days,subscription_start_at,subscription_end_at,last_login_at,
                      app_access
               FROM users ORDER BY is_admin DESC, username COLLATE NOCASE"""
        ).fetchall()]
    for user in rows:
        user.update(user_account(str(user["username"])) or {})
        user["today_used"] = completed_today_count(str(user["username"]), jobs_for_user(str(user["username"]), 2000))
        user["job_count"] = counts.get(str(user["username"]).casefold(), 0)
    return templates.TemplateResponse("user_management.html", {
        "request": request, "page": "user_management",
        "username": request.session.get("username", "team"),
        "users": rows, "message": request.query_params.get("message", ""),
        "error": request.query_params.get("error", ""),
    })


@app.post("/user-management")
def user_management_action(
    request: Request, action: str = Form(...), username: str = Form(""),
    secret_key: str = Form(""), daily_render_limit: int = Form(0),
    plan_days: int = Form(30), app_access: str = Form("scene_builder"),
):
    require_admin(request)
    clean = username.strip()
    actor = str(request.session.get("username") or "")
    if app_access not in {"scene_builder", "auto_caption", "lum", "both", "all"}:
        raise HTTPException(400, "Invalid app access")
    if plan_days not in {7, 15, 30}:
        raise HTTPException(400, "Plan must be 7, 15, or 30 days")
    now = datetime.now(timezone.utc)
    try:
        with db() as conn:
            if action == "create":
                if not re.fullmatch(r"[A-Za-z0-9_.-]{3,40}", clean):
                    raise HTTPException(400, "Invalid username")
                if len(secret_key) < 8:
                    raise HTTPException(400, "Secret key must be at least 8 characters")
                end = now + timedelta(days=plan_days)
                conn.execute("""INSERT INTO users(username,secret_hash,is_active,is_admin,created_at,daily_render_limit,
                              subscription_plan_days,subscription_start_at,subscription_end_at,last_login_at,app_access)
                              VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                             (clean, hash_secret(secret_key), 1, 0, now_iso(), max(0,daily_render_limit),
                              plan_days, now.isoformat(), end.isoformat(), "", app_access))
                message=f"User {clean} created"
            else:
                row=conn.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE",(clean,)).fetchone()
                if not row: raise HTTPException(404,"User not found")
                target_admin=bool(row["is_admin"])
                if action=="save":
                    effective_access = "both" if target_admin else app_access
                    conn.execute("UPDATE users SET daily_render_limit=?,app_access=? WHERE username=? COLLATE NOCASE",(max(0,daily_render_limit),effective_access,clean))
                    message=f"Quota and app access updated for {clean}"
                elif action=="extend":
                    current_end=parse_iso(row["subscription_end_at"])
                    base=current_end if current_end and current_end>now else now
                    end=base+timedelta(days=plan_days)
                    start=row["subscription_start_at"] or now.isoformat()
                    conn.execute("""UPDATE users SET subscription_plan_days=?,subscription_start_at=?,subscription_end_at=?,is_active=1
                                    WHERE username=? COLLATE NOCASE""",(plan_days,start,end.isoformat(),clean))
                    message=f"Subscription extended for {clean}"
                elif action=="reset_password":
                    if len(secret_key)<8: raise HTTPException(400,"Secret key must be at least 8 characters")
                    conn.execute("UPDATE users SET secret_hash=? WHERE username=? COLLATE NOCASE",(hash_secret(secret_key),clean))
                    message=f"Secret key reset for {clean}"
                elif action=="enable":
                    conn.execute("UPDATE users SET is_active=1 WHERE username=? COLLATE NOCASE",(clean,))
                    message=f"User {clean} enabled"
                elif action=="disable":
                    if clean.casefold()==actor.casefold(): raise HTTPException(400,"You cannot disable your own account")
                    conn.execute("UPDATE users SET is_active=0 WHERE username=? COLLATE NOCASE",(clean,))
                    message=f"User {clean} disabled"
                elif action=="delete":
                    if clean.casefold()==actor.casefold() or target_admin: raise HTTPException(400,"Administrator accounts cannot be deleted")
                    owned=conn.execute("SELECT COUNT(*) AS c FROM job_ownership WHERE owner_username=? COLLATE NOCASE",(clean,)).fetchone()["c"]
                    if owned: raise HTTPException(409,"User has jobs; disable the account instead")
                    conn.execute("DELETE FROM users WHERE username=? COLLATE NOCASE",(clean,))
                    message=f"User {clean} deleted"
                else: raise HTTPException(400,"Unknown action")
        audit_event("user_management",message,{"actor":actor,"target":clean,"action":action})
        return RedirectResponse(f"/user-management?message={requests.utils.quote(message)}",status_code=303)
    except sqlite3.IntegrityError:
        return RedirectResponse("/user-management?error=Username+already+exists",status_code=303)


# SCRIPT_WORK_UI_PREVIEW_V1_ROUTE
@app.get("/script-work", response_class=HTMLResponse)
def script_work_page(request: Request):
    require_login(request)
    return templates.TemplateResponse(
        "script_work.html",
        {"request": request, "page": "script_work", "username": request.session.get("username", "team")},
    )


@app.get("/local-upload-manager", response_class=HTMLResponse)
def local_upload_manager_page(request: Request):
    require_local_upload_manager_access(request)
    return templates.TemplateResponse(
        "local_upload_manager.html",
        {
            "request": request,
            "page": "local_upload_manager",
            "username": request.session.get("username", "team"),
        },
    )


@app.get("/internal/auto-caption-auth")
def auto_caption_auth(request: Request):
    if not logged_in(request):
        return Response(status_code=401)
    account = user_account(str(request.session.get("username") or ""))
    if not account or not int(account.get("is_active") or 0) or account.get("subscription_expired"):
        return Response(status_code=401)
    access = str(account.get("app_access") or "scene_builder")
    if not app_access_allows(account, "auto_caption"):
        return Response(status_code=403)
    return Response(status_code=204, headers={
        "X-Auth-User": str(account.get("username") or ""),
        "X-App-Access": access,
        "X-Is-Admin": "1" if bool(account.get("is_admin")) else "0",
    })


# This endpoint is for Nginx's INTERNAL auth_request subrequests only.
# The Scene session cookie remains the single login source of truth. Nginx
# overwrites identity headers from these response headers, never from clients.
SCENE_SSO_APP_IDS = frozenset({
    "short-maker", "short-stream", "self-media-library", "video-merger",
    "voice-studio", "auto-caption-studio", "local-upload-manager",
})


@app.get("/internal/workspace-auth")
def workspace_session_auth(request: Request, app: str = ""):
    """Validate one existing Scene session for one named independent app.

    Non-admin accounts retain the historical Caption/LUM grant boundaries;
    applications without a defined non-admin role remain administrator-only.
    """
    if app not in SCENE_SSO_APP_IDS:
        return Response(status_code=403)
    if not logged_in(request):
        return Response(status_code=401)
    account = user_account(str(request.session.get("username") or ""))
    if not account or not int(account.get("is_active") or 0) or account.get("subscription_expired"):
        return Response(status_code=401)
    admin = bool(account.get("is_admin"))
    if not admin:
        if app == "auto-caption-studio":
            permitted = app_access_allows(account, "auto_caption")
        elif app == "local-upload-manager":
            permitted = app_access_allows(account, "lum")
        else:
            permitted = False
        if not permitted:
            return Response(status_code=403)
    # The Caption backend interprets access="both" as administrator. Never
    # pass a broad account grant to it for a non-admin Scene user.
    access = "both" if admin else ("auto_caption" if app == "auto-caption-studio" else "lum")
    return Response(status_code=204, headers={
        "X-Auth-User": str(account.get("username") or ""),
        "X-App-Access": access,
        "X-Is-Admin": "1" if admin else "0",
        "Cache-Control": "no-store",
    })


@app.get("/internal/local-upload-manager-auth")
def local_upload_manager_auth(request: Request):
    """Nginx auth_request bridge for the independently served LUM app."""
    if not logged_in(request):
        return Response(status_code=401)
    account = user_account(str(request.session.get("username") or ""))
    if not account or not int(account.get("is_active") or 0) or account.get("subscription_expired"):
        return Response(status_code=401)
    if not app_access_allows(account, "lum"):
        return Response(status_code=403)
    access = str(account.get("app_access") or "scene_builder")
    return Response(status_code=204, headers={
        "X-Auth-User": str(account.get("username") or ""),
        "X-App-Access": access,
        "X-Is-Admin": "1" if bool(account.get("is_admin")) else "0",
    })


def require_storage_csrf(request: Request) -> None:
    supplied = request.headers.get("X-Storage-CSRF", "")
    expected = str(request.session.get("storage_csrf") or "")
    if not expected or not hmac.compare_digest(supplied, expected):
        raise HTTPException(403, "Invalid storage action token")


@app.get("/admin/storage", response_class=HTMLResponse)
def storage_manager_page(request: Request):
    require_admin(request)
    token = secrets.token_urlsafe(32)
    request.session["storage_csrf"] = token
    health = get_health()
    return templates.TemplateResponse(
        "storage_manager.html",
        {
            "request": request,
            "page": "storage_manager",
            "username": request.session.get("username", "team"),
            "storage_csrf": token,
            "drive_connected": bool(health.get("drive_upload_enabled")) and bool(health.get("ok", True)),
            "pexels_key_count": int(health.get("pexels_key_count") or 0),
        },
    )


@app.get("/api/admin/storage/pexels-keys")
def storage_pexels_keys(request: Request):
    require_admin(request)
    health = get_health()
    managed = masked_managed_pexels_keys()
    total = int(health.get("pexels_key_count") or len(managed))
    return {"ok": True, "keys": managed, "managed_count": len(managed), "total_key_count": total, "environment_key_count": max(0, total-len(managed)), "dynamic_reload": True}


@app.post("/api/admin/storage/pexels-keys")
async def storage_pexels_keys_update(request: Request):
    require_admin(request); require_storage_csrf(request)
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(400, "Invalid Pexels key request") from exc
    action = str(payload.get("action") or "").lower()
    keys = managed_pexels_keys()
    if action == "add":
        key = str(payload.get("key") or "").strip()
        if len(key) < 10 or len(key) > 512 or re.search(r"\s", key):
            raise HTTPException(400, "Invalid Pexels API key")
        if key not in keys:
            keys.append(key)
        save_managed_pexels_keys(keys)
        audit_event("pexels_key_added", "Administrator added a masked supplemental Pexels key", {"key_id": hashlib.sha256(key.encode()).hexdigest()[:16]})
    elif action == "remove":
        key_id = str(payload.get("key_id") or "")
        matched = next((key for key in keys if hashlib.sha256(key.encode()).hexdigest()[:16] == key_id), None)
        if not matched:
            raise HTTPException(404, "Managed Pexels key not found")
        keys = [key for key in keys if key != matched]
        save_managed_pexels_keys(keys)
        audit_event("pexels_key_removed", "Administrator removed a masked supplemental Pexels key", {"key_id": key_id})
    else:
        raise HTTPException(400, "Action must be add or remove")
    # Worker reads the protected supplemental key file dynamically; no container
    # recreation or render interruption is required.
    health = get_health()
    managed = masked_managed_pexels_keys()
    return {"ok": True, "keys": managed, "managed_count": len(managed), "total_key_count": int(health.get("pexels_key_count") or len(managed)), "dynamic_reload": True}


@app.get("/api/admin/storage/capabilities")
def storage_capabilities(request: Request):
    """Administrator-only, authenticated backend capability negotiation.

    A missing/older manager fails closed in the UI; no mutation authorization
    is implied by availability of the read-only VPS dashboard.
    """
    require_admin(request)
    return storage_manager_request("GET", "/v1/capabilities", timeout=5)


@app.get("/api/admin/storage/metrics")
def storage_metrics(request: Request):
    require_admin(request)
    return storage_manager_request("GET", "/v1/metrics", timeout=30)


# STORAGE_MANAGER_SERVICE_ACTIVATION_WEB_V6
@app.post("/api/admin/storage/services/{service_name}/start")
def storage_service_start_api(request: Request, service_name: str):
    require_admin(request)
    require_storage_csrf(request)
    result = storage_manager_request(
        "POST", "/v1/services/start", payload={"service": service_name}, timeout=120,
    )
    audit_event(
        "storage_service_start",
        f"Administrator requested service activation: {service_name}",
        {"service": result.get("service", service_name), "state": result.get("state", ""), "changed": bool(result.get("changed"))},
    )
    return result


@app.get("/api/admin/storage/snapshot")
def storage_snapshot_api(request: Request):
    require_admin(request)
    return storage_manager_request("GET", "/v1/snapshot", timeout=180)


@app.get("/api/admin/storage/history")
def storage_history_api(request: Request):
    require_admin(request)
    return storage_manager_request("GET", "/v1/history", timeout=30)


@app.post("/api/admin/storage/scan")
def storage_scan_api(request: Request):
    require_admin(request)
    require_storage_csrf(request)
    result = storage_manager_request("POST", "/v1/scan", payload={}, timeout=1800)
    audit_event("storage_scan", "Administrator refreshed reclaimable storage", {
        "reclaimable_bytes": result.get("reclaimable_bytes", 0),
    })
    return result


@app.post("/api/admin/storage/cleanup")
async def storage_cleanup_api(request: Request):
    require_admin(request)
    require_storage_csrf(request)
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(400, "Invalid cleanup request") from exc
    result = storage_manager_request("POST", "/v1/cleanup", payload=payload, timeout=7200)
    audit_event("storage_cleanup", "Administrator ran verified storage cleanup", result)
    return result


@app.post("/api/admin/storage/policy")
async def storage_policy_api(request: Request):
    require_admin(request)
    require_storage_csrf(request)
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(400, "Invalid policy request") from exc
    result = storage_manager_request("POST", "/v1/policy", payload=payload, timeout=30)
    audit_event("storage_policy", "Administrator updated the Storage Manager policy", result.get("policy", {}))
    return result


@app.get("/api/admin/storage/full-clean/status")
def storage_full_clean_status_api(request: Request):
    require_admin(request)
    return storage_manager_request("GET", "/v1/full-production-clean/status", timeout=30)


@app.post("/api/admin/storage/full-clean")
async def storage_full_clean_api(request: Request):
    require_admin(request)
    require_storage_csrf(request)
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(400, "Invalid full production clean request") from exc
    result = storage_manager_request("POST", "/v1/full-production-clean", payload=payload, timeout=30)
    audit_event("full_production_clean", "Administrator started full production clean", {
        "request_id": result.get("request_id", ""),
        "status": result.get("status", ""),
    })
    return result


@app.get("/auto-caption/live", response_class=HTMLResponse)
def auto_caption_live(request: Request):
    account = require_auto_caption_access(request)
    return templates.TemplateResponse(
        "auto_caption_live.html",
        {
            "request": request,
            "page": "auto_caption_live",
            "username": request.session.get("username", "team"),
            "account": account,
            "show_all_users": bool(account.get("is_admin")),
        },
    )

# VIDEO_STUDIO_SUBTABS_V1: Scene owns the frame; each app remains independently routed.
@app.get("/video-studio/{studio_app}", response_class=HTMLResponse)
def video_studio_embedded_app(request: Request, studio_app: str):
    allowed = {
        "short-maker": ("Short Maker", "/short-maker/"),
        "video-merger": ("Video Merger", "/video-merger/"),
    }
    if studio_app not in allowed:
        raise HTTPException(404, "Unknown Video Studio app")
    require_login(request)
    require_admin(request)
    title, url = allowed[studio_app]
    return templates.TemplateResponse(
        "video_studio_embed.html",
        {"request": request, "page": "video_studio_embedded",
         "studio_app": studio_app, "studio_title": title,
         "studio_url": url, "username": request.session.get("username", "team")},
    )


@app.get("/auto-caption/studio", response_class=HTMLResponse)
def auto_caption_studio(request: Request):
    account = require_auto_caption_access(request)
    return templates.TemplateResponse(
        "acs_studio_shell.html",
        {"request": request, "page": "auto_caption_studio", "username": request.session.get("username", "team"),
         "account": account, "show_all_users": bool(account.get("is_admin"))},
    )


@app.get("/auto-caption/admin")
def acs_admin(request: Request):
    require_auto_caption_access(request)
    return RedirectResponse("/auto-caption/live", status_code=303)


@app.get("/health")
def web_health():
    if RUNTIME_CANDIDATE:
        return {"ok": True, "worker": "disabled", "mode": "shadow-read-only", "version": app.version}
    return {"ok": True, "worker": get_health(), "version": "7.7.11-professional-editor-v17.5d-r6-internal-audio-studio-v1"}


@app.get("/internal/runtime-ready", include_in_schema=False)
def runtime_ready(request: Request):
    if not RUNTIME_CANDIDATE:
        raise HTTPException(404)
    expected = os.getenv("SCENE_RUNTIME_PROBE_TOKEN", "")
    supplied = request.headers.get("X-Runtime-Probe-Token", "")
    if not expected or not hmac.compare_digest(supplied, expected):
        raise HTTPException(403, "Forbidden")
    return {"ok": True, "mode": "shadow-read-only", "side_effect_workers": "disabled"}


# ATOMIC_FLOW_V1_REGISTER
from atomic_flow import register_atomic_flow
register_atomic_flow(app, templates)



# AUDIO_ENGINE_AUTH_BRIDGE_V1
@app.get("/internal/audio-engine-auth")
def audio_engine_auth(request: Request):
    """Nginx auth_request bridge for the standalone Audio Engine."""
    if not logged_in(request):
        return Response(status_code=401)
    account = user_account(str(request.session.get("username") or ""))
    if not account or not int(account.get("is_active") or 0) or account.get("subscription_expired"):
        return Response(status_code=401)
    return Response(status_code=204, headers={
        "X-Auth-User": str(account.get("username") or ""),
        "X-Is-Admin": "1" if bool(account.get("is_admin")) else "0",
    })

# AUDIO_ENGINE_HOST_SHELL_V2
@app.get("/audio-engine", include_in_schema=False)
def audio_engine_shell_redirect(request: Request):
    require_login(request)
    return RedirectResponse("/audio-engine/", status_code=308)

@app.get("/audio-engine/", response_class=HTMLResponse)
def audio_engine_shell_page(request: Request):
    require_login(request)
    return templates.TemplateResponse("audio_engine.html", {
        "request": request,
        "page": "audio_engine",
        "username": request.session.get("username", "team"),
    })


# VPS1_OMNIVOICE_VOICE_STUDIO_PROXY_V1
from fastapi import Request as _VSRequest
from starlette.responses import Response as _VSResponse
import requests as _vs_requests
import asyncio as _vs_asyncio

_VS_UPSTREAM="http://127.0.0.1:8130"

@app.get("/voice-studio", response_class=HTMLResponse)
async def vps1_voice_studio_host(request:_VSRequest):
    return templates.TemplateResponse("voice_studio_host.html",{
        "request":request,
        "page":"voice_studio",
        "username":str(request.session.get("username") or "team"),
    })

async def _vps1_voice_studio_proxy(request:_VSRequest,upstream_path:str):
    body=await request.body()
    url=_VS_UPSTREAM+"/"+upstream_path.lstrip("/")
    headers={}
    ct=request.headers.get("content-type")
    if ct:
        headers["content-type"]=ct
    def _do():
        return _vs_requests.request(
            request.method,url,
            params=list(request.query_params.multi_items()),
            data=body if body else None,
            headers=headers,
            timeout=1900,
            allow_redirects=False,
        )
    r=await _vs_asyncio.to_thread(_do)
    content=r.content
    rct=r.headers.get("content-type","")
    if upstream_path in {"","app"} and "text/html" in rct.lower():
        content=content.replace(b"/tts/",b"/voice-studio/service/tts/")
    out={k:r.headers[k] for k in ("content-type","content-disposition","cache-control") if k in r.headers}
    return _VSResponse(content=content,status_code=r.status_code,headers=out)

@app.api_route("/voice-studio/app",methods=["GET"])
async def vps1_voice_studio_app_proxy(request:_VSRequest):
    return await _vps1_voice_studio_proxy(request,"app")

@app.api_route("/voice-studio/service/{upstream_path:path}",methods=["GET","POST","PUT","PATCH","DELETE","OPTIONS"])
async def vps1_voice_studio_service_proxy(request:_VSRequest,upstream_path:str):
    return await _vps1_voice_studio_proxy(request,upstream_path)


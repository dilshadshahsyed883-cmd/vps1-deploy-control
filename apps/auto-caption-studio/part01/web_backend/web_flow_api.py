from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from typing import Any

import requests
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from .auth import Principal, get_current_principal, job_for_principal
from .config import app_root, projects_root
from .database import connect, initialize_storage, now_iso
from .job_states import HELD, QUEUED
from .drive_uploader import _load_runtime as _load_drive_runtime
from .queue_store import (
    create_job_record,
    get_job,
    list_jobs,
    request_cancel,
    retry_job,
    transition_job_in_db,
)
from .settings_bridge import defaults as project_defaults, normalize as normalize_project_settings, save_settings
from .shared_fg import shared_fg_root
from .shared_pexels_library import CONFIG_FILE as PEXELS_CONFIG_FILE, KEYS_FILE as PEXELS_KEYS_FILE, SHARED_LIBRARY as PEXELS_LIBRARY, next_server_pexels_key, server_pexels_keys
from .font_fallback import SCRIPT_FONTS, choose_font_family, detect_script
from .web_flow_runtime import queue_runtime_state
from .drive_batch_store import ensure_drive_batch_schema, deferred_batch_summaries

router = APIRouter(prefix="/api/web-flow", tags=["acs-new-web-flow"])

STUDIO_TEMPLATE = Path(__file__).with_name("acs_studio.html")


@router.get("/studio", response_class=HTMLResponse)
def web_flow_studio(_principal: Principal = Depends(get_current_principal)) -> HTMLResponse:
    """Serve the dedicated New Web Flow UI from the ACS service itself.

    Public access is intentionally through the existing authenticated nginx path:
    /auto-caption/api/web-flow/studio -> 127.0.0.1:8100/api/web-flow/studio.
    No Scene Builder web code or shared template is required.
    """
    if not STUDIO_TEMPLATE.is_file():
        raise HTTPException(status_code=503, detail="ACS Studio UI is not installed")
    return HTMLResponse(
        STUDIO_TEMPLATE.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store, max-age=0"},
    )

AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
MUSIC_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
WEB_FLOW_VERSION = 1
DOCUMENTARY_SCROLL_PROFILE_VERSION = 5

DATA_ROOT = app_root() / "data" / "web_flow"
CONFIG_ROOT = app_root() / "data" / "config"
FG_CONFIG = CONFIG_ROOT / "web_flow_fg_libraries.json"
VOICE_CONFIG = CONFIG_ROOT / "web_flow_voice_profiles.json"
GLOBAL_CONFIG = CONFIG_ROOT / "web_flow_global.json"
MUSIC_CONFIG = CONFIG_ROOT / "web_flow_music.json"
PRESETS_ROOT = DATA_ROOT / "presets"
USERS_ROOT = DATA_ROOT / "users"
AUDIT_FILE = app_root() / "logs" / "web_flow_audit.jsonl"
DEFAULT_FG_ROOT = app_root() / "data" / "fg_libraries"
DEFAULT_MUSIC_ROOT = app_root() / "data" / "music_library"
ALGROW_CONFIG = Path(os.environ.get("ALGROW_SIDECAR_CONFIG", "/home/ubuntu/apps/algrow_voice_bridge/shared/config.json"))


PREVIEW_REFERENCE_ROOT = DATA_ROOT / "protected_preview_reference"
PREVIEW_REFERENCE_SCRIPT = PREVIEW_REFERENCE_ROOT / "reference_script.txt"
PREVIEW_REFERENCE_AUDIO = PREVIEW_REFERENCE_ROOT / "reference_audio.wav"
PREVIEW_PROJECTS_ROOT = DATA_ROOT / "protected_preview_projects"
PREVIEW_STATE_ROOT = DATA_ROOT / "preview_state"
PRESET_PEXELS_ROOT = DATA_ROOT / "preset_pexels"
_FONT_CACHE_LOCK = threading.Lock()
_FONT_CACHE: dict[str, Any] = {"at": 0.0, "families": []}
_PRESET_MEDIA_LOCK = threading.Lock()
DEFAULT_PREVIEW_SCRIPT = """The best way to understand a video style is to watch it behave like a real finished project. So this permanent preview uses the same short documentary story every time. Imagine arriving in Iceland just before sunrise. The road is quiet, the mountains are still dark, and low clouds are moving across the valleys. A few minutes later, the sky begins to brighten and the first light reaches the snow-covered peaks. Small houses appear beside the road, waterfalls drop from black cliffs, and steam rises from the ground where geothermal heat meets the cold air. As the journey continues, the landscape changes quickly. One moment there is an open field with horses, and the next there is a dramatic coastline with waves crashing against volcanic rock. In a nearby town, people are starting their day while visitors stop for coffee before driving farther into the countryside. This reference story is intentionally simple because the content should stay the same while the preset changes around it. Captions can move, fonts can grow, colors can change, foreground images can appear, Pexels media can switch, motion can become slower or stronger, music can rise or duck under narration, and output settings can change. Because the script and audio remain fixed, every preview gives a fair comparison. What changes on screen comes from the selected preset, helping you see much more clearly what a real viewer will receive when that preset is used for production."""



def _renderer_font_catalog(force: bool = False) -> list[str]:
    """Return the exact font families the offscreen Qt renderer can use.

    A short-lived child process is used so the FastAPI process never owns a
    QGuiApplication. It loads the same bundled manifest fonts as render_process
    and then reports every Qt-visible system/application family.
    """
    now = time.time()
    with _FONT_CACHE_LOCK:
        cached = list(_FONT_CACHE.get("families") or [])
        if cached and not force and now - float(_FONT_CACHE.get("at") or 0.0) < 300:
            return cached
    code_root = Path(__file__).resolve().parents[1]
    probe = r"""import hashlib,json,os,sys
from pathlib import Path
os.environ.setdefault("QT_QPA_PLATFORM","offscreen")
from PySide6.QtGui import QFontDatabase,QGuiApplication
app=QGuiApplication.instance() or QGuiApplication(["acs-font-probe"])
root=Path(sys.argv[1]); fonts=root/"fonts"; manifest=fonts/"SHA256SUMS.json"
if manifest.is_file():
 data=json.loads(manifest.read_text(encoding="utf-8"))
 for filename,expected in data.items():
  path=fonts/filename
  if not path.is_file(): continue
  if hashlib.sha256(path.read_bytes()).hexdigest()!=expected: continue
  QFontDatabase.addApplicationFont(str(path))
for extra in (root/"smart_scroll"/"fonts",):
 if extra.is_dir():
  for path in sorted(list(extra.glob("*.ttf"))+list(extra.glob("*.otf"))): QFontDatabase.addApplicationFont(str(path))
print(json.dumps(sorted(set(str(x).strip() for x in QFontDatabase.families() if str(x).strip()),key=str.casefold),ensure_ascii=False))"""
    env = dict(os.environ)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    result = subprocess.run([sys.executable, "-c", probe, str(code_root)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False, env=env)
    if result.returncode == 0:
        try:
            families = json.loads((result.stdout or "[]").strip())
        except Exception:
            families = []
    else:
        families = []
    # Fail soft if Qt probing is temporarily unavailable: the curated renderer
    # families remain selectable and the render preflight still validates them.
    if not families:
        families = [name for values in SCRIPT_FONTS.values() for name in values]
    seen: set[str] = set()
    clean: list[str] = []
    preferred = [name for values in SCRIPT_FONTS.values() for name in values]
    for name in preferred + list(families):
        text = str(name or "").strip()
        key = text.casefold()
        if text and key not in seen:
            seen.add(key); clean.append(text)
    with _FONT_CACHE_LOCK:
        _FONT_CACHE["at"] = now
        _FONT_CACHE["families"] = clean
    return clean


def _font_bootstrap() -> dict[str, Any]:
    families = _renderer_font_catalog()
    defaults: dict[str, str] = {}
    for script in SCRIPT_FONTS:
        selected, _ = choose_font_family("", script, families)
        if selected:
            defaults[script] = selected
    return {"families": families, "count": len(families), "script_defaults": defaults, "mode_default": "auto"}


def _preset_media_root(principal: Principal, preset_id: str) -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9._-]+", "_", str(preset_id or "preset"))[:96] or "preset"
    return PRESET_PEXELS_ROOT / _safe_user_key(principal.username) / safe_id


def _preset_media_paths(principal: Principal, preset_id: str) -> tuple[Path, Path, Path, Path]:
    root = _preset_media_root(principal, preset_id)
    return root, root / "visual_plan.txt", root / "visual_plan.json", root / "prepare_state.json"


def _preset_preview_script_path(principal: Principal, preset_id: str) -> Path:
    return _preset_media_root(principal, preset_id) / "preview_script.txt"


def _preset_preview_script_status(principal: Principal, preset_id: str) -> dict[str, Any]:
    path = _preset_preview_script_path(principal, preset_id)
    if not path.is_file():
        return {"custom_script_ready": False, "script_name": "", "script_chars": 0, "script_sha256": "", "script_excerpt": ""}
    text = path.read_text(encoding="utf-8", errors="replace")
    return {
        "custom_script_ready": bool(text.strip()),
        "script_name": path.name,
        "script_chars": len(text),
        "script_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "script_excerpt": re.sub(r"\s+", " ", text).strip()[:180],
    }


def _active_preview_script(principal: Principal, preset_id: str) -> Path:
    custom = _preset_preview_script_path(principal, preset_id)
    if custom.is_file() and custom.stat().st_size > 0:
        return custom
    return _ensure_preview_script()


def _visual_query_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return (slug[:90] or "pexels")


def _clean_visual_query(value: str) -> str:
    text = re.sub(r"https?://\S+", " ", str(value or ""))
    text = re.sub(r"^\s*(?:[-*•]+|\d+[.)-]\s*)", "", text)
    text = re.sub(r"^\s*(?:scene|shot|clip|beat)\s*#?\d+\s*[:.)-]?\s*", "", text, flags=re.I)
    text = re.sub(r"^\s*\[?\d{1,2}:\d{2}(?::\d{2})?(?:\s*[-–—>]\s*\d{1,2}:\d{2}(?::\d{2})?)?\]?\s*", "", text)
    tagged = re.search(r"(?:pexels(?:\s+query)?|search(?:\s+query)?|visual(?:\s+search)?|keywords?)\s*[:=\-]\s*(.+)$", text, flags=re.I)
    if tagged:
        text = tagged.group(1)
    text = re.sub(r"^(?:visual|shot|image|background|b-roll|broll)\s*[:=\-]\s*", "", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip(" |,;:-")
    if not text or len(text) < 2:
        return ""
    if len(text) > 180:
        text = text[:180].rsplit(" ", 1)[0].strip()
    words = text.split()
    if len(words) > 14:
        text = " ".join(words[:14])
    return text


def _parse_visual_plan(text: str, limit: int = 600) -> list[str]:
    raw_text = str(text or "").replace("\r", "")
    lines = [line.strip() for line in raw_text.split("\n") if line.strip()]
    queries: list[str] = []
    seen: set[str] = set()
    explicit_markers = re.compile(r"(?:pexels|search|visual|keywords?|b-roll|broll)", re.I)
    candidates = [line for line in lines if explicit_markers.search(line)]
    if len(candidates) < 2:
        # Plain scrolling-preview scripts are usually prose, not scene-plan
        # rows. Split prose into sentences so Auto Pexels does not collapse a
        # whole preview script into one giant search query.
        prose = re.split(r"(?<=[.!?।！？])\s+", re.sub(r"\s+", " ", raw_text).strip())
        usable_prose = [x.strip() for x in prose if len(x.strip()) >= 8]
        candidates = usable_prose if len(usable_prose) >= 2 else lines
    for line in candidates:
        query = _clean_visual_query(line)
        if not query:
            continue
        if query.casefold() in {"scene plan", "visual plan", "intro", "outro", "hook", "notes", "spoken words"}:
            continue
        key = query.casefold()
        if key in seen:
            continue
        seen.add(key); queries.append(query)
        if len(queries) >= max(1, min(600, int(limit))):
            break
    if not queries:
        for piece in re.split(r"[,;|]+", str(text or "")):
            query = _clean_visual_query(piece)
            key = query.casefold()
            if query and key not in seen:
                seen.add(key); queries.append(query)
                if len(queries) >= limit: break
    return queries


def _load_visual_plan(principal: Principal, preset_id: str) -> dict[str, Any]:
    root, txt_path, json_path, state_path = _preset_media_paths(principal, preset_id)
    data = _load_json(json_path, {})
    if not isinstance(data, dict): data = {}
    queries = [str(x).strip() for x in data.get("queries", []) if str(x).strip()]
    state = _load_json(state_path, {})
    if not isinstance(state, dict): state = {}
    library = root / "library"
    cached_queries: list[str] = []
    cached_images = 0
    for query in queries:
        qdir = library / _visual_query_slug(query)
        good = [p for p in qdir.glob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS and p.stat().st_size >= 10_000] if qdir.is_dir() else []
        if good:
            cached_queries.append(query); cached_images += len(good)
    return {
        "plan_ready": bool(queries), "txt_name": str(data.get("txt_name") or (txt_path.name if txt_path.is_file() else "")),
        "plan_sha256": str(data.get("sha256") or ""), "queries": queries, "query_count": len(queries),
        "cached_query_count": len(cached_queries), "cached_image_count": cached_images,
        "missing_count": max(0, len(queries) - len(cached_queries)), "library_path": str(library),
        "prepare": state,
    }


def _preset_visual_signature(principal: Principal, preset_id: str) -> dict[str, Any]:
    status = _load_visual_plan(principal, preset_id)
    state = status.get("prepare") if isinstance(status.get("prepare"), dict) else {}
    return {"plan_sha256": status.get("plan_sha256", ""), "media_revision": state.get("media_revision", ""), "cached_query_count": status.get("cached_query_count", 0)}


def _valid_downloaded_image(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 10_000:
        return False
    try:
        result = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(path)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, check=False)
        return result.returncode == 0 and "x" in (result.stdout or "")
    except Exception:
        return False


def _choose_pexels_photo_url(photo: dict[str, Any]) -> str:
    src = photo.get("src") if isinstance(photo, dict) else {}
    if not isinstance(src, dict): return ""
    for key in ("large2x", "large", "landscape", "original", "medium"):
        value = str(src.get(key) or "").strip()
        if value: return value
    return ""


def _prepare_preset_media_task(state_path: str, library_path: str, queries: list[str]) -> None:
    state_file, library = Path(state_path), Path(library_path)
    library.mkdir(parents=True, exist_ok=True)
    state = {"status": "running", "total": len(queries), "done": 0, "downloaded": 0, "cached": 0, "failed": 0, "message": "Starting missing-only Pexels preparation", "started_at": now_iso(), "media_revision": ""}
    _atomic_json(state_file, state)
    session = requests.Session()
    try:
        for index, query in enumerate(queries, start=1):
            qdir = library / _visual_query_slug(query); qdir.mkdir(parents=True, exist_ok=True)
            existing = [p for p in qdir.glob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS and p.stat().st_size >= 10_000]
            if existing:
                state["cached"] += 1
                state["done"] = index
                state["message"] = f"Cached: {query}"
                _atomic_json(state_file, state); continue
            key = next_server_pexels_key()
            if not key:
                state["failed"] += 1; state["done"] = index; state["message"] = "No server Pexels key is configured"; _atomic_json(state_file, state); continue
            downloaded = False
            error_text = ""
            for attempt in range(2):
                try:
                    response = session.get("https://api.pexels.com/v1/search", headers={"Authorization": key}, params={"query": query, "per_page": 6, "orientation": "landscape"}, timeout=30)
                    if response.status_code == 429:
                        if attempt == 0:
                            time.sleep(2); key = next_server_pexels_key() or key; continue
                        state["status"] = "rate_limited"
                        state["done"] = index - 1
                        state["message"] = "Pexels rate limit reached. Cached media is preserved; run Prepare later to continue missing-only."
                        state["finished_at"] = now_iso()
                        state["media_revision"] = str(state.get("media_revision") or time.time_ns())
                        _atomic_json(state_file, state)
                        return
                    response.raise_for_status()
                    photos = (response.json() or {}).get("photos") or []
                    for photo in photos:
                        url = _choose_pexels_photo_url(photo)
                        if not url: continue
                        ext = Path(url.split("?",1)[0]).suffix.lower()
                        if ext not in IMAGE_EXTENSIONS: ext = ".jpg"
                        photo_id = re.sub(r"[^0-9A-Za-z_-]+", "", str(photo.get("id") or uuid.uuid4().hex[:12]))[:40]
                        target = qdir / f"{_visual_query_slug(query)}_{photo_id}{ext}"
                        temp = target.with_name("." + target.name + ".part")
                        if target.is_file() and _valid_downloaded_image(target): downloaded = True; break
                        img = session.get(url, timeout=40); img.raise_for_status(); temp.write_bytes(img.content)
                        if _valid_downloaded_image(temp): temp.replace(target); downloaded = True; break
                        temp.unlink(missing_ok=True)
                    break
                except Exception as exc:
                    error_text = str(exc)[:220]
            state["done"] = index
            if downloaded:
                state["downloaded"] += 1; state["message"] = f"Prepared: {query}"; state["media_revision"] = str(time.time_ns())
            else:
                state["failed"] += 1; state["message"] = f"No usable image: {query}" + (f" · {error_text}" if error_text else "")
            _atomic_json(state_file, state)
            time.sleep(0.15)
        state["status"] = "completed" if state["failed"] == 0 else "completed_with_errors"
        state["message"] = f"Preset media ready: {state['downloaded']} downloaded, {state['cached']} reused, {state['failed']} missing"
        state["finished_at"] = now_iso(); state["media_revision"] = str(time.time_ns())
        _atomic_json(state_file, state)
    except BaseException as exc:
        state["status"] = "failed"; state["message"] = str(exc)[:500]; state["finished_at"] = now_iso(); state["media_revision"] = str(time.time_ns())
        _atomic_json(state_file, state)




def _ensure_preview_script() -> Path:
    PREVIEW_REFERENCE_ROOT.mkdir(parents=True, exist_ok=True)
    marker = PREVIEW_REFERENCE_ROOT / ".cleanup_protected"
    if not marker.exists():
        marker.write_text("Permanent ACS preset preview assets. Do not remove during project cleanup.\n", encoding="utf-8")
    if not PREVIEW_REFERENCE_SCRIPT.is_file():
        PREVIEW_REFERENCE_SCRIPT.write_text(DEFAULT_PREVIEW_SCRIPT.strip() + "\n", encoding="utf-8")
        os.chmod(PREVIEW_REFERENCE_SCRIPT, 0o640)
    return PREVIEW_REFERENCE_SCRIPT


def _audio_duration(path: Path) -> float:
    if not path.is_file():
        return 0.0
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, check=False,
    )
    if result.returncode != 0:
        return 0.0
    try:
        return max(0.0, float((result.stdout or "0").strip()))
    except Exception:
        return 0.0


def _preview_reference_status() -> dict[str, Any]:
    script = _ensure_preview_script()
    audio_seconds = _audio_duration(PREVIEW_REFERENCE_AUDIO)
    return {
        "protected": True,
        "script_ready": script.is_file() and script.stat().st_size > 0,
        "script_chars": len(script.read_text(encoding="utf-8", errors="replace")) if script.is_file() else 0,
        "audio_ready": PREVIEW_REFERENCE_AUDIO.is_file() and audio_seconds >= 1,
        "audio_seconds": round(audio_seconds, 3),
        "target_seconds": round(audio_seconds, 3),
    }


def _preview_hash(principal: Principal, preset: dict[str, Any]) -> str:
    audio_stat = PREVIEW_REFERENCE_AUDIO.stat() if PREVIEW_REFERENCE_AUDIO.is_file() else None
    preset_id = str(preset.get("id") or "preset")
    script_path = _active_preview_script(principal, preset_id)
    script_stat = script_path.stat() if script_path.is_file() else None
    auto_pexels = bool(preset.get("auto_pexels_media", True))
    frozen = {
        "settings": normalize_project_settings(dict(preset.get("settings") or {})),
        "fg_library_id": str(preset.get("fg_library_id") or ""),
        "voice_mode": str(preset.get("voice_mode") or "upload_audio"),
        "voice_profile_id": str(preset.get("voice_profile_id") or ""),
        "reference_audio": {
            "size": int(audio_stat.st_size) if audio_stat else 0,
            "mtime_ns": int(audio_stat.st_mtime_ns) if audio_stat else 0,
        },
        "preview_script": {
            "size": int(script_stat.st_size) if script_stat else 0,
            "mtime_ns": int(script_stat.st_mtime_ns) if script_stat else 0,
        },
        "font_mode": str(preset.get("font_mode") or "auto"),
        "auto_pexels_media": auto_pexels,
        "preset_visuals": _preset_visual_signature(principal, preset_id) if auto_pexels else {},
    }
    raw = json.dumps(frozen, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _preview_state_file(principal: Principal, preset_id: str) -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9._-]+", "_", preset_id)
    return PREVIEW_STATE_ROOT / _safe_user_key(principal.username) / f"{safe_id}.json"


def _load_preview_state(principal: Principal, preset_id: str) -> dict[str, Any]:
    data = _load_json(_preview_state_file(principal, preset_id), {})
    return data if isinstance(data, dict) else {}


def _save_preview_state(principal: Principal, preset_id: str, state: dict[str, Any]) -> None:
    state["updated_at"] = now_iso()
    _atomic_json(_preview_state_file(principal, preset_id), state)


def _preview_project_identity(principal: Principal, preset_id: str) -> tuple[str, Path]:
    key = f"{principal.username}\n{preset_id}".encode("utf-8")
    project_id = "webpreview_" + hashlib.sha256(key).hexdigest()[:24]
    root = PREVIEW_PROJECTS_ROOT / _safe_user_key(principal.username) / re.sub(r"[^A-Za-z0-9._-]+", "_", preset_id)
    return project_id, root


def _ensure_preview_project(principal: Principal, preset: dict[str, Any]) -> tuple[str, Path]:
    if not _preview_reference_status()["audio_ready"]:
        raise HTTPException(409, "Permanent preview audio has not been uploaded yet")
    project_id, root = _preview_project_identity(principal, str(preset.get("id") or "preset"))
    for folder in ("input", "temp", "output", "preview", "logs", "artifacts"):
        (root / folder).mkdir(parents=True, exist_ok=True)
    (root / ".cleanup_protected").write_text("Protected preset preview project.\n", encoding="utf-8")
    shutil.copy2(_active_preview_script(principal, str(preset.get("id") or "preset")), root / "input" / "reference_script.txt")
    shutil.copy2(PREVIEW_REFERENCE_AUDIO, root / "input" / "reference_audio.wav")
    metadata = {"id": project_id, "name": f"Preset Preview - {preset.get('name') or 'Preset'}", "owner_id": principal.username, "status": "created", "created_at": now_iso(), "web_flow_v1": True, "web_preview_reference": True}
    (root / "project.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
    with connect() as db:
        row = db.execute("SELECT id,owner_id FROM projects WHERE id=?", (project_id,)).fetchone()
        if row is None:
            db.execute("INSERT INTO projects(id,name,owner_id,status,created_at,project_path) VALUES(?,?,?,'created',?,?)", (project_id, metadata["name"], principal.username, metadata["created_at"], str(root)))
        else:
            if str(row["owner_id"]) != principal.username:
                raise HTTPException(409, "Preview project ownership mismatch")
            db.execute("UPDATE projects SET name=?,project_path=? WHERE id=?", (metadata["name"], str(root), project_id))
        db.commit()
    return project_id, root


def _preview_job_status(job_id: str) -> dict[str, Any] | None:
    if not job_id:
        return None
    try:
        return get_job(job_id)
    except Exception:
        return None


def _preview_status_payload(principal: Principal, preset: dict[str, Any]) -> dict[str, Any]:
    """Return browser-preview state only; never inspect or mutate production jobs."""
    current_hash = _preview_hash(principal, preset)
    state = _load_preview_state(principal, str(preset["id"]))
    reference = _preview_reference_status()
    preview_hash = str(state.get("last_preview_hash") or state.get("preview_hash") or "")
    approved_hash = str(state.get("approved_hash") or "")
    active = bool(state.get("preview_status") == "running")
    is_current = bool(preview_hash and preview_hash == current_hash and not active)
    approved = bool(approved_hash and approved_hash == current_hash and state.get("approved_snapshot"))
    return {
        "ok": True,
        "preset_id": str(preset["id"]),
        "preset_hash": current_hash,
        "reference": reference,
        "active": active,
        "active_job_id": "",
        "active_run_id": "",
        "active_hash": preview_hash if active else "",
        "active_status": "running" if active else "",
        "active_progress": int(state.get("preview_progress") or 0) if active else 0,
        "active_stage": "browser_preview" if active else "",
        "active_message": "Live Preview is running locally in the browser." if active else "",
        "completed_job_id": "",
        "completed_run_id": "",
        "completed_hash": preview_hash,
        "video_ready": False,
        "is_current": is_current,
        "needs_render": False,
        "pending_hash": "",
        "preview_mode": "browser",
        "preview_message": str(state.get("preview_message") or "Live Preview is independent of production rendering."),
        "preview_approved": approved,
        "approved_hash": approved_hash,
        "approved_at": state.get("approved_at") or "",
        "approval_state": "approved" if approved else ("changes_since_approval" if state.get("approved_snapshot") else "not_approved"),
    }
def _prune_preview_jobs(project_id: str, keep: int = 4) -> None:
    with connect() as db:
        rows = db.execute("SELECT id,preview_path,output_path FROM jobs WHERE project_id=? AND status IN ('completed','failed','cancelled','interrupted') ORDER BY created_at DESC", (project_id,)).fetchall()
        for row in rows[keep:]:
            jid = str(row["id"])
            db.execute("DELETE FROM job_events WHERE job_id=?", (jid,))
            db.execute("DELETE FROM job_attempts WHERE job_id=?", (jid,))
            db.execute("DELETE FROM jobs WHERE id=?", (jid,))
            for raw in (row["preview_path"], row["output_path"]):
                p = Path(str(raw or ""))
                if p.is_file(): p.unlink(missing_ok=True)
        db.commit()

def _model_payload(model: BaseModel, **kwargs) -> dict[str, Any]:
    """Pydantic v1/v2 compatibility for the live FastAPI environment."""
    if hasattr(model, "model_dump"):
        return model.model_dump(**kwargs)
    return model.dict(**kwargs)


def _atomic_json(path: Path, payload: Any, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, mode)
    tmp.replace(path)


def _load_json(path: Path, fallback: Any) -> Any:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value
    except Exception:
        return fallback


def _audit(principal: Principal, action: str, detail: dict[str, Any] | None = None) -> None:
    AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "at": now_iso(),
        "actor": principal.username,
        "admin": principal.is_admin,
        "action": action,
        "detail": detail or {},
    }
    with AUDIT_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def _safe_user_key(username: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", username).strip("._-")[:48] or "user"
    digest = hashlib.sha256(username.encode("utf-8")).hexdigest()[:10]
    return f"{stem}-{digest}"


def _preset_file(principal: Principal) -> Path:
    return PRESETS_ROOT / f"{_safe_user_key(principal.username)}.json"


def _user_file(principal: Principal) -> Path:
    return USERS_ROOT / f"{_safe_user_key(principal.username)}.json"


def _documentary_scroll_profile_settings() -> dict[str, Any]:
    """Stable production settings for the Documentary Scroll preset.

    These values intentionally contain only reusable presentation/render
    behavior. Project ids, queue state, source/output paths, API keys,
    project text, scene plans, and language-specific/local-machine paths are
    never copied into the preset.
    """
    base = project_defaults()
    base.update({
        # Canvas / encode
        "width": 1280,
        "height": 720,
        "fps": 24,
        "quality_crf": 23,
        "ffmpeg_preset": "ultrafast",
        "video_encoder": "cpu_x264",
        "post_render_compress_enabled": True,
        "post_render_compress_quality": "Balanced Fast",
        "post_render_compress_encoder": "CPU Fast (Reliable)",
        "post_render_compress_overwrite_only_smaller": True,
        "post_render_compress_keep_failed_temp": False,

        # Pipeline / sync
        "auto_pipeline_enabled": True,
        "auto_generate_voice": False,
        "auto_prepare_backgrounds": True,
        "auto_sync_audio": True,
        "start_mode": "voice_centered",
        "extra_start_seconds": 0.0,
        "extra_end_seconds": 0.0,
        "single_paragraph": True,

        # Scroll text
        "captions_enabled": True,
        "caption_mode": False,
        "caption_text_source": "script",
        "caption_sync_mode": "better",
        "caption_language": "auto",
        "caption_prefer_script_language": True,
        "caption_export_srt": True,
        "caption_gap_tighten": True,
        "caption_max_words": 6,
        "caption_min_duration": 0.65,
        "caption_timing_offset": 0.0,
        "caption_whisper_model": "base",
        "caption_heavy_enabled": False,
        "caption_karaoke_enabled": False,
        "caption_highlight_color": "#FFD54A",
        "caption_pop_percent": 16,

        # Auto font supplies the language-specific family at render time.
        # Noto Sans is only the safe generic fallback stored in the preset.
        "font_family": "Noto Sans",
        "font_size": 40,
        "bold": False,
        "italic": False,
        "underline": False,
        "character_spacing": 0,
        "text_line_spacing": 26,
        "line_height_percent": 100,
        "paragraph_spacing": 20,
        "alignment": "left",
        "direction": "auto",
        "margin_left": 100,
        "margin_right": 55,
        "margin_top": 80,
        "margin_bottom": 80,
        "text_width_percent": 71,
        "text_x_percent": 100,
        "text_offset_x_percent": -2,
        "text_offset_y_percent": 0,
        "text_uniform_scale": False,
        "text_color": "#FFFFFF",
        "text_opacity": 100,

        # Proven scrolling-text plate from the supplied production manifest.
        # Follow Text is the default; fixed plate geometry remains stored so
        # switching the UI mode does not lose the user's plate layout.
        "text_background_enabled": True,
        "text_background_mode": "whole",
        "text_background_follow_text": True,
        "text_background_color": "#C8C8C8",
        "text_background_opacity_percent": 22,
        "text_background_padding": 24,
        "text_background_padding_x": 24,
        "text_background_padding_y": 24,
        "text_background_radius": 18,
        "text_background_width_percent": 92,
        "text_background_height_percent": 32,
        "text_background_x_percent": 50,
        "text_background_y_percent": 50,
        "text_background_radius_percent": 40,
        "outline_enabled": True,
        "outline_color": "#000000",
        "outline_width": 2,
        "shadow_enabled": False,
        "shadow_color": "#000000",
        "shadow_offset_x": 3,
        "shadow_offset_y": 3,

        # Scroll speed
        "manual_speed_px_per_sec": 80.0,

        # Story foreground. Renderer requires story_visualizer_enabled for the
        # foreground overlay path, so both flags are deliberately enabled.
        "story_visualizer_enabled": True,
        "story_visualizer_template": "custom",
        "foreground_enabled": True,
        "foreground_x_percent": 49,
        "foreground_y_percent": 58,
        "foreground_width_percent": 100,
        "foreground_opacity_percent": 100,
        "foreground_use_folder_rotation": False,

        # Keep supplementary overlays quiet for documentary scroll.
        "title_enabled": False,
        "waveform_enabled": False,
        "player_controls_enabled": False,
        "subscribe_badge_enabled": False,

        # Base visual treatment
        "custom_background_enabled": False,
        "background_mode": "gradient",
        "background_color": "#101018",
        "gradient_color_1": "#0A0A0A",
        "gradient_color_2": "#1F2937",
        "background_blur": 0,
        "background_brightness": 100,
        "dark_overlay_percent": 28,

        # Auto Pexels is prepared by the web layer into the protected preset
        # cache. Renderer consumes that cache; it does not perform live
        # per-frame Pexels downloads.
        "pexels_enabled": True,
        "pexels_download_enabled": False,
        "pexels_auto_keywords": False,
        "pexels_query": "",
        "use_image_backgrounds": True,
        "use_video_backgrounds": False,
        "pexels_use_library": True,
        "pexels_randomize_library": True,
        "pexels_min_library_images": 1,
        "pexels_interval_seconds": 8.0,
        "single_background_enabled": False,
        "motion_enabled": True,
        "visual_motion_style": "cinematic",
        "pexels_motion_speed_percent": 100,
        "pexels_motion_intensity_percent": 42,
        "visual_avoid_repeated_motion": True,
        "visual_quality_protection": True,
        "visual_smart_rotation": True,
        "image_transition_mode": "crossfade",
        "image_transition_seconds": 0.55,
        "image_timing_mode": "fixed",
        "image_timing_min_seconds": 4.0,
        "image_timing_max_seconds": 10.0,
        "pexels_video_interval_seconds": 4.0,
        "pexels_mix_mode": "alternate",
        "pexels_video_frame_fps": 6.0,

        # Audio
        "voice_volume_percent": 130,
        "voice_speed_percent": 100,
        "voice_processing_preset": "off",
        "music_enabled": False,
        "music_volume_percent": 50,
        "music_ducking_enabled": True,
        "music_ducking_strength": "medium",
        "music_normalize": True,
        "music_loop_if_short": True,
        "music_trim_if_long": True,
        "music_loop_crossfade_seconds": 0.5,
        "music_fade_in_seconds": 1.5,
        "music_fade_out_seconds": 2.0,
    })
    return normalize_project_settings(base)


def _documentary_scroll_profile_info(settings: dict[str, Any]) -> dict[str, str]:
    explicit_bg = str(settings.get("text_background_mode") or "").lower()
    bg_mode = "Whole Text Block" if explicit_bg == "whole" else ("Follow Text" if explicit_bg == "follow" else ("Off" if not bool(settings.get("text_background_enabled", True)) else "Whole Text Block"))
    resolution = f"{int(settings.get('width') or 1280)}×{int(settings.get('height') or 720)}"
    return {
        "version": f"v{DOCUMENTARY_SCROLL_PROFILE_VERSION}",
        "purpose": "Long-form documentary scrolling text with reusable preset media and optional foreground character.",
        "flow": "TXT script + reference/production audio → Auto Pexels preset cache → renderer → Drive verified → immediate local cleanup.",
        "text": (
            f"Scroll · Auto font · {settings.get('font_size', 40)}px · "
            f"{settings.get('alignment', 'left')} · width {settings.get('text_width_percent', 71)}% · "
            f"{int(settings.get('manual_speed_px_per_sec') or 80)} px/s"
        ),
        "text_background": (
            f"{bg_mode} · {settings.get('text_background_color', '#C8C8C8')} · "
            f"{settings.get('text_background_opacity_percent', 22)}% opacity · "
            f"{settings.get('text_background_padding_x', settings.get('text_background_padding', 24))}×{settings.get('text_background_padding_y', settings.get('text_background_padding', 24))}px padding"
        ),
        "foreground": (
            f"Enabled · X {settings.get('foreground_x_percent', 49)}% · "
            f"Y {settings.get('foreground_y_percent', 58)}% · "
            f"width {settings.get('foreground_width_percent', 100)}% · original aspect"
        ),
        "media": (
            f"Auto Pexels images · protected preset cache · {settings.get('pexels_interval_seconds', 8)}s interval · "
            f"{settings.get('visual_motion_style', 'cinematic')} motion · "
            f"{settings.get('image_transition_mode', 'crossfade')} {settings.get('image_transition_seconds', 0.55)}s"
        ),
        "audio": (
            f"Voice-centered auto sync · voice {settings.get('voice_volume_percent', 130)}% · "
            f"music off by default · ducking ready"
        ),
        "output": (
            f"{resolution} · {settings.get('fps', 24)}fps · CRF {settings.get('quality_crf', 23)} · "
            f"Balanced Fast post-compress"
        ),
        "safety": "No API keys, local source paths, project ids, queue state, script body, scene plan, or project-specific metadata is stored in this preset.",
        "source": "Profile distilled from the supplied working ACS manifest plus current live renderer/web-flow capabilities.",
    }


def _default_preset_settings(name: str) -> dict[str, Any]:
    if name == "Documentary Scroll":
        return _documentary_scroll_profile_settings()

    base = project_defaults()
    common = {
        "width": 1280,
        "height": 720,
        "fps": 24,
        "quality_crf": 23,
        "ffmpeg_preset": "ultrafast",
        "video_encoder": "cpu_x264",
        "captions_enabled": True,
        "pexels_use_library": True,
        "pexels_randomize_library": True,
        "post_render_compress_enabled": False,
    }
    base.update(common)
    if name == "Clean Caption":
        base.update({
            "caption_mode": True,
            "caption_text_source": "whisper",
            "caption_sync_mode": "better",
            "caption_position": "bottom",
            "caption_max_words": 8,
            "caption_heavy_enabled": False,
            "pexels_enabled": True,
            "use_image_backgrounds": True,
        })
    elif name == "English Shorts":
        base.update({
            "width": 720,
            "height": 1280,
            "caption_mode": True,
            "caption_heavy_enabled": True,
            "caption_heavy_preset": "viral_pop",
            "caption_karaoke_enabled": True,
            "caption_highlight_color": "#FFD54A",
            "pexels_enabled": True,
            "use_image_backgrounds": True,
            "use_video_backgrounds": True,
            "visual_motion_style": "dynamic",
        })
    elif name == "Urdu Storytelling":
        base.update({
            "caption_mode": False,
            "start_mode": "voice_centered",
            "font_family": "Jameel Noori Nastaleeq Kasheeda",
            "font_size": 54,
            "alignment": "center",
            "pexels_enabled": True,
            "use_image_backgrounds": True,
            "motion_enabled": True,
        })
    elif name == "Dark Viral":
        base.update({
            "caption_mode": True,
            "caption_heavy_enabled": True,
            "caption_heavy_preset": "viral_pop",
            "caption_karaoke_enabled": True,
            "text_background_enabled": True,
            "text_background_color": "#000000",
            "text_background_opacity_percent": 65,
            "pexels_enabled": True,
            "music_enabled": True,
            "music_ducking_enabled": True,
            "music_ducking_strength": "strong",
        })
    return normalize_project_settings(base)


def _upgrade_documentary_scroll_preset(item: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    if str(item.get("id") or "") != "documentary-scroll":
        return item, False
    try:
        current_version = int(item.get("profile_version") or 0)
    except Exception:
        current_version = 0
    if current_version >= DOCUMENTARY_SCROLL_PROFILE_VERSION:
        return item, False

    previous_font_mode = "manual" if str(item.get("font_mode") or "auto").lower() == "manual" else "auto"
    previous_settings = dict(item.get("settings") or {})
    upgraded = _documentary_scroll_profile_settings()

    # A deliberate manual font selection is user intent; keep it across the
    # profile upgrade. Auto mode gets the generic fallback and language-aware
    # runtime selection.
    if previous_font_mode == "manual" and previous_settings.get("font_family"):
        upgraded["font_family"] = previous_settings["font_family"]

    item["settings"] = upgraded
    item["font_mode"] = previous_font_mode
    item["auto_pexels_media"] = True
    item["profile_version"] = DOCUMENTARY_SCROLL_PROFILE_VERSION
    item["profile_info"] = _documentary_scroll_profile_info(upgraded)
    # Rendering behavior changed materially; require one fresh approval rather
    # than silently treating an older preview as approved.
    item["preview_approved"] = False
    item["updated_at"] = now_iso()
    return item, True


def _seed_presets() -> list[dict[str, Any]]:
    items = []
    for name in ("Documentary Scroll", "Clean Caption", "English Shorts", "Urdu Storytelling", "Dark Viral"):
        settings = _default_preset_settings(name)
        item = {
            "id": re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-"),
            "name": name,
            "settings": settings,
            "fg_library_id": "",
            "voice_mode": "upload_audio",
            "voice_profile_id": "",
            "font_mode": "auto",
            "preview_approved": True,
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
        if name == "Documentary Scroll":
            item["auto_pexels_media"] = True
            item["profile_version"] = DOCUMENTARY_SCROLL_PROFILE_VERSION
            item["profile_info"] = _documentary_scroll_profile_info(settings)
            item["preview_approved"] = False
        items.append(item)
    return items


def _load_presets(principal: Principal) -> list[dict[str, Any]]:
    path = _preset_file(principal)
    data = _load_json(path, [])
    changed = False
    if not isinstance(data, list) or not data:
        data = _seed_presets()
        _atomic_json(path, data)
    cleaned: list[dict[str, Any]] = []
    for raw in data:
        if not isinstance(raw, dict):
            changed = True
            continue
        item = dict(raw)
        item["settings"] = normalize_project_settings(dict(item.get("settings") or {}))
        item["font_mode"] = "manual" if str(item.get("font_mode") or "auto").lower() == "manual" else "auto"
        item, upgraded = _upgrade_documentary_scroll_preset(item)
        changed = changed or upgraded
        cleaned.append(item)
    if changed:
        _atomic_json(path, cleaned)
    return cleaned


def _save_presets(principal: Principal, presets: list[dict[str, Any]]) -> None:
    _atomic_json(_preset_file(principal), presets)


def _find_preset(principal: Principal, preset_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    presets = _load_presets(principal)
    item = next((x for x in presets if str(x.get("id")) == preset_id), None)
    if item is None:
        raise HTTPException(404, "Preset not found")
    return presets, item


def _normalize_upload_categories(raw: Any) -> list[str]:
    values = raw if isinstance(raw, list) else []
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = re.sub(r"[\/\x00]+", " ", str(value or "")).strip()
        text = re.sub(r"\s+", " ", text)[:120]
        if not text or text in {".", ".."}:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= 100:
            break
    if not out:
        out = ["ACS Final Videos"]
    return out


def _load_user_state(principal: Principal) -> dict[str, Any]:
    defaults_value = {
        "selected_preset_id": "documentary-scroll",
        "upload_voice_mode": "upload_audio",
        "upload_categories": ["ACS Final Videos"],
        "selected_upload_category": "ACS Final Videos",
        "updated_at": now_iso(),
    }
    value = _load_json(_user_file(principal), defaults_value)
    if not isinstance(value, dict):
        value = defaults_value
    merged = {**defaults_value, **value}
    categories = _normalize_upload_categories(merged.get("upload_categories"))
    selected = str(merged.get("selected_upload_category") or "").strip()
    if not any(x.casefold() == selected.casefold() for x in categories):
        selected = categories[0]
    merged["upload_categories"] = categories
    merged["selected_upload_category"] = selected
    return merged


def _save_user_state(principal: Principal, state: dict[str, Any]) -> dict[str, Any]:
    categories = _normalize_upload_categories(state.get("upload_categories"))
    selected = re.sub(r"[\/\x00]+", " ", str(state.get("selected_upload_category") or "")).strip()
    selected = re.sub(r"\s+", " ", selected)[:120]
    if not any(x.casefold() == selected.casefold() for x in categories):
        selected = categories[0]
    clean = {
        "selected_preset_id": str(state.get("selected_preset_id") or "documentary-scroll"),
        "upload_voice_mode": str(state.get("upload_voice_mode") or "upload_audio"),
        "upload_categories": categories,
        "selected_upload_category": selected,
        "updated_at": now_iso(),
    }
    _atomic_json(_user_file(principal), clean)
    return clean


def _global_defaults() -> dict[str, Any]:
    return {
        "queue_mode": "normal",
        "scene_builder_priority": True,
        "paused": False,
        "updated_at": now_iso(),
        "updated_by": "",
    }


def load_global_settings() -> dict[str, Any]:
    raw = _load_json(GLOBAL_CONFIG, _global_defaults())
    if not isinstance(raw, dict):
        raw = {}
    merged = {**_global_defaults(), **raw}
    merged["queue_mode"] = "normal"
    merged["scene_builder_priority"] = bool(merged.get("scene_builder_priority", True))
    merged["paused"] = bool(merged.get("paused", False))
    return merged


def _save_global_settings(principal: Principal, patch: dict[str, Any]) -> dict[str, Any]:
    if not principal.is_admin:
        raise HTTPException(403, "Administrator access required for global queue controls")
    current = load_global_settings()
    # Queue mode is fixed. Keep the response field for older clients, but do
    # not accept runtime mode changes.
    current["queue_mode"] = "normal"
    if "scene_builder_priority" in patch:
        current["scene_builder_priority"] = bool(patch["scene_builder_priority"])
    if "paused" in patch:
        current["paused"] = bool(patch["paused"])
    current["updated_at"] = now_iso()
    current["updated_by"] = principal.username
    _atomic_json(GLOBAL_CONFIG, current)
    _audit(principal, "queue_settings", current)
    return current


def _fg_registry() -> list[dict[str, Any]]:
    DEFAULT_FG_ROOT.mkdir(parents=True, exist_ok=True)
    raw = _load_json(FG_CONFIG, [])
    items = [dict(x) for x in raw if isinstance(x, dict)] if isinstance(raw, list) else []

    # Import the already-protected legacy shared FG root as a reusable folder once.
    legacy = shared_fg_root()
    if legacy.is_dir() and not any(Path(str(x.get("path") or "")) == legacy for x in items):
        items.insert(0, {
            "id": "shared-fg-assets",
            "name": "Shared FG Assets",
            "path": str(legacy),
            "protected": True,
            "created_at": now_iso(),
        })
        _atomic_json(FG_CONFIG, items)
    return items


def _fg_details(item: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(item.get("path") or ""))
    files = []
    if path.is_dir():
        files = [p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS]
    return {
        **item,
        "exists": path.is_dir(),
        "file_count": len(files),
        "preview_available": bool(files),
    }


def _resolve_fg_library(library_id: str) -> dict[str, Any] | None:
    if not library_id:
        return None
    item = next((x for x in _fg_registry() if str(x.get("id")) == library_id), None)
    if item is None:
        raise HTTPException(409, "Selected FG library no longer exists")
    path = Path(str(item.get("path") or ""))
    if not path.is_dir():
        raise HTTPException(409, "Selected FG library folder is unavailable on VPS")
    return item


def _fg_images(item: dict[str, Any]) -> list[Path]:
    root = Path(str(item.get("path") or ""))
    if not root.is_dir():
        return []
    return sorted(
        [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS],
        key=lambda p: (p.name.casefold(), str(p).casefold()),
    )


def _select_fg_image(item: dict[str, Any], item_order: int) -> Path:
    images = _fg_images(item)
    if not images:
        raise HTTPException(409, "Selected FG library has no usable images")
    index = max(0, int(item_order or 1) - 1) % len(images)
    return images[index]


def _unsafe_system_path(path: Path) -> bool:
    resolved = path.resolve()
    for forbidden in (Path("/etc"), Path("/root"), Path("/usr"), Path("/bin"), Path("/sbin"), Path("/boot"), Path("/proc"), Path("/sys"), Path("/dev")):
        try:
            resolved.relative_to(forbidden)
            return True
        except ValueError:
            pass
    return False


def _voice_profiles() -> list[dict[str, Any]]:
    raw = _load_json(VOICE_CONFIG, [])
    return [dict(x) for x in raw if isinstance(x, dict)] if isinstance(raw, list) else []


def _save_voice_profiles(values: list[dict[str, Any]]) -> None:
    _atomic_json(VOICE_CONFIG, values)


def _validate_voice_profile(raw: dict[str, Any]) -> dict[str, Any]:
    name = str(raw.get("name") or "").strip()
    language = str(raw.get("language") or "").strip()
    voice_id = str(raw.get("voice_id") or "").strip()
    if not name or not language or not voice_id:
        raise HTTPException(400, "Profile name, language and Voice ID are required")
    return {
        "id": str(raw.get("id") or uuid.uuid4().hex),
        "name": name[:120],
        "language": language[:80],
        "voice_id": voice_id[:200],
        "model": str(raw.get("model") or "Multilingual v2")[:120],
        "stability": max(0, min(100, int(raw.get("stability", 50)))),
        "similarity": max(0, min(100, int(raw.get("similarity", 75)))),
        "style": max(0, min(100, int(raw.get("style", 0)))),
        "speed": max(0.5, min(2.0, float(raw.get("speed", 1.0)))),
        "speaker_boost": bool(raw.get("speaker_boost", True)),
        "enabled": bool(raw.get("enabled", True)),
        "updated_at": now_iso(),
    }


def _music_config() -> dict[str, Any]:
    DEFAULT_MUSIC_ROOT.mkdir(parents=True, exist_ok=True)
    raw = _load_json(MUSIC_CONFIG, {})
    if not isinstance(raw, dict):
        raw = {}
    path = Path(str(raw.get("path") or DEFAULT_MUSIC_ROOT))
    return {"path": str(path), "rotation_index": int(raw.get("rotation_index") or 0)}


def _music_files(path: Path) -> list[Path]:
    if not path.is_dir():
        return []
    return sorted([p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in MUSIC_EXTENSIONS])


def _assign_music(settings: dict[str, Any]) -> dict[str, Any]:
    clean = dict(settings)
    metadata = dict(clean.get("metadata") or {})
    if not bool(clean.get("music_enabled", False)):
        clean["music_assigned_path"] = ""
        clean["music_paths"] = []
        clean["music_folder_path"] = ""
        return clean
    cfg = _music_config()
    folder = Path(cfg["path"])
    files = _music_files(folder)
    if not files:
        raise HTTPException(409, "Music is enabled but the VPS music library has no usable audio files")
    mode = str(clean.get("music_rotation_mode") or "sequential").lower()
    if mode == "random":
        # Stable pseudo-random choice from current time without persisting secrets/state.
        index = int(time.time_ns()) % len(files)
    else:
        index = int(cfg.get("rotation_index") or 0) % len(files)
        if bool(clean.get("music_remember_position", True)):
            cfg["rotation_index"] = (index + 1) % len(files)
            _atomic_json(MUSIC_CONFIG, cfg)
    clean["music_folder_path"] = str(folder)
    clean["music_paths"] = [str(p) for p in files]
    clean["music_assigned_path"] = str(files[index])
    metadata["web_music_track"] = files[index].name
    clean["metadata"] = metadata
    return clean


def _normalize_web_text_render_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Keep the web selector, preview and production renderer on one text mode."""
    clean = dict(settings or {})
    enabled = bool(clean.get("captions_enabled", True))
    caption_mode = bool(clean.get("caption_mode", True))
    if not enabled:
        mode = "off"
        clean["captions_enabled"] = False
        clean["caption_mode"] = False
    elif caption_mode:
        mode = "caption"
        clean["captions_enabled"] = True
        clean["caption_mode"] = True
    else:
        mode = "scroll"
        clean["captions_enabled"] = True
        clean["caption_mode"] = False
        clean["caption_text_source"] = "script"
    bg_mode = str(clean.get("text_background_mode") or "").strip().lower()
    if bg_mode not in {"off", "whole", "follow"}:
        bg_mode = "whole" if bool(clean.get("text_background_enabled", False)) else "off"
    clean["text_background_mode"] = bg_mode
    clean["text_background_enabled"] = bg_mode != "off"
    if bg_mode in {"whole", "follow"}:
        clean["text_background_follow_text"] = True
    metadata = dict(clean.get("metadata") or {})
    metadata["web_text_render_mode"] = mode
    metadata["web_scrolling_background_mode"] = bg_mode
    clean["metadata"] = metadata
    return clean


def _renderer_needs_transcription(settings: dict[str, Any]) -> bool:
    return bool(settings.get("captions_enabled", True)) and bool(settings.get("caption_mode", True))


def _apply_web_settings(
    principal: Principal,
    preset: dict[str, Any],
    *,
    fg_library_id: str = "",
    voice_mode: str = "upload_audio",
    item_order: int = 1,
) -> dict[str, Any]:
    settings = project_defaults()
    settings.update(normalize_project_settings(dict(preset.get("settings") or {})))
    settings = _normalize_web_text_render_settings(settings)
    metadata = dict(settings.get("metadata") or {})
    global_settings = load_global_settings()
    selected_fg = fg_library_id or str(preset.get("fg_library_id") or "")
    fg = _resolve_fg_library(selected_fg) if selected_fg else None
    if fg:
        # Proven desktop behavior: one sorted foreground image is frozen per
        # queue item, cycling through the reusable VPS folder by item order.
        chosen_fg = _select_fg_image(fg, item_order)
        settings["foreground_enabled"] = True
        settings["foreground_use_folder_rotation"] = False
        settings["foreground_folder_path"] = str(fg["path"])
        settings["foreground_image_path"] = str(chosen_fg)
    else:
        chosen_fg = None
        settings["foreground_enabled"] = False
        settings["foreground_use_folder_rotation"] = False
        settings["foreground_folder_path"] = ""
        settings["foreground_image_path"] = ""
    visual_plan = _load_visual_plan(principal, str(preset.get("id") or "preset"))
    auto_pexels_media = bool(preset.get("auto_pexels_media", True))
    if auto_pexels_media and visual_plan.get("plan_ready"):
        preset_library = Path(str(visual_plan.get("library_path") or ""))
        preset_library.mkdir(parents=True, exist_ok=True)
        settings["pexels_library_folder"] = str(preset_library)
        settings["pexels_use_library"] = True
        settings["pexels_query"] = ", ".join(list(visual_plan.get("queries") or [])[:600])
    else:
        settings["pexels_library_folder"] = str(PEXELS_LIBRARY)
        settings["pexels_use_library"] = True
    metadata.update({
        "web_flow_v1": True,
        "web_flow_version": WEB_FLOW_VERSION,
        "web_flow_owner": principal.username,
        "web_preset_id": str(preset.get("id") or ""),
        "web_preset_name": str(preset.get("name") or "Preset"),
        "web_fg_library_id": selected_fg,
        "web_fg_selected_image": chosen_fg.name if chosen_fg is not None else "",
        "web_voice_mode": voice_mode,
        "web_font_mode": "manual" if str(preset.get("font_mode") or "auto").lower() == "manual" else "auto",
        "web_manual_font_family": str(settings.get("font_family") or ""),
        "web_preset_visual_plan": bool(auto_pexels_media and visual_plan.get("plan_ready")),
        "web_auto_pexels_media": auto_pexels_media,
        "web_preset_pexels_library": str(settings.get("pexels_library_folder") or "") if auto_pexels_media and visual_plan.get("plan_ready") else "",
        "web_preset_visual_query_count": int(visual_plan.get("query_count") or 0),
        "web_queue_mode_at_submit": str(global_settings.get("queue_mode") or "normal"),
        "web_drive_upload_enabled": bool(metadata.get("web_drive_upload_enabled", True)),
        "web_cleanup_after_verify": True,
        "web_drive_verify_required": True,
        "web_text_render_mode": str((settings.get("metadata") or {}).get("web_text_render_mode") or "scroll"),
    })
    settings["metadata"] = metadata
    return _assign_music(settings)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_project_name(value: str) -> str:
    text = re.sub(r'[\\/:*?"<>|]+', "-", str(value or "").strip())
    text = re.sub(r"\s+", " ", text).strip(" .")
    if not text:
        raise HTTPException(400, "Project name is empty")
    return text[:120]


def _new_project(project_name: str, principal: Principal) -> tuple[str, Path]:
    project_id = uuid.uuid4().hex
    created_at = now_iso()
    root = projects_root() / project_id
    for folder in ("input", "temp", "output", "preview", "logs", "artifacts"):
        (root / folder).mkdir(parents=True, exist_ok=True)
    metadata = {
        "id": project_id,
        "name": project_name,
        "owner_id": principal.username,
        "status": "created",
        "created_at": created_at,
        "web_flow_v1": True,
    }
    (root / "project.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
    with connect() as db:
        db.execute(
            "INSERT INTO projects(id,name,owner_id,status,created_at,project_path) VALUES(?,?,?,'created',?,?)",
            (project_id, project_name, principal.username, created_at, str(root)),
        )
        db.commit()
    return project_id, root


def _validate_audio_file(path: Path) -> None:
    if path.suffix.lower() not in AUDIO_EXTENSIONS:
        raise HTTPException(400, f"Unsupported audio extension: {path.suffix}")
    if path.stat().st_size <= 0:
        raise HTTPException(400, "Audio file is empty")
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, check=False,
        )
    except Exception as exc:
        raise HTTPException(409, f"Unable to validate audio: {exc}") from exc
    if result.returncode != 0:
        raise HTTPException(409, "FFprobe rejected uploaded audio")
    try:
        if float((result.stdout or "0").strip()) <= 0:
            raise ValueError
    except ValueError:
        raise HTTPException(409, "Uploaded audio duration is invalid")


def _is_web_flow_job(job: dict[str, Any]) -> bool:
    metadata = dict((job.get("settings") or {}).get("metadata") or {})
    return bool(metadata.get("web_flow_v1"))


def _batch_for_principal(batch_id: str, principal: Principal):
    with connect() as db:
        row = db.execute("SELECT * FROM upload_batches WHERE id=?", (batch_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Upload session not found")
    if not principal.is_admin and str(row["owner_id"]) != principal.username:
        raise HTTPException(403, "Upload session access denied")
    return dict(row)


def _algrow_config() -> dict[str, Any]:
    try:
        data = json.loads(ALGROW_CONFIG.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(503, f"Algrow sidecar config is unavailable: {exc}") from exc
    if not isinstance(data, dict):
        raise HTTPException(503, "Algrow sidecar config is invalid")
    return data


def _canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _algrow_ingress_submit(
    *,
    principal: Principal,
    batch: dict[str, Any],
    item_order: int,
    project_name: str,
    script_bytes: bytes,
    preset: dict[str, Any],
    fg_library_id: str,
    voice_profile: dict[str, Any],
) -> dict[str, Any]:
    config = _algrow_config()
    ingress_root = Path(str(config.get("ingress_dir") or ""))
    if not ingress_root:
        raise HTTPException(503, "Algrow ingress directory is not configured")
    ingress_root.mkdir(parents=True, exist_ok=True)
    project_id = uuid.uuid4().hex
    target = ingress_root / project_id
    if target.exists():
        raise HTTPException(409, "Algrow project collision")
    source = target / "source"
    source.mkdir(parents=True, exist_ok=False)
    script_path = source / f"{project_name}.txt"
    script_path.write_bytes(script_bytes)
    settings = _apply_web_settings(principal, preset, fg_library_id=fg_library_id, voice_mode="algrow_voice", item_order=item_order)
    # The sidecar handoff will inject the generated audio path later.
    settings["audio_path"] = ""
    source_mode = "text_off" if not bool(settings.get("captions_enabled", True)) else (
        "scrolling_text" if not bool(settings.get("caption_mode", True)) else (
            "automatic_transcription" if str(settings.get("caption_text_source") or "whisper") == "whisper" else "script_captions"
        )
    )
    record = {
        "role": "script",
        "relative_path": f"source/{script_path.name}",
        "size": script_path.stat().st_size,
        "sha256": _sha256(script_path),
    }
    manifest = {
        "manifest_version": 2,
        "project_id": project_id,
        "owner_id": principal.username,
        "project_name": project_name,
        "category": str(batch.get("category") or "Uncategorized"),
        "source_mode": source_mode,
        "created_at": now_iso(),
        "settings": settings,
        "files": [record],
        "submission_type": "algrow_voice",
        "batch_id": str(batch["id"]),
        "batch_expected_count": int(batch["expected_count"]),
        "item_order": max(1, int(item_order)),
        "expected_voice_basename": project_name,
        "voice_profile": voice_profile,
        "material_contract": "web_flow_txt_to_algrow",
    }
    manifest_bytes = _canonical_json_bytes(manifest)
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    (target / "manifest.json").write_bytes(manifest_bytes)
    marker = {
        "project_id": project_id,
        "manifest_sha256": manifest_sha,
        "file_count": 1,
        "total_bytes": script_path.stat().st_size,
        "completed_at": now_iso(),
    }
    # Publish marker last; Sidecar treats it as the complete-folder contract.
    (target / "upload_complete.json").write_bytes(_canonical_json_bytes(marker))
    return {"project_id": project_id, "status": "algrow_queued", "project_name": project_name}


class PresetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    clone_from: str = ""


class PresetPatch(BaseModel):
    name: str | None = None
    settings: dict[str, Any] | None = None
    fg_library_id: str | None = None
    voice_mode: str | None = None
    voice_profile_id: str | None = None
    font_mode: str | None = None
    auto_pexels_media: bool | None = None
    preview_approved: bool | None = None


class AutoPexelsPayload(BaseModel):
    enabled: bool = True


class UserStatePatch(BaseModel):
    selected_preset_id: str | None = None
    upload_voice_mode: str | None = None
    upload_categories: list[str] | None = None
    selected_upload_category: str | None = None


class QueueSettingsPatch(BaseModel):
    queue_mode: str | None = None
    scene_builder_priority: bool | None = None
    paused: bool | None = None


class FgLibraryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    path: str = ""
    create: bool = False


class VoiceProfilePayload(BaseModel):
    id: str = ""
    name: str
    language: str
    voice_id: str
    model: str = "Multilingual v2"
    stability: int = 50
    similarity: int = 75
    style: int = 0
    speed: float = 1.0
    speaker_boost: bool = True
    enabled: bool = True


class PexelsKeyPayload(BaseModel):
    key: str = Field(min_length=8, max_length=500)


class MusicPathPayload(BaseModel):
    path: str


class UploadSessionPayload(BaseModel):
    expected_count: int = Field(ge=1, le=5000)
    preset_id: str
    voice_mode: str = "upload_audio"
    category: str = "Web Flow"


class UploadFinalizePayload(BaseModel):
    expected_count: int = Field(ge=1, le=5000)


class DriveFolderScanPayload(BaseModel):
    folder: str = Field(min_length=1, max_length=1000)


class DeferredDriveItemPayload(BaseModel):
    base: str = Field(min_length=1, max_length=180)
    txt_remote: str = Field(min_length=1, max_length=2000)
    audio_remote: str = Field(min_length=1, max_length=2000)
    fg_library_id: str = ""


class DeferredDriveBatchPayload(BaseModel):
    folder: str = Field(min_length=1, max_length=1000)
    preset_id: str
    category: str = "Web Flow"
    items: list[DeferredDriveItemPayload]


class JobMovePayload(BaseModel):
    direction: str


class JobFgPayload(BaseModel):
    fg_library_id: str = ""


class CleanupConfirm(BaseModel):
    confirm: str


@router.get("/bootstrap")
def bootstrap(principal: Principal = Depends(get_current_principal)) -> dict[str, Any]:
    initialize_storage()
    pexels_files = [p for p in PEXELS_LIBRARY.rglob("*") if p.is_file()] if PEXELS_LIBRARY.is_dir() else []
    image_count = sum(1 for p in pexels_files if p.suffix.lower() in IMAGE_EXTENSIONS)
    video_count = sum(1 for p in pexels_files if p.suffix.lower() in VIDEO_EXTENSIONS)
    key_data = _load_json(PEXELS_KEYS_FILE, {})
    keys = key_data.get("keys", []) if isinstance(key_data, dict) else key_data
    music_cfg = _music_config()
    music_files = _music_files(Path(music_cfg["path"]))
    return {
        "ok": True,
        "version": WEB_FLOW_VERSION,
        "principal": {"username": principal.username, "is_admin": principal.is_admin},
        "user_state": _load_user_state(principal),
        "queue_settings": load_global_settings(),
        "presets": _load_presets(principal),
        "fg_libraries": [_fg_details(x) for x in _fg_registry()],
        "voice_profiles": _voice_profiles(),
        "fonts": _font_bootstrap(),
        "pexels": {
            "path": str(PEXELS_LIBRARY),
            "file_count": len(pexels_files),
            "image_count": image_count,
            "video_count": video_count,
            "size_bytes": sum(p.stat().st_size for p in pexels_files),
            "key_count": len(keys) if isinstance(keys, list) else 0,
            "config": _load_json(PEXELS_CONFIG_FILE, {}),
        },
        "music": {
            "path": music_cfg["path"],
            "file_count": len(music_files),
            "files": [p.name for p in music_files[:50]],
        },
        "preview_reference": _preview_reference_status(),
    }



@router.get("/preview-reference")
def preview_reference(_principal: Principal = Depends(get_current_principal)) -> dict[str, Any]:
    return {"ok": True, **_preview_reference_status()}


@router.get("/preview-reference/audio-file")
def preview_reference_audio_file(_principal: Principal = Depends(get_current_principal)):
    if not PREVIEW_REFERENCE_AUDIO.is_file():
        raise HTTPException(404, "Preview audio is unavailable")
    return FileResponse(str(PREVIEW_REFERENCE_AUDIO), media_type="audio/wav", headers={"Cache-Control": "no-store, max-age=0", "Content-Disposition": 'inline; filename="preview_reference.wav"'})


@router.post("/preview-reference/audio")
def upload_preview_reference_audio(audio_file: UploadFile = File(...), principal: Principal = Depends(get_current_principal)) -> dict[str, Any]:
    if not principal.is_admin:
        raise HTTPException(403, "Administrator access required to replace the permanent preview audio")
    suffix = Path(audio_file.filename or "reference.wav").suffix.lower()
    if suffix not in AUDIO_EXTENSIONS:
        raise HTTPException(400, "Unsupported preview audio format")
    PREVIEW_REFERENCE_ROOT.mkdir(parents=True, exist_ok=True)
    tmp = PREVIEW_REFERENCE_ROOT / f".reference_audio_{uuid.uuid4().hex}{suffix}"
    try:
        with tmp.open("wb") as handle: shutil.copyfileobj(audio_file.file, handle)
        _validate_audio_file(tmp)
        duration = _audio_duration(tmp)
        if duration < 1:
            raise HTTPException(409, "Preview audio must contain at least 1 second of valid audio")
        result = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(tmp), "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(PREVIEW_REFERENCE_AUDIO)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=900, check=False)
        if result.returncode != 0:
            raise HTTPException(409, "Unable to normalize permanent preview audio: " + (result.stderr or "")[-800:])
        os.chmod(PREVIEW_REFERENCE_AUDIO, 0o640)
        _ensure_preview_script()
        _audit(principal, "preview_reference_audio", {"seconds": _audio_duration(PREVIEW_REFERENCE_AUDIO)})
        return {"ok": True, **_preview_reference_status()}
    finally:
        tmp.unlink(missing_ok=True)


@router.get("/presets/{preset_id}/preview/status")
def preset_preview_status(preset_id: str, principal: Principal = Depends(get_current_principal)) -> dict[str, Any]:
    _, preset = _find_preset(principal, preset_id)
    return _preview_status_payload(principal, preset)


@router.post("/presets/{preset_id}/preview/render")
def render_preset_preview(preset_id: str, principal: Principal = Depends(get_current_principal)) -> dict[str, Any]:
    """Mark a browser Live Preview as current without creating a production job."""
    _, preset = _find_preset(principal, preset_id)
    reference = _preview_reference_status()
    if not reference["audio_ready"]:
        raise HTTPException(409, "Set Preview Audio first.")
    current_hash = _preview_hash(principal, preset)
    state = _load_preview_state(principal, preset_id)
    state.update({
        "preset_id": preset_id,
        "preview_hash": current_hash,
        "last_preview_hash": current_hash,
        "preview_status": "ready",
        "preview_progress": 100,
        "preview_message": "Browser Live Preview is ready; no production render was started.",
        "preview_path": "",
        "active_job_id": "",
        "completed_job_id": "",
        "completed_hash": "",
        "pending_hash": "",
    })
    _save_preview_state(principal, preset_id, state)
    _audit(principal, "preset_live_preview_ready", {"preset_id": preset_id, "preview_hash": current_hash, "production_job": False})
    return {"ok": True, "status": "ready", **_preview_status_payload(principal, preset)}
@router.get("/presets/{preset_id}/preview/video")
def preset_preview_video(preset_id: str, principal: Principal = Depends(get_current_principal)):
    """There is no server MP4 for browser Live Preview."""
    _find_preset(principal, preset_id)
    raise HTTPException(404, "Live Preview is browser-rendered; no production preview video exists.")
@router.put("/user-state")
def put_user_state(payload: UserStatePatch, principal: Principal = Depends(get_current_principal)) -> dict[str, Any]:
    current = _load_user_state(principal)
    patch = _model_payload(payload, exclude_none=True)
    current.update(patch)
    saved = _save_user_state(principal, current)
    return {"ok": True, "state": saved}


@router.put("/queue-settings")
def put_queue_settings(payload: QueueSettingsPatch, principal: Principal = Depends(get_current_principal)) -> dict[str, Any]:
    return {"ok": True, "settings": _save_global_settings(principal, _model_payload(payload, exclude_none=True))}


@router.post("/queue/pause")
def pause_new_flow(principal: Principal = Depends(get_current_principal)) -> dict[str, Any]:
    return {"ok": True, "settings": _save_global_settings(principal, {"paused": True})}


@router.post("/queue/resume")
def resume_new_flow(principal: Principal = Depends(get_current_principal)) -> dict[str, Any]:
    return {"ok": True, "settings": _save_global_settings(principal, {"paused": False})}


@router.get("/queue")
def web_queue(principal: Principal = Depends(get_current_principal)) -> dict[str, Any]:
    jobs = list_jobs(owner_id=principal.username, include_all_owners=principal.is_admin)
    rows = [job for job in jobs if _is_web_flow_job(job) and not bool(dict((job.get("settings") or {}).get("metadata") or {}).get("web_preview_reference"))]
    counts = {"waiting": 0, "processing": 0, "delivered": 0, "attention": 0}
    terminal_attention = {"failed", "interrupted"}
    for job in rows:
        status = str(job.get("status") or "")
        if status == "completed":
            counts["delivered"] += 1
        elif status in terminal_attention:
            counts["attention"] += 1
        elif status == "running":
            counts["processing"] += 1
        elif status not in {"cancelled"}:
            counts["waiting"] += 1
        metadata = dict((job.get("settings") or {}).get("metadata") or {})
        job["web_preset_name"] = str(metadata.get("web_preset_name") or "")
        job["web_fg_library_id"] = str(metadata.get("web_fg_library_id") or "")
        job["web_voice_mode"] = str(metadata.get("web_voice_mode") or "")
    visible_rows = rows[:100]
    with connect() as db:
        runtime = queue_runtime_state(db)
        job_ids = [str(job.get("id") or "") for job in visible_rows if str(job.get("id") or "")]
        size_by_job: dict[str, int] = {}
        if job_ids:
            placeholders = ",".join("?" for _ in job_ids)
            size_rows = db.execute(
                f"SELECT job_id,audio_size FROM batch_items WHERE job_id IN ({placeholders})",
                tuple(job_ids),
            ).fetchall()
            size_by_job = {str(row["job_id"]): max(0, int(row["audio_size"] or 0)) for row in size_rows}
    for job in visible_rows:
        job["voice_size_bytes"] = size_by_job.get(str(job.get("id") or ""), 0)
    return {"ok": True, "counts": counts, "jobs": visible_rows, "queue_settings": load_global_settings(), "runtime": runtime}


@router.post("/queue/jobs/{job_id}/cancel")
def cancel_web_job(job_id: str, principal: Principal = Depends(get_current_principal)) -> dict[str, Any]:
    job = job_for_principal(job_id, principal)
    decoded = get_job(job_id)
    if not _is_web_flow_job(decoded):
        raise HTTPException(409, "This is not a New Web Flow job")
    _audit(principal, "cancel_job", {"job_id": job_id})
    return request_cancel(job_id)


@router.post("/queue/jobs/{job_id}/retry")
def retry_web_job(job_id: str, principal: Principal = Depends(get_current_principal)) -> dict[str, Any]:
    job_for_principal(job_id, principal)
    decoded = get_job(job_id)
    if not _is_web_flow_job(decoded):
        raise HTTPException(409, "This is not a New Web Flow job")
    _audit(principal, "retry_job", {"job_id": job_id})
    return retry_job(job_id)


@router.post("/queue/jobs/{job_id}/move")
def move_web_job(job_id: str, payload: JobMovePayload, principal: Principal = Depends(get_current_principal)) -> dict[str, Any]:
    job_for_principal(job_id, principal)
    job = get_job(job_id)
    if not _is_web_flow_job(job) or str(job.get("status")) not in {"queued", "held"}:
        raise HTTPException(409, "Only waiting New Web Flow jobs can be reordered")
    direction = payload.direction.strip().lower()
    if direction not in {"up", "down"}:
        raise HTTPException(400, "direction must be up or down")
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        batch_id = str(job.get("batch_id") or "")
        if batch_id:
            current = db.execute("SELECT * FROM batch_items WHERE job_id=?", (job_id,)).fetchone()
            if current is None:
                raise HTTPException(409, "Batch item not found")
            op = "<" if direction == "up" else ">"
            order = "DESC" if direction == "up" else "ASC"
            other = db.execute(
                f"SELECT * FROM batch_items WHERE batch_id=? AND item_order {op} ? ORDER BY item_order {order} LIMIT 1",
                (batch_id, int(current["item_order"])),
            ).fetchone()
            if other:
                db.execute("UPDATE batch_items SET item_order=? WHERE id=?", (int(other["item_order"]), str(current["id"])))
                db.execute("UPDATE batch_items SET item_order=? WHERE id=?", (int(current["item_order"]), str(other["id"])))
        else:
            current_seq = int(job.get("queue_sequence") or 0)
            op = "<" if direction == "up" else ">"
            order = "DESC" if direction == "up" else "ASC"
            other = db.execute(
                f"SELECT id,queue_sequence FROM jobs WHERE status='queued' AND queue_sequence {op} ? ORDER BY queue_sequence {order} LIMIT 1",
                (current_seq,),
            ).fetchone()
            if other:
                db.execute("UPDATE jobs SET queue_sequence=? WHERE id=?", (int(other["queue_sequence"]), job_id))
                db.execute("UPDATE jobs SET queue_sequence=? WHERE id=?", (current_seq, str(other["id"])))
        db.commit()
    return {"ok": True}


@router.put("/queue/jobs/{job_id}/fg")
def change_waiting_fg(job_id: str, payload: JobFgPayload, principal: Principal = Depends(get_current_principal)) -> dict[str, Any]:
    job_for_principal(job_id, principal)
    job = get_job(job_id)
    if not _is_web_flow_job(job) or str(job.get("status")) not in {"queued", "held"}:
        raise HTTPException(409, "FG can only be changed before this New Web Flow job starts")
    fg = _resolve_fg_library(payload.fg_library_id) if payload.fg_library_id else None
    settings = dict(job.get("settings") or {})
    metadata = dict(settings.get("metadata") or {})
    metadata["web_fg_library_id"] = payload.fg_library_id
    item_order = 1
    with connect() as lookup_db:
        batch_item = lookup_db.execute("SELECT item_order FROM batch_items WHERE job_id=?", (job_id,)).fetchone()
        if batch_item is not None:
            item_order = max(1, int(batch_item["item_order"] or 1))
    chosen = _select_fg_image(fg, item_order) if fg else None
    metadata["web_fg_selected_image"] = chosen.name if chosen is not None else ""
    settings["metadata"] = metadata
    settings["foreground_enabled"] = bool(fg)
    settings["foreground_use_folder_rotation"] = False
    settings["foreground_folder_path"] = str(fg["path"]) if fg else ""
    settings["foreground_image_path"] = str(chosen) if chosen is not None else ""
    with connect() as db:
        db.execute("UPDATE jobs SET settings_json=?,updated_at=? WHERE id=?", (json.dumps(settings, ensure_ascii=False, separators=(",", ":")), now_iso(), job_id))
        project = db.execute("SELECT project_path FROM projects WHERE id=?", (job["project_id"],)).fetchone()
        db.commit()
    if project:
        save_settings(Path(str(project["project_path"])), settings)
    return {"ok": True, "job": get_job(job_id)}


@router.post("/presets")
def create_preset(payload: PresetCreate, principal: Principal = Depends(get_current_principal)) -> dict[str, Any]:
    presets = _load_presets(principal)
    source = next((x for x in presets if str(x.get("id")) == payload.clone_from), None)
    settings = dict(source.get("settings") or {}) if source else _default_preset_settings("Documentary Scroll")
    item = {
        "id": uuid.uuid4().hex,
        "name": payload.name.strip(),
        "settings": normalize_project_settings(settings),
        "fg_library_id": str(source.get("fg_library_id") or "") if source else "",
        "voice_mode": str(source.get("voice_mode") or "upload_audio") if source else "upload_audio",
        "voice_profile_id": str(source.get("voice_profile_id") or "") if source else "",
        "font_mode": ("manual" if str(source.get("font_mode") or "auto").lower() == "manual" else "auto") if source else "auto",
        "auto_pexels_media": bool(source.get("auto_pexels_media", True)) if source else True,
        "preview_approved": bool(source.get("preview_approved", False)) if source else False,
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    presets.append(item)
    _save_presets(principal, presets)
    _audit(principal, "create_preset", {"preset_id": item["id"], "name": item["name"]})
    return {"ok": True, "preset": item}


@router.put("/presets/{preset_id}")
def update_preset(preset_id: str, payload: PresetPatch, principal: Principal = Depends(get_current_principal)) -> dict[str, Any]:
    presets, item = _find_preset(principal, preset_id)
    patch = _model_payload(payload, exclude_none=True)
    if "name" in patch:
        name = str(patch["name"]).strip()
        if not name:
            raise HTTPException(400, "Preset name cannot be empty")
        item["name"] = name[:120]
    if "settings" in patch:
        current = dict(item.get("settings") or {})
        current.update(normalize_project_settings(dict(patch["settings"] or {})))
        item["settings"] = _normalize_web_text_render_settings(current)
    for key in ("fg_library_id", "voice_mode", "voice_profile_id"):
        if key in patch:
            item[key] = patch[key]
    if "font_mode" in patch:
        item["font_mode"] = "manual" if str(patch.get("font_mode") or "auto").lower() == "manual" else "auto"
    if "auto_pexels_media" in patch:
        item["auto_pexels_media"] = bool(patch["auto_pexels_media"])
    if str(item.get("id") or "") == "documentary-scroll":
        item["profile_version"] = DOCUMENTARY_SCROLL_PROFILE_VERSION
        item["profile_info"] = _documentary_scroll_profile_info(dict(item.get("settings") or {}))
    if "preview_approved" in patch:
        requested = bool(patch["preview_approved"])
        state = _load_preview_state(principal, str(item["id"]))
        if requested:
            status = _preview_status_payload(principal, item)
            if not status["is_current"]:
                raise HTTPException(409, "Click Live Preview before approval.")
            # Keep the approval snapshot separate from reusable preset data.
            snapshot_keys = (
                "id", "name", "settings", "fg_library_id", "voice_mode",
                "voice_profile_id", "font_mode", "auto_pexels_media",
            )
            state["approved_snapshot"] = {k: item.get(k) for k in snapshot_keys}
            state["approved_hash"] = status["preset_hash"]
            state["approved_at"] = now_iso()
        else:
            state.pop("approved_snapshot", None)
            state.pop("approved_hash", None)
            state.pop("approved_at", None)
        _save_preview_state(principal, str(item["id"]), state)
        item["preview_approved"] = requested
    item["updated_at"] = now_iso()
    _save_presets(principal, presets)
    return {"ok": True, "preset": item}


@router.delete("/presets/{preset_id}")
def delete_preset(preset_id: str, principal: Principal = Depends(get_current_principal)) -> dict[str, Any]:
    presets, item = _find_preset(principal, preset_id)
    if len(presets) <= 1:
        raise HTTPException(409, "At least one preset must remain")
    retained = [x for x in presets if str(x.get("id")) != preset_id]
    _save_presets(principal, retained)
    _audit(principal, "delete_preset", {"preset_id": preset_id, "name": item.get("name")})
    return {"ok": True}


@router.get("/fg-libraries")
def fg_libraries(_principal: Principal = Depends(get_current_principal)) -> dict[str, Any]:
    return {"ok": True, "libraries": [_fg_details(x) for x in _fg_registry()]}


@router.post("/fg-libraries/validate")
def validate_fg_library(payload: FgLibraryCreate, principal: Principal = Depends(get_current_principal)) -> dict[str, Any]:
    if not principal.is_admin:
        raise HTTPException(403, "Administrator access required to validate shared FG libraries")
    if payload.path.strip():
        path = Path(payload.path).expanduser()
        if not path.is_absolute() or _unsafe_system_path(path):
            raise HTTPException(400, "FG library must be a safe absolute VPS folder")
    else:
        path = DEFAULT_FG_ROOT / re.sub(r"[^A-Za-z0-9._-]+", "_", payload.name.strip()).strip("._-")
    if not path.is_dir():
        raise HTTPException(409, "FG library folder does not exist")
    probe = {"id": "validation", "name": payload.name.strip() or path.name, "path": str(path.resolve()), "protected": True}
    return {"ok": True, "library": _fg_details(probe)}


@router.post("/fg-libraries")
def add_fg_library(payload: FgLibraryCreate, principal: Principal = Depends(get_current_principal)) -> dict[str, Any]:
    if not principal.is_admin:
        raise HTTPException(403, "Administrator access required to add shared FG libraries")
    name = payload.name.strip()
    if payload.path.strip():
        path = Path(payload.path).expanduser()
        if not path.is_absolute():
            raise HTTPException(400, "VPS FG path must be absolute")
        if _unsafe_system_path(path):
            raise HTTPException(400, "That system path cannot be registered as an FG library")
        if not path.exists() and payload.create:
            # Arbitrary paths are never auto-created; creation is restricted to the protected default root.
            raise HTTPException(400, "Create new FG folders under the protected ACS FG root, or create the VPS folder manually first")
    else:
        path = DEFAULT_FG_ROOT / re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._-")
        path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise HTTPException(409, "FG library folder does not exist")
    registry = _fg_registry()
    resolved = path.resolve()
    if any(Path(str(x.get("path") or "")).resolve() == resolved for x in registry if str(x.get("path") or "")):
        raise HTTPException(409, "That VPS folder is already registered")
    item = {"id": uuid.uuid4().hex, "name": name, "path": str(resolved), "protected": True, "created_at": now_iso()}
    registry.append(item)
    _atomic_json(FG_CONFIG, registry)
    _audit(principal, "add_fg_library", item)
    return {"ok": True, "library": _fg_details(item)}


@router.delete("/fg-libraries/{library_id}")
def remove_fg_library(library_id: str, principal: Principal = Depends(get_current_principal)) -> dict[str, Any]:
    if not principal.is_admin:
        raise HTTPException(403, "Administrator access required to remove shared FG libraries")
    registry = _fg_registry()
    item = next((x for x in registry if str(x.get("id")) == library_id), None)
    if item is None:
        raise HTTPException(404, "FG library not found")
    if library_id == "shared-fg-assets":
        raise HTTPException(409, "The legacy shared FG root is protected from unregistering")
    # Removal means unregister only. Files intentionally stay on VPS.
    retained = [x for x in registry if str(x.get("id")) != library_id]
    _atomic_json(FG_CONFIG, retained)
    _audit(principal, "remove_fg_library", {"library_id": library_id, "path": item.get("path"), "files_deleted": False})
    return {"ok": True, "files_deleted": False}


@router.get("/fg-libraries/{library_id}/preview")
def fg_library_preview(library_id: str, _principal: Principal = Depends(get_current_principal)):
    item = _resolve_fg_library(library_id)
    assert item is not None
    root = Path(str(item["path"]))
    files = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS)
    if not files:
        raise HTTPException(404, "FG library has no previewable image")
    path = files[0]
    media = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    return FileResponse(str(path), media_type=media)



class FontDetectPayload(BaseModel):
    text: str = ""
    language: str = "auto"


@router.post("/fonts/detect")
def detect_font(payload: FontDetectPayload, _principal: Principal = Depends(get_current_principal)) -> dict[str, Any]:
    families = _renderer_font_catalog()
    selected, script = choose_font_family(payload.text, payload.language, families)
    return {"ok": True, "script": script or detect_script(payload.text, payload.language), "family": selected, "available": bool(selected)}


def _preview_script_payload(principal: Principal, preset: dict[str, Any]) -> dict[str, Any]:
    preset_id = str(preset.get("id") or "")
    script = _preset_preview_script_status(principal, preset_id)
    visual = _load_visual_plan(principal, preset_id)
    return {
        **script,
        "auto_pexels_media": bool(preset.get("auto_pexels_media", True)),
        "query_count": int(visual.get("query_count") or 0),
        "cached_query_count": int(visual.get("cached_query_count") or 0),
        "cached_image_count": int(visual.get("cached_image_count") or 0),
        "missing_count": int(visual.get("missing_count") or 0),
        "prepare": visual.get("prepare") if isinstance(visual.get("prepare"), dict) else {},
    }


def _queue_auto_preset_media(background_tasks: BackgroundTasks, principal: Principal, preset: dict[str, Any]) -> bool:
    if not bool(preset.get("auto_pexels_media", True)):
        return False
    preset_id = str(preset.get("id") or "")
    status = _load_visual_plan(principal, preset_id)
    queries = list(status.get("queries") or [])
    if not queries or int(status.get("missing_count") or 0) <= 0:
        return False
    prepare = status.get("prepare") if isinstance(status.get("prepare"), dict) else {}
    if str(prepare.get("status") or "") in {"queued", "running"}:
        return False
    if not server_pexels_keys():
        return False
    root, _txt, _json, state_path = _preset_media_paths(principal, preset_id)
    state = {
        "status": "queued", "total": len(queries), "done": 0, "downloaded": 0, "cached": 0, "failed": 0,
        "message": "Auto Pexels media queued from preview script", "queued_at": now_iso(),
        "media_revision": str(prepare.get("media_revision") or ""),
    }
    _atomic_json(state_path, state)
    background_tasks.add_task(_prepare_preset_media_task, str(state_path), str(root / "library"), queries)
    return True


@router.get("/presets/{preset_id}/preview-script")
def preset_preview_script_status(preset_id: str, principal: Principal = Depends(get_current_principal)) -> dict[str, Any]:
    _, preset = _find_preset(principal, preset_id)
    return {"ok": True, "preset_id": preset_id, **_preview_script_payload(principal, preset)}


@router.get("/presets/{preset_id}/preview-script/text")
def preset_preview_script_text(preset_id: str, principal: Principal = Depends(get_current_principal)):
    _find_preset(principal, preset_id)
    path = _active_preview_script(principal, preset_id)
    return FileResponse(str(path), media_type="text/plain; charset=utf-8", headers={"Cache-Control": "no-store, max-age=0", "Content-Disposition": 'inline; filename="preview_script.txt"'})


@router.get("/presets/{preset_id}/preview/live-background")
def preset_live_background(preset_id: str, principal: Principal = Depends(get_current_principal)):
    _find_preset(principal, preset_id)
    root = _preset_media_root(principal, preset_id) / "library"
    candidates = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS) if root.is_dir() else []
    if not candidates and PEXELS_LIBRARY.is_dir():
        candidates = sorted(p for p in PEXELS_LIBRARY.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS)
    if not candidates:
        raise HTTPException(404, "No cached Pexels image is available for live preview")
    path = candidates[0]
    media = "image/png" if path.suffix.lower() == ".png" else "image/webp" if path.suffix.lower() == ".webp" else "image/jpeg"
    return FileResponse(str(path), media_type=media, headers={"Cache-Control": "no-store, max-age=0"})


@router.post("/presets/{preset_id}/preview-script")
def upload_preset_preview_script(
    preset_id: str,
    background_tasks: BackgroundTasks,
    script_file: UploadFile = File(...),
    principal: Principal = Depends(get_current_principal),
) -> dict[str, Any]:
    presets, preset = _find_preset(principal, preset_id)
    filename = Path(script_file.filename or "preview_script.txt").name
    if Path(filename).suffix.lower() != ".txt":
        raise HTTPException(400, "Preview script must be a .txt file")
    raw = script_file.file.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise HTTPException(413, "Preview script TXT is limited to 1 MB")
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        raise HTTPException(400, "Preview script TXT is empty")
    root, txt_path, json_path, state_path = _preset_media_paths(principal, preset_id)
    root.mkdir(parents=True, exist_ok=True)
    preview_script = _preset_preview_script_path(principal, preset_id)
    preview_script.write_text(text + "\n", encoding="utf-8")
    os.chmod(preview_script, 0o640)
    queries = _parse_visual_plan(text)
    txt_path.write_text(text + "\n", encoding="utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    _atomic_json(json_path, {
        "txt_name": filename, "sha256": digest, "queries": queries, "query_count": len(queries),
        "uploaded_at": now_iso(), "source": "preview_script",
    })
    old_state = _load_json(state_path, {})
    _atomic_json(state_path, {
        "status": "idle", "message": "Preview script loaded; Auto Pexels will fetch only missing media",
        "media_revision": str((old_state if isinstance(old_state, dict) else {}).get("media_revision") or ""),
    })
    preset["preview_approved"] = False
    preset["updated_at"] = now_iso()
    _save_presets(principal, presets)
    started = _queue_auto_preset_media(background_tasks, principal, preset)
    _audit(principal, "preset_preview_script", {
        "preset_id": preset_id, "script_name": filename, "script_chars": len(text),
        "query_count": len(queries), "auto_pexels_started": started,
    })
    return {"ok": True, "preset_id": preset_id, "auto_started": started, **_preview_script_payload(principal, preset)}


@router.put("/presets/{preset_id}/auto-pexels")
def set_preset_auto_pexels(
    preset_id: str,
    payload: AutoPexelsPayload,
    background_tasks: BackgroundTasks,
    principal: Principal = Depends(get_current_principal),
) -> dict[str, Any]:
    presets, preset = _find_preset(principal, preset_id)
    preset["auto_pexels_media"] = bool(payload.enabled)
    preset["preview_approved"] = False
    preset["updated_at"] = now_iso()
    _save_presets(principal, presets)
    started = _queue_auto_preset_media(background_tasks, principal, preset) if payload.enabled else False
    _audit(principal, "preset_auto_pexels", {"preset_id": preset_id, "enabled": bool(payload.enabled), "auto_started": started})
    return {"ok": True, "preset_id": preset_id, "auto_started": started, **_preview_script_payload(principal, preset)}


@router.get("/presets/{preset_id}/visual-plan")
def preset_visual_plan_status(preset_id: str, principal: Principal = Depends(get_current_principal)) -> dict[str, Any]:
    _, preset = _find_preset(principal, preset_id)
    return {"ok": True, "preset_id": preset_id, **_load_visual_plan(principal, preset_id)}


@router.post("/presets/{preset_id}/visual-plan")
def upload_preset_visual_plan(preset_id: str, visual_file: UploadFile = File(...), principal: Principal = Depends(get_current_principal)) -> dict[str, Any]:
    presets, preset = _find_preset(principal, preset_id)
    filename = Path(visual_file.filename or "visual_plan.txt").name
    if Path(filename).suffix.lower() != ".txt":
        raise HTTPException(400, "Preset visual plan must be a .txt file")
    raw = visual_file.file.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise HTTPException(413, "Preset visual TXT is limited to 1 MB")
    text = raw.decode("utf-8", errors="replace").strip()
    queries = _parse_visual_plan(text)
    if not queries:
        raise HTTPException(400, "No usable Pexels visual queries were detected in this TXT")
    root, txt_path, json_path, state_path = _preset_media_paths(principal, preset_id)
    root.mkdir(parents=True, exist_ok=True)
    txt_path.write_text(text + "\n", encoding="utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    _atomic_json(json_path, {"txt_name": filename, "sha256": digest, "queries": queries, "query_count": len(queries), "uploaded_at": now_iso()})
    old_state = _load_json(state_path, {})
    _atomic_json(state_path, {"status": "idle", "message": "Visual TXT loaded; prepare missing preset media when ready", "media_revision": str((old_state if isinstance(old_state, dict) else {}).get("media_revision") or "")})
    preset["preview_approved"] = False; preset["updated_at"] = now_iso(); _save_presets(principal, presets)
    _audit(principal, "preset_visual_plan", {"preset_id": preset_id, "query_count": len(queries), "sha256": digest})
    return {"ok": True, "preset_id": preset_id, **_load_visual_plan(principal, preset_id)}


@router.post("/presets/{preset_id}/visual-plan/prepare")
def prepare_preset_visual_plan(preset_id: str, background_tasks: BackgroundTasks, principal: Principal = Depends(get_current_principal)) -> dict[str, Any]:
    _, _preset = _find_preset(principal, preset_id)
    status = _load_visual_plan(principal, preset_id)
    queries = list(status.get("queries") or [])
    if not queries:
        raise HTTPException(409, "Upload a preset visual TXT first")
    root, _txt, _json, state_path = _preset_media_paths(principal, preset_id)
    prepare = status.get("prepare") if isinstance(status.get("prepare"), dict) else {}
    if str(prepare.get("status") or "") in {"queued", "running"}:
        return {"ok": True, "started": False, "already_running": True, **status}
    # Fast zero-hit completion when everything requested already exists locally.
    if int(status.get("missing_count") or 0) == 0:
        prepare = {"status": "completed", "total": len(queries), "done": len(queries), "downloaded": 0, "cached": len(queries), "failed": 0, "message": "All preset queries already cached; zero Pexels API calls needed", "finished_at": now_iso(), "media_revision": str(prepare.get("media_revision") or time.time_ns())}
        _atomic_json(state_path, prepare)
        return {"ok": True, "started": False, **_load_visual_plan(principal, preset_id)}
    if not server_pexels_keys():
        raise HTTPException(409, "No server Pexels API key is configured")
    state = {"status": "queued", "total": len(queries), "done": 0, "downloaded": 0, "cached": 0, "failed": 0, "message": "Preset media preparation queued", "queued_at": now_iso(), "media_revision": str(prepare.get("media_revision") or "")}
    _atomic_json(state_path, state)
    background_tasks.add_task(_prepare_preset_media_task, str(state_path), str(root / "library"), queries)
    _audit(principal, "preset_visual_prepare", {"preset_id": preset_id, "queries": len(queries), "missing": status.get("missing_count")})
    return {"ok": True, "started": True, **_load_visual_plan(principal, preset_id)}


@router.get("/voice-profiles")
def voice_profiles(_principal: Principal = Depends(get_current_principal)) -> dict[str, Any]:
    return {"ok": True, "profiles": _voice_profiles()}


@router.post("/voice-profiles")
def save_voice_profile(payload: VoiceProfilePayload, principal: Principal = Depends(get_current_principal)) -> dict[str, Any]:
    item = _validate_voice_profile(_model_payload(payload))
    values = [x for x in _voice_profiles() if str(x.get("id")) != item["id"]]
    values.append(item)
    _save_voice_profiles(values)
    _audit(principal, "save_voice_profile", {"profile_id": item["id"], "name": item["name"]})
    return {"ok": True, "profile": item}


@router.delete("/voice-profiles/{profile_id}")
def delete_voice_profile(profile_id: str, principal: Principal = Depends(get_current_principal)) -> dict[str, Any]:
    values = _voice_profiles()
    retained = [x for x in values if str(x.get("id")) != profile_id]
    if len(retained) == len(values):
        raise HTTPException(404, "Voice profile not found")
    _save_voice_profiles(retained)
    _audit(principal, "delete_voice_profile", {"profile_id": profile_id})
    return {"ok": True}


@router.put("/pexels/key")
def save_pexels_key(payload: PexelsKeyPayload, principal: Principal = Depends(get_current_principal)) -> dict[str, Any]:
    if not principal.is_admin:
        raise HTTPException(403, "Administrator access required to change the server Pexels key")
    key = payload.key.strip()
    raw = _load_json(PEXELS_KEYS_FILE, {})
    keys = raw.get("keys", []) if isinstance(raw, dict) else raw
    values = [str(x).strip() for x in keys if str(x).strip()] if isinstance(keys, list) else []
    if key not in values:
        values.append(key)
    _atomic_json(PEXELS_KEYS_FILE, {"keys": values})
    _audit(principal, "save_pexels_key", {"key_count": len(values)})
    return {"ok": True, "key_count": len(values)}


@router.get("/music-library")
def music_library(_principal: Principal = Depends(get_current_principal)) -> dict[str, Any]:
    cfg = _music_config()
    files = _music_files(Path(cfg["path"]))
    return {"ok": True, "path": cfg["path"], "file_count": len(files), "files": [p.name for p in files[:100]]}


@router.put("/music-library")
def set_music_library(payload: MusicPathPayload, principal: Principal = Depends(get_current_principal)) -> dict[str, Any]:
    if not principal.is_admin:
        raise HTTPException(403, "Administrator access required to change the shared music library")
    path = Path(payload.path).expanduser()
    if not path.is_absolute() or _unsafe_system_path(path) or not path.is_dir():
        raise HTTPException(400, "Music library must be an existing safe absolute VPS folder")
    cfg = _music_config()
    cfg["path"] = str(path.resolve())
    cfg["rotation_index"] = 0
    _atomic_json(MUSIC_CONFIG, cfg)
    _audit(principal, "music_library", {"path": cfg["path"]})
    return music_library(principal)


def _safe_drive_input_path(value: str, *, allow_file: bool = False) -> str:
    text = str(value or "").replace("\\", "/").strip().strip("/")
    if not text:
        raise HTTPException(400, "Drive folder is required")
    parts = [part.strip() for part in text.split("/") if part.strip()]
    if not parts or any(part in {".", ".."} or "\x00" in part or ":" in part for part in parts):
        raise HTTPException(400, "Invalid Drive path")
    if parts[0].casefold() == "automation_master_system":
        raise HTTPException(403, "Master maintenance folders cannot be used as ACS input")
    clean = "/".join(parts)
    if len(clean) > 1000:
        raise HTTPException(400, "Drive path is too long")
    if not allow_file and Path(clean).suffix:
        # Folder names may contain dots, so this is only a hint; do not reject.
        pass
    return clean


_DRIVE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,200}$")
_DRIVE_URL_PATTERNS = (
    re.compile(r"(?:https?://)?drive\.google\.com/drive/(?:u/\d+/)?folders/([A-Za-z0-9_-]{20,200})", re.I),
    re.compile(r"(?:https?://)?drive\.google\.com/(?:open|uc)\?[^#]*\bid=([A-Za-z0-9_-]{20,200})", re.I),
)
_DRIVE_ID_PREFIX = "driveid:"


def _parse_drive_folder_source(value: str) -> dict[str, str]:
    raw = str(value or "").strip()
    if not raw:
        raise HTTPException(400, "Drive folder is required")
    for pattern in _DRIVE_URL_PATTERNS:
        match = pattern.search(raw)
        if match:
            folder_id = match.group(1)
            return {"kind": "id", "folder_id": folder_id, "path": "", "display": raw}
    if _DRIVE_ID_RE.fullmatch(raw):
        return {"kind": "id", "folder_id": raw, "path": "", "display": raw}
    path = _safe_drive_input_path(raw)
    return {"kind": "path", "folder_id": "", "path": path, "display": path}


def _encode_drive_id_file(folder_id: str, relative_path: str) -> str:
    rel = _safe_drive_input_path(relative_path, allow_file=True)
    if not _DRIVE_ID_RE.fullmatch(folder_id):
        raise HTTPException(400, "Invalid Drive folder ID")
    return f"{_DRIVE_ID_PREFIX}{folder_id}/{rel}"


def _parse_drive_file_source(value: str) -> dict[str, str]:
    raw = str(value or "").strip()
    if raw.startswith(_DRIVE_ID_PREFIX):
        body = raw[len(_DRIVE_ID_PREFIX):]
        folder_id, sep, relative = body.partition("/")
        if not sep or not _DRIVE_ID_RE.fullmatch(folder_id):
            raise HTTPException(400, "Invalid Drive file reference")
        return {"kind": "id", "folder_id": folder_id, "path": _safe_drive_input_path(relative, allow_file=True)}
    return {"kind": "path", "folder_id": "", "path": _safe_drive_input_path(raw, allow_file=True)}


def _drive_copyto(remote: str, source: dict[str, str], local_path: Path) -> None:
    if source["kind"] == "id":
        _acs_rclone([
            "copyto", "--drive-root-folder-id", source["folder_id"],
            f"{remote}:{source['path']}", str(local_path),
        ], timeout=600)
    else:
        _acs_rclone(["copyto", f"{remote}:{source['path']}", str(local_path)], timeout=600)


def _acs_rclone(args: list[str], *, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    try:
        config, remote, _root = _load_drive_runtime()
    except Exception as exc:
        raise HTTPException(503, f"ACS Drive runtime unavailable: {exc}") from exc
    command = [
        "rclone", "--config", config,
        "--tpslimit", "2", "--tpslimit-burst", "2",
        "--transfers", "1", "--checkers", "1",
        "--retries", "5", "--low-level-retries", "10",
        "--timeout", "600s", "--contimeout", "30s",
    ] + args
    try:
        result = subprocess.run(
            command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            check=False, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(504, "Drive operation exceeded 600 seconds") from exc
    if result.returncode:
        raise HTTPException(502, (result.stdout or "Drive operation failed")[-3000:])
    return result


@router.post("/upload/drive-scan")
def scan_drive_folder(payload: DriveFolderScanPayload, principal: Principal = Depends(get_current_principal)) -> dict[str, Any]:
    source = _parse_drive_folder_source(payload.folder)
    try:
        _config, remote, _root = _load_drive_runtime()
    except Exception as exc:
        raise HTTPException(503, f"ACS Drive runtime unavailable: {exc}") from exc
    if source["kind"] == "id":
        scan_args = [
            "lsjson", "--drive-root-folder-id", source["folder_id"], f"{remote}:",
            "--recursive", "--files-only", "--no-mimetype", "--no-modtime",
        ]
    else:
        scan_args = [
            "lsjson", f"{remote}:{source['path']}",
            "--recursive", "--files-only", "--no-mimetype", "--no-modtime",
        ]
    result = _acs_rclone(scan_args, timeout=600)
    try:
        entries = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise HTTPException(502, "Drive returned an invalid folder listing") from exc
    if not isinstance(entries, list):
        raise HTTPException(502, "Drive returned an invalid folder listing")

    buckets: dict[str, dict[str, Any]] = {}
    file_count = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        rel = str(entry.get("Path") or entry.get("Name") or "").replace("\\", "/").strip("/")
        if not rel:
            continue
        # Source lifecycle rule: files already moved into a Used folder are never
        # considered new submission candidates on later scans.
        if any(part.casefold() in {"used", ".acs_used"} for part in Path(rel).parts[:-1]):
            continue
        suffix = Path(rel).suffix.lower()
        if suffix != ".txt" and suffix not in AUDIO_EXTENSIONS:
            continue
        file_count += 1
        if file_count > 10000:
            raise HTTPException(413, "Drive folder contains more than 10,000 relevant TXT/audio files; split it into smaller input folders")
        parent = str(Path(rel).parent).replace("\\", "/")
        parent = "" if parent == "." else parent.strip("/")
        stem = Path(rel).stem
        key = f"{parent}/{stem}".casefold()
        bucket = buckets.setdefault(key, {"stem": stem, "txt": [], "audio": []})
        if source["kind"] == "id":
            full = _encode_drive_id_file(source["folder_id"], rel)
        else:
            full = f"{source['path']}/{rel}".strip("/")
        item = {
            "remote": full,
            "name": Path(rel).name,
            "size": int(entry.get("Size") or 0),
        }
        if suffix == ".txt":
            bucket["txt"].append(item)
        else:
            bucket["audio"].append(item)

    seen_stems: set[str] = set()
    items: list[dict[str, Any]] = []
    for bucket in buckets.values():
        stem = str(bucket["stem"])
        valid = False
        reason = ""
        key = stem.casefold()
        if key in seen_stems:
            reason = "Rejected: duplicate basename"
        elif len(bucket["txt"]) != 1:
            reason = "Rejected: multiple TXT" if bucket["txt"] else "Rejected: TXT missing"
        elif len(bucket["audio"]) != 1:
            reason = "Rejected: multiple audio" if bucket["audio"] else "Rejected: audio missing"
        else:
            valid = True
            seen_stems.add(key)
        txt = bucket["txt"][0] if bucket["txt"] else None
        audio = bucket["audio"][0] if bucket["audio"] else None
        items.append({
            "base": stem,
            "txt_remote": txt["remote"] if txt else "",
            "txt_name": txt["name"] if txt else "",
            "audio_remote": audio["remote"] if audio else "",
            "audio_name": audio["name"] if audio else "",
            "valid": valid,
            "result": "Ready" if valid else reason,
        })
    items.sort(key=lambda x: x["base"].casefold())
    source_label = source["display"]
    _audit(principal, "drive_folder_scan", {
        "folder": source_label, "source_kind": source["kind"],
        "folder_id": source["folder_id"], "files": file_count,
        "pairs": sum(1 for x in items if x["valid"]),
    })
    return {
        "ok": True, "folder": source_label, "source_kind": source["kind"],
        "folder_id": source["folder_id"], "file_count": file_count,
        "valid_pairs": sum(1 for x in items if x["valid"]),
        "rejected": sum(1 for x in items if not x["valid"]),
        "items": items,
    }



def _drive_used_subfolder(category: str, batch_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", str(category or "Batch").strip()).strip("._")[:48] or "Batch"
    return f"Used/{safe}_{batch_id[-10:]}"


def _deferred_drive_input_folder(folder: str) -> str:
    source = _parse_drive_folder_source(folder)
    return f"driveid:{source['folder_id']}" if source["kind"] == "id" else str(source.get("path") or "")


@router.post("/upload/drive-batch")
def create_deferred_drive_batch(payload: DeferredDriveBatchPayload, principal: Principal = Depends(get_current_principal)) -> dict[str, Any]:
    """Persist a Drive batch without copying its media to the VPS.

    Every project/job name and immutable settings snapshot is created now, but
    source bytes remain on Drive. The persistent prefetcher later maintains
    exactly one active project plus one locally staged waiting project.
    """
    initialize_storage()
    ensure_drive_batch_schema()
    if not payload.items or len(payload.items) > 500:
        raise HTTPException(400, "Drive batch must contain between 1 and 500 projects")
    _, preset = _find_preset(principal, payload.preset_id)
    preview_status = _preview_status_payload(principal, preset)
    if not bool(preset.get("preview_approved", False)) or not bool(preview_status.get("is_current")):
        raise HTTPException(409, "Selected preset must have a current rendered preview approved before submission")
    source = _parse_drive_folder_source(payload.folder)
    category = payload.category.strip() or "Web Flow"
    batch_id = "WEB-" + uuid.uuid4().hex
    used_folder = _drive_used_subfolder(category, batch_id)
    ts = now_iso()

    prepared: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for order, item in enumerate(payload.items, 1):
        project_name = _safe_project_name(item.base)
        key = project_name.casefold()
        if key in seen_names:
            raise HTTPException(409, f"Duplicate project basename in Drive batch: {project_name}")
        seen_names.add(key)
        txt_source = _parse_drive_file_source(item.txt_remote)
        audio_source = _parse_drive_file_source(item.audio_remote)
        if txt_source["kind"] != audio_source["kind"] or txt_source["folder_id"] != audio_source["folder_id"]:
            raise HTTPException(409, f"{project_name}: TXT and audio must come from the same Drive folder source")
        if source["kind"] != txt_source["kind"] or (source["kind"] == "id" and source["folder_id"] != txt_source["folder_id"]):
            raise HTTPException(409, f"{project_name}: pair does not belong to the scanned Drive source")
        if Path(txt_source["path"]).suffix.lower() != ".txt":
            raise HTTPException(400, f"{project_name}: text source must be .txt")
        if Path(audio_source["path"]).suffix.lower() not in AUDIO_EXTENSIONS:
            raise HTTPException(400, f"{project_name}: unsupported audio extension")
        if Path(txt_source["path"]).stem.casefold() != Path(audio_source["path"]).stem.casefold():
            raise HTTPException(409, f"{project_name}: TXT and audio basenames do not match")
        settings = _apply_web_settings(principal, preset, fg_library_id=item.fg_library_id, voice_mode="upload_audio", item_order=order)
        metadata = settings.setdefault("metadata", {})
        metadata.update({
            "drive_folder_source": True,
            "deferred_drive_batch": True,
            "drive_input_folder": _deferred_drive_input_folder(payload.folder),
            "drive_source_original_txt": item.txt_remote,
            "drive_source_original_audio": item.audio_remote,
            "drive_source_used_folder": used_folder,
            "drive_source_moved_to_used": False,
        })
        prepared.append({
            "order": order, "project_name": project_name,
            "txt_remote": item.txt_remote, "audio_remote": item.audio_remote,
            "settings": settings,
        })

    # Persistent duplicate protection is based on the original Drive source paths,
    # not just the basename. This continues to work even after originals move to Used.
    with connect() as db:
        for item in prepared:
            duplicate = db.execute(
                """SELECT batch_id,project_name FROM drive_batch_queue
                   WHERE owner_id=? AND (txt_remote=? OR audio_remote=?) LIMIT 1""",
                (principal.username, item["txt_remote"], item["audio_remote"]),
            ).fetchone()
            if duplicate:
                raise HTTPException(409, f"Drive source already submitted as {duplicate['project_name']} in batch {duplicate['batch_id']}")

    created: list[tuple[str, str, Path]] = []
    try:
        batch_settings = {
            "web_flow_v1": True, "preset_id": payload.preset_id, "voice_mode": "upload_audio",
            "deferred_drive_batch": True, "source_folder": payload.folder,
            "source_kind": source["kind"], "source_folder_id": source.get("folder_id", ""),
            "used_folder": used_folder,
        }
        with connect() as db:
            db.execute(
                """INSERT INTO upload_batches(id,owner_id,status,expected_count,staged_count,verified_count,category,settings_json,control_state,created_at,updated_at,finalized_at)
                   VALUES(?,?,'accepted',?,?,0,?,?,'active',?,?,?)""",
                (batch_id, principal.username, len(prepared), len(prepared), category,
                 json.dumps(batch_settings, ensure_ascii=False, separators=(",", ":")), ts, ts, ts),
            )
            db.commit()

        for item in prepared:
            project_id, root = _new_project(item["project_name"], principal)
            created.append((project_id, "", root))
            settings = item["settings"]
            save_settings(root, settings)
            with connect() as db:
                db.execute("BEGIN IMMEDIATE")
                project = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
                job_id = create_job_record(
                    db, project=dict(project), settings=settings, initial_status=HELD,
                    batch_id=batch_id, client_item_id=item["project_name"], category=category, upload_verified=False,
                )
                pipeline_stage = "ready_transcribe" if _renderer_needs_transcription(settings) else "ready_render"
                db.execute(
                    """UPDATE jobs SET input_transport='google_drive_deferred',drive_input_status='remote_waiting',
                       drive_input_folder=?,drive_input_verified=0,pipeline_stage=?,current_stage='held',
                       message='On Drive — waiting for the single prefetch slot',updated_at=? WHERE id=?""",
                    (_deferred_drive_input_folder(payload.folder), pipeline_stage, ts, job_id),
                )
                db.execute(
                    """INSERT INTO batch_items(id,batch_id,client_item_id,project_id,job_id,project_name,category,upload_verified,item_order,
                       script_sha256,audio_sha256,script_size,audio_size,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,0,?,'','',0,0,?,?)""",
                    (uuid.uuid4().hex, batch_id, item["project_name"], project_id, job_id, item["project_name"], category, item["order"], ts, ts),
                )
                db.execute(
                    """INSERT INTO drive_batch_queue(id,batch_id,owner_id,item_order,project_name,project_id,job_id,txt_remote,audio_remote,
                       source_folder,used_folder,settings_json,status,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'remote_waiting',?,?)""",
                    (uuid.uuid4().hex, batch_id, principal.username, item["order"], item["project_name"], project_id, job_id,
                     item["txt_remote"], item["audio_remote"], payload.folder, used_folder,
                     json.dumps(settings, ensure_ascii=False, separators=(",", ":")), ts, ts),
                )
                db.commit()
            created[-1] = (project_id, job_id, root)
        _audit(principal, "deferred_drive_batch_saved", {
            "batch_id": batch_id, "projects": len(prepared), "source": source.get("display", payload.folder),
            "used_folder": used_folder, "prefetch_policy": "one_active_one_waiting",
        })
        return {
            "ok": True, "batch_id": batch_id, "status": "accepted", "expected_count": len(prepared),
            "used_folder": used_folder, "behavior": "one_active_one_waiting", "browser_independent": True,
        }
    except BaseException:
        with connect() as db:
            for project_id, job_id, _root in created:
                if job_id:
                    db.execute("DELETE FROM drive_batch_queue WHERE job_id=?", (job_id,))
                    db.execute("DELETE FROM batch_items WHERE job_id=?", (job_id,))
                    db.execute("DELETE FROM job_events WHERE job_id=?", (job_id,))
                    db.execute("DELETE FROM job_attempts WHERE job_id=?", (job_id,))
                    db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
                db.execute("DELETE FROM projects WHERE id=?", (project_id,))
            db.execute("DELETE FROM upload_batches WHERE id=?", (batch_id,))
            db.commit()
        for _project_id, _job_id, root in created:
            shutil.rmtree(root, ignore_errors=True)
        raise


@router.get("/drive-batches")
def list_deferred_drive_batches(principal: Principal = Depends(get_current_principal)) -> dict[str, Any]:
    return {"ok": True, "batches": deferred_batch_summaries(principal.username, include_all=principal.is_admin, limit=30)}


def _set_deferred_batch_control(batch_id: str, requested: str, principal: Principal) -> dict[str, Any]:
    ensure_drive_batch_schema()
    batch = _batch_for_principal(batch_id, principal)
    if str(batch.get("status") or "") in {"completed", "failed", "cancelled"}:
        raise HTTPException(409, f"Batch is already {batch['status']}")
    with connect() as db:
        active = db.execute(
            """SELECT COUNT(*) FROM drive_batch_queue q JOIN jobs j ON j.id=q.job_id
               WHERE q.batch_id=? AND q.status='staged' AND j.status IN ('queued','running','completed')""",
            (batch_id,),
        ).fetchone()[0]
        state = requested
        if requested == "pause_after_current" and not int(active or 0):
            state = "paused"
        db.execute("UPDATE upload_batches SET control_state=?,updated_at=? WHERE id=?", (state, now_iso(), batch_id))
        db.commit()
    _audit(principal, "deferred_drive_batch_control", {"batch_id": batch_id, "state": state})
    return {"ok": True, "batch_id": batch_id, "control_state": state}


@router.post("/drive-batches/{batch_id}/stop-after-current")
def deferred_drive_stop_after_current(batch_id: str, principal: Principal = Depends(get_current_principal)) -> dict[str, Any]:
    return _set_deferred_batch_control(batch_id, "pause_after_current", principal)


@router.post("/drive-batches/{batch_id}/resume")
def deferred_drive_resume(batch_id: str, principal: Principal = Depends(get_current_principal)) -> dict[str, Any]:
    return _set_deferred_batch_control(batch_id, "active", principal)


@router.post("/upload/drive-pair")
def upload_drive_pair(
    batch_id: str = Form(...),
    preset_id: str = Form(...),
    fg_library_id: str = Form(""),
    item_order: int = Form(1),
    txt_remote: str = Form(...),
    audio_remote: str = Form(...),
    principal: Principal = Depends(get_current_principal),
) -> dict[str, Any]:
    batch = _batch_for_principal(batch_id, principal)
    if str(batch["status"]) != "uploading":
        raise HTTPException(409, "Upload session is no longer accepting items")
    _, preset = _find_preset(principal, preset_id)
    txt_source = _parse_drive_file_source(txt_remote)
    audio_source = _parse_drive_file_source(audio_remote)
    if txt_source["kind"] != audio_source["kind"] or txt_source["folder_id"] != audio_source["folder_id"]:
        raise HTTPException(409, "TXT and audio must come from the same Drive folder source")
    txt_name = Path(txt_source["path"]).name
    aud_name = Path(audio_source["path"]).name
    if Path(txt_name).suffix.lower() != ".txt":
        raise HTTPException(400, "Drive text file must be .txt")
    if Path(aud_name).suffix.lower() not in AUDIO_EXTENSIONS:
        raise HTTPException(400, "Unsupported Drive audio file type")
    if Path(txt_name).stem.casefold() != Path(aud_name).stem.casefold():
        raise HTTPException(409, "TXT and audio basenames must match exactly")
    project_name = _safe_project_name(Path(txt_name).stem)
    project_id, root = _new_project(project_name, principal)
    script_path = root / "input" / f"{project_name}.txt"
    audio_path = root / "input" / f"{project_name}{Path(aud_name).suffix.lower()}"
    try:
        _config, remote, _drive_root = _load_drive_runtime()
        _drive_copyto(remote, txt_source, script_path)
        _drive_copyto(remote, audio_source, audio_path)
        if not script_path.is_file() or script_path.stat().st_size <= 0:
            raise HTTPException(400, "Drive TXT file is empty")
        _validate_audio_file(audio_path)
        settings = _apply_web_settings(principal, preset, fg_library_id=fg_library_id, voice_mode="upload_audio", item_order=item_order)
        metadata = settings.setdefault("metadata", {})
        metadata["drive_folder_source"] = True
        if txt_source["kind"] == "id":
            drive_input_folder = f"driveid:{txt_source['folder_id']}"
        else:
            drive_input_folder = str(Path(txt_source["path"]).parent).replace("\\", "/")
        metadata["drive_input_folder"] = drive_input_folder
        save_settings(root, settings)
        ts = now_iso()
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            duplicate = db.execute(
                "SELECT id FROM batch_items WHERE batch_id=? AND lower(client_item_id)=lower(?)",
                (batch_id, project_name),
            ).fetchone()
            if duplicate:
                raise HTTPException(409, f"Duplicate project basename in this upload session: {project_name}")
            project = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            job_id = create_job_record(
                db, project=dict(project), settings=settings, initial_status=HELD,
                batch_id=batch_id, client_item_id=project_name,
                category=str(batch.get("category") or "Web Flow"), upload_verified=True,
            )
            pipeline_stage = "ready_transcribe" if _renderer_needs_transcription(settings) else "ready_render"
            db.execute(
                """UPDATE jobs SET input_transport='google_drive_folder',drive_input_status='verified',
                   drive_input_folder=?,drive_input_verified=1,pipeline_stage=?,current_stage='held',
                   message='Drive source copied and verified — waiting for complete folder submission',updated_at=?
                   WHERE id=?""",
                (drive_input_folder, pipeline_stage, ts, job_id),
            )
            db.execute(
                """INSERT INTO batch_items(id,batch_id,client_item_id,project_id,job_id,project_name,category,upload_verified,item_order,
                   script_sha256,audio_sha256,script_size,audio_size,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,1,?,?,?,?,?,?,?)""",
                (uuid.uuid4().hex, batch_id, project_name, project_id, job_id, project_name,
                 str(batch.get("category") or "Web Flow"), max(1, int(item_order)),
                 _sha256(script_path), _sha256(audio_path), script_path.stat().st_size, audio_path.stat().st_size, ts, ts),
            )
            counts = db.execute(
                "SELECT COUNT(*) staged,SUM(upload_verified) verified FROM batch_items WHERE batch_id=?",
                (batch_id,),
            ).fetchone()
            db.execute(
                "UPDATE upload_batches SET staged_count=?,verified_count=?,updated_at=? WHERE id=?",
                (int(counts["staged"] or 0), int(counts["verified"] or 0), ts, batch_id),
            )
            db.commit()
        _audit(principal, "drive_pair_staged", {"batch_id": batch_id, "project": project_name, "txt": txt_remote, "audio": audio_remote})
        return {"ok": True, "project_id": project_id, "job_id": job_id, "project_name": project_name, "status": "staged"}
    except BaseException:
        with connect() as db:
            db.execute("DELETE FROM batch_items WHERE project_id=?", (project_id,))
            db.execute("DELETE FROM job_events WHERE job_id IN (SELECT id FROM jobs WHERE project_id=?)", (project_id,))
            db.execute("DELETE FROM job_attempts WHERE job_id IN (SELECT id FROM jobs WHERE project_id=?)", (project_id,))
            db.execute("DELETE FROM jobs WHERE project_id=?", (project_id,))
            db.execute("DELETE FROM projects WHERE id=?", (project_id,))
            db.commit()
        shutil.rmtree(root, ignore_errors=True)
        raise


@router.post("/upload/session")
def create_upload_session(payload: UploadSessionPayload, principal: Principal = Depends(get_current_principal)) -> dict[str, Any]:
    initialize_storage()
    _, preset = _find_preset(principal, payload.preset_id)
    preview_status = _preview_status_payload(principal, preset)
    if not bool(preset.get("preview_approved", False)) or not bool(preview_status.get("is_current")):
        raise HTTPException(409, "Selected preset must have a current rendered preview approved before submission")
    mode = payload.voice_mode.strip().lower()
    if mode not in {"upload_audio", "algrow_voice"}:
        raise HTTPException(400, "Voice mode must be upload_audio or algrow_voice")
    batch_id = "WEB-" + uuid.uuid4().hex
    ts = now_iso()
    with connect() as db:
        db.execute(
            """INSERT INTO upload_batches(id,owner_id,status,expected_count,staged_count,verified_count,category,settings_json,created_at,updated_at)
               VALUES(?,?,'uploading',?,0,0,?,?,?,?)""",
            (batch_id, principal.username, payload.expected_count, payload.category.strip() or "Web Flow",
             json.dumps({"web_flow_v1": True, "preset_id": payload.preset_id, "voice_mode": mode}, separators=(",", ":")), ts, ts),
        )
        db.commit()
    _audit(principal, "upload_session", {"batch_id": batch_id, "expected_count": payload.expected_count, "voice_mode": mode})
    return {"ok": True, "batch_id": batch_id, "expected_count": payload.expected_count, "voice_mode": mode}


@router.post("/upload/pair")
def upload_pair(
    batch_id: str = Form(...),
    preset_id: str = Form(...),
    fg_library_id: str = Form(""),
    item_order: int = Form(1),
    text_file: UploadFile = File(...),
    audio_file: UploadFile = File(...),
    principal: Principal = Depends(get_current_principal),
) -> dict[str, Any]:
    batch = _batch_for_principal(batch_id, principal)
    if str(batch["status"]) != "uploading":
        raise HTTPException(409, "Upload session is no longer accepting items")
    _, preset = _find_preset(principal, preset_id)
    txt_name = Path(text_file.filename or "").name
    aud_name = Path(audio_file.filename or "").name
    if Path(txt_name).suffix.lower() != ".txt":
        raise HTTPException(400, "Text file must be .txt")
    if Path(aud_name).suffix.lower() not in AUDIO_EXTENSIONS:
        raise HTTPException(400, "Unsupported audio file type")
    if Path(txt_name).stem.casefold() != Path(aud_name).stem.casefold():
        raise HTTPException(409, "TXT and audio basenames must match exactly")
    project_name = _safe_project_name(Path(txt_name).stem)
    project_id, root = _new_project(project_name, principal)
    script_path = root / "input" / f"{project_name}.txt"
    audio_path = root / "input" / f"{project_name}{Path(aud_name).suffix.lower()}"
    try:
        with script_path.open("wb") as handle:
            shutil.copyfileobj(text_file.file, handle)
        with audio_path.open("wb") as handle:
            shutil.copyfileobj(audio_file.file, handle)
        if script_path.stat().st_size <= 0:
            raise HTTPException(400, "TXT file is empty")
        _validate_audio_file(audio_path)
        settings = _apply_web_settings(principal, preset, fg_library_id=fg_library_id, voice_mode="upload_audio", item_order=item_order)
        save_settings(root, settings)
        ts = now_iso()
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            duplicate = db.execute("SELECT id FROM batch_items WHERE batch_id=? AND lower(client_item_id)=lower(?)", (batch_id, project_name)).fetchone()
            if duplicate:
                raise HTTPException(409, f"Duplicate project basename in this upload session: {project_name}")
            project = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            job_id = create_job_record(
                db, project=dict(project), settings=settings, initial_status=HELD,
                batch_id=batch_id, client_item_id=project_name, category=str(batch.get("category") or "Web Flow"), upload_verified=True,
            )
            pipeline_stage = "ready_transcribe" if _renderer_needs_transcription(settings) else "ready_render"
            db.execute(
                """UPDATE jobs SET input_transport='web_upload',drive_input_status='not_required',drive_input_verified=1,
                   pipeline_stage=?,current_stage='held',message='VPS Ready — waiting for complete web upload',updated_at=? WHERE id=?""",
                (pipeline_stage, ts, job_id),
            )
            db.execute(
                """INSERT INTO batch_items(id,batch_id,client_item_id,project_id,job_id,project_name,category,upload_verified,item_order,
                   script_sha256,audio_sha256,script_size,audio_size,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,1,?,?,?,?,?,?,?)""",
                (uuid.uuid4().hex, batch_id, project_name, project_id, job_id, project_name, str(batch.get("category") or "Web Flow"),
                 max(1, int(item_order)), _sha256(script_path), _sha256(audio_path), script_path.stat().st_size, audio_path.stat().st_size, ts, ts),
            )
            counts = db.execute("SELECT COUNT(*) staged,SUM(upload_verified) verified FROM batch_items WHERE batch_id=?", (batch_id,)).fetchone()
            db.execute("UPDATE upload_batches SET staged_count=?,verified_count=?,updated_at=? WHERE id=?", (int(counts["staged"] or 0), int(counts["verified"] or 0), ts, batch_id))
            db.commit()
        return {"ok": True, "project_id": project_id, "job_id": job_id, "project_name": project_name, "status": "staged"}
    except BaseException:
        # Project/job rollback is safe here because no worker can claim HELD work.
        with connect() as db:
            db.execute("DELETE FROM batch_items WHERE project_id=?", (project_id,))
            db.execute("DELETE FROM job_events WHERE job_id IN (SELECT id FROM jobs WHERE project_id=?)", (project_id,))
            db.execute("DELETE FROM job_attempts WHERE job_id IN (SELECT id FROM jobs WHERE project_id=?)", (project_id,))
            db.execute("DELETE FROM jobs WHERE project_id=?", (project_id,))
            db.execute("DELETE FROM projects WHERE id=?", (project_id,))
            db.commit()
        shutil.rmtree(root, ignore_errors=True)
        raise


@router.post("/upload/abort/{batch_id}")
def abort_upload_session(batch_id: str, principal: Principal = Depends(get_current_principal)) -> dict[str, Any]:
    """Safely discard an unfinalized Upload Audio web session.

    Only HELD New Web Flow jobs are eligible. Algrow sessions are deliberately
    not aborted here because the sidecar may already have observed the publish
    marker; those are handled through normal project controls instead.
    """
    batch = _batch_for_principal(batch_id, principal)
    batch_settings = _load_json_from_text(str(batch.get("settings_json") or "{}"))
    if not bool(batch_settings.get("web_flow_v1")):
        raise HTTPException(409, "Only New Web Flow upload sessions can be aborted here")
    if str(batch_settings.get("voice_mode") or "upload_audio") != "upload_audio":
        raise HTTPException(409, "Algrow Voice sessions cannot be automatically aborted after sidecar publication")
    if str(batch.get("status") or "") not in {"uploading", "failed"}:
        raise HTTPException(409, "Only an unfinalized upload session can be aborted")

    roots: list[Path] = []
    removed_jobs: list[str] = []
    ts = now_iso()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute(
            """SELECT j.id AS job_id,j.status AS job_status,j.settings_json,p.id AS project_id,p.project_path
               FROM batch_items bi
               JOIN jobs j ON j.id=bi.job_id
               JOIN projects p ON p.id=bi.project_id
               WHERE bi.batch_id=? ORDER BY bi.item_order,bi.created_at""",
            (batch_id,),
        ).fetchall()
        for row in rows:
            settings = _load_json_from_text(str(row["settings_json"] or "{}"))
            metadata = settings.get("metadata") if isinstance(settings.get("metadata"), dict) else {}
            if not bool(metadata.get("web_flow_v1")):
                raise HTTPException(409, "Batch contains a non-web-flow job; abort refused")
            if str(row["job_status"] or "").lower() != HELD:
                raise HTTPException(409, f"Job {row['job_id']} is no longer held; abort refused")
            roots.append(Path(str(row["project_path"])))
            removed_jobs.append(str(row["job_id"]))

        if removed_jobs:
            marks = ",".join("?" for _ in removed_jobs)
            db.execute(f"DELETE FROM job_events WHERE job_id IN ({marks})", removed_jobs)
            db.execute(f"DELETE FROM job_attempts WHERE job_id IN ({marks})", removed_jobs)
        db.execute("DELETE FROM batch_items WHERE batch_id=?", (batch_id,))
        if removed_jobs:
            marks = ",".join("?" for _ in removed_jobs)
            db.execute(f"DELETE FROM jobs WHERE id IN ({marks})", removed_jobs)
        project_ids = [str(row["project_id"]) for row in rows]
        if project_ids:
            marks = ",".join("?" for _ in project_ids)
            db.execute(f"DELETE FROM projects WHERE id IN ({marks})", project_ids)
        db.execute(
            "UPDATE upload_batches SET status='cancelled',staged_count=0,verified_count=0,error_text='Aborted before queue release',updated_at=? WHERE id=?",
            (ts, batch_id),
        )
        db.commit()

    for root in roots:
        try:
            resolved = root.resolve()
            project_base = projects_root().resolve()
            if resolved != project_base and project_base in resolved.parents:
                shutil.rmtree(resolved, ignore_errors=True)
        except Exception:
            pass
    _audit(principal, "upload_abort", {"batch_id": batch_id, "removed_jobs": len(removed_jobs)})
    return {"ok": True, "batch_id": batch_id, "status": "cancelled", "removed_jobs": len(removed_jobs)}


@router.post("/upload/algrow")
def upload_algrow_txt(
    batch_id: str = Form(...),
    preset_id: str = Form(...),
    fg_library_id: str = Form(""),
    voice_profile_id: str = Form(...),
    item_order: int = Form(1),
    text_file: UploadFile = File(...),
    principal: Principal = Depends(get_current_principal),
) -> dict[str, Any]:
    batch = _batch_for_principal(batch_id, principal)
    _, preset = _find_preset(principal, preset_id)
    profile = next((x for x in _voice_profiles() if str(x.get("id")) == voice_profile_id and bool(x.get("enabled", True))), None)
    if profile is None:
        raise HTTPException(409, "Selected Algrow voice profile is unavailable")
    txt_name = Path(text_file.filename or "").name
    if Path(txt_name).suffix.lower() != ".txt":
        raise HTTPException(400, "Algrow mode accepts TXT files")
    project_name = _safe_project_name(Path(txt_name).stem)
    script_bytes = text_file.file.read()
    if not script_bytes.strip():
        raise HTTPException(400, "TXT file is empty")
    result = _algrow_ingress_submit(
        principal=principal, batch=batch, item_order=item_order, project_name=project_name,
        script_bytes=script_bytes, preset=preset, fg_library_id=fg_library_id, voice_profile=profile,
    )
    return {"ok": True, **result}


@router.post("/upload/finalize/{batch_id}")
def finalize_upload_session(batch_id: str, payload: UploadFinalizePayload, principal: Principal = Depends(get_current_principal)) -> dict[str, Any]:
    batch = _batch_for_principal(batch_id, principal)
    if payload.expected_count != int(batch["expected_count"]):
        raise HTTPException(409, "Expected project count changed during upload")
    settings = _load_json_from_text(str(batch.get("settings_json") or "{}"))
    voice_mode = str(settings.get("voice_mode") or "upload_audio")
    if voice_mode == "algrow_voice":
        # Sidecar releases each project only after generated audio validation.
        return {"ok": True, "batch_id": batch_id, "status": "waiting_for_algrow", "expected_count": payload.expected_count}
    ts = now_iso()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        counts = db.execute("SELECT COUNT(*) staged,SUM(upload_verified) verified FROM batch_items WHERE batch_id=?", (batch_id,)).fetchone()
        staged, verified = int(counts["staged"] or 0), int(counts["verified"] or 0)
        if staged != payload.expected_count or verified != payload.expected_count:
            raise HTTPException(409, f"Upload incomplete: expected {payload.expected_count}, staged {staged}, verified {verified}")
        rows = db.execute("SELECT j.id FROM jobs j JOIN batch_items bi ON bi.job_id=j.id WHERE bi.batch_id=? AND j.status='held' ORDER BY bi.item_order", (batch_id,)).fetchall()
        for row in rows:
            transition_job_in_db(
                db, str(row["id"]), QUEUED, stage="queued", progress=0,
                message="Queued after complete web folder verification", event_type="web_batch_released", detail={"batch_id": batch_id},
            )
        db.execute("UPDATE upload_batches SET status='accepted',finalized_at=?,updated_at=? WHERE id=?", (ts, ts, batch_id))
        db.commit()
    return {"ok": True, "batch_id": batch_id, "status": "accepted", "released": len(rows)}


def _load_json_from_text(value: str) -> dict[str, Any]:
    try:
        data = json.loads(value or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


@router.get("/admin/status")
def admin_status(principal: Principal = Depends(get_current_principal)) -> dict[str, Any]:
    if not principal.is_admin:
        raise HTTPException(403, "Administrator access required")
    with connect() as db:
        workers = [dict(row) for row in db.execute("SELECT * FROM acs_worker_runtime ORDER BY name").fetchall()]
        jobs = db.execute(
            """SELECT j.*,p.project_path FROM jobs j JOIN projects p ON p.id=j.project_id
               WHERE j.settings_json LIKE '%\"web_flow_v1\":true%' ORDER BY j.created_at DESC"""
        ).fetchall()
    project_bytes = 0
    cleanup_eligible = 0
    for row in jobs:
        root = Path(str(row["project_path"]))
        if root.is_dir():
            project_bytes += sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
        settings = _load_json_from_text(str(row["settings_json"] or "{}"))
        metadata = settings.get("metadata") if isinstance(settings.get("metadata"), dict) else {}
        if bool(metadata.get("web_preview_reference")):
            continue
        if int(row["drive_verified"] or 0) and str(row["local_cleanup_status"] or "") != "cleared":
            cleanup_eligible += 1
    protected_bytes = 0
    for item in _fg_registry():
        root = Path(str(item.get("path") or ""))
        if root.is_dir():
            protected_bytes += sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
    if PEXELS_LIBRARY.is_dir():
        protected_bytes += sum(p.stat().st_size for p in PEXELS_LIBRARY.rglob("*") if p.is_file())
    music = Path(_music_config()["path"])
    if music.is_dir():
        protected_bytes += sum(p.stat().st_size for p in music.rglob("*") if p.is_file())
    return {
        "ok": True, "workers": workers, "web_flow_project_bytes": project_bytes,
        "cleanup_eligible": cleanup_eligible, "protected_library_bytes": protected_bytes,
    }


@router.get("/admin/orphans")
def admin_orphans(principal: Principal = Depends(get_current_principal)) -> dict[str, Any]:
    if not principal.is_admin:
        raise HTTPException(403, "Administrator access required")
    root = projects_root()
    with connect() as db:
        known = {str(row["id"]) for row in db.execute("SELECT id FROM projects").fetchall()}
    orphans = []
    if root.is_dir():
        for child in sorted(root.iterdir(), key=lambda p: p.name.casefold()):
            if not child.is_dir() or child.name in known:
                continue
            size = sum(p.stat().st_size for p in child.rglob("*") if p.is_file())
            orphans.append({"name": child.name, "path": str(child), "size_bytes": size})
    return {"ok": True, "count": len(orphans), "size_bytes": sum(x["size_bytes"] for x in orphans), "orphans": orphans[:200]}


@router.get("/admin/verify-delivered")
def admin_verify_delivered(principal: Principal = Depends(get_current_principal)) -> dict[str, Any]:
    if not principal.is_admin:
        raise HTTPException(403, "Administrator access required")
    with connect() as db:
        rows = db.execute("SELECT id,status,drive_status,drive_verified,local_cleanup_status,settings_json FROM jobs ORDER BY created_at DESC").fetchall()
    checked = []
    issues = []
    for row in rows:
        settings = _load_json_from_text(str(row["settings_json"] or "{}"))
        metadata = settings.get("metadata") if isinstance(settings.get("metadata"), dict) else {}
        if not bool(metadata.get("web_flow_v1")) or bool(metadata.get("web_preview_reference")) or str(row["status"] or "") != "completed":
            continue
        drive_enabled = bool(metadata.get("web_drive_upload_enabled", True))
        ok = (
            (not drive_enabled and str(row["drive_status"] or "") == "not_required")
            or (drive_enabled and int(row["drive_verified"] or 0) == 1 and str(row["local_cleanup_status"] or "") == "cleared")
        )
        record = {"job_id": str(row["id"]), "drive_enabled": drive_enabled, "drive_status": str(row["drive_status"] or ""), "drive_verified": bool(row["drive_verified"]), "cleanup": str(row["local_cleanup_status"] or ""), "ok": ok}
        checked.append(record)
        if not ok:
            issues.append(record)
    return {"ok": True, "checked": len(checked), "issue_count": len(issues), "issues": issues[:100]}


@router.post("/admin/cleanup-verified")
def cleanup_verified(payload: CleanupConfirm, principal: Principal = Depends(get_current_principal)) -> dict[str, Any]:
    if not principal.is_admin:
        raise HTTPException(403, "Administrator access required")
    if payload.confirm != "CLEAN VERIFIED WEB FLOW":
        raise HTTPException(400, "Exact confirmation required")
    removed: list[str] = []
    with connect() as db:
        rows = db.execute(
            """SELECT j.id,j.output_path,j.preview_path,j.drive_verified,j.local_cleanup_status,p.project_path,j.settings_json
               FROM jobs j JOIN projects p ON p.id=j.project_id
               WHERE j.status='completed'"""
        ).fetchall()
    for row in rows:
        settings = _load_json_from_text(str(row["settings_json"] or "{}"))
        cleanup_metadata = dict(settings.get("metadata") or {})
        if not bool(cleanup_metadata.get("web_flow_v1")) or bool(cleanup_metadata.get("web_preview_reference")):
            continue
        if not int(row["drive_verified"] or 0):
            continue
        root = Path(str(row["project_path"]))
        for target in (Path(str(row["output_path"] or "")), Path(str(row["preview_path"] or ""))):
            if target.is_file():
                target.unlink(missing_ok=True); removed.append(str(target))
        temp = root / "temp"
        if temp.is_dir():
            shutil.rmtree(temp); removed.append(str(temp))
        with connect() as db:
            db.execute("UPDATE jobs SET local_cleanup_status='cleared',updated_at=? WHERE id=?", (now_iso(), str(row["id"])))
            db.commit()
    _audit(principal, "cleanup_verified", {"removed_count": len(removed)})
    return {"ok": True, "removed_count": len(removed)}

# ACS_FG_PC_UPLOAD_V1_ROUTER
from .fg_pc_upload_api import router as fg_pc_upload_router
router.include_router(fg_pc_upload_router)

# DRIVE_PROFILES_PHASE1_V1_BACKEND
# Persistent Drive Profile management. Phase 1 intentionally does not scan or enqueue.
from pathlib import Path as _DPPath
import json as _dp_json
import os as _dp_os
import uuid as _dp_uuid
from datetime import datetime as _DPDatetime, timezone as _DPTimezone
from fastapi import Depends as _DPDepends, HTTPException as _DPHTTPException

_DP_CONFIG = _DPPath("/home/ubuntu/apps/auto_caption_studio/data/config/drive_profiles.json")
_DP_RUNTIME = _DPPath("/home/ubuntu/apps/auto_caption_studio/data/config/drive_profiles_runtime.json")
_DP_TRIGGER = _DPPath("/home/ubuntu/apps/auto_caption_studio/data/config/drive_profiles_sync.trigger")
_DP_DEFAULT_SETTINGS = {"auto_refill_enabled": False, "idle_recheck_minutes": 15}

def _dp_now():
    return _DPDatetime.now(_DPTimezone.utc).isoformat().replace("+00:00", "Z")

def _dp_default():
    return {"version": 1, "settings": dict(_DP_DEFAULT_SETTINGS), "profiles": []}

def _dp_load():
    if not _DP_CONFIG.exists():
        return _dp_default()
    try:
        raw = _dp_json.loads(_DP_CONFIG.read_text(encoding="utf-8"))
    except Exception as exc:
        raise _DPHTTPException(status_code=500, detail=f"Drive Profiles config unreadable: {exc}")
    if not isinstance(raw, dict):
        raw = _dp_default()
    raw.setdefault("version", 1)
    raw.setdefault("settings", dict(_DP_DEFAULT_SETTINGS))
    raw.setdefault("profiles", [])
    raw["settings"].setdefault("auto_refill_enabled", False)
    raw["settings"].setdefault("idle_recheck_minutes", 15)
    return raw

def _dp_save(data):
    _DP_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = _DP_CONFIG.with_suffix(".json.tmp")
    tmp.write_text(_dp_json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _dp_os.replace(tmp, _DP_CONFIG)

def _dp_text(value, field, *, required=False, max_len=1000):
    value = str(value or "").strip()
    if required and not value:
        raise _DPHTTPException(status_code=400, detail=f"{field} is required")
    if len(value) > max_len:
        raise _DPHTTPException(status_code=400, detail=f"{field} is too long")
    return value

def _dp_bool(value, default=False):
    if value is None:
        return default
    return bool(value)

def _dp_public(data):
    runtime = {}
    try:
        runtime = _dp_json.loads(_DP_RUNTIME.read_text(encoding="utf-8")) if _DP_RUNTIME.is_file() else {}
    except Exception:
        runtime = {}
    profile_runtime = runtime.get("profiles", {}) if isinstance(runtime, dict) else {}
    profiles = []
    for raw in data.get("profiles", []):
        item = dict(raw) if isinstance(raw, dict) else {}
        overlay = profile_runtime.get(str(item.get("id") or ""), {}) if isinstance(profile_runtime, dict) else {}
        if isinstance(overlay, dict):
            for key in ("last_sync", "last_result", "last_found", "last_added", "last_batch_id"):
                if key in overlay:
                    item[key] = overlay[key]
        profiles.append(item)
    return {
        "ok": True,
        "phase": 2,
        "auto_refill_runtime_enabled": True,
        "settings": data.get("settings", {}),
        "profiles": profiles,
        "runtime": runtime if isinstance(runtime, dict) else {},
    }

@router.get("/drive-profiles")
def drive_profiles_list(principal=_DPDepends(get_current_principal)):
    return _dp_public(_dp_load())

@router.post("/drive-profiles")
def drive_profiles_create(payload: dict, principal=_DPDepends(get_current_principal)):
    data = _dp_load()
    now = _dp_now()
    item = {
        "id": _dp_uuid.uuid4().hex,
        "name": _dp_text(payload.get("name"), "Profile name", required=True, max_len=80),
        "folder_ref": _dp_text(payload.get("folder_ref"), "Drive folder", required=True, max_len=1200),
        "category": _dp_text(payload.get("category"), "Category", max_len=120),
        "preset": _dp_text(payload.get("preset"), "Preset", max_len=160),
        "voice_mode": _dp_text(payload.get("voice_mode"), "Voice mode", max_len=120),
        "enabled": _dp_bool(payload.get("enabled"), True),
        "created_at": now,
        "updated_at": now,
        "created_by": str(getattr(principal, "username", "") or "team"),
        "last_sync": None,
        "last_result": "Never synced",
        "last_found": 0,
        "last_added": 0,
    }
    data["profiles"].append(item)
    _dp_save(data)
    return {"ok": True, "profile": item, "phase": 1}

@router.put("/drive-profiles/{profile_id}")
def drive_profiles_update(profile_id: str, payload: dict, principal=_DPDepends(get_current_principal)):
    data = _dp_load()
    item = next((x for x in data["profiles"] if str(x.get("id")) == profile_id), None)
    if item is None:
        raise _DPHTTPException(status_code=404, detail="Drive Profile not found")
    if "name" in payload:
        item["name"] = _dp_text(payload.get("name"), "Profile name", required=True, max_len=80)
    if "folder_ref" in payload:
        item["folder_ref"] = _dp_text(payload.get("folder_ref"), "Drive folder", required=True, max_len=1200)
    if "category" in payload:
        item["category"] = _dp_text(payload.get("category"), "Category", max_len=120)
    if "preset" in payload:
        item["preset"] = _dp_text(payload.get("preset"), "Preset", max_len=160)
    if "voice_mode" in payload:
        item["voice_mode"] = _dp_text(payload.get("voice_mode"), "Voice mode", max_len=120)
    if "enabled" in payload:
        item["enabled"] = _dp_bool(payload.get("enabled"))
    item["updated_at"] = _dp_now()
    _dp_save(data)
    return {"ok": True, "profile": item, "phase": 1}

@router.delete("/drive-profiles/{profile_id}")
def drive_profiles_delete(profile_id: str, principal=_DPDepends(get_current_principal)):
    data = _dp_load()
    before = len(data["profiles"])
    data["profiles"] = [x for x in data["profiles"] if str(x.get("id")) != profile_id]
    if len(data["profiles"]) == before:
        raise _DPHTTPException(status_code=404, detail="Drive Profile not found")
    _dp_save(data)
    return {"ok": True, "deleted": profile_id, "phase": 1}

@router.put("/drive-profiles/settings")
def drive_profiles_settings(payload: dict, principal=_DPDepends(get_current_principal)):
    data = _dp_load()
    settings = data.setdefault("settings", dict(_DP_DEFAULT_SETTINGS))
    if "auto_refill_enabled" in payload:
        settings["auto_refill_enabled"] = _dp_bool(payload.get("auto_refill_enabled"))
    if "idle_recheck_minutes" in payload:
        try:
            minutes = int(payload.get("idle_recheck_minutes"))
        except Exception:
            raise _DPHTTPException(status_code=400, detail="Idle recheck must be a number")
        if minutes < 1 or minutes > 1440:
            raise _DPHTTPException(status_code=400, detail="Idle recheck must be between 1 and 1440 minutes")
        settings["idle_recheck_minutes"] = minutes
    settings["updated_at"] = _dp_now()
    _dp_save(data)
    return {"ok": True, "settings": settings, "phase": 2, "auto_refill_runtime_enabled": True}

@router.post("/drive-profiles/sync-enabled")
def drive_profiles_sync_enabled_phase1(principal=_DPDepends(get_current_principal)):
    # Manual sync never bypasses the queue-empty safety rule. The runtime consumes
    # this trigger only once the whole New Web Flow production queue is empty.
    _DP_TRIGGER.parent.mkdir(parents=True, exist_ok=True)
    _DP_TRIGGER.write_text(_dp_json.dumps({"requested_at": _dp_now(), "requested_by": str(getattr(principal, "username", "") or "team")}) + "\n", encoding="utf-8")
    return {"ok": True, "phase": 2, "status": "requested", "message": "Sync requested; it will run when the queue is empty."}


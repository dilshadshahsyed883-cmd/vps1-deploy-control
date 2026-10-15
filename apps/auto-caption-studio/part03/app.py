#!/usr/bin/env python3
import cgi
import hashlib
import html
import json
import mimetypes
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

VERSION = "2.0.0"
BASE_PATH = "/audio-engine"
ROOT = Path(os.environ.get("AUDIO_ENGINE_ROOT", "/var/lib/audio-engine"))
PORT = int(os.environ.get("AUDIO_ENGINE_PORT", "8125"))
HOST = os.environ.get("AUDIO_ENGINE_HOST", "127.0.0.1")
DB_PATH = ROOT / "audio_engine.db"
INPUT_DIR = ROOT / "input"
OUTPUT_DIR = ROOT / "output"
PREVIEW_DIR = ROOT / "preview"
TMP_DIR = ROOT / "tmp"
MUSIC_DIR = ROOT / "music"
MUSIC_CACHE_DIR = ROOT / "music-cache"
MAX_BATCH = 500
CPU_COUNT = max(1, os.cpu_count() or 1)
WORKER_COUNT = max(1, min(4, int(os.environ.get("AUDIO_ENGINE_WORKERS", str(max(1, min(3, CPU_COUNT // 2)))))))
FFMPEG_THREADS = max(1, int(os.environ.get("AUDIO_ENGINE_FFMPEG_THREADS", str(max(1, CPU_COUNT // WORKER_COUNT)))))
MIN_MEM_MB = max(256, int(os.environ.get("AUDIO_ENGINE_MIN_MEM_MB", "768")))
CACHE_MAX_GB = max(1.0, float(os.environ.get("AUDIO_ENGINE_MUSIC_CACHE_GB", "8")))
MIN_FREE_GB = float(os.environ.get("AUDIO_ENGINE_MIN_FREE_GB", "5"))
ALLOWED_EXTS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".mp4"}
RUNNING_PROCS = {}
RUNNING_LOCK = threading.Lock()
STOP_EVENT = threading.Event()
MUSIC_CACHE_LOCK = threading.Lock()
WORKER_STATE_LOCK = threading.Lock()
WORKER_STATE = {}

PRESETS = {
    "documentary": {
        "name": "Documentary Voice",
        "description": "Natural documentary narration with transparent dynamics, calibrated music bed and speech-aware ducking.",
        "pipeline": "High-pass → Gentle EQ → De-esser → Natural compressor → Calibrated music bed → Speech ducking → 2-pass loudness → True-peak safety",
        "defaults": {"noise_enabled": False, "noise_floor": -48.0, "noise_reduction": 5.0, "highpass": 70.0, "bass_gain": 0.8, "mud_gain": -1.2, "presence_gain": 1.2, "air_gain": 0.3, "deesser": 0.18, "comp_threshold": 0.18, "comp_ratio": 2.4, "comp_attack": 12.0, "comp_release": 140.0, "comp_makeup": 1.0, "target_lufs": -16.0, "true_peak": -1.5, "lra": 10.0, "duck_db": 11.0, "duck_attack": 25.0, "duck_release": 700.0, "music_base_lufs": -24.0, "music_level_db": -2.0, "narration_start": 2.5, "music_start": 0.0, "music_fade_in": 2.0, "narration_fade_in": 0.10, "music_fade_out": 3.0, "output_gain_db": 0.0},
    },
    "clean": {
        "name": "Clean Narration",
        "description": "Conservative denoise and corrective EQ for lightly noisy recorded narration.",
        "pipeline": "Adaptive denoise → High-pass → Corrective EQ → De-esser → Compressor → Calibrated music bed → 2-pass loudness",
        "defaults": {"noise_enabled": True, "noise_floor": -44.0, "noise_reduction": 8.0, "highpass": 80.0, "bass_gain": -0.3, "mud_gain": -1.5, "presence_gain": 1.0, "air_gain": 0.2, "deesser": 0.24, "comp_threshold": 0.17, "comp_ratio": 2.5, "comp_attack": 10.0, "comp_release": 150.0, "comp_makeup": 1.0, "target_lufs": -17.0, "true_peak": -1.5, "lra": 11.0, "duck_db": 10.0, "duck_attack": 30.0, "duck_release": 750.0, "music_base_lufs": -24.0, "music_level_db": -2.5, "narration_start": 2.0, "music_start": 0.0, "music_fade_in": 2.0, "narration_fade_in": 0.10, "music_fade_out": 3.0, "output_gain_db": 0.0},
    },
    "natural": {
        "name": "Natural Voice",
        "description": "Very light processing that preserves source character and breathing dynamics.",
        "pipeline": "High-pass → Light EQ → Gentle compressor → Calibrated music bed → Speech ducking → 2-pass loudness",
        "defaults": {"noise_enabled": False, "noise_floor": -50.0, "noise_reduction": 4.0, "highpass": 65.0, "bass_gain": 0.3, "mud_gain": -0.5, "presence_gain": 0.4, "air_gain": 0.1, "deesser": 0.10, "comp_threshold": 0.20, "comp_ratio": 1.8, "comp_attack": 18.0, "comp_release": 180.0, "comp_makeup": 1.0, "target_lufs": -18.0, "true_peak": -1.8, "lra": 12.0, "duck_db": 9.0, "duck_attack": 35.0, "duck_release": 850.0, "music_base_lufs": -25.0, "music_level_db": -2.0, "narration_start": 2.5, "music_start": 0.0, "music_fade_in": 2.5, "narration_fade_in": 0.15, "music_fade_out": 3.5, "output_gain_db": 0.0},
    },
    "deep": {
        "name": "Deep Voice",
        "description": "Warm documentary treatment with controlled low mids and no artificial pitch shift.",
        "pipeline": "High-pass → Warm EQ → Mud control → De-esser → Gentle compressor → Music ducking → 2-pass loudness",
        "defaults": {"noise_enabled": False, "noise_floor": -48.0, "noise_reduction": 5.0, "highpass": 60.0, "bass_gain": 1.6, "mud_gain": -1.0, "presence_gain": 0.6, "air_gain": -0.2, "deesser": 0.16, "comp_threshold": 0.17, "comp_ratio": 2.6, "comp_attack": 14.0, "comp_release": 160.0, "comp_makeup": 1.0, "target_lufs": -16.5, "true_peak": -1.5, "lra": 10.0, "duck_db": 11.0, "duck_attack": 30.0, "duck_release": 750.0, "music_base_lufs": -24.0, "music_level_db": -2.5, "narration_start": 2.5, "music_start": 0.0, "music_fade_in": 2.0, "narration_fade_in": 0.12, "music_fade_out": 3.0, "output_gain_db": 0.0},
    },
    "youtube": {
        "name": "Loud YouTube Voice",
        "description": "Dense but controlled narration for higher-energy YouTube delivery without crushing speech.",
        "pipeline": "High-pass → Presence EQ → De-esser → Firm compressor → Calibrated music bed → Ducking → 2-pass loudness",
        "defaults": {"noise_enabled": False, "noise_floor": -46.0, "noise_reduction": 6.0, "highpass": 75.0, "bass_gain": 0.6, "mud_gain": -1.2, "presence_gain": 1.6, "air_gain": 0.4, "deesser": 0.25, "comp_threshold": 0.15, "comp_ratio": 3.0, "comp_attack": 8.0, "comp_release": 110.0, "comp_makeup": 1.0, "target_lufs": -14.5, "true_peak": -1.2, "lra": 8.0, "duck_db": 12.0, "duck_attack": 20.0, "duck_release": 600.0, "music_base_lufs": -23.5, "music_level_db": -2.0, "narration_start": 1.5, "music_start": 0.0, "music_fade_in": 1.5, "narration_fade_in": 0.08, "music_fade_out": 2.5, "output_gain_db": 0.0},
    },
}


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-32000")
    conn.execute("PRAGMA mmap_size=268435456")
    return conn


def init_storage():
    for p in (ROOT, INPUT_DIR, OUTPUT_DIR, PREVIEW_DIR, TMP_DIR, MUSIC_DIR, MUSIC_CACHE_DIR):
        p.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                external_id TEXT,
                source_app TEXT NOT NULL DEFAULT 'web',
                original_name TEXT NOT NULL,
                input_path TEXT NOT NULL,
                music_path TEXT,
                settings_json TEXT,
                qc_json TEXT,
                output_path TEXT,
                preset TEXT NOT NULL,
                output_format TEXT NOT NULL DEFAULT 'mp3',
                status TEXT NOT NULL DEFAULT 'queued',
                progress REAL NOT NULL DEFAULT 0,
                duration REAL,
                error TEXT,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT
            )
            """
        )
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        if "music_path" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN music_path TEXT")
        if "settings_json" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN settings_json TEXT")
        if "qc_json" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN qc_json TEXT")
        for col, ddl in {
            "worker_id": "ALTER TABLE jobs ADD COLUMN worker_id TEXT",
            "stage": "ALTER TABLE jobs ADD COLUMN stage TEXT",
            "attempts": "ALTER TABLE jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0",
            "batch_id": "ALTER TABLE jobs ADD COLUMN batch_id TEXT",
            "updated_at": "ALTER TABLE jobs ADD COLUMN updated_at TEXT",
        }.items():
            if col not in cols:
                conn.execute(ddl)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_batch ON jobs(batch_id)")
        conn.execute("UPDATE jobs SET status='queued', progress=0, stage='recovered', worker_id=NULL, error='Recovered after service restart', updated_at=? WHERE status='running'", (utcnow(),))
        conn.commit()


# SCENE_AUDIO_ENGINE_PERSISTED_SETTINGS_V1
INTEGRATION_SETTINGS_FILE = ROOT / "integration_settings.json"
INTEGRATION_SETTINGS_LOCK = threading.Lock()
INTEGRATION_SETTINGS_APPS = {"web", "scene_builder"}
# SCENE_AUDIO_ENGINE_PRESET_PERSIST_V2
PRESET_OVERRIDES_FILE = ROOT / "preset_overrides.json"
PRESET_OVERRIDES_LOCK = threading.Lock()
PRESET_FACTORY_DEFAULTS = {k: dict(v.get("defaults") or {}) for k, v in PRESETS.items()}
INTEGRATION_NUMERIC_RANGES = {
    "narration_start": (0, 60), "music_start": (0, 60), "music_fade_in": (0, 30),
    "narration_fade_in": (0, 5), "music_fade_out": (0, 30),
    "noise_floor": (-70, -20), "noise_reduction": (0.01, 24), "highpass": (30, 180),
    "bass_gain": (-8, 8), "mud_gain": (-8, 8), "presence_gain": (-8, 8), "air_gain": (-8, 8),
    "deesser": (0, 1), "comp_threshold": (0.01, 1), "comp_ratio": (1, 20),
    "comp_attack": (0.1, 1000), "comp_release": (1, 5000), "comp_makeup": (0, 8),
    "target_lufs": (-24, -10), "lra": (1, 20), "true_peak": (-6, -0.1),
    "output_gain_db": (-12, 12), "duck_db": (0, 24), "duck_attack": (0.01, 2000),
    "duck_release": (0.01, 9000), "music_level_db": (-40, 6), "music_base_lufs": (-35, -14),
}

def _setting_bool(value, default=False):
    if isinstance(value, bool): return value
    if value is None: return bool(default)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}

def _integration_settings_map_unlocked():
    try:
        data = json.loads(INTEGRATION_SETTINGS_FILE.read_text()) if INTEGRATION_SETTINGS_FILE.is_file() else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _sanitize_integration_settings(payload):
    payload = payload if isinstance(payload, dict) else {}
    preset = str(payload.get("preset") or "documentary").strip()
    if preset not in PRESETS:
        preset = "documentary" if "documentary" in PRESETS else next(iter(PRESETS))
    defaults = dict(PRESETS[preset].get("defaults") or {})
    result = {
        "preset": preset,
        "mode": str(payload.get("mode") or "auto").strip().lower(),
        "output_format": str(payload.get("output_format") or "mp3").strip().lower(),
        "noise_enabled": _setting_bool(payload.get("noise_enabled"), defaults.get("noise_enabled", False)),
        "duck_enabled": _setting_bool(payload.get("duck_enabled"), defaults.get("duck_enabled", True)),
    }
    if result["mode"] not in {"auto", "manual"}: result["mode"] = "auto"
    if result["output_format"] not in {"mp3", "wav"}: result["output_format"] = "mp3"
    for key, (lo, hi) in INTEGRATION_NUMERIC_RANGES.items():
        raw = payload.get(key, defaults.get(key))
        if raw is None: continue
        try: value = float(raw)
        except Exception:
            try: value = float(defaults.get(key))
            except Exception: continue
        result[key] = max(float(lo), min(float(hi), value))
    return result

def _preset_overrides_map_unlocked():
    try:
        data = json.loads(PRESET_OVERRIDES_FILE.read_text()) if PRESET_OVERRIDES_FILE.is_file() else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _sanitize_preset_defaults(preset_key, payload):
    if preset_key not in PRESETS:
        raise ValueError("Unknown preset")
    payload = payload if isinstance(payload, dict) else {}
    base = dict(PRESET_FACTORY_DEFAULTS[preset_key])
    result = dict(base)
    result["noise_enabled"] = _setting_bool(payload.get("noise_enabled"), base.get("noise_enabled", False))
    result["duck_enabled"] = _setting_bool(payload.get("duck_enabled"), base.get("duck_enabled", True))
    for key, (lo, hi) in INTEGRATION_NUMERIC_RANGES.items():
        if key not in base:
            continue
        raw = payload.get(key, base.get(key))
        try:
            value = float(raw)
        except Exception:
            try:
                value = float(base.get(key))
            except Exception:
                continue
        result[key] = max(float(lo), min(float(hi), value))
    return result


def _apply_preset_overrides():
    with PRESET_OVERRIDES_LOCK:
        data = _preset_overrides_map_unlocked()
        for preset_key, entry in data.items():
            if preset_key not in PRESETS or not isinstance(entry, dict):
                continue
            raw = entry.get("defaults") if isinstance(entry.get("defaults"), dict) else {}
            if not raw:
                continue
            PRESETS[preset_key]["defaults"] = _sanitize_preset_defaults(preset_key, raw)


def save_preset_defaults(preset_key, payload):
    defaults = _sanitize_preset_defaults(preset_key, payload)
    updated = utcnow()
    PRESET_OVERRIDES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with PRESET_OVERRIDES_LOCK:
        data = _preset_overrides_map_unlocked()
        data[preset_key] = {"defaults": defaults, "updated_at": updated}
        tmp = PRESET_OVERRIDES_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        os.replace(tmp, PRESET_OVERRIDES_FILE)
        PRESETS[preset_key]["defaults"] = dict(defaults)
    return {"preset": preset_key, "defaults": defaults, "updated_at": updated}


def get_integration_settings(source_app):
    if source_app not in INTEGRATION_SETTINGS_APPS: raise ValueError("Unknown source app")
    with INTEGRATION_SETTINGS_LOCK:
        data = _integration_settings_map_unlocked()
        entry = data.get(source_app) if isinstance(data.get(source_app), dict) else {}
        raw = entry.get("settings") if isinstance(entry.get("settings"), dict) else {}
        updated_at = entry.get("updated_at")
    settings = _sanitize_integration_settings(raw)
    return {"source_app": source_app, "settings": settings, "updated_at": updated_at}


def save_integration_settings(source_app, payload):
    if source_app not in INTEGRATION_SETTINGS_APPS: raise ValueError("Unknown source app")
    settings = _sanitize_integration_settings(payload)
    updated = utcnow()
    with INTEGRATION_SETTINGS_LOCK:
        data = _integration_settings_map_unlocked()
        data[source_app] = {"settings": settings, "updated_at": updated}
        INTEGRATION_SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = INTEGRATION_SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        os.replace(tmp, INTEGRATION_SETTINGS_FILE)
    preset_saved = None
    if source_app == "scene_builder":
        preset_saved = save_preset_defaults(settings["preset"], settings)["preset"]
    return {"source_app": source_app, "settings": settings, "updated_at": updated, "preset_saved": preset_saved}


_apply_preset_overrides()

def safe_name(name):
    name = Path(name or "audio").name
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .")
    return (name or "audio")[:180]


def disk_free_gb():
    return shutil.disk_usage(ROOT).free / (1024 ** 3)

def mem_available_mb():
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 999999

def load1():
    try:
        return float(os.getloadavg()[0])
    except Exception:
        return 0.0

def worker_can_start(worker_index):
    if disk_free_gb() < MIN_FREE_GB or mem_available_mb() < MIN_MEM_MB:
        return False
    # Worker 0 guarantees forward progress; extra workers yield to the rest of VPS1 under pressure.
    if worker_index > 0 and load1() > max(1.0, CPU_COUNT * 0.90):
        return False
    return True

def file_sha256(path):
    h=hashlib.sha256()
    with open(path,"rb") as f:
        for chunk in iter(lambda:f.read(4*1024*1024), b""):
            h.update(chunk)
    return h.hexdigest()

def prepare_music_cache(path, base_lufs):
    p=Path(path)
    if not p.is_file():
        return p
    key=hashlib.sha256((file_sha256(p)+f"|{float(base_lufs):.2f}|v2").encode()).hexdigest()
    out=MUSIC_CACHE_DIR/(key+".flac")
    if out.is_file() and out.stat().st_size>1024:
        return out
    with MUSIC_CACHE_LOCK:
        if out.is_file() and out.stat().st_size>1024:
            return out
        tmp=out.with_suffix(".tmp.flac")
        cmd=["ffmpeg","-hide_banner","-y","-threads",str(FFMPEG_THREADS),"-i",str(p),"-af",f"loudnorm=I={float(base_lufs):.2f}:TP=-2:LRA=14:linear=false","-ar","48000","-c:a","flac","-compression_level","1",str(tmp)]
        proc=subprocess.run(cmd,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True,timeout=1800)
        if proc.returncode!=0:
            tmp.unlink(missing_ok=True)
            raise RuntimeError("Music normalization cache failed: "+proc.stderr[-1200:])
        os.replace(tmp,out)
    return out

def prune_music_cache():
    try:
        files=[p for p in MUSIC_CACHE_DIR.glob("*.flac") if p.is_file()]
        total=sum(p.stat().st_size for p in files)
        cap=int(CACHE_MAX_GB*1024**3)
        if total<=cap: return
        for p in sorted(files,key=lambda x:x.stat().st_mtime):
            if total<=int(cap*0.8): break
            sz=p.stat().st_size; p.unlink(missing_ok=True); total-=sz
    except Exception:
        pass

def performance_snapshot():
    with WORKER_STATE_LOCK:
        active=sum(1 for v in WORKER_STATE.values() if v.get("busy"))
        workers=dict(WORKER_STATE)
    with db() as conn:
        row=conn.execute("SELECT COUNT(*) c, AVG((julianday(completed_at)-julianday(started_at))*86400.0) avg_s FROM jobs WHERE status='completed' AND julianday(completed_at) >= julianday('now','-1 hour')").fetchone()
    return {"workers_total":WORKER_COUNT,"workers_active":active,"ffmpeg_threads_per_worker":FFMPEG_THREADS,"cpu_count":CPU_COUNT,"load1":round(load1(),2),"mem_available_mb":mem_available_mb(),"completed_last_hour":int(row["c"] or 0),"avg_processing_seconds":round(float(row["avg_s"] or 0),1),"music_cache_gb":round(sum(p.stat().st_size for p in MUSIC_CACHE_DIR.glob("*.flac") if p.is_file())/(1024**3),3),"workers":workers}


def ffprobe_duration(path):
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            stderr=subprocess.DEVNULL,
            timeout=30,
            text=True,
        ).strip()
        return max(0.0, float(out))
    except Exception:
        return 0.0


def clamp(value, lo, hi, default):
    try:
        return max(lo, min(hi, float(value)))
    except Exception:
        return float(default)


def boolish(value, default=True):
    if value is None:
        return bool(default)
    return str(value).strip().lower() not in {"0", "false", "off", "no", "disabled", ""}


def parse_settings(form, preset_key):
    defaults = dict(PRESETS[preset_key]["defaults"])
    mode = str(form.getfirst("mode", "auto") or "auto").strip().lower()
    if mode not in {"auto", "manual"}:
        mode = "auto"
    s = dict(defaults)
    high_level = {
        "narration_start": (0, 60), "music_start": (0, 60), "music_fade_in": (0, 30),
        "narration_fade_in": (0, 5), "music_fade_out": (0, 30), "duck_db": (0, 24),
        "duck_attack": (0.01, 2000), "duck_release": (0.01, 9000), "music_level_db": (-40, 6),
        "target_lufs": (-24, -10), "true_peak": (-6, -0.1), "output_gain_db": (-12, 12),
    }
    for key, (lo, hi) in high_level.items():
        raw = form.getfirst(key)
        if raw not in (None, ""):
            s[key] = clamp(raw, lo, hi, defaults[key])
    if mode == "manual":
        advanced = {
            "noise_floor": (-70, -20), "noise_reduction": (0.01, 24), "highpass": (30, 180), "bass_gain": (-8, 8),
            "mud_gain": (-8, 8), "presence_gain": (-8, 8), "air_gain": (-8, 8), "deesser": (0, 1),
            "comp_threshold": (0.01, 1), "comp_ratio": (1, 20), "comp_attack": (0.01, 2000),
            "comp_release": (0.01, 9000), "comp_makeup": (1, 8),
        }
        for key, (lo, hi) in advanced.items():
            raw = form.getfirst(key)
            if raw not in (None, ""):
                s[key] = clamp(raw, lo, hi, defaults[key])
    s["mode"] = mode
    s["duck_enabled"] = boolish(form.getfirst("duck_enabled"), defaults.get("duck_enabled", True))
    s["noise_enabled"] = boolish(form.getfirst("noise_enabled"), defaults.get("noise_enabled", False))
    return s


def limiter_linear(db_value):
    return max(0.05, min(0.999, 10 ** (float(db_value) / 20.0)))


def voice_filters(settings):
    filters = [f"highpass=f={settings['highpass']:.2f}"]
    if settings.get("noise_enabled", False):
        filters.append(
            f"afftdn=nf={settings.get('noise_floor',-48):.2f}:nr={settings.get('noise_reduction',5):.2f}:tn=1:gs=5"
        )
    if abs(settings["bass_gain"]) > 0.01:
        filters.append(f"equalizer=f=120:t=q:w=1:g={settings['bass_gain']:.2f}")
    if abs(settings.get("mud_gain", 0)) > 0.01:
        filters.append(f"equalizer=f=250:t=q:w=1.1:g={settings.get('mud_gain',0):.2f}")
    if abs(settings["presence_gain"]) > 0.01:
        filters.append(f"equalizer=f=3200:t=q:w=1.2:g={settings['presence_gain']:.2f}")
    if abs(settings["air_gain"]) > 0.01:
        filters.append(f"equalizer=f=9000:t=q:w=1.0:g={settings['air_gain']:.2f}")
    if settings["deesser"] > 0.001:
        filters.append(f"deesser=i={settings['deesser']:.3f}:m=0.35:f=0.58")
    filters.append(
        "acompressor="
        f"threshold={settings['comp_threshold']:.4f}:ratio={settings['comp_ratio']:.2f}:"
        f"attack={settings['comp_attack']:.2f}:release={settings['comp_release']:.2f}:makeup={settings['comp_makeup']:.2f}:knee=3"
    )
    if settings["narration_fade_in"] > 0.001:
        filters.append(f"afade=t=in:st=0:d={settings['narration_fade_in']:.3f}")
    delay_ms = int(round(settings["narration_start"] * 1000))
    if delay_ms > 0:
        filters.append(f"adelay={delay_ms}:all=1")
    return ",".join(filters)


def limiter_linear(db_value):
    return max(0.05, min(0.999, 10 ** (float(db_value) / 20.0)))


def _extract_loudnorm_json(stderr_text):
    matches = re.findall(r"\{\s*\"input_i\".*?\}", stderr_text or "", flags=re.S)
    if not matches:
        return None
    try:
        return json.loads(matches[-1])
    except Exception:
        return None


def measure_loudness(path, target_lufs=-16.0, true_peak=-1.5, lra=10.0):
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats", "-threads", str(FFMPEG_THREADS), "-i", str(path),
        "-af", f"loudnorm=I={float(target_lufs):.2f}:TP={float(true_peak):.2f}:LRA={float(lra):.2f}:print_format=json",
        "-f", "null", "-"
    ]
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=180)
    if proc.returncode != 0:
        return None
    return _extract_loudnorm_json(proc.stderr)


def two_pass_loudnorm_filter(stats, target_lufs, true_peak, lra):
    if not stats:
        return f"loudnorm=I={target_lufs:.2f}:TP={true_peak:.2f}:LRA={lra:.2f}:linear=false"
    try:
        mi=float(stats["input_i"]); mlra=float(stats["input_lra"]); mtp=float(stats["input_tp"]); mth=float(stats["input_thresh"]); off=float(stats["target_offset"])
    except Exception:
        return f"loudnorm=I={target_lufs:.2f}:TP={true_peak:.2f}:LRA={lra:.2f}:linear=false"
    return (
        f"loudnorm=I={target_lufs:.2f}:TP={true_peak:.2f}:LRA={lra:.2f}:"
        f"measured_I={mi:.2f}:measured_LRA={mlra:.2f}:measured_TP={mtp:.2f}:"
        f"measured_thresh={mth:.2f}:offset={off:.2f}:linear=true"
    )


def qc_for_output(path, settings):
    target = max(-24.0, min(-10.0, float(settings.get("target_lufs", -16)) + float(settings.get("output_gain_db", 0))))
    tp_target = float(settings.get("true_peak", -1.5))
    stats = measure_loudness(path, target, tp_target, float(settings.get("lra", 10))) or {}
    try:
        measured_lufs = float(stats.get("input_i"))
    except Exception:
        measured_lufs = None
    try:
        measured_tp = float(stats.get("input_tp"))
    except Exception:
        measured_tp = None
    loud_ok = measured_lufs is not None and abs(measured_lufs - target) <= 0.7
    peak_ok = measured_tp is not None and measured_tp <= tp_target + 0.25
    return {
        "target_lufs": round(target, 2), "target_true_peak": round(tp_target, 2),
        "measured_lufs": round(measured_lufs, 2) if measured_lufs is not None else None,
        "measured_true_peak": round(measured_tp, 2) if measured_tp is not None else None,
        "measured_lra": stats.get("input_lra"),
        "pass": bool(loud_ok and peak_ok),
    }


def duck_ratio(duck_db):
    # Calibrated for a normalized sidechain around -18 LUFS and a -36 dB threshold.
    depth=max(0.0, min(16.0, float(duck_db)))
    headroom=18.0
    if depth <= 0.1:
        return 1.0
    if depth >= headroom - 0.5:
        return 20.0
    return max(1.0, min(20.0, headroom / max(0.5, headroom-depth)))


def update_job(job_id, **fields):
    if not fields:
        return
    fields.setdefault("updated_at", utcnow())
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [job_id]
    with db() as conn:
        conn.execute(f"UPDATE jobs SET {cols} WHERE id=?", vals)
        conn.commit()


def job_row(job_id):
    with db() as conn:
        return conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def job_dict(row):
    d = dict(row)
    if d.get("output_path"):
        d["output_url"] = f"{BASE_PATH}/media/{d['id']}/output"
    d["input_url"] = f"{BASE_PATH}/media/{d['id']}/input"
    try:
        d["settings"] = json.loads(d.get("settings_json") or "{}")
    except Exception:
        d["settings"] = {}
    try:
        d["qc"] = json.loads(d.get("qc_json") or "{}")
    except Exception:
        d["qc"] = {}
    d.pop("settings_json", None)
    d.pop("qc_json", None)
    d["has_music"] = bool(d.get("music_path"))
    d.pop("music_path", None)
    return d


def claim_job(worker_id):
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at ASC LIMIT 1").fetchone()
        if not row:
            conn.commit()
            return None
        now=utcnow()
        cur=conn.execute("UPDATE jobs SET status='running', started_at=?, updated_at=?, progress=1, stage='starting', worker_id=?, attempts=COALESCE(attempts,0)+1, cancel_requested=0, error=NULL WHERE id=? AND status='queued'", (now,now,worker_id,row["id"]))
        conn.commit()
        if cur.rowcount != 1:
            return None
        return job_row(row["id"])


def _run_ffmpeg_progress(cmd, job_id, progress_duration, start_pct, end_pct, log_path):
    last_update = 0.0
    with open(log_path, "wb") as logf:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=logf, text=True, bufsize=1)
        if job_id:
            with RUNNING_LOCK:
                RUNNING_PROCS[job_id] = proc
        try:
            for raw in proc.stdout or []:
                line = raw.strip()
                if job_id and time.time() - last_update > 0.9:
                    row = job_row(job_id)
                    if row and int(row["cancel_requested"] or 0):
                        proc.terminate()
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                        raise RuntimeError("Cancelled by user")
                    last_update = time.time()
                if job_id and line.startswith("out_time_ms=") and progress_duration > 0:
                    try:
                        out_seconds = int(line.split("=", 1)[1]) / 1_000_000.0
                        frac = min(1.0, max(0.0, out_seconds / progress_duration))
                        pct = start_pct + (end_pct-start_pct)*frac
                        update_job(job_id, progress=round(pct, 1), duration=progress_duration)
                    except Exception:
                        pass
            rc = proc.wait()
        finally:
            if job_id:
                with RUNNING_LOCK:
                    RUNNING_PROCS.pop(job_id, None)
    if rc != 0:
        err = "FFmpeg processing failed"
        try:
            tail = log_path.read_text(errors="replace")[-3000:].strip()
            if tail:
                err += ": " + tail
        except Exception:
            pass
        raise RuntimeError(err)


def process_audio(input_path, output_path, preset_key, output_format, job_id=None, duration_limit=None, music_path=None, settings=None):
    settings = dict(settings or PRESETS[preset_key]["defaults"])
    duration = ffprobe_duration(input_path)
    voice_trim = duration
    if duration_limit:
        voice_trim = max(0.25, min(duration or float(duration_limit), max(0.25, float(duration_limit) - settings.get("narration_start", 0))))
    total_duration = (voice_trim or duration or 0) + settings.get("narration_start", 0)
    if duration_limit:
        total_duration = min(float(duration_limit), total_duration or float(duration_limit))
    output_path.parent.mkdir(parents=True, exist_ok=True)

    token = job_id or uuid.uuid4().hex
    premaster = TMP_DIR / f"{token}.premaster.wav"
    pre_log = TMP_DIR / f"{token}.premaster.log"
    master_log = TMP_DIR / f"{token}.master.log"
    premaster.unlink(missing_ok=True)

    cmd = ["ffmpeg", "-hide_banner", "-y", "-threads", str(FFMPEG_THREADS), "-filter_complex_threads", str(FFMPEG_THREADS), "-i", str(input_path)]
    use_music = bool(music_path and Path(music_path).is_file())
    if use_music:
        if job_id: update_job(job_id, stage="music_cache")
        music_path = prepare_music_cache(music_path, settings.get("music_base_lufs", -24.0))
        cmd += ["-stream_loop", "-1", "-i", str(music_path)]
        if job_id: update_job(job_id, stage="mixing")

    vf = voice_filters(settings)
    if voice_trim and (duration_limit or (duration and voice_trim < duration - 0.01)):
        vf = f"atrim=0:{voice_trim:.3f},asetpts=PTS-STARTPTS," + vf
    graph = [f"[0:a]{vf}[voice0]"]
    if use_music:
        graph.append("[voice0]asplit=2[voice_mix][voice_sc0]")
        graph.append("[voice_sc0]loudnorm=I=-18:TP=-3:LRA=11:linear=false[voice_sc]")
        music_start = max(0.0, settings.get("music_start", 0.0))
        music_duration = max(0.25, total_duration - music_start) if total_duration else 36000
        base_lufs = float(settings.get("music_base_lufs", -24.0))
        trim_db = float(settings.get("music_level_db", -2.0))
        mf = [
            f"atrim=0:{music_duration:.3f}", "asetpts=PTS-STARTPTS", f"volume={trim_db:.2f}dB"
        ]
        if settings.get("music_fade_in", 0) > 0.001:
            mf.append(f"afade=t=in:st=0:d={min(settings['music_fade_in'], music_duration):.3f}")
        fade_out = min(settings.get("music_fade_out", 0), music_duration)
        if fade_out > 0.001 and music_duration > fade_out:
            mf.append(f"afade=t=out:st={max(0, music_duration-fade_out):.3f}:d={fade_out:.3f}")
        delay_ms = int(round(music_start * 1000))
        if delay_ms > 0:
            mf.append(f"adelay={delay_ms}:all=1")
        graph.append(f"[1:a]{','.join(mf)}[music0]")
        if settings.get("duck_enabled", True) and settings.get("duck_db", 0) > 0.1:
            ratio = duck_ratio(settings.get("duck_db", 11))
            graph.append(
                "[music0][voice_sc]sidechaincompress="
                f"threshold=0.01585:ratio={ratio:.2f}:attack={settings.get('duck_attack',25):.2f}:"
                f"release={settings.get('duck_release',700):.2f}:knee=4:detection=rms:link=average[ducked]"
            )
        else:
            graph.append("[music0]anull[ducked]")
        graph.append("[voice_mix][ducked]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[outa]")
    else:
        graph.append("[voice0]anull[outa]")

    cmd += ["-filter_complex", ";".join(graph), "-map", "[outa]", "-vn"]
    if duration_limit:
        cmd += ["-t", str(duration_limit)]
    cmd += ["-c:a", "pcm_f32le", "-ar", "48000", "-progress", "pipe:1", "-nostats", str(premaster)]
    if job_id: update_job(job_id, stage="premaster")
    _run_ffmpeg_progress(cmd, job_id, total_duration or duration or 1.0, 2.0, 68.0, pre_log)

    target = max(-24.0, min(-10.0, float(settings.get("target_lufs", -16.0)) + float(settings.get("output_gain_db", 0.0))))
    true_peak = float(settings.get("true_peak", -1.5))
    lra = float(settings.get("lra", 10.0))
    if job_id:
        update_job(job_id, progress=72)
    if job_id: update_job(job_id, stage="loudness_analysis")
    stats = measure_loudness(premaster, target, true_peak, lra)
    filt = two_pass_loudnorm_filter(stats, target, true_peak, lra)
    lim = limiter_linear(true_peak)
    master_filter = f"{filt},alimiter=limit={lim:.6f}:attack=5:release=80:level=false"
    cmd2 = ["ffmpeg", "-hide_banner", "-y", "-threads", str(FFMPEG_THREADS), "-filter_threads", str(FFMPEG_THREADS), "-i", str(premaster), "-af", master_filter, "-vn"]
    if output_format == "wav":
        cmd2 += ["-c:a", "pcm_s24le", "-ar", "48000"]
    else:
        cmd2 += ["-c:a", "libmp3lame", "-b:a", "192k"]
    cmd2 += ["-progress", "pipe:1", "-nostats", str(output_path)]
    if job_id: update_job(job_id, stage="mastering")
    _run_ffmpeg_progress(cmd2, job_id, total_duration or duration or 1.0, 74.0, 98.0, master_log)

    for f in (premaster, pre_log, master_log):
        try:
            f.unlink(missing_ok=True)
        except Exception:
            pass
    return total_duration or duration


def worker_loop(worker_index):
    worker_id=f"audio-{worker_index+1}"
    with WORKER_STATE_LOCK:
        WORKER_STATE[worker_id]={"busy":False,"job_id":None,"heartbeat":utcnow()}
    while not STOP_EVENT.is_set():
        with WORKER_STATE_LOCK:
            WORKER_STATE[worker_id]["heartbeat"]=utcnow()
        if not worker_can_start(worker_index):
            STOP_EVENT.wait(1.5)
            continue
        job = claim_job(worker_id)
        if not job:
            STOP_EVENT.wait(0.45)
            continue
        jid = job["id"]
        with WORKER_STATE_LOCK:
            WORKER_STATE[worker_id].update({"busy":True,"job_id":jid,"heartbeat":utcnow()})
        try:
            ext = ".wav" if job["output_format"] == "wav" else ".mp3"
            stem = Path(job["original_name"]).stem
            out_dir = OUTPUT_DIR / jid
            out = out_dir / f"{stem}{ext}"
            try:
                settings = json.loads(job["settings_json"] or "{}")
            except Exception:
                settings = dict(PRESETS[job["preset"]]["defaults"])
            duration = process_audio(Path(job["input_path"]), out, job["preset"], job["output_format"], job_id=jid, music_path=job["music_path"], settings=settings)
            row = job_row(jid)
            if row and int(row["cancel_requested"] or 0):
                out.unlink(missing_ok=True)
                update_job(jid, status="cancelled", stage="cancelled", progress=0, output_path=None, completed_at=utcnow(), error="Cancelled by user")
            else:
                update_job(jid, stage="qc", progress=99)
                qc = qc_for_output(out, settings)
                update_job(jid, status="completed", stage="completed", progress=100, output_path=str(out), duration=duration, qc_json=json.dumps(qc, separators=(",", ":")), completed_at=utcnow(), error=None)
        except Exception as e:
            row = job_row(jid)
            cancelled = bool(row and int(row["cancel_requested"] or 0)) or "Cancelled by user" in str(e)
            update_job(jid, status="cancelled" if cancelled else "failed", stage="cancelled" if cancelled else "failed", progress=0 if cancelled else (row["progress"] if row else 0), completed_at=utcnow(), error=str(e)[:4000])
        finally:
            with WORKER_STATE_LOCK:
                WORKER_STATE[worker_id].update({"busy":False,"job_id":None,"heartbeat":utcnow()})


def save_upload(fileitem, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fileitem.file.seek(0)
    with open(path, "wb") as dst:
        shutil.copyfileobj(fileitem.file, dst, length=1024 * 1024)

def save_shared_music(fileitem, batch_id):
    if fileitem is None or not getattr(fileitem, "filename", None):
        return None
    music_name=safe_name(fileitem.filename)
    if Path(music_name).suffix.lower() not in ALLOWED_EXTS:
        raise ValueError(f"Unsupported music file type: {music_name}")
    path=MUSIC_DIR/batch_id/music_name
    save_upload(fileitem,path)
    return path

def cleanup_shared_music(path):
    if not path: return
    p=Path(path)
    try:
        if MUSIC_DIR not in p.parents: return
        with db() as conn:
            left=conn.execute("SELECT COUNT(*) c FROM jobs WHERE music_path=?",(str(p),)).fetchone()["c"]
        if int(left or 0)==0:
            shutil.rmtree(p.parent,ignore_errors=True)
    except Exception:
        pass


def enqueue_file(fileitem, preset, output_format, source_app="web", external_id=None, settings=None, music_item=None, music_path_override=None, batch_id=None):
    if disk_free_gb() < MIN_FREE_GB:
        raise ValueError(f"Low disk: at least {MIN_FREE_GB:g} GB free is required")
    filename = safe_name(getattr(fileitem, "filename", None))
    if Path(filename).suffix.lower() not in ALLOWED_EXTS:
        raise ValueError(f"Unsupported file type: {filename}")
    jid = uuid.uuid4().hex
    in_dir = INPUT_DIR / jid
    in_path = in_dir / filename
    save_upload(fileitem, in_path)
    music_path = Path(music_path_override) if music_path_override else None
    if music_path is None and music_item is not None and getattr(music_item, "filename", None):
        music_name = safe_name(music_item.filename)
        if Path(music_name).suffix.lower() not in ALLOWED_EXTS:
            raise ValueError(f"Unsupported music file type: {music_name}")
        music_path = in_dir / ("music-" + music_name)
        save_upload(music_item, music_path)
    settings = dict(settings or PRESETS[preset]["defaults"])
    with db() as conn:
        conn.execute(
            "INSERT INTO jobs(id, external_id, source_app, original_name, input_path, music_path, settings_json, preset, output_format, status, progress, stage, batch_id, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (jid, external_id, source_app[:40], filename, str(in_path), str(music_path) if music_path else None, json.dumps(settings, separators=(",", ":")), preset, output_format, "queued", 0, "queued", batch_id, utcnow(), utcnow()),
        )
        conn.commit()
    return job_dict(job_row(jid))


def counts():
    result = {"queued": 0, "running": 0, "completed": 0, "failed": 0, "cancelled": 0}
    with db() as conn:
        for row in conn.execute("SELECT status, COUNT(*) c FROM jobs GROUP BY status"):
            result[row["status"]] = row["c"]
    return result


UI_HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Audio Engine</title>
<style>
:root{--bg:#f5f7fb;--card:#fff;--line:#dde3ee;--text:#14213d;--muted:#667085;--brand:#3157d5;--brand2:#2446b5;--good:#16845b;--bad:#c13b36;--warn:#b87500;--shadow:0 10px 28px rgba(28,46,86,.07)}*{box-sizing:border-box}body{margin:0;font-family:Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif;background:var(--bg);color:var(--text)}button,input,select{font:inherit}.shell{max-width:1450px;margin:auto;padding:22px}.top{display:flex;justify-content:space-between;gap:18px;align-items:center;margin-bottom:18px}.title h1{margin:0;font-size:28px}.title p{margin:5px 0 0;color:var(--muted);font-size:13px}.health{display:flex;align-items:center;gap:8px;font-size:12px;font-weight:750;background:#edf9f3;color:var(--good);border:1px solid #c7ead9;padding:8px 11px;border-radius:999px}.dot{width:8px;height:8px;border-radius:50%;background:currentColor}.tabs{display:flex;gap:6px;padding:5px;background:#e8edf6;border:1px solid #d9e0eb;border-radius:13px;margin-bottom:16px;overflow:auto}.tab{border:0;background:transparent;color:#536174;font-weight:750;padding:10px 16px;border-radius:9px;cursor:pointer;white-space:nowrap}.tab.active{background:#fff;color:var(--brand);box-shadow:0 3px 10px rgba(28,46,86,.1)}.panel{display:none}.panel.active{display:block}.stack{display:grid;gap:14px}.grid{display:grid;grid-template-columns:1.3fr .7fr;gap:14px}.two{display:grid;grid-template-columns:1fr 1fr;gap:12px}.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:17px;box-shadow:var(--shadow)}.card h2{font-size:16px;margin:0 0 4px}.sub{font-size:12px;color:var(--muted);margin:4px 0 14px;line-height:1.5}.timing{display:grid;grid-template-columns:repeat(5,1fr);gap:10px}.field label{display:block;font-size:10px;font-weight:800;color:#59667a;margin-bottom:5px;text-transform:uppercase;letter-spacing:.05em}.field input,.field select{width:100%;border:1px solid var(--line);background:#fff;padding:9px 10px;border-radius:10px;color:var(--text)}.field small{display:block;color:var(--muted);font-size:10px;margin-top:4px}.drop{border:1.5px dashed #b7c2d5;background:#f9fbfe;border-radius:14px;padding:20px;text-align:center}.drop.music{background:#fbfcff}.drop strong{display:block;margin-bottom:4px}.drop small{color:var(--muted)}.drop input{display:block;margin:12px auto 0;max-width:100%}.controls{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.modebar{display:inline-flex;background:#eef1f6;border:1px solid var(--line);padding:3px;border-radius:10px}.modebtn{border:0;background:transparent;padding:8px 18px;border-radius:8px;font-weight:800;color:#667085;cursor:pointer}.modebtn.active{background:#fff;color:var(--brand);box-shadow:0 2px 8px rgba(28,46,86,.1)}.auto-summary{margin-top:12px;padding:11px 12px;background:#f4f7ff;border:1px solid #dce4fb;border-radius:11px;font-size:12px;color:#405174}.advanced{margin-top:13px;border-top:1px solid var(--line);padding-top:13px}.advanced[hidden]{display:none}.advanced-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.duck-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:10px}.toggle{display:flex;align-items:center;gap:8px;font-size:12px;font-weight:750}.toggle input{width:auto}.actions{display:flex;gap:9px;flex-wrap:wrap;margin-top:14px}.btn{border:1px solid var(--line);background:#fff;color:#344054;padding:10px 14px;border-radius:10px;font-weight:750;cursor:pointer}.btn.primary{background:var(--brand);border-color:var(--brand);color:#fff}.btn.primary:hover{background:var(--brand2)}.btn:disabled{opacity:.5;cursor:not-allowed}.preview{margin-top:14px;border-top:1px solid var(--line);padding-top:14px}.preview-pair{display:grid;grid-template-columns:1fr 1fr;gap:12px}.preview-box{background:#f8fafc;border:1px solid var(--line);border-radius:11px;padding:10px}.preview audio{width:100%;margin-top:7px}.stats{display:grid;grid-template-columns:repeat(2,1fr);gap:9px}.stat{border:1px solid var(--line);border-radius:12px;padding:12px}.stat b{font-size:23px}.stat span{display:block;color:var(--muted);font-size:11px;margin-top:2px}.notice{display:none;margin-bottom:14px;padding:11px 13px;border-radius:10px;background:#eef3ff;color:#2446b5;font-size:13px}.notice.bad{background:#fff0ef;color:#a8302d}.table-card{padding:0;overflow:hidden}.table-head{padding:16px 17px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;gap:10px;align-items:center}.table-wrap{overflow:auto}.jobs{width:100%;border-collapse:collapse;font-size:12px;min-width:1000px}.jobs th,.jobs td{text-align:left;padding:11px 12px;border-bottom:1px solid #edf0f5;vertical-align:middle}.jobs th{font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);background:#fafbfc}.status{display:inline-flex;padding:4px 8px;border-radius:999px;background:#eef2ff;color:#3c4fd0;font-weight:800}.status.completed{background:#e9f8ef;color:var(--good)}.status.failed,.status.cancelled{background:#fff0ef;color:var(--bad)}.status.running{background:#fff6df;color:var(--warn)}.qc-ok{color:var(--good);font-weight:800}.qc-warn{color:var(--warn);font-weight:800}.bar{width:120px;height:7px;border-radius:999px;background:#edf0f5;overflow:hidden}.bar i{display:block;height:100%;background:var(--brand);border-radius:999px}.row-actions{display:flex;gap:5px}.mini{padding:6px 8px;border-radius:7px;border:1px solid var(--line);background:#fff;font-size:11px;cursor:pointer}.preset-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:12px}.preset-card{background:#fff;border:1px solid var(--line);border-radius:14px;padding:15px}.preset-card h3{font-size:14px;margin:0 0 5px}.preset-card p{font-size:12px;color:var(--muted);line-height:1.5}.pipeline{font-size:11px;color:#344054;background:#f6f8fb;border-radius:9px;padding:9px;line-height:1.55}.preset-meta{font-size:10px;color:#667085;margin-top:8px}.integration{display:grid;grid-template-columns:1fr 1fr;gap:14px}.code{background:#111827;color:#e5ecfa;border-radius:12px;padding:14px;white-space:pre-wrap;overflow:auto;font:12px ui-monospace,SFMono-Regular,Consolas,monospace}.tag{display:inline-flex;border:1px solid #cdd7ef;background:#f5f7ff;color:#4056b5;border-radius:999px;padding:4px 8px;font-size:10px;font-weight:800;margin-right:5px}@media(max-width:1050px){.timing{grid-template-columns:repeat(3,1fr)}.advanced-grid,.duck-grid{grid-template-columns:repeat(2,1fr)}}@media(max-width:900px){.grid,.two,.integration{grid-template-columns:1fr}.shell{padding:14px}.top{align-items:flex-start;flex-direction:column}}@media(max-width:600px){.timing,.controls,.advanced-grid,.duck-grid,.preview-pair{grid-template-columns:1fr 1fr}.actions .btn{flex:1}.title h1{font-size:23px}}@media(max-width:430px){.timing,.controls,.advanced-grid,.duck-grid,.preview-pair{grid-template-columns:1fr}}
</style></head><body><main class="shell">
<div class="top"><div class="title"><h1>Audio Engine</h1><p>Documentary-first narration + music processing · standalone today, ACS + Scene Builder ready later</p></div><div id="health" class="health"><span class="dot"></span><span>Checking engine</span></div></div>
<div class="tabs"><button class="tab active" data-tab="process">Process</button><button class="tab" data-tab="queue">Queue</button><button class="tab" data-tab="presets">Presets</button><button class="tab" data-tab="integration">Integration</button></div><div id="notice" class="notice"></div>
<section class="panel active" data-panel="process"><form id="upload-form" class="stack">
<div class="card"><h2>1. Start timing</h2><div class="sub">Control exactly when narration and music begin. These controls work in both AUTO and MANUAL modes.</div><div class="timing">
<div class="field"><label>Narration starts after</label><input id="narration_start" name="narration_start" type="number" min="0" max="60" step="0.1" value="2.5"><small>seconds</small></div>
<div class="field"><label>Music starts after</label><input id="music_start" name="music_start" type="number" min="0" max="60" step="0.1" value="0"><small>seconds</small></div>
<div class="field"><label>Music fade-in</label><input id="music_fade_in" name="music_fade_in" type="number" min="0" max="30" step="0.1" value="2"><small>seconds</small></div>
<div class="field"><label>Narration fade-in</label><input id="narration_fade_in" name="narration_fade_in" type="number" min="0" max="5" step="0.05" value="0.15"><small>seconds</small></div>
<div class="field"><label>Music end fade-out</label><input id="music_fade_out" name="music_fade_out" type="number" min="0" max="30" step="0.1" value="3"><small>seconds</small></div>
</div></div>
<div class="two"><div class="card"><h2>2. Narration</h2><div class="sub">Upload one narration or a batch. Permanent-ID filenames are preserved.</div><div class="drop"><strong>Drop or select narration files</strong><small>WAV · MP3 · M4A · AAC · FLAC · OGG · OPUS · up to 500</small><input id="files" name="files" type="file" multiple accept="audio/*,.m4a,.aac,.flac,.ogg,.opus"></div></div>
<div class="card"><h2>3. Background music <span class="tag">Optional</span></h2><div class="sub">One music track can be applied across the narration batch. It loops as needed and fades out with the narration.</div><div class="drop music"><strong>Drop or select music</strong><small>Auto Duck lowers this track only while narration is present.</small><input id="music_file" name="music_file" type="file" accept="audio/*,.m4a,.aac,.flac,.ogg,.opus"></div></div></div>
<div class="grid"><div class="card"><h2>4. Processing</h2><div class="sub">Documentary Voice is the default. AUTO uses safe recommendations; MANUAL opens engineering controls.</div>
<div class="controls"><div class="field"><label>Preset</label><select id="preset" name="preset"></select></div><div class="field"><label>Mode</label><div class="modebar"><button class="modebtn active" type="button" data-mode="auto">AUTO</button><button class="modebtn" type="button" data-mode="manual">MANUAL</button></div><input id="mode" name="mode" type="hidden" value="auto"></div><div class="field"><label>Output</label><select id="format" name="output_format"><option value="mp3">MP3 · 192 kbps</option><option value="wav">WAV · 48 kHz</option></select></div></div>
<div id="auto-summary" class="auto-summary">Documentary AUTO · voice cleanup · automatic music ducking · -16 LUFS</div>
<div class="duck-grid"><label class="toggle"><input id="duck_enabled" name="duck_enabled" type="checkbox" value="1" checked> Auto Music Duck</label><div class="field"><label>Duck depth</label><input id="duck_db" name="duck_db" type="number" min="0" max="24" step="0.5" value="11"><small>approx dB</small></div><div class="field"><label>Music bed trim</label><input id="music_level_db" name="music_level_db" type="number" min="-40" max="6" step="0.5" value="-2"><small>dB after auto-level</small></div><div class="field"><label>Target loudness</label><input id="target_lufs" name="target_lufs" type="number" min="-24" max="-10" step="0.5" value="-16"><small>LUFS</small></div></div>
<div id="advanced" class="advanced" hidden><div class="advanced-grid">
<label class="toggle"><input id="noise_enabled" name="noise_enabled" type="checkbox" value="1"> Noise cleanup</label><div class="field"><label>Noise floor</label><input id="noise_floor" name="noise_floor" type="number" min="-70" max="-20" step="1" value="-48"><small>dB</small></div><div class="field"><label>Noise reduction</label><input id="noise_reduction" name="noise_reduction" type="number" min="0" max="24" step="1" value="5"><small>dB</small></div><div class="field"><label>High-pass</label><input id="highpass" name="highpass" type="number" min="30" max="180" step="5" value="70"><small>Hz</small></div><div class="field"><label>De-esser</label><input id="deesser" name="deesser" type="number" min="0" max="1" step="0.05" value="0.18"><small>0–1 intensity</small></div>
<div class="field"><label>Bass EQ</label><input id="bass_gain" name="bass_gain" type="number" min="-8" max="8" step="0.5" value="0.8"><small>dB @ 120 Hz</small></div><div class="field"><label>Mud control</label><input id="mud_gain" name="mud_gain" type="number" min="-8" max="8" step="0.5" value="-1.2"><small>dB @ 250 Hz</small></div><div class="field"><label>Presence EQ</label><input id="presence_gain" name="presence_gain" type="number" min="-8" max="8" step="0.5" value="1.2"><small>dB @ 3.2 kHz</small></div><div class="field"><label>Air EQ</label><input id="air_gain" name="air_gain" type="number" min="-8" max="8" step="0.5" value="0.3"><small>dB @ 8 kHz</small></div><div class="field"><label>Comp threshold</label><input id="comp_threshold" name="comp_threshold" type="number" min="0.01" max="1" step="0.005" value="0.18"></div>
<div class="field"><label>Comp ratio</label><input id="comp_ratio" name="comp_ratio" type="number" min="1" max="20" step="0.1" value="3"></div><div class="field"><label>Comp attack</label><input id="comp_attack" name="comp_attack" type="number" min="0.01" max="2000" step="1" value="12"><small>ms</small></div><div class="field"><label>Comp release</label><input id="comp_release" name="comp_release" type="number" min="0.01" max="9000" step="5" value="140"><small>ms</small></div><div class="field"><label>Comp makeup</label><input id="comp_makeup" name="comp_makeup" type="number" min="1" max="8" step="0.1" value="1.8"></div>
<div class="field"><label>Duck attack</label><input id="duck_attack" name="duck_attack" type="number" min="0.01" max="2000" step="5" value="45"><small>ms</small></div><div class="field"><label>Duck release</label><input id="duck_release" name="duck_release" type="number" min="0.01" max="9000" step="25" value="650"><small>ms</small></div><div class="field"><label>Limiter ceiling</label><input id="true_peak" name="true_peak" type="number" min="-6" max="-0.1" step="0.1" value="-1.5"><small>dBTP</small></div><div class="field"><label>Loudness trim</label><input id="output_gain_db" name="output_gain_db" type="number" min="-12" max="12" step="0.5" value="0"><small>dB</small></div>
</div><div class="actions"><button id="reset-recommended" class="btn" type="button">Reset to recommended</button></div></div>
<input type="hidden" name="source_app" value="web"><div class="actions"><button id="process-btn" class="btn primary" type="button">Process batch</button><button id="preview-btn" class="btn" type="button">Preview first 20 sec</button></div>
<div id="preview" class="preview" hidden><div class="preview-pair"><div class="preview-box"><strong>Original narration</strong><audio id="original-audio" controls></audio></div><div class="preview-box"><strong>Processed mix</strong><audio id="preview-audio" controls></audio></div></div></div></div>
<div class="card"><h2>Engine status</h2><div class="sub">Preview remains separate from the production queue.</div><div class="stats"><div class="stat"><b id="s-queued">0</b><span>Queued</span></div><div class="stat"><b id="s-running">0</b><span>Running</span></div><div class="stat"><b id="s-done">0</b><span>Completed</span></div><div class="stat"><b id="s-failed">0</b><span>Failed / cancelled</span></div></div><div class="preview"><span class="tag">Documentary-first</span><span class="tag">Auto Duck</span><span class="tag">API v2</span><span class="tag">Multi-worker</span><p class="sub" style="margin-top:10px">Timing and processing settings are stored with every job for future ACS and Scene Builder submission.</p></div></div></div>
</form></section>
<section class="panel" data-panel="queue"><div class="card table-card"><div class="table-head"><div><h2>Processing queue</h2><div class="sub" style="margin:3px 0 0">Live status, mode, music, progress and retry controls.</div></div><button id="refresh" class="btn">Refresh</button></div><div class="table-wrap"><table class="jobs"><thead><tr><th>File</th><th>Preset / Mode</th><th>Music</th><th>Status</th><th>Progress</th><th>Created</th><th>Audio</th><th>Actions</th></tr></thead><tbody id="jobs-body"></tbody></table></div></div></section>
<section class="panel" data-panel="presets"><div class="card" style="margin-bottom:12px"><h2>Preset behavior</h2><div class="sub">AUTO uses recommended values. MANUAL exposes the detailed voice chain.</div></div><div id="preset-grid" class="preset-grid"></div></section>
<section class="panel" data-panel="integration"><div class="integration"><div class="card"><h2>Future app integration</h2><div class="sub">The engine remains standalone. ACS and Scene Builder can later submit the same timing/music/settings payload.</div><p><span class="tag">ACS</span> narration + optional music → Audio Engine → caption/render</p><p><span class="tag">Scene Builder</span> narration + optional music → Audio Engine → editor/render</p></div><div class="card"><h2>API contract</h2><div class="code">GET  /audio-engine/health
GET  /audio-engine/api/v1/capabilities
GET  /audio-engine/api/v1/presets
GET  /audio-engine/api/v1/jobs
POST /audio-engine/api/v1/jobs
  files[] + optional music_file
  mode=auto|manual
  narration_start / music_start / duck controls
POST /audio-engine/api/v1/preview</div></div></div></section>
</main><script>
const B='/audio-engine', $=id=>document.getElementById(id);let presets={},objectUrl=null;
function notice(msg,bad=false){const n=$('notice');n.textContent=msg;n.className='notice'+(bad?' bad':'');n.style.display='block';setTimeout(()=>n.style.display='none',5000)}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}function when(s){try{return new Date(s).toLocaleString()}catch(e){return s||''}}
document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>{document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('active',x===b));document.querySelectorAll('.panel').forEach(p=>p.classList.toggle('active',p.dataset.panel===b.dataset.tab));if(b.dataset.tab==='queue')loadJobs()});
document.querySelectorAll('.modebtn').forEach(b=>b.onclick=()=>setMode(b.dataset.mode));
function setMode(m){$('mode').value=m;document.querySelectorAll('.modebtn').forEach(x=>x.classList.toggle('active',x.dataset.mode===m));$('advanced').hidden=m!=='manual';updateSummary()}
function applyDefaults(key){const p=presets[key];if(!p||!p.defaults)return;Object.entries(p.defaults).forEach(([k,v])=>{const el=$(k);if(el&&el.type!=='checkbox')el.value=v});$('duck_enabled').checked=(p.defaults.duck_enabled??true);$('noise_enabled').checked=!!p.defaults.noise_enabled;updateSummary()}
function updateSummary(){const p=presets[$('preset').value];if(!p)return;$('auto-summary').textContent=$('mode').value==='auto'?`${p.name} AUTO · duck ${$('duck_db').value} dB · music trim ${$('music_level_db').value} dB · ${$('target_lufs').value} LUFS`:'MANUAL enabled · EQ, de-esser, compressor, duck timing, limiter and gain are editable.'}
async function loadPresets(){const r=await fetch(B+'/api/v1/presets');const d=await r.json();presets=d.presets||{};const s=$('preset');s.innerHTML='';$('preset-grid').innerHTML='';Object.entries(presets).forEach(([k,p])=>{s.insertAdjacentHTML('beforeend',`<option value="${k}">${p.name}</option>`);const d=p.defaults||{};$('preset-grid').insertAdjacentHTML('beforeend',`<article class="preset-card"><h3>${p.name}</h3><p>${p.description}</p><div class="pipeline">${p.pipeline||'Voice processing → Loudness → Limiter'}</div><div class="preset-meta">AUTO: narration ${d.narration_start??0}s · music ${d.music_start??0}s · duck ${d.duck_db??0} dB · target ${d.target_lufs??''} LUFS</div><div class="actions"><button class="mini" onclick="usePreset('${k}')">Use preset</button></div></article>`)}) ;s.value='documentary';applyDefaults('documentary')}
function usePreset(k){document.querySelector('[data-tab="process"]').click();$('preset').value=k;applyDefaults(k)}$('preset').onchange=()=>applyDefaults($('preset').value);$('reset-recommended').onclick=()=>applyDefaults($('preset').value);['duck_db','music_level_db','target_lufs'].forEach(id=>$(id).oninput=updateSummary);
async function health(){try{const r=await fetch(B+'/health');const d=await r.json();$('health').innerHTML='<span class="dot"></span><span>Engine online · '+d.free_gb+' GB free · '+(d.performance?.workers_total||1)+' workers</span>';$('s-queued').textContent=d.queue.queued||0;$('s-running').textContent=d.queue.running||0;$('s-done').textContent=d.queue.completed||0;$('s-failed').textContent=(d.queue.failed||0)+(d.queue.cancelled||0)}catch(e){$('health').style.color='#c13b36';$('health').textContent='Engine unavailable'}}
async function loadJobs(){try{const r=await fetch(B+'/api/v1/jobs?limit=200');const d=await r.json();const tb=$('jobs-body');tb.innerHTML='';(d.jobs||[]).forEach(j=>{const audio=j.status==='completed'?`<audio controls preload="none" src="${j.output_url}"></audio>`:'—';let act='';if(j.status==='queued'||j.status==='running')act=`<button class="mini" onclick="jobAction('${j.id}','cancel')">Cancel</button>`;else if(j.status==='failed'||j.status==='cancelled')act=`<button class="mini" onclick="jobAction('${j.id}','retry')">Retry</button>`;if(['completed','failed','cancelled'].includes(j.status))act+=` <button class="mini" onclick="jobAction('${j.id}','delete')">Delete</button>`;const mode=(j.settings?.mode||'auto').toUpperCase();tb.insertAdjacentHTML('beforeend',`<tr><td><b>${esc(j.original_name)}</b><br><small>${esc(j.external_id||j.id.slice(0,12))}</small></td><td>${esc(presets[j.preset]?.name||j.preset)}<br><small>${mode} · voice +${Number(j.settings?.narration_start||0)}s</small></td><td>${j.has_music?`<span class="tag">Music</span><br><small>duck ${Number(j.settings?.duck_db||0)} dB</small>`:'—'}</td><td><span class="status ${j.status}">${j.status}</span>${j.qc?.measured_lufs!=null?`<br><small class="${j.qc.pass?'qc-ok':'qc-warn'}">QC ${Number(j.qc.measured_lufs).toFixed(1)} LUFS · ${Number(j.qc.measured_true_peak).toFixed(1)} dBTP ${j.qc.pass?'✓':'⚠'}</small>`:''}${j.error?`<br><small title="${esc(j.error)}">${esc(j.error.slice(0,85))}</small>`:''}</td><td><div class="bar"><i style="width:${Number(j.progress||0)}%"></i></div><small>${Number(j.progress||0).toFixed(0)}%</small></td><td>${when(j.created_at)}</td><td>${audio}</td><td><div class="row-actions">${act}</div></td></tr>`)})}catch(e){notice('Could not load queue',true)}}
async function jobAction(id,action){const r=await fetch(`${B}/api/v1/jobs/${id}/${action}`,{method:'POST'});const d=await r.json();if(!r.ok)notice(d.error||'Action failed',true);await loadJobs();await health()}
async function submitBatch(){const btn=$('process-btn');try{const form=$('upload-form'),fs=$('files').files;if(!fs.length){notice('Select at least one narration file',true);return}if(fs.length>500){notice('Maximum batch size is 500 files',true);return}btn.disabled=true;btn.textContent='Submitting…';notice('Submitting batch to Audio Engine…');const fd=new FormData(form);if(!$('duck_enabled').checked)fd.set('duck_enabled','0');if(!$('noise_enabled').checked)fd.set('noise_enabled','0');const r=await fetch(B+'/api/v1/jobs',{method:'POST',body:fd});const raw=await r.text();let d={};try{d=raw?JSON.parse(raw):{}}catch(_){throw new Error('Audio Engine returned an invalid response')}if(!r.ok)throw new Error(d.error||`Submit failed (HTTP ${r.status})`);const jobs=Array.isArray(d.jobs)?d.jobs:[];notice(`${jobs.length} file(s) added to queue${$('music_file').files[0]?' with background music':''}`);document.querySelector('[data-tab="queue"]').click();await loadJobs()}catch(err){console.error('Audio Engine batch submit failed',err);notice(err?.message||'Batch submit failed',true)}finally{btn.disabled=false;btn.textContent='Process batch';await health()}}
$('process-btn').onclick=submitBatch;$('upload-form').onsubmit=e=>{e.preventDefault();submitBatch()};
$('preview-btn').onclick=async()=>{const f=$('files').files[0];if(!f)return notice('Select a narration file first',true);const fd=new FormData($('upload-form'));fd.delete('files');fd.append('file',f);if(!$('duck_enabled').checked)fd.set('duck_enabled','0');if(!$('noise_enabled').checked)fd.set('noise_enabled','0');const b=$('preview-btn');b.disabled=true;b.textContent='Building preview…';try{const r=await fetch(B+'/api/v1/preview',{method:'POST',body:fd});const d=await r.json();if(!r.ok)throw new Error(d.error||'Preview failed');$('preview').hidden=false;if(objectUrl)URL.revokeObjectURL(objectUrl);objectUrl=URL.createObjectURL(f);$('original-audio').src=objectUrl;$('preview-audio').src=d.preview_url+'?t='+Date.now();$('preview-audio').play().catch(()=>{})}catch(err){notice(err.message,true)}finally{b.disabled=false;b.textContent='Preview first 20 sec'}};
$('refresh').onclick=()=>{loadJobs();health()};setMode('auto');Promise.all([loadPresets(),health()]).then(loadJobs);setInterval(()=>{health();if(document.querySelector('[data-tab="queue"]').classList.contains('active'))loadJobs()},3000);
</script></body></html>'''


class Handler(BaseHTTPRequestHandler):
    server_version = "AudioEngine/1.1"

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}")

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, text, status=200):
        body = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location, status=308):
        self.send_response(status)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _form(self):
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            raise ValueError("multipart/form-data required")
        length = int(self.headers.get("Content-Length", "0") or 0)
        return cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": ctype, "CONTENT_LENGTH": str(length)},
            keep_blank_values=True,
        )

    def _send_media(self, path, download_name=None):
        p = Path(path)
        if not p.is_file():
            return self._json({"error": "file not found"}, 404)
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        size = p.stat().st_size
        range_header = self.headers.get("Range")
        start, end = 0, size - 1
        status = 200
        if range_header and range_header.startswith("bytes="):
            m = re.match(r"bytes=(\d*)-(\d*)", range_header)
            if m:
                if m.group(1): start = int(m.group(1))
                if m.group(2): end = min(end, int(m.group(2)))
                if start > end or start >= size:
                    self.send_response(416); self.end_headers(); return
                status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if download_name:
            self.send_header("Content-Disposition", f'inline; filename="{safe_name(download_name)}"')
        self.end_headers()
        with open(p, "rb") as f:
            f.seek(start)
            remain = length
            while remain > 0:
                chunk = f.read(min(1024 * 1024, remain))
                if not chunk: break
                self.wfile.write(chunk); remain -= len(chunk)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            return self._redirect(BASE_PATH + "/")
        if path == BASE_PATH:
            return self._redirect(BASE_PATH + "/")
        if path == BASE_PATH + "/":
            return self._html(UI_HTML)
        if path == BASE_PATH + "/health":
            return self._json({"ok": True, "service": "audio-engine", "version": VERSION, "worker": "running", "ffmpeg": shutil.which("ffmpeg") is not None, "free_gb": round(disk_free_gb(), 1), "queue": counts(), "performance": performance_snapshot(), "integrations": ["web", "scene_builder"]})
        if path == BASE_PATH + "/api/v1/capabilities":
            return self._json({"version": VERSION, "max_batch": MAX_BATCH, "input_extensions": sorted(ALLOWED_EXTS), "output_formats": ["mp3", "wav"], "preview_separate": True, "source_apps": ["web", "scene_builder"], "external_id": True, "callbacks": False, "processing_modes": ["auto", "manual"], "music_ducking": True, "two_pass_loudness": True, "true_peak_qc": True, "music_auto_level": True, "multi_worker": True, "resource_aware_concurrency": True, "shared_music_upload": True, "music_normalization_cache": True, "sqlite_wal": True, "api_metrics": True, "timing_controls": ["narration_start", "music_start", "music_fade_in", "narration_fade_in", "music_fade_out"]})
        if path == BASE_PATH + "/api/v2/metrics":
            return self._json({"ok": True, "performance": performance_snapshot(), "queue": counts(), "free_gb": round(disk_free_gb(),1)})
        if path == BASE_PATH + "/api/v1/presets":
            public = {k: {"name": v["name"], "description": v["description"], "pipeline": v["pipeline"], "defaults": v["defaults"]} for k, v in PRESETS.items()}
            return self._json({"presets": public})
        m = re.fullmatch(re.escape(BASE_PATH) + r"/api/v1/settings/(web|acs|scene_builder)", path)
        if m:
            try:
                data = get_integration_settings(m.group(1))
                return self._json({"ok": True, **data})
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            except Exception as e:
                return self._json({"error": str(e)[:1000]}, 500)
        if path == BASE_PATH + "/api/v1/jobs":
            qs = parse_qs(parsed.query)
            try: limit = max(1, min(500, int(qs.get("limit", [200])[0])))
            except Exception: limit = 200
            with db() as conn:
                rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            return self._json({"jobs": [job_dict(r) for r in rows], "queue": counts()})
        m = re.fullmatch(re.escape(BASE_PATH) + r"/api/v1/jobs/([a-f0-9]{32})", path)
        if m:
            row = job_row(m.group(1))
            return self._json(job_dict(row) if row else {"error": "job not found"}, 200 if row else 404)
        m = re.fullmatch(re.escape(BASE_PATH) + r"/media/([a-f0-9]{32})/(input|output)", path)
        if m:
            row = job_row(m.group(1))
            if not row: return self._json({"error": "job not found"}, 404)
            if m.group(2) == "input": return self._send_media(row["input_path"], row["original_name"])
            if not row["output_path"]: return self._json({"error": "output not ready"}, 404)
            return self._send_media(row["output_path"], Path(row["output_path"]).name)
        m = re.fullmatch(re.escape(BASE_PATH) + r"/media/preview/([a-f0-9]{32})", path)
        if m:
            p = PREVIEW_DIR / f"{m.group(1)}.mp3"
            return self._send_media(p, "preview.mp3")
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        # SCENE_AUDIO_ENGINE_SETTINGS_POST_V1
        m = re.fullmatch(re.escape(BASE_PATH) + r"/api/v1/settings/(web|acs|scene_builder)", path)
        if m:
            try:
                ctype = self.headers.get("Content-Type", "")
                if "application/json" not in ctype:
                    raise ValueError("application/json required")
                length = int(self.headers.get("Content-Length", "0") or 0)
                if length < 0 or length > 262144:
                    raise ValueError("settings payload too large")
                raw = self.rfile.read(length) if length else b"{}"
                payload = json.loads(raw.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    raise ValueError("settings payload must be an object")
                data = save_integration_settings(m.group(1), payload)
                return self._json({"ok": True, **data})
            except (ValueError, json.JSONDecodeError) as e:
                return self._json({"error": str(e)}, 400)
            except Exception as e:
                return self._json({"error": str(e)[:1000]}, 500)
        if path == BASE_PATH + "/api/v1/jobs":
            try:
                form = self._form()
                preset = str(form.getfirst("preset", "documentary"))
                output_format = str(form.getfirst("output_format", "mp3"))
                source_app = str(form.getfirst("source_app", "web"))
                external_id = form.getfirst("external_id")
                if preset not in PRESETS:
                    raise ValueError("Unknown preset")
                if output_format not in {"mp3", "wav"}:
                    raise ValueError("Output format must be mp3 or wav")
                if source_app not in {"web", "scene_builder"}:
                    source_app = "web"
                settings = parse_settings(form, preset)
                music_item = form["music_file"] if "music_file" in form and getattr(form["music_file"], "filename", None) else None
                if "files" not in form:
                    raise ValueError("No narration files uploaded")
                items = form["files"] if isinstance(form["files"], list) else [form["files"]]
                items = [x for x in items if getattr(x, "filename", None)]
                if not items:
                    raise ValueError("No narration files uploaded")
                if len(items) > MAX_BATCH:
                    raise ValueError(f"Maximum batch size is {MAX_BATCH}")
                batch_id = uuid.uuid4().hex
                shared_music = save_shared_music(music_item, batch_id) if music_item is not None else None
                jobs = [enqueue_file(item, preset, output_format, source_app, external_id, settings, None, shared_music, batch_id) for item in items]
                return self._json({"ok": True, "jobs": jobs, "batch_id": batch_id, "shared_music": bool(shared_music)}, 201)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            except Exception as e:
                return self._json({"error": str(e)}, 500)
        if path == BASE_PATH + "/api/v1/preview":
            try:
                form = self._form()
                preset = str(form.getfirst("preset", "documentary"))
                if preset not in PRESETS:
                    raise ValueError("Unknown preset")
                settings = parse_settings(form, preset)
                if "file" not in form or not getattr(form["file"], "filename", None):
                    raise ValueError("No preview narration uploaded")
                item = form["file"]
                filename = safe_name(item.filename)
                if Path(filename).suffix.lower() not in ALLOWED_EXTS:
                    raise ValueError("Unsupported preview file type")
                token = uuid.uuid4().hex
                tmp_in = TMP_DIR / f"preview-{token}-{filename}"
                save_upload(item, tmp_in)
                tmp_music = None
                if "music_file" in form and getattr(form["music_file"], "filename", None):
                    mi = form["music_file"]
                    mn = safe_name(mi.filename)
                    if Path(mn).suffix.lower() not in ALLOWED_EXTS:
                        raise ValueError("Unsupported music file type")
                    tmp_music = TMP_DIR / f"preview-{token}-music-{mn}"
                    save_upload(mi, tmp_music)
                out = PREVIEW_DIR / f"{token}.mp3"
                try:
                    process_audio(tmp_in, out, preset, "mp3", duration_limit=20, music_path=tmp_music, settings=settings)
                finally:
                    tmp_in.unlink(missing_ok=True)
                    if tmp_music:
                        tmp_music.unlink(missing_ok=True)
                cutoff = time.time() - 86400
                for p in PREVIEW_DIR.glob("*.mp3"):
                    try:
                        if p.stat().st_mtime < cutoff:
                            p.unlink()
                    except Exception:
                        pass
                return self._json({"ok": True, "preview_url": f"{BASE_PATH}/media/preview/{token}", "seconds": 20, "mode": settings["mode"], "music": bool(tmp_music)})
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            except Exception as e:
                return self._json({"error": str(e)[:4000]}, 500)
        m = re.fullmatch(re.escape(BASE_PATH) + r"/api/v1/jobs/([a-f0-9]{32})/(cancel|retry|delete)", path)
        if m:
            jid, action = m.groups()
            row = job_row(jid)
            if not row:
                return self._json({"error": "job not found"}, 404)
            if action == "cancel":
                if row["status"] == "queued":
                    update_job(jid, status="cancelled", cancel_requested=1, completed_at=utcnow(), error="Cancelled by user")
                elif row["status"] == "running":
                    update_job(jid, cancel_requested=1)
                    with RUNNING_LOCK:
                        proc = RUNNING_PROCS.get(jid)
                    if proc and proc.poll() is None:
                        try:
                            proc.terminate()
                        except Exception:
                            pass
                return self._json({"ok": True})
            if action == "retry":
                if row["status"] not in {"failed", "cancelled"}:
                    return self._json({"error": "only failed/cancelled jobs can be retried"}, 409)
                if row["output_path"]:
                    Path(row["output_path"]).unlink(missing_ok=True)
                update_job(jid, status="queued", stage="queued", worker_id=None, progress=0, error=None, cancel_requested=0, started_at=None, completed_at=None, output_path=None)
                return self._json({"ok": True})
            if action == "delete":
                if row["status"] in {"queued", "running"}:
                    return self._json({"error": "active jobs cannot be deleted"}, 409)
                shutil.rmtree(INPUT_DIR / jid, ignore_errors=True)
                shutil.rmtree(OUTPUT_DIR / jid, ignore_errors=True)
                music_path=row["music_path"]
                with db() as conn:
                    conn.execute("DELETE FROM jobs WHERE id=?", (jid,))
                    conn.commit()
                cleanup_shared_music(music_path)
                return self._json({"ok": True})
        return self._json({"error": "not found"}, 404)


def shutdown_handler(signum, frame):
    STOP_EVENT.set()
    with RUNNING_LOCK:
        procs = list(RUNNING_PROCS.values())
    for proc in procs:
        try: proc.terminate()
        except Exception: pass


def main():
    init_storage()
    signal.signal(signal.SIGTERM, shutdown_handler)
    prune_music_cache()
    for i in range(WORKER_COUNT):
        threading.Thread(target=worker_loop, args=(i,), name=f"audio-worker-{i+1}", daemon=True).start()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    httpd.daemon_threads = True
    print(f"Audio Engine {VERSION} listening on http://{HOST}:{PORT}{BASE_PATH}/ workers={WORKER_COUNT} ffmpeg_threads={FFMPEG_THREADS}", flush=True)
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        STOP_EVENT.set(); httpd.server_close()


if __name__ == "__main__":
    main()

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from media_integrity import sha256_file, score_clip

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("SML_DATA_ROOT", str(ROOT / "data"))).expanduser()
SOURCES_DIR = DATA / "sources"
CLIPS_DIR = DATA / "clips"
THUMBS_DIR = DATA / "thumbs"
METADATA_DIR = DATA / "metadata"
TEMP_DIR = DATA / "temp"
DB_PATH = DATA / "library.db"
for p in (DATA, SOURCES_DIR, CLIPS_DIR, THUMBS_DIR, METADATA_DIR, TEMP_DIR):
    p.mkdir(parents=True, exist_ok=True)

APP_VERSION = "2.0.0"
app = FastAPI(title="Self Media Library", version=APP_VERSION)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
app.mount("/media/clips", StaticFiles(directory=CLIPS_DIR), name="clips")
app.mount("/media/thumbs", StaticFiles(directory=THUMBS_DIR), name="thumbs")

DB_LOCK = threading.Lock()
PROCESS_LOCK = threading.Lock()
DRIVE_LOCK = threading.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as c:
        c.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS sources (
                id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                size_bytes INTEGER NOT NULL DEFAULT 0,
                duration REAL,
                status TEXT NOT NULL DEFAULT 'queued',
                stage TEXT NOT NULL DEFAULT 'queued',
                progress INTEGER NOT NULL DEFAULT 0,
                clip_count INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS clips (
                id TEXT PRIMARY KEY,
                source_id TEXT,
                filename TEXT NOT NULL,
                thumb_filename TEXT,
                start_time REAL NOT NULL DEFAULT 0,
                end_time REAL NOT NULL DEFAULT 0,
                duration REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'candidate',
                description TEXT NOT NULL DEFAULT '',
                quality INTEGER NOT NULL DEFAULT 0,
                ai_state TEXT NOT NULL DEFAULT 'pending',
                ai_confidence REAL,
                duplicate_state TEXT NOT NULL DEFAULT 'clear',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                deleted_at TEXT,
                FOREIGN KEY(source_id) REFERENCES sources(id)
            );
            CREATE TABLE IF NOT EXISTS clip_keywords (
                clip_id TEXT NOT NULL,
                keyword TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'ai',
                confidence REAL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(clip_id, keyword),
                FOREIGN KEY(clip_id) REFERENCES clips(id)
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        # Safe migrations for V1.1 Drive-backed library state.
        def ensure_column(table: str, column: str, definition: str) -> None:
            names = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
            if column not in names:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

        for column, definition in {
            "drive_state": "TEXT NOT NULL DEFAULT 'pending'",
            "drive_verified_at": "TEXT",
            "source_deleted_at": "TEXT",
            "source_cleanup_mode": "TEXT",
        }.items():
            ensure_column("sources", column, definition)
        for column, definition in {
            "drive_state": "TEXT NOT NULL DEFAULT 'pending'",
            "drive_clip_path": "TEXT",
            "drive_thumb_path": "TEXT",
            "drive_meta_path": "TEXT",
            "drive_verified_at": "TEXT",
            "drive_error": "TEXT",
        }.items():
            ensure_column("clips", column, definition)

        ensure_column("sources", "sha256", "TEXT")
        ensure_column("clips", "sha256", "TEXT")
        c.execute("CREATE INDEX IF NOT EXISTS idx_sources_sha256 ON sources(sha256)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_clips_sha256 ON clips(sha256)")

        defaults = {
            "clip_min_seconds": "2.0",
            "clip_max_seconds": "4.0",
            "scene_threshold": "0.32",
            "duplicate_detection": "true",
            "quality_filter": "true",
            "ai_auto": "true",
            "source_cleanup": "after_drive_verified",
            "drive_enabled": "false",
            "drive_remote": "gdrive",
            "drive_root": "Self Media Library",
            "drive_verify": "true",
            "drive_sync_metadata": "true",
            "drive_last_test_status": "never",
            "drive_last_test_at": "",
            "drive_last_test_detail": "",
        }
        for k, v in defaults.items():
            c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES (?,?)", (k, v))



def get_settings_map(conn: Optional[sqlite3.Connection] = None) -> dict[str, str]:
    if conn is not None:
        return {r["key"]: r["value"] for r in conn.execute("SELECT * FROM settings")}
    with db() as c:
        return {r["key"]: r["value"] for r in c.execute("SELECT * FROM settings")}


def setting_true(values: dict[str, str], key: str, default: bool = False) -> bool:
    raw = values.get(key)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def drive_base(values: dict[str, str]) -> str:
    remote = values.get("drive_remote", "gdrive").strip().rstrip(":")
    root = values.get("drive_root", "Self Media Library").strip().strip("/")
    if not remote:
        raise RuntimeError("Drive remote name is empty")
    return f"{remote}:{root}" if root else f"{remote}:"


def drive_path(values: dict[str, str], kind: str, filename: str) -> str:
    folders = {"clip": "clips", "thumb": "thumbnails", "meta": "metadata"}
    folder = folders[kind]
    return f"{drive_base(values).rstrip('/')}/{folder}/{filename}"


def runtime_rclone_config() -> Path:
    configured = os.getenv("RCLONE_CONFIG", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".config" / "self-media-library" / "rclone.conf"


def run_rclone(args: list[str], timeout: int = 180) -> subprocess.CompletedProcess[str]:
    if not shutil.which("rclone"):
        raise RuntimeError("rclone is not installed")
    cmd = ["rclone"]
    config = runtime_rclone_config()
    if os.getenv("RCLONE_CONFIG", "").strip():
        cmd += ["--config", str(config)]
    proc = subprocess.run([*cmd, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "rclone command failed").strip()
        raise RuntimeError(msg[-1200:])
    return proc


def drive_runtime_status(values: Optional[dict[str, str]] = None) -> dict:
    values = values or get_settings_map()
    config = runtime_rclone_config()
    status = {
        "rclone_available": shutil.which("rclone") is not None,
        "config_path": str(config),
        "config_exists": config.exists(),
        "remotes": [],
        "remote_configured": False,
        "remote": values.get("drive_remote", "gdrive").strip().rstrip(":"),
        "root": values.get("drive_root", "Self Media Library"),
        "enabled": setting_true(values, "drive_enabled"),
        "last_test_status": values.get("drive_last_test_status", "never"),
        "last_test_at": values.get("drive_last_test_at", ""),
        "last_test_detail": values.get("drive_last_test_detail", ""),
    }
    if not status["rclone_available"]:
        return status
    try:
        out = run_rclone(["listremotes"], timeout=30).stdout.splitlines()
        remotes = sorted({x.strip().rstrip(":") for x in out if x.strip()})
        status["remotes"] = remotes
        status["remote_configured"] = status["remote"] in remotes
    except Exception as exc:
        status["status_error"] = str(exc)[:400]
    return status


def remote_stat(remote_file: str) -> dict:
    proc = run_rclone(["lsjson", remote_file, "--stat", "--hash"], timeout=90)
    data = json.loads(proc.stdout or "{}")
    if not isinstance(data, dict):
        raise RuntimeError("Drive verification returned an unexpected response")
    return data


def md5_file(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def upload_and_verify(local_file: Path, remote_file: str, verify: bool = True) -> dict:
    if not local_file.exists():
        raise RuntimeError(f"Local file missing: {local_file.name}")
    run_rclone(["copyto", str(local_file), remote_file, "--retries", "3", "--low-level-retries", "6"], timeout=900)
    if not verify:
        raise RuntimeError("Drive verification cannot be bypassed for cleanup-eligible outputs")
    stat = remote_stat(remote_file)
    remote_size = int(stat.get("Size", -1))
    local_size = local_file.stat().st_size
    if remote_size != local_size:
        raise RuntimeError(f"Drive verification failed for {local_file.name}: size mismatch {local_size} != {remote_size}")
    hashes = stat.get("Hashes") or {}
    remote_md5 = hashes.get("MD5") or hashes.get("md5")
    if remote_md5 and remote_md5.lower() != md5_file(local_file).lower():
        raise RuntimeError(f"Drive verification failed for {local_file.name}: MD5 mismatch")
    return {"verified": True, "size": remote_size, "hashes": hashes}


def drive_preflight(values: Optional[dict[str, str]] = None) -> dict:
    values = values or get_settings_map()
    if not setting_true(values, "drive_enabled"):
        return {"ok": False, "enabled": False, "detail": "Drive backend is disabled"}
    base = drive_base(values).rstrip("/")
    probe = TEMP_DIR / f"drive-probe-{uuid.uuid4().hex[:8]}.json"
    probe.write_text(json.dumps({"app": "Self Media Library", "time": now_iso()}), encoding="utf-8")
    remote_probe = f"{base}/_system/preflight.json"
    try:
        run_rclone(["mkdir", f"{base}/clips"])
        run_rclone(["mkdir", f"{base}/thumbnails"])
        run_rclone(["mkdir", f"{base}/metadata"])
        run_rclone(["mkdir", f"{base}/_system"])
        upload_and_verify(probe, remote_probe, verify=True)
        run_rclone(["deletefile", remote_probe], timeout=60)
        return {"ok": True, "enabled": True, "base": base, "remote": values.get("drive_remote", "gdrive"), "root": values.get("drive_root", "Self Media Library")}
    finally:
        probe.unlink(missing_ok=True)


def clip_manifest(clip_id: str) -> dict:
    with db() as c:
        row = c.execute(
            """SELECT c.*, s.filename source_filename FROM clips c
               LEFT JOIN sources s ON s.id=c.source_id WHERE c.id=?""",
            (clip_id,),
        ).fetchone()
        if not row:
            raise RuntimeError("Clip not found")
        keywords = [dict(r) for r in c.execute(
            "SELECT keyword,source,confidence,created_at FROM clip_keywords WHERE clip_id=? ORDER BY keyword", (clip_id,)
        )]
        return {
            "schema_version": 1,
            "clip_id": row["id"],
            "source_id": row["source_id"],
            "source_filename": row["source_filename"],
            "filename": row["filename"],
            "thumbnail": row["thumb_filename"],
            "start_time": row["start_time"],
            "end_time": row["end_time"],
            "duration": row["duration"],
            "status": row["status"],
            "description": row["description"],
            "quality": row["quality"],
            "ai_state": row["ai_state"],
            "ai_confidence": row["ai_confidence"],
            "duplicate_state": row["duplicate_state"],
            "deleted_at": row["deleted_at"],
            "keywords": keywords,
            "drive": {
                "clip_path": row["drive_clip_path"],
                "thumbnail_path": row["drive_thumb_path"],
                "metadata_path": row["drive_meta_path"],
                "state": row["drive_state"],
                "verified_at": row["drive_verified_at"],
            },
            "updated_at": row["updated_at"],
        }


def write_clip_manifest(clip_id: str) -> Path:
    path = METADATA_DIR / f"{clip_id}.json"
    path.write_text(json.dumps(clip_manifest(clip_id), indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def sync_clip_to_drive(clip_id: str, upload_media: bool = True, values: Optional[dict[str, str]] = None) -> bool:
    values = values or get_settings_map()
    if not setting_true(values, "drive_enabled"):
        return False
    verify = True  # Mandatory per-app remote readback before local source cleanup.
    with DRIVE_LOCK:
        with db() as c:
            row = c.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
            if not row:
                raise RuntimeError("Clip not found")
            c.execute("UPDATE clips SET drive_state='uploading',drive_error=NULL,updated_at=? WHERE id=?", (now_iso(), clip_id))
        try:
            remote_clip = drive_path(values, "clip", row["filename"])
            remote_thumb = drive_path(values, "thumb", row["thumb_filename"]) if row["thumb_filename"] else None
            remote_meta = drive_path(values, "meta", f"{clip_id}.json")
            if upload_media:
                upload_and_verify(CLIPS_DIR / row["filename"], remote_clip, verify)
                if remote_thumb:
                    upload_and_verify(THUMBS_DIR / row["thumb_filename"], remote_thumb, verify)
            # Persist remote paths first so the JSON sidecar is self-describing.
            with db() as c:
                c.execute(
                    "UPDATE clips SET drive_clip_path=?,drive_thumb_path=?,drive_meta_path=?,updated_at=? WHERE id=?",
                    (remote_clip, remote_thumb, remote_meta, now_iso(), clip_id),
                )
            meta_path = write_clip_manifest(clip_id)
            upload_and_verify(meta_path, remote_meta, verify)
            ts = now_iso()
            with db() as c:
                c.execute("UPDATE clips SET drive_state='verified',drive_verified_at=?,drive_error=NULL,updated_at=? WHERE id=?", (ts, ts, clip_id))
            # Refresh sidecar once more so it contains the final verified state.
            meta_path = write_clip_manifest(clip_id)
            upload_and_verify(meta_path, remote_meta, verify)
            return True
        except Exception as exc:
            with db() as c:
                c.execute("UPDATE clips SET drive_state='sync_failed',drive_error=?,updated_at=? WHERE id=?", (str(exc)[:1000], now_iso(), clip_id))
            return False


def sync_metadata_batch(ids: list[str]) -> None:
    values = get_settings_map()
    if not setting_true(values, "drive_enabled") or not setting_true(values, "drive_sync_metadata", True):
        return
    for cid in ids:
        try:
            with db() as c:
                row = c.execute("SELECT drive_clip_path,drive_state FROM clips WHERE id=?", (cid,)).fetchone()
            if row and row["drive_clip_path"]:
                sync_clip_to_drive(cid, upload_media=False, values=values)
        except Exception:
            pass


def schedule_metadata_sync(ids: list[str]) -> None:
    ids = list(dict.fromkeys(ids))[:500]
    if not ids:
        return
    threading.Thread(target=sync_metadata_batch, args=(ids,), daemon=True).start()


def source_drive_gate(source_id: str) -> dict:
    """Fresh database gate used before any automatic source deletion."""
    with db() as c:
        rows = c.execute(
            """SELECT id,thumb_filename,drive_state,drive_clip_path,drive_thumb_path,
                      drive_meta_path,drive_verified_at
               FROM clips WHERE source_id=? ORDER BY created_at""",
            (source_id,),
        ).fetchall()
    total = len(rows)
    verified = 0
    failed = 0
    pending = 0
    unsafe: list[str] = []
    for row in rows:
        thumb_ok = (not row["thumb_filename"]) or bool(row["drive_thumb_path"])
        ok = (
            row["drive_state"] == "verified"
            and bool(row["drive_verified_at"])
            and bool(row["drive_clip_path"])
            and bool(row["drive_meta_path"])
            and thumb_ok
        )
        if ok:
            verified += 1
        else:
            unsafe.append(row["id"])
            if row["drive_state"] == "sync_failed":
                failed += 1
            else:
                pending += 1
    return {
        "total": total,
        "verified": verified,
        "failed": failed,
        "pending": pending,
        "all_verified": total > 0 and verified == total,
        "unsafe_ids": unsafe[:100],
    }


def cleanup_source_if_drive_safe(source_id: str, mode: str = "auto_drive_verified") -> dict:
    gate = source_drive_gate(source_id)
    if not gate["all_verified"]:
        return {"cleaned": False, "reason": "drive_gate_failed", **gate}
    with db() as c:
        src = c.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if not src:
            raise RuntimeError("Source not found")
        if src["source_deleted_at"]:
            return {"cleaned": True, "already_cleaned": True, **gate}
        source_file = SOURCES_DIR / src["stored_name"]
        bytes_removed = source_file.stat().st_size if source_file.exists() else 0
        source_file.unlink(missing_ok=True)
        ts = now_iso()
        c.execute(
            """UPDATE sources SET source_deleted_at=?,source_cleanup_mode=?,updated_at=?
               WHERE id=?""",
            (ts, mode, ts, source_id),
        )
    return {"cleaned": True, "bytes_removed": bytes_removed, **gate}


def sync_source_drive(source_id: str, values: Optional[dict[str, str]] = None) -> dict:
    values = values or get_settings_map()
    if not setting_true(values, "drive_enabled"):
        return {"ok": False, "verified": 0, "total": 0, "detail": "Drive backend disabled"}

    preflight = drive_preflight(values)
    if not preflight.get("ok"):
        return {"ok": False, "verified": 0, "total": 0, "detail": preflight.get("detail") or "Drive preflight failed"}

    with db() as c:
        clips = c.execute(
            "SELECT id,drive_state FROM clips WHERE source_id=? ORDER BY created_at",
            (source_id,),
        ).fetchall()
        src = c.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if src:
            c.execute(
                "UPDATE sources SET stage='drive_retry',drive_state='uploading',error=NULL,updated_at=? WHERE id=?",
                (now_iso(), source_id),
            )
    if not src:
        raise RuntimeError("Source not found")
    if not clips:
        return {"ok": False, "verified": 0, "total": 0, "detail": "No clips were generated; source retained"}

    retried = 0
    skipped_verified = 0
    for item in clips:
        if item["drive_state"] == "verified":
            skipped_verified += 1
            continue
        retried += 1
        sync_clip_to_drive(item["id"], upload_media=True, values=values)

    gate = source_drive_gate(source_id)
    ts = now_iso()
    with db() as c:
        c.execute(
            """UPDATE sources SET drive_state=?,drive_verified_at=?,status=?,stage=?,error=?,updated_at=?
               WHERE id=?""",
            (
                "verified" if gate["all_verified"] else "sync_failed",
                ts if gate["all_verified"] else None,
                "completed" if gate["all_verified"] else "failed",
                "drive_verified" if gate["all_verified"] else "drive_sync_failed",
                None if gate["all_verified"] else f"Drive verified {gate['verified']}/{gate['total']} clips. Source retained for safety.",
                ts,
                source_id,
            ),
        )

    cleaned = False
    bytes_removed = 0
    if gate["all_verified"] and values.get("source_cleanup", "after_drive_verified") == "after_drive_verified":
        cleanup = cleanup_source_if_drive_safe(source_id)
        cleaned = bool(cleanup.get("cleaned"))
        bytes_removed = int(cleanup.get("bytes_removed", 0) or 0)
        if cleaned:
            with db() as c:
                c.execute(
                    "UPDATE sources SET stage='completed_source_cleaned',status='completed',updated_at=? WHERE id=?",
                    (now_iso(), source_id),
                )

    return {
        "ok": gate["all_verified"],
        "verified": gate["verified"],
        "total": gate["total"],
        "failed": gate["failed"],
        "pending": gate["pending"],
        "retried": retried,
        "skipped_verified": skipped_verified,
        "source_cleaned": cleaned,
        "bytes_removed": bytes_removed,
        "detail": None if gate["all_verified"] else f"Drive verified {gate['verified']}/{gate['total']} clips",
    }

def remove_remote_file(remote_path: Optional[str]) -> None:
    if not remote_path:
        return
    run_rclone(["deletefile", remote_path], timeout=120)

def ffprobe_duration(path: Path) -> float:
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path)
    ]
    out = subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT).strip()
    return max(0.0, float(out))


def scene_boundaries(path: Path, threshold: float) -> list[float]:
    # Read scene-change timestamps from ffmpeg showinfo. If detection fails, caller falls back to intervals.
    cmd = [
        "ffmpeg", "-hide_banner", "-i", str(path),
        "-vf", f"select='gt(scene,{threshold})',showinfo",
        "-an", "-f", "null", "-"
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    times: list[float] = []
    for line in proc.stderr.splitlines():
        marker = "pts_time:"
        if marker in line:
            try:
                token = line.split(marker, 1)[1].split()[0]
                t = float(token)
                if t > 0:
                    times.append(t)
            except Exception:
                pass
    return sorted(set(times))


def build_segments(duration: float, boundaries: list[float], min_s: float, max_s: float) -> list[tuple[float, float]]:
    cuts = [0.0] + [x for x in boundaries if 0 < x < duration] + [duration]
    segments: list[tuple[float, float]] = []
    for a, b in zip(cuts, cuts[1:]):
        if b - a < min_s:
            continue
        pos = a
        while pos < b - 0.05:
            end = min(pos + max_s, b)
            if end - pos >= min_s:
                segments.append((round(pos, 3), round(end, 3)))
            pos = end
    # If scene detection was too sparse, create interval candidates.
    if not segments:
        pos = 0.0
        while pos + min_s <= duration:
            end = min(pos + max_s, duration)
            segments.append((round(pos, 3), round(end, 3)))
            pos = end
    return segments


def run_ffmpeg_clip(src: Path, out: Path, start: float, dur: float) -> None:
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{dur:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
        "-an", "-movflags", "+faststart", str(out)
    ]
    subprocess.check_call(cmd)


def run_ffmpeg_thumb(src: Path, out: Path, at: float) -> None:
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{at:.3f}", "-i", str(src), "-frames:v", "1",
        "-vf", "scale='min(640,iw)':-2", "-q:v", "3", str(out)
    ]
    subprocess.check_call(cmd)


def generic_ai_keywords(filename: str) -> list[str]:
    # Safe fallback only when no external vision analyzer is configured.
    stem = Path(filename).stem.replace("_", " ").replace("-", " ")
    words = [w.lower() for w in stem.split() if len(w) >= 3 and w.isalnum()]
    seen = []
    for w in words:
        if w not in seen:
            seen.append(w)
    return seen[:6]


def analyze_clip(clip_path: Path, source_filename: str) -> tuple[str, list[tuple[str, float]], float]:
    """Adapter point for a real vision AI service.

    If AI_ANALYZER_URL is set, POST JSON with clip_path and expect:
    {"description":"...", "keywords":[{"keyword":"mountain","confidence":0.95}], "confidence":0.9}
    The current V1 intentionally keeps provider secrets server-side.
    """
    url = os.getenv("AI_ANALYZER_URL", "").strip()
    if url:
        try:
            import urllib.request
            payload = json.dumps({"clip_path": str(clip_path), "source_filename": source_filename}).encode()
            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=90) as r:
                data = json.loads(r.read().decode())
            kws = []
            for item in data.get("keywords", []):
                if isinstance(item, str):
                    kws.append((item.strip().lower(), 0.8))
                elif isinstance(item, dict) and item.get("keyword"):
                    kws.append((str(item["keyword"]).strip().lower(), float(item.get("confidence", 0.8))))
            return str(data.get("description", "")), kws[:20], float(data.get("confidence", 0.8))
        except Exception:
            pass
    kws = [(x, 0.35) for x in generic_ai_keywords(source_filename)]
    description = "Automatic vision analysis pending provider configuration." if not kws else "Initial automatic metadata generated from source context; vision AI can refine it."
    return description, kws, 0.35 if kws else 0.0


def process_source(source_id: str) -> None:
    if not PROCESS_LOCK.acquire(blocking=False):
        return
    try:
        with db() as c:
            src = c.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
            if not src:
                return
            path = SOURCES_DIR / src["stored_name"]
            settings = get_settings_map(c)
            c.execute("UPDATE sources SET status='processing',stage='drive_preflight',progress=2,error=NULL,updated_at=? WHERE id=?", (now_iso(), source_id))
        if not path.exists():
            raise RuntimeError("Source file is no longer stored locally")
        if setting_true(settings, "drive_enabled"):
            preflight = drive_preflight(settings)
            if not preflight.get("ok"):
                raise RuntimeError(preflight.get("detail") or "Drive preflight failed")
        with db() as c:
            c.execute("UPDATE sources SET stage='probing',progress=5,updated_at=? WHERE id=?", (now_iso(), source_id))
        duration = ffprobe_duration(path)
        with db() as c:
            c.execute("UPDATE sources SET duration=?,stage='detecting_shots',progress=10,updated_at=? WHERE id=?", (duration, now_iso(), source_id))
        min_s = float(settings.get("clip_min_seconds", "2.0"))
        max_s = float(settings.get("clip_max_seconds", "4.0"))
        threshold = float(settings.get("scene_threshold", "0.32"))
        bounds = scene_boundaries(path, threshold)
        segments = build_segments(duration, bounds, min_s, max_s)
        with db() as c:
            c.execute("UPDATE sources SET stage='creating_clips',progress=15,updated_at=? WHERE id=?", (now_iso(), source_id))
        total = max(1, len(segments))
        created_ids: list[str] = []
        for i, (start_t, end_t) in enumerate(segments):
            cid = f"CLP-{uuid.uuid4().hex[:10].upper()}"
            clip_name = f"{cid}.mp4"
            thumb_name = f"{cid}.jpg"
            clip_path = CLIPS_DIR / clip_name
            thumb_path = THUMBS_DIR / thumb_name
            try:
                run_ffmpeg_clip(path, clip_path, start_t, end_t - start_t)
                run_ffmpeg_thumb(path, thumb_path, start_t + max(0.1, (end_t - start_t) / 2))
                duration_seconds = end_t - start_t
                q, blank = (score_clip(clip_path, duration_seconds)
                            if setting_true(settings, "quality_filter", True)
                            else (75, False))
                fingerprint = sha256_file(clip_path)
                if setting_true(settings, "ai_auto", True):
                    description, kws, conf = analyze_clip(clip_path, src["filename"])
                    ai_state = "complete" if kws else "pending"
                else:
                    description, kws, conf = "", [], 0.0
                    ai_state = "disabled"
                ts = now_iso()
                with db() as c:
                    # Single-worker processing serializes clip fingerprint checks and inserts.
                    match = (c.execute("SELECT id FROM clips WHERE sha256=? AND deleted_at IS NULL LIMIT 1",
                                       (fingerprint,)).fetchone()
                             if setting_true(settings, "duplicate_detection", True) else None)
                    duplicate_state = "duplicate" if match else "clear"
                    status = "rejected" if blank else "candidate"
                    if blank:
                        description = ("Auto review: repeated near-blank sample frames. "
                                       + description)[:1000]
                    c.execute(
                        """INSERT INTO clips(id,source_id,filename,thumb_filename,start_time,end_time,duration,status,description,quality,ai_state,ai_confidence,created_at,updated_at,drive_state,sha256,duplicate_state)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (cid, source_id, clip_name, thumb_name, start_t, end_t,
                         duration_seconds, status, description, q, ai_state, conf,
                         ts, ts, "pending", fingerprint, duplicate_state),
                    )
                    for kw, kconf in kws:
                        if kw:
                            c.execute("INSERT OR IGNORE INTO clip_keywords(clip_id,keyword,source,confidence,created_at) VALUES (?,?,?,?,?)", (cid, kw, "ai", kconf, ts))
                created_ids.append(cid)
            except Exception:
                clip_path.unlink(missing_ok=True)
                thumb_path.unlink(missing_ok=True)
            progress = 15 + int(60 * ((i + 1) / total))
            with db() as c:
                c.execute("UPDATE sources SET stage='ai_tagging',progress=?,clip_count=?,updated_at=? WHERE id=?", (progress, len(created_ids), now_iso(), source_id))
        if not created_ids:
            with db() as c:
                c.execute("UPDATE sources SET status='completed',stage='completed_no_clips',progress=100,clip_count=0,error='No usable clips were generated; source retained',updated_at=? WHERE id=?", (now_iso(), source_id))
            return
        if setting_true(settings, "drive_enabled"):
            with db() as c:
                c.execute("UPDATE sources SET stage='drive_upload',progress=78,drive_state='uploading',updated_at=? WHERE id=?", (now_iso(), source_id))
            verified = 0
            for idx, cid in enumerate(created_ids):
                if sync_clip_to_drive(cid, upload_media=True, values=settings):
                    verified += 1
                with db() as c:
                    progress = 78 + int(20 * ((idx + 1) / len(created_ids)))
                    c.execute("UPDATE sources SET progress=?,updated_at=? WHERE id=?", (progress, now_iso(), source_id))
            gate = source_drive_gate(source_id)
            if gate["all_verified"]:
                ts = now_iso()
                cleaned = False
                if settings.get("source_cleanup", "after_drive_verified") == "after_drive_verified":
                    cleanup = cleanup_source_if_drive_safe(source_id)
                    cleaned = bool(cleanup.get("cleaned"))
                with db() as c:
                    c.execute(
                        """UPDATE sources SET status='completed',stage=?,progress=100,clip_count=?,drive_state='verified',
                           drive_verified_at=?,error=NULL,updated_at=? WHERE id=?""",
                        ("completed_source_cleaned" if cleaned else "completed", len(created_ids), ts, ts, source_id),
                    )
            else:
                with db() as c:
                    c.execute(
                        """UPDATE sources SET status='failed',stage='drive_sync_failed',progress=100,clip_count=?,
                           drive_state='sync_failed',error=?,updated_at=? WHERE id=?""",
                        (len(created_ids), f"Drive verified {gate['verified']}/{gate['total']} clips. Source retained for safety.", now_iso(), source_id),
                    )
        else:
            with db() as c:
                c.execute("UPDATE sources SET status='completed',stage='completed_local_only',progress=100,clip_count=?,drive_state='disabled',updated_at=? WHERE id=?", (len(created_ids), now_iso(), source_id))
    except Exception as e:
        with db() as c:
            c.execute("UPDATE sources SET status='failed',stage='failed',error=?,updated_at=? WHERE id=?", (str(e)[:1000], now_iso(), source_id))
    finally:
        PROCESS_LOCK.release()


class KeywordUpdate(BaseModel):
    keywords: list[str]


class BulkKeywords(BaseModel):
    ids: list[str]
    keyword: str
    action: str = "add"


class BulkStatus(BaseModel):
    ids: list[str]
    status: str


class SettingsUpdate(BaseModel):
    values: dict[str, str]


def clip_to_dict(c: sqlite3.Row, conn: sqlite3.Connection) -> dict:
    kws = [dict(r) for r in conn.execute("SELECT keyword,source,confidence FROM clip_keywords WHERE clip_id=? ORDER BY keyword", (c["id"],))]
    d = dict(c)
    d["keywords"] = kws
    d["clip_url"] = f"/media/clips/{c['filename']}"
    d["thumb_url"] = f"/media/thumbs/{c['thumb_filename']}" if c["thumb_filename"] else None
    if c["source_id"]:
        s = conn.execute("SELECT filename FROM sources WHERE id=?", (c["source_id"],)).fetchone()
        d["source_filename"] = s["filename"] if s else "Unknown source"
    else:
        d["source_filename"] = "Manual library item"
    return d


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/health")
def health():
    values = get_settings_map()
    return {"ok": True, "version": APP_VERSION, "ffmpeg": shutil.which("ffmpeg") is not None, "rclone": shutil.which("rclone") is not None, "drive_enabled": setting_true(values, "drive_enabled"), "ai_configured": bool(os.getenv("AI_ANALYZER_URL"))}


@app.get("/api/stats")
def stats():
    with db() as c:
        total = c.execute("SELECT COUNT(*) n FROM clips WHERE deleted_at IS NULL").fetchone()["n"]
        approved = c.execute("SELECT COUNT(*) n FROM clips WHERE deleted_at IS NULL AND status='approved'").fetchone()["n"]
        candidate = c.execute("SELECT COUNT(*) n FROM clips WHERE deleted_at IS NULL AND status='candidate'").fetchone()["n"]
        trash = c.execute("SELECT COUNT(*) n FROM clips WHERE deleted_at IS NOT NULL").fetchone()["n"]
        sources = c.execute("SELECT COUNT(*) n FROM sources").fetchone()["n"]
        processing = c.execute("SELECT COUNT(*) n FROM sources WHERE status='processing'").fetchone()["n"]
        drive_verified = c.execute("SELECT COUNT(*) n FROM clips WHERE deleted_at IS NULL AND drive_state='verified'").fetchone()["n"]
        source_cleaned = c.execute("SELECT COUNT(*) n FROM sources WHERE source_deleted_at IS NOT NULL").fetchone()["n"]
    return {"clips": total, "approved": approved, "candidate": candidate, "trash": trash, "sources": sources, "processing": processing, "drive_verified": drive_verified, "source_cleaned": source_cleaned}


@app.get("/api/clips")
def clips(
    q: str = "",
    status: str = "all",
    source_id: str = "",
    trash: bool = False,
    sort: str = "newest",
    limit: int = Query(48, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    clauses = ["c.deleted_at IS NOT NULL" if trash else "c.deleted_at IS NULL"]
    params: list[object] = []
    if status != "all":
        clauses.append("c.status=?")
        params.append(status)
    if source_id:
        clauses.append("c.source_id=?")
        params.append(source_id)
    if q.strip():
        terms = [t.strip().lower() for t in q.split() if t.strip()]
        for term in terms:
            clauses.append("(LOWER(c.description) LIKE ? OR EXISTS (SELECT 1 FROM clip_keywords k WHERE k.clip_id=c.id AND LOWER(k.keyword) LIKE ?))")
            like = f"%{term}%"
            params += [like, like]
    order_map = {
        "newest": "c.created_at DESC",
        "oldest": "c.created_at ASC",
        "quality": "c.quality DESC, c.created_at DESC",
        "duration": "c.duration DESC, c.created_at DESC",
    }
    order = order_map.get(sort, order_map["newest"])
    where = " AND ".join(clauses)
    with db() as c:
        count = c.execute(f"SELECT COUNT(*) n FROM clips c WHERE {where}", params).fetchone()["n"]
        rows = c.execute(f"SELECT c.* FROM clips c WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?", params + [limit, offset]).fetchall()
        items = [clip_to_dict(r, c) for r in rows]
    return {"items": items, "total": count, "limit": limit, "offset": offset}


@app.patch("/api/clips/{clip_id}/keywords")
def update_keywords(clip_id: str, body: KeywordUpdate):
    cleaned = []
    for k in body.keywords:
        k = " ".join(k.strip().lower().split())[:80]
        if k and k not in cleaned:
            cleaned.append(k)
    with db() as c:
        if not c.execute("SELECT 1 FROM clips WHERE id=?", (clip_id,)).fetchone():
            raise HTTPException(404, "Clip not found")
        c.execute("DELETE FROM clip_keywords WHERE clip_id=?", (clip_id,))
        ts = now_iso()
        for k in cleaned[:60]:
            c.execute("INSERT INTO clip_keywords(clip_id,keyword,source,confidence,created_at) VALUES (?,?,?,?,?)", (clip_id, k, "manual", 1.0, ts))
        c.execute("UPDATE clips SET updated_at=? WHERE id=?", (ts, clip_id))
    schedule_metadata_sync([clip_id])
    return {"ok": True, "keywords": cleaned}


@app.post("/api/clips/bulk/keywords")
def bulk_keywords(body: BulkKeywords):
    kw = " ".join(body.keyword.strip().lower().split())[:80]
    if not kw or body.action not in {"add", "remove"}:
        raise HTTPException(400, "Invalid keyword operation")
    ts = now_iso()
    with db() as c:
        for cid in body.ids[:500]:
            if body.action == "add":
                c.execute("INSERT OR IGNORE INTO clip_keywords(clip_id,keyword,source,confidence,created_at) VALUES (?,?,?,?,?)", (cid, kw, "manual", 1.0, ts))
            else:
                c.execute("DELETE FROM clip_keywords WHERE clip_id=? AND keyword=?", (cid, kw))
            c.execute("UPDATE clips SET updated_at=? WHERE id=?", (ts, cid))
    schedule_metadata_sync(body.ids)
    return {"ok": True, "count": len(body.ids)}


@app.post("/api/clips/bulk/status")
def bulk_status(body: BulkStatus):
    if body.status not in {"candidate", "approved", "rejected"}:
        raise HTTPException(400, "Invalid status")
    ts = now_iso()
    with db() as c:
        for cid in body.ids[:500]:
            c.execute("UPDATE clips SET status=?,updated_at=? WHERE id=? AND deleted_at IS NULL", (body.status, ts, cid))
    schedule_metadata_sync(body.ids)
    return {"ok": True}


@app.delete("/api/clips/{clip_id}")
def trash_clip(clip_id: str):
    with db() as c:
        c.execute("UPDATE clips SET deleted_at=?,updated_at=? WHERE id=?", (now_iso(), now_iso(), clip_id))
    schedule_metadata_sync([clip_id])
    return {"ok": True}


@app.post("/api/clips/{clip_id}/restore")
def restore_clip(clip_id: str):
    with db() as c:
        c.execute("UPDATE clips SET deleted_at=NULL,updated_at=? WHERE id=?", (now_iso(), clip_id))
    schedule_metadata_sync([clip_id])
    return {"ok": True}


@app.delete("/api/clips/{clip_id}/permanent")
def permanent_delete(clip_id: str):
    with db() as c:
        row = c.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Clip not found")
    # If this clip exists on Drive, permanent deletion must remove remote assets first.
    if row["drive_clip_path"] or row["drive_thumb_path"] or row["drive_meta_path"]:
        try:
            with DRIVE_LOCK:
                remove_remote_file(row["drive_clip_path"])
                remove_remote_file(row["drive_thumb_path"])
                remove_remote_file(row["drive_meta_path"])
        except Exception as exc:
            raise HTTPException(502, f"Drive delete failed; local record kept for safety: {exc}")
    (CLIPS_DIR / row["filename"]).unlink(missing_ok=True)
    if row["thumb_filename"]:
        (THUMBS_DIR / row["thumb_filename"]).unlink(missing_ok=True)
    (METADATA_DIR / f"{clip_id}.json").unlink(missing_ok=True)
    with db() as c:
        c.execute("DELETE FROM clip_keywords WHERE clip_id=?", (clip_id,))
        c.execute("DELETE FROM clips WHERE id=?", (clip_id,))
    return {"ok": True}


@app.post("/api/clips/{clip_id}/reanalyze")
def reanalyze(clip_id: str):
    with db() as c:
        row = c.execute("SELECT c.*, s.filename source_filename FROM clips c LEFT JOIN sources s ON s.id=c.source_id WHERE c.id=?", (clip_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Clip not found")
        clip_path = CLIPS_DIR / row["filename"]
        desc, kws, conf = analyze_clip(clip_path, row["source_filename"] or row["filename"])
        ts = now_iso()
        c.execute("UPDATE clips SET description=?,ai_state=?,ai_confidence=?,updated_at=? WHERE id=?", (desc, "complete" if kws else "pending", conf, ts, clip_id))
        # Preserve manual tags; replace only AI tags.
        c.execute("DELETE FROM clip_keywords WHERE clip_id=? AND source='ai'", (clip_id,))
        for kw, kconf in kws:
            c.execute("INSERT OR IGNORE INTO clip_keywords(clip_id,keyword,source,confidence,created_at) VALUES (?,?,?,?,?)", (clip_id, kw, "ai", kconf, ts))
    schedule_metadata_sync([clip_id])
    return {"ok": True}


@app.get("/api/sources")
def get_sources():
    with db() as c:
        rows = []
        for raw in c.execute("SELECT * FROM sources ORDER BY created_at DESC"):
            item = dict(raw)
            counts = c.execute(
                """SELECT COUNT(*) total,
                          SUM(CASE WHEN drive_state='verified' THEN 1 ELSE 0 END) verified,
                          SUM(CASE WHEN drive_state='sync_failed' THEN 1 ELSE 0 END) failed
                   FROM clips WHERE source_id=?""",
                (raw["id"],),
            ).fetchone()
            total = int(counts["total"] or 0)
            verified = int(counts["verified"] or 0)
            failed = int(counts["failed"] or 0)
            item["drive_clip_total"] = total
            item["drive_clip_verified"] = verified
            item["drive_clip_failed"] = failed
            item["drive_clip_pending"] = max(0, total - verified - failed)
            rows.append(item)
    return {"items": rows}


@app.post("/api/sources/upload")
async def upload_source(file: UploadFile = File(...)):
    ext = Path(file.filename or "video.mp4").suffix.lower()
    allowed = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}
    if ext not in allowed:
        raise HTTPException(400, "Unsupported video format")
    sid = f"SRC-{uuid.uuid4().hex[:10].upper()}"
    stored = f"{sid}{ext}"
    dest = SOURCES_DIR / stored
    size = 0
    digest = hashlib.sha256()
    try:
        with dest.open("wb") as output:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > 2 * 1024 * 1024 * 1024:
                    raise HTTPException(413, "2 GB video import limit")
                digest.update(chunk)
                output.write(chunk)
        if size == 0:
            raise HTTPException(400, "Empty video")
        ts = now_iso()
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            previous = c.execute(
                "SELECT id,stored_name FROM sources WHERE sha256=? ORDER BY created_at DESC LIMIT 1",
                (digest.hexdigest(),),
            ).fetchone()
            if previous and (SOURCES_DIR / previous["stored_name"]).is_file():
                dest.unlink(missing_ok=True)
                return {"ok": True, "id": previous["id"], "duplicate": True}
            c.execute(
                """INSERT INTO sources(id,filename,stored_name,size_bytes,status,stage,
                         progress,created_at,updated_at,sha256)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (sid, Path(file.filename or stored).name, stored, size,
                 "queued", "queued", 0, ts, ts, digest.hexdigest()),
            )
        return {"ok": True, "id": sid, "duplicate": False}
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    finally:
        await file.close()


@app.post("/api/sources/{source_id}/process")
def start_processing(source_id: str):
    with db() as c:
        row = c.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Source not found")
        if row["status"] == "processing":
            return {"ok": True, "status": "processing"}
        if row["source_deleted_at"]:
            raise HTTPException(409, "Source was safely cleaned after Drive verification and cannot be reprocessed")
        existing = c.execute("SELECT COUNT(*) n FROM clips WHERE source_id=?", (source_id,)).fetchone()["n"]
        if existing:
            raise HTTPException(409, "This source already has generated clips. Use Drive Retry instead of creating duplicates.")
        values = get_settings_map(c)
    if setting_true(values, "drive_enabled"):
        try:
            result = drive_preflight(values)
            if not result.get("ok"):
                raise RuntimeError(result.get("detail") or "Drive unavailable")
        except Exception as exc:
            raise HTTPException(503, f"Drive preflight failed; processing not started: {exc}")
    with db() as c:
        c.execute("UPDATE sources SET status='queued',stage='queued',progress=0,error=NULL,updated_at=? WHERE id=?", (now_iso(), source_id))
    thread = threading.Thread(target=process_source, args=(source_id,), daemon=True)
    thread.start()
    return {"ok": True, "status": "queued"}


@app.post("/api/sources/{source_id}/manual-cleanup")
def manual_cleanup_source(source_id: str):
    """Manually remove only the local original source after a failed job.

    Generated clips, thumbnails, metadata records, and any Drive objects are intentionally
    left untouched so Drive sync can still be retried from the generated clip files.
    """
    with db() as c:
        src = c.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if not src:
            raise HTTPException(404, "Source not found")
        if src["status"] == "processing":
            raise HTTPException(409, "Cannot clean a source while it is processing")
        failed_state = (
            src["status"] == "failed"
            or src["drive_state"] == "sync_failed"
            or str(src["stage"] or "").endswith("failed")
            or "failed" in str(src["stage"] or "")
        )
        if not failed_state:
            raise HTTPException(409, "Manual cleanup is available only for failed jobs")
        if src["source_deleted_at"]:
            return {"ok": True, "already_cleaned": True, "bytes_removed": 0}
        source_file = SOURCES_DIR / src["stored_name"]
        bytes_removed = source_file.stat().st_size if source_file.exists() else 0
        source_file.unlink(missing_ok=True)
        ts = now_iso()
        c.execute(
            "UPDATE sources SET source_deleted_at=?,source_cleanup_mode='manual_failed',updated_at=? WHERE id=?",
            (ts, ts, source_id),
        )
    return {
        "ok": True,
        "bytes_removed": bytes_removed,
        "detail": "Local source removed. Generated clips and Drive assets were not deleted.",
    }


@app.delete("/api/sources/{source_id}")
def delete_source(source_id: str):
    with db() as c:
        src = c.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if not src:
            raise HTTPException(404, "Source not found")
        clip_count = c.execute("SELECT COUNT(*) n FROM clips WHERE source_id=?", (source_id,)).fetchone()["n"]
        if clip_count:
            raise HTTPException(409, "Source has generated clips. Keep the source record to preserve clip provenance.")
        (SOURCES_DIR / src["stored_name"]).unlink(missing_ok=True)
        c.execute("DELETE FROM sources WHERE id=?", (source_id,))
    return {"ok": True}


@app.get("/api/settings")
def get_settings():
    with db() as c:
        values = {r["key"]: r["value"] for r in c.execute("SELECT * FROM settings")}
    values["ai_configured"] = "true" if os.getenv("AI_ANALYZER_URL") else "false"
    runtime = drive_runtime_status(values)
    values["rclone_available"] = "true" if runtime["rclone_available"] else "false"
    values["rclone_config_path"] = runtime["config_path"]
    values["rclone_config_exists"] = "true" if runtime["config_exists"] else "false"
    values["drive_remote_configured"] = "true" if runtime["remote_configured"] else "false"
    values["drive_remotes"] = ",".join(runtime["remotes"])
    return {"values": values, "drive_runtime": runtime}


@app.patch("/api/settings")
def update_settings(body: SettingsUpdate):
    allowed = {"clip_min_seconds", "clip_max_seconds", "scene_threshold", "duplicate_detection", "quality_filter", "ai_auto", "source_cleanup", "drive_enabled", "drive_remote", "drive_root", "drive_verify", "drive_sync_metadata"}
    with db() as c:
        for k, v in body.values.items():
            if k in allowed:
                c.execute("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))
    return {"ok": True}

@app.get("/api/drive/status")
def drive_status():
    return drive_runtime_status()


@app.post("/api/drive/test")
def test_drive():
    values = get_settings_map()
    if not setting_true(values, "drive_enabled"):
        raise HTTPException(400, "Enable Drive-backed library and save settings before testing")
    ts = now_iso()
    try:
        runtime = drive_runtime_status(values)
        if not runtime["rclone_available"]:
            raise RuntimeError("rclone is not installed")
        if not runtime["config_exists"]:
            raise RuntimeError(f"Runtime rclone config is missing: {runtime['config_path']}")
        if not runtime["remote_configured"]:
            raise RuntimeError(f"rclone remote '{runtime['remote']}' is not configured in the runtime config")
        result = drive_preflight(values)
        if not result.get("ok"):
            raise RuntimeError(result.get("detail") or "Drive test failed")
        with db() as c:
            c.execute("INSERT INTO settings(key,value) VALUES('drive_last_test_status','pass') ON CONFLICT(key) DO UPDATE SET value='pass'")
            c.execute("INSERT INTO settings(key,value) VALUES('drive_last_test_at',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (ts,))
            c.execute("INSERT INTO settings(key,value) VALUES('drive_last_test_detail',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (f"{result.get('base','')} verified",))
        return {**result, "runtime": runtime, "tested_at": ts}
    except Exception as exc:
        detail = str(exc)[:1000]
        with db() as c:
            c.execute("INSERT INTO settings(key,value) VALUES('drive_last_test_status','fail') ON CONFLICT(key) DO UPDATE SET value='fail'")
            c.execute("INSERT INTO settings(key,value) VALUES('drive_last_test_at',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (ts,))
            c.execute("INSERT INTO settings(key,value) VALUES('drive_last_test_detail',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (detail,))
        raise HTTPException(503, detail)


@app.post("/api/clips/{clip_id}/drive-sync")
def retry_clip_drive_sync(clip_id: str):
    with db() as c:
        if not c.execute("SELECT 1 FROM clips WHERE id=?", (clip_id,)).fetchone():
            raise HTTPException(404, "Clip not found")
    ok = sync_clip_to_drive(clip_id, upload_media=True)
    if not ok:
        with db() as c:
            row = c.execute("SELECT drive_error FROM clips WHERE id=?", (clip_id,)).fetchone()
        raise HTTPException(502, row["drive_error"] if row else "Drive sync failed")
    return {"ok": True}


@app.post("/api/sources/{source_id}/drive-sync")
def retry_source_drive_sync(source_id: str):
    try:
        result = sync_source_drive(source_id)
    except Exception as exc:
        raise HTTPException(502, str(exc))
    if not result.get("ok"):
        raise HTTPException(502, result.get("detail") or f"Drive verified {result.get('verified', 0)}/{result.get('total', 0)} clips")
    return result



# Additive V2 media, image ingestion, and editor API. V1 records/routes remain intact.
from media_extensions import register_media_extensions
register_media_extensions(app, data_dir=DATA, db=db, now_iso=now_iso,
                          get_settings_map=get_settings_map, setting_true=setting_true,
                          drive_base=drive_base, drive_preflight=drive_preflight,
                          run_rclone=run_rclone, upload_and_verify=upload_and_verify,
                          source_dir=SOURCES_DIR)

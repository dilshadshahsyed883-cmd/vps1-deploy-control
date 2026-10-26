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
from media_catalog import probe_video

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
            "drive_source_path": "TEXT",
            "drive_source_verified_at": "TEXT",
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
        for col, definition in {
            "codec": "TEXT NOT NULL DEFAULT ''",
            "width": "INTEGER NOT NULL DEFAULT 0",
            "height": "INTEGER NOT NULL DEFAULT 0",
            "fps": "REAL NOT NULL DEFAULT 0",
            "orientation": "TEXT NOT NULL DEFAULT 'unknown'",
            "has_audio": "INTEGER NOT NULL DEFAULT 0",
            "source_kind": "TEXT NOT NULL DEFAULT 'legacy'",
            "source_locator": "TEXT NOT NULL DEFAULT ''",
            "rights_confirmed": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            ensure_column("sources", col, definition)
        c.execute("CREATE INDEX IF NOT EXISTS idx_sources_orientation ON sources(orientation)")

        defaults = {
            "clip_min_seconds": "2.0",
            "clip_max_seconds": "4.0",
            "scene_threshold": "0.32",
            "duplicate_detection": "true",
            "quality_filter": "true",
            "ai_auto": "true",
            "source_cleanup": "after_local_selection",
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


def schedule_metadata_sync(ids):
    # Metadata is already durable in the local catalog; no remote queue.
    return None


def ffprobe_duration(path: Path) -> float:
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path)
    ]
    out = subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT).strip()
    return max(0.0, float(out))


def scene_boundaries(path: Path, threshold: float) -> list[float]:
    """Adaptive advisory cut candidates from actual low-resolution scene scores."""
    import re
    import statistics
    try:
        proc = subprocess.run([
            "ffmpeg", "-hide_banner", "-threads", "2", "-i", str(path),
            "-vf", "fps=5,scale=320:-2,select='gt(scene,0.05)',metadata=print",
            "-an", "-f", "null", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError):
        return []
    samples=[];at=None
    for line in proc.stderr.splitlines():
        timestamp=re.search(r"pts_time:([0-9.]+)",line)
        if timestamp:at=float(timestamp.group(1))
        score=re.search(r"lavfi.scene_score=([0-9.]+)",line)
        if score and at is not None and at>0:samples.append((at,float(score.group(1))))
    if not samples:return []
    adaptive=max(.05,min(float(threshold),statistics.median(v for _,v in samples)*1.2))
    return sorted({round(at,3) for at,value in samples if value>=adaptive})


def build_segments(duration: float, boundaries: list[float], min_s: float, max_s: float) -> list[tuple[float, float]]:
    if not 0.5 <= min_s <= max_s <= 30:
        raise ValueError("Clip duration profile must satisfy 0.5 <= min <= max <= 30")
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
    # Actual inference belongs to the durable isolated AI worker. Filenames are
    # source context, never evidence of visual recognition.
    return "", [], 0.0


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
            c.execute("UPDATE sources SET status='processing',stage='probing',progress=2,error=NULL,updated_at=? WHERE id=?", (now_iso(), source_id))
        if not path.exists():
            raise RuntimeError("Source file is no longer stored locally")
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
        failed_segments = 0
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
                    ai_state = "pending"
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
                failed_segments += 1
                clip_path.unlink(missing_ok=True)
                thumb_path.unlink(missing_ok=True)
            progress = 15 + int(60 * ((i + 1) / total))
            with db() as c:
                c.execute("UPDATE sources SET stage='ai_tagging',progress=?,clip_count=?,updated_at=? WHERE id=?", (progress, len(created_ids), now_iso(), source_id))
        if not created_ids:
            with db() as c:
                c.execute("UPDATE sources SET status='completed',stage='completed_no_clips',progress=100,clip_count=0,error='No usable clips were generated; source retained',updated_at=? WHERE id=?", (now_iso(), source_id))
            return
        if failed_segments:
            raise RuntimeError(f"{failed_segments} clip segments failed; all source media retained")
        from local_library import create_review
        create_review(db, now_iso, source_id, created_ids)
        with db() as c:
            c.execute("UPDATE sources SET status='completed',stage='awaiting_pair_selection',progress=100,clip_count=?,drive_state='disabled',updated_at=? WHERE id=?", (len(created_ids), now_iso(), source_id))
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


class ClipSelection(BaseModel):
    ids: list[str]


class SettingsUpdate(BaseModel):
    values: dict[str, str]


def clip_to_dict(c: sqlite3.Row, conn: sqlite3.Connection) -> dict:
    kws = [dict(r) for r in conn.execute("SELECT keyword,source,confidence FROM clip_keywords WHERE clip_id=? ORDER BY keyword", (c["id"],))]
    d = dict(c)
    d["keywords"] = kws
    d["clip_url"] = f"/media/clips/{c['filename']}"
    d["thumb_url"] = f"/media/thumbs/{c['thumb_filename']}" if c["thumb_filename"] else None
    if c["source_id"]:
        s = conn.execute("SELECT filename,orientation,width,height,fps,codec,source_kind FROM sources WHERE id=?", (c["source_id"],)).fetchone()
        d["source_filename"] = s["filename"] if s else "Unknown source"
        d["source_orientation"] = s["orientation"] if s else "unknown"
        d["source_width"] = s["width"] if s else 0
        d["source_height"] = s["height"] if s else 0
        d["source_fps"] = s["fps"] if s else 0
        d["source_codec"] = s["codec"] if s else ""
        d["source_kind"] = s["source_kind"] if s else "legacy"
    else:
        d["source_filename"] = "Manual library item"
    return d


@app.on_event("startup")
def startup() -> None:
    init_db()
    from local_library import initialize
    with db() as c: initialize(c)


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/health")
def health():
    values = get_settings_map()
    return {"ok": True, "version": APP_VERSION, "ffmpeg": shutil.which("ffmpeg") is not None, "rclone": shutil.which("rclone") is not None, "drive_enabled": False, "storage_mode": "vps_only", "ai_configured": model_ready(DATA / "ai_models")}


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
    orientation: str = "all",
    trash: bool = False,
    date_from: str = "",
    date_to: str = "",
    sort: str = "newest",
    limit: int = Query(48, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    clauses = ["c.deleted_at IS NOT NULL" if trash else "c.deleted_at IS NULL"]
    params: list[object] = []
    if date_from:
        clauses.append("substr(c.created_at,1,10)>=?"); params.append(date_from)
    if date_to:
        clauses.append("substr(c.created_at,1,10)<=?"); params.append(date_to)
    if status != "all":
        clauses.append("c.status=?")
        params.append(status)
    if source_id:
        clauses.append("c.source_id=?")
        params.append(source_id)
    if orientation not in {"all", "landscape", "portrait", "square", "unknown"}:
        raise HTTPException(400, "Invalid orientation filter")
    if orientation != "all":
        clauses.append("EXISTS (SELECT 1 FROM sources s WHERE s.id=c.source_id AND s.orientation=?)")
        params.append(orientation)
    if q.strip():
        terms = [t.strip().lower() for t in q.split() if t.strip()]
        for term in terms:
            clauses.append("""(LOWER(c.filename) LIKE ?
                OR EXISTS (SELECT 1 FROM sources s WHERE s.id=c.source_id AND LOWER(s.filename) LIKE ?)
                OR LOWER(c.description) LIKE ?
                OR EXISTS (SELECT 1 FROM clip_keywords k WHERE k.clip_id=c.id AND LOWER(k.keyword) LIKE ?)
                OR EXISTS (SELECT 1 FROM sml_person_assets a JOIN sml_person_collections p ON p.id=a.person_id
                           WHERE a.media_type='video' AND a.asset_id=c.id
                           AND (LOWER(p.name) LIKE ? OR LOWER(p.aliases_json) LIKE ?
                                OR LOWER(a.action_tags_json) LIKE ?)))""")
            like = f"%{term}%"
            params += [like] * 7
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
    from ai_workspace import manual_patch
    manual_patch(DATA, "video", clip_id, {"keywords": cleaned})
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
    # Review may proceed independently of unrelated library imports/AI jobs.
    # Do not bypass pair-review, active-source or editor-reservation protection:
    # report blocked IDs, never silently approve the whole selection.
    ids = list(dict.fromkeys(body.ids))
    if not 1 <= len(ids) <= 500:
        raise HTTPException(400, "Select between 1 and 500 clips")
    ts = now_iso()
    updated, skipped = [], []
    from local_library import guard_manual, protect
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        for cid in ids:
            c.execute("SAVEPOINT selected_clip_review")
            try:
                if not c.execute("SELECT 1 FROM clips WHERE id=? AND deleted_at IS NULL", (cid,)).fetchone():
                    raise HTTPException(404, "Clip not found or already trashed")
                guard_manual(c, "video", [cid])
                protect(c, "video", [cid])
                c.execute("UPDATE clips SET status=?,updated_at=? WHERE id=?",
                          (body.status, ts, cid))
                c.execute("RELEASE SAVEPOINT selected_clip_review")
                updated.append(cid)
            except HTTPException as exc:
                c.execute("ROLLBACK TO SAVEPOINT selected_clip_review")
                c.execute("RELEASE SAVEPOINT selected_clip_review")
                skipped.append({"id": cid, "reason": str(exc.detail)})
    if updated:
        schedule_metadata_sync(updated)
    return {"ok": True, "updated": len(updated), "skipped": skipped,
            "requested": len(ids)}


@app.post("/api/clips/bulk/trash")
def bulk_trash_clips(body: ClipSelection):
    # One transaction instead of a burst of independent HTTP deletes.
    ids = list(dict.fromkeys(body.ids))
    if not ids or len(ids) > 500:
        raise HTTPException(400, "Select between 1 and 500 clips")
    from local_library import guard_manual, protect, reconcile_manual_pairs
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        for ident in ids:
            if not c.execute("SELECT 1 FROM clips WHERE id=? AND deleted_at IS NULL", (ident,)).fetchone():
                raise HTTPException(404, "Clip not found or already in Trash: " + ident)
        guard_manual(c, "video", ids)
        protect(c, "video", ids, pending=False)
        stamp = now_iso()
        c.executemany("UPDATE clips SET deleted_at=?,updated_at=? WHERE id=? AND deleted_at IS NULL",
                      [(stamp, stamp, ident) for ident in ids])
        reconcile_manual_pairs(c, ids)
    return {"ok": True, "trashed": len(ids)}


@app.delete("/api/clips/{clip_id}")
def trash_clip(clip_id: str):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        from local_library import guard_manual, protect, reconcile_manual_pairs
        if not c.execute("SELECT 1 FROM clips WHERE id=? AND deleted_at IS NULL", (clip_id,)).fetchone():
            raise HTTPException(404, "Clip not found or already in Trash")
        guard_manual(c, 'video', [clip_id]); protect(c, 'video', [clip_id], pending=False)
        c.execute("UPDATE clips SET deleted_at=?,updated_at=? WHERE id=?", (now_iso(), now_iso(), clip_id))
        reconcile_manual_pairs(c, [clip_id])
    schedule_metadata_sync([clip_id])
    return {"ok": True}


@app.post("/api/clips/{clip_id}/restore")
def restore_clip(clip_id: str):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        from local_library import protect, reconcile_manual_pairs
        protect(c, 'video', [clip_id], pending=False)
        row=c.execute('SELECT local_selection FROM clips WHERE id=?',(clip_id,)).fetchone()
        if row and row['local_selection']=='discarded':raise HTTPException(409, 'Discarded clip content was removed')
        c.execute("UPDATE clips SET deleted_at=NULL,updated_at=? WHERE id=?", (now_iso(), clip_id))
        reconcile_manual_pairs(c, [clip_id])
    schedule_metadata_sync([clip_id])
    return {"ok": True}


@app.delete("/api/clips/{clip_id}/permanent")
def permanent_delete(clip_id: str):
    from local_library import erase
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        erase(DATA,c,'video',[clip_id],now_iso)
    return {"ok":True}


@app.post("/api/clips/{clip_id}/reanalyze")
def reanalyze(clip_id: str):
    from ai_workspace import enqueue, locate
    locate(DATA, "video", clip_id)
    app.state.ai_workspace.require_ready()
    job = enqueue(DATA, "analyze", "video", clip_id)
    return {"ok": True, "state": "queued", "job_id": job}


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
            item["drive_failures"] = [dict(r) for r in c.execute("SELECT id AS clip_id,drive_error AS error FROM clips WHERE source_id=? AND drive_state='sync_failed' LIMIT 100", (raw["id"],))]
            rows.append(item)
    return {"items": rows}



# Pre-edited clips are already final assets. Do not run source cutting, AI jobs,
# quality grading or pair review when the owner only wants library storage.
@app.post("/api/clips/processed/upload")
async def upload_processed_clip(file: UploadFile = File(...)):
    allowed = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}
    original_name = str(file.filename or "").replace("\\", "/").rsplit("/", 1)[-1][:200]
    ext = Path(original_name).suffix.lower()
    if ext not in allowed:
        raise HTTPException(400, "Unsupported processed clip format")
    cid = f"CLP-{uuid.uuid4().hex[:10].upper()}"
    sid = f"SRC-{uuid.uuid4().hex[:10].upper()}"
    staging = TEMP_DIR / f".{cid}.upload{ext}"
    thumb_staging = TEMP_DIR / f".{cid}.thumb.jpg"
    clip_name = f"{cid}{ext}"
    thumb_name = f"{cid}.jpg"
    destination = CLIPS_DIR / clip_name
    thumbnail = THUMBS_DIR / thumb_name
    saved = False
    count = 0
    digest = hashlib.sha256()
    try:
        with staging.open("wb") as output:
            while chunk := await file.read(1024 * 1024):
                count += len(chunk)
                if count > 2 * 1024 * 1024 * 1024:
                    raise HTTPException(413, "2 GB clip upload limit")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if not count:
            raise HTTPException(400, "Empty processed clip")
        technical = probe_video(staging)
        try:
            duration = ffprobe_duration(staging)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise HTTPException(422, "Processed clip has no valid duration") from exc
        if not technical["width"] or not technical["height"] or not 0.2 <= duration <= 86400:
            raise HTTPException(422, "File is not a usable video clip")
        fingerprint = digest.hexdigest()
        with db() as conn:
            duplicate = conn.execute(
                "SELECT id,status FROM clips WHERE sha256=? AND deleted_at IS NULL LIMIT 1",
                (fingerprint,),
            ).fetchone()
            if duplicate:
                return {"ok": True, "duplicate": True, "id": duplicate["id"],
                        "status": duplicate["status"]}
        # A single frame is extracted for the UI. Video bytes are never re-encoded.
        try:
            run_ffmpeg_thumb(staging, thumb_staging, min(duration / 2, 5.0))
        except (OSError, subprocess.SubprocessError) as exc:
            raise HTTPException(422, "Could not generate clip preview") from exc
        if not thumb_staging.is_file() or thumb_staging.stat().st_size == 0:
            raise HTTPException(422, "Could not generate clip preview")
        ts = now_iso()
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # Repeat the hash check under a write lock for concurrent uploads.
            duplicate = conn.execute(
                "SELECT id,status FROM clips WHERE sha256=? AND deleted_at IS NULL LIMIT 1",
                (fingerprint,),
            ).fetchone()
            if duplicate:
                return {"ok": True, "duplicate": True, "id": duplicate["id"],
                        "status": duplicate["status"]}
            os.replace(staging, destination)
            os.replace(thumb_staging, thumbnail)
            conn.execute(
                """INSERT INTO sources(id,filename,stored_name,size_bytes,duration,status,
                       stage,progress,clip_count,created_at,updated_at,sha256,
                       codec,width,height,fps,orientation,has_audio,source_kind,
                       rights_confirmed,drive_state,source_deleted_at,source_cleanup_mode)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sid, original_name, f"{sid}.already_processed", count, duration,
                 "completed", "ready_clip_saved", 100, 1, ts, ts, fingerprint,
                 technical["codec"], technical["width"], technical["height"],
                 technical["fps"], technical["orientation"], int(technical["has_audio"]),
                 "processed_clip", 1, "disabled", ts, "already_processed_direct_save"),
            )
            conn.execute(
                """INSERT INTO clips(id,source_id,filename,thumb_filename,start_time,
                       end_time,duration,status,description,quality,ai_state,
                       created_at,updated_at,drive_state,sha256,duplicate_state,local_selection)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (cid, sid, clip_name, thumb_name, 0.0, duration, duration,
                 "approved", "", 0, "pending", ts, ts, "disabled",
                 fingerprint, "clear", "kept"),
            )
        saved = True
        return {"ok": True, "duplicate": False, "id": cid,
                "status": "approved", "duration": round(duration, 3)}
    finally:
        await file.close()
        staging.unlink(missing_ok=True)
        thumb_staging.unlink(missing_ok=True)
        if not saved:
            # Failed commit must not leave orphaned final assets.
            # Duplicate paths are never moved to final destinations.
            with db() as conn:
                persisted = conn.execute("SELECT 1 FROM clips WHERE id=?", (cid,)).fetchone()
            if not persisted:
                destination.unlink(missing_ok=True)
                thumbnail.unlink(missing_ok=True)

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
        technical = probe_video(dest)
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
                         progress,created_at,updated_at,sha256,codec,width,height,fps,
                         orientation,has_audio,source_kind)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sid, Path(file.filename or stored).name, stored, size,
                 "queued", "queued", 0, ts, ts, digest.hexdigest(),
                 technical["codec"], technical["width"], technical["height"],
                 technical["fps"], technical["orientation"], int(technical["has_audio"]),
                 "local"),
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
            raise HTTPException(409, "Source was safely cleaned after local selection verification and cannot be reprocessed")
        existing = c.execute("SELECT COUNT(*) n FROM clips WHERE source_id=?", (source_id,)).fetchone()["n"]
        if existing:
            raise HTTPException(409, "This source already has generated clips. Complete the existing pair review instead of creating duplicates.")
        values = get_settings_map(c)
    with db() as c:
        c.execute("UPDATE sources SET status='queued',stage='queued',progress=0,error=NULL,updated_at=? WHERE id=?", (now_iso(), source_id))
    thread = threading.Thread(target=process_source, args=(source_id,), daemon=True)
    thread.start()
    return {"ok": True, "status": "queued"}


@app.post("/api/sources/{source_id}/manual-cleanup")
def manual_cleanup_source(source_id: str):
    from local_library import finalize
    return finalize(DATA, db, now_iso, source_id)


@app.delete("/api/sources/{source_id}")
def delete_source(source_id: str):
    with db() as c:
        src = c.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if not src:
            raise HTTPException(404, "Source not found")
        c.execute('BEGIN IMMEDIATE')
        if src['status'] in ('queued','processing','failed'):
            raise HTTPException(409, 'Pending, active or failed source is protected')
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
    values["ai_configured"] = "true" if model_ready(DATA / "ai_models") else "false"
    values['drive_enabled'] = 'false'
    return {"values": values, "storage_mode":"vps_only"}



@app.patch("/api/settings")
def update_settings(body: SettingsUpdate):
    values={**get_settings_map(),**body.values}
    try:
        minimum=float(values.get("clip_min_seconds",2));maximum=float(values.get("clip_max_seconds",4));threshold=float(values.get("scene_threshold",.32))
        if not .5<=minimum<=maximum<=30 or not .05<=threshold<=.9:raise ValueError()
    except (ValueError,TypeError):
        raise HTTPException(400,"Use 0.5 <= minimum <= maximum <= 30 seconds and scene sensitivity 0.05–0.9")
    allowed = {"clip_min_seconds", "clip_max_seconds", "scene_threshold", "duplicate_detection", "quality_filter", "ai_auto"}
    with db() as c:
        for k, v in body.values.items():
            if k in allowed:
                c.execute("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))
    return {"ok": True}

# Additive V2 media, image ingestion, and editor API. V1 records/routes remain intact.
from media_extensions import register_media_extensions
register_media_extensions(app, data_dir=DATA, db=db, now_iso=now_iso,
                          get_settings_map=get_settings_map, setting_true=setting_true,
                          source_dir=SOURCES_DIR)

# Owner-opt-in, CPU-only face indexing and person-tag approval.
from face_people import register_face_people
register_face_people(app, data_dir=DATA, db=db, now_iso=now_iso)

# Manual collections coexist with CPU face recognition and all existing media tools.
from manual_people import register_manual_people
register_manual_people(app, db=db, now_iso=now_iso)


# Advanced AI remains an additive, isolated service within the independent app.
from ai_engine import model_ready
from ai_workspace import register_ai_workspace
# Local lifecycle callback retries deferred cleanup after jobs finish.
from local_library import finalize
def local_housekeeping():
    with db() as c:
        ids=[r[0] for r in c.execute("SELECT source_id FROM local_reviews WHERE state='pending'")]
    for ident in ids:
        try: finalize(DATA, db, now_iso, ident)
        except Exception: pass  # Retain all protected media; owner can retry explicitly.
register_ai_workspace(app, DATA, housekeeping_callback=local_housekeeping)
from local_library import register as register_local_library
register_local_library(app, DATA, db, now_iso)
(DATA / 'ai_previews').mkdir(exist_ok=True)
app.mount('/media/ai-previews', StaticFiles(directory=DATA / 'ai_previews'), name='ai-previews')

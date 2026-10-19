"""Independent Auto Stream V1 control plane: never writes Auto Uploader state.

The legacy Short Stream UI/RTMP sessions remain separate and unchanged.
The engine never publishes on import or on a read-only request. Production
publishing and the optional scheduler both require explicit environment gates.
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import threading
import time
import uuid

from fastapi import APIRouter, Body, HTTPException

ROOT = Path(os.environ.get("SHORT_STREAM_DATA", "/var/lib/short-stream"))
ENGINE_ROOT = Path(os.environ.get("AUTO_STREAM_ENGINE_DATA", str(ROOT / "auto-stream-engine")))
MEDIA_ROOT = ROOT / "media"  # Short Stream media ONLY; never Auto Uploader media.
CHANNEL_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
KEY_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
LIVE_STATES = ("STARTING", "INGESTING", "STOPPING")
RTMPS = "rtmps://a.rtmps.youtube.com/live2/"
router = APIRouter(prefix="/engine", tags=["Auto Stream Engine"])


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def scheduled_utc(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("scheduled_at must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("scheduled_at must include a timezone offset")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


class StreamEngine:
    """Durable draft/scheduled jobs with one running worker per channel."""

    def __init__(self, root: Path = ENGINE_ROOT, media: Path = MEDIA_ROOT):
        self.root, self.media = Path(root), Path(media)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = self.root / "stream-jobs.sqlite3"
        self.lock = threading.RLock()
        self.processes: dict[str, subprocess.Popen] = {}
        self._init_db()

    def _connect(self):
        con = sqlite3.connect(str(self.db), timeout=10, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=10000")
        con.execute("PRAGMA journal_mode=WAL")
        return con

    def _init_db(self):
        with self._connect() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, idem TEXT NOT NULL UNIQUE,
                channel_id TEXT NOT NULL, media_path TEXT NOT NULL,
                title TEXT NOT NULL, scheduled_at TEXT,
                continuous INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'DRAFT',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                failure TEXT NOT NULL DEFAULT '')""")
            con.execute("""CREATE UNIQUE INDEX IF NOT EXISTS single_active_channel
                ON jobs(channel_id) WHERE status IN ('STARTING','INGESTING','STOPPING')""")
            # A server restart cannot prove YouTube is live, nor safely
            # resurrect its old in-memory FFmpeg handle. Never auto-replay.
            con.execute("""UPDATE jobs SET status='INTERRUPTED',
                failure='Worker ownership lost; reconcile YouTube before retry',
                updated_at=? WHERE status IN ('STARTING','INGESTING','STOPPING')""",
                (utc_now(),))
        with contextlib.suppress(OSError):
            os.chmod(self.db, 0o600)

    def _source(self, filename: str) -> Path:
        if not filename or filename.startswith("/") or "\\" in filename:
            raise ValueError("media_path must be relative to Short Stream media")
        base = self.media.resolve()
        path = self.media / filename
        # Require a real, regular file; forbid symlinks and parent traversal.
        if any(p.is_symlink() for p in [path, *path.parents] if p != base and base in p.parents):
            raise ValueError("media symlinks are not accepted")
        target = path.resolve()
        if not target.is_relative_to(base) or not target.is_file():
            raise ValueError("Media file is absent or outside Auto Stream sources")
        if target.suffix.lower() not in {".mp4", ".mov", ".mkv", ".webm", ".m4v"}:
            raise ValueError("Unsupported video format")
        return target

    @staticmethod
    def _public(row):
        value = dict(row)
        value["continuous"] = bool(value["continuous"])
        return value

    def list_jobs(self):
        with self._connect() as con:
            return [self._public(r) for r in con.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC, id DESC LIMIT 250")]

    def get(self, jid):
        with self._connect() as con:
            row = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        if row is None:
            raise KeyError("Stream job not found")
        return self._public(row)

    def create(self, payload: dict):
        if not isinstance(payload, dict):
            raise ValueError("Job must be an object")
        channel = str(payload.get("channel_id") or "")
        if not CHANNEL_RE.fullmatch(channel):
            raise ValueError("Valid YouTube channel ID is required")
        source = str(payload.get("media_path") or "")
        self._source(source)
        title = str(payload.get("title") or "").strip()
        if not title or len(title) > 100:
            raise ValueError("Title must contain 1–100 characters")
        idem = str(payload.get("idempotency_key") or "")
        if not KEY_RE.fullmatch(idem):
            raise ValueError("idempotency_key must contain 8–128 safe characters")
        when = scheduled_utc(payload.get("scheduled_at"))
        continuous = bool(payload.get("continuous", False))
        jid, now = "stream_" + uuid.uuid4().hex, utc_now()
        with self.lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            prior = con.execute("SELECT * FROM jobs WHERE idem=?", (idem,)).fetchone()
            if prior:
                if (prior["channel_id"], prior["media_path"], prior["title"],
                    prior["scheduled_at"], bool(prior["continuous"])) != (
                        channel, source, title, when, continuous):
                    raise ValueError("Idempotency key already used for different job")
                return self._public(prior)
            con.execute("""INSERT INTO jobs
                (id, idem, channel_id, media_path, title, scheduled_at,
                 continuous, status, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (jid, idem, channel, source, title, when, int(continuous),
                 "DRAFT", now, now))
        return self.get(jid)

    def prepare(self, jid):
        with self.lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
            if row is None:
                raise KeyError("Stream job not found")
            if row["status"] not in ("DRAFT", "READY", "SCHEDULED"):
                raise ValueError("Job cannot be prepared in this state")
            self._source(row["media_path"])
            target = "SCHEDULED" if row["scheduled_at"] else "READY"
            con.execute("UPDATE jobs SET status=?, updated_at=? WHERE id=?",
                        (target, utc_now(), jid))
        return self.get(jid)

    def _stream_key(self, channel):
        path = self.root / "secrets" / (channel + ".key")
        if not path.is_file() or path.is_symlink():
            raise ValueError("Channel has no independent stream key")
        mode = path.stat().st_mode
        if (mode & 0o077) or not stat.S_ISREG(mode):
            raise ValueError("Stream-key file must be private (0600)")
        secret = path.read_text(encoding="utf-8").strip()
        if not secret or not re.fullmatch(r"[A-Za-z0-9_-]+", secret):
            raise ValueError("Invalid stream key")
        return secret

    def _command(self, row, key):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise ValueError("FFmpeg is not installed")
        source = self._source(row["media_path"])
        args = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                "-re"]
        if row["continuous"]:
            args.extend(["-stream_loop", "-1"])
        args.extend(["-i", str(source),
                     "-map", "0:v:0", "-map", "0:a:0?",
                     "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,"
                            "pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1",
                     "-r", "30", "-c:v", "libx264", "-preset", "veryfast",
                     "-pix_fmt", "yuv420p", "-b:v", "2500k",
                     "-maxrate", "2500k", "-bufsize", "5000k",
                     "-g", "60", "-c:a", "aac", "-b:a", "128k",
                     "-ar", "44100", "-f", "flv", RTMPS + key])
        return args

    def start(self, jid, *, scheduler=False):
        if os.environ.get("AUTO_STREAM_PUBLISH_ENABLED") != "1":
            raise PermissionError("Auto Stream publishing is disabled")
        if scheduler and os.environ.get("AUTO_STREAM_SCHEDULER_ENABLED") != "1":
            raise PermissionError("Automatic streams are disabled")
        with self.lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
            if row is None:
                raise KeyError("Stream job not found")
            if row["status"] not in ("READY", "SCHEDULED"):
                raise ValueError("Only prepared jobs may start")
            if scheduler and (not row["scheduled_at"] or row["scheduled_at"] > utc_now()):
                raise ValueError("Scheduled start has not arrived")
            # Fail closed before reserving the channel.
            command = self._command(row, self._stream_key(row["channel_id"]))
            active = con.execute("""SELECT id FROM jobs WHERE channel_id=?
                        AND status IN ('STARTING','INGESTING','STOPPING')""",
                                 (row["channel_id"],)).fetchone()
            if active:
                raise ValueError("Channel already has an active stream job")
            con.execute("UPDATE jobs SET status='STARTING',updated_at=?,failure='' WHERE id=?",
                        (utc_now(), jid))
        try:
            proc = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    start_new_session=True, close_fds=True)
            with self.lock:
                self.processes[jid] = proc
            self._set_if(jid, ("STARTING",), "INGESTING")
            threading.Thread(target=self._monitor, args=(jid, proc), daemon=True,
                             name="auto-stream-monitor").start()
        except Exception:
            self._set_if(jid, LIVE_STATES, "FAILED", "FFmpeg failed to launch")
            raise RuntimeError("FFmpeg worker launch failed (credentials redacted)") from None
        return self.get(jid)

    def _set_if(self, jid, from_states, status, failure=""):
        with self.lock, self._connect() as con:
            q = ",".join("?" for _ in from_states)
            con.execute("BEGIN IMMEDIATE")
            con.execute("UPDATE jobs SET status=?,failure=?,updated_at=? WHERE id=? "
                        + "AND status IN (" + q + ")",
                        (status, failure, utc_now(), jid, *from_states))

    def _monitor(self, jid, proc):
        result = proc.wait()
        old = self.get(jid)
        self._set_if(jid, LIVE_STATES, "STOPPED" if old["status"] == "STOPPING"
                     or result == 0 else "FAILED",
                     "" if result == 0 or old["status"] == "STOPPING"
                     else "Encoder exited unexpectedly")
        with self.lock:
            if self.processes.get(jid) is proc:
                self.processes.pop(jid, None)

    def stop(self, jid):
        with self.lock:
            row = self.get(jid)
            if row["status"] in ("STOPPED", "FAILED", "INTERRUPTED", "CANCELLED"):
                return row
            if row["status"] in ("DRAFT", "READY", "SCHEDULED"):
                self._set_if(jid, (row["status"],), "CANCELLED")
                return self.get(jid)
            proc = self.processes.get(jid)
            if proc is None:
                raise ValueError("Worker is not owned by this process; reconcile first")
            self._set_if(jid, LIVE_STATES, "STOPPING")
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGTERM)
            return self.get(jid)

    def tick(self):
        """Called only by separately configured, opted-in scheduler process."""
        if (os.environ.get("AUTO_STREAM_PUBLISH_ENABLED") != "1"
                or os.environ.get("AUTO_STREAM_SCHEDULER_ENABLED") != "1"):
            return 0
        with self._connect() as con:
            ids = [row["id"] for row in con.execute(
                "SELECT id FROM jobs WHERE status='SCHEDULED' AND scheduled_at<=? "
                "ORDER BY scheduled_at,created_at LIMIT 10", (utc_now(),))]
        started = 0
        for jid in ids:
            try:
                self.start(jid, scheduler=True)
                started += 1
            except (ValueError, PermissionError, RuntimeError, OSError):
                # Leave failed/prevented jobs for explicit operator review.
                continue
        return started

    def health(self):
        jobs = self.list_jobs()
        return {"ok": True, "engine": "auto-stream", "version": 1,
                "jobs": len(jobs), "encoder_sessions": sum(
                    j["status"] in LIVE_STATES for j in jobs),
                "publishing_enabled": os.environ.get("AUTO_STREAM_PUBLISH_ENABLED") == "1",
                "scheduler_enabled": os.environ.get("AUTO_STREAM_SCHEDULER_ENABLED") == "1",
                "youtube_live_verified": False}


_service: StreamEngine | None = None
_service_lock = threading.Lock()


def service():
    global _service
    with _service_lock:
        if _service is None:
            _service = StreamEngine()
        return _service


def translate(exc):
    if isinstance(exc, KeyError):
        raise HTTPException(404, str(exc)) from exc
    if isinstance(exc, PermissionError):
        raise HTTPException(403, str(exc)) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(409, str(exc)) from exc
    if isinstance(exc, RuntimeError):
        raise HTTPException(503, str(exc)) from exc
    raise exc


@router.get("/health")
def engine_health():
    return service().health()


@router.get("/jobs")
def engine_jobs():
    return {"jobs": service().list_jobs()}


@router.post("/jobs", status_code=201)
def engine_create(payload: dict = Body(...)):
    try:
        return service().create(payload)
    except Exception as exc:
        translate(exc)


@router.get("/jobs/{job_id}")
def engine_get(job_id: str):
    try:
        return service().get(job_id)
    except Exception as exc:
        translate(exc)


@router.post("/jobs/{job_id}/prepare")
def engine_prepare(job_id: str):
    try:
        return service().prepare(job_id)
    except Exception as exc:
        translate(exc)


@router.post("/jobs/{job_id}/start")
def engine_start(job_id: str):
    try:
        return service().start(job_id)
    except Exception as exc:
        translate(exc)


@router.post("/jobs/{job_id}/stop")
def engine_stop(job_id: str):
    try:
        return service().stop(job_id)
    except Exception as exc:
        translate(exc)

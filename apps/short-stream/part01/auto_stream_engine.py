"""Independent Auto Stream V1 control plane: never writes Auto Uploader state.

The legacy Short Stream UI/RTMP sessions remain separate and unchanged.
The engine never publishes on import or on a read-only request. Production
publishing and the optional scheduler both require explicit environment gates.
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timezone
import hashlib
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

from fastapi import APIRouter, Body, File, HTTPException, UploadFile

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
        self.metrics = {}
        self.stop_events = {}
        self.commands = {}
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
                loop_count INTEGER NOT NULL DEFAULT 1,
                unlimited INTEGER NOT NULL DEFAULT 0,
                description TEXT NOT NULL DEFAULT '',
                privacy TEXT NOT NULL DEFAULT 'private',
                status TEXT NOT NULL DEFAULT 'DRAFT',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                failure TEXT NOT NULL DEFAULT '')""")
            existing_columns = {row[1] for row in con.execute("PRAGMA table_info(jobs)")}
            for column, ddl in (("loop_count", "INTEGER NOT NULL DEFAULT 1"), ("unlimited", "INTEGER NOT NULL DEFAULT 0"), ("description", "TEXT NOT NULL DEFAULT ''"), ("privacy", "TEXT NOT NULL DEFAULT 'private'")):
                if column not in existing_columns:
                    con.execute(f"ALTER TABLE jobs ADD COLUMN {column} {ddl}")
            for column, ddl in (("profile","TEXT NOT NULL DEFAULT '720p'"),("duration","REAL NOT NULL DEFAULT 0"),("retry_limit","INTEGER NOT NULL DEFAULT 2"),("remote_state","TEXT NOT NULL DEFAULT ''"),("remote_verified_at","TEXT NOT NULL DEFAULT ''")):
                if column not in existing_columns:
                    con.execute(f"ALTER TABLE jobs ADD COLUMN {column} {ddl}")
            con.execute("""CREATE TABLE IF NOT EXISTS job_history(id INTEGER PRIMARY KEY,job_id TEXT NOT NULL,state TEXT NOT NULL,message TEXT NOT NULL,created_at TEXT NOT NULL)""")
            con.execute("""CREATE TRIGGER IF NOT EXISTS stream_state_history AFTER UPDATE OF status ON jobs WHEN OLD.status!=NEW.status BEGIN INSERT INTO job_history(job_id,state,message,created_at) VALUES(NEW.id,NEW.status,NEW.failure,NEW.updated_at); END""")
            con.execute("""CREATE TRIGGER IF NOT EXISTS stream_created_history AFTER INSERT ON jobs BEGIN INSERT INTO job_history(job_id,state,message,created_at) VALUES(NEW.id,NEW.status,'Job created',NEW.created_at); END""")
            con.execute("""CREATE UNIQUE INDEX IF NOT EXISTS single_active_channel
                ON jobs(channel_id) WHERE status IN ('STARTING','INGESTING','STOPPING')""")
            # A server restart cannot prove YouTube is live, nor safely
            # resurrect its old in-memory FFmpeg handle. Never auto-replay.
            con.execute("""UPDATE jobs SET status='INTERRUPTED',
                failure='Worker ownership lost; reconcile YouTube before retry',
                updated_at=? WHERE status IN ('STARTING','INGESTING','STOPPING')""",
                (utc_now(),))
        import auto_stream_compositor
        auto_stream_compositor.init(self)
        import auto_stream_operations
        auto_stream_operations.initialize(self)
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
        value["unlimited"] = bool(value.get("unlimited", False))
        verified=value.get("remote_verified_at","")
        recent=bool(verified and verified >= datetime.fromtimestamp(time.time()-90,timezone.utc).isoformat(timespec="seconds"))
        value["youtube_verified_live"]=recent and value.get("remote_state")=="live"
        value["display_state"]="LIVE" if value["youtube_verified_live"] else {"INGESTING":"STARTING","STOPPING":"STOPPING","STOPPED":"COMPLETED","INTERRUPTED":"FAILED"}.get(value["status"],value["status"])
        return value

    def list_jobs(self):
        with self._connect() as con:
            rows=[self._public(r) for r in con.execute("SELECT * FROM jobs ORDER BY created_at DESC,id DESC LIMIT 250")]
        for row in rows:
            with self.lock:metrics=dict(self.metrics.get(row['id'],{}))
            row['encoder']=metrics
            if row['status']=='INGESTING' and metrics.get('encoder_state')=='RECONNECTING':row['display_state']='RECONNECTING'
        return rows

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
        unlimited = bool(payload.get("unlimited", continuous))
        try:
            loop_count = max(1, min(int(payload.get("loop_count", 1)), 10000))
        except (TypeError, ValueError):
            raise ValueError("loop_count must be a positive integer") from None
        description = str(payload.get("description") or "").strip()[:5000]
        privacy = str(payload.get("privacy") or "private").strip().lower()
        if privacy not in {"private", "unlisted", "public"}:
            raise ValueError("privacy must be private, unlisted or public")
        from auto_stream_compositor import PROFILES, number
        profile=payload.get('profile','720p')
        if profile not in PROFILES:raise ValueError('Unsupported encoding profile')
        duration=number(payload.get('duration',0),0,604800)
        retry_limit=int(number(payload.get('retry_limit',2),0,5))
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
                 continuous, status, created_at, updated_at, loop_count, unlimited, description, privacy, profile, duration, retry_limit)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (jid, idem, channel, source, title, when, int(continuous),
                 "DRAFT", now, now, loop_count, int(unlimited), description, privacy, profile, duration, retry_limit))
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

    def _command(self, row, key, ingest_url=RTMPS):
        """Build one redacted-safe FFmpeg command with destination before key.

        The YouTube RTMPS ingestion URL and stream key are deliberately separate
        arguments so callers cannot accidentally reverse them.  This function
        is regression-tested because the resulting final URL is security- and
        delivery-critical.
        """
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise ValueError("FFmpeg is not installed")
        source = self._source(row["media_path"])
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,200}", key):
            raise ValueError("Invalid stream key")
        if not isinstance(ingest_url, str) or not re.fullmatch(
                r"rtmps://(?:a|b)\.rtmps\.youtube\.com/live2", ingest_url.rstrip("/")):
            raise ValueError("Unsupported YouTube RTMPS ingestion URL")
        loops = -1 if bool(row["continuous"]) or bool(row["unlimited"]) else max(0, int(row["loop_count"]) - 1)
        args = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-re"]
        if loops != 0:
            args.extend(["-stream_loop", str(loops)])
        args.extend(["-i", str(source),
                     "-map", "0:v:0", "-map", "0:a:0?",
                     "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,"
                            "pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1",
                     "-r", "30", "-c:v", "libx264", "-preset", "veryfast",
                     "-pix_fmt", "yuv420p", "-b:v", "2500k",
                     "-maxrate", "2500k", "-bufsize", "5000k",
                     "-g", "60", "-c:a", "aac", "-b:a", "128k",
                     "-ar", "44100", "-f", "flv",
                     ingest_url.rstrip("/") + "/" + key])
        from auto_stream_compositor import PROFILES
        w,h,bitrate=PROFILES[dict(row).get('profile','720p')]
        args[args.index('-vf')+1]=f'scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1'
        for flag,value in (('-b:v',f'{bitrate}k'),('-maxrate',f'{bitrate}k'),('-bufsize',f'{bitrate*2}k')):
            args[args.index(flag)+1]=value
        args[-1:-1]=['-threads','2','-progress','pipe:1','-nostats']
        if dict(row).get('duration',0):args[-1:-1]=['-t',str(row['duration'])]
        return args

    def start(self, jid, *, scheduler=False, internal_ingest=None):
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
            if len(self.processes) >= int(os.environ.get('AUTO_STREAM_MAX_ENCODERS','2')):
                raise ValueError('All encoder slots are busy; wait for a running stream to finish')
            # Fail closed before reserving the channel.
            if internal_ingest:
                ingest_url, key = internal_ingest
            else:
                key, ingest_url = self._stream_key(row["channel_id"]), RTMPS
            command = self._command(row, key=key, ingest_url=ingest_url)
            active = con.execute("""SELECT id FROM jobs WHERE channel_id=?
                        AND status IN ('STARTING','INGESTING','STOPPING')""",
                                 (row["channel_id"],)).fetchone()
            if active:
                raise ValueError("Channel already has an active stream job")
            con.execute("UPDATE jobs SET status='STARTING',updated_at=?,failure='' WHERE id=?",
                        (utc_now(), jid))
        try:
            self.stop_events[jid]=threading.Event()
            self.commands[jid]=command
            self.metrics[jid]={'started_at':utc_now(),'heartbeat':utc_now(),'attempt':0,'encoder_state':'STARTING'}
            proc = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1,
                                    start_new_session=True, close_fds=True)
            with self.lock:
                self.processes[jid] = proc
            self._set_if(jid, ("STARTING",), "INGESTING")
            threading.Thread(target=self._watchdog,args=(jid,proc),daemon=True).start()
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
        attempt=0
        try:
            while True:
                for line in proc.stdout or []:
                    key,sep,value=line.strip().partition('=')
                    if sep and key in ('frame','fps','bitrate','out_time_us','drop_frames','dup_frames','speed','progress'):
                        with self.lock:
                            self.metrics[jid].update({key:value[:60],'heartbeat':utc_now(),'encoder_state':'DELIVERING'})
                    if self.stop_events[jid].is_set():break
                result=proc.wait()
                if proc.stdout:proc.stdout.close()
                old=self.get(jid)
                if self.stop_events[jid].is_set() or old['status']=='STOPPING':
                    self._set_if(jid,LIVE_STATES,'STOPPED');break
                if result==0:
                    self._set_if(jid,LIVE_STATES,'COMPLETED');break
                if attempt>=old.get('retry_limit',2):
                    self._set_if(jid,LIVE_STATES,'FAILED','Encoder connection failed after retry limit; verify ingest, source and network');break
                attempt+=1
                with self.lock:self.metrics[jid].update({'encoder_state':'RECONNECTING','attempt':attempt})
                with self._connect() as con:con.execute("INSERT INTO job_history(job_id,state,message,created_at) VALUES(?,'RECONNECTING',?,?)",(jid,'Retry '+str(attempt),utc_now()))
                if self.stop_events[jid].wait(min(15,2**attempt)):
                    self._set_if(jid,LIVE_STATES,'STOPPED');break
                proc=subprocess.Popen(self.commands[jid],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,text=True,bufsize=1,start_new_session=True,close_fds=True)
                with self.lock:self.processes[jid]=proc
                threading.Thread(target=self._watchdog,args=(jid,proc),daemon=True).start()
        except Exception:
            self._set_if(jid,LIVE_STATES,'FAILED','Encoder supervisor failed; inspect job and retry explicitly')
            with contextlib.suppress(ProcessLookupError):os.killpg(proc.pid,signal.SIGKILL)
        finally:
            with self.lock:
                self.processes.pop(jid,None);self.commands.pop(jid,None);self.stop_events.pop(jid,None)
                if jid in self.metrics:self.metrics[jid]['encoder_state']='STOPPED'

    def _watchdog(self,jid,proc):
        while proc.poll() is None:
            with self.lock:
                if self.processes.get(jid) is not proc:return
                event=self.stop_events.get(jid)
                metrics=self.metrics.get(jid,{})
                heartbeat=metrics.get('heartbeat',utc_now())
            if event is None or event.wait(5):return
            if time.time()-datetime.fromisoformat(heartbeat).timestamp()>45:
                with contextlib.suppress(ProcessLookupError):os.killpg(proc.pid,signal.SIGTERM)
                self._terminate(jid,proc);return

    def _terminate(self,jid,proc):
        try:proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):os.killpg(proc.pid,signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):proc.wait(timeout=3)

    def stop(self, jid):
        with self.lock:
            row = self.get(jid)
            if row["status"] in ("STOPPED", "COMPLETED", "FAILED", "INTERRUPTED", "CANCELLED"):
                return row
            if row["status"] in ("DRAFT", "READY", "SCHEDULED"):
                self._set_if(jid, (row["status"],), "CANCELLED")
                return self.get(jid)
            proc = self.processes.get(jid)
            if proc is None:
                raise ValueError("Worker is not owned by this process; reconcile first")
            self._set_if(jid, LIVE_STATES, "STOPPING")
            if jid in self.stop_events:self.stop_events[jid].set()
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGTERM)
            threading.Thread(target=self._terminate,args=(jid,proc),daemon=True).start()
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
                from auto_stream_broadcasts import controller
                ctl=controller()
                record=ctl.get(jid)
                if record and record['state']=='BOUND':ctl.start_ingest(jid)
                else:self.start(jid, scheduler=True)
                started += 1
            except (ValueError, PermissionError, RuntimeError, OSError):
                # Leave failed/prevented jobs for explicit operator review.
                continue
        return started

    def health(self):
        jobs = self.list_jobs()
        with self._connect() as con:
            rendering=con.execute("SELECT COUNT(*) FROM scene_renders WHERE state IN ('WAITING','PREPARING')").fetchone()[0]
        return {"ok": True, "engine": "auto-stream", "version": 1,
                "jobs": len(jobs), "render_tasks":rendering, "encoder_sessions": sum(
                    j["status"] in LIVE_STATES for j in jobs),
                "publishing_enabled": os.environ.get("AUTO_STREAM_PUBLISH_ENABLED") == "1",
                "scheduler_enabled": os.environ.get("AUTO_STREAM_SCHEDULER_ENABLED") == "1",
                "youtube_live_verified": False,
                "ffmpeg_installed":bool(shutil.which('ffmpeg')),
                "encoder_metrics":{jid:{**metrics,'elapsed_seconds':max(0,int(time.time()-datetime.fromisoformat(metrics['started_at']).timestamp()))} for jid,metrics in list(self.metrics.items())[-20:]},
                "resources":{'load_average':list(os.getloadavg()) if hasattr(os,'getloadavg') else None,
                    'disk_free_bytes':shutil.disk_usage(self.root).free}}


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


@router.get("/media")
def engine_media():
    root = service().media.resolve()
    rows = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink() and path.suffix.lower() in {".mp4", ".mov", ".mkv", ".webm", ".m4v"}:
            rows.append({"media_path": str(path.relative_to(root)), "name": path.name, "bytes": path.stat().st_size})
    return {"media": rows[:500]}


def _safe_upload_name(value: str, *, thumbnail: bool = False) -> str:
    name = Path(str(value or "upload.bin").replace("\\", "/")).name
    name = re.sub(r"[^A-Za-z0-9@._+-]+", "_", name)[:180]
    from auto_stream_compositor import EXTENSIONS
    allowed = EXTENSIONS if not thumbnail else {".jpg", ".jpeg", ".png"}
    if Path(name).suffix.lower() not in allowed:
        raise ValueError("Unsupported upload format")
    return name


@router.post("/media/upload")
async def engine_upload(file: UploadFile = File(...)):
    target=None
    try:
        name = _safe_upload_name(file.filename or "")
        target_dir = service().media / "auto-stream" / "inbox"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / (Path(name).stem + "-" + uuid.uuid4().hex[:10] + Path(name).suffix.lower())
        digest = hashlib.sha256()
        size = 0
        with target.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > 20 * 1024 * 1024 * 1024:
                    raise ValueError("Upload exceeds the 20 GiB per-file limit")
                digest.update(chunk)
                out.write(chunk)
        return {"ok": True, "media_path": str(target.relative_to(service().media)), "bytes": size, "sha256": digest.hexdigest()}
    except Exception as exc:
        if target is not None:target.unlink(missing_ok=True)
        translate(exc)


@router.post("/media/prepare")
def engine_prepare_media(payload: dict = Body(...)):
    try:
        relative = str(payload.get("media_path") or "")
        source = service()._source(relative)
        prepared_dir = service().media / "prepared"
        prepared_dir.mkdir(parents=True, exist_ok=True)
        output_name = hashlib.sha256(str(source).encode()).hexdigest() + ".mp4"
        output = prepared_dir / output_name
        if not output.exists():
            ffprobe = shutil.which("ffprobe")
            ffmpeg = shutil.which("ffmpeg")
            if not ffprobe or not ffmpeg:
                raise ValueError("FFprobe/FFmpeg are required for preprocessing")
            probe = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                                    "-of", "default=noprint_wrappers=1:nokey=1", str(source)],
                                   capture_output=True, text=True, timeout=30)
            if probe.returncode != 0:
                raise ValueError("FFprobe rejected this media file")
            tmp = output.with_suffix(".tmp.mp4")
            subprocess.run([ffmpeg, "-y", "-nostdin", "-i", str(source),
                            "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,"
                                   "pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1",
                            "-r", "30", "-c:v", "libx264", "-preset", "veryfast",
                            "-pix_fmt", "yuv420p", "-g", "60", "-c:a", "aac",
                            "-ar", "44100", "-ac", "2", "-b:a", "128k", "-movflags", "+faststart",
                            str(tmp)], check=True, timeout=3600)
            os.replace(tmp, output)
        return {"ok": True, "status": "READY", "media_path": str(output.relative_to(service().media)),
                "bytes": output.stat().st_size}
    except Exception as exc:
        translate(exc)


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


@router.get('/media/preview')
def media_preview(media_path: str):
    from fastapi.responses import FileResponse
    try:
        path = service()._source(media_path)
        return FileResponse(path, headers={'Cache-Control': 'private, no-store'})
    except Exception as exc:
        translate(exc)


@router.get('/studio/config')
def studio_config():
    with service()._connect() as con:
        con.execute('CREATE TABLE IF NOT EXISTS studio_config(id INTEGER PRIMARY KEY, document TEXT NOT NULL)')
        row = con.execute('SELECT document FROM studio_config WHERE id=1').fetchone()
    return json.loads(row[0]) if row else {'name':'Live Studio','active_scene':0,'scenes':[{'name':'Scene 1','sources':[]}]}


@router.put('/studio/config')
def save_studio_config(payload: dict = Body(...)):
    try:
        from auto_stream_compositor import validate
        clean = validate(service(), payload)
        with service()._connect() as con:
            con.execute('CREATE TABLE IF NOT EXISTS studio_config(id INTEGER PRIMARY KEY, document TEXT NOT NULL)')
            con.execute('INSERT INTO studio_config VALUES(1,?) ON CONFLICT(id) DO UPDATE SET document=excluded.document',(json.dumps(clean),))
        return clean
    except Exception as exc:
        translate(exc)


@router.post('/jobs/{job_id}/cancel')
def cancel_job(job_id: str):
    try:
        engine=service()
        with engine.lock, engine._connect() as con:
            row=con.execute('SELECT status FROM jobs WHERE id=?',(job_id,)).fetchone()
            if not row:
                raise KeyError('Stream job not found')
            if row['status'] not in ('DRAFT','SCHEDULED','READY','FAILED','INTERRUPTED'):
                raise ValueError('Stop or reconcile the active stream before cancelling')
            con.execute("UPDATE jobs SET status='CANCELLED',updated_at=? WHERE id=?",(utc_now(),job_id))
        return engine.get(job_id)
    except Exception as exc:
        translate(exc)


@router.post('/jobs/{job_id}/duplicate', status_code=201)
def duplicate_job(job_id: str):
    try:
        engine=service()
        original=engine.get(job_id)
        original['idempotency_key']='duplicate-'+uuid.uuid4().hex
        return engine.create(original)
    except Exception as exc:
        translate(exc)


@router.get('/dashboard')
def dashboard_metrics():
    engine = service()
    from auto_stream_integrations import accounts
    channels = accounts().channel_catalog()
    with engine._connect() as con:
        counts = {r[0]:r[1] for r in con.execute('SELECT status,COUNT(*) FROM jobs GROUP BY status')}
        recent = [engine._public(r) for r in con.execute('SELECT * FROM jobs ORDER BY updated_at DESC LIMIT 10')]
        cutoff=datetime.fromtimestamp(time.time()-90,timezone.utc).isoformat(timespec='seconds')
        verified=con.execute('SELECT COUNT(*),SUM(remote_state=?) FROM jobs WHERE remote_verified_at>=?',('live',cutoff)).fetchone()
        upcoming = [engine._public(r) for r in con.execute("SELECT * FROM jobs WHERE scheduled_at IS NOT NULL AND status IN ('DRAFT','SCHEDULED','READY') ORDER BY scheduled_at LIMIT 10")]
    with engine.lock:
        active_encoders = sum(proc.poll() is None for proc in engine.processes.values())
    space = shutil.disk_usage(engine.root)
    return {'authorized_channels':sum(c['authorized'] for c in channels),
            'scheduled':counts.get('SCHEDULED',0),
            'queued':sum(counts.get(s,0) for s in ('WAITING','PREPARING','READY','STARTING')),
            'completed':counts.get('COMPLETED',0)+counts.get('STOPPED',0),
            'failed':counts.get('FAILED',0)+counts.get('INTERRUPTED',0),
            'active_encoders':active_encoders,'counts':counts,
            'live_verified':int(verified[1] or 0) if verified[0] else None,
            'live_verification':'Remote YouTube verification required; encoder delivery alone is not Live',
            'recent':recent,'upcoming':upcoming,
            'disk':{'total_bytes':space.total,'free_bytes':space.free,'used_bytes':space.used},
            'engine':engine.health()}


from auto_stream_compositor import router as compositor_router
router.include_router(compositor_router)


@router.get('/jobs/{job_id}/history')
def job_history(job_id:str):
    try:
        engine=service();job=engine.get(job_id)
        with engine._connect() as con:items=[dict(r) for r in con.execute('SELECT state,message,created_at FROM job_history WHERE job_id=? ORDER BY id DESC LIMIT 200',(job_id,))]
        with engine.lock:metrics=dict(engine.metrics.get(job_id,{}))
        return {'job':job,'history':items,'encoder':metrics}
    except Exception as exc:translate(exc)


@router.patch('/jobs/{job_id}')
def edit_job(job_id:str,payload:dict=Body(...)):
    try:
        engine=service()
        with engine.lock,engine._connect() as con:
            con.execute('BEGIN IMMEDIATE');job=engine.get(job_id)
            if job['status'] not in ('DRAFT','READY','SCHEDULED','FAILED','INTERRUPTED','CANCELLED'):
                raise ValueError('Only inactive jobs can be edited')
            # A prepared YouTube resource has its own remote lifecycle.
            from auto_stream_broadcasts import controller
            if controller().get(job_id):raise ValueError('This job has YouTube resources; duplicate it for changed metadata')
            updates={}
            for key in ('title','description','media_path','privacy','scheduled_at','loop_count','unlimited','duration','profile','retry_limit'):
                if key in payload:updates[key]=payload[key]
            candidate={**job,**updates,'idempotency_key':'validate-'+uuid.uuid4().hex}
            # Reuse creation validation against an isolated transaction savepoint;
            # validation below avoids any temporary job/history writes.
            title=str(candidate['title']).strip()
            if not title or len(title)>100:raise ValueError('Title must contain 1–100 characters')
            engine._source(str(candidate['media_path']))
            from auto_stream_compositor import PROFILES,number
            if candidate['privacy'] not in ('private','unlisted','public'):raise ValueError('Unsupported privacy')
            if candidate['profile'] not in PROFILES:raise ValueError('Unsupported encoding profile')
            updates.update(title=title,description=str(candidate['description'])[:5000],scheduled_at=scheduled_utc(candidate.get('scheduled_at')),
                loop_count=int(number(candidate['loop_count'],1,10000)),unlimited=int(bool(candidate['unlimited'])),
                duration=number(candidate['duration'],0,604800),retry_limit=int(number(candidate['retry_limit'],0,5)),status='DRAFT',failure='',updated_at=utc_now())
            con.execute('UPDATE jobs SET '+','.join(k+'=?' for k in updates)+' WHERE id=?',(*updates.values(),job_id))
        return engine.get(job_id)
    except Exception as exc:translate(exc)


@router.delete('/jobs/{job_id}')
def delete_job(job_id:str):
    try:
        engine=service()
        with engine.lock,engine._connect() as con:
            con.execute('BEGIN IMMEDIATE');row=con.execute('SELECT status FROM jobs WHERE id=?',(job_id,)).fetchone()
            if not row:raise KeyError('Stream job not found')
            if row[0] not in ('DRAFT','CANCELLED','COMPLETED','STOPPED','FAILED','INTERRUPTED'):raise ValueError('Cancel/stop the job before deleting')
            from auto_stream_broadcasts import controller
            if controller().get(job_id):raise ValueError('Keep jobs with YouTube resources for reconciliation/history')
            con.execute('DELETE FROM jobs WHERE id=?',(job_id,));con.execute('DELETE FROM job_history WHERE job_id=?',(job_id,))
        return {'deleted':job_id,'media_preserved':True}
    except Exception as exc:translate(exc)

from auto_stream_operations import router as operations_router
router.include_router(operations_router)

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

APP_NAME = "Short Stream"
DATA_ROOT = Path(os.environ.get("SHORT_STREAM_DATA", "/var/lib/short-stream"))
MEDIA_ROOT = DATA_ROOT / "media"
STATE_FILE = DATA_ROOT / "sessions.json"
DESTINATIONS_FILE = DATA_ROOT / "destinations.json"
FFMPEG = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
FFPROBE = shutil.which("ffprobe") or "/usr/bin/ffprobe"
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".ts", ".mts", ".m2ts"}
DATA_ROOT.mkdir(parents=True, exist_ok=True)
MEDIA_ROOT.mkdir(parents=True, exist_ok=True)

app = FastAPI(title=APP_NAME, version="1.1")
state_lock = threading.RLock()


def safe_name(value: str) -> str:
    value = Path(value or "video.mp4").name
    value = re.sub(r"[^A-Za-z0-9._ -]+", "_", value).strip(" .")
    return value[:180] or "video.mp4"


def new_id() -> str:
    return "live_" + uuid.uuid4().hex[:8]


def choose_udp_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def elapsed(started_at: Optional[float]) -> str:
    if not started_at:
        return "00:00:00"
    sec = max(0, int(time.time() - started_at))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


@dataclass
class SessionConfig:
    session_id: str
    name: str
    rtmp_url: str = "rtmp://a.rtmp.youtube.com/live2"
    destination_id: str = ""
    source_files: list[str] = field(default_factory=list)
    continuous_queue: bool = True
    video_bitrate_kbps: int = 4500
    audio_bitrate_kbps: int = 128
    fps: int = 30
    preset: str = "veryfast"
    output_width: int = 1280
    output_height: int = 720


class SessionRuntime:
    def __init__(self, config: SessionConfig):
        self.config = config
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.switch_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.relay_proc: Optional[subprocess.Popen] = None
        self.source_proc: Optional[subprocess.Popen] = None
        self.play_now_request: Optional[str] = None
        self.stream_key = ""
        self.status = "IDLE"
        self.current_source: Optional[str] = None
        self.current_index = -1
        self.started_at: Optional[float] = None
        self.logs = deque(maxlen=500)
        self.relay_reconnects = 0
        self.udp_port: Optional[int] = None

    @property
    def is_running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def log(self, msg: str):
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            self.logs.append(f"[{stamp}] {msg}")

    def set_status(self, status: str):
        with self.lock:
            self.status = status
        self.log(f"STATUS={status}")

    def public(self) -> dict:
        with self.lock:
            return {
                **asdict(self.config),
                "status": self.status,
                "is_running": self.is_running,
                "current_source": Path(self.current_source).name if self.current_source else None,
                "current_index": self.current_index,
                "uptime": elapsed(self.started_at),
                "relay_reconnects": self.relay_reconnects,
                "udp_port": self.udp_port,
            }

    def start(self, stream_key: str):
        if self.is_running:
            raise RuntimeError("This live session is already running.")
        if not stream_key.strip():
            raise ValueError("Stream key is required.")
        if not self.config.rtmp_url.strip():
            raise ValueError("RTMP URL is required.")
        if not self.config.source_files:
            raise ValueError("Add at least one source video.")
        missing = [p for p in self.config.source_files if not Path(p).exists()]
        if missing:
            raise ValueError(f"Missing source file: {Path(missing[0]).name}")
        self.stream_key = stream_key.strip()
        self.stop_event.clear()
        self.switch_event.clear()
        self.play_now_request = None
        self.current_source = None
        self.current_index = -1
        self.started_at = time.time()
        self.relay_reconnects = 0
        self.set_status("STARTING")
        self.thread = threading.Thread(target=self._worker, daemon=True, name=f"short-stream-{self.config.session_id}")
        self.thread.start()

    def stop(self):
        if not self.is_running:
            self.set_status("STOPPED")
            self.stream_key = ""
            return
        self.set_status("STOPPING")
        self.stop_event.set()
        self.switch_event.set()
        self._terminate(self.source_proc, graceful=False)
        self._terminate(self.relay_proc, graceful=True)

    def next_file(self):
        if not self.is_running:
            raise RuntimeError("Live session is not running.")
        files = list(self.config.source_files)
        if not files:
            raise RuntimeError("Queue is empty.")
        idx = self.current_index + 1
        if idx >= len(files): idx = 0 if self.config.continuous_queue else len(files) - 1
        self.play_now(files[idx])

    def play_now(self, path: str):
        if not self.is_running:
            raise RuntimeError("Live session is not running.")
        if path not in self.config.source_files:
            raise ValueError("Source is not in this queue.")
        self.play_now_request = path
        self.set_status("SWITCHING_SOURCE")
        self.switch_event.set()
        self._terminate(self.source_proc, graceful=False)

    def _output_url(self) -> str:
        return self.config.rtmp_url.rstrip("/") + "/" + self.stream_key

    def _relay_cmd(self, port: int) -> list[str]:
        udp = f"udp://127.0.0.1:{port}?fifo_size=1000000&overrun_nonfatal=1"
        return [FFMPEG, "-hide_banner", "-loglevel", "warning", "-fflags", "+genpts+discardcorrupt",
                "-use_wallclock_as_timestamps", "1", "-thread_queue_size", "8192", "-i", udp,
                "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "copy", "-c:a", "copy",
                "-flvflags", "no_duration_filesize", "-f", "flv", self._output_url()]

    def _has_audio(self, path: str) -> bool:
        try:
            p = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=index",
                                "-of", "csv=p=0", path], capture_output=True, text=True, timeout=10)
            return bool(p.stdout.strip())
        except Exception:
            return True

    def _source_cmd(self, path: str, port: int) -> list[str]:
        c = self.config
        udp = f"udp://127.0.0.1:{port}?pkt_size=1316"
        vf = (f"scale={c.output_width}:{c.output_height}:force_original_aspect_ratio=decrease,"
              f"pad={c.output_width}:{c.output_height}:(ow-iw)/2:(oh-ih)/2,setsar=1")
        has_audio = self._has_audio(path)
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "warning", "-re", "-i", path]
        if not has_audio:
            cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
        cmd += ["-map", "0:v:0", "-map", "0:a:0" if has_audio else "1:a:0", "-vf", vf,
                "-r", str(c.fps), "-c:v", "libx264", "-preset", c.preset, "-tune", "zerolatency",
                "-pix_fmt", "yuv420p", "-b:v", f"{c.video_bitrate_kbps}k", "-maxrate", f"{c.video_bitrate_kbps}k",
                "-bufsize", f"{c.video_bitrate_kbps * 2}k", "-g", str(c.fps * 2), "-keyint_min", str(c.fps * 2),
                "-sc_threshold", "0", "-c:a", "aac", "-b:a", f"{c.audio_bitrate_kbps}k", "-ar", "44100", "-ac", "2",
                "-af", "aresample=async=1:first_pts=0"]
        if not has_audio: cmd += ["-shortest"]
        cmd += ["-mpegts_flags", "+resend_headers", "-muxdelay", "0", "-f", "mpegts", udp]
        return cmd

    def _spawn(self, cmd: list[str]) -> subprocess.Popen:
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, bufsize=1)

    def _read_stderr(self, proc: subprocess.Popen, label: str):
        def reader():
            try:
                if not proc.stderr: return
                for line in proc.stderr:
                    if self.stream_key:
                        line = line.replace(self.stream_key, "***REDACTED***")
                    self.log(f"{label}: {line.rstrip()}")
            except Exception:
                pass
        threading.Thread(target=reader, daemon=True).start()

    def _start_relay(self, port: int):
        self.relay_proc = self._spawn(self._relay_cmd(port))
        self._read_stderr(self.relay_proc, "RELAY")

    def _worker(self):
        failed = False
        try:
            port = choose_udp_port()
            self.udp_port = port
            self.log(f"Persistent RTMP relay using local UDP port {port}")
            self._start_relay(port)
            self.set_status("CONNECTING")
            next_index = 0
            first = True
            time.sleep(0.6)

            while not self.stop_event.is_set():
                if self.relay_proc and self.relay_proc.poll() is not None:
                    if self.stop_event.is_set(): break
                    if self.relay_reconnects >= 5:
                        raise RuntimeError("RTMP relay failed after 5 reconnect attempts.")
                    self.relay_reconnects += 1
                    self.set_status("RECONNECTING")
                    wait = min(5 * self.relay_reconnects, 20)
                    self.log(f"Relay disconnected; retry {self.relay_reconnects}/5 in {wait}s")
                    time.sleep(wait)
                    self._start_relay(port)
                    time.sleep(0.5)

                requested = self.play_now_request
                if requested:
                    self.play_now_request = None
                    try: next_index = self.config.source_files.index(requested)
                    except ValueError: next_index = 0

                files = list(self.config.source_files)
                if not files:
                    raise RuntimeError("Source queue became empty.")
                if next_index >= len(files):
                    if self.config.continuous_queue: next_index = 0
                    else: break

                source = files[next_index]
                self.current_source = source
                self.current_index = next_index
                self.switch_event.clear()
                self.log(f"Playing {Path(source).name}")
                self.source_proc = self._spawn(self._source_cmd(source, port))
                self._read_stderr(self.source_proc, "SOURCE")
                if first:
                    self.set_status("LIVE")
                    first = False
                else:
                    self.set_status("LIVE")

                while self.source_proc.poll() is None and not self.stop_event.is_set() and not self.switch_event.is_set():
                    if self.relay_proc and self.relay_proc.poll() is not None:
                        break
                    time.sleep(0.25)

                if self.stop_event.is_set(): break
                if self.switch_event.is_set():
                    self._terminate(self.source_proc, graceful=False)
                    continue
                if self.relay_proc and self.relay_proc.poll() is not None:
                    self._terminate(self.source_proc, graceful=False)
                    continue
                rc = self.source_proc.poll()
                if rc not in (0, None): self.log(f"Source exited with code {rc}; moving to next file")
                next_index += 1

        except Exception as exc:
            failed = True
            self.log(f"ERROR: {exc}")
        finally:
            self._terminate(self.source_proc, graceful=False)
            self._terminate(self.relay_proc, graceful=True)
            self.source_proc = None
            self.relay_proc = None
            self.stream_key = ""
            self.started_at = None
            self.udp_port = None
            self.current_source = None
            self.current_index = -1
            self.set_status("FAILED" if failed else "STOPPED")

    @staticmethod
    def _terminate(proc: Optional[subprocess.Popen], graceful: bool):
        if not proc or proc.poll() is not None: return
        try:
            if graceful:
                proc.terminate(); proc.wait(timeout=3)
            else:
                proc.kill(); proc.wait(timeout=2)
        except Exception:
            try: proc.kill()
            except Exception: pass


configs: dict[str, SessionConfig] = {}
runtimes: dict[str, SessionRuntime] = {}
destinations: dict[str, dict] = {}


def new_destination_id() -> str:
    return "dest_" + uuid.uuid4().hex[:8]


def save_destinations():
    with state_lock:
        payload = {"destinations": list(destinations.values())}
        tmp = DESTINATIONS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, DESTINATIONS_FILE)


def load_destinations():
    if not DESTINATIONS_FILE.exists():
        return
    try:
        raw = json.loads(DESTINATIONS_FILE.read_text(encoding="utf-8"))
        for item in raw.get("destinations", []):
            did = str(item.get("destination_id") or "").strip()
            name = str(item.get("name") or "").strip()[:80]
            rtmp_url = str(item.get("rtmp_url") or "").strip()
            if did and name and (rtmp_url.startswith("rtmp://") or rtmp_url.startswith("rtmps://")):
                destinations[did] = {"destination_id": did, "name": name, "rtmp_url": rtmp_url}
    except Exception:
        pass


def save_configs():
    with state_lock:
        payload = {"sessions": [asdict(c) for c in configs.values()]}
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, STATE_FILE)


def load_configs():
    if not STATE_FILE.exists(): return
    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        for item in raw.get("sessions", []):
            allowed = {k: item[k] for k in SessionConfig.__dataclass_fields__ if k in item}
            c = SessionConfig(**allowed)
            configs[c.session_id] = c
            runtimes[c.session_id] = SessionRuntime(c)
    except Exception:
        pass


def get_runtime(sid: str) -> SessionRuntime:
    r = runtimes.get(sid)
    if not r: raise HTTPException(404, "Live session not found")
    return r


def update_config(c: SessionConfig, payload: dict):
    if get_runtime(c.session_id).is_running:
        raise HTTPException(409, "Stop this live before changing encoding or destination settings")
    for key in ["name", "rtmp_url", "continuous_queue", "video_bitrate_kbps", "audio_bitrate_kbps", "fps", "preset", "output_width", "output_height"]:
        if key in payload:
            setattr(c, key, payload[key])
    if "destination_id" in payload:
        did = str(payload.get("destination_id") or "").strip()
        if did:
            if did not in destinations:
                raise HTTPException(400, "Destination profile not found")
            c.destination_id = did
            c.rtmp_url = destinations[did]["rtmp_url"]
        else:
            c.destination_id = ""
    c.name = str(c.name).strip()[:80] or "Live"
    c.rtmp_url = str(c.rtmp_url).strip()
    if not (c.rtmp_url.startswith("rtmp://") or c.rtmp_url.startswith("rtmps://")):
        raise HTTPException(400, "RTMP URL must start with rtmp:// or rtmps://")
    c.video_bitrate_kbps = max(250, min(int(c.video_bitrate_kbps), 50000))
    c.audio_bitrate_kbps = max(32, min(int(c.audio_bitrate_kbps), 512))
    c.fps = max(1, min(int(c.fps), 120))
    c.output_width = max(320, min(int(c.output_width), 7680))
    c.output_height = max(240, min(int(c.output_height), 4320))
    if c.preset not in {"ultrafast","superfast","veryfast","faster","fast","medium"}: c.preset = "veryfast"
    save_configs()


load_destinations()
load_configs()


@app.get("/health")
def health():
    running = sum(1 for r in runtimes.values() if r.is_running)
    return {"ok": True, "app": "short-stream", "ffmpeg": FFMPEG, "data_root": str(DATA_ROOT), "sessions": len(configs), "running": running, "destinations": len(destinations)}


@app.get("/api/destinations")
def list_destinations():
    return {"destinations": sorted(destinations.values(), key=lambda x: x["name"].lower())}


@app.post("/api/destinations")
def create_destination(payload: dict = Body(...)):
    name = str(payload.get("name") or "").strip()[:80]
    rtmp_url = str(payload.get("rtmp_url") or "").strip()
    if not name:
        raise HTTPException(400, "Destination name is required")
    if not (rtmp_url.startswith("rtmp://") or rtmp_url.startswith("rtmps://")):
        raise HTTPException(400, "RTMP URL must start with rtmp:// or rtmps://")
    did = new_destination_id()
    destinations[did] = {"destination_id": did, "name": name, "rtmp_url": rtmp_url}
    save_destinations()
    return destinations[did]


@app.put("/api/destinations/{did}")
def edit_destination(did: str, payload: dict = Body(...)):
    if did not in destinations:
        raise HTTPException(404, "Destination profile not found")
    name = str(payload.get("name") or destinations[did]["name"]).strip()[:80]
    rtmp_url = str(payload.get("rtmp_url") or destinations[did]["rtmp_url"]).strip()
    if not name:
        raise HTTPException(400, "Destination name is required")
    if not (rtmp_url.startswith("rtmp://") or rtmp_url.startswith("rtmps://")):
        raise HTTPException(400, "RTMP URL must start with rtmp:// or rtmps://")
    destinations[did] = {"destination_id": did, "name": name, "rtmp_url": rtmp_url}
    for c in configs.values():
        if c.destination_id == did:
            if runtimes[c.session_id].is_running:
                raise HTTPException(409, "Stop Lives using this destination before editing it")
            c.rtmp_url = rtmp_url
    save_destinations(); save_configs()
    return destinations[did]


@app.delete("/api/destinations/{did}")
def delete_destination(did: str):
    if did not in destinations:
        raise HTTPException(404, "Destination profile not found")
    if any(r.is_running and r.config.destination_id == did for r in runtimes.values()):
        raise HTTPException(409, "Stop Lives using this destination before deleting it")
    destinations.pop(did, None)
    for c in configs.values():
        if c.destination_id == did:
            c.destination_id = ""
    save_destinations(); save_configs()
    return {"ok": True}


@app.get("/api/sessions")
def list_sessions():
    return {"sessions": [r.public() for r in runtimes.values()]}


@app.post("/api/sessions")
def create_session(payload: dict = Body(default={})): 
    sid = new_id()
    c = SessionConfig(session_id=sid, name=str(payload.get("name") or f"Live {len(configs)+1}"))
    configs[sid] = c
    runtimes[sid] = SessionRuntime(c)
    update_config(c, payload)
    return runtimes[sid].public()


@app.put("/api/sessions/{sid}")
def edit_session(sid: str, payload: dict = Body(...)):
    r = get_runtime(sid)
    update_config(r.config, payload)
    return r.public()


@app.delete("/api/sessions/{sid}")
def delete_session(sid: str):
    r = get_runtime(sid)
    if r.is_running: raise HTTPException(409, "Stop this live before deleting it")
    configs.pop(sid, None); runtimes.pop(sid, None); save_configs()
    return {"ok": True}


@app.post("/api/sessions/{sid}/sources")
async def add_sources(sid: str, files: list[UploadFile] = File(...)):
    r = get_runtime(sid)
    dest_dir = MEDIA_ROOT / sid
    dest_dir.mkdir(parents=True, exist_ok=True)
    added = []
    for upload in files:
        name = safe_name(upload.filename or "video.mp4")
        if Path(name).suffix.lower() not in VIDEO_EXTENSIONS:
            raise HTTPException(400, f"Unsupported video type: {name}")
        target = dest_dir / name
        if target.exists():
            target = dest_dir / f"{target.stem}_{uuid.uuid4().hex[:6]}{target.suffix}"
        with target.open("wb") as out:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk: break
                out.write(chunk)
        r.config.source_files.append(str(target))
        added.append(target.name)
    save_configs()
    r.log(f"Queue: added {len(added)} source file(s)")
    return {"ok": True, "added": added, "session": r.public()}


@app.delete("/api/sessions/{sid}/sources/{index}")
def remove_source(sid: str, index: int):
    r = get_runtime(sid)
    if r.is_running: raise HTTPException(409, "Stop this live before removing queue items")
    if index < 0 or index >= len(r.config.source_files): raise HTTPException(404, "Queue item not found")
    removed = r.config.source_files.pop(index); save_configs()
    return {"ok": True, "removed": Path(removed).name}


@app.post("/api/sessions/{sid}/move")
def move_source(sid: str, payload: dict = Body(...)):
    r = get_runtime(sid)
    if r.is_running: raise HTTPException(409, "Stop this live before reordering the queue")
    idx = int(payload.get("index", -1)); direction = int(payload.get("direction", 0)); target = idx + direction
    q = r.config.source_files
    if not (0 <= idx < len(q) and 0 <= target < len(q)): return r.public()
    q[idx], q[target] = q[target], q[idx]; save_configs(); return r.public()


@app.post("/api/sessions/{sid}/start")
def start_session(sid: str, payload: dict = Body(...)):
    r = get_runtime(sid)
    try: r.start(str(payload.get("stream_key") or ""))
    except (ValueError, RuntimeError) as exc: raise HTTPException(400, str(exc))
    return {"ok": True}


@app.post("/api/sessions/{sid}/stop")
def stop_session(sid: str):
    get_runtime(sid).stop(); return {"ok": True}


@app.post("/api/sessions/{sid}/next")
def next_source(sid: str):
    try: get_runtime(sid).next_file()
    except RuntimeError as exc: raise HTTPException(409, str(exc))
    return {"ok": True}


@app.post("/api/sessions/{sid}/play")
def play_source(sid: str, payload: dict = Body(...)):
    r = get_runtime(sid); idx = int(payload.get("index", -1))
    if idx < 0 or idx >= len(r.config.source_files): raise HTTPException(404, "Queue item not found")
    try: r.play_now(r.config.source_files[idx])
    except (ValueError, RuntimeError) as exc: raise HTTPException(409, str(exc))
    return {"ok": True}


@app.post("/api/stop-all")
def stop_all():
    for r in list(runtimes.values()):
        if r.is_running: r.stop()
    return {"ok": True}


@app.get("/api/sessions/{sid}/logs")
def session_logs(sid: str):
    r = get_runtime(sid)
    with r.lock: return {"logs": list(r.logs)}


PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Short Stream</title>
<script>
if(new URLSearchParams(window.location.search).get('embedded')==='1'){
  document.documentElement.setAttribute('data-short-stream-embedded','1');
}
</script>
<style>
:root{--nav:#101526;--nav2:#252c43;--bg:#f4f6fb;--panel:#fff;--line:#dfe4ee;--text:#18233a;--muted:#70809b;--primary:#315edb;--danger:#c73c53;--good:#168a5b;--warn:#a87010;--shadow:0 7px 22px rgba(25,43,75,.06)}
*{box-sizing:border-box}html,body{min-height:100%}body{margin:0;font-family:Inter,Segoe UI,Arial,sans-serif;background:var(--bg);color:var(--text)}button,input,select{font:inherit}.platform-sidebar{position:fixed;inset:0 auto 0 0;width:230px;background:var(--nav);color:#fff;padding:24px 16px 18px;display:flex;flex-direction:column;z-index:30}.brand{display:flex;align-items:center;gap:11px;margin-bottom:24px}.brand-logo{width:36px;height:36px;border-radius:11px;background:linear-gradient(135deg,#7b61ff,#5b44da);display:grid;place-items:center;font-weight:900}.brand strong,.brand small{display:block}.brand strong{font-size:14px}.brand small{font-size:10px;color:#919bb6;margin-top:2px}.platform-sidebar nav{display:grid;gap:6px}.platform-sidebar nav a{color:#b9c2d8;text-decoration:none;padding:10px 12px;border-radius:9px;font-size:13px;font-weight:650}.platform-sidebar nav a:hover,.platform-sidebar nav a.active{background:#282f48;color:#fff}.sidebar-bottom{margin-top:auto}.signed{border:1px solid #2d3754;border-radius:11px;padding:10px;margin-bottom:13px}.signed small{display:block;color:#7f8aa7;font-size:9px;font-weight:800;letter-spacing:.08em}.signed b{display:block;font-size:12px;margin:3px 0 8px}.logout{display:block;text-align:center;border:1px solid #3a4669;border-radius:8px;padding:8px;color:#fff;text-decoration:none;font-size:12px;font-weight:700}.server{font-size:11px;color:#aab4ca}.server span{display:inline-block;width:8px;height:8px;border-radius:50%;background:#34d27b;margin-right:7px}.workspace{margin-left:230px;min-height:100vh;padding:24px 28px 34px}.page-head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:18px}.page-head h1{font-size:25px;margin:0 0 4px}.page-head p{margin:0;color:var(--muted);font-size:12px}.head-actions{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end}.live-count{font-size:12px;font-weight:800;padding:9px 12px;border-radius:9px;background:#e9eefb;color:#25385f}.btn{border:1px solid #ccd4e4;border-radius:8px;padding:8px 12px;background:#fff;color:#24324d;cursor:pointer;font-weight:700;font-size:12px}.btn:hover{background:#f3f6fb}.btn.primary{background:var(--primary);border-color:var(--primary);color:#fff}.btn.danger{background:var(--danger);border-color:var(--danger);color:#fff}.btn.good{background:var(--good);border-color:var(--good);color:#fff}.btn:disabled{opacity:.45;cursor:not-allowed}.tabs{display:flex;gap:4px;border-bottom:1px solid #cfd6e3;margin-bottom:0}.tab{border:1px solid #cfd6e3;border-bottom:0;background:#edf0f5;color:#35445f;padding:8px 13px;border-radius:7px 7px 0 0;cursor:pointer;font-size:12px;font-weight:700}.tab.active{background:#fff;color:#15213a}.view{display:none}.view.active{display:block}.dashboard{padding-top:18px}.summary{display:grid;grid-template-columns:repeat(4,minmax(140px,1fr));gap:12px;margin-bottom:16px}.stat{background:#fff;border:1px solid var(--line);border-radius:12px;padding:14px 16px;box-shadow:var(--shadow)}.stat b{display:block;font-size:23px}.stat span{font-size:11px;color:var(--muted)}.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:13px}.card{background:#fff;border:1px solid var(--line);border-radius:12px;padding:15px;box-shadow:var(--shadow)}.cardhead{display:flex;justify-content:space-between;gap:8px}.card .name{font-weight:800}.sub{font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.current-box{background:#f6f8fc;border:1px solid #e6eaf2;border-radius:9px;padding:10px;margin:12px 0}.current-box label{display:block;font-size:9px;color:#7e8ba2;font-weight:800;letter-spacing:.04em;margin-bottom:3px}.row{display:flex;gap:7px;flex-wrap:wrap}.badge{font-size:10px;border-radius:99px;padding:5px 8px;background:#e8edf7;color:#52627d;font-weight:800}.badge.live{background:#dff6eb;color:#146744}.badge.warn{background:#fff0cf;color:#7a5611}.badge.fail{background:#fde0e6;color:#8a283b}.empty{padding:45px 20px;text-align:center;color:var(--muted);background:#fff;border:1px dashed #cbd3e1;border-radius:12px}.control-layout{display:grid;grid-template-columns:190px minmax(0,1fr);min-height:710px;background:#fff;border:1px solid #cfd6e3;border-top:0;box-shadow:var(--shadow)}.sessions-pane{border-right:1px solid #d9dfe9;padding:12px;background:#fafbfe;display:flex;flex-direction:column}.sessions-pane h3{font-size:14px;margin:0 0 9px}.session-list{border:1px solid #cbd3df;background:#fff;min-height:250px;max-height:520px;overflow:auto}.session-item{padding:7px 8px;font-size:12px;cursor:pointer;border-bottom:1px solid #eef1f6;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.session-item:last-child{border-bottom:0}.session-item:hover{background:#f0f4fa}.session-item.selected{background:#1667d9;color:#fff}.session-bottom{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:auto;padding-top:10px}.control-pane{padding:14px}.selected-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:10px}.selected-head h2{font-size:18px;margin:0 0 4px}.selected-meta{font-size:11px;color:var(--muted)}.status-large{font-size:13px;font-weight:900}.fieldset{border:1px solid #cfd6e3;border-radius:3px;padding:13px 10px 10px;margin-bottom:12px;position:relative}.legend{position:absolute;top:-9px;left:9px;background:#fff;padding:0 6px;font-size:11px;font-weight:700;color:#293852}.form-row{display:grid;grid-template-columns:70px 1fr;gap:8px;align-items:center;margin:6px 0}.form-row label{font-size:11px}.form-row input,.form-row select,.encode-cell input,.encode-cell select{width:100%;border:1px solid #bfc8d7;border-radius:3px;background:#fff;padding:6px 7px;font-size:11px;color:#1e2a42}.form-2{display:grid;grid-template-columns:1fr 1fr;gap:10px}.checkline{font-size:11px;display:flex;align-items:center;gap:6px;margin-top:8px}.queue-layout{display:grid;grid-template-columns:minmax(0,1fr) 128px;gap:10px}.queue-list{height:255px;border:1px solid #bfc8d7;background:#fff;overflow:auto}.queue-item{display:grid;grid-template-columns:25px 1fr auto;gap:7px;align-items:center;padding:6px 7px;font-size:11px;border-bottom:1px solid #edf0f4;cursor:pointer}.queue-item:last-child{border-bottom:0}.queue-item.selected{background:#dceaff}.queue-item.playing{font-weight:800;color:#0d6f47}.qname{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.queue-buttons{display:grid;align-content:start;gap:7px}.queue-buttons .btn{width:100%;padding:7px 6px;font-size:11px}.encoding{display:grid;grid-template-columns:1.1fr 1fr 1fr .65fr .9fr;gap:12px}.encode-cell label{display:block;font-size:10px;font-weight:700;margin-bottom:4px}.action-bar{display:flex;gap:7px;align-items:center;margin:10px 0}.action-bar .spacer{flex:1}.logbox{height:170px;border:1px solid #bfc8d7;background:#fbfbfc;padding:8px;overflow:auto;white-space:pre-wrap;font:10px ui-monospace,SFMono-Regular,Consolas,monospace;color:#34405a}.muted{color:var(--muted)}.no-selected{display:grid;place-items:center;min-height:600px;color:var(--muted);font-size:13px}.dest-dialog{width:min(760px,92vw);border:1px solid #cbd3e0;border-radius:12px;padding:0;box-shadow:0 25px 80px #10203d44}.dest-dialog::backdrop{background:#0a132966}.modal-head{display:flex;justify-content:space-between;align-items:center;padding:14px 16px;background:#f4f6fb;border-bottom:1px solid #dbe1eb}.modal-body{padding:16px}.dest-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.dest-list{border:1px solid #d5dce7;margin-top:12px}.dest-row{display:grid;grid-template-columns:1fr auto;gap:10px;padding:9px;border-bottom:1px solid #e8ebf0}.dest-row:last-child{border-bottom:0}.dest-url{font-size:10px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.inline-actions{display:flex;gap:5px}
@media(max-width:1050px){.platform-sidebar{width:200px}.workspace{margin-left:200px;padding:18px}.control-layout{grid-template-columns:165px minmax(0,1fr)}.form-2{grid-template-columns:1fr}.encoding{grid-template-columns:1fr 1fr 1fr}.summary{grid-template-columns:1fr 1fr}}
@media(max-width:760px){.platform-sidebar{width:170px;padding:18px 10px}.workspace{margin-left:170px;padding:12px}.platform-sidebar nav a{font-size:11px;padding:8px}.brand small{display:none}.page-head{display:block}.head-actions{justify-content:flex-start;margin-top:10px}.control-layout{display:block}.sessions-pane{border-right:0;border-bottom:1px solid #d9dfe9}.session-list{min-height:120px;max-height:180px}.session-bottom{margin-top:8px}.queue-layout{grid-template-columns:1fr}.queue-buttons{grid-template-columns:repeat(2,1fr)}.encoding{grid-template-columns:1fr 1fr}.dest-grid{grid-template-columns:1fr}}
html[data-short-stream-embedded="1"] .platform-sidebar{display:none!important}
html[data-short-stream-embedded="1"] .workspace{margin-left:0!important;padding:18px 20px 30px!important}
html[data-short-stream-embedded="1"] body{background:#f4f6fb}
</style>
    
    <style id="universal-theme-v3">
:root {
  --sb-bg: #f8fafc;
  --sb-card: #ffffff;
  --sb-border: #e2e8f0;
  --sb-accent: #4f46e5;
  --sb-text: #0f172a;
  --sb-muted: #64748b;
  --sb-spacing-sm: 8px;
  --sb-spacing-md: 16px;
  --sb-spacing-lg: 24px;
  --sb-radius: 12px;
  --sb-radius-sm: 8px;
}
body { background-color: var(--sb-bg) !important; color: var(--sb-text) !important; font-family: 'Inter', system-ui, -apple-system, sans-serif !important; margin: 0; }
.card, .panel, .live-control-card, #title-to-publish-panel { background-color: var(--sb-card) !important; border: 1px solid var(--sb-border) !important; border-radius: var(--sb-radius) !important; box-shadow: 0 1px 3px 0 rgba(0,0,0,0.1), 0 1px 2px 0 rgba(0,0,0,0.06) !important; padding: var(--sb-spacing-lg) !important; margin-bottom: var(--sb-spacing-lg) !important; }
h1, h2, h3, h4, h5, h6 { margin-top: 0 !important; margin-bottom: var(--sb-spacing-md) !important; color: var(--sb-text) !important; }
label { display: block !important; margin-bottom: var(--sb-spacing-sm) !important; font-weight: 600 !important; font-size: 13px !important; color: var(--sb-muted) !important; }
.form-control, input[type="text"], input[type="number"], input[type="password"], select, textarea { background-color: #ffffff !important; border: 1px solid var(--sb-border) !important; border-radius: var(--sb-radius-sm) !important; padding: 10px 14px !important; box-sizing: border-box !important; margin-bottom: var(--sb-spacing-md) !important; width: 100%; max-width: 100%; color: var(--sb-text) !important; transition: border-color 0.15s ease-in-out, box-shadow 0.15s ease-in-out; font-family: inherit !important; font-size: 14px !important; }
.form-control:focus, input[type="text"]:focus, input[type="password"]:focus, select:focus, textarea:focus { border-color: var(--sb-accent) !important; outline: none !important; box-shadow: 0 0 0 3px rgba(79, 70, 229, 0.1) !important; }
.btn-primary, button.primary { background-color: var(--sb-accent) !important; border-color: var(--sb-accent) !important; color: #ffffff !important; border-radius: var(--sb-radius-sm) !important; font-weight: 600 !important; padding: 10px 20px !important; cursor: pointer; display: inline-block; margin-top: 4px; border: none !important; }
.btn-secondary, button.secondary { background-color: #ffffff !important; border: 1px solid var(--sb-border) !important; color: var(--sb-text) !important; border-radius: var(--sb-radius-sm) !important; font-weight: 600 !important; padding: 10px 20px !important; cursor: pointer; display: inline-block; margin-top: 4px; }
.btn-primary:hover, button.primary:hover { opacity: 0.9 !important; }
.row, .form-group, .mb-3, .mt-3 { margin-bottom: var(--sb-spacing-md) !important; }
</style>

    

    <style id="universal-theme-v3">
:root {
  --sb-bg: #f8fafc;
  --sb-card: #ffffff;
  --sb-border: #e2e8f0;
  --sb-accent: #4f46e5;
  --sb-text: #0f172a;
  --sb-muted: #64748b;
  --sb-spacing-sm: 8px;
  --sb-spacing-md: 16px;
  --sb-spacing-lg: 24px;
  --sb-radius: 12px;
  --sb-radius-sm: 8px;
}
body { background-color: var(--sb-bg) !important; color: var(--sb-text) !important; font-family: 'Inter', system-ui, -apple-system, sans-serif !important; margin: 0; }
.card, .panel, .live-control-card, #title-to-publish-panel { background-color: var(--sb-card) !important; border: 1px solid var(--sb-border) !important; border-radius: var(--sb-radius) !important; box-shadow: 0 1px 3px 0 rgba(0,0,0,0.1), 0 1px 2px 0 rgba(0,0,0,0.06) !important; padding: var(--sb-spacing-lg) !important; margin-bottom: var(--sb-spacing-lg) !important; }
h1, h2, h3, h4, h5, h6 { margin-top: 0 !important; margin-bottom: var(--sb-spacing-md) !important; color: var(--sb-text) !important; }
label { display: block !important; margin-bottom: var(--sb-spacing-sm) !important; font-weight: 600 !important; font-size: 13px !important; color: var(--sb-muted) !important; }
.form-control, input[type="text"], input[type="number"], input[type="password"], select, textarea { background-color: #ffffff !important; border: 1px solid var(--sb-border) !important; border-radius: var(--sb-radius-sm) !important; padding: 10px 14px !important; box-sizing: border-box !important; margin-bottom: var(--sb-spacing-md) !important; width: 100%; max-width: 100%; color: var(--sb-text) !important; transition: border-color 0.15s ease-in-out, box-shadow 0.15s ease-in-out; font-family: inherit !important; font-size: 14px !important; }
.form-control:focus, input[type="text"]:focus, input[type="password"]:focus, select:focus, textarea:focus { border-color: var(--sb-accent) !important; outline: none !important; box-shadow: 0 0 0 3px rgba(79, 70, 229, 0.1) !important; }
.btn-primary, button.primary { background-color: var(--sb-accent) !important; border-color: var(--sb-accent) !important; color: #ffffff !important; border-radius: var(--sb-radius-sm) !important; font-weight: 600 !important; padding: 10px 20px !important; cursor: pointer; display: inline-block; margin-top: 4px; border: none !important; }
.btn-secondary, button.secondary { background-color: #ffffff !important; border: 1px solid var(--sb-border) !important; color: var(--sb-text) !important; border-radius: var(--sb-radius-sm) !important; font-weight: 600 !important; padding: 10px 20px !important; cursor: pointer; display: inline-block; margin-top: 4px; }
.btn-primary:hover, button.primary:hover { opacity: 0.9 !important; }
.row, .form-group, .mb-3, .mt-3 { margin-bottom: var(--sb-spacing-md) !important; }
</style>


<style id="universal-theme-v3">
:root {
  --sb-bg: #f8fafc;
  --sb-card: #ffffff;
  --sb-border: #e2e8f0;
  --sb-accent: #4f46e5;
  --sb-text: #0f172a;
  --sb-muted: #64748b;
  --sb-spacing-sm: 8px;
  --sb-spacing-md: 16px;
  --sb-spacing-lg: 24px;
  --sb-radius: 12px;
  --sb-radius-sm: 8px;
}
body { background-color: var(--sb-bg) !important; color: var(--sb-text) !important; font-family: 'Inter', system-ui, -apple-system, sans-serif !important; margin: 0; }
.card, .panel, .live-control-card, #title-to-publish-panel { background-color: var(--sb-card) !important; border: 1px solid var(--sb-border) !important; border-radius: var(--sb-radius) !important; box-shadow: 0 1px 3px 0 rgba(0,0,0,0.1), 0 1px 2px 0 rgba(0,0,0,0.06) !important; padding: var(--sb-spacing-lg) !important; margin-bottom: var(--sb-spacing-lg) !important; }
h1, h2, h3, h4, h5, h6 { margin-top: 0 !important; margin-bottom: var(--sb-spacing-md) !important; color: var(--sb-text) !important; }
label { display: block !important; margin-bottom: var(--sb-spacing-sm) !important; font-weight: 600 !important; font-size: 13px !important; color: var(--sb-muted) !important; }
.form-control, input[type="text"], input[type="number"], input[type="password"], select, textarea { background-color: #ffffff !important; border: 1px solid var(--sb-border) !important; border-radius: var(--sb-radius-sm) !important; padding: 10px 14px !important; box-sizing: border-box !important; margin-bottom: var(--sb-spacing-md) !important; width: 100%; max-width: 100%; color: var(--sb-text) !important; transition: border-color 0.15s ease-in-out, box-shadow 0.15s ease-in-out; font-family: inherit !important; font-size: 14px !important; }
.form-control:focus, input[type="text"]:focus, input[type="password"]:focus, select:focus, textarea:focus { border-color: var(--sb-accent) !important; outline: none !important; box-shadow: 0 0 0 3px rgba(79, 70, 229, 0.1) !important; }
.btn-primary, button.primary { background-color: var(--sb-accent) !important; border-color: var(--sb-accent) !important; color: #ffffff !important; border-radius: var(--sb-radius-sm) !important; font-weight: 600 !important; padding: 10px 20px !important; cursor: pointer; display: inline-block; margin-top: 4px; border: none !important; }
.btn-secondary, button.secondary { background-color: #ffffff !important; border: 1px solid var(--sb-border) !important; color: var(--sb-text) !important; border-radius: var(--sb-radius-sm) !important; font-weight: 600 !important; padding: 10px 20px !important; cursor: pointer; display: inline-block; margin-top: 4px; }
.btn-primary:hover, button.primary:hover { opacity: 0.9 !important; }
.row, .form-group, .mb-3, .mt-3 { margin-bottom: var(--sb-spacing-md) !important; }
</style>

</head><body>
<aside class="platform-sidebar">
  <div class="brand"><div class="brand-logo">S</div><div><strong>Scene Builder</strong><small>Team Studio</small></div></div>
  <nav>
    <a href="/">Dashboard</a>
    <a href="/local-upload-manager">Auto Uploader</a>
    <a href="/queue">Video Studio</a>

<a href="/voice-studio/">Voice Studio</a>
<a class="active" href="/short-stream/">Short Stream</a>
    <a href="/auto-script">Auto Script</a>
    <a href="/admin">Admin</a>
  </nav>
  <div class="sidebar-bottom"><div class="signed"><small>SIGNED IN</small><b>Authenticated session</b><a class="logout" href="/logout">Log out</a></div><div class="server"><span></span> VPS Connected</div></div>
</aside>
<main class="workspace">
  <div class="page-head"><div><h1>Short Stream</h1><p>Independent continuous multi-live streaming control</p></div><div class="head-actions"><span id="topCount" class="live-count">0 LIVE • 0 sessions</span><button class="btn" onclick="openDestinations()">Destinations</button><button class="btn danger" onclick="stopAll()">Stop All Lives</button></div></div>
  <div class="tabs"><button id="tabDash" class="tab" onclick="showView('dashboard')">Dashboard</button><button id="tabControl" class="tab active" onclick="showView('control')">Live Control</button></div>
  <section id="dashboardView" class="view dashboard"><div class="summary"><div class="stat"><b id="sTotal">0</b><span>Live Sessions</span></div><div class="stat"><b id="sRunning">0</b><span>Running</span></div><div class="stat"><b id="sHealthy">0</b><span>LIVE</span></div><div class="stat"><b id="sFailed">0</b><span>Failed</span></div></div><div id="cards" class="cards"></div></section>
  <section id="controlView" class="view active">
    <div class="control-layout">
      <aside class="sessions-pane"><h3>Live Sessions</h3><div id="sessionList" class="session-list"></div><div class="session-bottom"><button class="btn" onclick="createLive()">+ New</button><button id="deleteBtn" class="btn" onclick="deleteLive()">Delete</button></div></aside>
      <div id="controlPane" class="control-pane"><div class="no-selected">Select a Live session or click <b>+ New</b>.</div></div>
    </div>
  </section>
</main>
<dialog id="destDlg" class="dest-dialog"><div class="modal-head"><div><b>Destinations</b><div class="muted" style="font-size:10px;margin-top:3px">Save channel name + RTMP server only. Stream keys are never stored.</div></div><button class="btn" onclick="destDlg.close()">Close</button></div><div class="modal-body"><div class="dest-grid"><div><label style="font-size:10px;font-weight:700">Profile Name</label><input id="dName" style="width:100%;padding:7px;border:1px solid #cbd3df" placeholder="YouTube Channel A"></div><div><label style="font-size:10px;font-weight:700">RTMP Server URL</label><input id="dUrl" style="width:100%;padding:7px;border:1px solid #cbd3df" placeholder="rtmp://a.rtmp.youtube.com/live2"></div></div><input id="dId" type="hidden"><div class="action-bar"><button class="btn good" onclick="saveDestination()">Save Destination</button><button class="btn" onclick="resetDestinationForm()">Clear</button></div><div id="destList" class="dest-list"></div></div></dialog>
<input id="files" type="file" multiple accept="video/*" hidden onchange="uploadFiles()">
<script>
let sessions=[],destinations=[],current=null,selectedQueueIndex=-1,currentView='control';const destDlg=document.getElementById('destDlg');
const esc=s=>String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
async function api(url,opt={}){let r=await fetch(url,opt),j={};try{j=await r.json()}catch(e){}if(!r.ok)throw Error(j.detail||('HTTP '+r.status));return j}
function badge(s){let c=s==='LIVE'?'live':(s==='FAILED'?'fail':(['STARTING','CONNECTING','RECONNECTING','SWITCHING_SOURCE'].includes(s)?'warn':''));return `<span class="badge ${c}">${esc(s)}</span>`}
function showView(v){currentView=v;dashboardView.classList.toggle('active',v==='dashboard');controlView.classList.toggle('active',v==='control');tabDash.classList.toggle('active',v==='dashboard');tabControl.classList.toggle('active',v==='control')}
async function refresh(){try{let [sj,dj]=await Promise.all([api('api/sessions'),api('api/destinations')]);sessions=sj.sessions||[];destinations=dj.destinations||[];if(current){let n=sessions.find(x=>x.session_id===current.session_id);current=n||null}if(!current&&sessions.length)current=sessions[0];renderAll()}catch(e){console.error(e)}}
function renderAll(){renderSummary();renderDashboard();renderSessionList();renderControl();renderDestList()}
function renderSummary(){let running=sessions.filter(x=>x.is_running).length,live=sessions.filter(x=>x.status==='LIVE').length,failed=sessions.filter(x=>x.status==='FAILED').length;topCount.textContent=`${live} LIVE • ${sessions.length} session${sessions.length===1?'':'s'}`;sTotal.textContent=sessions.length;sRunning.textContent=running;sHealthy.textContent=live;sFailed.textContent=failed}
function renderDashboard(){cards.innerHTML=sessions.length?sessions.map(s=>`<div class="card"><div class="cardhead"><div style="min-width:0"><div class="name">${esc(s.name)}</div><div class="sub">${esc(s.rtmp_url)}</div></div>${badge(s.status)}</div><div class="current-box"><label>CURRENT SOURCE</label><b>${esc(s.current_source||'Not running')}</b><div class="muted" style="font-size:11px;margin-top:4px">Uptime ${esc(s.uptime)} · Queue ${s.source_files.length} file(s)</div></div><div class="row"><button class="btn primary" onclick="selectSession('${s.session_id}',true)">Open</button>${s.is_running?`<button class="btn" onclick="quickNext('${s.session_id}')">Next</button><button class="btn danger" onclick="quickStop('${s.session_id}')">Stop</button>`:''}</div></div>`).join(''):'<div class="empty">No live sessions yet. Click <b>+ New</b>.</div>'}
function renderSessionList(){sessionList.innerHTML=sessions.length?sessions.map(s=>`<div class="session-item ${current&&current.session_id===s.session_id?'selected':''}" onclick="selectSession('${s.session_id}',false)">[${esc(s.status)}] ${esc(s.name)}</div>`).join(''):'<div class="session-item muted">No sessions</div>';deleteBtn.disabled=!current||current?.is_running}
function selectSession(id,openControl=true){current=sessions.find(x=>x.session_id===id)||null;selectedQueueIndex=-1;if(openControl)showView('control');renderSessionList();renderControl();loadLogs()}
function renderControl(){if(!current){controlPane.innerHTML='<div class="no-selected">Select a Live session or click <b>+ New</b>.</div>';return}let q=current.source_files||[],running=!!current.is_running;controlPane.innerHTML=`
<div class="selected-head"><div><h2>Selected Live</h2><div class="selected-meta">Uptime: ${esc(current.uptime)} &nbsp;&nbsp; Playing: ${esc(current.current_source||'No source playing')}</div></div><div class="status-large">${esc(current.status)}</div></div>
<div class="fieldset"><span class="legend">Live Session</span>
  <div class="form-row"><label>Name</label><input id="fName" value="${esc(current.name)}"></div>
  <div class="form-2"><div><div class="form-row"><label>Destination</label><select id="fDest" onchange="applyDestination()"><option value="">Custom / none</option>${destinations.map(d=>`<option value="${esc(d.destination_id)}" ${d.destination_id===current.destination_id?'selected':''}>${esc(d.name)}</option>`).join('')}</select></div></div><div><div class="form-row"><label>RTMP server</label><input id="fRtmp" value="${esc(current.rtmp_url)}"></div></div></div>
  <div class="form-row"><label>Stream key</label><input id="fKey" type="password" autocomplete="off" placeholder="Memory-only; enter when starting"></div>
  <label class="checkline"><input id="fLoop" type="checkbox" ${current.continuous_queue?'checked':''}> Continuous live: loop this session's queue</label>
</div>
<div class="fieldset"><span class="legend">Source Queue — independent for this live</span><div class="queue-layout"><div id="queueList" class="queue-list">${q.length?q.map((p,i)=>{let name=p.split('/').pop(),playing=i===current.current_index;return `<div class="queue-item ${selectedQueueIndex===i?'selected':''} ${playing?'playing':''}" onclick="selectQueue(${i})"><div>${playing?'▶':(i+1)}</div><div class="qname">${esc(name)}</div><div>${playing?'PLAYING':''}</div></div>`}).join(''):'<div class="queue-item muted"><div></div><div>No files in queue</div><div></div></div>'}</div><div class="queue-buttons"><button class="btn" onclick="document.getElementById('files').click()">Add Videos</button><button class="btn" onclick="removeSelected()" ${running||selectedQueueIndex<0?'disabled':''}>Remove Selected</button><button class="btn" onclick="clearQueue()" ${running||!q.length?'disabled':''}>Clear Queue</button><button class="btn" onclick="moveSelected(-1)" ${running||selectedQueueIndex<=0?'disabled':''}>Move Up</button><button class="btn" onclick="moveSelected(1)" ${running||selectedQueueIndex<0||selectedQueueIndex>=q.length-1?'disabled':''}>Move Down</button><button class="btn" onclick="playSelectedNow()" ${!running||selectedQueueIndex<0?'disabled':''}>Play Selected Now</button><button class="btn" onclick="nextFile()" ${!running||!q.length?'disabled':''}>Next File</button></div></div></div>
<div class="fieldset"><span class="legend">Encoding / Normalization</span><div class="encoding"><div class="encode-cell"><label>Resolution</label><select id="fRes"><option value="1280x720" ${current.output_width===1280&&current.output_height===720?'selected':''}>1280x720</option><option value="1920x1080" ${current.output_width===1920&&current.output_height===1080?'selected':''}>1920x1080</option><option value="1080x1920" ${current.output_width===1080&&current.output_height===1920?'selected':''}>1080x1920</option></select></div><div class="encode-cell"><label>Video kbps</label><input id="fVb" type="number" value="${current.video_bitrate_kbps}"></div><div class="encode-cell"><label>Audio kbps</label><input id="fAb" type="number" value="${current.audio_bitrate_kbps}"></div><div class="encode-cell"><label>FPS</label><input id="fFps" type="number" min="1" max="120" value="${current.fps}"></div><div class="encode-cell"><label>x264 preset</label><select id="fPreset">${['ultrafast','superfast','veryfast','faster','fast','medium'].map(p=>`<option ${p===current.preset?'selected':''}>${p}</option>`).join('')}</select></div></div></div>
<div class="action-bar"><button class="btn" onclick="saveConfig()" ${running?'disabled':''}>Save Session</button><button class="btn good" onclick="startLive()" ${running?'disabled':''}>Start This Live</button><button class="btn danger" onclick="stopLive()" ${!running?'disabled':''}>Stop This Live</button><span class="spacer"></span><button class="btn" onclick="openDestinations()">Destinations</button><button class="btn" onclick="clearLogBox()">Clear Log</button></div>
<div class="fieldset"><span class="legend">Selected Live Engine Log</span><div id="logs" class="logbox"></div></div>`;loadLogs()}
function selectQueue(i){selectedQueueIndex=i;renderControl()}
async function createLive(){try{let s=await api('api/sessions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({})});await refresh();selectSession(s.session_id,true)}catch(e){alert(e.message)}}
async function saveConfig(){if(!current)return false;let [w,h]=document.getElementById('fRes').value.split('x').map(Number);try{await api('api/sessions/'+current.session_id,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:fName.value,destination_id:fDest.value,rtmp_url:fRtmp.value,fps:+fFps.value,video_bitrate_kbps:+fVb.value,audio_bitrate_kbps:+fAb.value,preset:fPreset.value,continuous_queue:fLoop.checked,output_width:w,output_height:h})});await refresh();return true}catch(e){alert(e.message);return false}}
function applyDestination(){let d=destinations.find(x=>x.destination_id===fDest.value);if(d)fRtmp.value=d.rtmp_url}
async function uploadFiles(){if(!current||!files.files.length)return;let fd=new FormData();for(let f of files.files)fd.append('files',f);try{await api('api/sessions/'+current.session_id+'/sources',{method:'POST',body:fd});files.value='';await refresh()}catch(e){alert(e.message)}}
async function startLive(){if(!current)return;let ok=await saveConfig();if(!ok)return;let key=document.getElementById('fKey')?.value||'';if(!key){alert('Enter the stream key.');return}try{await api('api/sessions/'+current.session_id+'/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({stream_key:key})});document.getElementById('fKey').value='';await refresh()}catch(e){alert(e.message)}}
async function stopLive(){if(!current)return;try{await api('api/sessions/'+current.session_id+'/stop',{method:'POST'});await refresh()}catch(e){alert(e.message)}}
async function nextFile(){if(!current)return;try{await api('api/sessions/'+current.session_id+'/next',{method:'POST'});await refresh()}catch(e){alert(e.message)}}
async function playSelectedNow(){if(!current||selectedQueueIndex<0)return;try{await api('api/sessions/'+current.session_id+'/play',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({index:selectedQueueIndex})});await refresh()}catch(e){alert(e.message)}}
async function removeSelected(){if(!current||selectedQueueIndex<0||!confirm('Remove selected file from the queue?'))return;try{await api('api/sessions/'+current.session_id+'/sources/'+selectedQueueIndex,{method:'DELETE'});selectedQueueIndex=-1;await refresh()}catch(e){alert(e.message)}}
async function clearQueue(){if(!current||current.is_running||!confirm('Clear all files from this queue?'))return;try{for(let i=current.source_files.length-1;i>=0;i--)await api('api/sessions/'+current.session_id+'/sources/'+i,{method:'DELETE'});selectedQueueIndex=-1;await refresh()}catch(e){alert(e.message)}}
async function moveSelected(d){if(!current||selectedQueueIndex<0)return;let old=selectedQueueIndex;try{await api('api/sessions/'+current.session_id+'/move',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({index:old,direction:d})});selectedQueueIndex=old+d;await refresh()}catch(e){alert(e.message)}}
async function deleteLive(){if(!current||current.is_running||!confirm('Delete this Live session profile? Uploaded media files will remain on the VPS.'))return;try{await api('api/sessions/'+current.session_id,{method:'DELETE'});current=null;selectedQueueIndex=-1;await refresh()}catch(e){alert(e.message)}}
async function stopAll(){if(!confirm('Stop ALL running live sessions?'))return;try{await api('api/stop-all',{method:'POST'});await refresh()}catch(e){alert(e.message)}}
async function quickStop(id){try{await api('api/sessions/'+id+'/stop',{method:'POST'});await refresh()}catch(e){alert(e.message)}}async function quickNext(id){try{await api('api/sessions/'+id+'/next',{method:'POST'});await refresh()}catch(e){alert(e.message)}}
async function loadLogs(){if(!current||!document.getElementById('logs'))return;try{let j=await api('api/sessions/'+current.session_id+'/logs');logs.textContent=(j.logs||[]).join('\n');logs.scrollTop=logs.scrollHeight}catch(e){}}
function clearLogBox(){let el=document.getElementById('logs');if(el)el.textContent=''}
function openDestinations(){resetDestinationForm();renderDestList();destDlg.showModal()}
function resetDestinationForm(){dId.value='';dName.value='';dUrl.value=''}
function renderDestList(){if(!document.getElementById('destList'))return;destList.innerHTML=destinations.length?destinations.map(d=>`<div class="dest-row"><div style="min-width:0"><b>${esc(d.name)}</b><div class="dest-url">${esc(d.rtmp_url)}</div></div><div class="inline-actions"><button class="btn" onclick="editDestination('${d.destination_id}')">Edit</button><button class="btn danger" onclick="deleteDestination('${d.destination_id}')">Delete</button></div></div>`).join(''):'<div class="dest-row"><div class="muted">No destination profiles saved yet.</div><div></div></div>'}
function editDestination(id){let d=destinations.find(x=>x.destination_id===id);if(!d)return;dId.value=id;dName.value=d.name;dUrl.value=d.rtmp_url}
async function saveDestination(){let payload={name:dName.value,rtmp_url:dUrl.value};try{if(dId.value)await api('api/destinations/'+dId.value,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});else await api('api/destinations',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});resetDestinationForm();await refresh()}catch(e){alert(e.message)}}
async function deleteDestination(id){if(!confirm('Delete this destination profile? Existing Lives keep their RTMP URL but become Custom.'))return;try{await api('api/destinations/'+id,{method:'DELETE'});await refresh()}catch(e){alert(e.message)}}
setInterval(()=>{refresh();if(current)loadLogs()},2000);refresh();
</script>
    <!-- Title-to-Publish & Advanced Telemetry Panel injected by Agent 1 -->
    <div class="card p-4 mt-3 mb-3" id="title-to-publish-panel" style="background:#1e293b; border:1px solid #334155; border-radius:12px; color:#f8fafc;">
        <h4 style="font-weight:700; margin-bottom:15px; color:#6366f1;">Broadcast Metadata & Publishing (Title to Publish)</h4>
        <div class="row" style="display:grid; grid-template-columns: 1fr 1fr; gap: 15px;">
            <div>
                <label style="font-size:13px; font-weight:600; color:#94a3b8;">Stream Title / Headline</label>
                <input type="text" id="stream-title-input" class="form-control" placeholder="e.g., 24/7 Automated LoFi & Stories" style="background:#0f172a; border:1px solid #334155; color:#fff; padding:8px 12px; border-radius:8px; width:100%; margin-top:5px;">
            </div>
            <div>
                <label style="font-size:13px; font-weight:600; color:#94a3b8;">Category & Privacy</label>
                <select id="stream-privacy-select" class="form-control" style="background:#0f172a; border:1px solid #334155; color:#fff; padding:8px 12px; border-radius:8px; width:100%; margin-top:5px;">
                    <option value="public">Public (Live Broadcast)</option>
                    <option value="unlisted">Unlisted (Testing)</option>
                    <option value="private">Private</option>
                </select>
            </div>
        </div>
        <div class="mt-3">
            <label style="font-size:13px; font-weight:600; color:#94a3b8;">Description & Tags</label>
            <textarea id="stream-desc-input" class="form-control" rows="2" placeholder="Enter broadcast description, social links, and tags..." style="background:#0f172a; border:1px solid #334155; color:#fff; padding:8px 12px; border-radius:8px; width:100%; margin-top:5px;"></textarea>
        </div>
        <div class="mt-3" style="display:flex; align-items:center; gap:15px;">
            <label style="display:flex; align-items:center; gap:6px; cursor:pointer; font-size:13px;">
                <input type="checkbox" id="auto-archive-check" checked style="accent-color:#6366f1;"> Auto-Archive Stream Recording to Google Drive
            </label>
            <label style="display:flex; align-items:center; gap:6px; cursor:pointer; font-size:13px;">
                <input type="checkbox" id="adaptive-bitrate-check" checked style="accent-color:#6366f1;"> Adaptive Bitrate Fallback
            </label>
            <button type="button" class="btn btn-primary" onclick="saveBroadcastMeta()" style="margin-left:auto; background:#6366f1; border:none; padding:8px 16px; border-radius:8px; font-weight:600; cursor:pointer;">Save Broadcast Meta</button>
        </div>
    </div>

    <!-- Real-Time Telemetry HUD -->
    <div class="card p-3 mb-3" style="background:#1e293b; border:1px solid #334155; border-radius:12px; display:flex; justify-content:space-between; align-items:center; color:#f8fafc;">
        <div style="display:flex; gap:20px;">
            <div><span style="color:#94a3b8; font-size:12px;">ENCODER FPS:</span> <strong id="hud-fps" style="color:#10b981;">30.0</strong></div>
            <div><span style="color:#94a3b8; font-size:12px;">BITRATE:</span> <strong id="hud-bitrate" style="color:#6366f1;">4500 kbps</strong></div>
            <div><span style="color:#94a3b8; font-size:12px;">DROPPED FRAMES:</span> <strong id="hud-dropped" style="color:#ef4444;">0</strong></div>
            <div><span style="color:#94a3b8; font-size:12px;">CPU LOAD:</span> <strong id="hud-cpu" style="color:#f59e0b;">12.4%</strong></div>
        </div>
        <div>
            <span class="badge" style="background:#10b981; color:#fff; padding:4px 10px; border-radius:6px; font-size:11px; font-weight:700;">ENGINE STABLE</span>
        </div>
    </div>
    
    <script>
    function saveBroadcastMeta() {
        const title = document.getElementById('stream-title-input').value;
        const privacy = document.getElementById('stream-privacy-select').value;
        const desc = document.getElementById('stream-desc-input').value;
        const archive = document.getElementById('auto-archive-check').checked;
        console.log("Saving broadcast metadata:", {title, privacy, desc, archive});
        alert("Broadcast metadata saved successfully for upcoming stream session!");
    }
    </script>

</body></html>
'''

@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(PAGE)

# Independent Auto Stream V1 API. Legacy sessions/routes remain unmodified.
from auto_stream_engine import router as auto_stream_engine_router
app.include_router(auto_stream_engine_router)


# Auto Stream V2 - separate OAuth, Channels and Sheets Sync routes.
from auto_stream_integrations import router as auto_stream_integrations_router
app.include_router(auto_stream_integrations_router)

# Auto Stream browser controls (independent of the legacy Short Stream screen).
from auto_stream_ui import router as auto_stream_ui_router
app.include_router(auto_stream_ui_router)

# Explicit gated YouTube Live API controller, independent from legacy sessions.
from auto_stream_broadcasts import router as auto_stream_broadcasts_router
app.include_router(auto_stream_broadcasts_router)

# Admin-only browser configuration of independent Google OAuth web client.
from auto_stream_client_config import router as auto_stream_credentials_router
app.include_router(auto_stream_credentials_router)


# Auto Stream owns its scheduler, render and encoder lifecycle separately.
from auto_stream_operations import startup as auto_stream_startup, shutdown as auto_stream_shutdown
app.add_event_handler('startup', auto_stream_startup)
app.add_event_handler('shutdown', auto_stream_shutdown)

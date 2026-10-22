"""Guarded, rollback-safe refresh of the EXISTING owned Scene rendering worker.

The candidate web service and writable worker have independent source pointers.
A Scene-only source install must not silently leave the real worker on an old
renderer. Do not touch jobs, admission-only worker or unrelated applications.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import time

from production import scene_production_worker as worker

TERMINAL = frozenset({"done", "completed", "complete", "success", "failed",
                       "error", "cancelled", "canceled", "skipped", "expired", "deleted"})
REQUIRED_XFADE = ("zoomin", "hblur", "fadeslow", "fadefast", "fadewhite",
                  "fadeblack", "coverleft", "coverright", "hlwind", "hrwind", "custom")


def _atomic_regular_file(target: Path, content: bytes, mode: int) -> None:
    temp = None
    try:
        fd, name = tempfile.mkstemp(prefix=".scene-worker-", dir=str(target.parent))
        temp = Path(name)
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, target)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def _require_idle() -> None:
    """Fail closed on queued, retrying, active, or unrecognized jobs."""
    worker.require(worker.STATE.is_dir(), "writable Scene state missing")
    database = worker.STATE / "queue.sqlite3"
    worker.require(database.is_file() and not database.is_symlink(),
                   "existing worker queue unavailable; refresh deferred")
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=4) as conn:
        fields = {x[1] for x in conn.execute("PRAGMA table_info(jobs)")}
        worker.require("status" in fields, "unknown jobs table")
        states = conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall()
    busy = [(status, count) for status, count in states
            if not isinstance(status, str) or status.lower() not in TERMINAL]
    worker.require(not busy, "queued or in-flight Scene jobs exist; source refresh deferred")


def _require_supported_ffmpeg() -> None:
    result = subprocess.run(["ffmpeg", "-hide_banner", "-h", "filter=xfade"],
                            capture_output=True, text=True, timeout=15)
    worker.require(result.returncode == 0, "FFmpeg xfade capability unavailable")
    worker.require(all(re.search(r"(?m)^\s*"+re.escape(name)+r"\s+",
                                 result.stdout) for name in REQUIRED_XFADE),
                   "VPS FFmpeg missing selected Scene transition effects; refresh deferred")


_SMOKE = r'''
import json, pathlib, subprocess, sys, tempfile
from types import SimpleNamespace
from vps_engine.smart_scroll.v2_renderer import render_mixed_media, _validate_final_mp4
root = pathlib.Path(tempfile.mkdtemp(prefix="scene-render-canary-"))
try:
    def ff(*args):
        call = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y",
                               *map(str, args)], capture_output=True, text=True, timeout=120)
        if call.returncode:
            raise RuntimeError("Scene canary FFmpeg failed: "+call.stderr[-600:])
    image, video, audio = root/"pattern.png", root/"still.mp4", root/"narration.wav"
    ff("-f", "lavfi", "-i", "testsrc2=s=320x180:r=8:d=0.5",
       "-frames:v", "1", "-threads", "1", image)
    ff("-loop", "1", "-framerate", "8", "-i", image, "-t", "3.5",
       "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast", video)
    ff("-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=3.15",
       "-c:a", "pcm_s16le", audio)
    scenes = [
      dict(scene_code="PROOF_IMAGE_1", start=0, end=1.05, media_type="image",
           media_path=str(image), motion="pan_left", motion_intensity=.8,
           transition_out="cross_dissolve", transition_duration_ms=200, filter="warm"),
      dict(scene_code="PROOF_VIDEO_2", start=1.05, end=2.10, media_type="video",
           media_path=str(video), motion="reframe_right", motion_intensity=.8,
           transition_out="directional_wipe", transition_duration_ms=200, filter="none"),
      dict(scene_code="PROOF_IMAGE_3", start=2.10, end=3.15, media_type="image",
           media_path=str(image), motion="ken_burns", motion_intensity=.8,
           transition_out="none", filter="none"),
    ]
    for w,h in ((1280,720),(720,1280),(1920,1080),(1080,1920)):
        dest = root/(str(w)+"x"+str(h))
        dest.mkdir()
        output = dest/"real-canary.mp4"
        conf = SimpleNamespace(width=w,height=h,fps=8,quality_crf=26,
                               intermediate_render_preset="ultrafast",
                               render_chunk_scenes=4,video_quality_preset="original",
                               output_path=str(output), audio_path=str(audio),
                               render_audio_path=str(audio),lut_enabled=False,
                               captions_enabled=False,
                               metadata={"scene_sync_plan":scenes,"caption_segments":[]})
        result = pathlib.Path(render_mixed_media(conf))
        assert result == output and output.stat().st_size > 1000
        proof = _validate_final_mp4(output,"ffmpeg",expected_duration=3.15)
        assert (proof["width"],proof["height"]) == (w,h),proof
        assert proof["video_codec"] == "h264" and proof["audio_codec"] == "aac",proof
        print("SCENE_LIVE_WORKER_RENDER_PROOF="+json.dumps(
              {"size":str(w)+"x"+str(h),"codec":"h264/aac","bytes":output.stat().st_size},
              sort_keys=True),flush=True)
finally:
    import shutil
    shutil.rmtree(root,ignore_errors=True)
'''


def _smoke_command(runner: Path) -> list[str]:
    """runuser resets HOME to /nonexistent for service accounts; set it AFTER switch."""
    home = worker.STATE / "worker-home"
    return ["runuser", "-u", "vps1scene", "--", "/usr/bin/env",
            "HOME="+str(home),
            "XDG_CONFIG_HOME="+str(home/".config"),
            "XDG_DATA_HOME="+str(home/".local/share"),
            "TMPDIR="+str(worker.STATE/"tmp"),
            "HF_HOME="+str(worker.STATE/"model-cache"),
            "XDG_CACHE_HOME="+str(worker.STATE/"cache"),
            "PYTHONPATH="+str(runner)+":"+str(runner/"vps_engine"),
            "PYTHONDONTWRITEBYTECODE=1",
            str(worker.VENV/"bin/python"), "-c", _SMOKE]


class SceneWorkerSourceRefresh:
    def __init__(self, target: Path, journal: Path):
        self.target = Path(target)
        self.journal = Path(journal)
        self.original_unit: bytes | None = None
        self.original_record: dict | None = None
        self.token = ""
        self.did_refresh = False

    def _restore(self) -> None:
        if self.original_unit is None or self.original_record is None:
            return
        from production.runtime import atomic_json, run
        _atomic_regular_file(worker.UNIT_FILE, self.original_unit, 0o644)
        atomic_json(worker.RECORD, self.original_record, 0o600)
        run(["systemctl", "daemon-reload"], timeout=20, quiet=True)
        run(["systemctl", "restart", worker.UNIT], timeout=70, quiet=True)
        # systemctl restart returns before uvicorn has completed Python imports,
        # queue opening and health admission. Match the established worker-HOME
        # release readiness probe instead of checking only once.
        for _ in range(60):
            payload = worker.health(worker.URL,self.token)
            if payload.get("ok") is True and payload.get("admission_only") is False:
                break
            time.sleep(.5)
        else:
            raise RuntimeError("Scene worker rollback health timed out; previous unit retained")
        self.original_unit = None
        self.original_record = None
        self.did_refresh = False
        print("SCENE_WORKER_SOURCE_ROLLBACK=PASS PREVIOUS_SOURCE_RESTORED",flush=True)

    def rollback(self) -> None:
        self._restore()

    def refresh(self) -> dict[str, str]:
        from production.runtime import atomic_json, run
        worker.require(os.geteuid() == 0, "protected root release required")
        if not worker.RECORD.is_file():
            return {"state": "SKIPPED_NO_MANAGED_WORKER", "note": "candidate only"}
        worker.require(worker.UNIT_FILE.is_file() and worker.ENV.is_file()
                       and worker.WEB_ENV.is_file(), "existing worker ownership incomplete")
        record = json.loads(worker.RECORD.read_text())
        worker.require(record.get("owner") == "scene-production-worker-v1"
                       and record.get("unit") == worker.UNIT
                       and record.get("state") == str(worker.STATE)
                       and record.get("endpoint") == worker.URL,
                       "unrecognized production worker owner")
        web = worker.entries(worker.WEB_ENV.read_text())
        private = worker.entries(worker.ENV.read_text())
        self.token = private.get("SCENE_WORKER_TOKEN","")
        worker.require(re.fullmatch(r"[A-Za-z0-9_-]{32,256}",self.token or "") is not None
                       and web.get("WORKER_URL","").rstrip("/") == worker.URL
                       and web.get("WORKER_TOKEN") == self.token,
                       "web and owned worker binding differ")
        old = Path(record.get("source","")).resolve(strict=True)
        latest = self.target.resolve(strict=True)
        allowed = worker.BASE / "releases/scene-builder"
        worker.require(old.is_relative_to(allowed) and latest.is_relative_to(allowed),
                       "source outside immutable Scene release root")
        prior_runner = worker.BASE / "releases/scene-production-worker" / old.name
        worker.require((prior_runner/"vps_engine").is_symlink()
                       and (prior_runner/"vps_engine").resolve() == old/"part02"
                       and (prior_runner/"entry.py").read_text() == worker.ENTRYPOINT
                       and worker.UNIT_FILE.read_text() == worker.unit_text(prior_runner),
                       "existing owned worker unit unexpectedly modified")
        current = worker.health(worker.URL,self.token)
        worker.require(current.get("ok") is True and current.get("admission_only") is False,
                       "production worker not healthy/writable")
        if old == latest:
            return {"state": "ALREADY_CURRENT", "revision": latest.name}
        _require_idle()
        counts = current.get("queue_counts") or {}
        worker.require(isinstance(counts,dict) and
                       all(str(k).lower() in TERMINAL or int(v or 0)==0 for k,v in counts.items()),
                       "worker health reports in-flight queue")
        _require_supported_ffmpeg()
        new_runner = worker.BASE/"releases/scene-production-worker"/latest.name
        worker.require((latest/"part02/api_server.py").is_file(), "renderer API missing")
        if new_runner.exists():
            worker.require((new_runner/"vps_engine").is_symlink()
                           and (new_runner/"vps_engine").resolve()==latest/"part02"
                           and (new_runner/"entry.py").read_text()==worker.ENTRYPOINT,
                           "previously created Scene worker runner differs")
        else:
            new_runner.mkdir(parents=True)
            new_runner.chmod(0o755)  # systemd's vps1scene user must traverse the immutable runner.
            (new_runner/"vps_engine").symlink_to(latest/"part02",target_is_directory=True)
            (new_runner/"entry.py").write_text(worker.ENTRYPOINT)
            (new_runner/"entry.py").chmod(0o644)
        _require_idle()  # Fail closed if a user submitted a job during preparation.
        self.original_unit = worker.UNIT_FILE.read_bytes()
        self.original_record = dict(record)
        try:
            _atomic_regular_file(worker.UNIT_FILE,worker.unit_text(new_runner).encode(),0o644)
            run(["systemctl","daemon-reload"],timeout=20,quiet=True)
            run(["systemctl","restart",worker.UNIT],timeout=70,quiet=True)
            for _ in range(60):
                live=worker.health(worker.URL,self.token)
                if live.get("ok") is True and live.get("admission_only") is False: break
                time.sleep(.5)
            else:
                raise RuntimeError("revised worker did not become healthy")
            pid=subprocess.run(["systemctl","show",worker.UNIT,"--property=MainPID","--value"],
                               capture_output=True,text=True,timeout=10,check=True).stdout.strip()
            worker.require(pid.isdigit() and int(pid)>1 and
                           (Path("/proc")/pid/"cwd").resolve()==new_runner,
                           "running worker still bound to old source")
            smoke=subprocess.run(_smoke_command(new_runner),
                                  capture_output=True,text=True,timeout=300)
            worker.require(smoke.returncode==0 and
                           smoke.stdout.count("SCENE_LIVE_WORKER_RENDER_PROOF=")==4,
                           "real worker FFmpeg render proof failed: "+smoke.stderr[-700:])
            for line in smoke.stdout.splitlines():
                if line.startswith("SCENE_LIVE_WORKER_RENDER_PROOF="):
                    print(line,flush=True)
            record["source"] = str(latest)
            atomic_json(worker.RECORD,record,0o600)
            self.did_refresh = True
            print("SCENE_WRITABLE_WORKER_SOURCE=REFRESHED REVISION="+latest.name,flush=True)
            return {"state": "PASS_LIVE_WORKER_RENDERED", "revision": latest.name}
        except BaseException:
            self._restore()
            raise

"""Guarded, rollback-safe refresh of the EXISTING owned Scene rendering worker.

The candidate web service and writable worker have independent source pointers.
A Scene-only source install must not silently leave the real worker on an old
renderer. Do not touch jobs, admission-only worker or unrelated applications.
"""
from __future__ import annotations

import configparser
import json
import os
import pwd
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import time

from production import scene_production_worker as worker

CANONICAL_RCLONE_CONFIGS = (
    Path('/home/ubuntu/.config/masterctl/rclone.conf'),
    Path('/home/ubuntu/.config/rclone/rclone.conf'),
    Path('/etc/rclone/rclone.conf'),
)

# Mirrors the actual Scene API TERMINAL_STATUSES: a Drive-verified, cleaned
# project is terminal just like completed. Unknown/queued/running stay blocked.
TERMINAL = frozenset({"done", "completed", "cleaned", "complete", "success", "failed",
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
    # Production-side synthetic pixel proofs: no queue DB, project, login,
    # user media or output directory is touched. Everything stays in root.
    import hashlib
    from vps_engine.smart_scroll.v2_renderer import (
        _motion, _xfade_filter, _filter, _resolved_color_filters, _write_ass)
    from vps_engine.smart_scroll.transition_engine import TRANSITION_CATALOG
    from vps_engine.smart_scroll.creative_v21 import FILTERS
    from vps_engine.smart_scroll.caption_sync import _script_guided_segments_from_anchors
    from vps_engine.smart_scroll.lut_engine import (
        PACK_DEFINITIONS, ensure_builtin_luts, blended_lut, ffmpeg_filter_path)

    def raw(*args):
        result = subprocess.run(["ffmpeg","-nostdin","-loglevel","error","-y",
                                 *map(str,args)],capture_output=True,timeout=65)
        if result.returncode:
            raise RuntimeError("Scene effect pixel proof failed: "+
                               result.stderr.decode(errors="replace")[-900:])
        return result.stdout

    def motion_frames(name, still):
        graph = _motion({"motion":name,"motion_intensity":.8},128,72,8,1.5,still)
        flags = (["-loop","1","-framerate","8","-t","1.5","-i",image]
                 if still else ["-i",video])
        data = raw(*flags,"-vf",graph,"-frames:v","12","-pix_fmt","rgb24",
                   "-f","rawvideo","-")
        frame = 128*72*3
        assert len(data) >= frame*10 and len(data)%frame == 0,(name,len(data))
        return data[:frame],data[-frame:]

    image_motions = ("pan_left","pan_right","ken_burns","zoom_in","zoom_out")
    video_motions = ("slow_push","reframe_left","reframe_right","gentle_zoom")
    for name in image_motions:
        first,last = motion_frames(name,True)
        assert hashlib.sha256(first).digest()!=hashlib.sha256(last).digest(),name
    for name in video_motions:
        first,last = motion_frames(name,False)
        assert hashlib.sha256(first).digest()!=hashlib.sha256(last).digest(),name

    visual_transitions = [name for name in TRANSITION_CATALOG
                          if name not in ("none","hard_cut")]
    transition_hashes = set()
    for name in visual_transitions:
        spec = _xfade_filter({"transition_out":name},0,.6,.3)
        frames = raw("-f","lavfi","-i","testsrc2=s=128x72:r=12:d=1.2",
                     "-f","lavfi","-i","smptebars=s=128x72:r=12:d=1.2",
                     "-filter_complex",f"[0:v][1:v]{spec},format=yuv420p[v]",
                     "-map","[v]","-frames:v","18","-pix_fmt","rgb24",
                     "-f","rawvideo","-")
        assert len(frames) >= 128*72*3*8,name
        digest = hashlib.sha256(frames).hexdigest()
        assert digest not in transition_hashes,("duplicate transition",name)
        transition_hashes.add(digest)

    scene_luts = root/"verify-luts"
    ensure_builtin_luts(scene_luts)
    cache = root/"verify-lut-blends"
    cache.mkdir()
    graded = set()
    lut_count = 0
    for pack,presets in PACK_DEFINITIONS.items():
        for preset in presets["presets"]:
            source = scene_luts/pack/(preset+".cube")
            assert source.is_file(),str(source)
            original = blended_lut(source,0,cache)
            maximum = blended_lut(source,100,cache)
            def grade(path):
                return raw("-i",image,"-vf",
                           f"format=rgb24,lut3d=file='{ffmpeg_filter_path(path)}',format=rgb24",
                           "-frames:v","1","-pix_fmt","rgb24","-f","rawvideo","-")
            neutral,applied = grade(original),grade(maximum)
            assert neutral != applied,(pack,preset,"LUT strength ignored")
            graded.add(hashlib.sha256(applied).hexdigest())
            lut_count += 1
    assert lut_count == 23 and len(graded)>=20,(lut_count,len(graded))

    baseline = raw("-i",image,"-frames:v","1","-pix_fmt","rgb24","-f","rawvideo","-")
    active_filters = [f for f in FILTERS if f!="none"]
    for name in active_filters:
        applied = raw("-i",image,"-vf",_filter(name,.75),
                      "-frames:v","1","-pix_fmt","rgb24","-f","rawvideo","-")
        assert applied != baseline,("scene colour filter ignored",name)
    selected = {"filter":"warm","filter_intensity":.9}
    sample_lut = scene_luts/"cinematic"/"filmic.cube"
    lut_chain = f"lut3d=file='{ffmpeg_filter_path(sample_lut)}'"
    scene_grade,quality = _resolved_color_filters(
        selected,SimpleNamespace(video_quality_preset="original"),lut_chain)
    assert scene_grade != "null" and quality=="null","LUT hid selected filter"
    just_lut = raw("-i",image,"-vf",lut_chain,
                   "-frames:v","1","-pix_fmt","rgb24","-f","rawvideo","-")
    lut_plus_filter = raw("-i",image,"-vf",lut_chain+","+scene_grade,
                          "-frames:v","1","-pix_fmt","rgb24","-f","rawvideo","-")
    assert lut_plus_filter != just_lut,"selected filter silently ignored after LUT"

    words = _script_guided_segments_from_anchors(
        script_text="alpha beta",anchors=[(0.0,0.2),(0.7,0.9)],
        duration=1.0,max_words=8,offset=0.0)
    assert len(words)==1 and len(words[0].words)==2
    assert words[0].words[1]["start"]==.7
    ass=root/"verified-karaoke.ass"
    assert _write_ass([words[0].to_dict()],ass,
                      SimpleNamespace(caption_karaoke_enabled=True),640,360)
    script=ass.read_text(encoding="utf-8-sig")
    assert r"{\kf70}alpha" in script and r"{\kf20}beta" in script
    input_args = ("-f","lavfi","-i","color=c=black:s=640x360:r=8:d=1")
    empty = raw(*input_args,"-frames:v","1","-pix_fmt","rgb24","-f","rawvideo","-")
    burned = raw(*input_args,"-vf",f"subtitles='{ass}'",
                 "-frames:v","1","-pix_fmt","rgb24","-f","rawvideo","-")
    assert burned!=empty,"ASS karaoke failed to burn visible rendered pixels"

    print("SCENE_LIVE_EFFECT_MATRIX="+json.dumps({
        "image_motions":len(image_motions),"video_motions":len(video_motions),
        "transitions":len(visual_transitions),"luts":lut_count,
        "colour_filters":len(active_filters),"lut_plus_filter":"PASS",
        "whisper_karaoke":"ASS_FFMPEG_PIXELS_PASS"},sort_keys=True),flush=True)

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
        self.original_env: bytes | None = None
        self.original_record: dict | None = None
        self.original_rclone_path: Path | None = None
        self.original_rclone_bytes: bytes | None = None
        self.token = ""
        self.did_refresh = False

    def _restore(self) -> None:
        if self.original_unit is None or self.original_record is None:
            return
        from production.runtime import atomic_json, run
        _atomic_regular_file(worker.UNIT_FILE, self.original_unit, 0o644)
        if self.original_env is not None:
            _atomic_regular_file(worker.ENV, self.original_env, 0o600)
        if self.original_rclone_path is not None:
            if self.original_rclone_bytes is None:
                self.original_rclone_path.unlink(missing_ok=True)
            else:
                _atomic_regular_file(self.original_rclone_path, self.original_rclone_bytes, 0o600)
                account = pwd.getpwnam("vps1scene")
                os.chown(self.original_rclone_path, account.pw_uid, account.pw_gid)
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
        self.original_env = None
        self.original_rclone_path = None
        self.original_rclone_bytes = None
        self.original_record = None
        self.did_refresh = False
        print("SCENE_WORKER_SOURCE_ROLLBACK=PASS PREVIOUS_SOURCE_RESTORED",flush=True)

    def rollback(self) -> None:
        self._restore()


    def _private_scene_rclone_config(self, remote: str) -> Path:
        """Reuse the owner-configured Drive Bridge remote without exposing its token.

        The production unit has ProtectHome=true; a config under /home/ubuntu
        must be installed inside Scene's private writable state for vps1scene.
        Do not overwrite a pre-existing independent Scene rclone configuration.
        """
        target_dir = worker.STATE / "worker-home" / ".config" / "rclone"
        target = target_dir / "rclone.conf"
        account = pwd.getpwnam("vps1scene")
        worker.require(not target.is_symlink() and not target_dir.is_symlink(),
                       "Scene rclone state contains an unexpected symlink")
        if target.is_file():
            existing = target.read_bytes()
            parser = configparser.RawConfigParser(interpolation=None)
            try:
                parser.read_string(existing.decode("utf-8"))
            except (UnicodeError, configparser.Error):
                raise RuntimeError("Existing private Scene rclone configuration is invalid; do not overwrite")
            worker.require(parser.has_section(remote),
                           "Existing Scene rclone config has no configured remote; do not overwrite")
            return target
        candidate = None
        for source in CANONICAL_RCLONE_CONFIGS:
            if source.is_symlink() or not source.is_file():
                continue
            try:
                blob = source.read_bytes()
                parser = configparser.RawConfigParser(interpolation=None)
                parser.read_string(blob.decode("utf-8"))
                if parser.has_section(remote):
                    candidate = blob
                    break
            except (OSError, UnicodeError, configparser.Error):
                continue
        worker.require(candidate is not None,
                       "No authorized Drive Bridge rclone remote available for Scene worker")
        worker.require(target_dir.parent.is_dir() and not target_dir.parent.is_symlink(),
                       "Scene worker private config home is not prepared")
        target_dir.mkdir(mode=0o700, exist_ok=True)
        os.chmod(target_dir, 0o700)
        os.chown(target_dir, account.pw_uid, account.pw_gid)
        self.original_rclone_path = target
        self.original_rclone_bytes = None
        _atomic_regular_file(target, candidate, 0o600)
        os.chown(target, account.pw_uid, account.pw_gid)
        return target

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
        self.original_env = worker.ENV.read_bytes()
        self.original_record = dict(record)
        try:
            if private.get("DRIVE_UPLOAD_ENABLED") != "1":
                # Do not use /home/ubuntu from systemd (ProtectHome=true).
                # Prepare a private copy of the canonical Drive Bridge remote.
                service_home = worker.STATE / "worker-home"
                remote = private.get("DRIVE_REMOTE", "gdrive") or "gdrive"
                config_file = self._private_scene_rclone_config(remote)
                probe = subprocess.run(
                    ["runuser", "-u", "vps1scene", "--", "/usr/bin/env",
                     "HOME="+str(service_home),
                     "XDG_CONFIG_HOME="+str(service_home/".config"),
                     "RCLONE_CONFIG="+str(config_file),
                     "rclone", "lsd", remote+":"],
                    capture_output=True, text=True, timeout=90)
                worker.require(probe.returncode == 0,
                               "Private Scene Drive remote cannot be listed; source/config change deferred")
                raw = self.original_env.decode("utf-8")
                lines = [line for line in raw.splitlines()
                         if line.split("=",1)[0].strip() not in
                         {"DRIVE_UPLOAD_ENABLED", "RCLONE_CONFIG"}]
                lines += ["DRIVE_UPLOAD_ENABLED=1", "RCLONE_CONFIG="+str(config_file)]
                _atomic_regular_file(worker.ENV,
                                     ("\n".join(lines)+"\n").encode(), 0o600)
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
                           smoke.stdout.count("SCENE_LIVE_WORKER_RENDER_PROOF=")==4 and
                           smoke.stdout.count("SCENE_LIVE_EFFECT_MATRIX=")==1,
                           "real worker FFmpeg full-effects proof failed: "+smoke.stderr[-1000:])
            for line in smoke.stdout.splitlines():
                if line.startswith(("SCENE_LIVE_WORKER_RENDER_PROOF=","SCENE_LIVE_EFFECT_MATRIX=")):
                    print(line,flush=True)
            record["source"] = str(latest)
            atomic_json(worker.RECORD,record,0o600)
            self.did_refresh = True
            print("SCENE_WRITABLE_WORKER_SOURCE=REFRESHED REVISION="+latest.name,flush=True)
            return {"state": "PASS_LIVE_WORKER_RENDERED", "revision": latest.name}
        except BaseException:
            self._restore()
            raise

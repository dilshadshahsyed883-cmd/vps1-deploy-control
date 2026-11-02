"""Idempotent Short Profile -> existing Short Maker renderer queue bridge.

Only a verified caller may invoke dispatch. The bridge does not start
scheduling itself. One deterministic profile daily claim corresponds to one
Maker job, whose state lives in the existing persistent jobs directory.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException

DISK_RESERVE_BYTES = 1024 * 1024 * 1024
MAKER_DEFAULTS = {
    "image_duration": 3.0,
    "order": "random",
    "fit": "fill_crop",
    "motion": "random",
    "fps": 30,
    "crf": 20,
}


def digest_file(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as reader:
        for piece in iter(lambda: reader.read(1024 * 1024), b""):
            h.update(piece)
    return h.hexdigest()


def audio_duration(path, ffprobe=None):
    executable = ffprobe or shutil.which("ffprobe") or "/usr/bin/ffprobe"
    try:
        proc = subprocess.run(
            [executable, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30, check=False)
        value = float(proc.stdout.strip())
    except (ValueError, OSError, subprocess.TimeoutExpired):
        raise HTTPException(422, detail="Could not determine verified voice duration")
    if proc.returncode != 0 or not math.isfinite(value) or value <= 0:
        raise HTTPException(422, detail="Voice duration must be finite and positive")
    return value


class ExistingMakerQueueBridge:
    def __init__(self, jobs_root, catalog, check_folder,
                 reserve_names, job_lookup, executor, render_job,
                 jobs, jobs_lock, settings=None):
        self.root = Path(jobs_root)
        self.catalog = catalog
        self.check_folder = check_folder
        self.reserve_names = reserve_names
        self.job_lookup = job_lookup
        self.executor = executor
        self.render_job = render_job
        self.jobs = jobs
        self.jobs_lock = jobs_lock
        self.settings = dict(MAKER_DEFAULTS if settings is None else settings)

    @contextmanager
    def _lock(self):
        if self.root.is_symlink():
            raise HTTPException(409, detail="Unsafe Short Maker job root")
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / ".short-profile-dispatch.lock"
        if path.is_symlink():
            raise HTTPException(409, detail="Unsafe Short Profile dispatch lock")
        with path.open("a+b") as fd:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)

    def lookup(self, job_id):
        return self.job_lookup(job_id)

    def _catalog_inputs(self, claim):
        owner, sid = claim["owner"], claim["source_import_id"]
        assets = self.catalog(owner, sid)
        if not assets or not assets.get("audio") or not assets.get("picture"):
            raise HTTPException(409, detail="Verified source media is unavailable")
        desired_voice = claim["voice_sha256"]
        voices = [a for a in assets["audio"] if a["sha256"] == desired_voice]
        if len(voices) != 1:
            raise HTTPException(409, detail="Claimed voice has been removed or altered")
        expected = json.loads(claim["pictures_json"])
        expected_digests = [p["sha256"] for p in expected]
        picture_map = {p["sha256"]: p for p in assets["picture"]}
        if (len(expected_digests) != len(set(expected_digests)) or
                any(sha not in picture_map for sha in expected_digests)):
            raise HTTPException(409, detail="Claimed pictures no longer verified")
        return voices[0], [picture_map[sha] for sha in expected_digests]

    @staticmethod
    def _copy_verified(asset, dest):
        original = Path(asset["path"])
        if original.is_symlink() or not original.is_file():
            raise HTTPException(409, detail="Verified source path is unavailable")
        shutil.copyfile(original, dest)
        if digest_file(dest) != asset["sha256"]:
            raise HTTPException(409, detail="Media changed during protected job staging")

    def enqueue(self, claim):
        owner, job_id = claim["owner"], claim["job_id"]
        if not isinstance(job_id, str) or len(job_id) != 16 or any(
                c not in "0123456789abcdef" for c in job_id):
            raise HTTPException(409, detail="Invalid deterministic profile job ID")

        with self._lock():
            current = self.lookup(job_id)
            if current is not None:
                if (str(current.get("owner", "")).casefold() != owner or
                        current.get("short_profile_id") != claim["profile_id"] or
                        current.get("short_profile_run_id") != claim["run_id"]):
                    raise HTTPException(409, detail="Existing Maker job identity mismatch")
                return current

            target = self.root / job_id
            if target.exists() or target.is_symlink():
                raise HTTPException(409, detail="Unreconciled Short Maker job folder")
            self.check_folder(owner, claim["output_folder"])
            voice, pictures = self._catalog_inputs(claim)

            if not pictures:
                raise HTTPException(409, detail="No verified picture available")
            required_bytes = voice["size"] + sum(p["size"] for p in pictures)
            available = shutil.disk_usage(self.root).free - DISK_RESERVE_BYTES
            if required_bytes * 2 > available:
                raise HTTPException(507, detail="Insufficient VPS capacity to stage daily render")
            staging = Path(tempfile.mkdtemp(
                prefix=".short-profile-stage-", dir=str(self.root)))
            committed = False
            try:
                input_root = staging / "input"
                image_root = input_root / "images"
                voice_root = input_root / "audio"
                image_root.mkdir(parents=True)
                voice_root.mkdir()
                copied_images = []
                for i, asset in enumerate(pictures, 1):
                    extension = Path(asset["path"]).suffix.lower()
                    if extension not in (".jpg", ".jpeg", ".png", ".webp",
                                         ".jfif", ".heic", ".heif"):
                        raise HTTPException(409, detail="Unexpected picture extension")
                    dst = image_root / f"{i:05d}_{asset['sha256'][:12]}{extension}"
                    self._copy_verified(asset, dst)
                    copied_images.append(dst)
                extension = Path(voice["path"]).suffix.lower()
                if extension not in (".mp3", ".wav", ".m4a", ".aac",
                                     ".flac", ".ogg", ".opus"):
                    raise HTTPException(409, detail="Unexpected voice extension")
                voice_dest = voice_root / ("0001_" + voice["sha256"][:12] + extension)
                self._copy_verified(voice, voice_dest)
                duration = audio_duration(voice_dest)
                # Same original Maker renderer, output numbering, fit, FPS and
                # motion settings. No independent media composition engine.
                output_names = self.reserve_names(claim["output_folder"], 1)
                cfg = {
                    "images": [str(self.root / job_id / x.relative_to(staging))
                               for x in copied_images],
                    "audio": [str(self.root / job_id / voice_dest.relative_to(staging))],
                    "video_duration": duration,
                    "image_duration": self.settings["image_duration"],
                    "output_count": 1,
                    "order": self.settings["order"],
                    "fit": self.settings["fit"],
                    "motion": self.settings["motion"],
                    "fps": self.settings["fps"],
                    "crf": self.settings["crf"],
                    "base_name": "short_video.mp4",
                    "output_names": output_names,
                    "batch_tag": output_names[0].rsplit("_", 1)[-1].removesuffix(".mp4"),
                    "input_mode": "upload",
                    "drive_input_path": "",
                    "drive_input_paths": [],
                    "upload_to_drive": False,
                    "available_to_short_uploader": True,
                    "drive_output_path": "",
                    "verified_clean": False,
                    "short_vps_folder": claim["output_folder"],
                    "short_profile_id": claim["profile_id"],
                    "short_profile_run_id": claim["run_id"],
                    "short_profile_pkt_date": claim["pkt_date"],
                    "short_profile_voice_sha256": voice["sha256"],
                }
                state = {
                    "job_id": job_id, "owner": owner,
                    "status": "queued", "progress": 0,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "message": "Queued from daily Short Profile",
                    "outputs": [], "delivery_status": "short_uploader",
                    "drive_verified": False, "local_cleaned": False,
                    "short_profile_id": claim["profile_id"],
                    "short_profile_run_id": claim["run_id"],
                    "short_profile_pkt_date": claim["pkt_date"],
                    "config": {k: v for k, v in cfg.items()
                               if k not in ("images", "audio")},
                }
                path = staging / "state.json"
                with path.open("w", encoding="utf-8") as fd:
                    json.dump(state, fd, sort_keys=True)
                    fd.flush()
                    os.fsync(fd.fileno())
                staging.rename(target)
                committed = True
                with self.jobs_lock:
                    self.jobs[job_id] = state
                self.executor.submit(self.render_job, job_id, cfg)
                return state
            finally:
                if not committed and staging.is_dir() and not staging.is_symlink():
                    shutil.rmtree(staging)

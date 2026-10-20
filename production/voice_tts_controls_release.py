"""Idle-gated immutable release of ONLY Voice Kokoro/Edge queue web + workers.

No busy job, active Drive transfer, OmniVoice worker, Auto Script, or user
artifact is modified by this installer. Called only by the protected VPS1
production deploy helper with the explicit [vps1-voice-tts-controls-only] tag.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

from production.runtime import BASE, DATA, REPO, atomic_json, atomic_link, event, http_check, run
from production.voice_queue_release import verify_queues

APP = "voice-studio"
CURRENT = BASE / "current/voice-studio"
RELEASES = BASE / "releases/voice-studio"
JOURNALS = BASE / "transactions/voice-tts-queue-controls"
WEB = "vps1-production-voice-studio.service"
WORKERS = ("vps1-production-kokoro-worker.service", "vps1-production-edge-worker.service")
UNITS = (WEB,) + WORKERS
FILES = (
    Path("part01/queue_actions.py"),
    Path("part01/kokoro_api.py"),
    Path("part01/edge_api.py"),
    Path("part01/templates/voice_studio.html"),
    Path("part02/kokoro_worker.py"),
    Path("part03/edge_worker.py"),
)
BUSY = ("queued", "processing", "retry_wait", "pause_requested", "cancel_requested")
DELIVERY_BUSY = ("pending", "retry_pending", "transferring")
DATA_ROOTS = (
    DATA / "voice-studio/kokoro/data/kokoro_jobs.sqlite3",
    DATA / "voice-studio/edge/data/edge_jobs.sqlite3",
)


def require(ok, reason):
    if not ok:
        raise RuntimeError("VOICE_CONTROLS_RELEASE_BLOCKED: " + reason)


def check_idle_and_lock(db_paths):
    """Hold SQLite writer fences across the stop sequence; close on all errors."""
    connections = []
    try:
        for path in db_paths:
            require(path.is_file() and not path.is_symlink(), "queue DB absent or symlink: " + str(path))
            db = sqlite3.connect("file:" + str(path) + "?mode=rw", uri=True, timeout=10)
            connections.append(db)
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("BEGIN IMMEDIATE")
            columns = {v[1] for v in db.execute("PRAGMA table_info(jobs)")}
            require({"status", "delivery_status"} <= columns, "unrecognized job schema: " + str(path))
            params = BUSY + DELIVERY_BUSY
            placeholders = ",".join("?" for _ in BUSY)
            delivery_placeholders = ",".join("?" for _ in DELIVERY_BUSY)
            busy = db.execute(
                "SELECT status, delivery_status FROM jobs WHERE status IN (" + placeholders +
                ") OR (status='completed' AND delivery_status IN (" + delivery_placeholders + ")) LIMIT 1",
                params,
            ).fetchone()
            require(busy is None, "active/queued synthesis or Drive delivery: " + str(path))
        return connections
    except BaseException:
        for db in reversed(connections):
            db.rollback()
            db.close()
        raise


def unlock(connections):
    for db in reversed(connections):
        db.rollback()  # Preflight uses no write, release locks without altering a job.
        db.close()


def backup_sqlite(src, dest):
    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(not dest.exists(), "snapshot already exists")
    with sqlite3.connect("file:" + str(src) + "?mode=ro", uri=True) as origin:
        with sqlite3.connect(str(dest)) as target:
            origin.backup(target)
            require(target.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
                    "SQLite snapshot integrity failed")
    dest.chmod(0o600)


def stage(revision, previous):
    require(re.fullmatch(r"[0-9a-f]{40}", revision) is not None, "revision not immutable SHA")
    patches = {}
    for relative in FILES:
        src = REPO / "apps/voice-studio" / relative
        require(src.is_file() and not src.is_symlink(), "missing or unsafe source " + str(relative))
        data = src.read_bytes()
        if relative.suffix == ".py":
            compile(data, str(relative), "exec")
        patches[relative] = data
    require(b"from queue_actions import migrate" in patches[Path("part01/kokoro_api.py")],
            "Kokoro API does not load state machine")
    require(b"from queue_actions import migrate" in patches[Path("part01/edge_api.py")],
            "Edge API does not load state machine")
    require(b"allowed_actions" in patches[Path("part01/templates/voice_studio.html")],
            "Voice UI lacks backend permission rendering")
    target = RELEASES / (revision + "-tts-controls-only")
    require(not target.exists() and not target.is_symlink(), "candidate already exists")
    shutil.copytree(previous, target, symlinks=True)
    manifest_file = target / "_production_manifest.json"
    require(manifest_file.is_file() and not manifest_file.is_symlink(), "manifest absent")
    manifest = json.loads(manifest_file.read_text())
    require(manifest.get("app") == APP and isinstance(manifest.get("files"), dict),
            "manifest identity invalid")
    for relative, data in patches.items():
        destination = target / relative
        require(destination.parent.is_dir() and not destination.parent.is_symlink(),
                "unsafe module destination")
        destination.write_bytes(data)
        manifest["files"][str(relative)] = hashlib.sha256(data).hexdigest()
    manifest["revision"] = revision
    manifest["voice_tts_controls_overlay"] = str(previous)
    atomic_json(manifest_file, manifest, 0o644)
    return target


def active(name):
    return subprocess.run(["systemctl", "is-active", "--quiet", name],
                          check=False, capture_output=True).returncode == 0


def start_all():
    run(["systemctl", "start", WEB], timeout=65, quiet=True)
    http_check(APP)
    for unit in WORKERS:
        run(["systemctl", "start", unit], timeout=65, quiet=True)
    for unit in UNITS:
        require(active(unit), "required unit failed to start: " + unit)


def install(revision):
    require(CURRENT.is_symlink(), "Voice release pointer is not managed")
    previous = CURRENT.resolve(strict=True)
    require(previous.is_dir() and previous.is_relative_to(RELEASES), "Voice release escapes root")
    require(all(active(unit) for unit in UNITS), "Voice web/Kokoro/Edge units must be active")
    http_check(APP)
    # Fully stage and syntax-validate from reviewed Git before stopping anything.
    target = stage(revision, previous)
    journal = JOURNALS / revision
    require(not journal.exists(), "immutable release already attempted")
    journal.mkdir(parents=True, mode=0o700)
    atomic_json(journal / "before.json", {
        "revision": revision, "old_release": str(previous), "target": str(target),
        "affected_units": list(UNITS), "other_workers": "untouched",
    })
    fences = []
    stopped = False
    switched = False
    try:
        fences = check_idle_and_lock(DATA_ROOTS)
        try:
            # Block new job submissions before halting the two idle worker loops.
            for unit in UNITS:
                run(["systemctl", "stop", unit], timeout=65, quiet=True)
            stopped = True
        finally:
            unlock(fences)
            fences = []
        for source in DATA_ROOTS:
            backup_sqlite(source, journal / "sqlite-backup" / source.name)
        atomic_link(CURRENT, target)
        switched = True
        start_all()
        verify_queues()  # Real local + authenticated HTTPS GETs; no mutation.
        event(app=APP, result="PASS_VOICE_TTS_CONTROLS", revision=revision,
              idle_gate="PASS", db_backup="PASS", affected_units=list(UNITS),
              other_workers="PRESERVED", auto_script="OFFLINE")
        atomic_json(journal / "result.json", {
            "result": "PASS_VOICE_TTS_CONTROLS", "revision": revision,
            "backup": "PRESERVED", "workers": list(UNITS),
        })
    except BaseException:
        if fences:
            unlock(fences)
        if stopped or switched:
            # Roll back source pointer only; additive schema + all queue records stay.
            for unit in reversed(UNITS):
                subprocess.run(["systemctl", "stop", unit], capture_output=True, check=False)
            if switched:
                atomic_link(CURRENT, previous)
            start_all()
            http_check(APP)
            event(app=APP, rollback="PASS", queues="PRESERVED", other_workers="UNTOUCHED")
        raise

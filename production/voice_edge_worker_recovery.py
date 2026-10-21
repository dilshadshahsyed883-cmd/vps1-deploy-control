"""Repair only the Edge TTS worker dependency and restore its genuine heartbeat.

Protected main/Tailscale release; no database mutations, no user job submission.
Web restarts must not stop the independent Edge worker (Requires -> Wants).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from production.runtime import BASE, DATA, atomic_json, event, run

APP = "voice-studio"
WEB = "vps1-production-voice-studio.service"
EDGE = "vps1-production-edge-worker.service"
UNIT_PATH = Path("/etc/systemd/system") / EDGE
DB = DATA / "voice-studio/edge/data/edge_jobs.sqlite3"
JOURNALS = BASE / "transactions/voice-edge-worker-recovery"
URL = "http://127.0.0.1:8143/tts/api/edge/status"
OLD_DEP = "Requires=" + WEB
NEW_DEP = "Wants=" + WEB
BUSY = ("processing", "pause_requested", "cancel_requested")
DELIVERY_BUSY = ("transferring",)


def require(ok, detail):
    if not ok:
        raise RuntimeError("EDGE_RECOVERY_BLOCKED: " + detail)


def active(unit):
    return subprocess.run(["systemctl", "is-active", "--quiet", unit],
                          capture_output=True, check=False).returncode == 0


def dependency_rewrite(text):
    """Accept only the owned Edge unit and change its parent coupling exactly."""
    require("edge_worker.py" in text and
            "WorkingDirectory=" + str(BASE) + "/current/voice-studio/part03" in text and
            "EnvironmentFile=/etc/fresh-website/production-voice-studio.env" in text,
            "live systemd unit ownership mismatch")
    if OLD_DEP in text:
        require(text.count(OLD_DEP) == 1 and NEW_DEP not in text,
                "ambiguous worker dependencies")
        return text.replace(OLD_DEP, NEW_DEP, 1)
    require(text.count(NEW_DEP) == 1 and OLD_DEP not in text,
            "unknown worker dependencies")
    return text


def atomic_unit(contents):
    temp = UNIT_PATH.with_name(UNIT_PATH.name + ".edge-recovery.tmp")
    try:
        temp.write_text(contents, encoding="utf8")
        temp.chmod(0o644)
        os.replace(temp, UNIT_PATH)
    finally:
        temp.unlink(missing_ok=True)


def dependency_verified():
    requires = run(["systemctl", "show", EDGE, "--property=Requires", "--value"],
                   quiet=True).split()
    wants = run(["systemctl", "show", EDGE, "--property=Wants", "--value"],
                quiet=True).split()
    require(WEB not in requires and WEB in wants, "effective systemd dependency still coupled")


def worker_status():
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                URL, timeout=4) as response:
            data = json.loads(response.read(4000))
        if response.status != 200 or not isinstance(data, dict):
            return {"ready": False, "reason": "invalid_response"}
        return data
    except (OSError, ValueError, TypeError, urllib.error.URLError):
        return {"ready": False, "reason": "status_unavailable"}


def lock_idle_queue():
    """Block new claims until a stale, still-active worker has been restarted."""
    require(DB.is_file() and not DB.is_symlink(), "Edge SQLite queue unavailable")
    connection = sqlite3.connect("file:" + str(DB) + "?mode=rw", uri=True, timeout=10)
    try:
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("BEGIN IMMEDIATE")
        cols = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
        require({"status", "delivery_status"} <= cols, "unknown Edge jobs schema")
        busy = connection.execute(
            "SELECT 1 FROM jobs WHERE status IN (?,?,?) "
            "OR (status='completed' AND delivery_status IN (?)) LIMIT 1",
            BUSY + DELIVERY_BUSY).fetchone()
        require(busy is None, "Edge synthesis/Drive transfer active; never restart")
        return connection
    except BaseException:
        connection.rollback()
        connection.close()
        raise


def await_ready(seconds=100):
    deadline = time.monotonic() + seconds
    last = "unavailable"
    while time.monotonic() < deadline:
        state = worker_status()
        if active(EDGE) and state.get("ready") is True:
            return
        last = str(state.get("reason", "invalid_status"))[:80]
        time.sleep(4)
    raise RuntimeError("EDGE_RECOVERY_NOT_READY: " + last)


def install(revision):
    require(re.fullmatch(r"[a-f0-9]{40}", revision) is not None,
            "immutable revision invalid")
    require(active(WEB), "Voice web unit unavailable")
    require(UNIT_PATH.is_file() and not UNIT_PATH.is_symlink(),
            "Edge worker unit missing/unowned")
    old = UNIT_PATH.read_text(encoding="utf8")
    corrected = dependency_rewrite(old)
    journal = JOURNALS / revision
    require(not journal.exists(), "recovery release already attempted")
    journal.mkdir(parents=True, mode=0o700)
    atomic_json(journal / "before.json", {
        "revision": revision, "unit_original": old,
        "edge_active_before": active(EDGE)
    })
    switched = False
    try:
        if corrected != old:
            atomic_unit(corrected)
            switched = True
        run(["systemd-analyze", "verify", UNIT_PATH], timeout=25, quiet=True)
        run(["systemctl", "daemon-reload"], timeout=35, quiet=True)
        dependency_verified()

        state = worker_status()
        action = "preserved"
        if not active(EDGE):
            # Clear only this worker's rate-limit failure, never restart other engines.
            run(["systemctl", "reset-failed", EDGE], timeout=25, quiet=True)
            run(["systemctl", "start", EDGE], timeout=60, quiet=True)
            action = "started"
        elif state.get("reason") in ("worker_heartbeat_stale", "worker_offline",
                                      "status_unavailable"):
            fence = lock_idle_queue()
            try:
                # Prevent concurrent worker claims while recovering a stuck worker.
                run(["systemctl", "restart", EDGE], timeout=60, quiet=True)
                action = "idle_stale_restarted"
            finally:
                fence.rollback()
                fence.close()
        # A fresh heartbeat with unavailable catalog is an upstream problem,
        # never hide it by repeatedly restarting a healthy worker process.
        await_ready()
        dependency_verified()
        atomic_json(journal / "result.json", {
            "result": "PASS_VOICE_EDGE_HEARTBEAT_RECOVERY",
            "revision": revision, "action": action, "edge_ready": True,
            "unit_decoupled": True, "owner_jobs_mutated": False
        })
        event(app=APP, result="PASS_VOICE_EDGE_HEARTBEAT_RECOVERY",
              action=action, edge_ready=True, workers_affected=[EDGE],
              edge_dependency="Wants", queues="PRESERVED", auto_script="OFFLINE")
    except BaseException:
        # Roll back only failed configuration writes. Do not stop recovered
        # services or erase any output, queue, or Google Drive data.
        if switched:
            atomic_unit(old)
            run(["systemctl", "daemon-reload"], timeout=35, quiet=True)
            event(app=APP, edge_unit_config_rollback="PASS", data="PRESERVED")
        raise

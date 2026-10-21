"""Repair only the Kokoro TTS worker dependency and restore its genuine heartbeat.

Protected main/Tailscale release; no database mutations, no user job submission.
Web restarts must not stop the independent Kokoro worker (Requires -> Wants).
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
KOKORO = "vps1-production-kokoro-worker.service"
UNIT_PATH = Path("/etc/systemd/system") / KOKORO
DB = DATA / "voice-studio/kokoro/data/kokoro_jobs.sqlite3"
JOURNALS = BASE / "transactions/voice-kokoro-worker-recovery"
URL = "http://127.0.0.1:8143/tts/api/kokoro/status"
OLD_DEP = "Requires=" + WEB
NEW_DEP = "Wants=" + WEB
BUSY = ("processing", "pause_requested", "cancel_requested")
DELIVERY_BUSY = ("transferring",)


def require(ok, detail):
    if not ok:
        raise RuntimeError("KOKORO_RECOVERY_BLOCKED: " + detail)


def active(unit):
    return subprocess.run(["systemctl", "is-active", "--quiet", unit],
                          capture_output=True, check=False).returncode == 0


def dependency_rewrite(text):
    """Accept only the owned Kokoro unit and change its parent coupling exactly."""
    require("kokoro_worker.py" in text and
            "WorkingDirectory=" + str(BASE) + "/current/voice-studio/part02" in text and
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
    temp = UNIT_PATH.with_name(UNIT_PATH.name + ".kokoro-recovery.tmp")
    try:
        temp.write_text(contents, encoding="utf8")
        temp.chmod(0o644)
        os.replace(temp, UNIT_PATH)
    finally:
        temp.unlink(missing_ok=True)


def dependency_verified():
    requires = run(["systemctl", "show", KOKORO, "--property=Requires", "--value"],
                   quiet=True).split()
    wants = run(["systemctl", "show", KOKORO, "--property=Wants", "--value"],
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
    require(DB.is_file() and not DB.is_symlink(), "Kokoro SQLite queue unavailable")
    connection = sqlite3.connect("file:" + str(DB) + "?mode=rw", uri=True, timeout=10)
    try:
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("BEGIN IMMEDIATE")
        cols = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
        require({"status", "delivery_status"} <= cols, "unknown Kokoro jobs schema")
        busy = connection.execute(
            "SELECT 1 FROM jobs WHERE status IN (?,?,?) "
            "OR (status='completed' AND delivery_status IN (?)) LIMIT 1",
            BUSY + DELIVERY_BUSY).fetchone()
        require(busy is None, "Kokoro synthesis/Drive transfer active; never restart")
        return connection
    except BaseException:
        connection.rollback()
        connection.close()
        raise


def verify_public_and_catalog():
    """Require real Voice API readiness and a nonempty live catalog, not a key."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open("http://127.0.0.1:8143/tts/api/kokoro/voices", timeout=6) as response:
        payload = json.loads(response.read(200_000))
        require(response.status == 200 and isinstance(payload, dict) and
                isinstance(payload.get("voices"), list) and len(payload["voices"]) > 0,
                "Kokoro voice catalog unavailable")
    from production.route_uploader_scene_https import https_get, scene_cookie
    status, kind, body = https_get("/voice-studio/tts/api/kokoro/status", scene_cookie())
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        payload = {}
    require(status == 200 and "json" in kind.lower() and isinstance(payload, dict)
            and payload.get("ready") is True and payload.get("mode") == "kokoro-onnx-cpu",
            "authenticated public Kokoro readiness not verified")
    anonymous, _, _ = https_get("/voice-studio/tts/api/kokoro/status")
    require(anonymous in (401, 403, 303), "Kokoro public endpoint not authenticated")


def heartbeat_fresh(since=None):
    path = DATA / "voice-studio/kokoro/data/heartbeat.json"
    if not path.is_file() or path.is_symlink():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf8"))
        stamp = float(payload.get("epoch", 0))
        age = time.time() - stamp
        return payload.get("ready") is True and -5 <= age <= 20 and (
            since is None or stamp >= since - 2)
    except (OSError, ValueError, TypeError):
        return False


def await_ready(seconds=100, fresh_since=None):
    deadline = time.monotonic() + seconds
    last = "unavailable"
    while time.monotonic() < deadline:
        state = worker_status()
        if (active(KOKORO) and state.get("ready") is True and
                state.get("mode") == "kokoro-onnx-cpu" and
                heartbeat_fresh(fresh_since)):
            return
        last = str(state.get("reason", "invalid_status"))[:80]
        time.sleep(4)
    raise RuntimeError("KOKORO_RECOVERY_NOT_READY: " + last)


def install(revision):
    require(re.fullmatch(r"[a-f0-9]{40}", revision) is not None,
            "immutable revision invalid")
    require(active(WEB), "Voice web unit unavailable")
    require(UNIT_PATH.is_file() and not UNIT_PATH.is_symlink(),
            "Kokoro worker unit missing/unowned")
    old = UNIT_PATH.read_text(encoding="utf8")
    corrected = dependency_rewrite(old)
    journal = JOURNALS / revision
    require(not journal.exists(), "recovery release already attempted")
    journal.mkdir(parents=True, mode=0o700)
    atomic_json(journal / "before.json", {
        "revision": revision, "unit_original": old,
        "kokoro_active_before": active(KOKORO)
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
        fresh_since = None
        if not active(KOKORO):
            # Clear only this worker's rate-limit failure, never restart other engines.
            fence = lock_idle_queue()
            try:
                fresh_since = time.time()
                run(["systemctl", "reset-failed", KOKORO], timeout=25, quiet=True)
                run(["systemctl", "start", KOKORO], timeout=60, quiet=True)
                action = "started"
            finally:
                fence.rollback()
                fence.close()
        elif state.get("reason") in ("worker_heartbeat_stale", "worker_offline",
                                      "status_unavailable"):
            fence = lock_idle_queue()
            try:
                # Prevent concurrent worker claims while recovering a stuck worker.
                fresh_since = time.time()
                run(["systemctl", "restart", KOKORO], timeout=60, quiet=True)
                action = "idle_stale_restarted"
            finally:
                fence.rollback()
                fence.close()
        # A fresh heartbeat with unavailable catalog is an upstream problem,
        # never hide it by repeatedly restarting a healthy worker process.
        await_ready(fresh_since=fresh_since)
        dependency_verified()
        verify_public_and_catalog()
        atomic_json(journal / "result.json", {
            "result": "PASS_VOICE_KOKORO_HEARTBEAT_RECOVERY",
            "revision": revision, "action": action, "kokoro_ready": True,
            "unit_decoupled": True, "owner_jobs_mutated": False
        })
        event(app=APP, result="PASS_VOICE_KOKORO_HEARTBEAT_RECOVERY",
              action=action, kokoro_ready=True, workers_affected=[KOKORO],
              kokoro_dependency="Wants", queues="PRESERVED", auto_script="OFFLINE")
    except BaseException:
        # Roll back only failed configuration writes. Do not stop recovered
        # services or erase any output, queue, or Google Drive data.
        if switched:
            atomic_unit(old)
            run(["systemctl", "daemon-reload"], timeout=35, quiet=True)
            event(app=APP, kokoro_unit_config_rollback="PASS", data="PRESERVED")
        raise

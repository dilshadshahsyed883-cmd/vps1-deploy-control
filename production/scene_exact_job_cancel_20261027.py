"""One-shot, owner-authorized cancellation of one Scene Builder render.

No service restart, no project deletion, no renderer checkpoint modification.
Invoked only by the exact protected-main Scene-only release marker.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request

from production import scene_production_worker as worker

TARGET_JOB = "3327eb5e931e417f9daf5bca9af526da"
TARGET_PROJECT = "Eala_Tokyo_V25"
MARKER = "[vps1-scene-job-terminate-3327-20261027]"
TERMINAL = frozenset({"cancelled", "failed", "completed", "cleaned"})


def _fetch(method: str, route: str, token: str) -> dict:
    # Loopback URL and target are constants; no user-supplied host, path or command.
    request = urllib.request.Request(
        worker.URL + route,
        method=method,
        headers={"x-scene-worker-token": token, "Accept": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=12) as response:
        if response.status != 200:
            raise RuntimeError("Scene job cancellation HTTP status " + str(response.status))
        value = json.load(response)
    if not isinstance(value, dict):
        raise RuntimeError("Scene worker returned non-object response")
    return value


def _verified_job(data: dict) -> dict:
    if str(data.get("id") or "") != TARGET_JOB:
        raise RuntimeError("Scene cancellation job identity did not match")
    if str(data.get("project_name") or "") != TARGET_PROJECT:
        raise RuntimeError("Scene cancellation project identity did not match")
    return data


def terminate_exact_job() -> str:
    worker.require(os.geteuid() == 0 and socket.gethostname() == "vps-01a23801",
                   "targeted cancellation requires protected production host")
    worker.require(worker.RECORD.is_file() and not worker.RECORD.is_symlink(),
                   "owned Scene worker record missing")
    record = json.loads(worker.RECORD.read_text(encoding="utf-8"))
    worker.require(record.get("owner") == "scene-production-worker-v1"
                   and record.get("unit") == worker.UNIT
                   and record.get("state") == str(worker.STATE)
                   and record.get("endpoint") == worker.URL,
                   "Scene worker ownership mismatch")
    worker.require(worker.ENV.is_file() and not worker.ENV.is_symlink(),
                   "private Scene worker environment missing")
    settings = worker.entries(worker.ENV.read_text(encoding="utf-8"))
    token = str(settings.get("SCENE_WORKER_TOKEN") or "")
    worker.require(re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token) is not None,
                   "invalid Scene worker private token")
    worker.require(settings.get("SCENE_WORKER_ADMISSION_ONLY") == "0",
                   "refuse to touch read-only admission worker")
    worker.require(subprocess.run(["systemctl", "is-active", "--quiet", worker.UNIT],
                                  check=False, timeout=10).returncode == 0,
                   "owned Scene worker not active")

    health = _fetch("GET", "/health", token)
    worker.require(health.get("ok") is True and health.get("admission_only") is False,
                   "owned Scene worker is not writable/healthy")
    old = _verified_job(_fetch("GET", f"/jobs/{TARGET_JOB}", token))
    old_status = str(old.get("status") or "").lower()
    if old_status in TERMINAL:
        print("SCENE_TARGET_JOB_TERMINATION=ALREADY_TERMINAL STATUS=" + old_status,
              flush=True)
        return old_status
    worker.require(old_status in {"queued", "waiting_for_audit", "preparing",
                   "downloading_media", "ready_to_render", "rendering",
                   "ready_to_upload", "drive_uploading", "running"},
                   "unknown Scene job state; no cancellation requested")
    # The normal worker API sets cancel_requested, then the existing renderer
    # terminates only the matching job and preserves verified chunk checkpoints.
    acknowledged = _verified_job(_fetch("POST", f"/jobs/{TARGET_JOB}/cancel", token))
    worker.require(int(acknowledged.get("cancel_requested") or 0) == 1
                   or str(acknowledged.get("status") or "") == "cancelled",
                   "Scene worker did not acknowledge cancellation")
    print("SCENE_TARGET_JOB_CANCEL_REQUEST=ACK PRESERVE_CHECKPOINTS=YES", flush=True)
    for attempt in range(26):
        status = _verified_job(_fetch("GET", f"/jobs/{TARGET_JOB}", token))
        state = str(status.get("status") or "").lower()
        if state in TERMINAL:
            print("SCENE_TARGET_JOB_TERMINATION=CONFIRMED STATUS=" + state
                  + " CHECKPOINTS=UNCHANGED", flush=True)
            return state
        if attempt < 25:
            time.sleep(1)
    # A request is not termination. Leave status unmodified, preserve media,
    # and stop before triggering another application or gateway operation.
    raise RuntimeError("SCENE_TARGET_JOB_CANCEL_PENDING; no forced kill or status rewrite")

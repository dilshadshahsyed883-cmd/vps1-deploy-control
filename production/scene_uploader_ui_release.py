#!/usr/bin/env python3
"""Small, reversible Scene Builder UI release for the Auto Uploader HTTPS fix.

Called only from the pinned GitHub/Tailscale production release transaction.
Scene's data, credentials, worker, other apps, and upload queues are unchanged.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request

from production.runtime import BASE, REPO, atomic_json, atomic_link
from production.route_uploader_scene_https import deploy as publish_route

CURRENT = BASE / "current/scene-builder"
RELEASES = BASE / "releases/scene-builder"
UNIT = "vps1-scene-candidate.service"
SOURCE = REPO / "apps/scene-builder/part01"
TRANSACTIONS = BASE / "transactions/scene-uploader-ui"
FILES = (
    "templates/local_upload_manager.html",
    "static/lum_host_state_v1.js",
)


def require(ok: bool, why: str) -> None:
    if not ok:
        raise RuntimeError("SCENE_UI_BLOCKED: " + why)


def healthy() -> bool:
    for _ in range(40):
        try:
            with urllib.request.urlopen("http://127.0.0.1:18080/health", timeout=3) as r:
                if r.status == 200 and json.load(r).get("ok") is True:
                    return True
        except (OSError, ValueError):
            pass
        time.sleep(0.5)
    return False


def active() -> bool:
    return subprocess.run(["systemctl", "is-active", "--quiet", UNIT],
                          capture_output=True).returncode == 0


def restart() -> None:
    subprocess.run(["systemctl", "restart", UNIT], check=True, capture_output=True, timeout=35)
    require(healthy(), "Scene candidate failed after restart")


def source_is_valid() -> None:
    for relative in FILES:
        p = SOURCE / relative
        require(p.is_file() and not p.is_symlink(), "missing trusted source: " + relative)
    html = (SOURCE / FILES[0]).read_text()
    script = (SOURCE / FILES[1]).read_text()
    require("/LUM/app" not in html + script, "legacy LUM path present in source")
    require("/local-upload-manager/app" in html and "/local-upload-manager/app" in script,
            "target uploader path missing")
    require("auto_mode" in html and "auto_mode" in script, "four-mode routing incomplete")


def activate(revision: str) -> None:
    require(os.geteuid() == 0 and socket.gethostname().split(".")[0] == "vps-01a23801",
            "VPS_1 root-only operation")
    require(re.fullmatch("[0-9a-f]{40}", revision) is not None, "invalid SHA")
    require(shutil.disk_usage("/").free >= 5_000_000_000, "operational disk reserve")
    require(CURRENT.is_symlink(), "Scene candidate current pointer missing")
    old = CURRENT.resolve(strict=True)
    require(old.is_relative_to(RELEASES) and old.is_dir(), "Scene candidate release not owned")
    require(active() and healthy(), "Scene candidate unavailable")
    source_is_valid()
    for relative in FILES:
        require((old / "part01" / relative).is_file(), "old Scene source missing: " + relative)
    target = RELEASES / (revision + "-auto-uploader")
    journal = TRANSACTIONS / revision
    staged = False
    switched = False
    # If already on the new exact UI, still run the idempotent Nginx route check.
    needs_ui = any((old / "part01" / rel).read_bytes() != (SOURCE / rel).read_bytes()
                   for rel in FILES)
    if needs_ui:
        require(not target.exists() and not target.is_symlink(), "release path exists")
        require(not journal.exists(), "release journal already exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(old, target, symlinks=True)
        staged = True
        try:
            for relative in FILES:
                dst = target / "part01" / relative
                require(not dst.is_symlink(), "unsafe Scene release file")
                shutil.copy2(SOURCE / relative, dst)
                dst.chmod(0o644)
            journal.mkdir(parents=True, mode=0o700)
            atomic_json(journal / "previous.json", {
                "source_revision": revision,
                "previous_pointer": str(old),
                "new_pointer": str(target),
                "modified_files": FILES,
                "status": "STAGED",
            })
            atomic_link(CURRENT, target)
            switched = True
            restart()
            publish_route(revision)
            atomic_json(journal / "release.json", {
                "source_revision": revision, "status": "ACTIVE",
                "previous_pointer": str(old), "new_pointer": str(target),
                "modified_files": FILES, "route": "/local-upload-manager/",
            })
            print("SCENE_UI_RESULT=PASS ROUTE=LIVE DATA=UNCHANGED", flush=True)
        except BaseException:
            if switched:
                atomic_link(CURRENT, old)
                try:
                    restart()
                    print("SCENE_UI_ROLLBACK=PREVIOUS_RELEASE_RESTORED", flush=True)
                except Exception:
                    print("SCENE_UI_ROLLBACK=MANUAL_SERVICE_RECOVERY_REQUIRED", flush=True)
            if staged and not switched:
                shutil.rmtree(target)
            raise
    else:
        publish_route(revision)
        print("SCENE_UI_RESULT=ALREADY_CURRENT ROUTE=VERIFIED", flush=True)

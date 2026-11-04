"""Guarded Cloud Render Drive context web-only release.

Clone the live Scene source, replace only its Cloud Render module, preserve all
media/SQLite/OAuth and leave the independent render worker running. Fail closed
on an active Cloud Render ingress/upload and on missing worker-owned rclone
configuration. The approved SSH helper retains the production deployment lock.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile

from production.runtime import BASE, REPO, atomic_json, atomic_link, run

CURRENT = BASE / "current/scene-builder"
RELEASES = BASE / "releases/scene-builder"
TRANSACTIONS = BASE / "transactions/cloud-render-drive-web"
SOURCE = REPO / "apps/scene-builder/part01/cloud_render.py"
UNIT = "vps1-scene-candidate.service"
RENDERER = "vps1-scene-production-worker.service"
RELATIVE = "part01/cloud_render.py"
DB = Path("/var/lib/fresh-website/scene-builder/web/cloud-render/cloud_render.sqlite3")
WORKER_HOME = Path("/var/lib/fresh-website/scene-builder/worker-home")


def require(ok, why):
    if not ok:
        raise RuntimeError("CLOUD_RENDER_DRIVE_WEB_BLOCKED: " + why)


def check_idle(db=DB):
    """Read-only fail-closed check; retained failed and ready MP4 are safe."""
    require(db.is_file() and not db.is_symlink(), "Cloud Render database missing or unsafe")
    try:
        with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=4) as con:
            con.execute("PRAGMA query_only=ON")
            require(con.execute("PRAGMA quick_check").fetchone()[0] == "ok",
                    "Cloud Render database integrity")
            states = {str(s): int(n) for s, n in con.execute(
                "SELECT state, COUNT(*) FROM files GROUP BY state").fetchall()}
    except (sqlite3.Error, OSError) as exc:
        raise RuntimeError("CLOUD_RENDER_DRIVE_WEB_BLOCKED: Cloud Render state unreadable") from exc
    active = {"receiving", "queued", "uploading", "verifying"}
    require(not any(n for s, n in states.items() if s.lower() in active),
            "Cloud Render has an active incoming/upload job")
    return states


def worker_pid():
    pid = run(["systemctl", "show", "--property=MainPID", "--value", RENDERER],
              quiet=True).strip()
    require(pid.isdecimal() and int(pid) > 1, "render worker not active")
    return pid


def verify_rclone_context():
    """Exercise only listremotes as vps1scene; no transfer or credential output."""
    require(WORKER_HOME.is_dir() and (WORKER_HOME / ".config").is_dir(),
            "existing Scene worker HOME unavailable")
    command = ["runuser", "-u", "vps1scene", "--",
               "env", "-u", "RCLONE_CONFIG",
               "HOME=" + str(WORKER_HOME),
               "XDG_CONFIG_HOME=" + str(WORKER_HOME / ".config"),
               "rclone", "listremotes"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("CLOUD_RENDER_DRIVE_WEB_BLOCKED: worker rclone preflight unavailable") from exc
    require(result.returncode == 0 and "gdrive:" in result.stdout.splitlines(),
            "Scene worker HOME has no gdrive remote; aborting without changes")


def preflight(revision):
    require(os.geteuid() == 0, "approved root helper required")
    require(re.fullmatch("[a-f0-9]{40}", revision) is not None, "invalid revision")
    require(shutil.disk_usage("/").free >= 5_000_000_000, "disk reserve unavailable")
    require(CURRENT.is_symlink(), "Scene source pointer unavailable")
    previous = CURRENT.resolve(strict=True)
    require(previous.is_dir() and previous.is_relative_to(RELEASES),
            "unmanaged Scene source pointer")
    require(subprocess.run(["systemctl", "is-active", "--quiet", UNIT],
                           capture_output=True, timeout=10).returncode == 0,
            "Scene web inactive")
    from production.scene_candidate_release import _health
    _health()
    pid = worker_pid()
    check_idle()
    require(SOURCE.is_file() and not SOURCE.is_symlink(), "reviewed Cloud Render source absent")
    src = SOURCE.read_bytes()
    require(b"def scene_rclone_env(" in src and b"def register_cloud_render(" in src
            and b"def _upload(self, job):" in src
            and b"CANONICAL_RCLONE_CONFIG" not in src,
            "expected fixed Cloud Render implementation missing")
    installed = previous / RELATIVE
    require(installed.is_file() and not installed.is_symlink(),
            "existing Cloud Render module missing or linked")
    before = installed.read_bytes()
    require(b"def register_cloud_render(" in before and b"def _upload(self, job):" in before,
            "live Cloud Render API layout unrecognized")
    manifest = previous / "_scene_candidate_manifest.json"
    require(manifest.is_file() and not manifest.is_symlink(),
            "live immutable source manifest missing")
    saved = json.loads(manifest.read_text())
    require(saved.get("app") == "scene-builder" and isinstance(saved.get("files"), dict)
            and RELATIVE in saved["files"], "Scene release manifest ownership invalid")
    require(hashlib.sha256(before).hexdigest() == saved["files"][RELATIVE],
            "live Cloud Render source drift")
    verify_rclone_context()
    return previous, pid, src, saved


def restart_web():
    run(["systemctl", "restart", UNIT], quiet=True, timeout=75)
    from production.scene_candidate_release import _health
    _health()


def verify_ui():
    from production.route_uploader_scene_https import scene_cookie
    from production.scene_candidate_release import _request
    cookie = scene_cookie()
    status, _, body = _request("/cloud-render/", cookie)
    require(status == 200 and b"Cloud Render" in body,
            "authenticated Cloud Render UI unavailable")
    status, _, body = _request("/api/cloud-render/media", cookie)
    require(status == 200 and isinstance(json.loads(body).get("items"), list),
            "authenticated media API unavailable")


def install(revision):
    old, pid, blob, manifest = preflight(revision)
    target = RELEASES / (revision + "-cloud-render-drive-web")
    receipt = TRANSACTIONS / (revision + ".json")
    if old == target:
        verify_ui()
        print("CLOUD_RENDER_DRIVE_WEB=ALREADY_INSTALLED", flush=True)
        return
    require(not target.exists() and not target.is_symlink() and not receipt.exists(),
            "release target or transaction exists")
    copied = changed = False
    try:
        shutil.copytree(old, target, symlinks=True)
        copied = True
        destination = target / RELATIVE
        require(not destination.is_symlink() and destination.is_file(),
                "unmanaged candidate file")
        destination.write_bytes(blob)
        destination.chmod(0o644)
        changed_manifest = dict(manifest)
        changed_manifest["files"] = {**manifest["files"], RELATIVE: hashlib.sha256(blob).hexdigest()}
        changed_manifest["revision"] = revision
        changed_manifest["overlay"] = "cloud-render-drive-web-only"
        atomic_json(target / "_scene_candidate_manifest.json", changed_manifest, 0o644)
        result = subprocess.run(["python3", "-m", "py_compile", str(destination)],
                                capture_output=True, timeout=25, check=False)
        require(result.returncode == 0, "Python compilation failed")
        check_idle()  # Recheck before web source switch; no active ingress/upload.
        require(worker_pid() == pid, "renderer changed during preparation")
        atomic_link(CURRENT, target)
        changed = True
        restart_web()
        verify_ui()
        require(worker_pid() == pid, "render worker restarted during web-only release")
        require(CURRENT.resolve(strict=True) == target, "Scene source pointer drift")
        receipt.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_json(receipt, {"revision": revision, "previous": str(old), "after": str(target),
                             "renderer_pid": pid, "status": "ACTIVE",
                             "media": "PRESERVED", "oauth": "UNCHANGED",
                             "file": RELATIVE}, 0o600)
        print("CLOUD_RENDER_DRIVE_WEB=PASS WORKER=UNTOUCHED MEDIA=PRESERVED", flush=True)
    except BaseException:
        if changed:
            atomic_link(CURRENT, old)
            restart_web()
            require(worker_pid() == pid, "render worker changed during rollback")
            print("CLOUD_RENDER_DRIVE_WEB=ROLLBACK_PASS MEDIA=PRESERVED", flush=True)
        elif copied:
            shutil.rmtree(target, ignore_errors=True)
        raise


def rollback(revision):
    require(re.fullmatch("[a-f0-9]{40}", revision) is not None, "invalid revision")
    receipt = TRANSACTIONS / (revision + ".json")
    require(receipt.is_file() and not receipt.is_symlink(), "release receipt unavailable")
    details = json.loads(receipt.read_text())
    after = RELEASES / (revision + "-cloud-render-drive-web")
    before = Path(details.get("previous", ""))
    require(details.get("revision") == revision and details.get("status") == "ACTIVE",
            "release state mismatch")
    require(CURRENT.is_symlink() and CURRENT.resolve(strict=True) == after,
            "current Scene is not this release")
    require(before.is_relative_to(RELEASES) and before.is_dir(),
            "previous Scene source unavailable")
    require(worker_pid() == details.get("renderer_pid"), "renderer changed")
    check_idle()
    atomic_link(CURRENT, before)
    try:
        restart_web()
        verify_ui()
    except BaseException:
        atomic_link(CURRENT, after)
        restart_web()
        raise
    require(worker_pid() == details.get("renderer_pid"),
            "render worker changed during rollback")
    atomic_json(receipt, {**details, "status": "ROLLED_BACK"}, 0o600)
    print("CLOUD_RENDER_DRIVE_WEB=ROLLBACK_PASS MEDIA=PRESERVED", flush=True)

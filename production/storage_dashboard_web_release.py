"""Guarded Storage dashboard web-only overlay for busy Scene rendering queues.

Preserve the live Scene render worker and all jobs. Only patch the admin-safe
web route, persistent CSRF and content-addressed storage dashboard assets.
Runs under the existing protected root deployment lock.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from production.runtime import BASE, REPO, atomic_json, atomic_link, run

CURRENT = BASE / "current/scene-builder"
RELEASES = BASE / "releases/scene-builder"
TRANSACTIONS = BASE / "transactions/storage-dashboard-web"
SOURCE = REPO / "apps/scene-builder/part01"
UNIT = "vps1-scene-candidate.service"
WORKER_UNIT = "vps1-scene-production-worker.service"
BASE_FILES = (
    "part01/templates/storage_manager.html",
    "part01/static/storage_manager.js",
    "part01/static/storage_manager.css",
    "part01/static/shell-v2/manifest.json",
)


def require(condition: bool, explanation: str) -> None:
    if not condition:
        raise RuntimeError("STORAGE_DASHBOARD_WEB_BLOCKED: " + explanation)


def patch_web(original: str, reviewed: str) -> str:
    """Insert only reviewed GC endpoint and persistent admin CSRF, not all app.py."""
    marker = 'MANUAL_STORAGE_GC_REQUEST_DIR = Path("/run/vps1-storage-gc-manual")'
    next_route = '@app.get("/api/admin/storage/history")'
    require(reviewed.count(marker) == 1 and reviewed.count(next_route) == 1,
            "reviewed admin GC endpoint markers missing")
    start = reviewed.index(marker)
    end = reviewed.index(next_route, start)
    block = reviewed[start:end]
    require(block.count('@app.post("/api/admin/storage/run-safe-cleanup")') == 1
            and 'require_admin(request)' in block
            and 'require_storage_csrf(request)' in block
            and '"RUN SAFE CLEANUP"' in block
            and 'queue_guarded_storage_gc()' in block
            and 'os.O_EXCL' in block, "reviewed endpoint has drifted")
    require(original.count(next_route) == 1, "existing admin history route missing")
    if marker in original:
        require(original.count(marker) == 1
                and original.count('@app.post("/api/admin/storage/run-safe-cleanup")') == 1,
                "existing manual GC route has unexpected shape")
    else:
        # Local imports keep this overlay independent of the old file's import list.
        scoped = block.replace(
            '    """Queue a fixed systemd path request; never perform privileged deletion."""',
            '    """Queue a fixed systemd path request; never perform privileged deletion."""\n'
            '    import fcntl, stat, time')
        require('    import fcntl, stat, time' in scoped,
                "cannot scope manual request dependencies")
        original = original.replace(next_route, scoped + next_route, 1)
    persist = 'def persistent_workspace(request: Request):'
    require(original.count(persist) == 1, "persistent shell route missing")
    before = '"storage_csrf": "",'
    after = '"storage_csrf": request.session.setdefault("storage_csrf", shell_secrets.token_urlsafe(32)) if request.session.get("is_admin") else "",'
    if after not in original:
        require(original.count(before) == 1 and original.count('    import json as shell_json') >= 1,
                "existing persistent admin CSRF boundary differs")
        original = original.replace('    import json as shell_json',
                                    '    import json as shell_json\n    import secrets as shell_secrets', 1)
        original = original.replace(before, after, 1)
    require(original.count('@app.post("/api/admin/storage/run-safe-cleanup")') == 1
            and original.count(after) == 1, "non-idempotent storage admin overlay")
    return original


def trusted_ui_files() -> dict[str, bytes]:
    manifest = json.loads((SOURCE / "static/shell-v2/manifest.json").read_text())
    require(manifest.get("contract") == 2 and isinstance(manifest.get("assets"), list),
            "invalid reviewed shell manifest")
    assets = manifest["assets"]
    require(len(assets) == 5 and len(set(assets)) == 5
            and manifest.get("entry") == assets[0], "unexpected shell asset scope")
    files = {}
    for relative in BASE_FILES:
        src = REPO / "apps/scene-builder" / relative
        require(src.is_file() and not src.is_symlink(), "missing reviewed storage source")
        files[relative] = src.read_bytes()
    require(b'id="gc-manual-clean"' in files["part01/templates/storage_manager.html"]
            and b"run-safe-cleanup" in files["part01/static/storage_manager.js"],
            "manual button and request route are not paired")
    for asset in assets:
        require(isinstance(asset, str) and asset.startswith("/static/shell-v2/")
                and ".." not in asset, "untrusted shell asset reference")
        name = asset.removeprefix("/static/")
        relative = "part01/static/" + name
        src = REPO / "apps/scene-builder" / relative
        require(src.is_file() and not src.is_symlink(), "content-addressed asset absent")
        blob = src.read_bytes()
        digest = hashlib.sha256(blob).hexdigest()[:20]
        require(("." + digest + ".") in src.name, "content-addressed asset drift")
        files[relative] = blob
    return files


def worker_pid() -> str:
    pid = run(["systemctl", "show", "--property=MainPID", "--value", WORKER_UNIT], quiet=True).strip()
    require(pid.isdecimal() and int(pid) > 1, "independent Scene renderer not running")
    return pid


def install(revision: str) -> None:
    require(os.geteuid() == 0, "requires approved root helper")
    require(re.fullmatch(r"[a-f0-9]{40}", revision) is not None, "invalid release SHA")
    require(shutil.disk_usage("/").free >= 5_000_000_000, "operational disk reserve")
    require(CURRENT.is_symlink(), "Scene current pointer missing")
    old = CURRENT.resolve(strict=True)
    require(old.is_dir() and old.is_relative_to(RELEASES), "unowned Scene pointer")
    require(subprocess.run(["systemctl", "is-active", "--quiet", UNIT],
                           capture_output=True, timeout=10).returncode == 0,
            "Scene web service inactive")
    from production.scene_candidate_release import _health
    _health()
    renderer_pid = worker_pid()
    web = old / "part01/app.py"
    reviewed = SOURCE / "app.py"
    require(web.is_file() and not web.is_symlink()
            and reviewed.is_file() and not reviewed.is_symlink(),
            "Scene web source unavailable")
    patched = patch_web(web.read_text(encoding="utf-8"), reviewed.read_text(encoding="utf-8"))
    files = trusted_ui_files()
    files["part01/app.py"] = patched.encode("utf-8")
    manifest_path = old / "_scene_candidate_manifest.json"
    require(manifest_path.is_file() and not manifest_path.is_symlink(), "unowned release manifest")
    existing_manifest = json.loads(manifest_path.read_text())
    require(existing_manifest.get("app") == "scene-builder"
            and isinstance(existing_manifest.get("files"), dict),
            "existing Scene release manifest invalid")

    target = RELEASES / (revision + "-storage-dashboard-web")
    receipt = TRANSACTIONS / (revision + ".json")
    if CURRENT.resolve(strict=True) == target:
        require((target / "part01/app.py").read_bytes() == files["part01/app.py"],
                "already deployed pointer drift")
        return
    require(not target.exists() and not target.is_symlink() and not receipt.exists(),
            "release target or transaction already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    changed = False
    staged = False
    try:
        shutil.copytree(old, target, symlinks=True)
        staged = True
        for relative, blob in files.items():
            destination = target / relative
            require(not destination.is_symlink(), "target file symlink refused")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(blob)
            destination.chmod(0o644)
        manifest = dict(existing_manifest)
        manifest["revision"] = revision
        manifest["overlay"] = "storage-dashboard-web-only"
        manifest["files"] = {**existing_manifest["files"],
                             **{name: hashlib.sha256(blob).hexdigest()
                                for name, blob in files.items()}}
        atomic_json(target / "_scene_candidate_manifest.json", manifest, 0o644)
        # Verify web Python without importing production workers or touching queues.
        subprocess.run(["python3", "-m", "py_compile", str(target / "part01/app.py")],
                       check=True, capture_output=True, timeout=25)
        atomic_link(CURRENT, target)
        changed = True
        run(["systemctl", "restart", UNIT], quiet=True, timeout=75)
        _health()
        require(worker_pid() == renderer_pid, "Scene renderer PID changed during web release")
        require(CURRENT.resolve(strict=True) == target, "new web pointer did not persist")
        require((target / "part01/static/shell-v2/manifest.json").read_bytes()
                == files["part01/static/shell-v2/manifest.json"],
                "live shell manifest drift")
        receipt.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_json(receipt, {"revision": revision, "old": str(old), "new": str(target),
                              "renderer_pid": renderer_pid, "status": "ACTIVE",
                              "files": sorted(files)}, 0o600)
        print("STORAGE_DASHBOARD_WEB=PASS ADMIN_SAFE_CLEANUP=DEPLOYED "
              "RENDER_WORKER=UNTOUCHED USER_JOBS=UNTOUCHED AUTO_SCRIPT=OFFLINE", flush=True)
    except BaseException:
        if changed:
            atomic_link(CURRENT, old)
            run(["systemctl", "restart", UNIT], quiet=True, timeout=75)
            _health()
            require(worker_pid() == renderer_pid, "renderer changed during rollback")
            print("STORAGE_DASHBOARD_WEB_ROLLBACK=PASS JOBS=UNTOUCHED", flush=True)
        elif staged:
            shutil.rmtree(target)
        raise

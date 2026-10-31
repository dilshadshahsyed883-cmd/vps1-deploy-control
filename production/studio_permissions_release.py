"""Atomic 3-Studio permission rollout: Scene web overlay + two auth_request routes.

This is a protected-main, root-only, SHA-pinned and web-only rollout.
It never restarts or edits upload/scheduler/render workers or their queues.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from production.runtime import BASE, REPO, atomic_json, atomic_link, backup_databases, run
from production.scene_candidate_release import CURRENT, RELEASES, STATE, UNIT, _health

FILES = (
    "part01/app.py",
    "part01/templates/base.html",
    "part01/templates/user_management.html",
    "part01/static/vps1_native_site_shell.js",
    "part01/static/vps1_native_site_shell.css",
)
WORKER = "vps1-scene-production-worker.service"
JOURNALS = BASE / "transactions/three-studio-permissions"


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise RuntimeError("THREE_STUDIO_PERMISSIONS_BLOCKED: " + reason)


def worker_pid() -> str:
    pid = run(["systemctl", "show", "--property=MainPID", "--value", WORKER], quiet=True).strip()
    require(pid not in ("", "0"), "Scene render worker not ready")
    return pid


def healthy() -> None:
    require(subprocess.run(["systemctl", "is-active", "--quiet", UNIT],
                           capture_output=True, timeout=12).returncode == 0,
            "Scene candidate inactive")
    _health()


def validate_sources(source: dict[str, bytes]) -> None:
    app = source["part01/app.py"].decode("utf-8")
    ui = source["part01/templates/user_management.html"].decode("utf-8")
    shell = source["part01/static/vps1_native_site_shell.js"].decode("utf-8")
    css = source["part01/static/vps1_native_site_shell.css"].decode("utf-8")
    compile(app, "scene-three-studio-permissions.py", "exec")
    for needle in (
        "def allowed_studios(", '\"studio_access\": \"TEXT NOT NULL DEFAULT',
        'request.session["studio_access"] = allowed_studios(account)',
        "def studio_for_path(", "def workspace_session_auth(",
        'request.url.path.startswith("/api/experiment-browser/")',
        '\"short-uploader\", \"copyright-uploader\"',
    ):
        require(needle in app, "reviewed Scene auth source missing marker")
    for name in ("studio_song", "studio_short", "studio_copyright"):
        require(('name="' + name + '"') in ui, "User Management checkbox missing")
    require('identity.allowed_studios' in shell and 'experimentTabs.hidden = !isAdmin' in shell,
            "native sidebar does not enforce the verified role")
    require('nav[aria-label="Experiment Studio"][hidden]' in css,
            "non-admin Experiment nav could override hidden")
    require(b'Experiment Studio' in source["part01/templates/base.html"],
            "reviewed navigation source missing")


def preflight() -> str:
    require(os.geteuid() == 0, "root execution required")
    require(shutil.disk_usage("/").free >= 5_000_000_000, "5 GB disk reserve unavailable")
    require(CURRENT.is_symlink(), "Scene source pointer absent")
    previous = CURRENT.resolve(strict=True)
    require(previous.is_dir() and previous.is_relative_to(RELEASES),
            "Scene source pointer outside managed releases")
    healthy()
    pid = worker_pid()
    # Routes are reloaded only after all three uploader families are confirmed idle.
    # Queued jobs and user media are never deleted or rescheduled.
    from production.short_uploader_group_release import idle as short_idle
    from production.runtime import require_copyright_uploader_idle, require_lum_release_idle, DATA
    require_lum_release_idle(DATA / "local-upload-manager")
    require_copyright_uploader_idle(DATA / "copyright-uploader")
    short_idle()
    require(worker_pid() == pid, "Scene worker changed during preflight")
    return pid


def restart_web(expected_pid: str) -> None:
    run(["systemctl", "restart", UNIT], timeout=75, quiet=True)
    healthy()
    require(worker_pid() == expected_pid, "Scene render worker unexpectedly restarted")


def install(revision: str) -> None:
    require(re.fullmatch("[a-f0-9]{40}", revision) is not None, "invalid source SHA")
    pid = preflight()
    previous = CURRENT.resolve(strict=True)
    target = RELEASES / (revision + "-three-studio-permissions")
    journal = JOURNALS / revision
    source = {name: (REPO / "apps/scene-builder" / name).read_bytes() for name in FILES}
    validate_sources(source)
    require(not target.exists() and not target.is_symlink()
            and not journal.exists(), "immutable release or transaction already exists")
    changed = False
    short_receipt = None
    copyright_receipt = None
    journal.mkdir(mode=0o700, parents=True)
    atomic_json(journal / "previous.json", {
        "revision": revision, "current": str(previous), "worker_pid": pid,
    })
    backup_databases(STATE, journal / "sqlite-backup")
    try:
        shutil.copytree(previous, target, symlinks=True)
        manifest_path = target / "_scene_candidate_manifest.json"
        require(manifest_path.is_file() and not manifest_path.is_symlink(),
                "Scene source manifest missing")
        manifest = json.loads(manifest_path.read_text())
        require(isinstance(manifest.get("files"), dict), "Scene manifest invalid")
        for name, data in source.items():
            destination = target / name
            require(destination.is_file() and not destination.is_symlink()
                    and name in manifest["files"], "Scene overlay path drift: " + name)
            destination.write_bytes(data)
            manifest["files"][name] = hashlib.sha256(data).hexdigest()
        manifest.update(revision=revision, overlay="three-studio-permissions",
                        overlay_previous=str(previous))
        atomic_json(manifest_path, manifest, 0o644)
        preflight()
        atomic_link(CURRENT, target)
        changed = True
        restart_web(pid)
        from production.scene_candidate_release import _verify_integrations
        _verify_integrations()
        from production.short_studio_release import install_route as short_route
        from production.copyright_studio_release import install_route as copyright_route
        short_receipt = short_route(revision)
        copyright_receipt = copyright_route(revision)
        from production.route_uploader_scene_https import scene_cookie, https_get
        owner_cookie = scene_cookie()
        for url, marker in (
            ("/user-management", b"Song Studio"),
            ("/song-studio/", b"Song Studio"),
            ("/short-maker/", b"Short"),
            ("/video-merger/", b"Video Merger"),
        ):
            status, _, body = https_get(url, owner_cookie)
            require(status == 200 and marker in body, "authenticated Studio probe failed: " + url)
        for url in ("/user-management", "/short-uploader/app", "/copyright-uploader/app"):
            status, _, _ = https_get(url)
            require(status in (303, 401, 403), "anonymous Studio access exposed: " + url)
        require(worker_pid() == pid, "Scene worker changed after release")
        atomic_json(journal / "receipt.json", {
            "revision": revision, "before": str(previous), "after": str(target),
            "worker_pid": pid, "state": "ACTIVE",
            "short_route_changed": short_receipt is not None,
            "copyright_route_changed": copyright_receipt is not None,
        })
        print("THREE_STUDIO_PERMISSIONS=PASS USERS=RETAINED UPLOAD_WORKERS=UNTOUCHED AUTO_SCRIPT=OFFLINE", flush=True)
    except BaseException:
        try:
            if copyright_receipt is not None:
                from production.copyright_studio_release import restore_route
                restore_route(copyright_receipt)
            if short_receipt is not None:
                from production.short_studio_release import restore_route
                restore_route(short_receipt)
        finally:
            if changed:
                atomic_link(CURRENT, previous)
                restart_web(pid)
        raise


def rollback(revision: str) -> None:
    require(re.fullmatch("[a-f0-9]{40}", revision) is not None, "invalid revision")
    journal = JOURNALS / revision
    receipt_path = journal / "receipt.json"
    require(receipt_path.is_file() and not receipt_path.is_symlink(), "release receipt unavailable")
    receipt = json.loads(receipt_path.read_text())
    require(receipt.get("revision") == revision and receipt.get("state") == "ACTIVE",
            "release receipt invalid")
    require(CURRENT.is_symlink() and str(CURRENT.resolve(strict=True)) == receipt["after"],
            "Scene has changed since release")
    pid = worker_pid()
    require(pid == receipt["worker_pid"], "render worker changed since release")
    from production.short_studio_release import restore_route as restore_short
    from production.copyright_studio_release import restore_route as restore_copyright
    from production.short_studio_release import SITE, ROUTE as SHORT_ROUTE, LEGACY_ROUTE as SHORT_LEGACY
    from production.copyright_studio_release import ROUTE as COPYRIGHT_ROUTE, LEGACY_ROUTE as COPYRIGHT_LEGACY
    before_site = SITE.read_text()
    after_site = before_site
    if receipt["copyright_route_changed"]:
        require(after_site.count(COPYRIGHT_ROUTE) == 1, "Copyright route changed externally")
        after_site = after_site.replace(COPYRIGHT_ROUTE, COPYRIGHT_LEGACY, 1)
    if receipt["short_route_changed"]:
        require(after_site.count(SHORT_ROUTE) == 1, "Short route changed externally")
        after_site = after_site.replace(SHORT_ROUTE, SHORT_LEGACY, 1)
    # Nginx route rollback is all-or-nothing; perform one validated reload.
    if after_site != before_site:
        from production.short_studio_release import write_site, command
        write_site(after_site)
        try:
            command("nginx", "-t")
            command("systemctl", "reload", "nginx")
        except BaseException:
            write_site(before_site)
            command("nginx", "-t")
            command("systemctl", "reload", "nginx")
            raise
    previous = Path(receipt["before"])
    require(previous.is_dir() and previous.is_relative_to(RELEASES), "previous Scene source lost")
    atomic_link(CURRENT, previous)
    restart_web(pid)
    atomic_json(receipt_path, {**receipt, "state": "ROLLED_BACK"})
    print("THREE_STUDIO_PERMISSIONS=ROLLED_BACK USER_DATA=RETAINED", flush=True)

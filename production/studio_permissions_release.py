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


def copyright_web_idle(state: Path) -> None:
    """Check active transfers, not parked/manual queue rows; no worker restart."""
    import sqlite3
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open("http://127.0.0.1:8020/copyright-uploader/health", timeout=5) as response:
            require(response.status == 200, "Copyright health unavailable")
            health = json.loads(response.read(8192))
    except (OSError, ValueError) as exc:
        raise RuntimeError("Copyright health preflight unavailable") from exc
    require(health.get("ok") is True and health.get("engine_instance") == "copyright",
            "Copyright independent service identity mismatch")
    require(health.get("active_project_id") in (None, "", 0),
            "Copyright web upload in progress")
    core = state / "lum-copyright/core/data/uploader.db"
    require(core.is_file() and not core.is_symlink(), "Copyright uploader DB missing")
    with sqlite3.connect(core.as_uri() + "?mode=ro", uri=True, timeout=5) as conn:
        conn.execute("PRAGMA query_only=ON")
        busy = conn.execute(
            """SELECT 1 FROM videos WHERE UPPER(status) IN
               ('CLAIMED','UPLOADING','THUMBNAIL_UPLOADING','PROCESSING','IN_PROGRESS')
               LIMIT 1"""
        ).fetchone()
        require(not busy, "Copyright video transfer in progress")
    project_db = state / "lum-copyright/projects.sqlite3"
    require(project_db.is_file() and not project_db.is_symlink(),
            "Copyright projects DB missing")
    with sqlite3.connect(project_db.as_uri() + "?mode=ro", uri=True, timeout=5) as conn:
        conn.execute("PRAGMA query_only=ON")
        busy = conn.execute(
            """SELECT 1 FROM projects WHERE UPPER(status) IN
               ('CLAIMED','UPLOADING','PROCESSING','IN_PROGRESS','MERGING',
                'RENDERING','CLEANUP','VERIFYING') LIMIT 1"""
        ).fetchone()
        require(not busy, "Copyright project transfer/processing in progress")


def preflight() -> str:
    require(os.geteuid() == 0, "root execution required")
    require(shutil.disk_usage("/").free >= 5_000_000_000, "5 GB disk reserve unavailable")
    require(CURRENT.is_symlink(), "Scene source pointer absent")
    previous = CURRENT.resolve(strict=True)
    require(previous.is_dir() and previous.is_relative_to(RELEASES),
            "Scene source pointer outside managed releases")
    healthy()
    pid = worker_pid()
    # This is a web-only permission cutover: pending/waiting media stays queued.
    # Fail closed on an active upload, claimed transfer or unknown DB/service state.
    from production.short_uploader_group_release import idle as short_idle
    from production.runtime import require_lum_release_idle, DATA
    require_lum_release_idle(DATA / "local-upload-manager")
    copyright_web_idle(DATA / "copyright-uploader")
    short_idle(allow_waiting_network=True)
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
    site_changed = False
    site_before = None
    site_after = None
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
        # One Nginx reload, one full-site CAS receipt, one rollback: never
        # leave half-updated Short/Copyright auth_request locations.
        from production.short_studio_release import (
            SITE, ENABLED, apply_route as short_apply, verify_local as short_local,
            verify_https_route as verify_short, write_site, command,
        )
        from production.copyright_studio_release import (
            apply_route as copyright_apply, verify_local as copyright_local,
            verify_https_route as verify_copyright,
        )
        require(SITE.is_file() and ENABLED.is_symlink() and ENABLED.resolve() == SITE,
                "trusted existing HTTPS site unavailable")
        short_local()
        copyright_local()
        site_before = SITE.read_text()
        site_after = copyright_apply(short_apply(site_before))
        if site_after != site_before:
            (journal / "before-nginx.conf").write_text(site_before)
            (journal / "before-nginx.conf").chmod(0o600)
            require(SITE.read_text() == site_before, "concurrent Nginx edit")
            write_site(site_after)
            site_changed = True
            command("nginx", "-t")
            command("systemctl", "reload", "nginx")
        from production.route_uploader_scene_https import scene_cookie, https_get
        verify_short(https_get, scene_cookie)
        verify_copyright(https_get, scene_cookie)
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
            "site_changed": site_changed,
            "site_before_sha256": hashlib.sha256(site_before.encode()).hexdigest(),
            "site_after_sha256": hashlib.sha256(site_after.encode()).hexdigest(),
        })
        print("THREE_STUDIO_PERMISSIONS=PASS USERS=RETAINED UPLOAD_WORKERS=UNTOUCHED AUTO_SCRIPT=OFFLINE", flush=True)
    except BaseException:
        try:
            if site_changed:
                from production.short_studio_release import SITE, write_site, command
                require(SITE.read_text() == site_after,
                        "concurrent Nginx edit prevents automatic rollback")
                write_site(site_before)
                command("nginx", "-t")
                command("systemctl", "reload", "nginx")
        finally:
            if changed:
                atomic_link(CURRENT, previous)
                restart_web(pid)
        raise

def rollback(revision: str) -> None:
    require(re.fullmatch("[a-f0-9]{40}", revision) is not None, "invalid revision")
    journal = JOURNALS / revision
    receipt_path = journal / "receipt.json"
    require(receipt_path.is_file() and not receipt_path.is_symlink(),
            "release receipt unavailable")
    receipt = json.loads(receipt_path.read_text())
    require(receipt.get("revision") == revision and receipt.get("state") == "ACTIVE",
            "release receipt invalid")
    require(CURRENT.is_symlink() and str(CURRENT.resolve(strict=True)) == receipt["after"],
            "Scene has changed since release")
    pid = worker_pid()
    require(pid == receipt["worker_pid"], "render worker changed since release")
    from production.short_studio_release import SITE, write_site, command
    current_site = SITE.read_text()
    require(hashlib.sha256(current_site.encode()).hexdigest() ==
            receipt["site_after_sha256"], "Nginx site changed externally")
    if receipt["site_changed"]:
        before_path = journal / "before-nginx.conf"
        require(before_path.is_file() and not before_path.is_symlink(),
                "original Nginx receipt missing")
        previous_site = before_path.read_text()
        require(hashlib.sha256(previous_site.encode()).hexdigest() ==
                receipt["site_before_sha256"], "original Nginx content drift")
    else:
        previous_site = current_site
    previous = Path(receipt["before"])
    require(previous.is_dir() and previous.is_relative_to(RELEASES),
            "previous Scene source lost")
    if receipt["site_changed"]:
        write_site(previous_site)
        try:
            command("nginx", "-t")
            command("systemctl", "reload", "nginx")
        except BaseException:
            write_site(current_site)
            command("nginx", "-t")
            command("systemctl", "reload", "nginx")
            raise
    atomic_link(CURRENT, previous)
    try:
        restart_web(pid)
    except BaseException:
        # Restore the new Scene source if reversing it failed.
        atomic_link(CURRENT, Path(receipt["after"]))
        restart_web(pid)
        raise
    atomic_json(receipt_path, {**receipt, "state": "ROLLED_BACK"})
    print("THREE_STUDIO_PERMISSIONS=ROLLED_BACK USER_DATA=RETAINED", flush=True)

#!/usr/bin/env python3
"""Update the existing Scene candidate through the production app allowlist.

The public Scene service predates the generic production installer and owns a
different unit, environment and state directory.  This adapter changes only its
immutable source pointer, preserves those resources, and proves the new session
bridge plus the existing Script Work and Storage Manager pages before commit.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import urllib.error
import urllib.request


BASE = Path("/srv/fresh-website")
CURRENT = BASE / "current/scene-builder"
RELEASES = BASE / "releases/scene-builder"
STATE = Path("/var/lib/fresh-website/scene-builder")
ENV = Path("/etc/fresh-website/scene-builder.env")
UNIT = "vps1-scene-candidate.service"
RECORD = BASE / "control/state/scene-candidate.json"


def _request(path: str, cookie: str = "") -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(
        "http://127.0.0.1:18080" + path,
        headers={"Cookie": cookie, "Connection": "close"} if cookie else {"Connection": "close"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=12) as response:
            return response.status, dict(response.headers.items()), response.read(300000)
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()), exc.read(300000)


def _health(timeout: float = 50) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status, _, body = _request("/health")
            payload = json.loads(body)
            if status == 200 and payload.get("ok") is True:
                return payload
        except (OSError, ValueError):
            pass
        time.sleep(.5)
    raise RuntimeError("Scene candidate health did not recover")


def _stage(revision: str) -> Path:
    from rebuild.fresh_vps import sources
    from production.runtime import atomic_json

    target = RELEASES / revision
    files = sources("scene-builder")
    manifest = {
        "app": "scene-builder",
        "revision": revision,
        "files": {name: hashlib.sha256(content).hexdigest() for name, content in files},
    }
    manifest_path = target / "_scene_candidate_manifest.json"
    if target.exists():
        if not manifest_path.is_file() or json.loads(manifest_path.read_text()) != manifest:
            raise RuntimeError("existing Scene candidate release is not owned by this adapter")
        for name, digest in manifest["files"].items():
            if hashlib.sha256((target / name).read_bytes()).hexdigest() != digest:
                raise RuntimeError("Scene candidate immutable source drift")
        return target
    target.mkdir(parents=True)
    try:
        for name, content in files:
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            path.chmod(0o644)
        atomic_json(manifest_path, manifest, 0o644)
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return target


def _verify_integrations() -> dict:
    from production.route_uploader_scene_https import scene_cookie

    cookie = scene_cookie()
    status, headers, _ = _request("/internal/workspace-auth?app=short-maker", cookie)
    normalized = {key.lower(): value for key, value in headers.items()}
    if status != 204 or not normalized.get("x-auth-user") or normalized.get("x-is-admin") != "1":
        raise RuntimeError("Scene administrator session bridge is unavailable")
    anonymous, _, _ = _request("/internal/workspace-auth?app=short-maker")
    if anonymous != 401:
        raise RuntimeError("Scene session bridge does not deny anonymous access")
    pages = {}
    for name, path in (("script_work", "/script-work"), ("storage_manager", "/admin/storage")):
        code, response_headers, body = _request(path, cookie)
        if code not in {200, 302, 303, 307, 308}:
            raise RuntimeError(name + " authenticated page unavailable")
        if code == 200 and b"<" not in body:
            raise RuntimeError(name + " response is not a browser document")
        pages[name] = code
    return {"session_bridge": "PASS", **pages}


def install(revision: str) -> None:
    from production.browser_runtime import install as install_browser
    from production.runtime import atomic_json, atomic_link, backup_databases, event, run

    if not CURRENT.is_symlink() or not ENV.is_file() or not STATE.is_dir():
        raise RuntimeError("existing Scene candidate ownership is incomplete")
    previous = CURRENT.resolve(strict=True)
    if not previous.is_relative_to(RELEASES):
        raise RuntimeError("existing Scene candidate pointer escaped its release root")
    if subprocess.run(["systemctl", "is-active", "--quiet", UNIT]).returncode:
        raise RuntimeError("existing Scene candidate service is inactive")
    _health()
    target = _stage(revision)
    if previous == target:
        integrations = _verify_integrations()
        event(app="scene-builder", result="PASS_ALREADY_INSTALLED", revision=revision,
              port=18080, integrations=integrations)
        return

    journal = BASE / "transactions/scene-candidate" / (revision + "-" + str(time.time_ns()))
    journal.mkdir(parents=True)
    atomic_json(journal / "previous.json", {"current": str(previous), "unit": UNIT})
    backups = backup_databases(STATE, journal / "sqlite-backup")
    changed = False
    try:
        atomic_link(CURRENT, target)
        changed = True
        run(["systemctl", "restart", UNIT], timeout=70, quiet=True)
        health = _health()
        integrations = _verify_integrations()
        # Idempotent dedicated Chrome/noVNC setup used by the existing Script Work UI.
        install_browser()
        integrations = _verify_integrations()
        if shutil.disk_usage("/").free < 5_000_000_000:
            raise RuntimeError("operational disk reserve breached")
        atomic_json(RECORD, {"revision": revision, "status": "ACTIVE", "unit": UNIT,
                            "previous": str(previous), "transaction": str(journal)})
        event(app="scene-builder", result="PASS_CANDIDATE_SOURCE_AND_SSO", revision=revision,
              port=18080, health=health, integrations=integrations,
              sqlite_backups=len(backups), rollback=str(journal))
    except BaseException:
        if changed:
            atomic_link(CURRENT, previous)
            subprocess.run(["systemctl", "restart", UNIT], capture_output=True, timeout=70)
            _health()
            event(app="scene-builder", rollback="PASS", data="PRESERVED")
        raise


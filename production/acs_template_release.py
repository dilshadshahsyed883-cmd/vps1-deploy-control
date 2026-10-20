"""Worker-safe Auto Caption Studio template-only release.

This path changes only the browser-facing ACS HTML asset cache key. It never
calls the generic app installer, never invokes workers.stop/install, and keeps
ACS data, queues, render/compression/upload workers, and credentials unchanged.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import urllib.request

from production.runtime import BASE, REPO, atomic_json, atomic_link, event, http_check, run

APP = "auto-caption-studio"
CURRENT = BASE / "current/auto-caption-studio"
RELEASES = BASE / "releases/auto-caption-studio"
UNIT = "vps1-production-auto-caption-studio.service"
PAGE = Path("part01/web_backend/acs_studio.html")
JOURNALS = BASE / "transactions/acs-template-only"
TITLE = b"<title>Auto Caption Studio</title>"
OLD_ASSETS = (
    b'<link rel="stylesheet" href="/&#115;tatic/vps1_native_site_shell.css?v=20261020-upload-studio-v1">\n'
    b'<script src="/&#115;tatic/vps1_native_site_shell.js?v=20261020-upload-studio-v1" data-workspace="acs" defer></script>'
)
NEW_ASSETS = (
    b'<link rel="stylesheet" href="/&#115;tatic/vps1_native_site_shell.css?v=20261020-cw003-rail-v2">\n'
    b'<script src="/&#115;tatic/vps1_native_site_shell.js?v=20261020-cw003-rail-v2" data-workspace="acs" defer></script>'
)
ASSET_NOTE = b"<!-- VPS1 ACS native shell assets cache-busted by CW-003. -->"


def require(ok: bool, why: str) -> None:
    if not ok:
        raise RuntimeError("ACS_TEMPLATE_RELEASE_BLOCKED: " + why)


def patch_template(payload: bytes) -> bytes:
    require(payload.count(TITLE) == 1, "ACS template title changed")
    if payload.count(NEW_ASSETS) == 1 and payload.count(OLD_ASSETS) == 0:
        require(payload.count(ASSET_NOTE) == 1, "ACS cache-bust note missing")
        return payload
    require(payload.count(OLD_ASSETS) == 1 and payload.count(NEW_ASSETS) == 0,
            "expected one approved ACS shell asset pair")
    updated = payload.replace(OLD_ASSETS, ASSET_NOTE + b"\n" + NEW_ASSETS, 1)
    require(updated.count(NEW_ASSETS) == 1 and updated.count(OLD_ASSETS) == 0,
            "ACS shell asset cache-bust failed")
    require(updated.count(TITLE) == 1, "ACS template title changed during patch")
    return updated


def verify_runtime() -> None:
    http_check(APP)
    from production.route_uploader_scene_https import https_get, scene_cookie
    cookie = scene_cookie()
    code, kind, html = https_get("/auto-caption-studio/", cookie)
    require(code == 200 and "html" in kind.lower()
            and NEW_ASSETS in html and OLD_ASSETS not in html
            and b'id="acs-native-live"' in html,
            "authenticated public ACS template guard not updated")
    anonymous, _, _ = https_get("/auto-caption-studio/")
    require(anonymous in {401, 403, 303},
            "anonymous ACS page became accessible")


def install(revision: str) -> None:
    require(len(revision) == 40 and all(c in "0123456789abcdef" for c in revision),
            "invalid approved immutable SHA")
    require(CURRENT.is_symlink(), "current ACS release is not an approved symlink")
    previous = CURRENT.resolve(strict=True)
    require(previous.is_relative_to(RELEASES) and previous.is_dir(),
            "current ACS pointer is not production-owned")
    require((previous / PAGE).is_file() and not (previous / PAGE).is_symlink(),
            "previous ACS template missing or linked")
    active = subprocess.run(["systemctl", "is-active", "--quiet", UNIT],
                            check=False, capture_output=True).returncode == 0
    require(active, "ACS web service unavailable")
    http_check(APP)

    source = REPO / "apps/auto-caption-studio" / PAGE
    require(source.is_file() and not source.is_symlink(),
            "trusted ACS template source missing")
    source_html = source.read_bytes()
    require(NEW_ASSETS in source_html and source_html.count(NEW_ASSETS) == 1,
            "approved ACS cache-bust markers missing")
    original_html = (previous / PAGE).read_bytes()
    patched_html = patch_template(original_html)

    target = RELEASES / (revision + "-acs-template-only")
    if previous == target:
        verify_runtime()
        event(app=APP, result="PASS_ACS_TEMPLATE_ALREADY_LIVE",
              workers="PRESERVED", data="UNCHANGED")
        return
    require(not target.exists() and not target.is_symlink(),
            "immutable ACS UI target already exists")
    journal = JOURNALS / revision
    require(not journal.exists(), "ACS UI release journal already exists")
    journal.mkdir(parents=True, mode=0o700)
    atomic_json(journal / "previous.json", {
        "old_release": str(previous),
        "old_template_sha256": hashlib.sha256(original_html).hexdigest(),
        "new_template_sha256": hashlib.sha256(patched_html).hexdigest(),
        "app": APP, "revision": revision,
        "workers": "unchanged", "queues": "unchanged", "data": "unchanged",
    })
    shutil.copytree(previous, target, symlinks=True)
    (target / PAGE).write_bytes(patched_html)
    manifest_file = target / "_production_manifest.json"
    require(manifest_file.is_file() and not manifest_file.is_symlink(),
            "copied ACS production manifest missing")
    manifest = json.loads(manifest_file.read_text())
    require(manifest.get("app") == APP and str(PAGE) in manifest.get("files", {}),
            "copied ACS production manifest invalid")
    manifest["revision"] = revision
    manifest["files"][str(PAGE)] = hashlib.sha256(patched_html).hexdigest()
    manifest["ui_only_overlay"] = str(previous)
    atomic_json(manifest_file, manifest, 0o644)

    switched = False
    try:
        atomic_link(CURRENT, target)
        switched = True
        # Restart only the browser-facing ACS web process so Jinja reloads the
        # template. Rendering/compression/upload workers and all data stay live.
        run(["systemctl", "restart", UNIT], timeout=60, quiet=True)
        verify_runtime()
        atomic_json(journal / "result.json", {
            "revision": revision, "state": "ACTIVE", "workers": "PRESERVED",
            "queues": "PRESERVED", "data": "PRESERVED",
            "ui": "PASS", "public_https": "PASS",
        })
        event(app=APP, result="PASS_ACS_TEMPLATE_ONLY",
              workers="PRESERVED", queues="PRESERVED", data="UNCHANGED",
              rollback=str(journal))
    except BaseException:
        if switched:
            atomic_link(CURRENT, previous)
            run(["systemctl", "restart", UNIT], timeout=60, quiet=True)
            http_check(APP)
            event(app=APP, rollback="PASS", workers="UNCHANGED",
                  queues="PRESERVED", data="PRESERVED")
        raise

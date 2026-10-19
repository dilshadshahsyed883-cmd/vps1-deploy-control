"""VPS1 Voice Studio sidebar-only release: immutable HTML overlay, no worker restarts.

The protected-main production installer dispatches here only for the explicit
[vps1-voice-template-only] marker. A locked, authenticated GitHub/Tailscale
operation owns the deployment. All Voice API, engine code, OAuth, queue storage,
worker services and systemd worker states remain unchanged.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time
import urllib.request

from production.runtime import BASE, REPO, atomic_json, atomic_link, event, http_check, run

APP = "voice-studio"
CURRENT = BASE / "current/voice-studio"
RELEASES = BASE / "releases/voice-studio"
UNIT = "vps1-production-voice-studio.service"
PAGE = Path("part01/templates/voice_studio.html")
JOURNALS = BASE / "transactions/voice-sidebar-only"
TITLE = b"<title>Voice Studio \xc2\xb7 VPS1</title>"
OLD_ASSETS = (
    b'<link rel="stylesheet" href="/&#115;tatic/vps1_native_site_shell.css?v=1">\n'
    b'<script src="/&#115;tatic/vps1_native_site_shell.js?v=1" data-workspace="voice" defer></script>'
)
NEW_ASSETS = (
    b'<link rel="stylesheet" href="/&#115;tatic/vps1_native_site_shell.css?v=20261020-upload-studio-v1">\n'
    b'<script src="/&#115;tatic/vps1_native_site_shell.js?v=20261020-upload-studio-v1" data-workspace="voice" defer></script>'
)
PREVIOUS_ASSETS = (
    b'<link rel="stylesheet" href="/&#115;tatic/vps1_native_site_shell.css?v=20261019-voice-nav-v2">\n'
    b'<script src="/&#115;tatic/vps1_native_site_shell.js?v=20261019-voice-nav-v2" data-workspace="voice" defer></script>'
)
ASSET_NOTE = b"<!-- VPS1 Voice native sidebar assets loaded before large inline CSS. -->"


def require(ok: bool, why: str) -> None:
    if not ok:
        raise RuntimeError("VOICE_UI_RELEASE_BLOCKED: " + why)


def patch_template(payload: bytes) -> bytes:
    """Only relocate two trusted asset links; preserve all other Voice markup."""
    require(payload.count(TITLE) == 1, "existing Voice template title changed")
    # The last approved Voice sidebar-only release already moved assets above
    # the large inline stylesheet. Upgrade that exact version in place without
    # touching engine controls, worker services or the rest of the HTML.
    if payload.count(PREVIOUS_ASSETS) == 1 and payload.count(ASSET_NOTE) == 1:
        require(payload.count(OLD_ASSETS) == 0 and payload.count(NEW_ASSETS) == 0,
                "mixed Voice sidebar assets")
        updated = payload.replace(PREVIOUS_ASSETS, NEW_ASSETS, 1)
        require(updated.count(NEW_ASSETS) == 1 and
                b'id="vsEngineOmni"' in updated and
                b'id="vsEngineKokoro"' in updated,
                "Voice sidebar upgrade failed")
        return updated
    require(payload.count(OLD_ASSETS) == 1, "expected old native CSS/JS links not found")
    require(payload.count(b'vps1_native_site_shell.js?v=20261020-upload-studio-v1') == 0,
            "Voice template already patched")
    updated = payload.replace(OLD_ASSETS, ASSET_NOTE, 1)
    updated = updated.replace(TITLE, TITLE + b"\n" + NEW_ASSETS, 1)
    require(updated.count(NEW_ASSETS) == 1 and updated.count(OLD_ASSETS) == 0,
            "Voice template asset relocation failed")
    require(b'id="vsEngineOmni"' in updated and b'id="vsEngineKokoro"' in updated,
            "Voice engine UI controls unexpectedly missing")
    return updated


def verify_runtime() -> None:
    """Read the actual web document locally, then verify authenticated HTTPS."""
    http_check(APP)
    address = "http://127.0.0.1:8143/app"
    with urllib.request.urlopen(address, timeout=15) as response:
        body = response.read(500000)
        require(response.status == 200 and "html" in response.headers.get("Content-Type", "").lower(),
                "Voice web UI HTTP response unavailable")
        require(NEW_ASSETS in body and b'id="vsEngineOmni"' in body,
                "Voice web UI is still serving old native assets")
    from production.route_uploader_scene_https import https_get, scene_cookie
    cookie = scene_cookie()
    code, kind, html = https_get("/voice-studio/app", cookie)
    require(code == 200 and "html" in kind.lower()
            and b"vps1_native_site_shell.js?v=20261020-upload-studio-v1" in html,
            "authenticated public Voice page not updated")
    anonymous, _, _ = https_get("/voice-studio/app")
    require(anonymous in {401, 403, 303}, "anonymous Voice page became accessible")


def install(revision: str) -> None:
    require(len(revision) == 40 and all(c in "0123456789abcdef" for c in revision),
            "invalid approved immutable SHA")
    require(CURRENT.is_symlink(), "current Voice release not an approved symlink")
    previous = CURRENT.resolve(strict=True)
    require(previous.is_relative_to(RELEASES) and previous.is_dir(),
            "current Voice pointer not owned by production releases")
    require((previous / PAGE).is_file() and not (previous / PAGE).is_symlink(),
            "previous Voice HTML missing or linked")
    active = subprocess.run(["systemctl", "is-active", "--quiet", UNIT],
                            check=False, capture_output=True).returncode == 0
    require(active, "current Voice web service unavailable")
    http_check(APP)

    source = (REPO / "apps/voice-studio" / PAGE)
    require(source.is_file() and not source.is_symlink(),
            "trusted Voice template source missing")
    source_html = source.read_bytes()
    require(NEW_ASSETS in source_html, "approved GitHub Voice template marker missing")
    original_html = (previous / PAGE).read_bytes()
    patched_html = patch_template(original_html)

    target = RELEASES / (revision + "-sidebar-only")
    if previous == target:
        verify_runtime()
        event(app=APP, result="PASS_VOICE_UI_ALREADY_LIVE", workers="PRESERVED")
        return
    require(not target.exists() and not target.is_symlink(),
            "immutable Voice UI target already exists")
    journal = JOURNALS / revision
    require(not journal.exists(), "Voice UI release journal already exists")
    journal.mkdir(parents=True, mode=0o700)
    atomic_json(journal / "previous.json", {
        "old_release": str(previous),
        "old_template_sha256": hashlib.sha256(original_html).hexdigest(),
        "new_template_sha256": hashlib.sha256(patched_html).hexdigest(),
        "app": APP, "revision": revision, "workers": "unchanged",
    })
    shutil.copytree(previous, target, symlinks=True)
    (target / PAGE).write_bytes(patched_html)
    manifest_file = target / "_production_manifest.json"
    require(manifest_file.is_file() and not manifest_file.is_symlink(),
            "copied immutable release manifest missing")
    manifest = json.loads(manifest_file.read_text())
    require(manifest.get("app") == APP and str(PAGE) in manifest.get("files", {}),
            "copied Voice release manifest invalid")
    manifest["revision"] = revision
    manifest["files"][str(PAGE)] = hashlib.sha256(patched_html).hexdigest()
    manifest["ui_only_overlay"] = str(previous)
    atomic_json(manifest_file, manifest, 0o644)
    switched = False
    try:
        atomic_link(CURRENT, target)
        switched = True
        # Only the browser-facing Voice web process reloads Jinja templates.
        # Never stop/restart voice workers or touch job databases here.
        run(["systemctl", "restart", UNIT], timeout=60, quiet=True)
        verify_runtime()
        atomic_json(journal / "result.json", {
            "revision": revision, "state": "ACTIVE", "workers": "PRESERVED",
            "ui": "PASS", "public_https": "PASS",
        })
        event(app=APP, result="PASS_VOICE_TEMPLATE_ONLY",
              workers="PRESERVED", data="UNCHANGED", rollback=str(journal))
    except BaseException:
        if switched:
            atomic_link(CURRENT, previous)
            run(["systemctl", "restart", UNIT], timeout=60, quiet=True)
            http_check(APP)
            event(app=APP, rollback="PASS", workers="UNCHANGED", data="PRESERVED")
        raise

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
    "templates/base.html",
    "templates/acs_studio_shell.html",
    "templates/video_studio_embed.html",
    "templates/dashboard.html",
    "templates/storage_manager.html",
    "static/dashboard_control_center.js",
    "static/storage_manager.js",
    "static/vps1_native_site_shell.js",
    "static/vps1_native_site_shell.css",
    "app.py",
    "song_uploader_alias.py",
)
# A new wrapper did not exist in older Scene releases; all other files did.
NEW_FILES = frozenset({"templates/video_studio_embed.html",
                       "static/vps1_native_site_shell.js",
                       "static/vps1_native_site_shell.css",
                       "song_uploader_alias.py"})


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
    base = (SOURCE / "templates/base.html").read_text()
    acs = (SOURCE / "templates/acs_studio_shell.html").read_text()
    app = (SOURCE / "app.py").read_text()
    wrapper = (SOURCE / "templates/video_studio_embed.html").read_text()
    require('href="/auto-caption-studio/"' in base and
            'href="/short-maker"' in base and
            'href="/video-merger/"' in base and
            'href="/local-upload-manager/app?embedded=1&amp;tab=dashboard"' in base and
            'href="/experiment-studio/"' in base and
            'href="/auto-script"' not in base,
            "original Scene navigation or Video Studio subtabs regressed")
    require('href="/auto-caption-studio/"' in acs
            and "<iframe" not in acs, "ACS fallback still embeds full document")
    require('href="{{ studio_url }}"' in wrapper and "<iframe" not in wrapper,
            "Video Studio fallback still embeds full document")
    require('RedirectResponse("/auto-caption-studio/"' in app
            and 'RedirectResponse("/local-upload-manager/app?embedded=1&tab=dashboard"' in app
            and 'RedirectResponse("/voice-studio/app"' in app
            and "return RedirectResponse(url, status_code=303)" in app
            and "/video-studio/{studio_app}" in app
            and "require_admin(request)" in app,
            "single-document routing or admin authorization missing")
    native = (SOURCE / "static/vps1_native_site_shell.js").read_text()
    require("vps1-native-site-sidebar" in native and "iframe" in native,
            "native website shell source missing")



def preflight_scene_proxy_dependency() -> None:
    """Probe/install the existing approved pinned httpx dependency before UI flip.

    The Scene candidate imports httpx at app-module load. This install is
    scoped to its existing pinned venv; if it fails the pointer stays intact.
    """
    from production.scene_candidate_release import _ensure_song_uploader_dependency
    _ensure_song_uploader_dependency()


def verify_video_studio() -> None:
    """Read back native UI documents, not merely an outer embedded iframe."""
    from production.route_uploader_scene_https import scene_cookie, https_get
    cookie = scene_cookie()
    route_destinations = {
        "/local-upload-manager": "/local-upload-manager/app?embedded=1&tab=dashboard",
        "/auto-caption/studio": "/auto-caption-studio/",
        "/video-studio/video-merger": "/video-merger/",
        "/voice-studio": "/voice-studio/app",
    }
    for route in route_destinations:
        for attempt in range(12):
            status, kind, _body = https_get(route, cookie)
            if status == 303:
                print("SCENE_NATIVE_ROUTE_PASS=" + route, flush=True)
                break
            transient = status in (200, 301, 302, 307, 308, 404, 502, 503)
            if transient and attempt < 11:
                time.sleep(0.6)
                continue
            require(False, "Scene native redirect failed path=" + route +
                    " status=" + str(status))
        else:
            require(False, "Scene native route never settled: " + route)
    markers = {
        "/local-upload-manager/app?embedded=1&tab=dashboard":
            (b'lum-shell', b'vps1_native_site_shell.js'),
        "/auto-caption-studio/":
            (b'data-page="queue"', b'data-page="upload"', b'data-page="voice"',
             b'id="acs-native-live"', b'vps1_native_site_shell.js',
             b"const API='/auto-caption-studio/api/web-flow'"),
        "/short-maker/": (b'id="makerTabBtn"', b'aria-label="Short Studio"',
                           b'data-vps1-server-identity="1"', b'class="vps1-server-admin"'),
        "/video-merger/": (b'id="tab-merge"', b'data-video-studio-subnav',
                           b'data-vps1-server-identity="1"', b'class="vps1-server-admin"'),
        "/voice-studio/app": (b'vsEngineOmni', b'vsEngineKokoro',
                               b'vps1_native_site_shell.js'),
    }
    for url, required in markers.items():
        code, content_type, html = https_get(url, cookie)
        require(code == 200 and "html" in content_type.lower() and
                all(marker in html for marker in required),
                "native app document/controls absent: " + url +
                " status=" + str(code))
        require(b'id="video-studio-app-frame"' not in html and
                b'id="acs-studio-frame"' not in html and
                b'id="voice-studio-frame"' not in html,
                "nested outer iframe returned: " + url)
    # A shell JS file returning 200 is insufficient: the authenticated page
    # must actually request the same shared asset revision and have a usable
    # server-rendered identity if browser JS/CSP/network fails.
    short_status, _, short_html = https_get("/short-maker/", cookie)
    merged_status, _, merged_html = https_get("/video-merger/", cookie)
    js_status, js_type, shared_js = https_get(
        "/static/vps1_native_site_shell.js?v=20261020-experiment-v1", cookie)
    require(short_status == 200 and merged_status == 200 and
            js_status == 200 and
            b"function installLegacySidebar()" in shared_js and
            b"javascript" in js_type.lower().encode(),
            "live shared sidebar JavaScript could not be loaded")
    for label, document in (("Short Maker", short_html), ("Video Merger", merged_html)):
        require(b'data-unify-sidebar="1"' in document and
                b'vps1_native_site_shell.js?v=20261020-experiment-v1' in document,
                label + " live HTML is missing the shared sidebar bootstrap")
    print("VPS1_SSO_SERVER_SIDEBARS=PASS ADMIN=VERIFIED SIGN_IN_FALLBACK=READY", flush=True)
    print("VPS1_NATIVE_STUDIOS=PASS VIDEO_TABS=PASS SIDEBAR=PASS", flush=True)


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
    preflight_scene_proxy_dependency()
    for relative in FILES:
        if relative not in NEW_FILES:
            require((old / "part01" / relative).is_file(), "old Scene source missing: " + relative)
    target = RELEASES / (revision + "-auto-uploader")
    journal = TRANSACTIONS / revision
    staged = False
    switched = False
    # If already on the new exact UI, still run the idempotent Nginx route check.
    needs_ui = any(not (old / "part01" / rel).is_file() or
                   (old / "part01" / rel).read_bytes() != (SOURCE / rel).read_bytes()
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
            verify_video_studio()
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
        verify_video_studio()
        print("SCENE_UI_RESULT=ALREADY_CURRENT ROUTE=VERIFIED", flush=True)

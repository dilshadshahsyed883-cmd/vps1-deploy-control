"""Guarded Auto Script Kokoro UI-only Scene web release.

Deploy the TWO approved UI assets onto a copy of the already-live Scene web
tree. Crucially, never upgrade/rebind the independent Scene renderer when it
has pending work. This is narrower than the full Scene candidate installer.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import urllib.request

from production.runtime import BASE, REPO, atomic_json, atomic_link

CURRENT = BASE / "current/scene-builder"
RELEASES = BASE / "releases/scene-builder"
TRANSACTIONS = BASE / "transactions/scene-auto-script-ui"
UNIT = "vps1-scene-candidate.service"
CSS_PATH = "part01/static/auto_script_kokoro_20261026.css"
BASE_PATH = "part01/templates/base.html"
CSS_SOURCE = REPO / "apps/scene-builder" / CSS_PATH
LINK = '''{% if page=='auto_script' %}<link rel="stylesheet" href="/static/auto_script_kokoro_20261026.css?v=1">{% endif %}'''
ANCHOR = "{% block content %}{% endblock %}"
MARKER = b"AS-UI-KOKORO-1440-20261026"


def require(ok: bool, message: str) -> None:
    if not ok:
        raise RuntimeError("AUTO_SCRIPT_UI_OVERLAY_BLOCKED: " + message)


def patch_base(existing: str) -> str:
    """Append a single page-scoped link after Auto Script's content block."""
    require(existing.count("vps1-experiment-tabs") >= 1
            and "page=='auto_script'" in existing,
            "existing Experiment/Auto Script layout unrecognized")
    require(existing.count(ANCHOR) == 1, "template content block not unique")
    if LINK in existing:
        require(existing.count(LINK) == 1, "duplicate existing UI link")
        return existing
    require("auto_script_kokoro_20261026.css" not in existing,
            "unrecognized legacy Kokoro UI link")
    return existing.replace(ANCHOR, ANCHOR + LINK, 1)


def css_bytes() -> bytes:
    require(CSS_SOURCE.is_file() and not CSS_SOURCE.is_symlink(),
            "trusted CSS source absent or linked")
    data = CSS_SOURCE.read_bytes()
    require(MARKER in data and b"#auto-script-app" in data and b"#as-modal" in data,
            "unexpected Auto Script CSS payload")
    return data


def healthy() -> bool:
    for _ in range(35):
        try:
            with urllib.request.urlopen("http://127.0.0.1:18080/health", timeout=3) as response:
                if response.status == 200 and json.load(response).get("ok") is True:
                    return True
        except (OSError, ValueError):
            pass
        time.sleep(0.5)
    return False


def verify_live() -> None:
    from production.scene_candidate_release import _request
    from production.route_uploader_scene_https import scene_cookie
    with urllib.request.urlopen(
            "http://127.0.0.1:18080/static/auto_script_kokoro_20261026.css",
            timeout=15) as response:
        require(response.status == 200 and MARKER in response.read(),
                "live CSS readback failed")
    status, _, page = _request("/auto-script", scene_cookie())
    require(status == 200 and LINK.encode() in page,
            "authenticated Auto Script page did not load the scoped CSS")


def restart_web() -> None:
    subprocess.run(["systemctl", "restart", UNIT], check=True,
                   capture_output=True, timeout=70)
    require(healthy(), "existing Scene web did not recover")


def install(revision: str) -> None:
    require(os.geteuid() == 0, "protected root release required")
    require(re.fullmatch(r"[0-9a-f]{40}", revision) is not None, "invalid revision")
    require(shutil.disk_usage("/").free >= 5_000_000_000, "operational disk reserve")
    require(CURRENT.is_symlink(), "Scene web pointer unavailable")
    previous = CURRENT.resolve(strict=True)
    require(previous.is_dir() and previous.is_relative_to(RELEASES),
            "active web source not a managed release")
    require(subprocess.run(["systemctl", "is-active", "--quiet", UNIT],
                           capture_output=True, timeout=10).returncode == 0 and healthy(),
            "Scene web health unavailable")

    approved_css = css_bytes()
    original_base = previous / BASE_PATH
    require(original_base.is_file() and not original_base.is_symlink(),
            "active template unavailable")
    patched_base = patch_base(original_base.read_text(encoding="utf-8")).encode("utf-8")
    target = RELEASES / (revision + "-auto-script-ui-only")
    journal = TRANSACTIONS / revision
    if previous == target:
        verify_live()
        print("AUTO_SCRIPT_KOKORO_WEB_ONLY=PASS_ALREADY_INSTALLED WORKER=UNTOUCHED",
              flush=True)
        return
    require(not target.exists() and not target.is_symlink(),
            "immutable candidate already exists")
    require(not journal.exists(), "transaction already exists")
    copied = changed = False
    try:
        # Copy the currently running source, NOT the latest Scene renderer.
        # Source differences other than base-template link and CSS are forbidden.
        shutil.copytree(previous, target, symlinks=True)
        copied = True
        for relative, data in ((BASE_PATH, patched_base), (CSS_PATH, approved_css)):
            output = target / relative
            require(not output.is_symlink() and not output.parent.is_symlink(),
                    "linked UI destination")
            require(output.parent.is_dir(), "expected Scene UI directory absent")
            pending = output.with_name(output.name + ".new")
            require(not pending.exists() and not pending.is_symlink(),
                    "unexpected UI staging filename")
            pending.write_bytes(data)
            pending.chmod(0o644)
            os.replace(pending, output)

        manifest_path = target / "_scene_candidate_manifest.json"
        require(manifest_path.is_file() and not manifest_path.is_symlink(),
                "Scene immutable source manifest absent")
        manifest = json.loads(manifest_path.read_text())
        require(manifest.get("app") == "scene-builder"
                and isinstance(manifest.get("files"), dict),
                "Scene manifest ownership unknown")
        for relative, data in ((BASE_PATH, patched_base), (CSS_PATH, approved_css)):
            manifest["files"][relative] = hashlib.sha256(data).hexdigest()
        manifest["revision"] = revision
        manifest["web_only_overlay"] = {"source": str(previous),
                                        "files": [BASE_PATH, CSS_PATH]}
        atomic_json(manifest_path, manifest, 0o644)

        journal.mkdir(parents=True, mode=0o700)
        atomic_json(journal / "state.json", {
            "revision": revision, "previous": str(previous), "new": str(target),
            "modified_files": [BASE_PATH, CSS_PATH], "state": "STAGED"})
        atomic_link(CURRENT, target)
        changed = True
        restart_web()
        verify_live()
        atomic_json(journal / "state.json", {
            "revision": revision, "previous": str(previous), "new": str(target),
            "modified_files": [BASE_PATH, CSS_PATH], "state": "ACTIVE",
            "scene_worker": "UNTOUCHED", "scene_jobs": "UNTOUCHED"})
        print("AUTO_SCRIPT_KOKORO_WEB_ONLY=PASS PUBLIC_UI=LIVE"
              " SCENE_WORKER=UNTOUCHED SCENE_JOBS=UNTOUCHED", flush=True)
    except BaseException:
        if changed:
            atomic_link(CURRENT, previous)
            restart_web()
            print("AUTO_SCRIPT_KOKORO_WEB_ONLY_ROLLBACK=PASS"
                  " WORKER=UNTOUCHED PROJECTS=PRESERVED", flush=True)
        elif copied:
            shutil.rmtree(target, ignore_errors=True)
        raise

"""Guarded Scene candidate-only routing fix for Song Uploader POST actions.

Only the web candidate policy is patched, while separate Song uploader
OAuth, channel state, workers, scheduler, and Scene render worker remain untouched.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from production.runtime import BASE, REPO, atomic_json, atomic_link

CURRENT = BASE / "current/scene-builder"
RELEASES = BASE / "releases/scene-builder"
TRANSACTIONS = BASE / "transactions/song-uploader-candidate-policy"
UNIT = "vps1-scene-candidate.service"
POLICY_SOURCE = REPO / "apps/scene-builder/part01/precutover_policy.py"
POLICY_REL = "part01/precutover_policy.py"
SONG_ALIAS = '    if method == "POST" and path.startswith("/song-uploader/"):\n        return True\n'


def require(value: bool, detail: str) -> None:
    if not value:
        raise RuntimeError("SONG_UPLOADER_CANDIDATE_POLICY_BLOCKED: " + detail)


def patch_song_alias(previous: str) -> str:
    """Retain the running policy verbatim except for trusted Song POST admission."""
    anchor = "    if not editing_enabled:\n        return False\n"
    require(previous.count(anchor) == 1, "candidate write-fence anchor changed")
    if SONG_ALIAS in previous:
        return previous
    require('def mutation_allowed(' in previous, "candidate mutation policy missing")
    return previous.replace(anchor, SONG_ALIAS + anchor, 1)


def policy_allows_code(source: str) -> bool:
    import tempfile
    with tempfile.TemporaryDirectory(prefix="song-policy-probe-") as temp:
        file = Path(temp) / "policy.py"
        file.write_text(source, encoding="utf-8")
        policy = runpy.run_path(str(file))["mutation_allowed"]
        return (policy("POST", "/song-uploader/actions/settings", False)
                and policy("POST", "/song-uploader/actions/auto-v6/engine", False)
                and not policy("PUT", "/song-uploader/actions/auto-v6/engine", False)
                and not policy("POST", "/song-maker/api/queue", False)
                and not policy("POST", "/song-uploader-other/actions/settings", False))


def healthy() -> bool:
    for _ in range(40):
        try:
            with urllib.request.urlopen("http://127.0.0.1:18080/health", timeout=3) as response:
                if response.status == 200 and json.load(response).get("ok") is True:
                    return True
        except (OSError, ValueError):
            pass
        time.sleep(0.5)
    return False


def restart_web() -> None:
    subprocess.run(["systemctl", "restart", UNIT], check=True, capture_output=True, timeout=60)
    require(healthy(), "web candidate did not become healthy")


def _no_redirect():
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


def verify_post_gate_without_action() -> None:
    """Bad CSRF is rejected upstream; never save settings or arm a schedule."""
    from production.route_uploader_scene_https import scene_cookie
    cookie = scene_cookie()
    require(cookie, "cannot establish verified Scene admin session")
    for endpoint in ("/song-uploader/actions/settings",
                     "/song-uploader/actions/auto-v6/engine"):
        data = urllib.parse.urlencode({"csrf_token": "invalid-verification-only",
                                        "action": "arm_scheduler"}).encode()
        request = urllib.request.Request(
            "https://app.scenebuilder.online" + endpoint,
            data=data, method="POST",
            headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with _no_redirect().open(request, timeout=20) as response:
                status = response.status
                body = response.read(8192)
        except urllib.error.HTTPError as exc:
            status = exc.code
            body = exc.read(8192)
        require(status in (400, 401, 403, 422),
                "unvalidated POST reached a state-mutating handler " + endpoint + ":" + str(status))
        require(b"candidate: write requires separate approval" not in body,
                "Scene candidate still intercepts Song action " + endpoint)
    print("SONG_UPLOADER_CANDIDATE_POST=REACHABLE BAD_CSRF=REJECTED NO_ACTION_EXECUTED", flush=True)


def install(revision: str) -> None:
    require(os.geteuid() == 0, "root-owned production helper required")
    require(re.fullmatch(r"[a-f0-9]{40}", revision) is not None, "invalid SHA")
    require(shutil.disk_usage("/").free >= 5_000_000_000, "disk reserve")
    require(CURRENT.is_symlink(), "Scene candidate pointer absent")
    old = CURRENT.resolve(strict=True)
    require(old.is_dir() and old.is_relative_to(RELEASES), "unowned Scene release")
    require(subprocess.run(["systemctl", "is-active", "--quiet", UNIT],
                           capture_output=True, timeout=10).returncode == 0 and healthy(),
            "existing Scene candidate must be healthy")
    source = POLICY_SOURCE
    require(source.is_file() and not source.is_symlink()
            and policy_allows_code(source.read_text(encoding="utf-8")),
            "reviewed source lacks Song actions")
    original = old / POLICY_REL
    require(original.is_file() and not original.is_symlink(), "old policy not owned")
    app_file = old / "part01/app.py"
    require(app_file.is_file() and not app_file.is_symlink(), "Scene candidate app missing")
    app_content = app_file.read_text(encoding="utf-8")
    require("candidate_mutation_allowed" in app_content
            and '@app.api_route("/song-uploader/{upstream_path:path}"' in app_content,
            "canonical Song proxy not available in Scene candidate")
    patched = patch_song_alias(original.read_text(encoding="utf-8"))
    require(policy_allows_code(patched), "patched policy fails exact Song contract")

    target = RELEASES / (revision + "-song-uploader-post-gate")
    journal = TRANSACTIONS / revision
    require(not target.exists() and not target.is_symlink(), "immutable target already exists")
    require(not journal.exists(), "immutable journal already exists")
    copied = changed = False
    try:
        shutil.copytree(old, target, symlinks=True)
        copied = True
        destination = target / POLICY_REL
        require(destination.is_file() and not destination.is_symlink(), "policy symlink drift")
        destination.write_text(patched, encoding="utf-8")
        destination.chmod(0o644)
        journal.mkdir(parents=True, mode=0o700)
        atomic_json(journal / "state.json", {
            "revision": revision, "old": str(old), "new": str(target),
            "modified_files": [POLICY_REL], "status": "STAGED",
        })
        atomic_link(CURRENT, target)
        changed = True
        restart_web()
        require(policy_allows_code((CURRENT / POLICY_REL).read_text(encoding="utf-8")),
                "readback: current policy does not permit Song actions")
        verify_post_gate_without_action()
        atomic_json(journal / "state.json", {
            "revision": revision, "old": str(old), "new": str(target),
            "modified_files": [POLICY_REL], "status": "ACTIVE",
            "song_scheduler": "NOT_ARMED_BY_RELEASE", "scene_render_worker": "UNTOUCHED",
            "oauth_and_databases": "UNTOUCHED",
        })
        print("SONG_UPLOADER_CANDIDATE=PASS SCOPED_POLICY=LIVE NO_SCHEDULING_CHANGED", flush=True)
    except BaseException:
        if changed:
            atomic_link(CURRENT, old)
            restart_web()
            print("SONG_UPLOADER_CANDIDATE_ROLLBACK=PASS", flush=True)
        elif copied:
            shutil.rmtree(target, ignore_errors=True)
        raise

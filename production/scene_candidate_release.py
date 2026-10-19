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
import re
import stat
import tempfile
from pathlib import Path
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request


BASE = Path("/srv/fresh-website")
CURRENT = BASE / "current/scene-builder"
RELEASES = BASE / "releases/scene-builder"
STATE = Path("/var/lib/fresh-website/scene-builder")
ENV = Path("/etc/fresh-website/scene-builder.env")
UNIT = "vps1-scene-candidate.service"
RECORD = BASE / "control/state/scene-candidate.json"
STORAGE_CANDIDATE_ENV = Path("/etc/fresh-website/vps-storage-manager.env")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Preserve the original anonymous 303 instead of following to login HTML."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _request(path: str, cookie: str = "", data: bytes | None = None,
             *, follow_redirects: bool = True) -> tuple[int, dict[str, str], bytes]:
    headers = {"Connection": "close"}
    if cookie:
        headers["Cookie"] = cookie
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(
        "http://127.0.0.1:18080" + path,
        headers=headers,
        data=data,
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        *([] if follow_redirects else [_NoRedirect()]),
    )
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



def _candidate_storage_token() -> str | None:
    """Read only the existing root-owned candidate credential; never emit it."""
    if not STORAGE_CANDIDATE_ENV.is_file() or STORAGE_CANDIDATE_ENV.is_symlink():
        return None
    if subprocess.run(
        ["systemctl", "is-active", "--quiet", "vps1-storage-candidate.service"],
        capture_output=True,
    ).returncode:
        return None
    if STORAGE_CANDIDATE_ENV.stat().st_size > 16384:
        raise RuntimeError("Storage candidate environment is unexpectedly large")
    values = []
    for line in STORAGE_CANDIDATE_ENV.read_text(encoding="utf-8").splitlines():
        if line.startswith("STORAGE_MANAGER_TOKEN="):
            values.append(line.split("=", 1)[1].strip().strip("\"'"))
    if len(values) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]{24,256}", values[0]):
        return None
    return values[0]


def _storage_candidate_env_patch(current: str, token: str) -> str:
    """Connect a Scene candidate to 8149 without overriding another backend."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{24,256}", token):
        raise ValueError("Invalid candidate token format")
    lines = current.splitlines()
    positions: dict[str, list[int]] = {"STORAGE_MANAGER_URL": [], "STORAGE_MANAGER_TOKEN": []}
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in positions and "=" in line:
            positions[key].append(i)
    if any(len(entries) > 1 for entries in positions.values()):
        raise RuntimeError("Duplicate Scene storage environment keys")
    urls = positions["STORAGE_MANAGER_URL"]
    if urls and lines[urls[0]].split("=", 1)[1].strip().strip("\"'") != "http://127.0.0.1:8149":
        return current  # Preserve an explicitly configured independent backend.
    for key, value in (
        ("STORAGE_MANAGER_URL", "http://127.0.0.1:8149"),
        ("STORAGE_MANAGER_TOKEN", token),
    ):
        if positions[key]:
            lines[positions[key][0]] = key + "=" + value
        else:
            lines.append(key + "=" + value)
    updated = "\n".join(lines) + "\n"
    return current if updated == current else updated


def _atomic_scene_env(payload: bytes) -> None:
    """Replace only Scene's existing environment with its original ownership/mode."""
    metadata = ENV.lstat()
    if not stat.S_ISREG(metadata.st_mode) or len(payload) > 65536:
        raise RuntimeError("Scene environment ownership or size is invalid")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".scene-env-", dir=ENV.parent, delete=False) as target:
            temporary = Path(target.name)
            os.fchmod(target.fileno(), stat.S_IMODE(metadata.st_mode))
            os.fchown(target.fileno(), metadata.st_uid, metadata.st_gid)
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, ENV)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _verify_integrations(*, require_storage_backend: bool = False) -> dict:
    from production.route_uploader_scene_https import scene_cookie

    cookie = scene_cookie()
    status, headers, _ = _request("/internal/workspace-auth?app=short-maker", cookie)
    normalized = {key.lower(): value for key, value in headers.items()}
    if status != 204 or not normalized.get("x-auth-user") or normalized.get("x-is-admin") != "1":
        raise RuntimeError("Scene administrator session bridge is unavailable")
    anonymous, _, _ = _request("/internal/workspace-auth?app=short-maker")
    if anonymous != 401:
        raise RuntimeError("Scene session bridge does not deny anonymous access")
    identity_status, identity_headers, identity_body = _request("/api/session/identity", cookie)
    try:
        identity = json.loads(identity_body)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Scene session identity response is invalid") from exc
    identity_headers = {key.lower(): value for key, value in identity_headers.items()}
    if (identity_status != 200 or identity.get("authenticated") is not True
            or identity.get("is_admin") is not True
            or identity.get("username") != normalized.get("x-auth-user")
            or "no-store" not in identity_headers.get("cache-control", "")):
        raise RuntimeError("Scene administrator session identity is unavailable")
    anonymous_identity, _, _ = _request("/api/session/identity", follow_redirects=False)
    if anonymous_identity not in {303, 401}:
        raise RuntimeError("Scene session identity exposed to anonymous visitor")
    native_status, _, native_js = _request("/static/vps1_native_site_shell.js")
    if (native_status != 200
            or b'fetch("/api/session/identity"' not in native_js
            or b'adminLink.href = "/admin"' not in native_js):
        raise RuntimeError("Scene native role-aware sidebar JavaScript unavailable")
    pages = {}
    for name, path in (("script_work", "/script-work"), ("storage_manager", "/admin/storage"),
                       ("user_management", "/user-management")):
        code, response_headers, body = _request(path, cookie)
        if name == "user_management":
            if code != 200 or b'name="csrf_token"' not in body or b'name="role"' not in body:
                raise RuntimeError("User Management account form unavailable")
        elif code not in {200, 302, 303, 307, 308}:
            raise RuntimeError(name + " authenticated page unavailable")
        elif code == 200 and b"<" not in body:
            raise RuntimeError(name + " response is not a browser document")
        pages[name] = code

    # Deliberately INVALID username and session CSRF prove the live POST
    # reaches the admin handler, but cannot create a production account.
    post_data = urllib.parse.urlencode({
        "action": "create", "username": "invalid username!",
        "secret_key": "not-a-real-secret",
        "role": "non_admin", "csrf_token": "intentionally-invalid-csrf",
    }).encode("ascii")
    post_status, _, post_body = _request("/user-management", cookie, data=post_data)
    if post_status != 403 or b"Invalid user-management action token" not in post_body:
        raise RuntimeError("User Management POST did not reach role and CSRF handler")
    # Release admission for the actual authenticated dashboard API. A template
    # existence check alone would miss a stale Algrow Voice card.
    dashboard_status, _, dashboard_body = _request("/api/dashboard/v1", cookie)
    if dashboard_status != 200:
        raise RuntimeError("dashboard authenticated API failed")
    try:
        payload = json.loads(dashboard_body)
        app_names = {str(item.get("name", "")) for item in payload.get("apps", []) if isinstance(item, dict)}
    except (ValueError, TypeError, AttributeError) as exc:
        raise RuntimeError("dashboard API returned malformed JSON") from exc
    if "Algrow Voice" in app_names:
        raise RuntimeError("obsolete Algrow Voice dashboard card remains in the API")
    if "Scene Builder" not in app_names or "Auto Caption Studio" not in app_names:
        raise RuntimeError("dashboard essential apps not present")

    # The dashboard's HTML must reference its new cache-busted JS artifact.
    dashboard_page, _, dashboard_html = _request("/", cookie)
    if dashboard_page == 200 and b"dashboard_control_center.js?" in dashboard_html:
        if b"hide-algrow-v3" not in dashboard_html:
            raise RuntimeError("stale dashboard asset version remains in served HTML")
    storage_backend = "UNVERIFIED_READ_ONLY_FALLBACK"
    if require_storage_backend:
        code, _, body = _request("/api/admin/storage/capabilities", cookie)
        if code != 200:
            raise RuntimeError("Storage Manager authenticated capabilities probe failed")
        try:
            capabilities = json.loads(body)
        except (ValueError, TypeError) as exc:
            raise RuntimeError("Storage Manager capabilities returned invalid JSON") from exc
        if capabilities.get("read_only") is not True or capabilities.get("can_modify") is not False:
            raise RuntimeError("Storage candidate unexpectedly advertises writable capability")
        code, _, body = _request("/api/admin/storage/metrics", cookie)
        if code != 200:
            raise RuntimeError("Storage Manager authenticated metrics probe failed")
        try:
            metrics = json.loads(body)
        except (ValueError, TypeError) as exc:
            raise RuntimeError("Storage Manager metrics returned invalid JSON") from exc
        if not isinstance(metrics, dict) or not isinstance(metrics.get("disk"), dict):
            raise RuntimeError("Storage Manager metrics payload is incomplete")
        storage_backend = "CANDIDATE_READ_ONLY_PASS"
    return {"session_bridge": "PASS", "dashboard_no_algrow": "PASS",
            "native_sidebar_role": "PASS", "user_management_post_csrf": "PASS",
            "storage_backend": storage_backend, **pages}


def _verify_native_ui_release(target: Path) -> None:
    """Fail the guarded Scene cutover if independently owned native UIs are missing.

    Called only for this new UI revision, after all required apps have been installed.
    Browser authentication, route health, tabs and real app documents are checked
    over the existing trusted HTTPS gateway. A failure rolls Scene back.
    """
    if not (target / "part01/static/vps1_native_site_shell.js").is_file():
        return  # older unrelated deployments retain their established behavior
    from production.scene_uploader_ui_release import verify_video_studio
    verify_video_studio()


def _verify_native_ui_release(target: Path) -> None:
    """Fail the guarded Scene cutover if independently owned native UIs are missing.

    Called only for this new UI revision, after all required apps have been installed.
    Browser authentication, route health, tabs and real app documents are checked
    over the existing trusted HTTPS gateway. A failure rolls Scene back.
    """
    if not (target / "part01/static/vps1_native_site_shell.js").is_file():
        return  # older unrelated deployments retain their established behavior
    from production.scene_uploader_ui_release import verify_video_studio
    verify_video_studio()


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
        _verify_native_ui_release(target)
        event(app="scene-builder", result="PASS_ALREADY_INSTALLED", revision=revision,
              port=18080, integrations=integrations)
        return

    journal = BASE / "transactions/scene-candidate" / (revision + "-" + str(time.time_ns()))
    journal.mkdir(parents=True)
    atomic_json(journal / "previous.json", {"current": str(previous), "unit": UNIT})
    backups = backup_databases(STATE, journal / "sqlite-backup")
    changed = False
    original_env: bytes | None = None
    require_storage_backend = False
    try:
        token = _candidate_storage_token()
        if token is not None:
            original = ENV.read_bytes()
            new_text = _storage_candidate_env_patch(original.decode("utf-8"), token)
            require_storage_backend = (
                "STORAGE_MANAGER_URL=http://127.0.0.1:8149" in new_text.splitlines()
                and ("STORAGE_MANAGER_TOKEN=" + token) in new_text.splitlines()
            )
            if new_text.encode("utf-8") != original:
                _atomic_scene_env(new_text.encode("utf-8"))
                original_env = original
        atomic_link(CURRENT, target)
        changed = True
        run(["systemctl", "restart", UNIT], timeout=70, quiet=True)
        health = _health()
        integrations = _verify_integrations(require_storage_backend=require_storage_backend)
        # Idempotent dedicated Chrome/noVNC setup used by the existing Script Work UI.
        install_browser()
        integrations = _verify_integrations(require_storage_backend=require_storage_backend)
        _verify_native_ui_release(target)
        if shutil.disk_usage("/").free < 5_000_000_000:
            raise RuntimeError("operational disk reserve breached")
        atomic_json(RECORD, {"revision": revision, "status": "ACTIVE", "unit": UNIT,
                            "previous": str(previous), "transaction": str(journal)})
        event(app="scene-builder", result="PASS_CANDIDATE_SOURCE_AND_SSO", revision=revision,
              port=18080, health=health, integrations=integrations,
              sqlite_backups=len(backups), rollback=str(journal))
    except BaseException:
        if original_env is not None:
            _atomic_scene_env(original_env)  # Restore credentials before service rollback.
        if changed or original_env is not None:
            atomic_link(CURRENT, previous)
            subprocess.run(["systemctl", "restart", UNIT], capture_output=True, timeout=70)
            _health()
            event(app="scene-builder", rollback="PASS", data="PRESERVED")
        raise


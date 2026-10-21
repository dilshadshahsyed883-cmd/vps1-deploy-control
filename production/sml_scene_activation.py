"""VPS_1 guarded SML -> Scene Builder Editor-token activation.

Runs only inside the existing root-owned protected-main SML installer.
The token is minted on the VPS, never in git, CI logs or job arguments.
It pairs only an idle owned production renderer; admission and user queues are preserved.
"""
from __future__ import annotations

import os
import re
import secrets
import stat
import subprocess
import tempfile
import urllib.error
import urllib.request
import json
from pathlib import Path

SML_ENV = Path("/etc/fresh-website/production-self-media-library.env")
SCENE_ENV = Path("/etc/fresh-website/scene-builder.env")
SML_SERVICE = "vps1-production-self-media-library.service"
SCENE_SERVICE = "vps1-scene-candidate.service"
SCENE_CURRENT = Path("/srv/fresh-website/current/scene-builder")
SML_CURRENT = Path("/srv/fresh-website/current/self-media-library")
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{48,128}\Z")
KEY_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
ORIGIN = "http://127.0.0.1:8147"


def _ensure_private(path: Path) -> bytes:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("Required owned runtime env is absent or unsafe")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise RuntimeError("Runtime env ownership or permissions are unsafe")
    if info.st_size > 65536:
        raise RuntimeError("Unexpected oversized runtime env")
    return path.read_bytes()


def _entries(data: bytes) -> dict[str, str]:
    try:
        text = data.decode("utf-8")
    except UnicodeError as exc:
        raise RuntimeError("Invalid env encoding") from exc
    result: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if not KEY_PATTERN.fullmatch(key):
            continue
        if key in result:
            raise RuntimeError("Duplicate key in protected environment")
        result[key] = value.strip()
    return result


def _patched(data: bytes, updates: dict[str, str]) -> bytes:
    entries = _entries(data)
    for key, value in updates.items():
        if not KEY_PATTERN.fullmatch(key) or not value or "\n" in value or "\r" in value:
            raise ValueError("Invalid fixed environment update")
        if key in entries and entries[key] and entries[key] != value:
            raise RuntimeError("Existing independent service credential differs")
    prefix = data.decode("utf-8")
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    for key, value in updates.items():
        if key not in entries or not entries[key]:
            if key in entries:  # replace exact blank assignment, not duplicate key
                prefix = re.sub(r"(?m)^" + re.escape(key) + r"=\s*$", key + "=" + value, prefix)
            else:
                prefix += key + "=" + value + "\n"
    return prefix.encode("utf-8")


def _atomic_replace(path: Path, data: bytes) -> None:
    previous = path.lstat()
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".sml-scene-", dir=path.parent,
                                         delete=False) as handle:
            temp_name = handle.name
            os.fchmod(handle.fileno(), 0o600)
            os.fchown(handle.fileno(), previous.st_uid, previous.st_gid)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)


def _restart(service: str) -> None:
    subprocess.run(["systemctl", "restart", service], check=True, timeout=75,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["systemctl", "is-active", "--quiet", service], check=True, timeout=12)


def _get_json(port: int, path: str, *, token: str = "") -> dict:
    headers = {"X-SML-Editor-Token": token} if token else {}
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=15) as response:
        if response.status != 200:
            raise RuntimeError("Scoped live health/status did not return HTTP 200")
        result = json.loads(response.read(262144))
    if not isinstance(result, dict):
        raise RuntimeError("Unexpected integration health format")
    return result


def activate() -> dict:
    if os.geteuid() != 0:
        raise RuntimeError("Existing approved root deploy lane required")
    # Fail closed rather than enabling the token for stale/unknown source.
    if not ((SCENE_CURRENT / "part01/sml_scene_bridge.py").is_file()
            and (SML_CURRENT / "part01/media_extensions.py").is_file()):
        raise RuntimeError("Scene and SML approved V2 integration source are not staged")
    for unit in (SCENE_SERVICE, SML_SERVICE):
        subprocess.run(["systemctl", "is-active", "--quiet", unit], check=True, timeout=12)
    scene_before = _get_json(18080, "/health")
    sml_before = _get_json(8147, "/api/health")
    if scene_before.get("ok") is not True or sml_before.get("ok") is not True:
        raise RuntimeError("Existing production service health failed")
    original_sml, original_scene = _ensure_private(SML_ENV), _ensure_private(SCENE_ENV)
    old_sml = _entries(original_sml).get("SML_EDITOR_API_TOKEN", "")
    old_scene = _entries(original_scene).get("SCENE_SML_EDITOR_TOKEN", "")
    if old_sml and not TOKEN_PATTERN.fullmatch(old_sml):
        raise RuntimeError("Existing SML Editor credential has unknown format")
    if old_scene and not TOKEN_PATTERN.fullmatch(old_scene):
        raise RuntimeError("Existing Scene Editor credential has unknown format")
    if old_sml and old_scene and old_sml != old_scene:
        raise RuntimeError("Existing SML and Scene editor credentials mismatch")
    token = old_sml or old_scene or secrets.token_urlsafe(48)
    if not TOKEN_PATTERN.fullmatch(token):
        raise RuntimeError("Generated scoped Editor token has unsafe format")
    new_sml = _patched(original_sml, {"SML_EDITOR_API_TOKEN": token})
    new_scene = _patched(original_scene, {
        "SCENE_SML_EDITOR_TOKEN": token, "SCENE_SML_URL": ORIGIN,
    })
    changed_sml = changed_scene = False
    from production.scene_sml_worker import SceneSMLWorker
    worker = SceneSMLWorker(SCENE_CURRENT.resolve(), token)
    try:
        if new_sml != original_sml:
            _atomic_replace(SML_ENV, new_sml)
            changed_sml = True
        if new_scene != original_scene:
            _atomic_replace(SCENE_ENV, new_scene)
            changed_scene = True
        # SML and Scene web only: never restart vps1-scene-worker-admission.
        if changed_sml:
            _restart(SML_SERVICE)
        if changed_scene:
            _restart(SCENE_SERVICE)
        renderer = worker.install()
        sml_health = _get_json(8147, "/api/health")
        scene_health = _get_json(18080, "/health")
        editor = _get_json(8147, "/api/v1/media/status")
        approved = _get_json(
            8147, "/api/v1/media/search?media_type=video&limit=1", token=token,
        )
        if (sml_health.get("ok") is not True or scene_health.get("ok") is not True
                or editor.get("editor_token_configured") is not True
                or not isinstance(approved.get("items"), list)):
            raise RuntimeError("SML-Scene Editor activation verification failed")
        proof = worker.render_proof()
        return {"ok": True, "already_active": not (changed_sml or changed_scene),
                **renderer, "three_scene_render": proof}
    except BaseException:
        try:
            worker.rollback()
        finally:
            _restore_pair(changed_sml, original_sml, changed_scene, original_scene)
        raise


def _restore_pair(changed_sml, original_sml, changed_scene, original_scene):
    if changed_sml:
        _atomic_replace(SML_ENV, original_sml)
    if changed_scene:
        _atomic_replace(SCENE_ENV, original_scene)
    # Best-effort service restoration without echoing env values.
    for service in (SML_SERVICE, SCENE_SERVICE):
        try:
            _restart(service)
        except Exception:
            pass

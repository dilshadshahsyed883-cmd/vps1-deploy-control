"""Voice V1.1 live read-only acceptance, run only via the protected Voice lane.

No synthesis job creation, user switching, database writes, Nginx edits, service
restarts, Drive calls or provider configuration. Real two-account UAT and actual
audio generation remain separate acceptance requirements, never inferred here.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess

from production import nginx_route_only as https
from production import scene_session_routes as scene_sso
from production.runtime import BASE, http_check, verify_voice_release_anonymous_denial


UNITS = (
    "vps1-production-voice-studio.service",
    "vps1-production-kokoro-worker.service",
    "vps1-production-edge-worker.service",
    "vps1-production-voice-dispatcher.service",
    "vps1-production-voice-drive-spooler.service",
)
ENGINES = ("kokoro", "edge", "clone")


def require(condition, code):
    if not condition:
        raise RuntimeError("VOICE_READONLY_ACCEPTANCE_BLOCKED:" + code)


def installed_revision():
    """Proof of managed existing Voice install, not the new audit commit SHA."""
    state_path = BASE / "control/state/voice-studio.json"
    current = BASE / "current/voice-studio"
    require(state_path.is_file() and not state_path.is_symlink(),
            "VOICE_RELEASE_RECORD_MISSING")
    require(current.is_symlink(), "VOICE_RELEASE_POINTER_UNMANAGED")
    state = json.loads(state_path.read_text())
    revision = state.get("revision")
    require(state.get("status") == "ACTIVE"
            and isinstance(revision, str)
            and re.fullmatch(r"[a-f0-9]{40}", revision) is not None,
            "VOICE_RELEASE_RECORD_INVALID")
    root = (BASE / "releases/voice-studio").resolve(strict=True)
    target = current.resolve(strict=True)
    require(target.is_dir() and target.is_relative_to(root)
            and (target.name == revision or target.name.startswith(revision + "-")),
            "VOICE_RELEASE_POINTER_MISMATCH")
    return revision


def probe_engine(engine, cookie, fetch=None):
    """Read public anonymous + signed Scene status, never include owner records."""
    require(engine in ENGINES, "UNKNOWN_ENGINE")
    fetch = fetch or https.tls_get
    path = "/voice-studio/tts/api/" + engine + "/status"
    code, _, _ = fetch(path)
    require(code == 401, "ANONYMOUS_ENGINE_EXPOSURE_" + engine.upper())
    code, kind, body = fetch(path, cookie=cookie)
    require(code == 200 and "json" in kind.lower(),
            "SIGNED_ENGINE_STATUS_MISSING_" + engine.upper())
    try:
        payload = json.loads(body)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("VOICE_READONLY_ACCEPTANCE_BLOCKED:INVALID_ENGINE_JSON_" + engine.upper()) from exc
    require(isinstance(payload, dict), "INVALID_ENGINE_PAYLOAD_" + engine.upper())
    if engine == "clone":
        require(payload.get("ok") is True
                and isinstance(payload.get("worker_configured"), bool)
                and isinstance(payload.get("worker_online"), bool),
                "CLONE_STATUS_SCHEMA_DRIFT")
        return {"configured": payload["worker_configured"],
                "online": payload["worker_online"],
                "generation_test": "NOT_RUN"}
    require(isinstance(payload.get("ready"), bool),
            "ENGINE_READINESS_SCHEMA_DRIFT_" + engine.upper())
    result = {"ready": payload["ready"], "generation_test": "NOT_RUN"}
    catalog_path = "/voice-studio/tts/api/" + engine + "/voices"
    code, _, _ = fetch(catalog_path)
    require(code == 401, "ANONYMOUS_CATALOG_EXPOSURE_" + engine.upper())
    code, kind, body = fetch(catalog_path, cookie=cookie)
    require(code == 200 and "json" in kind.lower(),
            "SIGNED_ENGINE_CATALOG_MISSING_" + engine.upper())
    try:
        catalog = json.loads(body)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("VOICE_READONLY_ACCEPTANCE_BLOCKED:INVALID_CATALOG_JSON_" + engine.upper()) from exc
    require(isinstance(catalog, dict) and isinstance(catalog.get("voices"), list),
            "CATALOG_SCHEMA_DRIFT_" + engine.upper())
    result["catalog_nonempty"] = bool(catalog["voices"])
    return result


def report():
    revision = installed_revision()
    live = http_check("voice-studio")
    require(isinstance(live, dict) and live.get("ok") is True,
            "VOICE_8143_HEALTH_NOT_READY")
    require(subprocess.run(["nginx", "-t"], capture_output=True,
                           timeout=15, check=False).returncode == 0,
            "NGINX_CONFIGURATION_INVALID")
    for unit in UNITS:
        require(subprocess.run(["systemctl", "is-active", "--quiet", unit],
                               capture_output=True, timeout=8, check=False).returncode == 0,
                "SERVICE_INACTIVE_" + unit.split("vps1-production-", 1)[-1].replace(".service", "").upper())
    cookie = scene_sso.ready_cookie("voice-studio")
    scene_sso.verify("voice-studio", cookie)
    scene_sso.verify_voice_shell(cookie)
    verify_voice_release_anonymous_denial()
    statuses = {engine: probe_engine(engine, cookie) for engine in ENGINES}
    return {
        "app": "voice-studio",
        "mode": "READ_ONLY_LIVE_ACCEPTANCE",
        "result": "PASS_READONLY_PROBES",
        "installed_voice_revision": revision,
        "signed_scene_sso": "PASS",
        "anonymous_api_denial": "PASS",
        "nginx_syntax": "PASS",
        "voice_web_and_four_workers": "ACTIVE",
        "engines": statuses,
        "edge_real_generation": "NOT_TESTED",
        "drive_real_delivery": "NOT_TESTED",
        "admin_nonadmin_real_2_user_uat": "PENDING",
        "mutation": "NONE",
        "auto_script": "OFFLINE",
    }

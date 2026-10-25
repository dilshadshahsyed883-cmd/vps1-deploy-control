"""Fail closed before any Scene Builder import-time schema/data creation."""
from __future__ import annotations
import os
from pathlib import Path

ROOT = Path("/var/lib/fresh-website/scene-builder")


def validate_candidate_environment() -> None:
    if os.getenv("VPS1_RUNTIME_CANDIDATE") != "1":
        return
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise RuntimeError("shadow candidate must run as non-root")
    if os.getenv("FRESH_WEBSITE_INSTALL") != "1":
        raise RuntimeError("Algrow and legacy install must remain excluded")
    # Production Auto Script needs an explicit, separately audited database
    # beneath Scene's writable state root. No other candidate integrations change.
    if os.getenv("FRESH_AUTO_SCRIPT_READY", "0") == "1":
        db_raw = os.getenv("AUTO_SCRIPT_DB_PATH", "")
        expected = ROOT / "auto-script" / "auto_script.sqlite3"
        if not db_raw or Path(db_raw).resolve() != expected:
            raise RuntimeError("Auto Script requires its isolated owner-approved database")
    if os.getenv("WEB_HOST") != "127.0.0.1" or os.getenv("WEB_PORT") != "18080":
        raise RuntimeError("candidate must bind only proposed shadow port 18080")
    editing = os.getenv("VPS1_SCENE_PRECUTOVER_EDITING", "0")
    if editing not in ("", "0", "1"):
        raise RuntimeError("precutover editing must be explicitly 0 or 1")
    # The isolated Auto Script scheduler is independent of the Scene render
    # worker; editing mode must not silently start unrelated worker loops.
    if os.getenv("ATOMIC_FLOW_ENABLED", "0").lower() not in ("", "0", "false", "off"):
        raise RuntimeError("atomic flow disabled in candidate")
    for key in ("WEB_ACCESS_KEY", "SESSION_SECRET", "SCENE_RUNTIME_PROBE_TOKEN"):
        value = os.getenv(key, "")
        if len(value) < 24 or value in ("change-this-session-secret", "replace-me"):
            raise RuntimeError(key + " requires injected strong value")
    for key in ("WEB_DATA_DIR", "SCENE_PERMANENT_ROOT", "SCRIPT_WORK_DATA_DIR",
                "MASTER_CONTROLLER_DB_PATH", "ATOMIC_FLOW_DB_PATH",
                "AUTOFLOW_FLOW_RESULT_ROOT", "SCENE_CHANNELS_FILE"):
        raw = os.getenv(key, "")
        if not raw or not Path(raw).is_absolute():
            raise RuntimeError(key + " requires an absolute isolated path")
        resolved = Path(raw).resolve()
        if resolved == ROOT or ROOT not in resolved.parents:
            raise RuntimeError(key + " must be beneath " + str(ROOT))

from __future__ import annotations

import json
from pathlib import Path
import urllib.request
from typing import Any

from .config import app_root
from .database import now_iso

GLOBAL_CONFIG = app_root() / "data" / "config" / "web_flow_global.json"
SERIAL_KEY = "webflow_serial_owner"
TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


def _load_global() -> dict[str, Any]:
    try:
        raw = json.loads(GLOBAL_CONFIG.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raw = {}
    except Exception:
        raw = {}
    return {
        "queue_mode": "normal",
        "scene_builder_priority": bool(raw.get("scene_builder_priority", True)),
        "paused": bool(raw.get("paused", False)),
    }


def _settings(row) -> dict[str, Any]:
    try:
        raw = row["settings_json"] if "settings_json" in row.keys() else "{}"
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def is_web_flow_row(row) -> bool:
    settings = _settings(row)
    return bool(dict(settings.get("metadata") or {}).get("web_flow_v1"))


def _runtime_value(db, key: str) -> str:
    row = db.execute("SELECT value FROM scheduler_state WHERE name=?", (key,)).fetchone()
    return str(row["value"] if row else "")


def _runtime_set(db, key: str, value: str) -> None:
    db.execute(
        """INSERT INTO scheduler_state(name,value,updated_at) VALUES(?,?,?)
           ON CONFLICT(name) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
        (key, value, now_iso()),
    )


def scene_builder_busy() -> bool:
    """Fail open: an unavailable Scene Builder health endpoint must not deadlock ACS."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=0.65) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        if not isinstance(payload, dict) or not payload.get("ok"):
            return False
        counts = payload.get("queue_counts") or {}
        if not isinstance(counts, dict):
            return False
        terminal = {"completed", "failed", "cancelled"}
        return any(int(value or 0) > 0 for key, value in counts.items() if str(key).lower() not in terminal)
    except Exception:
        return False


def _serial_release_ready(row) -> bool:
    if str(row["status"] or "") != "completed":
        return False
    settings = _settings(row)
    metadata = settings.get("metadata") if isinstance(settings.get("metadata"), dict) else {}
    drive_enabled = bool(metadata.get("web_drive_upload_enabled", True))
    if not drive_enabled:
        # Drive-disabled Web Flow jobs are deliberately retained locally. They
        # may release serial ownership after a verified local completion.
        return str(row["drive_status"] or "") == "not_required"
    return (
        int(row["drive_verified"] or 0) == 1
        and str(row["local_cleanup_status"] or "") == "cleared"
    )


def _reconcile_serial_owner(db) -> str:
    config = _load_global()
    if config["queue_mode"] != "verified_serial":
        if _runtime_value(db, SERIAL_KEY):
            _runtime_set(db, SERIAL_KEY, "")
        return ""

    owner = _runtime_value(db, SERIAL_KEY).strip()
    if owner:
        row = db.execute("SELECT * FROM jobs WHERE id=?", (owner,)).fetchone()
        if row is None or not is_web_flow_row(row):
            _runtime_set(db, SERIAL_KEY, "")
            owner = ""
        elif _serial_release_ready(row):
            _runtime_set(db, SERIAL_KEY, "")
            owner = ""
        else:
            return owner

    # If Verified Serial is enabled while web-flow work is already in-flight,
    # adopt the oldest progressed item rather than allowing another project to start.
    rows = db.execute(
        """SELECT * FROM jobs
           WHERE status NOT IN ('completed','failed','cancelled','interrupted')
           ORDER BY CASE WHEN status='running' THEN 0 ELSE 1 END,created_at,id"""
    ).fetchall()
    for row in rows:
        if is_web_flow_row(row):
            pipeline = str(row["pipeline_stage"] or "")
            if str(row["status"] or "") == "running" or pipeline not in {"ready_transcribe", ""}:
                owner = str(row["id"])
                _runtime_set(db, SERIAL_KEY, owner)
                return owner
    return ""


def claim_allowed(db, row, *, sb_busy: bool | None = None) -> bool:
    if not is_web_flow_row(row):
        return True
    config = _load_global()
    if config["paused"]:
        return False
    if config["scene_builder_priority"]:
        busy = scene_builder_busy() if sb_busy is None else bool(sb_busy)
        if busy:
            return False
    if config["queue_mode"] != "verified_serial":
        _reconcile_serial_owner(db)
        return True
    owner = _reconcile_serial_owner(db)
    if owner:
        return owner == str(row["id"])
    # First eligible candidate becomes the owner. Persistence happens inside the
    # same DB transaction as the later claim, so another worker sees the owner.
    owner = str(row["id"])
    _runtime_set(db, SERIAL_KEY, owner)
    return True


def queue_runtime_state(db) -> dict[str, Any]:
    config = _load_global()
    owner = _reconcile_serial_owner(db)
    return {
        **config,
        "serial_active_job_id": owner,
        "scene_builder_busy": scene_builder_busy() if config["scene_builder_priority"] else False,
    }

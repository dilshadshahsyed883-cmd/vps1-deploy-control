from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from fastapi import Body, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse


PIPELINE = [
    {"id": "links", "label": "Links", "owner": "Atomic Flow"},
    {"id": "permanent_id_metadata", "label": "Permanent ID and metadata", "owner": "Google Sheet"},
    {"id": "script_extraction", "label": "Script Extraction", "owner": "Script Extractor"},
    {"id": "script_processing", "label": "Script Processing", "owner": "Script Processor"},
    {"id": "algrow_voice", "label": "Algrow Voice", "owner": "Algrow Voice"},
    {"id": "auto_caption_studio", "label": "Auto Caption Studio", "owner": "Auto Caption Studio"},
    {"id": "verified_final_mp4", "label": "Verified Final MP4", "owner": "Auto Caption Studio"},
]
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
URL_RE = re.compile(r"^https?://[^\s]+$", re.I)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _enabled() -> bool:
    return os.getenv("ATOMIC_FLOW_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}


def _db_path() -> Path:
    configured = os.getenv("ATOMIC_FLOW_DB_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(os.getenv("WEB_DATA_DIR", "/opt/scene-builder-web/data")) / "atomic_flow.sqlite3"


def _db() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db() -> None:
    with _db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS atomic_flow_runs (
            flow_id TEXT PRIMARY KEY, owner TEXT NOT NULL, group_name TEXT NOT NULL,
            script_profile TEXT NOT NULL, voice_profile TEXT NOT NULL, acs_preset TEXT NOT NULL,
            status TEXT NOT NULL, stage TEXT NOT NULL, items_json TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, error TEXT NOT NULL DEFAULT ''
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS atomic_flow_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, flow_id TEXT NOT NULL, stage TEXT NOT NULL,
            status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
        )""")


def _labels_from_sqlite(path: Path, hints: tuple[str, ...]) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    out: list[dict[str, str]] = []
    try:
        with sqlite3.connect(path, timeout=2) as conn:
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            for table in tables:
                if not any(h in table.lower() for h in hints):
                    continue
                safe = table.replace('"', '""')
                cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{safe}")')]
                key = next((c for c in cols if c.lower() in {"name", "label", "profile_name", "preset_name", "profile_id", "preset_id"}), None)
                if not key:
                    continue
                skey = key.replace('"', '""')
                for row in conn.execute(f'SELECT "{skey}" FROM "{safe}" WHERE "{skey}" IS NOT NULL LIMIT 100'):
                    value = str(row[0]).strip()
                    if value and not any(x["value"] == value for x in out):
                        out.append({"value": value, "label": value, "source": str(path)})
    except (OSError, sqlite3.Error):
        return []
    return out


def _catalog() -> dict[str, Any]:
    data_dir = Path(os.getenv("WEB_DATA_DIR", "/opt/scene-builder-web/data"))
    script_profiles = _labels_from_sqlite(data_dir / "web.sqlite3", ("preset", "profile"))
    voice_path = Path(os.getenv("ALGROW_VOICE_DB", "/home/ubuntu/apps/algrow_voice_web/shared/data/web-sidecar/algrow_voice.sqlite3"))
    voice_profiles = _labels_from_sqlite(voice_path, ("profile", "preset", "voice"))
    acs = [x.strip() for x in os.getenv("ATOMIC_ACS_PRESETS", "default").split(",") if x.strip()]
    groups = [x.strip() for x in os.getenv("ATOMIC_FLOW_GROUPS", "Default").split(",") if x.strip()]
    return {
        "read_only": True,
        "feature_enabled": _enabled(),
        "pipeline": PIPELINE,
        "groups": [{"value": x, "label": x, "source": "atomic-flow-config"} for x in groups],
        "script_profiles": script_profiles or [{"value": "default", "label": "Default", "source": "fallback"}],
        "voice_profiles": voice_profiles or [{"value": "default", "label": "Default", "source": "fallback"}],
        "acs_presets": [{"value": x, "label": x, "source": "atomic-flow-config"} for x in acs],
        "authority": {"permanent_id": "Google Sheet", "profile_settings": "owned by existing apps", "final_video": "Auto Caption Studio"},
    }


def _parse_items(raw: Any) -> list[dict[str, str]]:
    if isinstance(raw, str):
        values = [x.strip() for x in raw.splitlines() if x.strip()]
    elif isinstance(raw, list):
        values = raw
    else:
        values = []
    result: list[dict[str, str]] = []
    for item in values:
        if isinstance(item, dict):
            url = str(item.get("url") or item.get("link") or "").strip()
            permanent_id = str(item.get("permanent_id") or item.get("id") or "").strip()
        else:
            line = str(item).strip()
            parts = re.split(r"\s*\|\s*|\s+", line, maxsplit=1)
            if len(parts) == 2 and URL_RE.match(parts[1]):
                permanent_id, url = parts[0].strip(), parts[1].strip()
            else:
                url, permanent_id = line, ""
        result.append({"url": url, "permanent_id": permanent_id, "status": "ready" if URL_RE.match(url) and ID_RE.match(permanent_id) else "awaiting_sheet_id"})
    return result


def _require_login(request: Request) -> str:
    if not bool(request.session.get("authenticated")):
        raise HTTPException(401, "Login required")
    return str(request.session.get("username") or "team")


def _event(flow_id: str, stage: str, status: str, detail: str = "") -> None:
    with _db() as conn:
        conn.execute("INSERT INTO atomic_flow_events(flow_id,stage,status,detail,created_at) VALUES (?,?,?,?,?)", (flow_id, stage, status, detail, _utc()))


def register_atomic_flow(app, templates) -> None:
    _init_db()

    @app.get("/atomic-flow", response_class=HTMLResponse)
    def atomic_flow_page(request: Request):
        username = _require_login(request)
        return templates.TemplateResponse("atomic_flow.html", {"request": request, "page": "atomic_flow", "username": username})

    @app.get("/api/atomic-flow/catalog")
    def atomic_flow_catalog(request: Request):
        _require_login(request)
        return JSONResponse(_catalog())

    @app.get("/api/atomic-flow/state")
    def atomic_flow_state(request: Request):
        owner = _require_login(request)
        with _db() as conn:
            rows = conn.execute("SELECT * FROM atomic_flow_runs WHERE owner=? ORDER BY created_at DESC LIMIT 25", (owner,)).fetchall()
        return JSONResponse({"feature_enabled": _enabled(), "runs": [dict(r) for r in rows]})

    @app.post("/api/atomic-flow/validate")
    def atomic_flow_validate(request: Request, payload: dict[str, Any] = Body(default={})): 
        _require_login(request)
        items = _parse_items(payload.get("links") if isinstance(payload, dict) else None)
        return JSONResponse({"items": items, "valid": bool(items) and all(x["status"] == "ready" for x in items), "authority": "Google Sheet"})

    @app.post("/api/atomic-flow/start")
    def atomic_flow_start(request: Request, payload: dict[str, Any] = Body(default={})): 
        owner = _require_login(request)
        if not isinstance(payload, dict):
            raise HTTPException(400, "JSON object required")
        items = _parse_items(payload.get("links"))
        if not items:
            raise HTTPException(400, "At least one link is required")
        if any(x["status"] != "ready" for x in items):
            raise HTTPException(400, "Every link needs a permanent ID from the Google Sheet")
        flow_id = "AF-" + uuid.uuid4().hex[:20].upper()
        now = _utc()
        group_name = str(payload.get("group") or "Default")
        script_profile = str(payload.get("script_profile") or "default")
        voice_profile = str(payload.get("voice_profile") or "default")
        acs_preset = str(payload.get("acs_preset") or "default")
        status, stage, error = ("ready_for_adapter", "links", "")
        dispatch_url = os.getenv("ATOMIC_FLOW_DISPATCH_URL", "").strip()
        if _enabled() and dispatch_url:
            try:
                response = requests.post(dispatch_url, json={"flow_id": flow_id, "items": items, "group": group_name, "script_profile": script_profile, "voice_profile": voice_profile, "acs_preset": acs_preset, "pipeline": PIPELINE, "idempotency_key": flow_id}, headers={"Authorization": f"Bearer {os.getenv('ATOMIC_FLOW_DISPATCH_TOKEN', '')}"}, timeout=10)
                response.raise_for_status()
                status, stage = "dispatched", "script_extraction"
            except Exception as exc:
                status, stage, error = "dispatch_failed", "links", str(exc)[:240]
        elif not _enabled():
            status = "prepared_feature_disabled"
        with _db() as conn:
            conn.execute("INSERT INTO atomic_flow_runs(flow_id,owner,group_name,script_profile,voice_profile,acs_preset,status,stage,items_json,created_at,updated_at,error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (flow_id, owner, group_name, script_profile, voice_profile, acs_preset, status, stage, json.dumps(items, separators=(",", ":")), now, now, error))
        _event(flow_id, "links", status, error or "No existing queue was touched")
        return JSONResponse({"flow_id": flow_id, "status": status, "stage": stage, "items": items, "existing_queues_touched": False, "feature_enabled": _enabled()}, status_code=202)

    @app.get("/api/atomic-flow/health")
    def atomic_flow_health(request: Request):
        _require_login(request)
        return JSONResponse({"ok": True, "feature_enabled": _enabled(), "db": str(_db_path()), "pipeline_stages": len(PIPELINE)})

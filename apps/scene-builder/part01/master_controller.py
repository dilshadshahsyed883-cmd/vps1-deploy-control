from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import os

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("MASTER_CONTROLLER_DB_PATH", str(APP_DIR / "data" / "master_controller.sqlite3")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

TRANSITIONS: dict[str, tuple[str, str] | None] = {
    "SHEET_READY": ("CHATGPT_GENERATE_SCRIPT_SCENE_PLAN", "chatgpt"),
    "SCENE_PLAN_COMPLETE": ("ALGROW_GENERATE_VOICE", "algrow"),
    "AUDIO_COMPLETE": ("SCENE_BUILDER_START", "scene_builder"),
    "VIDEO_COMPLETE": ("AUTOFLOW_LUM_SCHEDULE_UPLOAD", "autoflow_lum"),
    "UPLOADED": None,
}
ACTION_SUCCESS_EVENT = {
    "CHATGPT_GENERATE_SCRIPT_SCENE_PLAN": "SCENE_PLAN_COMPLETE",
    "ALGROW_GENERATE_VOICE": "AUDIO_COMPLETE",
    "SCENE_BUILDER_START": "VIDEO_COMPLETE",
    "LUM_SCHEDULE_UPLOAD": "UPLOADED",
    "AUTOFLOW_LUM_SCHEDULE_UPLOAD": "UPLOADED",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _init_db() -> None:
    with _db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 1,
              active INTEGER NOT NULL DEFAULT 0,
              drive_folder_url TEXT NOT NULL DEFAULT '',
              master_prompt TEXT NOT NULL DEFAULT '',
              chatgpt_browser_profile TEXT NOT NULL DEFAULT '',
              chatgpt_thread_url TEXT NOT NULL DEFAULT '',
              voice_id TEXT NOT NULL DEFAULT '',
              voice_settings_json TEXT NOT NULL DEFAULT '{}',
              scene_builder_preset TEXT NOT NULL DEFAULT '',
              lum_profile TEXT NOT NULL DEFAULT '',
              timezone TEXT NOT NULL DEFAULT 'Asia/Karachi',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS project_sheets (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              project_id INTEGER NOT NULL,
              label TEXT NOT NULL DEFAULT '',
              url TEXT NOT NULL,
              tab_name TEXT NOT NULL DEFAULT '',
              sheet_gid TEXT NOT NULL DEFAULT '',
              range_a1 TEXT NOT NULL DEFAULT '',
              active INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS trigger_jobs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              project_id INTEGER NOT NULL,
              permanent_id TEXT NOT NULL,
              source_event TEXT NOT NULL,
              action TEXT NOT NULL,
              worker TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'QUEUED',
              payload_json TEXT NOT NULL DEFAULT '{}',
              dedupe_key TEXT NOT NULL UNIQUE,
              attempt_count INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              locked_at TEXT NOT NULL DEFAULT '',
              result_json TEXT NOT NULL DEFAULT '{}',
              error_text TEXT NOT NULL DEFAULT '',
              FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS controller_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              project_id INTEGER NOT NULL,
              permanent_id TEXT NOT NULL,
              event TEXT NOT NULL,
              payload_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS bridge_status (
              name TEXT PRIMARY KEY,
              profile TEXT NOT NULL DEFAULT '',
              last_seen TEXT NOT NULL,
              meta_json TEXT NOT NULL DEFAULT '{}'
            );
            """
        )
        # AUTOFLOW_STAGE3_RESOURCES_MIGRATION
        project_cols = {r[1] for r in conn.execute("PRAGMA table_info(projects)").fetchall()}
        for col in ("control_sheet_url", "script_doc_url", "scene_plan_doc_url"):
            if col not in project_cols:
                conn.execute(f"ALTER TABLE projects ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
        # AUTOFLOW_SHEET_GID_V1
        sheet_cols = {r[1] for r in conn.execute("PRAGMA table_info(project_sheets)").fetchall()}
        if "sheet_gid" not in sheet_cols:
            conn.execute("ALTER TABLE project_sheets ADD COLUMN sheet_gid TEXT NOT NULL DEFAULT ''")
        import re as _gid_re
        for _sheet_row in conn.execute("SELECT id,url FROM project_sheets WHERE sheet_gid='' OR sheet_gid IS NULL").fetchall():
            _m = _gid_re.search(r"(?:[?#&]gid=)(\d+)", str(_sheet_row["url"] or ""))
            if _m:
                conn.execute("UPDATE project_sheets SET sheet_gid=? WHERE id=?", (_m.group(1), _sheet_row["id"]))
        # AUTOFLOW_STAGE6_SCENE_AUDIO_CONTRACT
        project_cols = {r[1] for r in conn.execute("PRAGMA table_info(projects)").fetchall()}
        if "audio_engine_enabled" not in project_cols:
            conn.execute("ALTER TABLE projects ADD COLUMN audio_engine_enabled INTEGER NOT NULL DEFAULT 1")
        if "audio_engine_preset" not in project_cols:
            conn.execute("ALTER TABLE projects ADD COLUMN audio_engine_preset TEXT NOT NULL DEFAULT 'natural'")
        # AUTOFLOW_STAGE7_AUTO_CONTROLS
        project_cols = {r[1] for r in conn.execute("PRAGMA table_info(projects)").fetchall()}
        if "flow_mode" not in project_cols:
            conn.execute("ALTER TABLE projects ADD COLUMN flow_mode TEXT NOT NULL DEFAULT 'full_auto'")
        # AUTOFLOW_STAGE8_LIGHT_LUM
        project_cols = {r[1] for r in conn.execute("PRAGMA table_info(projects)").fetchall()}
        additions = {
            "autoflow_lum_enabled": "INTEGER NOT NULL DEFAULT 1",
            "autoflow_lum_sheet_metadata": "INTEGER NOT NULL DEFAULT 1",
            "autoflow_lum_schedule_mode": "TEXT NOT NULL DEFAULT 'sheet'",
            "autoflow_lum_privacy": "TEXT NOT NULL DEFAULT 'private'",
        }
        for col, ddl in additions.items():
            if col not in project_cols:
                conn.execute(f"ALTER TABLE projects ADD COLUMN {col} {ddl}")



def _json_load(value: str, default: Any) -> Any:
    try:
        return json.loads(value or "")
    except Exception:
        return default


def _require_api(request: Request) -> None:
    # AUTOFLOW_STAGE12_PC_EXTENSION_TOKEN_AUTH
    if request.session.get("username"):
        return
    import hmac
    import os
    expected = str(os.getenv("AUTOFLOW_BRIDGE_TOKEN") or "").strip()
    supplied = str(request.headers.get("X-AutoFlow-Bridge-Token") or "").strip()
    if expected and supplied and hmac.compare_digest(expected, supplied):
        return
    raise HTTPException(401, "Login or Auto Flow bridge token required")


def _project_exists(conn: sqlite3.Connection, project_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Project not found")
    return row


# AUTOFLOW_STAGE4_SCRIPT_RESOURCE_CONTRACT
def _google_resource_id(url: str, kind: str) -> str:
    import re
    url = str(url or "").strip()
    pattern = r"docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]+)" if kind == "sheet" else r"docs\.google\.com/document/d/([A-Za-z0-9_-]+)"
    match = re.search(pattern, url)
    return match.group(1) if match else ""


def _resource_readiness(project: dict[str, Any]) -> dict[str, Any]:
    # AUTOFLOW_SOT_PROJECT_SHEET_VIDEOS_ROW_LINKS_V1
    rows = {"control_sheet": ("sheet", str(project.get("control_sheet_url") or ""))}
    items = {}
    for name, (kind, url) in rows.items():
        rid = _google_resource_id(url, "sheet")
        status = "READY" if rid else ("MISSING" if not url.strip() else "INVALID")
        items[name] = {"status": status, "resource_id": rid, "configured": bool(url.strip())}
    return {"ready": all(x["status"] == "READY" for x in items.values()), "items": items, "asset_model": "video_row_links"}


def _project_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["enabled"] = bool(item["enabled"])
    item["active"] = bool(item["active"])
    item["audio_engine_enabled"] = bool(item.get("audio_engine_enabled", 1))
    item["autoflow_lum_enabled"] = bool(item.get("autoflow_lum_enabled", 1))
    item["autoflow_lum_sheet_metadata"] = bool(item.get("autoflow_lum_sheet_metadata", 1))
    item["voice_settings"] = _json_load(item.pop("voice_settings_json", "{}"), {})
    sheets = [dict(r) for r in conn.execute(
        "SELECT * FROM project_sheets WHERE project_id=? ORDER BY active DESC,id ASC", (row["id"],)
    ).fetchall()]
    for sheet in sheets:
        sheet["active"] = bool(sheet["active"])
    item["sheets"] = sheets
    item["resource_readiness"] = _resource_readiness(item)
    return item


def _row_asset_link(conn: sqlite3.Connection, project_id: int, permanent_id: str, asset_type: str) -> str:
    """Resolve per-video Sheet/asset link while preserving legacy project-doc fallback elsewhere."""
    try:
        row = conn.execute("SELECT source_url,file_url FROM autoflow_assets WHERE project_id=? AND permanent_id=? AND asset_type=?", (project_id, permanent_id, asset_type)).fetchone()
        if not row:
            return ""
        return str(row["source_url"] or row["file_url"] or "").strip()
    except Exception:
        return ""


def _enqueue_event(conn: sqlite3.Connection, project_id: int, permanent_id: str, event: str, payload: dict[str, Any]) -> dict[str, Any]:
    event = str(event or "").strip().upper()
    permanent_id = str(permanent_id or "").strip()
    if event not in TRANSITIONS:
        raise HTTPException(400, f"Unsupported event: {event}")
    if not permanent_id:
        raise HTTPException(400, "Permanent ID is required")
    project = _project_exists(conn, project_id)
    # AUTOFLOW_BACKEND_CORE_V1_EVENT_HOOK
    try:
        from autoflow_control_backend import ensure_video_event
        ensure_video_event(conn, project_id, permanent_id, event, payload)
    except Exception:
        pass
    if event == "SHEET_READY":
        payload = dict(payload)
        payload["auto_flow_resources"] = {
            "control_sheet_url": project["control_sheet_url"],
            "script_doc_url": _row_asset_link(conn, project_id, permanent_id, "script") or project["script_doc_url"],
            "script_link": _row_asset_link(conn, project_id, permanent_id, "script"),
            "scene_plan_doc_url": _row_asset_link(conn, project_id, permanent_id, "scene") or project["scene_plan_doc_url"],
            "scene_plan_link": _row_asset_link(conn, project_id, permanent_id, "scene"),
            "permanent_id": permanent_id,
        }
    if event == "AUDIO_COMPLETE":
        payload = dict(payload)
        payload["scene_builder_input"] = {
            "permanent_id": permanent_id,
            "script_doc_url": _row_asset_link(conn, project_id, permanent_id, "script") or project["script_doc_url"],
            "script_link": _row_asset_link(conn, project_id, permanent_id, "script"),
            "scene_plan_doc_url": _row_asset_link(conn, project_id, permanent_id, "scene") or project["scene_plan_doc_url"],
            "scene_plan_link": _row_asset_link(conn, project_id, permanent_id, "scene"),
            "scene_builder_preset": project["scene_builder_preset"],
            "audio_source": str(payload.get("audio_path") or payload.get("audio_url") or payload.get("drive_audio_url") or ""),
            "audio_engine": {
                "enabled": bool(project["audio_engine_enabled"]),
                "preset": str(project["audio_engine_preset"] or "natural"),
                "health_url": "http://127.0.0.1:8125/audio-engine/health",
                "position": "inside_scene_builder_handoff",
            },
            "scene_builder_submit": {
                "endpoint": "/queue/submit",
                "script_field": "script_file",
                "audio_field": "audio_file",
                "preset_field": "preset_id",
            },
        }
    if event == "VIDEO_COMPLETE":
        payload = dict(payload)
        payload["publish_contract"] = {
            "adapter": "autoflow_lum",
            "enabled": bool(project["autoflow_lum_enabled"]),
            "permanent_id": permanent_id,
            "video_source": str(payload.get("video_path") or payload.get("video_url") or payload.get("drive_video_url") or ""),
            "metadata": {
                "source": "google_sheet" if bool(project["autoflow_lum_sheet_metadata"]) else "worker_payload",
                "control_sheet_url": project["control_sheet_url"],
                "match_key": permanent_id,
            },
            "lum_profile": project["lum_profile"],
            "timezone": project["timezone"],
            "schedule_mode": project["autoflow_lum_schedule_mode"],
            "privacy": project["autoflow_lum_privacy"],
            "existing_lum": {
                "base_url": "http://127.0.0.1:8015/LUM",
                "health_url": "http://127.0.0.1:8015/LUM/health",
                "standalone_preserved": True,
            },
        }
    now = _now()
    conn.execute(
        "INSERT INTO controller_events(project_id,permanent_id,event,payload_json,created_at) VALUES(?,?,?,?,?)",
        (project_id, permanent_id, event, json.dumps(payload, ensure_ascii=False), now),
    )
    transition = TRANSITIONS[event]
    if transition is None:
        return {"ok": True, "event": event, "complete": True, "job": None}
    action, worker = transition
    if not int(project["enabled"]):
        return {"ok": True, "event": event, "paused": True, "job": None}
    dedupe = f"{project_id}:{permanent_id}:{event}:{action}"
    existing = conn.execute("SELECT * FROM trigger_jobs WHERE dedupe_key=?", (dedupe,)).fetchone()
    if existing:
        return {"ok": True, "event": event, "deduped": True, "job": dict(existing)}
    cur = conn.execute(
        """INSERT INTO trigger_jobs(project_id,permanent_id,source_event,action,worker,status,payload_json,dedupe_key,created_at,updated_at)
           VALUES(?,?,?,?,?,'QUEUED',?,?,?,?)""",
        (project_id, permanent_id, event, action, worker, json.dumps(payload, ensure_ascii=False), dedupe, now, now),
    )
    job = conn.execute("SELECT * FROM trigger_jobs WHERE id=?", (cur.lastrowid,)).fetchone()
    return {"ok": True, "event": event, "job": dict(job) if job else None}


def register_master_controller(app, templates) -> None:
    _init_db()
    # AUTOFLOW_BACKEND_CORE_V1_REGISTER
    from autoflow_control_backend import register_autoflow_control_backend
    register_autoflow_control_backend(app)

    # AUTOFLOW_FLOW_PHASE2A_REGISTER_V1
    from autoflow_flow_profiles import register_autoflow_flow_profiles
    register_autoflow_flow_profiles(app)

    # AUTOFLOW_FLOW_WORKER_PAIRING_V1
    from autoflow_flow_bridge import register_autoflow_flow_bridge
    register_autoflow_flow_bridge(app)
    # AUTOFLOW_GOOGLE_SYNC_V1_REGISTER
    from autoflow_google_sync import register_autoflow_google_sync
    register_autoflow_google_sync(app)
    # AUTOFLOW_CHATGPT_EXTENSION_V1_REGISTER
    from autoflow_chatgpt_bridge import register_autoflow_chatgpt_bridge
    register_autoflow_chatgpt_bridge(app)

    @app.get("/master-controller", response_class=HTMLResponse)
    def master_controller_page(request: Request):
        if not request.session.get("username"):
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(
            "master_controller.html",
            {"request": request, "page": "master_controller", "username": request.session.get("username", "team")},
        )

    @app.get("/api/master-controller/state")
    def master_controller_state(request: Request):
        _require_api(request)
        with _db() as conn:
            projects = [_project_dict(conn, r) for r in conn.execute("SELECT * FROM projects ORDER BY active DESC,id DESC").fetchall()]
            recent = [dict(r) for r in conn.execute(
                "SELECT * FROM trigger_jobs ORDER BY id DESC LIMIT 60"
            ).fetchall()]
            events = [dict(r) for r in conn.execute(
                "SELECT * FROM controller_events ORDER BY id DESC LIMIT 60"
            ).fetchall()]
            bridges = [dict(r) for r in conn.execute("SELECT * FROM bridge_status ORDER BY name").fetchall()]
            counts = {
                r["status"]: r["n"] for r in conn.execute(
                    "SELECT status,COUNT(*) n FROM trigger_jobs GROUP BY status"
                ).fetchall()
            }
        return {
            "ok": True,
            "projects": projects,
            "recent_jobs": recent,
            "recent_events": events,
            "bridges": bridges,
            "counts": counts,
            "transitions": [
                {"event": e, "action": v[0], "worker": v[1]} if v else {"event": e, "action": "COMPLETE", "worker": "-"}
                for e, v in TRANSITIONS.items()
            ],
        }

    @app.post("/api/master-controller/projects")
    async def create_project(request: Request):
        _require_api(request)
        body = await request.json()
        name = str(body.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "Project name is required")
        now = _now()
        with _db() as conn:
            first = conn.execute("SELECT COUNT(*) n FROM projects").fetchone()["n"] == 0
            cur = conn.execute(
                "INSERT INTO projects(name,active,created_at,updated_at) VALUES(?,?,?,?)",
                (name, 1 if first else 0, now, now),
            )
            row = conn.execute("SELECT * FROM projects WHERE id=?", (cur.lastrowid,)).fetchone()
            return {"ok": True, "project": _project_dict(conn, row)}

    @app.post("/api/master-controller/projects/{project_id}")
    async def update_project(project_id: int, request: Request):
        _require_api(request)
        body = await request.json()
        allowed = {
            "name", "drive_folder_url", "master_prompt", "chatgpt_browser_profile", "chatgpt_thread_url",
            "voice_id", "scene_builder_preset", "lum_profile", "timezone",
            "control_sheet_url", "script_doc_url", "scene_plan_doc_url",
            "audio_engine_preset",
            "autoflow_lum_schedule_mode", "autoflow_lum_privacy",
        }
        updates: list[str] = []
        values: list[Any] = []
        for key in allowed:
            if key in body:
                updates.append(f"{key}=?")
                values.append(str(body.get(key) or ""))
        if "voice_settings" in body:
            if not isinstance(body["voice_settings"], dict):
                raise HTTPException(400, "voice_settings must be a JSON object")
            updates.append("voice_settings_json=?")
            values.append(json.dumps(body["voice_settings"], ensure_ascii=False))
        if "enabled" in body:
            updates.append("enabled=?")
            values.append(1 if body["enabled"] else 0)
        if "audio_engine_enabled" in body:
            updates.append("audio_engine_enabled=?")
            values.append(1 if body["audio_engine_enabled"] else 0)
        if "autoflow_lum_enabled" in body:
            updates.append("autoflow_lum_enabled=?")
            values.append(1 if body["autoflow_lum_enabled"] else 0)
        if "autoflow_lum_sheet_metadata" in body:
            updates.append("autoflow_lum_sheet_metadata=?")
            values.append(1 if body["autoflow_lum_sheet_metadata"] else 0)
        if "flow_mode" in body:
            mode = str(body.get("flow_mode") or "full_auto").strip().lower()
            if mode not in {"full_auto", "manual"}:
                raise HTTPException(400, "flow_mode must be full_auto or manual")
            updates.append("flow_mode=?")
            values.append(mode)
        if "autoflow_lum_schedule_mode" in body:
            mode = str(body.get("autoflow_lum_schedule_mode") or "sheet").strip().lower()
            if mode not in {"sheet", "now", "manual"}:
                raise HTTPException(400, "Invalid Auto Flow LUM schedule mode")
            if "autoflow_lum_schedule_mode=?" in updates:
                values[updates.index("autoflow_lum_schedule_mode=?")] = mode
        if "autoflow_lum_privacy" in body:
            privacy = str(body.get("autoflow_lum_privacy") or "private").strip().lower()
            if privacy not in {"private", "unlisted", "public"}:
                raise HTTPException(400, "Invalid Auto Flow LUM privacy")
            if "autoflow_lum_privacy=?" in updates:
                values[updates.index("autoflow_lum_privacy=?")] = privacy
        if not updates:
            raise HTTPException(400, "Nothing to update")
        updates.append("updated_at=?")
        values.append(_now())
        values.append(project_id)
        with _db() as conn:
            _project_exists(conn, project_id)
            conn.execute(f"UPDATE projects SET {','.join(updates)} WHERE id=?", values)
            row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            return {"ok": True, "project": _project_dict(conn, row)}

    @app.delete("/api/master-controller/projects/{project_id}")
    def delete_project(project_id: int, request: Request):
        _require_api(request)
        with _db() as conn:
            _project_exists(conn, project_id)
            conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
            row = conn.execute("SELECT id FROM projects ORDER BY id LIMIT 1").fetchone()
            if row:
                conn.execute("UPDATE projects SET active=CASE WHEN id=? THEN 1 ELSE 0 END", (row["id"],))
        return {"ok": True}

    @app.post("/api/master-controller/projects/{project_id}/activate")
    def activate_project(project_id: int, request: Request):
        _require_api(request)
        with _db() as conn:
            _project_exists(conn, project_id)
            conn.execute("UPDATE projects SET active=0")
            conn.execute("UPDATE projects SET active=1,updated_at=? WHERE id=?", (_now(), project_id))
        return {"ok": True}

    @app.post("/api/master-controller/projects/{project_id}/sheets")
    async def add_project_sheet(project_id: int, request: Request):
        _require_api(request)
        body = await request.json()
        url = str(body.get("url") or "").strip()
        if not url:
            raise HTTPException(400, "Google Sheet URL is required")
        label = str(body.get("label") or "Sheet").strip() or "Sheet"
        gid = str(body.get("gid") or body.get("sheet_gid") or "").strip()
        if not gid:
            import re as _gid_re
            _m = _gid_re.search(r"(?:[?#&]gid=)(\d+)", url)
            gid = _m.group(1) if _m else ""
        if gid and not gid.isdigit():
            raise HTTPException(400, "Sheet GID must be numeric")
        with _db() as conn:
            _project_exists(conn, project_id)
            first = conn.execute("SELECT COUNT(*) n FROM project_sheets WHERE project_id=?", (project_id,)).fetchone()["n"] == 0
            cur = conn.execute(
                "INSERT INTO project_sheets(project_id,label,url,tab_name,sheet_gid,range_a1,active,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (project_id, label, url, str(body.get("tab_name") or ""), gid, str(body.get("range_a1") or ""), 1 if first else 0, _now()),
            )
            row = conn.execute("SELECT * FROM project_sheets WHERE id=?", (cur.lastrowid,)).fetchone()
        result = dict(row); result["active"] = bool(result["active"])
        return {"ok": True, "sheet": result}

    @app.delete("/api/master-controller/projects/{project_id}/sheets/{sheet_id}")
    def delete_project_sheet(project_id: int, sheet_id: int, request: Request):
        _require_api(request)
        with _db() as conn:
            _project_exists(conn, project_id)
            conn.execute("DELETE FROM project_sheets WHERE id=? AND project_id=?", (sheet_id, project_id))
            if conn.execute("SELECT COUNT(*) n FROM project_sheets WHERE project_id=? AND active=1", (project_id,)).fetchone()["n"] == 0:
                row = conn.execute("SELECT id FROM project_sheets WHERE project_id=? ORDER BY id LIMIT 1", (project_id,)).fetchone()
                if row:
                    conn.execute("UPDATE project_sheets SET active=1 WHERE id=?", (row["id"],))
        return {"ok": True}

    @app.post("/api/master-controller/projects/{project_id}/sheets/{sheet_id}/activate")
    def activate_project_sheet(project_id: int, sheet_id: int, request: Request):
        _require_api(request)
        with _db() as conn:
            _project_exists(conn, project_id)
            row = conn.execute("SELECT id FROM project_sheets WHERE id=? AND project_id=?", (sheet_id, project_id)).fetchone()
            if not row:
                raise HTTPException(404, "Sheet not found")
            conn.execute("UPDATE project_sheets SET active=0 WHERE project_id=?", (project_id,))
            conn.execute("UPDATE project_sheets SET active=1 WHERE id=?", (sheet_id,))
        return {"ok": True}

    @app.get("/api/master-controller/projects/{project_id}/resources/check")
    def check_project_resources(project_id: int, request: Request):
        _require_api(request)
        with _db() as conn:
            row = _project_exists(conn, project_id)
            project = dict(row)
        readiness = _resource_readiness(project)
        return {"ok": True, **readiness}

    @app.get("/api/master-controller/projects/{project_id}/scene-contract")
    def scene_contract(project_id: int, request: Request):
        _require_api(request)
        with _db() as conn:
            row = _project_exists(conn, project_id)
            project = _project_dict(conn, row)
        return {
            "ok": True,
            "permanent_id_source": "trigger_event",
            "script_doc_url": project.get("script_doc_url", ""),
            "scene_plan_doc_url": project.get("scene_plan_doc_url", ""),
            "scene_builder_preset": project.get("scene_builder_preset", ""),
            "audio_engine": {"enabled": bool(project.get("audio_engine_enabled", True)), "preset": project.get("audio_engine_preset", "natural")},
            "submit_contract": {"endpoint": "/queue/submit", "script_file": True, "audio_file": True, "preset_id": True},
        }

    @app.get("/api/master-controller/projects/{project_id}/publish-contract")
    def publish_contract(project_id: int, request: Request):
        _require_api(request)
        with _db() as conn:
            row = _project_exists(conn, project_id)
            project = _project_dict(conn, row)
        return {
            "ok": True,
            "adapter": "autoflow_lum",
            "enabled": project.get("autoflow_lum_enabled", True),
            "metadata_source": "google_sheet" if project.get("autoflow_lum_sheet_metadata", True) else "worker_payload",
            "control_sheet_url": project.get("control_sheet_url", ""),
            "lum_profile": project.get("lum_profile", ""),
            "timezone": project.get("timezone", "Asia/Karachi"),
            "schedule_mode": project.get("autoflow_lum_schedule_mode", "sheet"),
            "privacy": project.get("autoflow_lum_privacy", "private"),
            "old_lum_untouched": True,
        }

    @app.post("/api/master-controller/events")
    async def controller_event(request: Request):
        _require_api(request)
        body = await request.json()
        project_id = int(body.get("project_id") or 0)
        payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
        with _db() as conn:
            result = _enqueue_event(conn, project_id, body.get("permanent_id"), body.get("event"), payload)
        return result

    @app.post("/api/master-controller/projects/{project_id}/retry-failed")
    def retry_failed_project(project_id: int, request: Request):
        _require_api(request)
        with _db() as conn:
            _project_exists(conn, project_id)
            changed = conn.execute("UPDATE trigger_jobs SET status='QUEUED',error_text='',locked_at='',updated_at=? WHERE project_id=? AND status='FAILED'", (_now(), project_id)).rowcount
        return {"ok": True, "requeued": changed}

    @app.post("/api/master-controller/projects/{project_id}/run-next")
    async def run_next_project(project_id: int, request: Request):
        _require_api(request)
        body = await request.json()
        permanent_id = str(body.get("permanent_id") or "").strip()
        if not permanent_id:
            raise HTTPException(400, "Permanent ID is required")
        with _db() as conn:
            _project_exists(conn, project_id)
            row = conn.execute("SELECT * FROM trigger_jobs WHERE project_id=? AND permanent_id=? AND status='COMPLETED' ORDER BY id DESC LIMIT 1", (project_id, permanent_id)).fetchone()
            if not row:
                raise HTTPException(409, "No completed stage found for this permanent ID")
            next_event = ACTION_SUCCESS_EVENT.get(row["action"])
            if not next_event:
                return {"ok": True, "complete": True}
            payload = _json_load(row["result_json"], {})
            result = _enqueue_event(conn, project_id, permanent_id, next_event, payload if isinstance(payload, dict) else {})
        return {"ok": True, "next_event": next_event, "result": result}

    @app.post("/api/master-controller/projects/{project_id}/run-from-here")
    async def run_from_here(project_id: int, request: Request):
        _require_api(request)
        body = await request.json()
        permanent_id = str(body.get("permanent_id") or "").strip()
        event = str(body.get("event") or "").strip().upper()
        if not permanent_id or event not in TRANSITIONS:
            raise HTTPException(400, "Permanent ID and valid event are required")
        ordered_events = list(TRANSITIONS.keys())
        start = ordered_events.index(event)
        actions = [TRANSITIONS[e][0] for e in ordered_events[start:] if TRANSITIONS[e] is not None]
        with _db() as conn:
            _project_exists(conn, project_id)
            for action in actions:
                rows = conn.execute("SELECT id,dedupe_key FROM trigger_jobs WHERE project_id=? AND permanent_id=? AND action=? AND status!='SUPERSEDED'", (project_id, permanent_id, action)).fetchall()
                for row in rows:
                    conn.execute("UPDATE trigger_jobs SET status='SUPERSEDED',dedupe_key=?,updated_at=? WHERE id=?", (f"{row['dedupe_key']}:superseded:{row['id']}:{int(__import__('time').time())}", _now(), row['id']))
            result = _enqueue_event(conn, project_id, permanent_id, event, {"source": "run_from_here"})
        return {"ok": True, "event": event, "result": result}

    @app.get("/api/master-controller/workers/{worker}/next")
    def worker_next(worker: str, request: Request):
        _require_api(request)
        worker = worker.strip().lower()
        with _db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT j.* FROM trigger_jobs j JOIN projects p ON p.id=j.project_id
                   WHERE j.worker=? AND j.status='QUEUED' AND p.enabled=1
                   -- AUTOFLOW_BACKEND_CORE_V1_PAUSE_GUARD
                   AND NOT EXISTS (SELECT 1 FROM autoflow_videos v WHERE v.project_id=j.project_id AND v.permanent_id=j.permanent_id AND v.paused=1)
                   AND NOT EXISTS (SELECT 1 FROM autoflow_project_settings s WHERE s.project_id=j.project_id AND s.workflow_paused=1)
                   ORDER BY j.id LIMIT 1""",
                (worker,),
            ).fetchone()
            if not row:
                conn.commit()
                return {"ok": True, "job": None}
            now = _now()
            conn.execute(
                "UPDATE trigger_jobs SET status='RUNNING',attempt_count=attempt_count+1,locked_at=?,updated_at=? WHERE id=? AND status='QUEUED'",
                (now, now, row["id"]),
            )
            job = conn.execute("SELECT * FROM trigger_jobs WHERE id=?", (row["id"],)).fetchone()
            project = _project_exists(conn, int(job["project_id"]))
            project_payload = _project_dict(conn, project)
            conn.commit()
        data = dict(job)
        data["payload"] = _json_load(data.pop("payload_json", "{}"), {})
        data["project"] = project_payload
        return {"ok": True, "job": data}

    @app.post("/api/master-controller/workers/{worker}/jobs/{job_id}/result")
    async def worker_result(worker: str, job_id: int, request: Request):
        _require_api(request)
        body = await request.json()
        success = bool(body.get("success"))
        result = body.get("result") if isinstance(body.get("result"), dict) else {}
        error = str(body.get("error") or "")[:4000]
        with _db() as conn:
            row = conn.execute("SELECT * FROM trigger_jobs WHERE id=? AND worker=?", (job_id, worker.strip().lower())).fetchone()
            if not row:
                raise HTTPException(404, "Job not found")
            status = "COMPLETED" if success else "FAILED"
            conn.execute(
                "UPDATE trigger_jobs SET status=?,result_json=?,error_text=?,updated_at=? WHERE id=?",
                (status, json.dumps(result, ensure_ascii=False), error, _now(), job_id),
            )
            # AUTOFLOW_BACKEND_CORE_V1_RESULT_HOOK
            try:
                from autoflow_control_backend import on_worker_result
                on_worker_result(conn, row, success, result, error)
            except Exception:
                pass
            next_result = None
            if success:
                next_event = ACTION_SUCCESS_EVENT.get(row["action"])
                if next_event:
                    project = _project_exists(conn, int(row["project_id"]))
                    if str(project["flow_mode"] or "full_auto") == "full_auto":
                        merged = dict(_json_load(row["payload_json"], {})); merged.update(result)
                        next_result = _enqueue_event(conn, int(row["project_id"]), row["permanent_id"], next_event, merged)
                    else:
                        next_result = {"ok": True, "awaiting_manual": True, "next_event": next_event}
        return {"ok": True, "status": status, "next": next_result}

    @app.post("/api/master-controller/jobs/{job_id}/retry")
    def retry_job(job_id: int, request: Request):
        _require_api(request)
        with _db() as conn:
            row = conn.execute("SELECT * FROM trigger_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise HTTPException(404, "Job not found")
            conn.execute("UPDATE trigger_jobs SET status='QUEUED',error_text='',locked_at='',updated_at=? WHERE id=?", (_now(), job_id))
        return {"ok": True}

    @app.post("/api/master-controller/bridge/heartbeat")
    async def bridge_heartbeat(request: Request):
        _require_api(request)
        body = await request.json()
        name = str(body.get("name") or "chatgpt_extension").strip() or "chatgpt_extension"
        profile = str(body.get("profile") or "").strip()
        meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
        with _db() as conn:
            conn.execute(
                """INSERT INTO bridge_status(name,profile,last_seen,meta_json) VALUES(?,?,?,?)
                   ON CONFLICT(name) DO UPDATE SET profile=excluded.profile,last_seen=excluded.last_seen,meta_json=excluded.meta_json""",
                (name, profile, _now(), json.dumps(meta, ensure_ascii=False)),
            )
        return {"ok": True}

    @app.get("/api/master-controller/health")
    def master_controller_health(request: Request):
        _require_api(request)
        with _db() as conn:
            projects = conn.execute("SELECT COUNT(*) n FROM projects").fetchone()["n"]
            queued = conn.execute("SELECT COUNT(*) n FROM trigger_jobs WHERE status='QUEUED'").fetchone()["n"]
        return {"ok": True, "projects": projects, "queued": queued, "db": str(DB_PATH)}

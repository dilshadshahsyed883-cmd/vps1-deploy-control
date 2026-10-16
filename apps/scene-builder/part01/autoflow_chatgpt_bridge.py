from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import string
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request, WebSocket, WebSocketDisconnect

from autoflow_control_backend import DB_PATH, _db, _now, _require_api

PAIR_TTL_MINUTES = 0  # permanent pairing key; manual deletion only
LEASE_MINUTES = 12
WORKER_ONLINE_SECONDS = 45
DEFAULT_PROJECT_PARALLEL = 3
DEFAULT_GLOBAL_PARALLEL = 8
MAX_PARALLEL_HARD = 20
WS_CONNECTIONS: dict[str, WebSocket] = {}


def _hash(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _dt(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def _iso_after(minutes: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def init_chatgpt_schema() -> None:
    with _db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS autoflow_chatgpt_pairings (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              code_hash TEXT NOT NULL UNIQUE,
              expires_at TEXT NOT NULL,
              used_at TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS autoflow_chatgpt_workers (
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              account_label TEXT NOT NULL DEFAULT '',
              chrome_profile TEXT NOT NULL DEFAULT '',
              token_hash TEXT NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 1,
              max_parallel INTEGER NOT NULL DEFAULT 3,
              last_seen TEXT NOT NULL DEFAULT '',
              last_ip TEXT NOT NULL DEFAULT '',
              active_count INTEGER NOT NULL DEFAULT 0,
              browser_state TEXT NOT NULL DEFAULT 'UNKNOWN',
              last_error TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS autoflow_chatgpt_global_settings (
              id INTEGER PRIMARY KEY CHECK(id=1),
              max_parallel INTEGER NOT NULL DEFAULT 8,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS autoflow_chatgpt_project_settings (
              project_id INTEGER PRIMARY KEY,
              enabled INTEGER NOT NULL DEFAULT 1,
              max_parallel INTEGER NOT NULL DEFAULT 3,
              preferred_worker_id TEXT NOT NULL DEFAULT '',
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS autoflow_chatgpt_conversations (
              project_id INTEGER NOT NULL,
              permanent_id TEXT NOT NULL,
              chat_url TEXT NOT NULL DEFAULT '',
              chat_ref TEXT NOT NULL DEFAULT '',
              worker_id TEXT NOT NULL DEFAULT '',
              account_label TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'ACTIVE',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              PRIMARY KEY(project_id, permanent_id)
            );

            CREATE TABLE IF NOT EXISTS autoflow_chatgpt_jobs (
              id TEXT PRIMARY KEY,
              project_id INTEGER,
              permanent_id TEXT NOT NULL DEFAULT '',
              task_type TEXT NOT NULL,
              mode TEXT NOT NULL DEFAULT 'AUTO',
              status TEXT NOT NULL DEFAULT 'QUEUED',
              priority INTEGER NOT NULL DEFAULT 100,
              prompt_text TEXT NOT NULL DEFAULT '',
              prompt_version TEXT NOT NULL DEFAULT '',
              input_json TEXT NOT NULL DEFAULT '{}',
              output_json TEXT NOT NULL DEFAULT '{}',
              preferred_worker_id TEXT NOT NULL DEFAULT '',
              worker_id TEXT NOT NULL DEFAULT '',
              lease_hash TEXT NOT NULL DEFAULT '',
              lease_expires_at TEXT NOT NULL DEFAULT '',
              attempt_count INTEGER NOT NULL DEFAULT 0,
              response_text TEXT NOT NULL DEFAULT '',
              response_json TEXT NOT NULL DEFAULT '{}',
              chat_url TEXT NOT NULL DEFAULT '',
              chat_ref TEXT NOT NULL DEFAULT '',
              error_code TEXT NOT NULL DEFAULT '',
              error_text TEXT NOT NULL DEFAULT '',
              source_trigger_job_id INTEGER,
              test_only INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              started_at TEXT NOT NULL DEFAULT '',
              completed_at TEXT NOT NULL DEFAULT '',
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_af_chatgpt_jobs_queue
              ON autoflow_chatgpt_jobs(status, priority, created_at);
            CREATE INDEX IF NOT EXISTS idx_af_chatgpt_jobs_worker
              ON autoflow_chatgpt_jobs(worker_id, status);
            CREATE INDEX IF NOT EXISTS idx_af_chatgpt_jobs_video
              ON autoflow_chatgpt_jobs(project_id, permanent_id, created_at DESC);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_af_chatgpt_source_trigger_unique
              ON autoflow_chatgpt_jobs(source_trigger_job_id) WHERE source_trigger_job_id IS NOT NULL;
            """
        )
        # AUTOFLOW_CHATGPT_PROMPT_LIBRARY_V2_SCHEMA
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS autoflow_chatgpt_prompt_library (
              project_id INTEGER NOT NULL,
              slot TEXT NOT NULL,
              prompt_text TEXT NOT NULL DEFAULT '',
              enabled INTEGER NOT NULL DEFAULT 1,
              version INTEGER NOT NULL DEFAULT 1,
              updated_at TEXT NOT NULL,
              PRIMARY KEY(project_id, slot)
            );
            CREATE TABLE IF NOT EXISTS autoflow_chatgpt_prompt_versions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              project_id INTEGER NOT NULL,
              slot TEXT NOT NULL,
              version INTEGER NOT NULL,
              prompt_text TEXT NOT NULL DEFAULT '',
              enabled INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_af_chatgpt_prompt_versions
              ON autoflow_chatgpt_prompt_versions(project_id, slot, version DESC);
            CREATE TABLE IF NOT EXISTS autoflow_chatgpt_task_routes (
              project_id INTEGER NOT NULL,
              task_type TEXT NOT NULL,
              prompt_slots_json TEXT NOT NULL DEFAULT '[]',
              expected_json TEXT NOT NULL DEFAULT '{}',
              routes_json TEXT NOT NULL DEFAULT '{}',
              enabled INTEGER NOT NULL DEFAULT 1,
              updated_at TEXT NOT NULL,
              PRIMARY KEY(project_id, task_type)
            );
            """
        )
        # Metadata tags are project/video state; thumbnail prompt lives in thumbnail asset text_content.
        video_cols = {r[1] for r in conn.execute("PRAGMA table_info(autoflow_videos)").fetchall()}
        if "tags" not in video_cols:
            conn.execute("ALTER TABLE autoflow_videos ADD COLUMN tags TEXT NOT NULL DEFAULT ''")
        now = _now()
        conn.execute("INSERT OR IGNORE INTO autoflow_chatgpt_global_settings(id,max_parallel,updated_at) VALUES(1,?,?)", (DEFAULT_GLOBAL_PARALLEL, now))
        conn.execute(
            """INSERT OR IGNORE INTO autoflow_chatgpt_project_settings(project_id,updated_at)
               SELECT id,? FROM projects""",
            (now,),
        )



def _json_obj(value: str | None) -> dict[str, Any]:
    try:
        out = json.loads(value or "{}")
        return out if isinstance(out, dict) else {}
    except Exception:
        return {}


PROMPT_SLOTS = ("MASTER", "SCRIPT", "SCENE_PLAN", "THUMBNAIL", "METADATA")

DEFAULT_PROMPTS: dict[str, str] = {
    "MASTER": """You are the content-production intelligence layer inside Auto Flow. Obey the project instructions, preserve the supplied Permanent ID exactly, use only supported source facts, and return only the output schema requested by Auto Flow. Do not claim that you wrote to Google Docs, Sheets, Drive, YouTube, or any external system; Auto Flow performs those writes after validation.""",
    "SCRIPT": """Create the final narration script for this video. Follow the project's language, tone, duration, audience, structure, and factual constraints from the source data and master instructions. Output clean narration content, not production commentary.""",
    "SCENE_PLAN": """Create the final scene plan that matches the narration and project visual rules. Keep it practical for Scene Builder, preserve chronology, cover the full script, avoid unsupported visuals, and make the plan directly usable by downstream automation.""",
    "THUMBNAIL": """Create one production-ready thumbnail-generation prompt that follows the project's thumbnail rules and the video's actual topic. Make the visual concept clear, high-contrast and specific without inventing facts. Return the prompt text in the required JSON field.""",
    "METADATA": """Create final YouTube metadata for this exact video: a strong accurate title, a useful description, and relevant tags. Follow the project's SEO and style rules, avoid misleading claims, and return only the required JSON fields.""",
}

DEFAULT_TASK_ROUTES: dict[str, dict[str, Any]] = {
    "CHATGPT_GENERATE_SCRIPT_SCENE_PLAN": {
        "slots": ["MASTER", "SCRIPT", "SCENE_PLAN"],
        "required": ["script_text", "scene_plan_text"],
        "routes": {
            "script_text": {"target": "asset", "asset_type": "script", "google": "script_doc"},
            "scene_plan_text": {"target": "asset", "asset_type": "scene", "google": "scene_plan_doc"},
        },
    },
    "CHATGPT_GENERATE_SCRIPT": {
        "slots": ["MASTER", "SCRIPT"],
        "required": ["script_text"],
        "routes": {"script_text": {"target": "asset", "asset_type": "script", "google": "script_doc"}},
    },
    "CHATGPT_GENERATE_SCENE_PLAN": {
        "slots": ["MASTER", "SCENE_PLAN"],
        "required": ["scene_plan_text"],
        "routes": {"scene_plan_text": {"target": "asset", "asset_type": "scene", "google": "scene_plan_doc"}},
    },
    "CHATGPT_GENERATE_METADATA": {
        "slots": ["MASTER", "METADATA"],
        "required": ["title", "description", "tags"],
        "routes": {
            "title": {"target": "video", "field": "title", "google": "sheet"},
            "description": {"target": "video", "field": "description", "google": "sheet"},
            "tags": {"target": "video", "field": "tags", "google": "sheet"},
        },
    },
    "CHATGPT_GENERATE_THUMBNAIL_PROMPT": {
        "slots": ["MASTER", "THUMBNAIL"],
        "required": ["thumbnail_prompt"],
        "routes": {"thumbnail_prompt": {"target": "asset", "asset_type": "thumbnail", "field": "text_content", "google": "sheet"}},
    },
    "CHATGPT_GENERATE_CONTENT_PACKAGE": {
        "slots": ["MASTER", "SCRIPT", "SCENE_PLAN", "METADATA", "THUMBNAIL"],
        "required": ["script_text", "scene_plan_text", "title", "description", "tags", "thumbnail_prompt"],
        "routes": {
            "script_text": {"target": "asset", "asset_type": "script", "google": "script_doc"},
            "scene_plan_text": {"target": "asset", "asset_type": "scene", "google": "scene_plan_doc"},
            "title": {"target": "video", "field": "title", "google": "sheet"},
            "description": {"target": "video", "field": "description", "google": "sheet"},
            "tags": {"target": "video", "field": "tags", "google": "sheet"},
            "thumbnail_prompt": {"target": "asset", "asset_type": "thumbnail", "field": "text_content", "google": "sheet"},
        },
    },
}


def _normalize_slot(slot: str) -> str:
    slot = str(slot or "").strip().upper().replace(" ", "_")
    if slot not in PROMPT_SLOTS:
        raise HTTPException(400, f"Invalid prompt slot: {slot}")
    return slot


def _ensure_project_prompt_library(conn: sqlite3.Connection, project_id: int) -> None:
    project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project:
        raise HTTPException(404, "Project not found")
    now = _now()
    existing_master = str(project["master_prompt"] or "").strip() if "master_prompt" in project.keys() else ""
    for slot in PROMPT_SLOTS:
        text = existing_master if slot == "MASTER" and existing_master else DEFAULT_PROMPTS[slot]
        conn.execute(
            """INSERT OR IGNORE INTO autoflow_chatgpt_prompt_library(project_id,slot,prompt_text,enabled,version,updated_at)
               VALUES(?,?,?,1,1,?)""",
            (project_id, slot, text, now),
        )
    for task_type, cfg in DEFAULT_TASK_ROUTES.items():
        conn.execute(
            """INSERT OR IGNORE INTO autoflow_chatgpt_task_routes
               (project_id,task_type,prompt_slots_json,expected_json,routes_json,enabled,updated_at)
               VALUES(?,?,?,?,?,1,?)""",
            (
                project_id, task_type, json.dumps(cfg["slots"]),
                json.dumps({"type": "json", "required": cfg["required"]}),
                json.dumps(cfg["routes"]), now,
            ),
        )


def _prompt_library_rows(conn: sqlite3.Connection, project_id: int) -> list[dict[str, Any]]:
    _ensure_project_prompt_library(conn, project_id)
    rows = conn.execute(
        "SELECT * FROM autoflow_chatgpt_prompt_library WHERE project_id=? ORDER BY CASE slot WHEN 'MASTER' THEN 1 WHEN 'SCRIPT' THEN 2 WHEN 'SCENE_PLAN' THEN 3 WHEN 'THUMBNAIL' THEN 4 ELSE 5 END",
        (project_id,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r); d["enabled"] = bool(d["enabled"]); out.append(d)
    return out


def _task_route(conn: sqlite3.Connection, project_id: int, task_type: str) -> dict[str, Any]:
    _ensure_project_prompt_library(conn, project_id)
    task_type = str(task_type or "").strip().upper()
    row = conn.execute(
        "SELECT * FROM autoflow_chatgpt_task_routes WHERE project_id=? AND task_type=?",
        (project_id, task_type),
    ).fetchone()
    if not row:
        raise HTTPException(400, f"No prompt route configured for task: {task_type}")
    d = dict(row)
    return {
        "project_id": project_id, "task_type": task_type, "enabled": bool(d["enabled"]),
        "slots": json.loads(d["prompt_slots_json"] or "[]"),
        "expected": _json_obj(d["expected_json"]), "routes": _json_obj(d["routes_json"]),
        "updated_at": d["updated_at"],
    }


def _compose_library_prompt(conn: sqlite3.Connection, project_id: int, permanent_id: str, task_type: str, payload: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
    project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project:
        raise HTTPException(404, "Project not found")
    route = _task_route(conn, project_id, task_type)
    if not route["enabled"]:
        raise HTTPException(409, f"Prompt route disabled for {task_type}")
    prompt_rows = {r["slot"]: r for r in _prompt_library_rows(conn, project_id)}
    sections = [
        "AUTO FLOW CONTROLLED TASK",
        f"Project: {project['name']}",
        f"Permanent ID: {permanent_id}",
        f"Task Type: {task_type}",
        "Never change or invent the Permanent ID.",
    ]
    versions = []
    for slot in route["slots"]:
        r = prompt_rows.get(slot)
        if not r or not r["enabled"]:
            continue
        text = str(r["prompt_text"] or "").strip()
        if text:
            sections += [f"{slot.replace('_',' ')} INSTRUCTIONS", text]
        versions.append(f"{slot}@{int(r['version']) if r else 0}")
    required = [str(x) for x in (route["expected"].get("required") or [])]
    if required:
        sample = {k: f"<{k}>" for k in required}
        sections += [
            "OUTPUT CONTRACT",
            "Return ONLY one valid JSON object. Do not use Markdown fences or commentary outside JSON.",
            "Required JSON keys: " + ", ".join(required),
            json.dumps(sample, ensure_ascii=False, indent=2),
        ]
    sections += [
        "AUTO FLOW OUTPUT ROUTING",
        json.dumps(route["routes"], ensure_ascii=False, indent=2),
        "Auto Flow performs all destination writes after validating your response. Do not claim to have written or uploaded anything yourself.",
        "SOURCE DATA (JSON)",
        json.dumps(payload or {}, ensure_ascii=False, indent=2),
    ]
    return "\n\n".join(sections).strip(), {"type":"json","required":required,"routes":route["routes"]}, "PROMPT_LIBRARY_V2:" + ",".join(versions)


def _legacy_prompt(conn: sqlite3.Connection, trigger: sqlite3.Row) -> tuple[str, dict[str, Any], dict[str, Any], str]:
    project = conn.execute("SELECT * FROM projects WHERE id=?", (trigger["project_id"],)).fetchone()
    if not project:
        raise RuntimeError("Source project no longer exists")
    payload = _json_obj(trigger["payload_json"])
    permanent_id = str(trigger["permanent_id"] or "").strip()
    action = str(trigger["action"] or "").strip().upper()
    # AUTOFLOW_CHATGPT_ROW_DESTINATIONS_V1
    def _row_destination(asset_type: str) -> str:
        try:
            r = conn.execute("SELECT source_url,file_url FROM autoflow_assets WHERE project_id=? AND permanent_id=? AND asset_type=?", (int(trigger["project_id"]), permanent_id, asset_type)).fetchone()
            return str((r["source_url"] or r["file_url"] or "") if r else "").strip()
        except Exception:
            return ""
    if action in DEFAULT_TASK_ROUTES:
        prompt, expected, version = _compose_library_prompt(conn, int(trigger["project_id"]), permanent_id, action, payload)
        expected["destinations"] = {
            "script": _row_destination("script") or (project["script_doc_url"] if "script_doc_url" in project.keys() else ""),
            "scene_plan": _row_destination("scene") or (project["scene_plan_doc_url"] if "scene_plan_doc_url" in project.keys() else ""),
            "sheet": project["control_sheet_url"] if "control_sheet_url" in project.keys() else "",
            "drive": project["drive_folder_url"] if "drive_folder_url" in project.keys() else "",
        }
        return prompt, payload, expected, version
    # Safe fallback for older/custom Auto Flow actions.
    task = f"""AUTO FLOW TASK\nProject: {project['name']}\nPermanent ID: {permanent_id}\nAction: {action}\n\nComplete the requested workflow task using the supplied source data. Return the final result only. Auto Flow performs destination writes."""
    master = str(project["master_prompt"] or "").strip() if "master_prompt" in project.keys() else ""
    parts = (["PROJECT MASTER INSTRUCTIONS", master] if master else []) + [task, "SOURCE DATA (JSON)", json.dumps(payload, ensure_ascii=False, indent=2)]
    return "\n\n".join(parts).strip(), payload, {"type":"text"}, "LEGACY_FALLBACK_V1"

def _import_legacy_trigger_jobs(conn: sqlite3.Connection, limit: int = 60) -> int:
    """Mirror existing Auto Flow chatgpt trigger jobs into the leased extension queue.

    This is called only by an authenticated extension worker while claiming work. It
    preserves Auto Flow's existing trigger_jobs table as the workflow source of truth.
    """
    rows = conn.execute(
        """SELECT j.* FROM trigger_jobs j JOIN projects p ON p.id=j.project_id
           WHERE j.worker='chatgpt' AND j.status='QUEUED' AND p.enabled=1
           ORDER BY j.id LIMIT ?""",
        (max(1, min(200, int(limit))),),
    ).fetchall()
    imported = 0
    now = _now()
    for trigger in rows:
        exists = conn.execute(
            "SELECT id FROM autoflow_chatgpt_jobs WHERE source_trigger_job_id=?",
            (trigger["id"],),
        ).fetchone()
        if exists:
            # A previous bridge record owns this source job. Keep the generic worker
            # from racing it if the source was manually returned to QUEUED.
            conn.execute(
                "UPDATE trigger_jobs SET status='BRIDGED',updated_at=? WHERE id=? AND status='QUEUED'",
                (now, trigger["id"]),
            )
            continue
        prompt, input_data, output_spec, prompt_version = _legacy_prompt(conn, trigger)
        project_settings = _project_settings(conn, int(trigger["project_id"]))
        preferred = str(project_settings.get("preferred_worker_id") or "")
        jid = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO autoflow_chatgpt_jobs
               (id,project_id,permanent_id,task_type,mode,status,priority,prompt_text,prompt_version,
                input_json,output_json,preferred_worker_id,source_trigger_job_id,test_only,created_at,updated_at)
               VALUES(?,?,?,?,?,'QUEUED',?,?,?,?,?,?,?,?,?,?)""",
            (
                jid, int(trigger["project_id"]), str(trigger["permanent_id"] or ""),
                str(trigger["action"] or "CHATGPT").upper(), "AUTO", 50, prompt,
                prompt_version, json.dumps(input_data, ensure_ascii=False),
                json.dumps(output_spec, ensure_ascii=False), preferred, int(trigger["id"]), 0, now, now,
            ),
        )
        conn.execute(
            "UPDATE trigger_jobs SET status='BRIDGED',locked_at='',updated_at=? WHERE id=? AND status='QUEUED'",
            (now, trigger["id"]),
        )
        imported += 1
    return imported


def _legacy_result_valid(action: str, result: dict[str, Any]) -> tuple[bool, str]:
    action = str(action or "").upper()
    if action == "CHATGPT_GENERATE_SCRIPT_SCENE_PLAN":
        script = str(result.get("script_text") or result.get("script") or "").strip()
        scene = str(result.get("scene_plan_text") or result.get("scene_plan") or "").strip()
        if not script or not scene:
            return False, "Expected JSON fields script_text and scene_plan_text"
    return True, ""


def _fail_source_trigger(conn: sqlite3.Connection, bridge_row: sqlite3.Row, code: str, text: str) -> None:
    source_id = bridge_row["source_trigger_job_id"]
    if source_id is None:
        return
    src = conn.execute("SELECT * FROM trigger_jobs WHERE id=?", (source_id,)).fetchone()
    if not src:
        return
    now = _now()
    conn.execute(
        "UPDATE trigger_jobs SET status='FAILED',error_text=?,result_json='{}',updated_at=? WHERE id=?",
        (f"{code}: {text}"[:4000], now, source_id),
    )
    try:
        from autoflow_control_backend import on_worker_result
        on_worker_result(conn, src, False, {}, f"{code}: {text}"[:4000])
    except Exception:
        pass


def _complete_source_trigger(conn: sqlite3.Connection, bridge_row: sqlite3.Row, result: dict[str, Any]) -> dict[str, Any] | None:
    source_id = bridge_row["source_trigger_job_id"]
    if source_id is None:
        return None
    src = conn.execute("SELECT * FROM trigger_jobs WHERE id=?", (source_id,)).fetchone()
    if not src:
        raise RuntimeError("Original Auto Flow trigger job was not found")
    valid, reason = _legacy_result_valid(src["action"], result)
    if not valid:
        raise ValueError(reason)
    now = _now()
    conn.execute(
        "UPDATE trigger_jobs SET status='COMPLETED',result_json=?,error_text='',updated_at=? WHERE id=?",
        (json.dumps(result, ensure_ascii=False), now, source_id),
    )
    try:
        from autoflow_control_backend import on_worker_result
        on_worker_result(conn, src, True, result, "")
    except Exception:
        # Keep parity with the existing generic worker route, which treats the
        # hook as best-effort while still recording the trigger result.
        pass
    next_result = None
    try:
        import master_controller as mc
        next_event = mc.ACTION_SUCCESS_EVENT.get(src["action"])
        if next_event:
            project = conn.execute("SELECT * FROM projects WHERE id=?", (src["project_id"],)).fetchone()
            if project and str(project["flow_mode"] or "full_auto") == "full_auto":
                merged = _json_obj(src["payload_json"])
                merged.update(result)
                next_result = mc._enqueue_event(conn, int(src["project_id"]), src["permanent_id"], next_event, merged)
            else:
                next_result = {"ok": True, "awaiting_manual": True, "next_event": next_event}
    except Exception:
        # The source trigger is already safely completed; a later manual Run Next
        # remains available if a downstream enqueue ever fails.
        next_result = {"ok": False, "next_enqueue_deferred": True}
    return next_result


def _expected_valid(expected: dict[str, Any], result: dict[str, Any]) -> tuple[bool, str]:
    required = [str(x) for x in (expected.get("required") or [])]
    missing = []
    for key in required:
        value = result.get(key)
        if isinstance(value, str):
            ok = bool(value.strip())
        elif isinstance(value, list):
            ok = bool(value)
        else:
            ok = value is not None
        if not ok:
            missing.append(key)
    return (not missing, "" if not missing else "Missing required output fields: " + ", ".join(missing))


def _apply_direct_routes(conn: sqlite3.Connection, bridge_row: sqlite3.Row, result: dict[str, Any]) -> dict[str, Any]:
    if bridge_row["project_id"] is None or not str(bridge_row["permanent_id"] or "").strip():
        return {"applied": False, "reason": "no_project_video"}
    project_id = int(bridge_row["project_id"]); permanent_id = str(bridge_row["permanent_id"])
    spec = _json_obj(bridge_row["output_json"]); routes = spec.get("routes") if isinstance(spec.get("routes"), dict) else {}
    if not routes:
        return {"applied": False, "reason": "no_routes"}
    from autoflow_control_backend import _dependency_after, _ensure_video, _mark_outdated, _upsert_asset
    video = _ensure_video(conn, project_id, permanent_id)
    changed_assets: list[str] = []
    changed_video = False
    for key, route in routes.items():
        if key not in result or not isinstance(route, dict):
            continue
        value = result.get(key)
        target = str(route.get("target") or "")
        if target == "asset":
            asset_type = str(route.get("asset_type") or "")
            text = str(value or "")
            if asset_type in ("script", "scene", "thumbnail") and text.strip():
                current = conn.execute("SELECT * FROM autoflow_assets WHERE project_id=? AND permanent_id=? AND asset_type=?", (project_id, permanent_id, asset_type)).fetchone()
                old_text = str(current["text_content"] or "") if current else ""
                _upsert_asset(conn, project_id, permanent_id, asset_type, text_content=text, mode="AUTO", locked=False, outdated=False, save_previous=bool(current and old_text != text))
                if old_text and old_text != text and asset_type in ("script", "scene"):
                    _mark_outdated(conn, project_id, permanent_id, _dependency_after(asset_type))
                changed_assets.append(asset_type)
        elif target == "video":
            field = str(route.get("field") or "")
            if field in ("title", "description", "tags"):
                if field == "tags" and isinstance(value, list):
                    value = ", ".join(str(x).strip() for x in value if str(x).strip())
                conn.execute(f"UPDATE autoflow_videos SET {field}=?,updated_at=? WHERE project_id=? AND permanent_id=?", (str(value or ""), _now(), project_id, permanent_id))
                changed_video = True
    # Reuse existing Google outbox. It remains the only writer to Sheet/Docs.
    try:
        from autoflow_google_sync import queue_asset_push, queue_video_push
        if "script" in changed_assets:
            queue_asset_push(conn, project_id, permanent_id, "script", str(result.get("script_text") or ""))
        if "scene" in changed_assets:
            queue_asset_push(conn, project_id, permanent_id, "scene", str(result.get("scene_plan_text") or ""))
        if changed_video or changed_assets:
            queue_video_push(conn, project_id, permanent_id)
    except Exception:
        pass
    return {"applied": True, "assets": changed_assets, "video_metadata": changed_video}


def _clean_expired(conn: sqlite3.Connection) -> None:
    now = datetime.now(timezone.utc)
    rows = conn.execute(
        "SELECT id,lease_expires_at FROM autoflow_chatgpt_jobs WHERE status='RUNNING' AND lease_expires_at!=''"
    ).fetchall()
    for row in rows:
        exp = _dt(row["lease_expires_at"])
        if exp and exp <= now:
            now_s = _now()
            conn.execute(
                """UPDATE autoflow_chatgpt_jobs
                   SET status='QUEUED',worker_id='',lease_hash='',lease_expires_at='',
                       error_code='LEASE_EXPIRED',error_text='Worker lease expired; safely requeued',updated_at=?
                   WHERE id=? AND status='RUNNING'""",
                (now_s, row["id"]),
            )
            src = conn.execute("SELECT source_trigger_job_id FROM autoflow_chatgpt_jobs WHERE id=?", (row["id"],)).fetchone()
            if src and src["source_trigger_job_id"] is not None:
                conn.execute("UPDATE trigger_jobs SET status='BRIDGED',locked_at='',updated_at=? WHERE id=? AND status='RUNNING'", (now_s, src["source_trigger_job_id"]))


def _global_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM autoflow_chatgpt_global_settings WHERE id=1").fetchone()
    if not row:
        conn.execute("INSERT INTO autoflow_chatgpt_global_settings(id,max_parallel,updated_at) VALUES(1,?,?)", (DEFAULT_GLOBAL_PARALLEL, _now()))
        row = conn.execute("SELECT * FROM autoflow_chatgpt_global_settings WHERE id=1").fetchone()
    return dict(row)


def _project_settings(conn: sqlite3.Connection, project_id: int | None) -> dict[str, Any]:
    if project_id is None:
        return {"enabled": True, "max_parallel": DEFAULT_PROJECT_PARALLEL, "preferred_worker_id": ""}
    row = conn.execute("SELECT * FROM autoflow_chatgpt_project_settings WHERE project_id=?", (project_id,)).fetchone()
    if not row:
        conn.execute(
            "INSERT INTO autoflow_chatgpt_project_settings(project_id,updated_at) VALUES(?,?)",
            (project_id, _now()),
        )
        row = conn.execute("SELECT * FROM autoflow_chatgpt_project_settings WHERE project_id=?", (project_id,)).fetchone()
    out = dict(row)
    out["enabled"] = bool(out["enabled"])
    return out


def _worker_auth_values(worker_id: str, token: str) -> dict[str, Any]:
    if not worker_id or not token:
        raise HTTPException(401, "Worker credentials required")
    with _db() as conn:
        row = conn.execute("SELECT * FROM autoflow_chatgpt_workers WHERE id=?", (worker_id,)).fetchone()
    if not row or not int(row["enabled"]):
        raise HTTPException(401, "Worker is disabled or unknown")
    if not hmac.compare_digest(str(row["token_hash"]), _hash(token)):
        raise HTTPException(401, "Invalid worker token")
    return dict(row)


def _worker_auth(request: Request) -> dict[str, Any]:
    return _worker_auth_values(
        str(request.headers.get("X-AutoFlow-Worker-Id") or "").strip(),
        str(request.headers.get("X-AutoFlow-Worker-Token") or "").strip(),
    )


def _worker_public(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    d = dict(row)
    last = _dt(d.get("last_seen", ""))
    online = bool(last and (datetime.now(timezone.utc) - last).total_seconds() <= WORKER_ONLINE_SECONDS and d.get("enabled"))
    return {
        "id": d["id"], "name": d["name"], "account_label": d.get("account_label", ""),
        "chrome_profile": d.get("chrome_profile", ""), "enabled": bool(d.get("enabled")),
        "max_parallel": int(d.get("max_parallel") or 1), "active_count": int(d.get("active_count") or 0),
        "browser_state": d.get("browser_state", "UNKNOWN"), "last_seen": d.get("last_seen", ""),
        "last_error": d.get("last_error", ""), "online": online,
    }


def _conversation(conn: sqlite3.Connection, project_id: int | None, permanent_id: str) -> dict[str, Any] | None:
    if project_id is None or not permanent_id:
        return None
    row = conn.execute(
        "SELECT * FROM autoflow_chatgpt_conversations WHERE project_id=? AND permanent_id=?",
        (project_id, permanent_id),
    ).fetchone()
    return dict(row) if row else None


def _resolved_mode(job: dict[str, Any], conv: dict[str, Any] | None) -> str:
    mode = str(job.get("mode") or "AUTO").upper()
    if mode == "AUTO":
        return "FOLLOW_UP" if conv and conv.get("chat_url") else "NEW_CHAT"
    if mode not in ("NEW_CHAT", "FOLLOW_UP"):
        return "NEW_CHAT"
    return mode


def _job_public(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    d = dict(row)
    return {
        "id": d["id"], "project_id": d.get("project_id"), "permanent_id": d.get("permanent_id", ""),
        "task_type": d.get("task_type", ""), "mode": d.get("mode", "AUTO"), "status": d.get("status", ""),
        "priority": d.get("priority", 100), "worker_id": d.get("worker_id", ""),
        "attempt_count": d.get("attempt_count", 0), "chat_url": d.get("chat_url", ""),
        "chat_ref": d.get("chat_ref", ""), "error_code": d.get("error_code", ""),
        "error_text": d.get("error_text", ""), "created_at": d.get("created_at", ""),
        "started_at": d.get("started_at", ""), "completed_at": d.get("completed_at", ""),
        "updated_at": d.get("updated_at", ""), "test_only": bool(d.get("test_only", 0)),
    }


def chatgpt_attention(conn: sqlite3.Connection | None = None, project_id: int | None = None) -> list[dict[str, Any]]:
    own = conn is None
    if own:
        conn = _db()
    try:
        args: list[Any] = []
        where = "status='ERROR'"
        if project_id is not None:
            where += " AND project_id=?"; args.append(project_id)
        rows = conn.execute(
            f"SELECT * FROM autoflow_chatgpt_jobs WHERE {where} ORDER BY updated_at DESC LIMIT 50", args
        ).fetchall()
        out = []
        for r in rows:
            out.append({
                "type": "ERROR", "project_id": r["project_id"], "permanent_id": r["permanent_id"] or "ChatGPT",
                "stage": "ChatGPT", "message": f"ChatGPT {r['error_code'] or 'job error'}: {r['error_text'] or r['task_type']}",
                "job_id": r["id"],
            })
        # Surface offline/unpaired workers whenever either bridge jobs or the
        # existing Auto Flow chatgpt trigger lane has work waiting.
        queued_bridge = conn.execute("SELECT COUNT(*) FROM autoflow_chatgpt_jobs WHERE status='QUEUED'").fetchone()[0]
        legacy_sql = "SELECT COUNT(*) FROM trigger_jobs WHERE worker='chatgpt' AND status IN ('QUEUED','BRIDGED')"
        legacy_args: tuple[Any, ...] = ()
        if project_id is not None:
            legacy_sql += " AND project_id=?"; legacy_args = (project_id,)
        queued_legacy = conn.execute(legacy_sql, legacy_args).fetchone()[0]
        queued = int(queued_bridge) + int(queued_legacy)
        if queued:
            workers = conn.execute("SELECT * FROM autoflow_chatgpt_workers WHERE enabled=1").fetchall()
            if not workers or not any(_worker_public(w)["online"] for w in workers):
                out.insert(0, {"type":"ERROR","project_id":project_id,"permanent_id":"ChatGPT Extension","stage":"ChatGPT","message":f"{queued} ChatGPT job(s) waiting; no extension worker online"})
        return out
    finally:
        if own:
            conn.close()


async def _notify_workers(message: dict[str, Any], preferred_worker_id: str = "") -> None:
    targets = [preferred_worker_id] if preferred_worker_id else list(WS_CONNECTIONS.keys())
    for wid in targets:
        ws = WS_CONNECTIONS.get(wid)
        if not ws:
            continue
        try:
            await ws.send_json(message)
        except Exception:
            WS_CONNECTIONS.pop(wid, None)


def register_autoflow_chatgpt_bridge(app) -> None:
    init_chatgpt_schema()

    @app.get("/api/master-controller/chatgpt/pairings")
    def pairing_status(request: Request):
        with _db() as conn:
            row = conn.execute("SELECT id,created_at FROM autoflow_chatgpt_pairings ORDER BY id DESC LIMIT 1").fetchone()
        return {"ok": True, "active": bool(row), "permanent": True, "created_at": str(row["created_at"] if row else "")}

    @app.post("/api/master-controller/chatgpt/pairings")
    async def create_pairing(request: Request):
        code = secrets.token_urlsafe(18)
        now = _now()
        with _db() as conn:
            # Exactly one permanent reusable pairing key is active at a time.
            conn.execute("DELETE FROM autoflow_chatgpt_pairings")
            conn.execute(
                "INSERT INTO autoflow_chatgpt_pairings(code_hash,expires_at,created_at,used_at) VALUES(?,?,?,?)",
                (_hash(code), "", now, ""),
            )
        return {"ok": True, "code": code, "permanent": True, "ttl_minutes": 0}

    @app.delete("/api/master-controller/chatgpt/pairings")
    def delete_pairing(request: Request):
        with _db() as conn:
            cur = conn.execute("DELETE FROM autoflow_chatgpt_pairings")
        return {"ok": True, "deleted": int(cur.rowcount or 0), "workers_untouched": True}

    @app.post("/api/master-controller/chatgpt/pairings/exchange")
    async def exchange_pairing(request: Request):
        body = await request.json()
        code = str(body.get("code") or "").strip()  # pairing keys are case-sensitive
        if not code:
            raise HTTPException(400, "Pairing code required")
        now = _now()
        with _db() as conn:
            row = conn.execute("SELECT * FROM autoflow_chatgpt_pairings WHERE code_hash=?", (_hash(code),)).fetchone()
            if not row:
                raise HTTPException(401, "Pairing key invalid")
            if str(row["expires_at"] or "").strip() and (_dt(row["expires_at"]) or datetime.min.replace(tzinfo=timezone.utc)) <= datetime.now(timezone.utc):
                raise HTTPException(401, "Pairing code expired")
            worker_id = str(uuid.uuid4())
            token = secrets.token_urlsafe(36)
            name = str(body.get("worker_name") or "ChatGPT Worker").strip()[:100]
            account = str(body.get("account_label") or "").strip()[:160]
            profile = str(body.get("chrome_profile") or "").strip()[:100]
            max_parallel = max(1, min(MAX_PARALLEL_HARD, int(body.get("max_parallel") or 3)))
            conn.execute(
                """INSERT INTO autoflow_chatgpt_workers
                   (id,name,account_label,chrome_profile,token_hash,max_parallel,last_seen,browser_state,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (worker_id, name, account, profile, _hash(token), max_parallel, now, "PAIRED", now, now),
            )
        return {"ok": True, "worker_id": worker_id, "worker_token": token, "max_parallel": max_parallel}

    @app.get("/api/master-controller/chatgpt/workers")
    def list_workers(request: Request):
        _require_api(request)
        with _db() as conn:
            _clean_expired(conn)
            rows = conn.execute("SELECT * FROM autoflow_chatgpt_workers ORDER BY created_at DESC").fetchall()
        return {"ok": True, "workers": [_worker_public(r) for r in rows]}

    @app.post("/api/master-controller/chatgpt/workers/{worker_id}")
    async def update_worker(worker_id: str, request: Request):
        _require_api(request); body = await request.json(); now = _now()
        sets=[]; vals=[]
        for key in ("name","account_label","chrome_profile"):
            if key in body:
                sets.append(f"{key}=?"); vals.append(str(body[key] or "").strip()[:160])
        if "enabled" in body:
            sets.append("enabled=?"); vals.append(int(bool(body["enabled"])))
        if "max_parallel" in body:
            sets.append("max_parallel=?"); vals.append(max(1,min(MAX_PARALLEL_HARD,int(body["max_parallel"]))))
        if not sets:
            raise HTTPException(400,"No supported worker fields supplied")
        sets.append("updated_at=?"); vals.append(now); vals.append(worker_id)
        with _db() as conn:
            cur=conn.execute(f"UPDATE autoflow_chatgpt_workers SET {','.join(sets)} WHERE id=?", vals)
            if not cur.rowcount: raise HTTPException(404,"Worker not found")
            row=conn.execute("SELECT * FROM autoflow_chatgpt_workers WHERE id=?",(worker_id,)).fetchone()
        return {"ok":True,"worker":_worker_public(row)}

    @app.delete("/api/master-controller/chatgpt/workers/{worker_id}")
    def revoke_worker(worker_id: str, request: Request):
        _require_api(request)
        with _db() as conn:
            cur=conn.execute("UPDATE autoflow_chatgpt_workers SET enabled=0,token_hash=?,updated_at=? WHERE id=?",(_hash(secrets.token_urlsafe(32)),_now(),worker_id))
            if not cur.rowcount: raise HTTPException(404,"Worker not found")
            conn.execute("UPDATE autoflow_chatgpt_jobs SET status='QUEUED',worker_id='',lease_hash='',lease_expires_at='',updated_at=? WHERE worker_id=? AND status='RUNNING'",(_now(),worker_id))
        WS_CONNECTIONS.pop(worker_id,None)
        return {"ok":True}

    @app.get("/api/master-controller/chatgpt/settings")
    def get_global_settings(request: Request):
        _require_api(request)
        with _db() as conn:
            settings = _global_settings(conn)
            running = conn.execute("SELECT COUNT(*) FROM autoflow_chatgpt_jobs WHERE status='RUNNING'").fetchone()[0]
            queued = conn.execute("SELECT COUNT(*) FROM autoflow_chatgpt_jobs WHERE status='QUEUED'").fetchone()[0]
        return {"ok": True, "settings": settings, "running": running, "queued": queued, "hard_max": MAX_PARALLEL_HARD}

    @app.post("/api/master-controller/chatgpt/settings")
    async def set_global_settings(request: Request):
        _require_api(request)
        body = await request.json()
        maxp = max(1, min(MAX_PARALLEL_HARD, int(body.get("max_parallel") or DEFAULT_GLOBAL_PARALLEL)))
        now = _now()
        with _db() as conn:
            _global_settings(conn)
            conn.execute("UPDATE autoflow_chatgpt_global_settings SET max_parallel=?,updated_at=? WHERE id=1", (maxp, now))
            settings = _global_settings(conn)
        return {"ok": True, "settings": settings}

    @app.get("/api/master-controller/chatgpt/projects/{project_id}/settings")
    def get_project_settings(project_id: int, request: Request):
        _require_api(request)
        with _db() as conn:
            conn.execute("SELECT 1 FROM projects WHERE id=?",(project_id,)).fetchone() or (_ for _ in ()).throw(HTTPException(404,"Project not found"))
            s=_project_settings(conn,project_id)
        return {"ok":True,"settings":s}

    @app.post("/api/master-controller/chatgpt/projects/{project_id}/settings")
    async def set_project_settings(project_id: int, request: Request):
        _require_api(request); body=await request.json(); now=_now()
        with _db() as conn:
            if not conn.execute("SELECT 1 FROM projects WHERE id=?",(project_id,)).fetchone(): raise HTTPException(404,"Project not found")
            s=_project_settings(conn,project_id)
            enabled=int(bool(body.get("enabled",s["enabled"])))
            maxp=max(1,min(MAX_PARALLEL_HARD,int(body.get("max_parallel",s["max_parallel"]))))
            pref=str(body.get("preferred_worker_id",s.get("preferred_worker_id",'')) or '').strip()
            conn.execute("UPDATE autoflow_chatgpt_project_settings SET enabled=?,max_parallel=?,preferred_worker_id=?,updated_at=? WHERE project_id=?",(enabled,maxp,pref,now,project_id))
            out=_project_settings(conn,project_id)
        return {"ok":True,"settings":out}

    # AUTOFLOW_CHATGPT_PROMPT_LIBRARY_V2_API
    @app.get("/api/master-controller/chatgpt/projects/{project_id}/prompts")
    def get_prompt_library(project_id: int, request: Request):
        _require_api(request)
        with _db() as conn:
            rows = _prompt_library_rows(conn, project_id)
            routes = [_task_route(conn, project_id, t) for t in DEFAULT_TASK_ROUTES]
        return {"ok": True, "prompts": rows, "routes": routes, "slots": list(PROMPT_SLOTS)}

    @app.post("/api/master-controller/chatgpt/projects/{project_id}/prompts/{slot}")
    async def save_prompt(project_id: int, slot: str, request: Request):
        _require_api(request); slot = _normalize_slot(slot); body = await request.json(); now = _now()
        with _db() as conn:
            _ensure_project_prompt_library(conn, project_id)
            row = conn.execute("SELECT * FROM autoflow_chatgpt_prompt_library WHERE project_id=? AND slot=?", (project_id, slot)).fetchone()
            text = str(body.get("prompt_text", row["prompt_text"] if row else ""))
            enabled = int(bool(body.get("enabled", bool(row["enabled"]) if row else True)))
            changed = (not row) or text != str(row["prompt_text"] or "") or enabled != int(row["enabled"])
            version = int(row["version"] if row else 0)
            if changed and row:
                conn.execute("INSERT INTO autoflow_chatgpt_prompt_versions(project_id,slot,version,prompt_text,enabled,created_at) VALUES(?,?,?,?,?,?)", (project_id,slot,version,row["prompt_text"],row["enabled"],now))
                version += 1
            if row:
                conn.execute("UPDATE autoflow_chatgpt_prompt_library SET prompt_text=?,enabled=?,version=?,updated_at=? WHERE project_id=? AND slot=?", (text,enabled,version,now,project_id,slot))
            else:
                version = 1
                conn.execute("INSERT INTO autoflow_chatgpt_prompt_library(project_id,slot,prompt_text,enabled,version,updated_at) VALUES(?,?,?,?,?,?)", (project_id,slot,text,enabled,version,now))
            out = conn.execute("SELECT * FROM autoflow_chatgpt_prompt_library WHERE project_id=? AND slot=?", (project_id,slot)).fetchone()
        d=dict(out); d["enabled"]=bool(d["enabled"])
        return {"ok":True,"prompt":d,"changed":changed}

    @app.get("/api/master-controller/chatgpt/projects/{project_id}/prompts/{slot}/versions")
    def prompt_versions(project_id: int, slot: str, request: Request, limit: int = 20):
        _require_api(request); slot=_normalize_slot(slot)
        with _db() as conn:
            _ensure_project_prompt_library(conn, project_id)
            rows=conn.execute("SELECT * FROM autoflow_chatgpt_prompt_versions WHERE project_id=? AND slot=? ORDER BY version DESC,id DESC LIMIT ?",(project_id,slot,max(1,min(100,int(limit))))).fetchall()
        return {"ok":True,"versions":[dict(r) for r in rows]}

    @app.post("/api/master-controller/chatgpt/projects/{project_id}/prompts/{slot}/restore/{version}")
    def restore_prompt(project_id: int, slot: str, version: int, request: Request):
        _require_api(request); slot=_normalize_slot(slot); now=_now()
        with _db() as conn:
            _ensure_project_prompt_library(conn, project_id)
            old=conn.execute("SELECT * FROM autoflow_chatgpt_prompt_versions WHERE project_id=? AND slot=? AND version=? ORDER BY id DESC LIMIT 1",(project_id,slot,int(version))).fetchone()
            cur=conn.execute("SELECT * FROM autoflow_chatgpt_prompt_library WHERE project_id=? AND slot=?",(project_id,slot)).fetchone()
            if not old or not cur: raise HTTPException(404,"Prompt version not found")
            conn.execute("INSERT INTO autoflow_chatgpt_prompt_versions(project_id,slot,version,prompt_text,enabled,created_at) VALUES(?,?,?,?,?,?)",(project_id,slot,int(cur["version"]),cur["prompt_text"],cur["enabled"],now))
            newv=int(cur["version"])+1
            conn.execute("UPDATE autoflow_chatgpt_prompt_library SET prompt_text=?,enabled=?,version=?,updated_at=? WHERE project_id=? AND slot=?",(old["prompt_text"],old["enabled"],newv,now,project_id,slot))
            row=conn.execute("SELECT * FROM autoflow_chatgpt_prompt_library WHERE project_id=? AND slot=?",(project_id,slot)).fetchone()
        d=dict(row); d["enabled"]=bool(d["enabled"])
        return {"ok":True,"prompt":d,"restored_from":int(version)}

    @app.post("/api/master-controller/chatgpt/projects/{project_id}/compose")
    async def compose_project_prompt(project_id: int, request: Request):
        _require_api(request); body=await request.json()
        permanent_id=str(body.get("permanent_id") or "PREVIEW-ID").strip() or "PREVIEW-ID"
        task_type=str(body.get("task_type") or "CHATGPT_GENERATE_SCRIPT_SCENE_PLAN").strip().upper()
        payload=body.get("input") if isinstance(body.get("input"),dict) else {}
        with _db() as conn:
            prompt,spec,version=_compose_library_prompt(conn,project_id,permanent_id,task_type,payload)
        return {"ok":True,"prompt_text":prompt,"output":spec,"prompt_version":version,"task_type":task_type}

    @app.post("/api/master-controller/chatgpt/projects/{project_id}/run")
    async def run_project_prompt(project_id: int, request: Request):
        _require_api(request); body=await request.json(); now=_now()
        permanent_id=str(body.get("permanent_id") or "").strip()
        if not permanent_id: raise HTTPException(400,"permanent_id is required")
        task_type=str(body.get("task_type") or "CHATGPT_GENERATE_SCRIPT_SCENE_PLAN").strip().upper()
        mode=str(body.get("mode") or "AUTO").upper()
        if mode not in ("AUTO","NEW_CHAT","FOLLOW_UP"): raise HTTPException(400,"Invalid mode")
        payload=body.get("input") if isinstance(body.get("input"),dict) else {}
        with _db() as conn:
            ps=_project_settings(conn,project_id)
            if not ps["enabled"]: raise HTTPException(409,"ChatGPT disabled for this project")
            if mode=="FOLLOW_UP" and not _conversation(conn,project_id,permanent_id): raise HTTPException(409,"No existing ChatGPT conversation for this video")
            prompt,spec,version=_compose_library_prompt(conn,project_id,permanent_id,task_type,payload)
            jid=str(uuid.uuid4()); pref=str(body.get("preferred_worker_id") or ps.get("preferred_worker_id") or "").strip()
            conn.execute("""INSERT INTO autoflow_chatgpt_jobs
                (id,project_id,permanent_id,task_type,mode,status,priority,prompt_text,prompt_version,input_json,output_json,preferred_worker_id,test_only,created_at,updated_at)
                VALUES(?,?,?,?,?,'QUEUED',?,?,?,?,?,?,0,?,?)""",
                (jid,project_id,permanent_id,task_type,mode,max(1,int(body.get("priority") or 80)),prompt,version,json.dumps(payload,ensure_ascii=False),json.dumps(spec,ensure_ascii=False),pref,now,now))
            row=conn.execute("SELECT * FROM autoflow_chatgpt_jobs WHERE id=?",(jid,)).fetchone()
        await _notify_workers({"type":"job_available","job_id":jid},pref)
        return {"ok":True,"job":_job_public(row),"prompt_version":version,"output":spec}

    @app.post("/api/master-controller/chatgpt/jobs")
    async def create_job(request: Request):
        _require_api(request); body=await request.json(); now=_now()
        project_id=body.get("project_id")
        if project_id is not None: project_id=int(project_id)
        permanent_id=str(body.get("permanent_id") or "").strip()
        task_type=str(body.get("task_type") or "CUSTOM").strip().upper()[:80]
        prompt_text=str(body.get("prompt_text") or body.get("instruction") or "")
        if not prompt_text.strip(): raise HTTPException(400,"prompt_text is required")
        mode=str(body.get("mode") or "AUTO").upper()
        if mode not in ("AUTO","NEW_CHAT","FOLLOW_UP"): raise HTTPException(400,"mode must be AUTO, NEW_CHAT or FOLLOW_UP")
        test_only=int(bool(body.get("test_only",False)))
        if test_only and not permanent_id: permanent_id=f"TEST-{uuid.uuid4().hex[:8].upper()}"
        if not permanent_id: raise HTTPException(400,"permanent_id is required")
        preferred=str(body.get("preferred_worker_id") or "").strip()
        jid=str(uuid.uuid4())
        with _db() as conn:
            if project_id is not None and not conn.execute("SELECT 1 FROM projects WHERE id=?",(project_id,)).fetchone(): raise HTTPException(404,"Project not found")
            if project_id is not None:
                ps=_project_settings(conn,project_id)
                if not ps["enabled"]: raise HTTPException(409,"ChatGPT disabled for this project")
                if not preferred: preferred=str(ps.get("preferred_worker_id") or "")
            if mode=="FOLLOW_UP" and not _conversation(conn,project_id,permanent_id):
                raise HTTPException(409,"No existing ChatGPT conversation for this video")
            conn.execute(
                """INSERT INTO autoflow_chatgpt_jobs
                   (id,project_id,permanent_id,task_type,mode,status,priority,prompt_text,prompt_version,input_json,output_json,preferred_worker_id,test_only,created_at,updated_at)
                   VALUES(?,?,?,?,?,'QUEUED',?,?,?,?,?,?,?,?,?)""",
                (jid,project_id,permanent_id,task_type,mode,max(1,int(body.get("priority") or 100)),prompt_text,
                 str(body.get("prompt_version") or ''),json.dumps(body.get("input") or {},ensure_ascii=False),json.dumps(body.get("output") or {},ensure_ascii=False),preferred,test_only,now,now),
            )
            row=conn.execute("SELECT * FROM autoflow_chatgpt_jobs WHERE id=?",(jid,)).fetchone()
        await _notify_workers({"type":"job_available","job_id":jid},preferred)
        return {"ok":True,"job":_job_public(row)}

    @app.get("/api/master-controller/chatgpt/jobs")
    def list_jobs(request: Request, project_id: int | None = None, status: str = "", limit: int = 100):
        _require_api(request)
        where=[]; args=[]
        if project_id is not None: where.append("project_id=?"); args.append(project_id)
        if status: where.append("status=?"); args.append(status.upper())
        sql="SELECT * FROM autoflow_chatgpt_jobs"+(" WHERE "+" AND ".join(where) if where else "")+" ORDER BY created_at DESC LIMIT ?"; args.append(max(1,min(500,int(limit))))
        with _db() as conn:
            _clean_expired(conn); rows=conn.execute(sql,args).fetchall()
        return {"ok":True,"jobs":[_job_public(r) for r in rows]}

    @app.post("/api/master-controller/chatgpt/jobs/{job_id}/retry")
    def retry_job(job_id: str, request: Request):
        _require_api(request)
        with _db() as conn:
            row=conn.execute("SELECT * FROM autoflow_chatgpt_jobs WHERE id=?",(job_id,)).fetchone()
            if not row: raise HTTPException(404,"Job not found")
            if row["status"]=="RUNNING": raise HTTPException(409,"Job is currently running")
            now = _now()
            conn.execute("UPDATE autoflow_chatgpt_jobs SET status='QUEUED',worker_id='',lease_hash='',lease_expires_at='',error_code='',error_text='',completed_at='',updated_at=? WHERE id=?",(now,job_id))
            if row["source_trigger_job_id"] is not None:
                conn.execute("UPDATE trigger_jobs SET status='BRIDGED',error_text='',locked_at='',updated_at=? WHERE id=?", (now, row["source_trigger_job_id"]))
        return {"ok":True}

    @app.post("/api/master-controller/chatgpt/jobs/{job_id}/cancel")
    def cancel_job(job_id: str, request: Request):
        _require_api(request)
        with _db() as conn:
            row = conn.execute("SELECT * FROM autoflow_chatgpt_jobs WHERE id=?", (job_id,)).fetchone()
            if not row: raise HTTPException(404,"Job not found")
            now = _now()
            conn.execute("UPDATE autoflow_chatgpt_jobs SET status='CANCELLED',lease_hash='',lease_expires_at='',completed_at=?,updated_at=? WHERE id=? AND status NOT IN ('DONE','CANCELLED')",(now,now,job_id))
            if row["source_trigger_job_id"] is not None and row["status"] not in ('DONE','CANCELLED'):
                conn.execute("UPDATE trigger_jobs SET status='SUPERSEDED',error_text='Cancelled from ChatGPT Extension bridge',updated_at=? WHERE id=?", (now, row["source_trigger_job_id"]))
        return {"ok":True}

    @app.post("/api/master-controller/chatgpt/worker/heartbeat")
    async def worker_heartbeat(request: Request):
        worker=_worker_auth(request); body=await request.json(); now=_now()
        active=max(0,int(body.get("active_count") or 0)); state=str(body.get("browser_state") or "READY")[:80]; err=str(body.get("last_error") or "")[:1000]
        with _db() as conn:
            _clean_expired(conn)
            conn.execute("UPDATE autoflow_chatgpt_workers SET last_seen=?,active_count=?,browser_state=?,last_error=?,updated_at=? WHERE id=?",(now,active,state,err,now,worker["id"]))
        return {"ok":True,"server_time":now}

    @app.post("/api/master-controller/chatgpt/worker/claim")
    async def worker_claim(request: Request):
        worker=_worker_auth(request); now=_now(); lease_token=secrets.token_urlsafe(30)
        with _db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            _clean_expired(conn)
            _import_legacy_trigger_jobs(conn)
            current=conn.execute("SELECT COUNT(*) FROM autoflow_chatgpt_jobs WHERE worker_id=? AND status='RUNNING'",(worker["id"],)).fetchone()[0]
            maxp=max(1,min(MAX_PARALLEL_HARD,int(worker["max_parallel"])))
            if str(worker.get("browser_state") or "").upper() == "ATTENTION":
                conn.commit(); return {"ok":True,"job":None,"reason":"WORKER_ATTENTION"}
            if current>=maxp:
                conn.commit(); return {"ok":True,"job":None,"reason":"WORKER_LIMIT"}
            global_max = max(1, min(MAX_PARALLEL_HARD, int(_global_settings(conn)["max_parallel"])))
            global_running = conn.execute("SELECT COUNT(*) FROM autoflow_chatgpt_jobs WHERE status='RUNNING'").fetchone()[0]
            if global_running >= global_max:
                conn.commit(); return {"ok":True,"job":None,"reason":"GLOBAL_LIMIT"}
            candidates=conn.execute("""SELECT * FROM autoflow_chatgpt_jobs
                WHERE status='QUEUED' AND (preferred_worker_id='' OR preferred_worker_id=?)
                ORDER BY priority ASC,created_at ASC LIMIT 30""",(worker["id"],)).fetchall()
            chosen=None
            for row in candidates:
                pid=row["project_id"]
                ps=_project_settings(conn,pid)
                if not ps["enabled"]: continue
                if ps.get("preferred_worker_id") and ps["preferred_worker_id"]!=worker["id"]: continue
                if pid is not None:
                    active_project=conn.execute("SELECT COUNT(*) FROM autoflow_chatgpt_jobs WHERE project_id=? AND status='RUNNING'",(pid,)).fetchone()[0]
                    if active_project>=max(1,int(ps["max_parallel"])): continue
                chosen=row; break
            if not chosen:
                conn.commit(); return {"ok":True,"job":None,"reason":"EMPTY"}
            conv=_conversation(conn,chosen["project_id"],chosen["permanent_id"])
            mode=_resolved_mode(dict(chosen),conv)
            if mode=="FOLLOW_UP" and not (conv and conv.get("chat_url")):
                conn.execute("UPDATE autoflow_chatgpt_jobs SET status='ERROR',error_code='CHAT_NOT_FOUND',error_text='Follow-up requested but no saved conversation exists',completed_at=?,updated_at=? WHERE id=?",(now,now,chosen["id"]))
                conn.commit(); return {"ok":True,"job":None,"reason":"CHAT_NOT_FOUND"}
            exp=_iso_after(LEASE_MINUTES)
            conn.execute("""UPDATE autoflow_chatgpt_jobs SET status='RUNNING',worker_id=?,lease_hash=?,lease_expires_at=?,attempt_count=attempt_count+1,started_at=CASE WHEN started_at='' THEN ? ELSE started_at END,error_code='',error_text='',updated_at=? WHERE id=? AND status='QUEUED'""",(worker["id"],_hash(lease_token),exp,now,now,chosen["id"]))
            row=conn.execute("SELECT * FROM autoflow_chatgpt_jobs WHERE id=?",(chosen["id"],)).fetchone()
            if row["source_trigger_job_id"] is not None:
                conn.execute("UPDATE trigger_jobs SET status='RUNNING',attempt_count=attempt_count+1,locked_at=?,updated_at=? WHERE id=?", (now, now, row["source_trigger_job_id"]))
            conn.execute("UPDATE autoflow_chatgpt_workers SET last_seen=?,active_count=?,browser_state='WORKING',updated_at=? WHERE id=?",(now,current+1,now,worker["id"]))
            conn.commit()
        payload={
            "id":row["id"],"project_id":row["project_id"],"permanent_id":row["permanent_id"],"task_type":row["task_type"],
            "mode":mode,"prompt_text":row["prompt_text"],"prompt_version":row["prompt_version"],
            "input":json.loads(row["input_json"] or '{}'),"output":json.loads(row["output_json"] or '{}'),
            "chat_url":(conv or {}).get("chat_url","") if mode=="FOLLOW_UP" else "",
            "chat_ref":(conv or {}).get("chat_ref","") if mode=="FOLLOW_UP" else "",
            "lease_token":lease_token,"lease_expires_at":exp,"test_only":bool(row["test_only"]),
        }
        return {"ok":True,"job":payload}

    def _verify_lease(conn: sqlite3.Connection, worker: dict[str, Any], job_id: str, lease_token: str, allow_done: bool=False) -> sqlite3.Row:
        row=conn.execute("SELECT * FROM autoflow_chatgpt_jobs WHERE id=?",(job_id,)).fetchone()
        if not row: raise HTTPException(404,"Job not found")
        if allow_done and row["status"]=="DONE": return row
        if row["status"]!="RUNNING" or row["worker_id"]!=worker["id"]: raise HTTPException(409,"Job is not leased to this worker")
        if not lease_token or not hmac.compare_digest(str(row["lease_hash"]),_hash(lease_token)): raise HTTPException(401,"Invalid lease token")
        return row

    @app.post("/api/master-controller/chatgpt/worker/jobs/{job_id}/lease")
    async def extend_lease(job_id: str, request: Request):
        worker=_worker_auth(request); body=await request.json(); token=str(body.get("lease_token") or "")
        exp=_iso_after(LEASE_MINUTES); now=_now()
        with _db() as conn:
            _verify_lease(conn,worker,job_id,token)
            conn.execute("UPDATE autoflow_chatgpt_jobs SET lease_expires_at=?,updated_at=? WHERE id=?",(exp,now,job_id))
            conn.execute("UPDATE autoflow_chatgpt_workers SET last_seen=?,browser_state='WORKING',updated_at=? WHERE id=?",(now,now,worker["id"]))
        return {"ok":True,"lease_expires_at":exp}

    @app.post("/api/master-controller/chatgpt/worker/jobs/{job_id}/result")
    async def worker_result(job_id: str, request: Request):
        worker=_worker_auth(request); body=await request.json(); token=str(body.get("lease_token") or ""); now=_now()
        response_text=str(body.get("response_text") or "")
        if not response_text.strip(): raise HTTPException(400,"response_text required")
        chat_url=str(body.get("chat_url") or "").strip()[:2000]; chat_ref=str(body.get("chat_ref") or "").strip()[:500]
        structured=body.get("structured_result") if isinstance(body.get("structured_result"),dict) else {}
        with _db() as conn:
            row=conn.execute("SELECT * FROM autoflow_chatgpt_jobs WHERE id=?",(job_id,)).fetchone()
            if not row: raise HTTPException(404,"Job not found")
            if row["status"]=="DONE": return {"ok":True,"duplicate":True}
            row=_verify_lease(conn,worker,job_id,token)
            if not row["test_only"] and row["source_trigger_job_id"] is not None:
                source = conn.execute("SELECT * FROM trigger_jobs WHERE id=?", (row["source_trigger_job_id"],)).fetchone()
                if not source:
                    raise HTTPException(409, "Original Auto Flow trigger job was not found")
                valid, reason = _legacy_result_valid(source["action"], structured)
                if not valid:
                    conn.execute("UPDATE autoflow_chatgpt_jobs SET status='ERROR',response_text=?,response_json=?,chat_url=?,chat_ref=?,error_code='INVALID_OUTPUT',error_text=?,lease_hash='',lease_expires_at='',completed_at=?,updated_at=? WHERE id=?",
                        (response_text,json.dumps(structured,ensure_ascii=False),chat_url,chat_ref,reason,now,now,job_id))
                    _fail_source_trigger(conn, row, "INVALID_OUTPUT", reason)
                    active=conn.execute("SELECT COUNT(*) FROM autoflow_chatgpt_jobs WHERE worker_id=? AND status='RUNNING'",(worker["id"],)).fetchone()[0]
                    conn.execute("UPDATE autoflow_chatgpt_workers SET last_seen=?,active_count=?,browser_state='READY',last_error=?,updated_at=? WHERE id=?",(now,active,f"INVALID_OUTPUT: {reason}"[:1000],now,worker["id"]))
                    return {"ok":False,"status":"ERROR","error_code":"INVALID_OUTPUT","error_text":reason}
            if not row["test_only"] and row["project_id"] is not None and row["permanent_id"] and chat_url:
                conn.execute("""INSERT INTO autoflow_chatgpt_conversations(project_id,permanent_id,chat_url,chat_ref,worker_id,account_label,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(project_id,permanent_id) DO UPDATE SET
                    chat_url=excluded.chat_url,chat_ref=excluded.chat_ref,worker_id=excluded.worker_id,account_label=excluded.account_label,status='ACTIVE',updated_at=excluded.updated_at""",
                    (row["project_id"],row["permanent_id"],chat_url,chat_ref,worker["id"],worker.get("account_label",''),now,now))
            if not row["test_only"] and row["source_trigger_job_id"] is None:
                expected = _json_obj(row["output_json"])
                valid, reason = _expected_valid(expected, structured)
                if not valid:
                    conn.execute("UPDATE autoflow_chatgpt_jobs SET status='ERROR',response_text=?,response_json=?,chat_url=?,chat_ref=?,error_code='INVALID_OUTPUT',error_text=?,lease_hash='',lease_expires_at='',completed_at=?,updated_at=? WHERE id=?",
                        (response_text,json.dumps(structured,ensure_ascii=False),chat_url,chat_ref,reason,now,now,job_id))
                    active=conn.execute("SELECT COUNT(*) FROM autoflow_chatgpt_jobs WHERE worker_id=? AND status='RUNNING'",(worker["id"],)).fetchone()[0]
                    conn.execute("UPDATE autoflow_chatgpt_workers SET last_seen=?,active_count=?,browser_state='READY',last_error=?,updated_at=? WHERE id=?",(now,active,f"INVALID_OUTPUT: {reason}"[:1000],now,worker["id"]))
                    return {"ok":False,"status":"ERROR","error_code":"INVALID_OUTPUT","error_text":reason}
            next_result = _complete_source_trigger(conn, row, structured) if row["source_trigger_job_id"] is not None else (_apply_direct_routes(conn,row,structured) if not row["test_only"] else None)
            conn.execute("""UPDATE autoflow_chatgpt_jobs SET status='DONE',response_text=?,response_json=?,chat_url=?,chat_ref=?,lease_hash='',lease_expires_at='',completed_at=?,updated_at=? WHERE id=?""",
                (response_text,json.dumps(structured,ensure_ascii=False),chat_url,chat_ref,now,now,job_id))
            active=conn.execute("SELECT COUNT(*) FROM autoflow_chatgpt_jobs WHERE worker_id=? AND status='RUNNING'",(worker["id"],)).fetchone()[0]
            conn.execute("UPDATE autoflow_chatgpt_workers SET last_seen=?,active_count=?,browser_state='READY',last_error='',updated_at=? WHERE id=?",(now,active,now,worker["id"]))
        await _notify_workers({"type":"claim_more"},worker["id"])
        return {"ok":True,"next":next_result}

    @app.post("/api/master-controller/chatgpt/worker/jobs/{job_id}/fail")
    async def worker_fail(job_id: str, request: Request):
        worker=_worker_auth(request); body=await request.json(); token=str(body.get("lease_token") or ""); now=_now()
        code=str(body.get("error_code") or "EXTENSION_ERROR").upper()[:80]; text=str(body.get("error_text") or "")[:4000]
        with _db() as conn:
            row=conn.execute("SELECT * FROM autoflow_chatgpt_jobs WHERE id=?",(job_id,)).fetchone()
            if not row: raise HTTPException(404,"Job not found")
            if row["status"]=="ERROR": return {"ok":True,"duplicate":True}
            _verify_lease(conn,worker,job_id,token)
            conn.execute("UPDATE autoflow_chatgpt_jobs SET status='ERROR',error_code=?,error_text=?,lease_hash='',lease_expires_at='',completed_at=?,updated_at=? WHERE id=?",(code,text,now,now,job_id))
            _fail_source_trigger(conn, row, code, text)
            active=conn.execute("SELECT COUNT(*) FROM autoflow_chatgpt_jobs WHERE worker_id=? AND status='RUNNING'",(worker["id"],)).fetchone()[0]
            blocking = code in {"LOGIN_REQUIRED","CAPTCHA","RATE_LIMIT","ACCOUNT_RESTRICTION","UNEXPECTED_PAGE"}
            conn.execute("UPDATE autoflow_chatgpt_workers SET last_seen=?,active_count=?,browser_state=?,last_error=?,updated_at=? WHERE id=?",(now,active,"ATTENTION" if blocking else "READY",f"{code}: {text}"[:1000],now,worker["id"]))
        return {"ok":True,"worker_paused":blocking}

    @app.websocket("/api/master-controller/chatgpt/ws")
    async def chatgpt_ws(websocket: WebSocket):
        worker_id=str(websocket.query_params.get("worker_id") or "").strip()
        token=str(websocket.query_params.get("token") or "").strip()
        try:
            worker=_worker_auth_values(worker_id,token)
        except HTTPException:
            await websocket.close(code=4401); return
        await websocket.accept(); WS_CONNECTIONS[worker_id]=websocket
        try:
            with _db() as conn:
                conn.execute("UPDATE autoflow_chatgpt_workers SET last_seen=?,browser_state='READY',updated_at=? WHERE id=?",(_now(),_now(),worker_id))
            await websocket.send_json({"type":"connected","worker_id":worker_id,"max_parallel":worker["max_parallel"]})
            while True:
                msg=await websocket.receive_json()
                typ=str(msg.get("type") or "")
                if typ in ("heartbeat","status"):
                    with _db() as conn:
                        conn.execute("UPDATE autoflow_chatgpt_workers SET last_seen=?,active_count=?,browser_state=?,last_error=?,updated_at=? WHERE id=?",(_now(),max(0,int(msg.get("active_count") or 0)),str(msg.get("browser_state") or "READY")[:80],str(msg.get("last_error") or "")[:1000],_now(),worker_id))
                    await websocket.send_json({"type":"heartbeat_ack","server_time":_now()})
                elif typ=="claim":
                    await websocket.send_json({"type":"claim_via_rest"})
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            if WS_CONNECTIONS.get(worker_id) is websocket: WS_CONNECTIONS.pop(worker_id,None)

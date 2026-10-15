from __future__ import annotations

import hmac
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request

APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("MASTER_CONTROLLER_DB_PATH", str(APP_DIR / "data" / "master_controller.sqlite3")))

STAGES = ("script", "scene", "voice", "audio", "video", "thumbnail", "approval", "upload")
ASSETS = ("script", "scene", "voice", "audio", "video", "thumbnail")
ACTION_STAGE = {
    "CHATGPT_GENERATE_SCRIPT_SCENE_PLAN": "script",
    "ALGROW_GENERATE_VOICE": "voice",
    "SCENE_BUILDER_START": "video",
    "LUM_SCHEDULE_UPLOAD": "upload",
    "AUTOFLOW_LUM_SCHEDULE_UPLOAD": "upload",
}
EVENT_JOB = {
    "SHEET_READY": ("CHATGPT_GENERATE_SCRIPT_SCENE_PLAN", "chatgpt"),
    "SCENE_PLAN_COMPLETE": ("ALGROW_GENERATE_VOICE", "algrow"),
    "AUDIO_COMPLETE": ("SCENE_BUILDER_START", "scene_builder"),
    "VIDEO_COMPLETE": ("AUTOFLOW_LUM_SCHEDULE_UPLOAD", "autoflow_lum"),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _require_api(request: Request) -> None:
    try:
        if request.session.get("username"):
            return
    except Exception:
        pass
    expected = str(os.getenv("AUTOFLOW_BRIDGE_TOKEN") or "").strip()
    supplied = str(request.headers.get("X-AutoFlow-Bridge-Token") or "").strip()
    if expected and supplied and hmac.compare_digest(expected, supplied):
        return
    raise HTTPException(401, "Login or Auto Flow bridge token required")


def init_control_schema() -> None:
    with _db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS autoflow_project_settings (
              project_id INTEGER PRIMARY KEY,
              daily_upload_limit INTEGER NOT NULL DEFAULT 5,
              upload_gap_minutes INTEGER NOT NULL DEFAULT 120,
              first_upload_time TEXT NOT NULL DEFAULT '09:00',
              pause_scheduling INTEGER NOT NULL DEFAULT 0,
              workflow_paused INTEGER NOT NULL DEFAULT 0,
              thumbnail_approval_required INTEGER NOT NULL DEFAULT 1,
              default_voice_profile_id INTEGER,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS autoflow_videos (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              project_id INTEGER NOT NULL,
              permanent_id TEXT NOT NULL,
              title TEXT NOT NULL DEFAULT '',
              description TEXT NOT NULL DEFAULT '',
              stage TEXT NOT NULL DEFAULT 'script',
              status TEXT NOT NULL DEFAULT 'WAITING',
              paused INTEGER NOT NULL DEFAULT 0,
              schedule_at TEXT NOT NULL DEFAULT '',
              schedule_mode TEXT NOT NULL DEFAULT 'AUTO',
              voice_profile_id INTEGER,
              approval_override TEXT NOT NULL DEFAULT 'DEFAULT',
              approval_status TEXT NOT NULL DEFAULT 'PENDING',
              approval_reason TEXT NOT NULL DEFAULT '',
              youtube_url TEXT NOT NULL DEFAULT '',
              uploaded_at TEXT NOT NULL DEFAULT '',
              failed_at TEXT NOT NULL DEFAULT '',
              last_error TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(project_id, permanent_id)
            );
            CREATE INDEX IF NOT EXISTS idx_autoflow_videos_project ON autoflow_videos(project_id, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_autoflow_videos_status ON autoflow_videos(status, stage);

            CREATE TABLE IF NOT EXISTS autoflow_assets (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              project_id INTEGER NOT NULL,
              permanent_id TEXT NOT NULL,
              asset_type TEXT NOT NULL,
              text_content TEXT NOT NULL DEFAULT '',
              source_url TEXT NOT NULL DEFAULT '',
              file_url TEXT NOT NULL DEFAULT '',
              mode TEXT NOT NULL DEFAULT 'AUTO',
              locked INTEGER NOT NULL DEFAULT 0,
              outdated INTEGER NOT NULL DEFAULT 0,
              version INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(project_id, permanent_id, asset_type)
            );
            CREATE INDEX IF NOT EXISTS idx_autoflow_assets_video ON autoflow_assets(project_id, permanent_id);

            CREATE TABLE IF NOT EXISTS autoflow_asset_versions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              project_id INTEGER NOT NULL,
              permanent_id TEXT NOT NULL,
              asset_type TEXT NOT NULL,
              version INTEGER NOT NULL,
              text_content TEXT NOT NULL DEFAULT '',
              source_url TEXT NOT NULL DEFAULT '',
              file_url TEXT NOT NULL DEFAULT '',
              mode TEXT NOT NULL DEFAULT 'AUTO',
              locked INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_autoflow_asset_versions ON autoflow_asset_versions(project_id, permanent_id, asset_type, version DESC);

            CREATE TABLE IF NOT EXISTS autoflow_voice_profiles (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL UNIQUE,
              voice_id TEXT NOT NULL,
              stability REAL NOT NULL DEFAULT 0.50,
              similarity REAL NOT NULL DEFAULT 0.75,
              style REAL NOT NULL DEFAULT 0.00,
              speaker_boost INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS autoflow_thumbnail_history (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              project_id INTEGER NOT NULL,
              permanent_id TEXT NOT NULL,
              action TEXT NOT NULL,
              reason TEXT NOT NULL DEFAULT '',
              thumbnail_url TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL
            );
            -- AUTOFLOW_THUMBNAIL_PHASE1_QUEUE_V1
            CREATE TABLE IF NOT EXISTS autoflow_thumbnail_jobs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              project_id INTEGER NOT NULL,
              permanent_id TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'PENDING',
              version INTEGER NOT NULL DEFAULT 1,
              prompt TEXT NOT NULL DEFAULT '',
              reference_image TEXT NOT NULL DEFAULT '',
              local_file TEXT NOT NULL DEFAULT '',
              drive_url TEXT NOT NULL DEFAULT '',
              approval_required INTEGER NOT NULL DEFAULT 1,
              flow_profile TEXT NOT NULL DEFAULT '',
              final_prompt TEXT NOT NULL DEFAULT '',
              error_text TEXT NOT NULL DEFAULT '',
              attempt_count INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              completed_at TEXT NOT NULL DEFAULT '',
              UNIQUE(project_id, permanent_id, version)
            );
            CREATE INDEX IF NOT EXISTS idx_autoflow_thumb_jobs_queue
              ON autoflow_thumbnail_jobs(status, updated_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_autoflow_thumb_jobs_video
              ON autoflow_thumbnail_jobs(project_id, permanent_id, version DESC);

            CREATE TABLE IF NOT EXISTS autoflow_thumbnail_audit (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              project_id INTEGER NOT NULL,
              permanent_id TEXT NOT NULL,
              job_id INTEGER,
              event TEXT NOT NULL,
              from_status TEXT NOT NULL DEFAULT '',
              to_status TEXT NOT NULL DEFAULT '',
              detail_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_autoflow_thumb_audit_video
              ON autoflow_thumbnail_audit(project_id, permanent_id, id DESC);

            """
        )
        now = _now()
        conn.execute(
            """INSERT OR IGNORE INTO autoflow_project_settings(project_id, updated_at)
               SELECT id, ? FROM projects""",
            (now,),
        )


def _project_row(conn: sqlite3.Connection, project_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Project not found")
    return row


def _settings(conn: sqlite3.Connection, project_id: int) -> dict[str, Any]:
    _project_row(conn, project_id)
    row = conn.execute("SELECT * FROM autoflow_project_settings WHERE project_id=?", (project_id,)).fetchone()
    if not row:
        conn.execute("INSERT INTO autoflow_project_settings(project_id,updated_at) VALUES(?,?)", (project_id, _now()))
        row = conn.execute("SELECT * FROM autoflow_project_settings WHERE project_id=?", (project_id,)).fetchone()
    out = dict(row)
    for key in ("pause_scheduling", "workflow_paused", "thumbnail_approval_required"):
        out[key] = bool(out[key])
    return out


def _ensure_video(conn: sqlite3.Connection, project_id: int, permanent_id: str, title: str = "") -> sqlite3.Row:
    permanent_id = str(permanent_id or "").strip()
    if not permanent_id:
        raise HTTPException(400, "Permanent ID is required")
    _project_row(conn, project_id)
    now = _now()
    conn.execute(
        """INSERT INTO autoflow_videos(project_id,permanent_id,title,created_at,updated_at)
           VALUES(?,?,?,?,?)
           ON CONFLICT(project_id,permanent_id) DO UPDATE SET
             title=CASE WHEN excluded.title!='' THEN excluded.title ELSE autoflow_videos.title END,
             updated_at=excluded.updated_at""",
        (project_id, permanent_id, str(title or ""), now, now),
    )
    return conn.execute(
        "SELECT * FROM autoflow_videos WHERE project_id=? AND permanent_id=?", (project_id, permanent_id)
    ).fetchone()


def _asset(conn: sqlite3.Connection, project_id: int, permanent_id: str, asset_type: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM autoflow_assets WHERE project_id=? AND permanent_id=? AND asset_type=?",
        (project_id, permanent_id, asset_type),
    ).fetchone()


def _save_version(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    conn.execute(
        """INSERT INTO autoflow_asset_versions(project_id,permanent_id,asset_type,version,text_content,source_url,file_url,mode,locked,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            row["project_id"], row["permanent_id"], row["asset_type"], row["version"], row["text_content"],
            row["source_url"], row["file_url"], row["mode"], row["locked"], _now(),
        ),
    )


def _mark_outdated(conn: sqlite3.Connection, project_id: int, permanent_id: str, asset_types: list[str]) -> None:
    now = _now()
    for asset_type in asset_types:
        row = _asset(conn, project_id, permanent_id, asset_type)
        if row:
            conn.execute("UPDATE autoflow_assets SET outdated=1,updated_at=? WHERE id=?", (now, row["id"]))
        else:
            conn.execute(
                """INSERT INTO autoflow_assets(project_id,permanent_id,asset_type,outdated,created_at,updated_at)
                   VALUES(?,?,?,1,?,?)""",
                (project_id, permanent_id, asset_type, now, now),
            )


def _dependency_after(asset_type: str) -> list[str]:
    if asset_type == "script":
        return ["voice", "audio", "video"]
    if asset_type == "scene":
        return ["video"]
    if asset_type in ("voice", "audio"):
        return ["video"]
    return []


def _upsert_asset(
    conn: sqlite3.Connection,
    project_id: int,
    permanent_id: str,
    asset_type: str,
    *,
    text_content: str | None = None,
    source_url: str | None = None,
    file_url: str | None = None,
    mode: str | None = None,
    locked: bool | None = None,
    outdated: bool | None = None,
    save_previous: bool = False,
) -> sqlite3.Row:
    if asset_type not in ASSETS:
        raise HTTPException(400, f"Unsupported asset type: {asset_type}")
    _ensure_video(conn, project_id, permanent_id)
    old = _asset(conn, project_id, permanent_id, asset_type)
    now = _now()
    if old and save_previous:
        _save_version(conn, old)
    if old:
        values = {
            "text_content": old["text_content"] if text_content is None else str(text_content),
            "source_url": old["source_url"] if source_url is None else str(source_url),
            "file_url": old["file_url"] if file_url is None else str(file_url),
            "mode": old["mode"] if mode is None else str(mode).upper(),
            "locked": old["locked"] if locked is None else int(bool(locked)),
            "outdated": old["outdated"] if outdated is None else int(bool(outdated)),
            "version": int(old["version"]) + (1 if save_previous else 0),
        }
        conn.execute(
            """UPDATE autoflow_assets SET text_content=?,source_url=?,file_url=?,mode=?,locked=?,outdated=?,version=?,updated_at=? WHERE id=?""",
            (values["text_content"], values["source_url"], values["file_url"], values["mode"], values["locked"], values["outdated"], values["version"], now, old["id"]),
        )
    else:
        conn.execute(
            """INSERT INTO autoflow_assets(project_id,permanent_id,asset_type,text_content,source_url,file_url,mode,locked,outdated,version,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,1,?,?)""",
            (
                project_id, permanent_id, asset_type, str(text_content or ""), str(source_url or ""), str(file_url or ""),
                str(mode or "AUTO").upper(), int(bool(locked)) if locked is not None else 0,
                int(bool(outdated)) if outdated is not None else 0, now, now,
            ),
        )
    return _asset(conn, project_id, permanent_id, asset_type)


def _latest_job(conn: sqlite3.Connection, project_id: int, permanent_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM trigger_jobs WHERE project_id=? AND permanent_id=? ORDER BY id DESC LIMIT 1",
        (project_id, permanent_id),
    ).fetchone()
    if not row:
        return None
    out = dict(row)
    out["payload"] = _json(out.pop("payload_json", "{}"), {})
    out["result"] = _json(out.pop("result_json", "{}"), {})
    return out


def _json(value: str, default: Any) -> Any:
    try:
        return json.loads(value or "")
    except Exception:
        return default


def _video_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    out["paused"] = bool(out["paused"])
    assets = [dict(r) for r in conn.execute(
        "SELECT * FROM autoflow_assets WHERE project_id=? AND permanent_id=? ORDER BY id",
        (row["project_id"], row["permanent_id"]),
    ).fetchall()]
    for a in assets:
        a["locked"] = bool(a["locked"]); a["outdated"] = bool(a["outdated"])
    out["assets"] = assets
    out["latest_job"] = _latest_job(conn, row["project_id"], row["permanent_id"])
    thumb = _asset(conn, row["project_id"], row["permanent_id"], "thumbnail")
    out["thumbnail_prompt"] = str(thumb["text_content"] or "") if thumb else ""
    out["thumbnail_reference"] = str(thumb["source_url"] or "") if thumb else ""
    out["thumbnail_url"] = str(thumb["file_url"] or "") if thumb else ""
    tj = _thumbnail_latest_job(conn, row["project_id"], row["permanent_id"])
    out["thumbnail_job"] = _thumbnail_job_dict(conn, tj) if tj else None
    return out


def _purge_old_rows(conn: sqlite3.Connection) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    rows = conn.execute(
        """SELECT id,project_id,permanent_id FROM autoflow_videos
           WHERE (uploaded_at!='' AND uploaded_at<?)
              OR (failed_at!='' AND failed_at<? AND stage='upload')""",
        (cutoff, cutoff),
    ).fetchall()
    for r in rows:
        conn.execute("DELETE FROM autoflow_assets WHERE project_id=? AND permanent_id=?", (r["project_id"], r["permanent_id"]))
        conn.execute("DELETE FROM autoflow_asset_versions WHERE project_id=? AND permanent_id=?", (r["project_id"], r["permanent_id"]))
        conn.execute("DELETE FROM autoflow_thumbnail_history WHERE project_id=? AND permanent_id=?", (r["project_id"], r["permanent_id"]))
        conn.execute("DELETE FROM autoflow_thumbnail_audit WHERE project_id=? AND permanent_id=?", (r["project_id"], r["permanent_id"]))
        conn.execute("DELETE FROM autoflow_thumbnail_jobs WHERE project_id=? AND permanent_id=?", (r["project_id"], r["permanent_id"]))
        conn.execute("DELETE FROM autoflow_videos WHERE id=?", (r["id"],))
    return len(rows)


def _approval_required(settings: dict[str, Any], video: dict[str, Any]) -> bool:
    override = str(video.get("approval_override") or "DEFAULT").upper()
    if override == "REQUIRED":
        return True
    if override == "NOT_REQUIRED":
        return False
    return bool(settings.get("thumbnail_approval_required", True))



# AUTOFLOW_THUMBNAIL_PHASE1_QUEUE_V1_HELPERS
THUMBNAIL_COMPLETED_STATUSES = {"COMPLETED", "APPROVED", "AUTO_APPROVED"}
THUMBNAIL_QUEUE_STATUSES = {"PENDING", "GENERATING", "UPLOADING", "WAITING_APPROVAL", "RETRYING", "BLOCKED", "FAILED", "PAUSED", "PROMPT_CHANGED", "REJECTED"}

def _thumbnail_latest_job(conn: sqlite3.Connection, project_id: int, permanent_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM autoflow_thumbnail_jobs WHERE project_id=? AND permanent_id=? ORDER BY version DESC,id DESC LIMIT 1",
        (project_id, permanent_id),
    ).fetchone()

def _thumbnail_job_dict(conn: sqlite3.Connection, row: sqlite3.Row | None) -> dict[str, Any] | None:
    if not row:
        return None
    out = dict(row)
    out["approval_required"] = bool(out.get("approval_required"))
    v = conn.execute("SELECT title,approval_status FROM autoflow_videos WHERE project_id=? AND permanent_id=?", (row["project_id"], row["permanent_id"])).fetchone()
    a = _asset(conn, row["project_id"], row["permanent_id"], "thumbnail")
    out["title"] = str(v["title"] or "") if v else str(row["permanent_id"])
    out["approval_status"] = str(v["approval_status"] or "") if v else ""
    out["thumbnail_url"] = str(a["file_url"] or "") if a else ""
    return out

def _thumbnail_audit(conn: sqlite3.Connection, project_id: int, permanent_id: str, job_id: int | None, event: str, from_status: str = "", to_status: str = "", detail: dict[str, Any] | None = None) -> None:
    conn.execute(
        "INSERT INTO autoflow_thumbnail_audit(project_id,permanent_id,job_id,event,from_status,to_status,detail_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (project_id, permanent_id, job_id, event, from_status, to_status, json.dumps(detail or {}, ensure_ascii=False), _now()),
    )

def _thumbnail_set_status(conn: sqlite3.Connection, job: sqlite3.Row, status: str, *, error_text: str | None = None, event: str = "STATUS_CHANGED", detail: dict[str, Any] | None = None) -> sqlite3.Row:
    status = str(status or "").upper()
    old = str(job["status"] or "")
    err = str(job["error_text"] or "") if error_text is None else str(error_text or "")
    completed = _now() if status in THUMBNAIL_COMPLETED_STATUSES else str(job["completed_at"] or "")
    conn.execute("UPDATE autoflow_thumbnail_jobs SET status=?,error_text=?,completed_at=?,updated_at=? WHERE id=?", (status, err, completed, _now(), job["id"]))
    if old != status or event != "STATUS_CHANGED":
        _thumbnail_audit(conn, int(job["project_id"]), str(job["permanent_id"]), int(job["id"]), event, old, status, detail)
    return conn.execute("SELECT * FROM autoflow_thumbnail_jobs WHERE id=?", (job["id"],)).fetchone()

def reconcile_thumbnail_job(conn: sqlite3.Connection, project_id: int, permanent_id: str, source: str = "control_state") -> sqlite3.Row | None:
    video = conn.execute("SELECT * FROM autoflow_videos WHERE project_id=? AND permanent_id=?", (project_id, permanent_id)).fetchone()
    if not video:
        return None
    asset = _asset(conn, project_id, permanent_id, "thumbnail")
    prompt = str(asset["text_content"] or "").strip() if asset else ""
    reference = str(asset["source_url"] or "").strip() if asset else ""
    thumb_url = str(asset["file_url"] or "").strip() if asset else ""
    required = _approval_required(_settings(conn, project_id), dict(video))
    uploaded = bool(str(video["uploaded_at"] or "").strip()) or (str(video["stage"] or "").lower() == "upload" and str(video["status"] or "").upper() == "COMPLETE")
    latest = _thumbnail_latest_job(conn, project_id, permanent_id)
    now = _now()
    if latest is None:
        version = max(1, int(asset["version"] or 1) if asset else 1)
        if uploaded:
            status, err = "COMPLETED", ""
        elif thumb_url:
            status, err = ("WAITING_APPROVAL", "") if required and str(video["approval_status"] or "").upper() not in ("APPROVED", "NOT_REQUIRED") else ("COMPLETED", "")
        elif not prompt:
            status, err = "BLOCKED", "Thumbnail prompt missing"
        else:
            status, err = "PENDING", ""
        cur = conn.execute(
            """INSERT INTO autoflow_thumbnail_jobs(project_id,permanent_id,status,version,prompt,reference_image,approval_required,error_text,created_at,updated_at,completed_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (project_id, permanent_id, status, version, prompt, reference, int(required), err, now, now, now if status in THUMBNAIL_COMPLETED_STATUSES else ""),
        )
        latest = conn.execute("SELECT * FROM autoflow_thumbnail_jobs WHERE id=?", (cur.lastrowid,)).fetchone()
        _thumbnail_audit(conn, project_id, permanent_id, int(cur.lastrowid), "QUEUE_CREATED", "", status, {"source": source})
        return latest

    old_status = str(latest["status"] or "").upper()
    previous_prompt = str(latest["prompt"] or "").strip()
    prompt_changed = previous_prompt != prompt
    new_status, err = old_status, str(latest["error_text"] or "")
    if uploaded:
        new_status, err = "COMPLETED", ""
    elif old_status in ("FAILED", "PAUSED", "REJECTED", "PROMPT_CHANGED"):
        new_status = old_status
    elif prompt_changed and thumb_url:
        new_status, err = "PROMPT_CHANGED", "Thumbnail prompt changed after generation"
    elif thumb_url:
        new_status, err = ("WAITING_APPROVAL", "") if required and str(video["approval_status"] or "").upper() not in ("APPROVED", "NOT_REQUIRED") else ("COMPLETED", "")
    elif not prompt:
        new_status, err = "BLOCKED", "Thumbnail prompt missing"
    elif old_status == "WAITING_APPROVAL":
        new_status, err = "PENDING", ""
    elif old_status == "BLOCKED" or prompt_changed:
        new_status, err = "PENDING", ""

    conn.execute(
        "UPDATE autoflow_thumbnail_jobs SET prompt=?,reference_image=?,approval_required=?,status=?,error_text=?,completed_at=?,updated_at=? WHERE id=?",
        (prompt, reference, int(required), new_status, err, now if new_status in THUMBNAIL_COMPLETED_STATUSES else str(latest["completed_at"] or ""), now, latest["id"]),
    )
    current = conn.execute("SELECT * FROM autoflow_thumbnail_jobs WHERE id=?", (latest["id"],)).fetchone()
    if old_status != new_status:
        _thumbnail_audit(conn, project_id, permanent_id, int(latest["id"]), "RECONCILED", old_status, new_status, {"source": source})
    if prompt_changed:
        _thumbnail_audit(conn, project_id, permanent_id, int(latest["id"]), "PROMPT_SYNCED", old_status, new_status, {"source": source, "had_previous_prompt": bool(previous_prompt)})
    return current

def _thumbnail_jobs(conn: sqlite3.Connection, project_id: int | None = None, limit: int = 500) -> list[dict[str, Any]]:
    sql = "SELECT * FROM autoflow_thumbnail_jobs"
    args: tuple[Any, ...] = ()
    if project_id is not None:
        sql += " WHERE project_id=?"
        args = (project_id,)
    sql += " ORDER BY updated_at DESC,id DESC LIMIT ?"
    args = (*args, max(1, min(int(limit), 1000)))
    return [_thumbnail_job_dict(conn, r) for r in conn.execute(sql, args).fetchall()]

def _thumbnail_counts(conn: sqlite3.Connection, project_id: int | None = None) -> dict[str, int]:
    sql = "SELECT status,COUNT(*) n FROM autoflow_thumbnail_jobs"
    args: tuple[Any, ...] = ()
    if project_id is not None:
        sql += " WHERE project_id=?"
        args = (project_id,)
    sql += " GROUP BY status"
    return {str(r["status"]): int(r["n"]) for r in conn.execute(sql, args).fetchall()}

def _attention(conn: sqlite3.Connection, videos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    settings_cache: dict[int, dict[str, Any]] = {}
    for video in videos:
        pid = int(video["project_id"])
        settings_cache.setdefault(pid, _settings(conn, pid))
        latest = video.get("latest_job") or {}
        tjob = video.get("thumbnail_job") or {}
        tstatus = str(tjob.get("status") or "").upper()
        if tstatus == "FAILED":
            items.append({"type":"ERROR","project_id":pid,"permanent_id":video["permanent_id"],"stage":"thumbnail","message":tjob.get("error_text") or "Thumbnail generation failed"})
        elif tstatus in ("BLOCKED", "PROMPT_CHANGED"):
            items.append({"type":"BLOCKED","project_id":pid,"permanent_id":video["permanent_id"],"stage":"thumbnail","message":tjob.get("error_text") or ("Thumbnail prompt changed" if tstatus == "PROMPT_CHANGED" else "Thumbnail is blocked")})
        if video.get("last_error") or latest.get("status") == "FAILED" or str(video.get("status")) == "ERROR":
            items.append({"type":"ERROR","project_id":pid,"permanent_id":video["permanent_id"],"stage":video.get("stage"),"message":video.get("last_error") or latest.get("error_text") or "Stage failed"})
        if str(video.get("status")) == "BLOCKED":
            items.append({"type":"BLOCKED","project_id":pid,"permanent_id":video["permanent_id"],"stage":video.get("stage"),"message":"Video is blocked"})
        if video.get("thumbnail_url") and _approval_required(settings_cache[pid], video) and str(video.get("approval_status")) not in ("APPROVED","NOT_REQUIRED"):
            items.append({"type":"APPROVAL","project_id":pid,"permanent_id":video["permanent_id"],"stage":"thumbnail","message":"Thumbnail approval required"})
        outdated = [a["asset_type"] for a in video.get("assets",[]) if a.get("outdated")]
        if outdated:
            items.append({"type":"OUTDATED","project_id":pid,"permanent_id":video["permanent_id"],"stage":outdated[0],"message":"Outdated: " + ", ".join(outdated),"assets":outdated})
        if str(video.get("schedule_mode")).upper() == "MANUAL" and video.get("schedule_at") and not video.get("uploaded_at"):
            try:
                dt = datetime.fromisoformat(str(video["schedule_at"]).replace("Z", "+00:00"))
                if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
                if dt < now:
                    items.append({"type":"MISSED_MANUAL_SLOT","project_id":pid,"permanent_id":video["permanent_id"],"stage":"upload","message":"Manual upload slot was missed"})
            except Exception:
                pass
    rank = {"ERROR":0,"BLOCKED":1,"APPROVAL":2,"OUTDATED":3,"MISSED_MANUAL_SLOT":4}
    items.sort(key=lambda x: (rank.get(x["type"],99), x["project_id"], x["permanent_id"]))
    return items


def _enqueue_compatible(conn: sqlite3.Connection, project_id: int, permanent_id: str, event: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    event = str(event or "").upper()
    if event not in EVENT_JOB:
        raise HTTPException(400, f"Unsupported rebuild event: {event}")
    project = _project_row(conn, project_id)
    video = _ensure_video(conn, project_id, permanent_id)
    if int(video["paused"]):
        raise HTTPException(409, "Video is paused")
    settings = _settings(conn, project_id)
    if settings.get("workflow_paused") or not int(project["enabled"]):
        raise HTTPException(409, "Project workflow is paused")
    action, worker = EVENT_JOB[event]
    now = _now()
    body = dict(payload or {})
    conn.execute(
        "INSERT INTO controller_events(project_id,permanent_id,event,payload_json,created_at) VALUES(?,?,?,?,?)",
        (project_id, permanent_id, event, json.dumps(body, ensure_ascii=False), now),
    )
    # Supersede unfinished same-action jobs so manual control wins.
    for r in conn.execute(
        "SELECT id,dedupe_key FROM trigger_jobs WHERE project_id=? AND permanent_id=? AND action=? AND status IN ('QUEUED','FAILED')",
        (project_id, permanent_id, action),
    ).fetchall():
        conn.execute("UPDATE trigger_jobs SET status='SUPERSEDED',dedupe_key=?,updated_at=? WHERE id=?", (f"{r['dedupe_key']}:superseded:{r['id']}:{int(datetime.now().timestamp())}", now, r["id"]))
    dedupe = f"{project_id}:{permanent_id}:{event}:{action}:manual:{int(datetime.now().timestamp()*1000)}"
    cur = conn.execute(
        """INSERT INTO trigger_jobs(project_id,permanent_id,source_event,action,worker,status,payload_json,dedupe_key,created_at,updated_at)
           VALUES(?,?,?,?,?,'QUEUED',?,?,?,?)""",
        (project_id, permanent_id, event, action, worker, json.dumps(body, ensure_ascii=False), dedupe, now, now),
    )
    conn.execute("UPDATE autoflow_videos SET stage=?,status='WAITING',last_error='',updated_at=? WHERE id=?", (ACTION_STAGE.get(action,"script"), now, video["id"]))
    return {"job_id":cur.lastrowid,"event":event,"action":action,"worker":worker}


def _stage_to_event(stage: str) -> str:
    s = str(stage or "").strip().lower()
    if s in ("script", "scene"):
        return "SHEET_READY"
    if s in ("voice", "audio"):
        return "SCENE_PLAN_COMPLETE"
    if s in ("video", "thumbnail", "approval"):
        return "AUDIO_COMPLETE"
    if s == "upload":
        return "VIDEO_COMPLETE"
    raise HTTPException(400, f"Unsupported stage: {stage}")


def _schedule_suggestion(conn: sqlite3.Connection, project_id: int, desired: str = "") -> str:
    settings = _settings(conn, project_id)
    gap = max(1, int(settings["upload_gap_minutes"]))
    daily_limit = max(1, int(settings["daily_upload_limit"]))
    now = datetime.now(timezone.utc)
    if desired:
        try:
            candidate = datetime.fromisoformat(desired.replace("Z", "+00:00"))
            if candidate.tzinfo is None:
                candidate = candidate.replace(tzinfo=timezone.utc)
        except Exception:
            raise HTTPException(400, "Invalid schedule time")
    else:
        hh, mm = [int(x) for x in str(settings["first_upload_time"] or "09:00").split(":")[:2]]
        candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate < now:
            candidate += timedelta(days=1)
    for _ in range(2000):
        day_start = candidate.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        rows = conn.execute(
            "SELECT schedule_at FROM autoflow_videos WHERE project_id=? AND schedule_at!='' AND uploaded_at=''",
            (project_id,),
        ).fetchall()
        scheduled: list[datetime] = []
        for r in rows:
            try:
                d = datetime.fromisoformat(str(r["schedule_at"]).replace("Z", "+00:00"))
                if d.tzinfo is None: d=d.replace(tzinfo=timezone.utc)
                if day_start <= d < day_end: scheduled.append(d)
            except Exception: pass
        if len(scheduled) >= daily_limit:
            candidate = day_end
            hh, mm = [int(x) for x in str(settings["first_upload_time"] or "09:00").split(":")[:2]]
            candidate = candidate.replace(hour=hh, minute=mm)
            continue
        if all(abs((candidate-d).total_seconds()) >= gap*60 for d in scheduled):
            return candidate.isoformat()
        candidate += timedelta(minutes=gap)
    raise HTTPException(409, "No schedule slot found")


def ensure_video_event(conn: sqlite3.Connection, project_id: int, permanent_id: str, event: str, payload: dict[str, Any] | None = None) -> None:
    """Best-effort hook called by the legacy event engine."""
    try:
        row = _ensure_video(conn, int(project_id), permanent_id, str((payload or {}).get("title") or ""))
        mapping = {
            "SHEET_READY": ("script", "WAITING"),
            "SCENE_PLAN_COMPLETE": ("voice", "WAITING"),
            "AUDIO_COMPLETE": ("video", "WAITING"),
            "VIDEO_COMPLETE": ("upload", "WAITING"),
            "UPLOADED": ("upload", "COMPLETE"),
        }
        stage, status = mapping.get(str(event).upper(), (row["stage"], row["status"]))
        uploaded = _now() if str(event).upper() == "UPLOADED" else row["uploaded_at"]
        conn.execute("UPDATE autoflow_videos SET stage=?,status=?,uploaded_at=?,updated_at=? WHERE id=?", (stage,status,uploaded,_now(),row["id"]))
    except Exception:
        return


def on_worker_result(conn: sqlite3.Connection, job_row: sqlite3.Row, success: bool, result: dict[str, Any], error: str) -> None:
    """Best-effort hook; never allowed to break the legacy worker result endpoint."""
    try:
        project_id = int(job_row["project_id"]); permanent_id = str(job_row["permanent_id"])
        video = _ensure_video(conn, project_id, permanent_id)
        action = str(job_row["action"])
        stage = ACTION_STAGE.get(action, video["stage"])
        now = _now()
        if not success:
            failed_at = now if stage == "upload" else video["failed_at"]
            conn.execute("UPDATE autoflow_videos SET stage=?,status='ERROR',last_error=?,failed_at=?,updated_at=? WHERE id=?", (stage,str(error or "")[:4000],failed_at,now,video["id"]))
            # AUTOFLOW_GOOGLE_SYNC_V1_WORKER_FAILURE
            try:
                from autoflow_google_sync import queue_video_push
                queue_video_push(conn,project_id,permanent_id)
            except Exception:
                pass
            return
        if action == "CHATGPT_GENERATE_SCRIPT_SCENE_PLAN":
            script = str(result.get("script_text") or result.get("script") or "")
            scene = str(result.get("scene_plan_text") or result.get("scene_plan") or "")
            if script: _upsert_asset(conn, project_id, permanent_id, "script", text_content=script, source_url=str(result.get("script_doc_url") or ""), outdated=False)
            if scene: _upsert_asset(conn, project_id, permanent_id, "scene", text_content=scene, source_url=str(result.get("scene_plan_doc_url") or ""), outdated=False)
            conn.execute("UPDATE autoflow_videos SET stage='voice',status='WAITING',last_error='',updated_at=? WHERE id=?", (now,video["id"]))
        elif action == "ALGROW_GENERATE_VOICE":
            audio = str(result.get("audio_url") or result.get("audio_path") or result.get("drive_audio_url") or "")
            _upsert_asset(conn, project_id, permanent_id, "voice", file_url=audio, outdated=False)
            _upsert_asset(conn, project_id, permanent_id, "audio", file_url=audio, outdated=False)
            conn.execute("UPDATE autoflow_videos SET stage='video',status='WAITING',last_error='',updated_at=? WHERE id=?", (now,video["id"]))
        elif action == "SCENE_BUILDER_START":
            url = str(result.get("video_url") or result.get("video_path") or result.get("drive_video_url") or "")
            _upsert_asset(conn, project_id, permanent_id, "video", file_url=url, outdated=False)
            conn.execute("UPDATE autoflow_videos SET stage='upload',status='WAITING',last_error='',updated_at=? WHERE id=?", (now,video["id"]))
        elif action in ("LUM_SCHEDULE_UPLOAD", "AUTOFLOW_LUM_SCHEDULE_UPLOAD"):
            yt = str(result.get("youtube_url") or result.get("video_url") or result.get("url") or "")
            conn.execute("UPDATE autoflow_videos SET stage='upload',status='COMPLETE',youtube_url=?,uploaded_at=?,last_error='',updated_at=? WHERE id=?", (yt,now,now,video["id"]))
        # AUTOFLOW_GOOGLE_SYNC_V1_WORKER_SUCCESS
        try:
            from autoflow_google_sync import queue_asset_push, queue_video_push
            if action == "CHATGPT_GENERATE_SCRIPT_SCENE_PLAN":
                if script: queue_asset_push(conn,project_id,permanent_id,"script",script)
                if scene: queue_asset_push(conn,project_id,permanent_id,"scene",scene)
            queue_video_push(conn,project_id,permanent_id)
        except Exception:
            pass
    except Exception:
        return


def register_autoflow_control_backend(app) -> None:
    init_control_schema()

    @app.get("/api/master-controller/control/state")
    def control_state(request: Request, project_id: int | None = None):
        _require_api(request)
        with _db() as conn:
            _purge_old_rows(conn)
            sql = "SELECT * FROM autoflow_videos"
            args: tuple[Any,...] = ()
            if project_id is not None:
                _project_row(conn, project_id)
                sql += " WHERE project_id=?"; args=(project_id,)
            sql += " ORDER BY updated_at DESC,id DESC"
            raw_videos = conn.execute(sql,args).fetchall()
            for r in raw_videos:
                reconcile_thumbnail_job(conn, int(r["project_id"]), str(r["permanent_id"]), "control_state")
            raw_videos = conn.execute(sql,args).fetchall()
            videos = [_video_dict(conn,r) for r in raw_videos]
            thumbnail_jobs = _thumbnail_jobs(conn, project_id, 500)
            thumbnail_counts = _thumbnail_counts(conn, project_id)
            profiles = [dict(r) for r in conn.execute("SELECT * FROM autoflow_voice_profiles ORDER BY name COLLATE NOCASE").fetchall()]
            projects = []
            for p in conn.execute("SELECT id,name,enabled,active FROM projects ORDER BY active DESC,id DESC").fetchall():
                item=dict(p); item["enabled"]=bool(item["enabled"]); item["active"]=bool(item["active"]); item["settings"]=_settings(conn,int(p["id"])); projects.append(item)
            attention = _attention(conn,videos)
            # AUTOFLOW_CHATGPT_EXTENSION_V1_ATTENTION
            try:
                from autoflow_chatgpt_bridge import chatgpt_attention
                attention.extend(chatgpt_attention(conn, project_id))
            except Exception:
                pass
        return {"ok":True,"projects":projects,"videos":videos,"attention":attention,"voice_profiles":profiles,"thumbnail_jobs":thumbnail_jobs,"thumbnail_counts":thumbnail_counts,"retention_days":7}

    @app.get("/api/master-controller/projects/{project_id}/control/settings")
    def get_settings(project_id: int, request: Request):
        _require_api(request)
        with _db() as conn: return {"ok":True,"settings":_settings(conn,project_id)}

    @app.post("/api/master-controller/projects/{project_id}/control/settings")
    async def update_settings(project_id: int, request: Request):
        _require_api(request); body=await request.json(); allowed={"daily_upload_limit","upload_gap_minutes","first_upload_time","pause_scheduling","workflow_paused","thumbnail_approval_required","default_voice_profile_id"}
        sets=[]; vals=[]
        for key in allowed:
            if key in body:
                val=body[key]
                if key in ("pause_scheduling","workflow_paused","thumbnail_approval_required"): val=int(bool(val))
                if key in ("daily_upload_limit","upload_gap_minutes"): val=max(1,int(val))
                sets.append(f"{key}=?"); vals.append(val)
        if not sets: raise HTTPException(400,"No supported settings supplied")
        with _db() as conn:
            _settings(conn,project_id); sets.append("updated_at=?"); vals.append(_now()); vals.append(project_id)
            conn.execute(f"UPDATE autoflow_project_settings SET {','.join(sets)} WHERE project_id=?", vals)
            return {"ok":True,"settings":_settings(conn,project_id)}

    @app.get("/api/master-controller/projects/{project_id}/videos")
    def list_videos(project_id: int, request: Request):
        _require_api(request)
        with _db() as conn:
            _project_row(conn,project_id); _purge_old_rows(conn)
            rows=conn.execute("SELECT * FROM autoflow_videos WHERE project_id=? ORDER BY updated_at DESC,id DESC",(project_id,)).fetchall()
            for r in rows: reconcile_thumbnail_job(conn,project_id,str(r["permanent_id"]),"list_videos")
            videos=[_video_dict(conn,r) for r in conn.execute("SELECT * FROM autoflow_videos WHERE project_id=? ORDER BY updated_at DESC,id DESC",(project_id,)).fetchall()]
            return {"ok":True,"videos":videos,"attention":_attention(conn,videos),"thumbnail_jobs":_thumbnail_jobs(conn,project_id,500),"thumbnail_counts":_thumbnail_counts(conn,project_id)}

    @app.post("/api/master-controller/projects/{project_id}/videos/upsert")
    async def upsert_video(project_id: int, request: Request):
        _require_api(request); body=await request.json(); permanent_id=str(body.get("permanent_id") or "").strip()
        with _db() as conn:
            row=_ensure_video(conn,project_id,permanent_id,str(body.get("title") or "")); updates=[]; vals=[]
            fields=("title","description","stage","status","schedule_at","schedule_mode","approval_override","approval_status","youtube_url")
            for key in fields:
                if key in body: updates.append(f"{key}=?"); vals.append(str(body[key] or ""))
            if "voice_profile_id" in body: updates.append("voice_profile_id=?"); vals.append(body.get("voice_profile_id"))
            if updates:
                updates.append("updated_at=?"); vals.append(_now()); vals.append(row["id"]); conn.execute(f"UPDATE autoflow_videos SET {','.join(updates)} WHERE id=?",vals)
            # AUTOFLOW_GOOGLE_SYNC_V1_VIDEO_UPSERT
            try:
                from autoflow_google_sync import queue_video_push
                queue_video_push(conn,project_id,permanent_id)
            except Exception:
                pass
            return {"ok":True,"video":_video_dict(conn,conn.execute("SELECT * FROM autoflow_videos WHERE id=?",(row["id"],)).fetchone())}

    @app.get("/api/master-controller/projects/{project_id}/videos/{permanent_id}")
    def get_video(project_id: int, permanent_id: str, request: Request):
        _require_api(request)
        with _db() as conn:
            row=_ensure_video(conn,project_id,permanent_id); return {"ok":True,"video":_video_dict(conn,row)}

    @app.post("/api/master-controller/projects/{project_id}/videos/{permanent_id}/control")
    async def video_control(project_id: int, permanent_id: str, request: Request):
        _require_api(request); body=await request.json(); action=str(body.get("action") or "").strip().lower()
        with _db() as conn:
            video=_ensure_video(conn,project_id,permanent_id); now=_now(); result:dict[str,Any]={}
            if action=="pause": conn.execute("UPDATE autoflow_videos SET paused=1,status='PAUSED',updated_at=? WHERE id=?",(now,video["id"]))
            elif action=="resume": conn.execute("UPDATE autoflow_videos SET paused=0,status=CASE WHEN status='PAUSED' THEN 'WAITING' ELSE status END,updated_at=? WHERE id=?",(now,video["id"]))
            elif action=="retry":
                job=conn.execute("SELECT * FROM trigger_jobs WHERE project_id=? AND permanent_id=? AND status='FAILED' ORDER BY id DESC LIMIT 1",(project_id,permanent_id)).fetchone()
                if not job: raise HTTPException(409,"No failed job to retry")
                conn.execute("UPDATE trigger_jobs SET status='QUEUED',error_text='',locked_at='',updated_at=? WHERE id=?",(now,job["id"])); conn.execute("UPDATE autoflow_videos SET paused=0,status='WAITING',last_error='',failed_at='',updated_at=? WHERE id=?",(now,video["id"])); result={"job_id":job["id"]}
            elif action=="start_from_here": result=_enqueue_compatible(conn,project_id,permanent_id,_stage_to_event(str(body.get("stage") or video["stage"])),{"source":"control_center"})
            elif action=="change_voice":
                profile_id=body.get("voice_profile_id");
                if profile_id is not None and not conn.execute("SELECT 1 FROM autoflow_voice_profiles WHERE id=?",(profile_id,)).fetchone(): raise HTTPException(404,"Voice profile not found")
                conn.execute("UPDATE autoflow_videos SET voice_profile_id=?,updated_at=? WHERE id=?",(profile_id,now,video["id"])); _mark_outdated(conn,project_id,permanent_id,["voice","audio","video"])
            elif action in ("lock","unlock"):
                asset_type=str(body.get("asset_type") or "").lower(); row=_asset(conn,project_id,permanent_id,asset_type)
                if not row: row=_upsert_asset(conn,project_id,permanent_id,asset_type)
                conn.execute("UPDATE autoflow_assets SET locked=?,updated_at=? WHERE id=?",(1 if action=="lock" else 0,now,row["id"]))
            elif action=="replace":
                asset_type=str(body.get("asset_type") or "").lower(); row=_upsert_asset(conn,project_id,permanent_id,asset_type,text_content=body.get("text_content"),source_url=body.get("source_url"),file_url=body.get("file_url"),mode="MANUAL",locked=True,outdated=False,save_previous=True); _mark_outdated(conn,project_id,permanent_id,_dependency_after(asset_type)); result={"asset":dict(row)}
            elif action=="schedule":
                mode=str(body.get("mode") or "MANUAL").upper(); desired=str(body.get("schedule_at") or "")
                if mode=="MANUAL":
                    suggested=_schedule_suggestion(conn,project_id,desired)
                    if suggested != datetime.fromisoformat(desired.replace("Z","+00:00")).isoformat(): raise HTTPException(409,detail={"message":"Schedule conflict","suggested_time":suggested})
                    chosen=desired
                else: chosen=_schedule_suggestion(conn,project_id,desired)
                conn.execute("UPDATE autoflow_videos SET schedule_at=?,schedule_mode=?,updated_at=? WHERE id=?",(chosen,mode,now,video["id"])); result={"schedule_at":chosen,"schedule_mode":mode}
            elif action=="unschedule": conn.execute("UPDATE autoflow_videos SET schedule_at='',schedule_mode='AUTO',updated_at=? WHERE id=?",(now,video["id"]))
            elif action=="rebuild":
                outdated=[r["asset_type"] for r in conn.execute("SELECT asset_type FROM autoflow_assets WHERE project_id=? AND permanent_id=? AND outdated=1",(project_id,permanent_id)).fetchall()]
                if any(x in outdated for x in ("voice","audio")): event="SCENE_PLAN_COMPLETE"
                elif "video" in outdated: event="AUDIO_COMPLETE"
                else: raise HTTPException(409,"No outdated downstream stage to rebuild")
                result=_enqueue_compatible(conn,project_id,permanent_id,event,{"source":"rebuild_from_here","outdated":outdated})
            elif action=="set_approval_override":
                value=str(body.get("value") or "DEFAULT").upper();
                if value not in ("DEFAULT","REQUIRED","NOT_REQUIRED"): raise HTTPException(400,"Invalid approval override")
                conn.execute("UPDATE autoflow_videos SET approval_override=?,updated_at=? WHERE id=?",(value,now,video["id"]))
            else: raise HTTPException(400,f"Unsupported control action: {action}")
            current=conn.execute("SELECT * FROM autoflow_videos WHERE id=?",(video["id"],)).fetchone()
            # AUTOFLOW_GOOGLE_SYNC_V1_VIDEO_CONTROL
            try:
                from autoflow_google_sync import queue_video_push
                queue_video_push(conn,project_id,permanent_id)
            except Exception:
                pass
            return {"ok":True,"action":action,"result":result,"video":_video_dict(conn,current)}

    @app.get("/api/master-controller/projects/{project_id}/videos/{permanent_id}/assets/{asset_type}")
    def get_asset(project_id: int, permanent_id: str, asset_type: str, request: Request):
        _require_api(request)
        with _db() as conn:
            _ensure_video(conn,project_id,permanent_id); row=_asset(conn,project_id,permanent_id,asset_type.lower()); return {"ok":True,"asset":dict(row) if row else None}

    @app.post("/api/master-controller/projects/{project_id}/videos/{permanent_id}/assets/{asset_type}")
    async def save_asset(project_id: int, permanent_id: str, asset_type: str, request: Request):
        _require_api(request); body=await request.json(); asset_type=asset_type.lower()
        with _db() as conn:
            row=_upsert_asset(conn,project_id,permanent_id,asset_type,text_content=body.get("text_content"),source_url=body.get("source_url"),file_url=body.get("file_url"),mode="MANUAL",locked=True,outdated=False,save_previous=True); _mark_outdated(conn,project_id,permanent_id,_dependency_after(asset_type)); conn.execute("UPDATE autoflow_videos SET updated_at=? WHERE project_id=? AND permanent_id=?",(_now(),project_id,permanent_id)); text=str(row["text_content"] or ""); words=len(text.split()); duration=round(words/150*60) if words else 0
            # AUTOFLOW_GOOGLE_SYNC_V1_ASSET_SAVE
            try:
                from autoflow_google_sync import queue_asset_push, queue_video_push
                queue_asset_push(conn,project_id,permanent_id,asset_type,text)
                queue_video_push(conn,project_id,permanent_id)
            except Exception:
                pass
            return {"ok":True,"asset":dict(row),"word_count":words,"estimated_duration_sec":duration,"outdated":_dependency_after(asset_type)}

    @app.get("/api/master-controller/projects/{project_id}/videos/{permanent_id}/assets/{asset_type}/versions")
    def asset_versions(project_id: int, permanent_id: str, asset_type: str, request: Request):
        _require_api(request)
        with _db() as conn:
            rows=[dict(r) for r in conn.execute("SELECT * FROM autoflow_asset_versions WHERE project_id=? AND permanent_id=? AND asset_type=? ORDER BY version DESC,id DESC LIMIT 50",(project_id,permanent_id,asset_type.lower())).fetchall()]; return {"ok":True,"versions":rows}

    @app.post("/api/master-controller/projects/{project_id}/videos/{permanent_id}/assets/{asset_type}/restore/{version_id}")
    def restore_asset(project_id: int, permanent_id: str, asset_type: str, version_id: int, request: Request):
        _require_api(request); asset_type=asset_type.lower()
        with _db() as conn:
            old=conn.execute("SELECT * FROM autoflow_asset_versions WHERE id=? AND project_id=? AND permanent_id=? AND asset_type=?",(version_id,project_id,permanent_id,asset_type)).fetchone()
            if not old: raise HTTPException(404,"Version not found")
            row=_upsert_asset(conn,project_id,permanent_id,asset_type,text_content=old["text_content"],source_url=old["source_url"],file_url=old["file_url"],mode="MANUAL",locked=True,outdated=False,save_previous=True); _mark_outdated(conn,project_id,permanent_id,_dependency_after(asset_type))
            # AUTOFLOW_GOOGLE_SYNC_V1_ASSET_RESTORE
            try:
                from autoflow_google_sync import queue_asset_push, queue_video_push
                queue_asset_push(conn,project_id,permanent_id,asset_type,str(row["text_content"] or ""))
                queue_video_push(conn,project_id,permanent_id)
            except Exception:
                pass
            return {"ok":True,"asset":dict(row),"outdated":_dependency_after(asset_type)}

    @app.get("/api/master-controller/voice-profiles")
    def voice_profiles(request: Request):
        _require_api(request)
        with _db() as conn: return {"ok":True,"profiles":[dict(r) for r in conn.execute("SELECT * FROM autoflow_voice_profiles ORDER BY name COLLATE NOCASE").fetchall()]}

    @app.post("/api/master-controller/voice-profiles")
    async def create_voice_profile(request: Request):
        _require_api(request); b=await request.json(); name=str(b.get("name") or "").strip(); voice_id=str(b.get("voice_id") or "").strip()
        if not name or not voice_id: raise HTTPException(400,"Profile name and Voice ID are required")
        with _db() as conn:
            try:
                cur=conn.execute("INSERT INTO autoflow_voice_profiles(name,voice_id,stability,similarity,style,speaker_boost,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",(name,voice_id,float(b.get("stability",.5)),float(b.get("similarity",.75)),float(b.get("style",0)),int(bool(b.get("speaker_boost",True))),_now(),_now()))
            except sqlite3.IntegrityError: raise HTTPException(409,"Profile name already exists")
            return {"ok":True,"profile":dict(conn.execute("SELECT * FROM autoflow_voice_profiles WHERE id=?",(cur.lastrowid,)).fetchone())}

    @app.post("/api/master-controller/voice-profiles/{profile_id}")
    async def update_voice_profile(profile_id: int, request: Request):
        _require_api(request); b=await request.json()
        with _db() as conn:
            row=conn.execute("SELECT * FROM autoflow_voice_profiles WHERE id=?",(profile_id,)).fetchone()
            if not row: raise HTTPException(404,"Voice profile not found")
            vals={k:row[k] for k in ("name","voice_id","stability","similarity","style","speaker_boost")}
            for k in vals:
                if k in b: vals[k]=b[k]
            try: conn.execute("UPDATE autoflow_voice_profiles SET name=?,voice_id=?,stability=?,similarity=?,style=?,speaker_boost=?,updated_at=? WHERE id=?",(str(vals["name"]).strip(),str(vals["voice_id"]).strip(),float(vals["stability"]),float(vals["similarity"]),float(vals["style"]),int(bool(vals["speaker_boost"])),_now(),profile_id))
            except sqlite3.IntegrityError: raise HTTPException(409,"Profile name already exists")
            return {"ok":True,"profile":dict(conn.execute("SELECT * FROM autoflow_voice_profiles WHERE id=?",(profile_id,)).fetchone())}

    @app.delete("/api/master-controller/voice-profiles/{profile_id}")
    def delete_voice_profile(profile_id: int, request: Request):
        _require_api(request)
        with _db() as conn:
            if conn.execute("SELECT 1 FROM autoflow_videos WHERE voice_profile_id=? LIMIT 1",(profile_id,)).fetchone(): raise HTTPException(409,"Profile is currently assigned to a video")
            if conn.execute("SELECT 1 FROM autoflow_project_settings WHERE default_voice_profile_id=? LIMIT 1",(profile_id,)).fetchone(): raise HTTPException(409,"Profile is a channel default")
            cur=conn.execute("DELETE FROM autoflow_voice_profiles WHERE id=?",(profile_id,)); return {"ok":True,"deleted":bool(cur.rowcount)}

    @app.post("/api/master-controller/projects/{project_id}/videos/{permanent_id}/thumbnail/approve")
    async def approve_thumbnail(project_id: int, permanent_id: str, request: Request):
        _require_api(request); b=await request.json()
        with _db() as conn:
            v=_ensure_video(conn,project_id,permanent_id); url=str(b.get("thumbnail_url") or (_asset(conn,project_id,permanent_id,"thumbnail")["file_url"] if _asset(conn,project_id,permanent_id,"thumbnail") else "")); conn.execute("UPDATE autoflow_videos SET approval_status='APPROVED',approval_reason='',updated_at=? WHERE id=?",(_now(),v["id"])); conn.execute("INSERT INTO autoflow_thumbnail_history(project_id,permanent_id,action,thumbnail_url,created_at) VALUES(?,?,'APPROVED',?,?)",(project_id,permanent_id,url,_now()))
            job=_thumbnail_latest_job(conn,project_id,permanent_id)
            if job: _thumbnail_set_status(conn,job,"COMPLETED",error_text="",event="APPROVED")
            # AUTOFLOW_GOOGLE_SYNC_V1_APPROVAL_PUSH
            try:
                from autoflow_google_sync import queue_video_push
                queue_video_push(conn,project_id,permanent_id)
            except Exception:
                pass
            return {"ok":True,"status":"APPROVED"}

    @app.post("/api/master-controller/projects/{project_id}/videos/{permanent_id}/thumbnail/reject")
    async def reject_thumbnail(project_id: int, permanent_id: str, request: Request):
        _require_api(request); b=await request.json(); reason=str(b.get("reason") or "").strip()
        if not reason: raise HTTPException(400,"Rejection reason is required")
        with _db() as conn:
            v=_ensure_video(conn,project_id,permanent_id); a=_asset(conn,project_id,permanent_id,"thumbnail"); url=str(a["file_url"] if a else ""); conn.execute("UPDATE autoflow_videos SET approval_status='REJECTED',approval_reason=?,updated_at=? WHERE id=?",(reason,_now(),v["id"])); conn.execute("INSERT INTO autoflow_thumbnail_history(project_id,permanent_id,action,reason,thumbnail_url,created_at) VALUES(?,?,'REJECTED',?,?,?)",(project_id,permanent_id,reason,url,_now()))
            job=_thumbnail_latest_job(conn,project_id,permanent_id)
            if job: _thumbnail_set_status(conn,job,"REJECTED",error_text=reason,event="REJECTED",detail={"reason":reason})
            # AUTOFLOW_GOOGLE_SYNC_V1_REJECTION_PUSH
            try:
                from autoflow_google_sync import queue_video_push
                queue_video_push(conn,project_id,permanent_id)
            except Exception:
                pass
            return {"ok":True,"status":"REJECTED","reason":reason}

    @app.get("/api/master-controller/projects/{project_id}/videos/{permanent_id}/thumbnail/history")
    def thumbnail_history(project_id: int, permanent_id: str, request: Request):
        _require_api(request)
        with _db() as conn: return {"ok":True,"history":[dict(r) for r in conn.execute("SELECT * FROM autoflow_thumbnail_history WHERE project_id=? AND permanent_id=? ORDER BY id DESC LIMIT 100",(project_id,permanent_id)).fetchall()]}

    # AUTOFLOW_THUMBNAIL_PHASE1_QUEUE_V1_API
    @app.get("/api/master-controller/projects/{project_id}/thumbnails")
    def list_thumbnail_jobs(project_id: int, request: Request, view: str = "queue"):
        _require_api(request)
        with _db() as conn:
            _project_row(conn, project_id)
            for r in conn.execute("SELECT permanent_id FROM autoflow_videos WHERE project_id=?", (project_id,)).fetchall():
                reconcile_thumbnail_job(conn, project_id, str(r["permanent_id"]), "thumbnail_api")
            rows = _thumbnail_jobs(conn, project_id, 1000)
            view = str(view or "queue").lower()
            if view == "completed": rows = [r for r in rows if str(r.get("status") or "").upper() in THUMBNAIL_COMPLETED_STATUSES]
            elif view == "queue": rows = [r for r in rows if str(r.get("status") or "").upper() not in THUMBNAIL_COMPLETED_STATUSES and str(r.get("status") or "").upper() != "REMOVED"]
            return {"ok":True,"view":view,"jobs":rows,"counts":_thumbnail_counts(conn,project_id)}

    @app.post("/api/master-controller/projects/{project_id}/thumbnails/reconcile")
    def reconcile_thumbnail_jobs(project_id: int, request: Request):
        _require_api(request)
        with _db() as conn:
            _project_row(conn, project_id); n=0
            for r in conn.execute("SELECT permanent_id FROM autoflow_videos WHERE project_id=?", (project_id,)).fetchall():
                if reconcile_thumbnail_job(conn, project_id, str(r["permanent_id"]), "manual_reconcile"): n += 1
            return {"ok":True,"reconciled":n,"counts":_thumbnail_counts(conn,project_id)}

    @app.post("/api/master-controller/projects/{project_id}/thumbnail/jobs/{job_id}/action")
    async def thumbnail_job_action(project_id: int, job_id: int, request: Request):
        _require_api(request); body=await request.json(); action=str(body.get("action") or "").lower()
        with _db() as conn:
            job=conn.execute("SELECT * FROM autoflow_thumbnail_jobs WHERE id=? AND project_id=?", (job_id,project_id)).fetchone()
            if not job: raise HTTPException(404,"Thumbnail job not found")
            asset=_asset(conn,project_id,str(job["permanent_id"]),"thumbnail"); prompt=str(asset["text_content"] or "").strip() if asset else str(job["prompt"] or "").strip()
            if action=="pause": target="PAUSED"
            elif action=="resume": target="PENDING" if prompt else "BLOCKED"
            elif action=="retry": target="PENDING" if prompt else "BLOCKED"
            elif action in ("remove","cancel"): target="REMOVED"
            else: raise HTTPException(400,"Unsupported thumbnail job action")
            error="" if target=="PENDING" else ("Thumbnail prompt missing" if target=="BLOCKED" else str(job["error_text"] or ""))
            current=_thumbnail_set_status(conn,job,target,error_text=error,event=f"MANUAL_{action.upper()}")
            return {"ok":True,"job":_thumbnail_job_dict(conn,current)}

    @app.get("/api/master-controller/projects/{project_id}/videos/{permanent_id}/thumbnail/audit")
    def thumbnail_audit(project_id: int, permanent_id: str, request: Request):
        _require_api(request)
        with _db() as conn:
            rows=[dict(r) for r in conn.execute("SELECT * FROM autoflow_thumbnail_audit WHERE project_id=? AND permanent_id=? ORDER BY id DESC LIMIT 250",(project_id,permanent_id)).fetchall()]
            for r in rows: r["detail"]=_json(r.pop("detail_json", "{}"), {})
            return {"ok":True,"audit":rows}

    @app.get("/api/master-controller/projects/{project_id}/schedule/suggest")
    def schedule_suggest(project_id: int, request: Request, desired: str = ""):
        _require_api(request)
        with _db() as conn: return {"ok":True,"suggested_time":_schedule_suggestion(conn,project_id,desired)}

    @app.post("/api/master-controller/control/cleanup")
    def cleanup(request: Request):
        _require_api(request)
        with _db() as conn: return {"ok":True,"removed":_purge_old_rows(conn),"retention_days":7}

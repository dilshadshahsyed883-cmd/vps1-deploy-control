from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request

from autoflow_control_backend import (
    DB_PATH,
    _asset,
    _db,
    _dependency_after,
    _ensure_video,
    _mark_outdated,
    _now,
    _project_row,
    _require_api,
    _save_version,
    _settings,
    _upsert_asset,
    _video_dict,
)

GOOGLE_AUTH_FILE = Path(os.getenv("SCRIPT_WORK_GOOGLE_AUTH_FILE", "/etc/scene-builder-web/script-work-google-auth.json"))
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]
OUTBOX_POLL_SECONDS = max(3, int(os.getenv("AUTOFLOW_GOOGLE_OUTBOX_POLL_SECONDS", "5") or 5))
_worker_started = False
_worker_lock = threading.Lock()

CORE_FIXED = {
    "id": 0,
    "youtube_link": 1,
    "title": 2,
    "description": 3,
    "status": 4,
}
HEADER_ALIASES = {
    "id": {"id", "permanent id", "permanent_id", "video id", "video_id"},
    "youtube_link": {"youtube link", "youtube url", "source link", "link", "url"},
    "title": {"title", "video title"},
    "description": {"description", "video description", "desc"},
    "tags": {"tags", "video tags", "keywords"},
    "script_link": {"script link", "script doc link", "script google doc", "script document link", "script_doc_url"},
    "scene_plan_link": {"scene plan link", "scene plan doc link", "scene plan google doc", "scene plan document link", "scene_plan_doc_url"},
    "thumbnail_prompt": {"thumbnail prompt", "thumbnail_prompt", "thumb prompt", "thumbnail generation prompt"},
    # AUTOFLOW_THUMBNAIL_PHASE1_SHEET_V1
    "thumbnail_reference": {"thumbnail reference", "thumbnail reference image", "reference image", "thumbnail_reference"},
    "thumbnail_status": {"thumbnail status", "thumbnail queue status", "thumbnail_status"},
    "thumbnail_version": {"thumbnail version", "thumbnail_version"},
    "thumbnail_file": {"thumbnail file", "thumbnail_file"},
    "thumbnail_drive_link": {"thumbnail drive link", "thumbnail link", "thumbnail_drive_link"},
    "thumbnail_flow_profile": {"flow profile used", "thumbnail flow profile", "thumbnail_flow_profile"},
    "thumbnail_final_prompt": {"final thumbnail prompt", "flow prompt used", "thumbnail_final_prompt"},
    "thumbnail_error": {"thumbnail error", "thumbnail_error"},
    "status": {"status", "workflow status"},
    "approval": {"approval", "approval status", "thumbnail approval status"},
    "schedule": {"schedule", "scheduled at", "upload time", "schedule at"},
    "youtube_url": {"published url", "youtube published url", "youtube output url", "youtube video url"},
    "updated_at": {"updated at", "updated_at", "last updated", "modified at"},
    "approval_override": {"thumbnail approval", "approval override", "thumbnail approval override"},
}
STANDARD_HEADERS = [
    "ID", "YouTube Link", "Title", "Description", "Status",
    "Approval", "Schedule", "Published URL", "Updated At", "Thumbnail Approval", "Tags", "Thumbnail Prompt",
    "Thumbnail Reference", "Thumbnail Status", "Thumbnail Version", "Thumbnail File", "Thumbnail Drive Link",
    "Flow Profile Used", "Final Thumbnail Prompt", "Thumbnail Error", "Script Link", "Scene Plan Link",
]


def init_google_schema() -> None:
    with _db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS autoflow_google_project_state (
              project_id INTEGER PRIMARY KEY,
              baseline_complete INTEGER NOT NULL DEFAULT 0,
              sheet_name TEXT NOT NULL DEFAULT '',
              last_sync_at TEXT NOT NULL DEFAULT '',
              last_error TEXT NOT NULL DEFAULT '',
              last_summary_json TEXT NOT NULL DEFAULT '{}',
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS autoflow_google_rowmap (
              project_id INTEGER NOT NULL,
              permanent_id TEXT NOT NULL,
              sheet_name TEXT NOT NULL DEFAULT '',
              row_number INTEGER NOT NULL DEFAULT 0,
              last_sheet_hash TEXT NOT NULL DEFAULT '',
              last_sheet_updated_at TEXT NOT NULL DEFAULT '',
              updated_at TEXT NOT NULL,
              PRIMARY KEY(project_id, permanent_id)
            );
            CREATE TABLE IF NOT EXISTS autoflow_google_outbox (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              project_id INTEGER NOT NULL,
              permanent_id TEXT NOT NULL DEFAULT '',
              kind TEXT NOT NULL,
              payload_json TEXT NOT NULL DEFAULT '{}',
              status TEXT NOT NULL DEFAULT 'PENDING',
              attempt_count INTEGER NOT NULL DEFAULT 0,
              error_text TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_autoflow_google_outbox_status
              ON autoflow_google_outbox(status, id);
            """
        )
        # AUTOFLOW_CHATGPT_PROMPT_LIBRARY_V2_METADATA_SCHEMA
        video_cols = {r[1] for r in conn.execute("PRAGMA table_info(autoflow_videos)").fetchall()}
        if "tags" not in video_cols:
            conn.execute("ALTER TABLE autoflow_videos ADD COLUMN tags TEXT NOT NULL DEFAULT ''")
        now = _now()
        conn.execute(
            """INSERT OR IGNORE INTO autoflow_google_project_state(project_id,updated_at)
               SELECT id,? FROM projects""", (now,)
        )


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower().replace("_", " "))


def _parse_dt(value: str) -> datetime | None:
    value = str(value or "").strip()
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _load_google_creds():
    if not GOOGLE_AUTH_FILE.is_file():
        raise RuntimeError("Server Google credential is not configured")
    data = json.loads(GOOGLE_AUTH_FILE.read_text(encoding="utf-8"))
    if str(data.get("type") or "") == "service_account":
        from google.oauth2.service_account import Credentials
        return Credentials.from_service_account_info(data, scopes=GOOGLE_SCOPES)
    from google.oauth2.credentials import Credentials
    return Credentials.from_authorized_user_info(data, GOOGLE_SCOPES)


def _sheets_service():
    from googleapiclient.discovery import build
    return build("sheets", "v4", credentials=_load_google_creds(), cache_discovery=False)


def _drive_service():
    from googleapiclient.discovery import build
    return build("drive", "v3", credentials=_load_google_creds(), cache_discovery=False)


def _spreadsheet_id(value: str) -> str:
    value = str(value or "").strip()
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", value)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{20,}", value):
        return value
    return ""


def _sheet_config(conn: sqlite3.Connection, project_id: int) -> dict[str, str]:
    project = _project_row(conn, project_id)
    row = conn.execute(
        "SELECT * FROM project_sheets WHERE project_id=? ORDER BY active DESC,id ASC LIMIT 1",
        (project_id,),
    ).fetchone()
    url = str((row["url"] if row else "") or project["control_sheet_url"] or "").strip()
    tab = str((row["tab_name"] if row else "") or "").strip()
    gid = str((row["sheet_gid"] if row and "sheet_gid" in row.keys() else "") or "").strip()
    if not gid:
        m = re.search(r"(?:[?#&]gid=)(\d+)", url)
        gid = m.group(1) if m else ""
    sid = _spreadsheet_id(url)
    return {"url": url, "spreadsheet_id": sid, "sheet_name": tab, "sheet_gid": gid}


def _docs_config(conn: sqlite3.Connection, project_id: int) -> dict[str, str]:
    p = _project_row(conn, project_id)
    return {
        "script": str(p["script_doc_url"] or "").strip(),
        "scene": str(p["scene_plan_doc_url"] or "").strip(),
    }


def _project_configured(conn: sqlite3.Connection, project_id: int) -> bool:
    sh = _sheet_config(conn, project_id)
    docs = _docs_config(conn, project_id)
    return bool(sh["spreadsheet_id"] or docs["script"] or docs["scene"])


def _header_map(header: list[Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    normalized = [_norm(x) for x in header]
    for key, aliases in HEADER_ALIASES.items():
        for i, value in enumerate(normalized):
            if value in aliases:
                out[key] = i
                break
    # Preserve the historical A-E contract even when labels are unusual.
    for key, index in CORE_FIXED.items():
        out.setdefault(key, index)
    return out


def _sheet_name(service, spreadsheet_id: str, requested: str = "", requested_gid: str = "") -> str:
    meta = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="sheets(properties(sheetId,title,index))",
    ).execute()
    sheets = meta.get("sheets") or []
    if not sheets:
        raise RuntimeError("Spreadsheet has no sheets")
    requested_gid = str(requested_gid or "").strip()
    if requested_gid:
        for s in sheets:
            props = s.get("properties") or {}
            if str(props.get("sheetId") or "") == requested_gid:
                return str(props.get("title") or "")
        raise RuntimeError(f"Sheet GID not found: {requested_gid}")
    if requested:
        for s in sheets:
            if str((s.get("properties") or {}).get("title") or "") == requested:
                return requested
        raise RuntimeError(f"Sheet tab not found: {requested}")
    sheets.sort(key=lambda s: int((s.get("properties") or {}).get("index") or 0))
    return str((sheets[0].get("properties") or {}).get("title") or "")


def _quote_sheet(name: str) -> str:
    return "'" + str(name).replace("'", "''") + "'"


def _read_sheet(service, spreadsheet_id: str, sheet_name: str) -> tuple[list[str], list[list[str]], dict[str, int]]:
    rng = f"{_quote_sheet(sheet_name)}!A1:Z5000"
    resp = service.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=rng).execute()
    values = resp.get("values") or []
    if not values:
        # Initialize only an empty sheet; never rewrite a populated user's headers.
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{_quote_sheet(sheet_name)}!A1:L1",
            valueInputOption="RAW",
            body={"values": [STANDARD_HEADERS]},
        ).execute()
        return list(STANDARD_HEADERS), [], _header_map(STANDARD_HEADERS)
    header = [str(x or "") for x in values[0]]
    normalized = {_norm(x) for x in header}
    append_headers = []
    for h in STANDARD_HEADERS[5:]:
        if _norm(h) not in normalized:
            append_headers.append(h)
    if append_headers:
        start = len(header) + 1
        end = start + len(append_headers) - 1
        def col(n: int) -> str:
            s = ""
            while n:
                n, r = divmod(n - 1, 26); s = chr(65+r) + s
            return s
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{_quote_sheet(sheet_name)}!{col(start)}1:{col(end)}1",
            valueInputOption="RAW",
            body={"values": [append_headers]},
        ).execute()
        header.extend(append_headers)
    return header, [[str(x or "") for x in r] for r in values[1:]], _header_map(header)


def _cell(row: list[str], index: int | None) -> str:
    if index is None or index < 0 or index >= len(row):
        return ""
    return str(row[index] or "").strip()


def _sheet_hash(row: list[str], hm: dict[str, int]) -> str:
    keys = ("id","youtube_link","title","description","tags","thumbnail_prompt","thumbnail_reference","thumbnail_status","thumbnail_version","thumbnail_file","thumbnail_drive_link","thumbnail_flow_profile","thumbnail_final_prompt","thumbnail_error","status","approval","schedule","youtube_url","approval_override")
    data = "\x1f".join(_cell(row, hm.get(k)) for k in keys)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _doc_helpers():
    import script_extractor_service as sw
    return sw


def _doc_text_from_tab(tab: dict[str, Any]) -> str:
    chunks: list[str] = []
    root = tab.get("documentTab") or tab
    body = root.get("body") or {}
    for item in body.get("content") or []:
        para = item.get("paragraph") or {}
        for el in para.get("elements") or []:
            tr = el.get("textRun") or {}
            if tr.get("content"):
                chunks.append(str(tr["content"]))
        table = item.get("table") or {}
        for row in table.get("tableRows") or []:
            for cell in row.get("tableCells") or []:
                for c in cell.get("content") or []:
                    para2 = c.get("paragraph") or {}
                    for el in para2.get("elements") or []:
                        tr = el.get("textRun") or {}
                        if tr.get("content"):
                            chunks.append(str(tr["content"]))
    return "".join(chunks).strip()


def _load_doc_tabs(url: str) -> tuple[str, dict[str, dict[str, Any]]]:
    if not url:
        return "", {}
    sw = _doc_helpers()
    doc_id = sw._document_id(url)
    if not doc_id:
        raise RuntimeError("Invalid Google Doc URL or ID")
    doc = sw._doc_get("", doc_id)
    tabs: dict[str, dict[str, Any]] = {}
    for tab in sw._tab_data(doc):
        title = sw._tab_title(tab)
        if title:
            tabs[str(title)] = tab
    return doc_id, tabs


def _push_doc_tab(url: str, permanent_id: str, text: str) -> dict[str, Any]:
    sw = _doc_helpers()
    doc_id = sw._document_id(url)
    if not doc_id:
        raise RuntimeError("Invalid Google Doc URL or ID")
    doc = sw._doc_get("", doc_id)
    target = None
    for tab in sw._tab_data(doc):
        if sw._tab_title(tab) == permanent_id:
            target = tab; break
    if target is None:
        tab_id = sw._append_doc("", doc_id, permanent_id, text)
        return {"document_id": doc_id, "tab_id": tab_id, "created": True}
    tab_id = sw._tab_id(target)
    sw._replace_tab_text(doc_id, tab_id, sw._tab_end(target), text)
    return {"document_id": doc_id, "tab_id": tab_id, "created": False}


def _state(conn: sqlite3.Connection, project_id: int) -> sqlite3.Row:
    _project_row(conn, project_id)
    conn.execute(
        "INSERT OR IGNORE INTO autoflow_google_project_state(project_id,updated_at) VALUES(?,?)",
        (project_id, _now()),
    )
    return conn.execute("SELECT * FROM autoflow_google_project_state WHERE project_id=?", (project_id,)).fetchone()


def _queue(conn: sqlite3.Connection, project_id: int, permanent_id: str, kind: str, payload: dict[str, Any]) -> int:
    now = _now()
    # Coalesce pending pushes for the same object; newest state wins.
    if kind == "VIDEO_PUSH":
        rows = conn.execute(
            "SELECT id FROM autoflow_google_outbox WHERE project_id=? AND permanent_id=? AND kind=? AND status='PENDING'",
            (project_id, permanent_id, kind),
        ).fetchall()
        for r in rows:
            conn.execute("UPDATE autoflow_google_outbox SET status='SUPERSEDED',updated_at=? WHERE id=?", (now, r["id"]))
    cur = conn.execute(
        """INSERT INTO autoflow_google_outbox(project_id,permanent_id,kind,payload_json,status,created_at,updated_at)
           VALUES(?,?,?,?, 'PENDING', ?,?)""",
        (project_id, permanent_id, kind, json.dumps(payload, ensure_ascii=False), now, now),
    )
    return int(cur.lastrowid)


def queue_asset_push(conn: sqlite3.Connection, project_id: int, permanent_id: str, asset_type: str, text: str) -> int | None:
    asset_type = str(asset_type or "").lower()
    if asset_type not in ("script", "scene"):
        return None
    docs = _docs_config(conn, project_id)
    if not docs.get(asset_type):
        return None
    return _queue(conn, project_id, permanent_id, "ASSET_PUSH", {"asset_type": asset_type, "text": str(text or "")})


def queue_video_push(conn: sqlite3.Connection, project_id: int, permanent_id: str) -> int | None:
    sh = _sheet_config(conn, project_id)
    if not sh["spreadsheet_id"]:
        return None
    row = conn.execute(
        "SELECT * FROM autoflow_videos WHERE project_id=? AND permanent_id=?", (project_id, permanent_id)
    ).fetchone()
    if not row:
        return None
    payload = {k: row[k] for k in row.keys()}
    thumb = conn.execute("SELECT text_content,source_url,file_url,version FROM autoflow_assets WHERE project_id=? AND permanent_id=? AND asset_type='thumbnail'", (project_id, permanent_id)).fetchone()
    job = conn.execute("SELECT * FROM autoflow_thumbnail_jobs WHERE project_id=? AND permanent_id=? ORDER BY version DESC,id DESC LIMIT 1", (project_id, permanent_id)).fetchone()
    payload["thumbnail_prompt"] = str(thumb["text_content"] or "") if thumb else ""
    payload["thumbnail_reference"] = str(thumb["source_url"] or "") if thumb else ""
    payload["thumbnail_file"] = str(thumb["file_url"] or "") if thumb else ""
    payload["thumbnail_status"] = str(job["status"] or "") if job else ""
    payload["thumbnail_version"] = str(job["version"] or (thumb["version"] if thumb else 1)) if (job or thumb) else ""
    payload["thumbnail_drive_link"] = str(job["drive_url"] or "") if job else (str(thumb["file_url"] or "") if thumb and "drive.google" in str(thumb["file_url"] or "") else "")
    payload["thumbnail_flow_profile"] = str(job["flow_profile"] or "") if job else ""
    payload["thumbnail_final_prompt"] = str(job["final_prompt"] or "") if job else ""
    payload["thumbnail_error"] = str(job["error_text"] or "") if job else ""
    return _queue(conn, project_id, permanent_id, "VIDEO_PUSH", payload)


def queue_row_delete(conn: sqlite3.Connection, project_id: int, permanent_id: str) -> int | None:
    sh = _sheet_config(conn, project_id)
    if not sh["spreadsheet_id"]:
        return None
    return _queue(conn, project_id, permanent_id, "ROW_DELETE", {})


def _find_sheet_row(service, spreadsheet_id: str, sheet_name: str, permanent_id: str) -> tuple[int, list[str], list[str], dict[str,int]]:
    header, rows, hm = _read_sheet(service, spreadsheet_id, sheet_name)
    for i, row in enumerate(rows, start=2):
        if _cell(row, hm.get("id")) == permanent_id:
            return i, row, header, hm
    return 0, [], header, hm


def _col(n0: int) -> str:
    n=n0+1; s=""
    while n:
        n,r=divmod(n-1,26); s=chr(65+r)+s
    return s


def _process_video_push(conn: sqlite3.Connection, project_id: int, permanent_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    cfg = _sheet_config(conn, project_id)
    if not cfg["spreadsheet_id"]:
        return {"skipped": "sheet_not_configured"}
    service = _sheets_service()
    sheet_name = _sheet_name(service, cfg["spreadsheet_id"], cfg["sheet_name"])
    row_number, row, header, hm = _find_sheet_row(service, cfg["spreadsheet_id"], sheet_name, permanent_id)
    width = max(len(header), max(hm.values(), default=0)+1)
    data = list(row) + [""] * max(0, width-len(row))
    values = {
        "id": permanent_id,
        "title": str(payload.get("title") or ""),
        "description": str(payload.get("description") or ""),
        "tags": str(payload.get("tags") or ""),
        "thumbnail_prompt": str(payload.get("thumbnail_prompt") or ""),
        "thumbnail_reference": str(payload.get("thumbnail_reference") or ""),
        "thumbnail_status": str(payload.get("thumbnail_status") or ""),
        "thumbnail_version": str(payload.get("thumbnail_version") or ""),
        "thumbnail_file": str(payload.get("thumbnail_file") or ""),
        "thumbnail_drive_link": str(payload.get("thumbnail_drive_link") or ""),
        "thumbnail_flow_profile": str(payload.get("thumbnail_flow_profile") or ""),
        "thumbnail_final_prompt": str(payload.get("thumbnail_final_prompt") or ""),
        "thumbnail_error": str(payload.get("thumbnail_error") or ""),
        "status": str(payload.get("status") or ""),
        "approval": str(payload.get("approval_status") or ""),
        "schedule": str(payload.get("schedule_at") or ""),
        "youtube_url": str(payload.get("youtube_url") or ""),
        "updated_at": _now(),
        "approval_override": str(payload.get("approval_override") or "DEFAULT"),
    }
    for key, value in values.items():
        if key in hm:
            while len(data) <= hm[key]: data.append("")
            data[hm[key]] = value
    if row_number:
        rng = f"{_quote_sheet(sheet_name)}!A{row_number}:{_col(len(data)-1)}{row_number}"
        service.spreadsheets().values().update(
            spreadsheetId=cfg["spreadsheet_id"], range=rng, valueInputOption="RAW", body={"values":[data]}
        ).execute()
    else:
        rng = f"{_quote_sheet(sheet_name)}!A:{_col(len(data)-1)}"
        resp = service.spreadsheets().values().append(
            spreadsheetId=cfg["spreadsheet_id"], range=rng,
            valueInputOption="RAW", insertDataOption="INSERT_ROWS", body={"values":[data]},
        ).execute()
        updated = str((resp.get("updates") or {}).get("updatedRange") or "")
        m = re.search(r"!(?:[A-Z]+)(\d+):", updated)
        row_number = int(m.group(1)) if m else 0
    conn.execute(
        """INSERT INTO autoflow_google_rowmap(project_id,permanent_id,sheet_name,row_number,last_sheet_hash,last_sheet_updated_at,updated_at)
           VALUES(?,?,?,?,?,?,?) ON CONFLICT(project_id,permanent_id) DO UPDATE SET
           sheet_name=excluded.sheet_name,row_number=excluded.row_number,last_sheet_updated_at=excluded.last_sheet_updated_at,updated_at=excluded.updated_at""",
        (project_id,permanent_id,sheet_name,row_number,"",values["updated_at"],_now()),
    )
    return {"sheet_name":sheet_name,"row_number":row_number,"created":not bool(row)}


def _process_asset_push(conn: sqlite3.Connection, project_id: int, permanent_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    asset_type = str(payload.get("asset_type") or "").lower()
    docs = _docs_config(conn, project_id)
    url = docs.get(asset_type, "")
    if not url:
        return {"skipped": f"{asset_type}_doc_not_configured"}
    result = _push_doc_tab(url, permanent_id, str(payload.get("text") or ""))
    conn.execute(
        "UPDATE autoflow_assets SET source_url=?,updated_at=? WHERE project_id=? AND permanent_id=? AND asset_type=?",
        (url,_now(),project_id,permanent_id,asset_type),
    )
    return result


def _process_row_delete(conn: sqlite3.Connection, project_id: int, permanent_id: str) -> dict[str, Any]:
    cfg = _sheet_config(conn, project_id)
    if not cfg["spreadsheet_id"]:
        return {"skipped": "sheet_not_configured"}
    service = _sheets_service()
    sheet_name = _sheet_name(service, cfg["spreadsheet_id"], cfg["sheet_name"])
    row_number, _, _, _ = _find_sheet_row(service, cfg["spreadsheet_id"], sheet_name, permanent_id)
    if not row_number:
        return {"deleted":False,"reason":"row_not_found"}
    meta = service.spreadsheets().get(spreadsheetId=cfg["spreadsheet_id"], fields="sheets(properties(sheetId,title))").execute()
    sheet_id = None
    for s in meta.get("sheets") or []:
        if str((s.get("properties") or {}).get("title") or "") == sheet_name:
            sheet_id = int((s.get("properties") or {}).get("sheetId")); break
    if sheet_id is None:
        raise RuntimeError("Sheet tab ID not found")
    service.spreadsheets().batchUpdate(
        spreadsheetId=cfg["spreadsheet_id"],
        body={"requests":[{"deleteDimension":{"range":{"sheetId":sheet_id,"dimension":"ROWS","startIndex":row_number-1,"endIndex":row_number}}}]},
    ).execute()
    conn.execute("DELETE FROM autoflow_google_rowmap WHERE project_id=?", (project_id,))
    return {"deleted":True,"row_number":row_number}


def process_outbox(limit: int = 25) -> dict[str, int]:
    stats={"completed":0,"failed":0,"skipped":0}
    with _db() as conn:
        rows=conn.execute(
            "SELECT * FROM autoflow_google_outbox WHERE status='PENDING' ORDER BY id LIMIT ?", (max(1,int(limit)),)
        ).fetchall()
        for row in rows:
            conn.execute("UPDATE autoflow_google_outbox SET status='RUNNING',attempt_count=attempt_count+1,updated_at=? WHERE id=?",(_now(),row["id"]))
            conn.commit()
            try:
                payload=json.loads(row["payload_json"] or "{}")
                if row["kind"]=="VIDEO_PUSH": result=_process_video_push(conn,int(row["project_id"]),str(row["permanent_id"]),payload)
                elif row["kind"]=="ASSET_PUSH": result=_process_asset_push(conn,int(row["project_id"]),str(row["permanent_id"]),payload)
                elif row["kind"]=="ROW_DELETE": result=_process_row_delete(conn,int(row["project_id"]),str(row["permanent_id"]))
                else: raise RuntimeError(f"Unknown outbox kind: {row['kind']}")
                status="SKIPPED" if result.get("skipped") else "COMPLETED"
                conn.execute("UPDATE autoflow_google_outbox SET status=?,error_text='',payload_json=?,updated_at=? WHERE id=?",(status,json.dumps({"request":payload,"result":result},ensure_ascii=False),_now(),row["id"]))
                stats["skipped" if status=="SKIPPED" else "completed"]+=1
            except Exception as exc:
                attempts=int(row["attempt_count"])+1
                # Keep transient failures retryable for several cycles.
                status="FAILED" if attempts>=5 else "PENDING"
                conn.execute("UPDATE autoflow_google_outbox SET status=?,error_text=?,updated_at=? WHERE id=?",(status,str(exc)[:4000],_now(),row["id"]))
                stats["failed"]+=1
            conn.commit()
    return stats


def _outbox_loop() -> None:
    while True:
        try: process_outbox(25)
        except Exception: pass
        time.sleep(OUTBOX_POLL_SECONDS)


def _start_worker() -> None:
    global _worker_started
    with _worker_lock:
        if _worker_started: return
        _worker_started=True
        threading.Thread(target=_outbox_loop,name="autoflow-google-outbox",daemon=True).start()


def _sync_docs_pull(conn: sqlite3.Connection, project_id: int, baseline: bool) -> dict[str,int]:
    stats={"script_imported":0,"scene_imported":0,"locked_skipped":0,"missing_tabs":0,"errors":0}
    docs=_docs_config(conn,project_id)
    videos=[str(r["permanent_id"]) for r in conn.execute("SELECT permanent_id FROM autoflow_videos WHERE project_id=?",(project_id,)).fetchall()]
    for asset_type in ("script","scene"):
        url=docs.get(asset_type,"")
        if not url: continue
        try:
            _, tabs=_load_doc_tabs(url)
        except Exception:
            stats["errors"]+=1; continue
        for pid in videos:
            tab=tabs.get(pid)
            if tab is None:
                existing=_asset(conn,project_id,pid,asset_type)
                if existing and str(existing["text_content"] or ""):
                    queue_asset_push(conn,project_id,pid,asset_type,str(existing["text_content"] or ""))
                else: stats["missing_tabs"]+=1
                continue
            text=_doc_text_from_tab(tab)
            current=_asset(conn,project_id,pid,asset_type)
            if current and (int(current["locked"]) or str(current["mode"]).upper()=="MANUAL"):
                if str(current["text_content"] or "").strip()!=text.strip():
                    queue_asset_push(conn,project_id,pid,asset_type,str(current["text_content"] or ""))
                    stats["locked_skipped"]+=1
                continue
            if current and str(current["text_content"] or "").strip()==text.strip():
                if str(current["source_url"] or "")!=url:
                    conn.execute("UPDATE autoflow_assets SET source_url=?,updated_at=? WHERE id=?",(url,_now(),current["id"]))
                continue
            row=_upsert_asset(conn,project_id,pid,asset_type,text_content=text,source_url=url,mode="AUTO",locked=False,outdated=False,save_previous=bool(current))
            if current and not baseline:
                _mark_outdated(conn,project_id,pid,_dependency_after(asset_type))
            stats[f"{asset_type}_imported"]+=1
    return stats


def _import_row(conn: sqlite3.Connection, project_id: int, row: list[str], hm: dict[str,int], baseline: bool) -> tuple[str,bool]:
    pid=_cell(row,hm.get("id"))
    if not pid: return "",False
    existing=conn.execute("SELECT * FROM autoflow_videos WHERE project_id=? AND permanent_id=?",(project_id,pid)).fetchone()
    is_new=existing is None
    now=_now()
    if existing is None:
        existing=_ensure_video(conn,project_id,pid,_cell(row,hm.get("title")))
    sheet_time=_parse_dt(_cell(row,hm.get("updated_at")))
    web_time=_parse_dt(str(existing["updated_at"] or ""))
    sheet_wins = baseline or is_new or sheet_time is None or web_time is None or sheet_time >= web_time
    if sheet_wins:
        fields={
            "title":_cell(row,hm.get("title")),
            "description":_cell(row,hm.get("description")),
            "tags":_cell(row,hm.get("tags")) if "tags" in hm else str(existing["tags"] or ""),
            "thumbnail_prompt":_cell(row,hm.get("thumbnail_prompt")) if "thumbnail_prompt" in hm else "",
            "thumbnail_reference":_cell(row,hm.get("thumbnail_reference")) if "thumbnail_reference" in hm else "",
            "thumbnail_file":_cell(row,hm.get("thumbnail_file")) if "thumbnail_file" in hm else "",
            "thumbnail_drive_link":_cell(row,hm.get("thumbnail_drive_link")) if "thumbnail_drive_link" in hm else "",
            "status":_cell(row,hm.get("status")) or str(existing["status"] or "WAITING"),
            "approval_status":_cell(row,hm.get("approval")) or str(existing["approval_status"] or "PENDING"),
            "schedule_at":_cell(row,hm.get("schedule")) or str(existing["schedule_at"] or ""),
            "youtube_url":_cell(row,hm.get("youtube_url")) or str(existing["youtube_url"] or ""),
            "approval_override":_cell(row,hm.get("approval_override")) or str(existing["approval_override"] or "DEFAULT"),
        }
        if fields["approval_override"].upper() not in ("DEFAULT","REQUIRED","NOT_REQUIRED"):
            fields["approval_override"]="DEFAULT"
        conn.execute(
            """UPDATE autoflow_videos SET title=?,description=?,tags=?,status=?,approval_status=?,schedule_at=?,youtube_url=?,approval_override=?,updated_at=?
               WHERE project_id=? AND permanent_id=?""",
            (fields["title"],fields["description"],fields["tags"],fields["status"],fields["approval_status"],fields["schedule_at"],fields["youtube_url"],fields["approval_override"],sheet_time.isoformat() if sheet_time else now,project_id,pid),
        )
        # AUTOFLOW_VIDEO_ROW_DOC_LINK_IMPORT_V1
        for _atype, _hkey in (("script", "script_link"), ("scene", "scene_plan_link")):
            if _hkey in hm:
                _link = _cell(row, hm.get(_hkey)).strip()
                _cur = _asset(conn, project_id, pid, _atype)
                if _link or _cur:
                    _text = str(_cur["text_content"] or "") if _cur else ""
                    _file = str(_cur["file_url"] or "") if _cur else ""
                    _old_link = str(_cur["source_url"] or "") if _cur else ""
                    _upsert_asset(conn, project_id, pid, _atype, text_content=_text, source_url=_link, file_url=_file, save_previous=bool(_cur and _old_link != _link))
        if "thumbnail_prompt" in hm or "thumbnail_reference" in hm or "thumbnail_file" in hm or "thumbnail_drive_link" in hm:
            current_thumb = _asset(conn,project_id,pid,"thumbnail")
            prompt = fields["thumbnail_prompt"] if "thumbnail_prompt" in hm else (str(current_thumb["text_content"] or "") if current_thumb else "")
            reference = fields["thumbnail_reference"] if "thumbnail_reference" in hm else (str(current_thumb["source_url"] or "") if current_thumb else "")
            sheet_file = fields["thumbnail_drive_link"] or fields["thumbnail_file"]
            file_url = sheet_file or (str(current_thumb["file_url"] or "") if current_thumb else "")
            if prompt or reference or file_url or current_thumb:
                _upsert_asset(conn,project_id,pid,"thumbnail",text_content=prompt,source_url=reference,file_url=file_url,save_previous=bool(current_thumb and (str(current_thumb["text_content"] or "") != prompt or str(current_thumb["source_url"] or "") != reference or (sheet_file and str(current_thumb["file_url"] or "") != file_url))))
            from autoflow_control_backend import reconcile_thumbnail_job
            reconcile_thumbnail_job(conn,project_id,pid,"google_sheet_sync")
    else:
        queue_video_push(conn,project_id,pid)
    return pid,is_new


def sync_project(project_id: int, trigger_new: bool = True) -> dict[str,Any]:
    summary: dict[str,Any] = {"project_id":project_id,"configured":False,"sheet":{"imported":0,"new":0,"removed":0,"web_wins":0},"docs":{},"triggered":0,"outbox":{}}
    try:
        with _db() as conn:
            project=_project_row(conn,project_id)
            state=_state(conn,project_id)
            baseline=not bool(state["baseline_complete"])
            cfg=_sheet_config(conn,project_id)
            docs_cfg=_docs_config(conn,project_id)
            summary["configured"]=_project_configured(conn,project_id)
            if not summary["configured"]:
                summary["skipped"]="resources_not_configured"
                conn.execute("UPDATE autoflow_google_project_state SET last_sync_at=?,last_error='',last_summary_json=?,updated_at=? WHERE project_id=?",(_now(),json.dumps(summary),_now(),project_id))
                return summary

        present_ids:set[str]=set()
        new_ids:list[str]=[]
        sheet_name=""
        if cfg["spreadsheet_id"]:
            service=_sheets_service()
            sheet_name=_sheet_name(service,cfg["spreadsheet_id"],cfg["sheet_name"],cfg.get("sheet_gid", ""))
            header,rows,hm=_read_sheet(service,cfg["spreadsheet_id"],sheet_name)
            with _db() as conn:
                for n,row in enumerate(rows,start=2):
                    pid,is_new=_import_row(conn,project_id,row,hm,baseline)
                    if not pid: continue
                    present_ids.add(pid); summary["sheet"]["imported"]+=1
                    if is_new: new_ids.append(pid); summary["sheet"]["new"]+=1
                    conn.execute(
                        """INSERT INTO autoflow_google_rowmap(project_id,permanent_id,sheet_name,row_number,last_sheet_hash,last_sheet_updated_at,updated_at)
                           VALUES(?,?,?,?,?,?,?) ON CONFLICT(project_id,permanent_id) DO UPDATE SET
                           sheet_name=excluded.sheet_name,row_number=excluded.row_number,last_sheet_hash=excluded.last_sheet_hash,
                           last_sheet_updated_at=excluded.last_sheet_updated_at,updated_at=excluded.updated_at""",
                        (project_id,pid,sheet_name,n,_sheet_hash(row,hm),_cell(row,hm.get("updated_at")),_now()),
                    )
                if not baseline:
                    dbids=[str(r["permanent_id"]) for r in conn.execute("SELECT permanent_id FROM autoflow_videos WHERE project_id=?",(project_id,)).fetchall()]
                    for pid in dbids:
                        if pid in present_ids: continue
                        # Sheet deletion wins: stop unfinished jobs, then remove current Web operational state.
                        conn.execute("UPDATE trigger_jobs SET status='SUPERSEDED',updated_at=? WHERE project_id=? AND permanent_id=? AND status IN ('QUEUED','FAILED')",(_now(),project_id,pid))
                        conn.execute("DELETE FROM autoflow_assets WHERE project_id=? AND permanent_id=?",(project_id,pid))
                        conn.execute("DELETE FROM autoflow_asset_versions WHERE project_id=? AND permanent_id=?",(project_id,pid))
                        conn.execute("DELETE FROM autoflow_thumbnail_history WHERE project_id=? AND permanent_id=?",(project_id,pid))
                        conn.execute("DELETE FROM autoflow_thumbnail_audit WHERE project_id=? AND permanent_id=?",(project_id,pid))
                        conn.execute("DELETE FROM autoflow_thumbnail_jobs WHERE project_id=? AND permanent_id=?",(project_id,pid))
                        conn.execute("DELETE FROM autoflow_videos WHERE project_id=? AND permanent_id=?",(project_id,pid))
                        conn.execute("DELETE FROM autoflow_google_rowmap WHERE project_id=? AND permanent_id=?",(project_id,pid))
                        summary["sheet"]["removed"]+=1
                summary["docs"]=_sync_docs_pull(conn,project_id,baseline)
                if not baseline and trigger_new and new_ids and int(project["enabled"]):
                    from autoflow_control_backend import _enqueue_compatible
                    settings=_settings(conn,project_id)
                    if not settings.get("workflow_paused"):
                        for pid in new_ids:
                            try:
                                _enqueue_compatible(conn,project_id,pid,"SHEET_READY",{"source":"google_sheet_sync","baseline":False})
                                summary["triggered"]+=1
                            except Exception: pass
                conn.execute(
                    """UPDATE autoflow_google_project_state SET baseline_complete=1,sheet_name=?,last_sync_at=?,last_error='',last_summary_json=?,updated_at=? WHERE project_id=?""",
                    (sheet_name,_now(),json.dumps(summary,ensure_ascii=False),_now(),project_id),
                )
        else:
            with _db() as conn:
                summary["docs"]=_sync_docs_pull(conn,project_id,baseline)
                # Docs-only sync must not consume the Sheet baseline. If a Sheet is configured later, its first import remains baseline-only.
                conn.execute("UPDATE autoflow_google_project_state SET last_sync_at=?,last_error='',last_summary_json=?,updated_at=? WHERE project_id=?",(_now(),json.dumps(summary,ensure_ascii=False),_now(),project_id))
        summary["outbox"]=process_outbox(100)
        return summary
    except Exception as exc:
        with _db() as conn:
            _state(conn,project_id)
            conn.execute("UPDATE autoflow_google_project_state SET last_sync_at=?,last_error=?,last_summary_json=?,updated_at=? WHERE project_id=?",(_now(),str(exc)[:4000],json.dumps(summary,ensure_ascii=False),_now(),project_id))
        raise


def google_status(project_id: int) -> dict[str,Any]:
    with _db() as conn:
        p=_project_row(conn,project_id); st=dict(_state(conn,project_id)); sh=_sheet_config(conn,project_id); docs=_docs_config(conn,project_id)
        counts={r["status"]:r["n"] for r in conn.execute("SELECT status,COUNT(*) n FROM autoflow_google_outbox WHERE project_id=? GROUP BY status",(project_id,)).fetchall()}
        return {"ok":True,"project":{"id":p["id"],"name":p["name"]},"configured":_project_configured(conn,project_id),"resources":{"sheet":bool(sh["spreadsheet_id"]),"script_doc":bool(docs["script"]),"scene_doc":bool(docs["scene"])},"state":st,"outbox":counts}


def register_autoflow_google_sync(app) -> None:
    init_google_schema()
    if os.getenv("VPS1_RUNTIME_CANDIDATE") != "1":
        _start_worker()

    @app.get("/api/master-controller/projects/{project_id}/google/status")
    def api_google_status(project_id:int, request:Request):
        _require_api(request); return google_status(project_id)

    @app.post("/api/master-controller/projects/{project_id}/google/sync")
    async def api_google_sync(project_id:int, request:Request):
        _require_api(request)
        body=await request.json()
        try: return {"ok":True,"summary":sync_project(project_id,trigger_new=bool(body.get("trigger_new",True)))}
        except Exception as exc: raise HTTPException(502,f"Google sync failed: {exc}")

    @app.post("/api/master-controller/google/sync-all")
    async def api_google_sync_all(request:Request):
        _require_api(request); body=await request.json(); results=[]; errors=[]
        with _db() as conn: ids=[int(r["id"]) for r in conn.execute("SELECT id FROM projects ORDER BY active DESC,id ASC").fetchall()]
        for pid in ids:
            try: results.append(sync_project(pid,trigger_new=bool(body.get("trigger_new",True))))
            except Exception as exc: errors.append({"project_id":pid,"error":str(exc)})
        return {"ok":not bool(errors),"results":results,"errors":errors,"projects":len(ids)}

    @app.post("/api/master-controller/google/flush")
    async def api_google_flush(request:Request):
        _require_api(request); body=await request.json(); return {"ok":True,"outbox":process_outbox(int(body.get("limit") or 100))}

    @app.delete("/api/master-controller/projects/{project_id}/videos/{permanent_id}")
    def api_delete_video(project_id:int, permanent_id:str, request:Request):
        _require_api(request)
        with _db() as conn:
            row=conn.execute("SELECT id FROM autoflow_videos WHERE project_id=? AND permanent_id=?",(project_id,permanent_id)).fetchone()
            if not row: raise HTTPException(404,"Video not found")
            queue_row_delete(conn,project_id,permanent_id)
            conn.execute("UPDATE trigger_jobs SET status='SUPERSEDED',updated_at=? WHERE project_id=? AND permanent_id=? AND status IN ('QUEUED','FAILED')",(_now(),project_id,permanent_id))
            conn.execute("DELETE FROM autoflow_assets WHERE project_id=? AND permanent_id=?",(project_id,permanent_id))
            conn.execute("DELETE FROM autoflow_asset_versions WHERE project_id=? AND permanent_id=?",(project_id,permanent_id))
            conn.execute("DELETE FROM autoflow_thumbnail_history WHERE project_id=? AND permanent_id=?",(project_id,permanent_id))
            conn.execute("DELETE FROM autoflow_videos WHERE project_id=? AND permanent_id=?",(project_id,permanent_id))
            conn.execute("DELETE FROM autoflow_google_rowmap WHERE project_id=? AND permanent_id=?",(project_id,permanent_id))
        return {"ok":True,"deleted":True,"sheet_delete_queued":True}

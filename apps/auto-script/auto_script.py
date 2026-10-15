from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException, Request

from autoflow_control_backend import _db as master_db, _now as master_now
from autoflow_chatgpt_bridge import _notify_workers
from autoflow_google_sync import _quote_sheet, _sheet_name, _sheets_service, _spreadsheet_id
from script_extractor_service import (
    _doc_get,
    _docs_service,
    _place_created_file,
    _replace_tab_text,
    _tab_data,
    _tab_end,
    _tab_id,
    _tab_title,
)

APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "data" / "auto_script.sqlite3"
TICK_LOCK = threading.Lock()
SCHEDULER_TASK: asyncio.Task | None = None
UPLOADER_WRITEBACK_PATH = Path("/var/lib/automation-shared/lum_sheet_writebacks.jsonl")

VID_ALIASES = {"vid id", "vid", "video id", "permanent id", "permanent_id", "id"}
TITLE_ALIASES = {"title", "video title", "video topic", "topic", "script title"}
DESCRIPTION_ALIASES = {"description", "video description", "desc"}
TAGS_ALIASES = {"tags", "video tags"}
SCRIPT_DOCUMENT_ALIASES = {"script document", "script doc", "script_document", "script url"}
SCENE_DOCUMENT_ALIASES = {"scene plan document", "scene plan doc", "scene_document", "scene plan url"}
DONE_ALIASES = {"done", "script done", "script status", "script_done", "completed"}
YOUTUBE_URL_ALIASES = {"youtube url", "youtube_url", "url"}
ERROR_ALIASES = {"error", "upload error", "error message"}
STATUS_ALIASES = {"status", "upload status"}
DONE_VALUES = {"YES", "Y", "TRUE", "DONE", "1"}
BRIDGE_FINAL = {"DONE", "ERROR", "CANCELLED"}
GEN_RETRY_MAX = 3
WRITE_RETRY_MAX = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _ensure_col(conn: sqlite3.Connection, table: str, name: str, ddl: str) -> None:
    if name not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def init_schema() -> None:
    with _conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS channels (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL UNIQUE,
              enabled INTEGER NOT NULL DEFAULT 1,
              sheet_url TEXT NOT NULL DEFAULT '',
              sheet_gid TEXT NOT NULL DEFAULT '',
              docs_folder_url TEXT NOT NULL DEFAULT '',
              prompt_text TEXT NOT NULL DEFAULT '',
              prompt_version INTEGER NOT NULL DEFAULT 1,
              daily_limit INTEGER NOT NULL DEFAULT 5,
              max_tabs_per_doc INTEGER NOT NULL DEFAULT 50,
              preferred_worker_id TEXT NOT NULL DEFAULT '',
              timezone TEXT NOT NULL DEFAULT 'Asia/Karachi',
              active_document_id TEXT NOT NULL DEFAULT '',
              active_document_title TEXT NOT NULL DEFAULT '',
              document_index INTEGER NOT NULL DEFAULT 0,
              tabs_used INTEGER NOT NULL DEFAULT 0,
              last_scan_at TEXT NOT NULL DEFAULT '',
              last_error TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS prompt_versions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              channel_id INTEGER NOT NULL,
              version INTEGER NOT NULL,
              prompt_text TEXT NOT NULL,
              created_at TEXT NOT NULL,
              UNIQUE(channel_id, version),
              FOREIGN KEY(channel_id) REFERENCES channels(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS items (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              channel_id INTEGER NOT NULL,
              vid TEXT NOT NULL,
              title TEXT NOT NULL DEFAULT '',
              sheet_row INTEGER NOT NULL DEFAULT 0,
              sheet_name TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'DISCOVERED',
              bridge_job_id TEXT NOT NULL DEFAULT '',
              prompt_version INTEGER NOT NULL DEFAULT 1,
              attempts INTEGER NOT NULL DEFAULT 0,
              publish_attempts INTEGER NOT NULL DEFAULT 0,
              day_key TEXT NOT NULL DEFAULT '',
              generated_text TEXT NOT NULL DEFAULT '',
              document_id TEXT NOT NULL DEFAULT '',
              document_title TEXT NOT NULL DEFAULT '',
              document_tab_id TEXT NOT NULL DEFAULT '',
              document_url TEXT NOT NULL DEFAULT '',
              error_text TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              completed_at TEXT NOT NULL DEFAULT '',
              UNIQUE(channel_id, vid),
              FOREIGN KEY(channel_id) REFERENCES channels(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_auto_script_items_status ON items(status, channel_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_auto_script_items_bridge ON items(bridge_job_id);
            CREATE INDEX IF NOT EXISTS idx_auto_script_items_day ON items(channel_id, day_key);

            CREATE TABLE IF NOT EXISTS uploader_writebacks (
              event_id TEXT PRIMARY KEY,
              status TEXT NOT NULL DEFAULT '',
              error_text TEXT NOT NULL DEFAULT '',
              processed_at TEXT NOT NULL DEFAULT ''
            );
            """
        )

        # V2 additive migration. Old fields stay in place for rollback/backward compatibility.
        for name, ddl in [
            ("metadata_prompt_text", "TEXT NOT NULL DEFAULT ''"),
            ("script_prompt_text", "TEXT NOT NULL DEFAULT ''"),
            ("scene_prompt_text", "TEXT NOT NULL DEFAULT ''"),
            ("active_scene_document_id", "TEXT NOT NULL DEFAULT ''"),
            ("active_scene_document_title", "TEXT NOT NULL DEFAULT ''"),
            ("scene_document_index", "INTEGER NOT NULL DEFAULT 0"),
            ("scene_tabs_used", "INTEGER NOT NULL DEFAULT 0"),
        ]:
            _ensure_col(conn, "channels", name, ddl)
        for name, ddl in [
            ("description", "TEXT NOT NULL DEFAULT ''"),
            ("tags", "TEXT NOT NULL DEFAULT ''"),
            ("metadata_bridge_job_id", "TEXT NOT NULL DEFAULT ''"),
            ("script_bridge_job_id", "TEXT NOT NULL DEFAULT ''"),
            ("scene_bridge_job_id", "TEXT NOT NULL DEFAULT ''"),
            ("metadata_attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("script_attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("scene_attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("metadata_write_attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("script_write_attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("scene_write_attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("script_text", "TEXT NOT NULL DEFAULT ''"),
            ("scene_plan_text", "TEXT NOT NULL DEFAULT ''"),
            ("script_document_id", "TEXT NOT NULL DEFAULT ''"),
            ("script_document_title", "TEXT NOT NULL DEFAULT ''"),
            ("script_document_tab_id", "TEXT NOT NULL DEFAULT ''"),
            ("script_document_url", "TEXT NOT NULL DEFAULT ''"),
            ("scene_document_id", "TEXT NOT NULL DEFAULT ''"),
            ("scene_document_title", "TEXT NOT NULL DEFAULT ''"),
            ("scene_document_tab_id", "TEXT NOT NULL DEFAULT ''"),
            ("scene_document_url", "TEXT NOT NULL DEFAULT ''"),
        ]:
            _ensure_col(conn, "items", name, ddl)

        # Migrate the V1 script prompt/output fields forward without changing V1 data.
        conn.execute("UPDATE channels SET script_prompt_text=prompt_text WHERE script_prompt_text='' AND prompt_text<>''")
        conn.execute("UPDATE items SET script_text=generated_text WHERE script_text='' AND generated_text<>''")
        conn.execute("UPDATE items SET script_document_id=document_id WHERE script_document_id='' AND document_id<>''")
        conn.execute("UPDATE items SET script_document_title=document_title WHERE script_document_title='' AND document_title<>''")
        conn.execute("UPDATE items SET script_document_tab_id=document_tab_id WHERE script_document_tab_id='' AND document_tab_id<>''")
        conn.execute("UPDATE items SET script_document_url=document_url WHERE script_document_url='' AND document_url<>''")
        conn.commit()


def _require_user(request: Request) -> str:
    username = str(request.session.get("username") or "").strip()
    if not username and not request.session.get("is_admin"):
        raise HTTPException(401, "Login required")
    return username or "admin"


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9_]+", " ", str(value or "").strip().lower()).strip()


def _find_header(header: list[Any], aliases: set[str]) -> int:
    normalized = [_norm(v) for v in header]
    for idx, value in enumerate(normalized):
        if value in aliases:
            return idx
    return -1


def _folder_id(value: str) -> str:
    value = str(value or "").strip()
    m = re.search(r"/folders/([A-Za-z0-9_-]+)", value)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{20,}", value):
        return value
    return ""


def _col_letter(index_zero: int) -> str:
    n = index_zero + 1
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def _local_day(channel: sqlite3.Row | dict[str, Any]) -> str:
    tz_name = str(channel["timezone"] if isinstance(channel, sqlite3.Row) else channel.get("timezone") or "Asia/Karachi")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Asia/Karachi")
    return datetime.now(tz).date().isoformat()


def _channel_public(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["enabled"] = bool(d["enabled"])
    d["today_count"] = 0
    return d


def _item_public(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["generated_text"] = ""
    d["script_text"] = ""
    d["scene_plan_text"] = ""
    return d


def _document_text_for_tab(document: dict[str, Any], tab_id: str) -> str:
    for tab in _tab_data(document):
        if _tab_id(tab) != tab_id:
            continue
        body = (tab.get("documentTab") or {}).get("body") or tab.get("body") or {}
        chunks: list[str] = []
        for block in body.get("content") or []:
            para = block.get("paragraph") or {}
            for el in para.get("elements") or []:
                tr = el.get("textRun") or {}
                if tr.get("content"):
                    chunks.append(str(tr["content"]))
        return "".join(chunks).strip()
    return ""


def _doc_state(channel: sqlite3.Row, kind: str) -> tuple[str, str, int, int]:
    if kind == "scene":
        return (
            str(channel["active_scene_document_id"] or ""),
            str(channel["active_scene_document_title"] or ""),
            int(channel["scene_document_index"] or 0),
            int(channel["scene_tabs_used"] or 0),
        )
    return (
        str(channel["active_document_id"] or ""),
        str(channel["active_document_title"] or ""),
        int(channel["document_index"] or 0),
        int(channel["tabs_used"] or 0),
    )


def _create_document(channel: sqlite3.Row, kind: str) -> tuple[str, str, int]:
    _, _, current_index, _ = _doc_state(channel, kind)
    index = current_index + 1
    label = "Scene Plans" if kind == "scene" else "Scripts"
    title = f"{channel['name']} {label} {index:03d}"
    created = _docs_service().documents().create(body={"title": title}).execute()
    document_id = str(created["documentId"])
    folder_id = _folder_id(str(channel["docs_folder_url"] or ""))
    if folder_id:
        _place_created_file(document_id, folder_id)
    with _conn() as conn:
        if kind == "scene":
            conn.execute(
                """UPDATE channels SET active_scene_document_id=?,active_scene_document_title=?,scene_document_index=?,
                   scene_tabs_used=0,updated_at=? WHERE id=?""",
                (document_id, title, index, _now(), int(channel["id"])),
            )
        else:
            conn.execute(
                """UPDATE channels SET active_document_id=?,active_document_title=?,document_index=?,
                   tabs_used=0,updated_at=? WHERE id=?""",
                (document_id, title, index, _now(), int(channel["id"])),
            )
        conn.commit()
    return document_id, title, index


def _ensure_document(channel: sqlite3.Row, kind: str) -> tuple[str, str]:
    doc_id, title, _, used = _doc_state(channel, kind)
    max_tabs = max(1, int(channel["max_tabs_per_doc"] or 50))
    if doc_id and used < max_tabs:
        try:
            _doc_get("auto-script", doc_id)
            return doc_id, title
        except Exception:
            pass
    doc_id, title, _ = _create_document(channel, kind)
    return doc_id, title


def _sanitize_tab_title(vid: str) -> str:
    value = re.sub(r"[\r\n\t]+", " ", str(vid or "").strip())
    return value[:90] or f"VID-{uuid.uuid4().hex[:8].upper()}"


def _write_verified_doc(channel_id: int, vid: str, text: str, kind: str) -> dict[str, str]:
    with _conn() as conn:
        channel = conn.execute("SELECT * FROM channels WHERE id=?", (channel_id,)).fetchone()
    if not channel:
        raise RuntimeError("Channel not found")
    doc_id, doc_title = _ensure_document(channel, kind)
    document = _doc_get("auto-script", doc_id)
    tabs = _tab_data(document)
    wanted = _sanitize_tab_title(vid)
    target = next((t for t in tabs if _tab_title(t) == wanted), None)
    created_new = False

    _, _, _, used = _doc_state(channel, kind)
    if target is None and used == 0 and len(tabs) == 1:
        target = tabs[0]
        tid = _tab_id(target)
        if tid and _tab_title(target) != wanted:
            _docs_service().documents().batchUpdate(
                documentId=doc_id,
                body={"requests": [{
                    "updateDocumentTabProperties": {
                        "tabProperties": {"tabId": tid, "title": wanted},
                        "fields": "title",
                    }
                }]},
            ).execute()
        created_new = True
        document = _doc_get("auto-script", doc_id)
        target = next((t for t in _tab_data(document) if _tab_title(t) == wanted), None)
    elif target is None:
        result = _docs_service().documents().batchUpdate(
            documentId=doc_id,
            body={"requests": [{"addDocumentTab": {"tabProperties": {"title": wanted}}}]},
        ).execute()
        reply = (result.get("replies") or [{}])[0].get("addDocumentTab") or {}
        tid = str((reply.get("tabProperties") or {}).get("tabId") or reply.get("tabId") or "")
        document = _doc_get("auto-script", doc_id)
        target = next((t for t in _tab_data(document) if (_tab_id(t) == tid if tid else _tab_title(t) == wanted)), None)
        created_new = True

    if target is None:
        raise RuntimeError("Unable to resolve Google Doc tab after creation")
    tab_id = _tab_id(target)
    _replace_tab_text(doc_id, tab_id, _tab_end(target), text)
    verified = _document_text_for_tab(_doc_get("auto-script", doc_id), tab_id)
    if hashlib.sha256(verified.strip().encode("utf-8")).hexdigest() != hashlib.sha256(text.strip().encode("utf-8")).hexdigest():
        raise RuntimeError("Google Doc verification failed: written text does not match generated output")

    if created_new:
        with _conn() as conn:
            if kind == "scene":
                conn.execute("UPDATE channels SET scene_tabs_used=scene_tabs_used+1,updated_at=? WHERE id=?", (_now(), channel_id))
            else:
                conn.execute("UPDATE channels SET tabs_used=tabs_used+1,updated_at=? WHERE id=?", (_now(), channel_id))
            conn.commit()

    return {
        "document_id": doc_id,
        "document_title": doc_title,
        "document_tab_id": tab_id,
        "document_url": f"https://docs.google.com/document/d/{doc_id}/edit",
    }


def _sheet_context(channel: sqlite3.Row) -> tuple[Any, str, str, list[list[str]], dict[str, int]]:
    sid = _spreadsheet_id(str(channel["sheet_url"] or ""))
    gid = str(channel["sheet_gid"] or "").strip()
    if not sid:
        raise RuntimeError("Invalid Google Sheet URL/ID")
    if not gid:
        raise RuntimeError("Sub-sheet GID is required")
    service = _sheets_service()
    sheet_name = _sheet_name(service, sid, requested="", requested_gid=gid)
    rng = f"{_quote_sheet(sheet_name)}!A1:Z5000"
    values = service.spreadsheets().values().get(spreadsheetId=sid, range=rng).execute().get("values") or []
    if not values:
        raise RuntimeError("Configured sheet is empty")
    header = list(values[0])
    fields = {
        "vid": _find_header(header, VID_ALIASES),
        "title": _find_header(header, TITLE_ALIASES),
        "description": _find_header(header, DESCRIPTION_ALIASES),
        "tags": _find_header(header, TAGS_ALIASES),
        "script_document": _find_header(header, SCRIPT_DOCUMENT_ALIASES),
        "scene_document": _find_header(header, SCENE_DOCUMENT_ALIASES),
        "done": _find_header(header, DONE_ALIASES),
    }
    labels = {
        "vid": "Video ID",
        "title": "Title",
        "description": "Description",
        "tags": "Tags",
        "script_document": "Script Document",
        "scene_document": "Scene Plan Document",
        "done": "Done",
    }
    missing = [labels[k] for k, idx in fields.items() if idx < 0]
    if missing:
        raise RuntimeError("Missing required sheet columns: " + ", ".join(missing))
    rows = [list(r) for r in values[1:]]
    return service, sid, sheet_name, rows, fields


def _cell(row: list[Any], idx: int) -> str:
    return str(row[idx] if idx < len(row) else "").strip()


def _guard_sheet_row(channel: sqlite3.Row, item: sqlite3.Row) -> tuple[Any, str, str, list[Any], dict[str, int]]:
    service, sid, sheet_name, rows, fields = _sheet_context(channel)
    row_num = int(item["sheet_row"] or 0)
    if row_num < 2:
        raise RuntimeError("Invalid stored sheet row")
    actual = rows[row_num - 2] if row_num - 2 < len(rows) else []
    if _cell(actual, fields["vid"]) != str(item["vid"]):
        raise RuntimeError("Sheet row Video ID changed; refusing to write to the wrong row")
    return service, sid, sheet_name, actual, fields


def _write_sheet_fields(channel: sqlite3.Row, item: sqlite3.Row, fields_to_write: dict[str, str]) -> None:
    service, sid, sheet_name, _actual, fields = _guard_sheet_row(channel, item)
    row_num = int(item["sheet_row"])
    data = []
    for key, value in fields_to_write.items():
        idx = fields[key]
        rng = f"{_quote_sheet(sheet_name)}!{_col_letter(idx)}{row_num}"
        data.append({"range": rng, "values": [[str(value)]]})
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=sid,
        body={"valueInputOption": "RAW", "data": data},
    ).execute()
    for key, expected in fields_to_write.items():
        idx = fields[key]
        rng = f"{_quote_sheet(sheet_name)}!{_col_letter(idx)}{row_num}"
        got = service.spreadsheets().values().get(spreadsheetId=sid, range=rng).execute().get("values") or []
        actual = str((got[0][0] if got and got[0] else "") or "").strip()
        if actual != str(expected).strip():
            raise RuntimeError(f"Sheet verification failed for {key}")



def _apply_uploader_writeback(event: dict[str, Any]) -> None:
    sheet_url = str(event.get("sheet_url") or "").strip()
    gid = str(event.get("sheet_gid") or "").strip()
    vid = str(event.get("video_id") or "").strip()
    if not sheet_url or not gid or not vid:
        raise RuntimeError("Uploader writeback event is missing sheet/gid/Video ID")
    sid = _spreadsheet_id(sheet_url)
    if not sid:
        raise RuntimeError("Uploader writeback has invalid Google Sheet URL/ID")
    service = _sheets_service()
    sheet_name = _sheet_name(service, sid, requested="", requested_gid=gid)
    rng = f"{_quote_sheet(sheet_name)}!A1:Z5000"
    values = service.spreadsheets().values().get(spreadsheetId=sid, range=rng).execute().get("values") or []
    if not values:
        raise RuntimeError("Uploader writeback target sheet is empty")
    header = list(values[0])
    vid_i = _find_header(header, VID_ALIASES)
    url_i = _find_header(header, YOUTUBE_URL_ALIASES)
    error_i = _find_header(header, ERROR_ALIASES)
    status_i = _find_header(header, STATUS_ALIASES)
    missing = []
    if vid_i < 0: missing.append("Video ID")
    if url_i < 0: missing.append("YouTube URL")
    if error_i < 0: missing.append("Error")
    if status_i < 0: missing.append("Status")
    if missing:
        raise RuntimeError("Uploader writeback target is missing columns: " + ", ".join(missing))

    row_num = int(event.get("row_number") or 0)
    rows = [list(r) for r in values[1:]]
    actual = rows[row_num - 2] if row_num >= 2 and row_num - 2 < len(rows) else []
    if not actual or _cell(actual, vid_i) != vid:
        matches = [idx for idx, row in enumerate(rows, start=2) if _cell(row, vid_i) == vid]
        if not matches:
            raise RuntimeError(f"Uploader writeback Video ID not found in Sheet: {vid}")
        row_num = matches[0]

    data = []
    status_value = str(event.get("status") or "").strip()
    youtube_url = str(event.get("youtube_url") or "").strip()
    error_value = str(event.get("error") or "").strip()
    if status_value:
        data.append({"range": f"{_quote_sheet(sheet_name)}!{_col_letter(status_i)}{row_num}", "values": [[status_value]]})
    if youtube_url:
        data.append({"range": f"{_quote_sheet(sheet_name)}!{_col_letter(url_i)}{row_num}", "values": [[youtube_url]]})
    data.append({"range": f"{_quote_sheet(sheet_name)}!{_col_letter(error_i)}{row_num}", "values": [[error_value]]})
    if data:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=sid, body={"valueInputOption": "RAW", "data": data}
        ).execute()

    check = service.spreadsheets().values().get(
        spreadsheetId=sid,
        range=f"{_quote_sheet(sheet_name)}!A{row_num}:Z{row_num}",
    ).execute().get("values") or []
    row = list(check[0]) if check else []
    if _cell(row, vid_i) != vid:
        raise RuntimeError("Uploader writeback verification failed: Video ID changed")
    if status_value and _cell(row, status_i) != status_value:
        raise RuntimeError("Uploader writeback verification failed: Status mismatch")
    if youtube_url and _cell(row, url_i) != youtube_url:
        raise RuntimeError("Uploader writeback verification failed: YouTube URL mismatch")
    if _cell(row, error_i) != error_value:
        raise RuntimeError("Uploader writeback verification failed: Error mismatch")


def _process_uploader_writebacks() -> int:
    path = UPLOADER_WRITEBACK_PATH
    if not path.is_file():
        return 0
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-10000:]
    except Exception:
        return 0
    processed = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
            event_id = str(event.get("event_id") or "").strip()
            if not event_id:
                continue
        except Exception:
            continue
        with _conn() as conn:
            exists = conn.execute("SELECT 1 FROM uploader_writebacks WHERE event_id=? AND status='DONE'", (event_id,)).fetchone()
        if exists:
            continue
        try:
            _apply_uploader_writeback(event)
            with _conn() as conn:
                conn.execute(
                    """INSERT INTO uploader_writebacks(event_id,status,error_text,processed_at)
                       VALUES(?,'DONE','',?)
                       ON CONFLICT(event_id) DO UPDATE SET status='DONE',error_text='',processed_at=excluded.processed_at""",
                    (event_id, _now()),
                )
                conn.commit()
            processed += 1
        except Exception as exc:
            with _conn() as conn:
                conn.execute(
                    """INSERT INTO uploader_writebacks(event_id,status,error_text,processed_at)
                       VALUES(?,'RETRY',?,?)
                       ON CONFLICT(event_id) DO UPDATE SET status='RETRY',error_text=excluded.error_text,processed_at=excluded.processed_at""",
                    (event_id, str(exc)[:2000], _now()),
                )
                conn.commit()
        if processed >= 200:
            break
    return processed


def _discover_channel(channel_id: int) -> int:
    with _conn() as conn:
        channel = conn.execute("SELECT * FROM channels WHERE id=?", (channel_id,)).fetchone()
    if not channel or not channel["enabled"]:
        return 0
    _service, _sid, sheet_name, rows, fields = _sheet_context(channel)
    found = 0
    now = _now()
    with _conn() as conn:
        for offset, row in enumerate(rows, start=2):
            vid = _cell(row, fields["vid"])
            title = _cell(row, fields["title"])
            done = _cell(row, fields["done"]).upper()
            if not vid or not title or done in DONE_VALUES:
                continue
            cur = conn.execute(
                """INSERT OR IGNORE INTO items
                   (channel_id,vid,title,sheet_row,sheet_name,status,prompt_version,created_at,updated_at)
                   VALUES(?,?,?,?,?,'DISCOVERED',?,?,?)""",
                (channel_id, vid, title, offset, sheet_name, int(channel["prompt_version"] or 1), now, now),
            )
            if cur.rowcount:
                found += 1
            else:
                conn.execute(
                    "UPDATE items SET title=?,sheet_row=?,sheet_name=?,updated_at=? WHERE channel_id=? AND vid=? AND status<>'DONE'",
                    (title, offset, sheet_name, now, channel_id, vid),
                )
        conn.execute("UPDATE channels SET last_scan_at=?,last_error='',updated_at=? WHERE id=?", (now, now, channel_id))
        conn.commit()
    return found


def _prompt_block(channel: sqlite3.Row, key: str) -> str:
    value = str(channel[key] or "").strip()
    if not value and key == "script_prompt_text":
        value = str(channel["prompt_text"] or "").strip()
    return value


def _compose_prompt(channel: sqlite3.Row, item: sqlite3.Row, stage: str) -> tuple[str, dict[str, Any], str]:
    if stage == "metadata":
        master = _prompt_block(channel, "metadata_prompt_text")
        prompt = f"""AUTO SCRIPT METADATA TASK

Channel: {channel['name']}
Video ID: {item['vid']}
Video Title: {item['title']}

CHANNEL METADATA MASTER INSTRUCTIONS
{master}

EXECUTION RULES
1. Create the final YouTube description and tags for the exact title above.
2. Do not claim that you wrote to Google Sheets or Google Docs.
3. Return JSON only with exactly these top-level fields:
{{"description":"<final description>","tags":"<final tags>"}}
4. Both fields must be ready to publish, not explanations or placeholders.
"""
        return prompt, {"required": ["description", "tags"]}, "AUTOSCRIPT_METADATA"
    if stage == "script":
        master = _prompt_block(channel, "script_prompt_text")
        prompt = f"""AUTO SCRIPT SCRIPT TASK

Channel: {channel['name']}
Video ID: {item['vid']}
Video Title: {item['title']}
Description: {item['description']}
Tags: {item['tags']}

CHANNEL SCRIPT MASTER INSTRUCTIONS
{master}

EXECUTION RULES
1. Write the complete final script for the exact video title above.
2. Follow the channel script master instructions as authoritative.
3. Do not claim that you wrote to Google Docs or Google Sheets.
4. Return JSON only with exactly this top-level field:
{{"script_text":"<complete final script>"}}
5. script_text must contain the full usable script, not an outline, summary, explanation, or placeholder.
"""
        return prompt, {"required": ["script_text"]}, "AUTOSCRIPT_SCRIPT"
    master = _prompt_block(channel, "scene_prompt_text")
    prompt = f"""AUTO SCRIPT SCENE PLAN TASK

Channel: {channel['name']}
Video ID: {item['vid']}
Video Title: {item['title']}

CHANNEL SCENE PLAN MASTER INSTRUCTIONS
{master}

COMPLETED SCRIPT
{item['script_text']}

EXECUTION RULES
1. Build the final scene plan from the completed script above.
2. Preserve the script's content/order unless the scene-plan master instructions explicitly require otherwise.
3. Do not claim that you wrote to Google Docs or Google Sheets.
4. Return JSON only with exactly this top-level field:
{{"scene_plan_text":"<complete final scene plan>"}}
5. scene_plan_text must be the full usable scene plan, not a summary or placeholder.
"""
    return prompt, {"required": ["scene_plan_text"]}, "AUTOSCRIPT_SCENE_PLAN"


def _insert_bridge_job(channel: sqlite3.Row, item: sqlite3.Row, stage: str) -> str:
    jid = str(uuid.uuid4())
    now = master_now()
    prompt, expected, task_type = _compose_prompt(channel, item, stage)
    input_json = {"source": "AUTO_SCRIPT", "stage": stage, "channel": channel["name"], "vid": item["vid"], "title": item["title"]}
    preferred = str(channel["preferred_worker_id"] or "")
    with master_db() as conn:
        conn.execute(
            """INSERT INTO autoflow_chatgpt_jobs
               (id,project_id,permanent_id,task_type,mode,status,priority,prompt_text,prompt_version,
                input_json,output_json,preferred_worker_id,test_only,created_at,updated_at)
               VALUES(?,NULL,?,?, 'NEW_CHAT','QUEUED',90,?,?,?,?,?,1,?,?)""",
            (
                jid,
                str(item["vid"]),
                task_type,
                prompt,
                f"AUTOSCRIPT@{int(channel['prompt_version'] or 1)}:{stage}",
                json.dumps(input_json, ensure_ascii=False),
                json.dumps(expected, ensure_ascii=False),
                preferred,
                now,
                now,
            ),
        )
        conn.commit()
    return jid


def _bridge_row(job_id: str) -> sqlite3.Row | None:
    if not job_id:
        return None
    with master_db() as conn:
        return conn.execute("SELECT * FROM autoflow_chatgpt_jobs WHERE id=?", (job_id,)).fetchone()


def _response_obj(bridge: sqlite3.Row) -> dict[str, Any]:
    for raw in (str(bridge["response_json"] or "").strip(), str(bridge["response_text"] or "").strip()):
        if not raw:
            continue
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
        m = re.search(r"\{.*\}", raw, flags=re.S)
        if m:
            try:
                obj = json.loads(m.group(0))
                if isinstance(obj, dict):
                    return obj
            except Exception:
                pass
    return {}


def _write_metadata(item_id: int) -> None:
    with _conn() as conn:
        item = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        channel = conn.execute("SELECT * FROM channels WHERE id=?", (int(item["channel_id"]),)).fetchone() if item else None
    if not item or not channel:
        return
    if not str(item["description"] or "").strip() or not str(item["tags"] or "").strip():
        raise RuntimeError("Metadata output is incomplete")
    _write_sheet_fields(channel, item, {"description": str(item["description"]), "tags": str(item["tags"])})
    with _conn() as conn:
        conn.execute("UPDATE items SET status='SCRIPT_READY',error_text='',updated_at=? WHERE id=?", (_now(), item_id))
        conn.commit()


def _write_script(item_id: int) -> None:
    with _conn() as conn:
        item = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        channel = conn.execute("SELECT * FROM channels WHERE id=?", (int(item["channel_id"]),)).fetchone() if item else None
    if not item or not channel:
        return
    text = str(item["script_text"] or "").strip()
    if len(text) < 100:
        raise RuntimeError("Generated script is empty or too short")
    info = _write_verified_doc(int(channel["id"]), str(item["vid"]), text, "script")
    _write_sheet_fields(channel, item, {"script_document": info["document_url"]})
    with _conn() as conn:
        conn.execute(
            """UPDATE items SET status='SCENE_READY',generated_text=?,document_id=?,document_title=?,document_tab_id=?,document_url=?,
               script_document_id=?,script_document_title=?,script_document_tab_id=?,script_document_url=?,error_text='',updated_at=? WHERE id=?""",
            (text, info["document_id"], info["document_title"], info["document_tab_id"], info["document_url"],
             info["document_id"], info["document_title"], info["document_tab_id"], info["document_url"], _now(), item_id),
        )
        conn.commit()


def _write_scene_and_finish(item_id: int) -> None:
    with _conn() as conn:
        item = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        channel = conn.execute("SELECT * FROM channels WHERE id=?", (int(item["channel_id"]),)).fetchone() if item else None
    if not item or not channel:
        return
    text = str(item["scene_plan_text"] or "").strip()
    if len(text) < 100:
        raise RuntimeError("Generated scene plan is empty or too short")
    info = _write_verified_doc(int(channel["id"]), str(item["vid"]), text, "scene")
    _write_sheet_fields(channel, item, {"scene_document": info["document_url"], "done": "YES"})
    now = _now()
    with _conn() as conn:
        conn.execute(
            """UPDATE items SET status='DONE',scene_document_id=?,scene_document_title=?,scene_document_tab_id=?,scene_document_url=?,
               error_text='',completed_at=?,updated_at=? WHERE id=?""",
            (info["document_id"], info["document_title"], info["document_tab_id"], info["document_url"], now, now, item_id),
        )
        conn.commit()


def _retry_write(item: sqlite3.Row, stage: str) -> None:
    field = {"metadata": "metadata_write_attempts", "script": "script_write_attempts", "scene": "scene_write_attempts"}[stage]
    try:
        if stage == "metadata":
            _write_metadata(int(item["id"]))
        elif stage == "script":
            _write_script(int(item["id"]))
        else:
            _write_scene_and_finish(int(item["id"]))
    except Exception as exc:
        attempts = int(item[field] or 0) + 1
        status = "ERROR" if attempts >= WRITE_RETRY_MAX else f"{stage.upper()}_WRITE_RETRY"
        with _conn() as conn:
            conn.execute(f"UPDATE items SET status=?,{field}=?,publish_attempts=publish_attempts+1,error_text=?,updated_at=? WHERE id=?",
                         (status, attempts, str(exc)[:2000], _now(), int(item["id"])))
            conn.commit()


def _generation_retry(item: sqlite3.Row, stage: str, err: str) -> None:
    field = {"metadata": "metadata_attempts", "script": "script_attempts", "scene": "scene_attempts"}[stage]
    attempts = int(item[field] or 0)
    status = "ERROR" if attempts >= GEN_RETRY_MAX else f"{stage.upper()}_RETRY"
    job_field = {"metadata": "metadata_bridge_job_id", "script": "script_bridge_job_id", "scene": "scene_bridge_job_id"}[stage]
    with _conn() as conn:
        conn.execute(f"UPDATE items SET status=?,{job_field}='',bridge_job_id='',error_text=?,updated_at=? WHERE id=?",
                     (status, err[:2000], _now(), int(item["id"])))
        conn.commit()


def _reconcile() -> None:
    with _conn() as conn:
        items = conn.execute("SELECT * FROM items WHERE status NOT IN ('DISCOVERED','DONE','ERROR','METADATA_RETRY','SCRIPT_READY','SCRIPT_RETRY','SCENE_READY','SCENE_RETRY') ORDER BY updated_at ASC LIMIT 150").fetchall()
    for item in items:
        status = str(item["status"] or "")
        if status == "METADATA_WRITE_RETRY":
            _retry_write(item, "metadata"); continue
        if status == "SCRIPT_WRITE_RETRY":
            _retry_write(item, "script"); continue
        if status == "SCENE_WRITE_RETRY":
            _retry_write(item, "scene"); continue

        stage = "metadata" if status.startswith("METADATA_") else "script" if status.startswith("SCRIPT_") else "scene" if status.startswith("SCENE_") else ""
        if not stage:
            continue
        job_field = {"metadata": "metadata_bridge_job_id", "script": "script_bridge_job_id", "scene": "scene_bridge_job_id"}[stage]
        bridge = _bridge_row(str(item[job_field] or item["bridge_job_id"] or ""))
        if not bridge:
            continue
        bstatus = str(bridge["status"] or "").upper()
        if bstatus in {"QUEUED", "RUNNING"}:
            wanted = f"{stage.upper()}_{bstatus}"
            if status != wanted:
                with _conn() as conn:
                    conn.execute("UPDATE items SET status=?,updated_at=? WHERE id=?", (wanted, _now(), int(item["id"])))
                    conn.commit()
            continue
        if bstatus == "DONE":
            obj = _response_obj(bridge)
            if stage == "metadata":
                description = str(obj.get("description") or "").strip()
                tags = str(obj.get("tags") or "").strip()
                if len(description) < 20 or not tags:
                    _generation_retry(item, stage, "ChatGPT returned invalid description/tags")
                    continue
                with _conn() as conn:
                    conn.execute("UPDATE items SET description=?,tags=?,status='METADATA_WRITE_RETRY',error_text='',updated_at=? WHERE id=?",
                                 (description, tags, _now(), int(item["id"])))
                    conn.commit()
                with _conn() as conn:
                    fresh = conn.execute("SELECT * FROM items WHERE id=?", (int(item["id"]),)).fetchone()
                _retry_write(fresh, "metadata")
            elif stage == "script":
                script = str(obj.get("script_text") or "").strip()
                if len(script) < 100:
                    _generation_retry(item, stage, "ChatGPT returned no valid script_text")
                    continue
                with _conn() as conn:
                    conn.execute("UPDATE items SET script_text=?,generated_text=?,status='SCRIPT_WRITE_RETRY',error_text='',updated_at=? WHERE id=?",
                                 (script, script, _now(), int(item["id"])))
                    conn.commit()
                with _conn() as conn:
                    fresh = conn.execute("SELECT * FROM items WHERE id=?", (int(item["id"]),)).fetchone()
                _retry_write(fresh, "script")
            else:
                scene = str(obj.get("scene_plan_text") or "").strip()
                if len(scene) < 100:
                    _generation_retry(item, stage, "ChatGPT returned no valid scene_plan_text")
                    continue
                with _conn() as conn:
                    conn.execute("UPDATE items SET scene_plan_text=?,status='SCENE_WRITE_RETRY',error_text='',updated_at=? WHERE id=?",
                                 (scene, _now(), int(item["id"])))
                    conn.commit()
                with _conn() as conn:
                    fresh = conn.execute("SELECT * FROM items WHERE id=?", (int(item["id"]),)).fetchone()
                _retry_write(fresh, "scene")
            continue
        if bstatus in {"ERROR", "CANCELLED"}:
            err = str(bridge["error_text"] or bridge["error_code"] or f"Bridge job {bstatus}")
            _generation_retry(item, stage, err)


def _queue_stage(channel: sqlite3.Row, item: sqlite3.Row, stage: str, day: str) -> tuple[str, str]:
    jid = _insert_bridge_job(channel, item, stage)
    attempt_field = {"metadata": "metadata_attempts", "script": "script_attempts", "scene": "scene_attempts"}[stage]
    job_field = {"metadata": "metadata_bridge_job_id", "script": "script_bridge_job_id", "scene": "scene_bridge_job_id"}[stage]
    status = f"{stage.upper()}_QUEUED"
    with _conn() as conn:
        conn.execute(
            f"""UPDATE items SET status=?,bridge_job_id=?,{job_field}=?,{attempt_field}={attempt_field}+1,
                attempts=attempts+1,day_key=CASE WHEN day_key='' THEN ? ELSE day_key END,prompt_version=?,error_text='',updated_at=? WHERE id=?""",
            (status, jid, jid, day, int(channel["prompt_version"] or 1), _now(), int(item["id"])),
        )
        conn.commit()
    return jid, str(channel["preferred_worker_id"] or "")


def _enqueue_available(channel_id: int | None = None) -> list[tuple[str, str]]:
    notify: list[tuple[str, str]] = []
    with _conn() as conn:
        channels = conn.execute("SELECT * FROM channels WHERE enabled=1 ORDER BY id" if channel_id is None else "SELECT * FROM channels WHERE id=? AND enabled=1", (() if channel_id is None else (channel_id,))).fetchall()
    for channel in channels:
        day = _local_day(channel)
        with _conn() as conn:
            # Continue already-started items regardless of today's new-item quota.
            continuing = conn.execute(
                """SELECT * FROM items WHERE channel_id=? AND status IN ('METADATA_RETRY','SCRIPT_READY','SCRIPT_RETRY','SCENE_READY','SCENE_RETRY')
                   ORDER BY sheet_row ASC,id ASC LIMIT 100""", (int(channel["id"]),)
            ).fetchall()
        for item in continuing:
            status = str(item["status"])
            stage = "metadata" if status == "METADATA_RETRY" else "script" if status in {"SCRIPT_READY","SCRIPT_RETRY"} else "scene"
            try:
                notify.append(_queue_stage(channel, item, stage, day))
            except Exception as exc:
                with _conn() as conn:
                    conn.execute("UPDATE items SET status='ERROR',error_text=?,updated_at=? WHERE id=?", (str(exc)[:2000], _now(), int(item["id"])))
                    conn.commit()

        with _conn() as conn:
            used = conn.execute("SELECT COUNT(*) FROM items WHERE channel_id=? AND day_key=?", (int(channel["id"]), day)).fetchone()[0]
            room = max(0, int(channel["daily_limit"] or 0) - int(used))
            pending = conn.execute(
                "SELECT * FROM items WHERE channel_id=? AND status='DISCOVERED' ORDER BY sheet_row ASC,id ASC LIMIT ?",
                (int(channel["id"]), room),
            ).fetchall() if room > 0 else []
        for item in pending:
            try:
                notify.append(_queue_stage(channel, item, "metadata", day))
            except Exception as exc:
                with _conn() as conn:
                    conn.execute("UPDATE items SET status='ERROR',error_text=?,updated_at=? WHERE id=?", (str(exc)[:2000], _now(), int(item["id"])))
                    conn.commit()
    return notify


def _tick_sync(channel_id: int | None = None, force_scan: bool = False) -> dict[str, Any]:
    if not TICK_LOCK.acquire(blocking=False):
        return {"ok": True, "skipped": "tick already running", "notify": []}
    try:
        writebacks = _process_uploader_writebacks()
        _reconcile()
        with _conn() as conn:
            channels = conn.execute("SELECT * FROM channels WHERE enabled=1 ORDER BY id" if channel_id is None else "SELECT * FROM channels WHERE id=? AND enabled=1", (() if channel_id is None else (channel_id,))).fetchall()
        discovered = 0
        for ch in channels:
            should_scan = force_scan or not str(ch["last_scan_at"] or "")
            if not should_scan and ch["last_scan_at"]:
                try:
                    last = datetime.fromisoformat(str(ch["last_scan_at"]).replace("Z", "+00:00"))
                    should_scan = (datetime.now(timezone.utc) - last.astimezone(timezone.utc)).total_seconds() >= 120
                except Exception:
                    should_scan = True
            if should_scan:
                try:
                    discovered += _discover_channel(int(ch["id"]))
                except Exception as exc:
                    with _conn() as conn:
                        conn.execute("UPDATE channels SET last_error=?,last_scan_at=?,updated_at=? WHERE id=?", (str(exc)[:2000], _now(), _now(), int(ch["id"])))
                        conn.commit()
        notify = _enqueue_available(channel_id)
        return {"ok": True, "discovered": discovered, "writebacks": writebacks, "notify": notify}
    finally:
        TICK_LOCK.release()


async def _run_tick(channel_id: int | None = None, force_scan: bool = False) -> dict[str, Any]:
    result = await asyncio.to_thread(_tick_sync, channel_id, force_scan)
    for _jid, preferred in result.get("notify") or []:
        try:
            await _notify_workers({"type": "job_available", "source": "auto_script"}, preferred)
        except Exception:
            pass
    return result


async def _scheduler() -> None:
    while True:
        try:
            await _run_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(30)


def register_auto_script(app, templates) -> None:
    init_schema()

    @app.on_event("startup")
    async def _auto_script_startup():
        global SCHEDULER_TASK
        if SCHEDULER_TASK is None or SCHEDULER_TASK.done():
            SCHEDULER_TASK = asyncio.create_task(_scheduler())

    @app.on_event("shutdown")
    async def _auto_script_shutdown():
        global SCHEDULER_TASK
        if SCHEDULER_TASK and not SCHEDULER_TASK.done():
            SCHEDULER_TASK.cancel()
            try:
                await SCHEDULER_TASK
            except BaseException:
                pass
        SCHEDULER_TASK = None

    @app.get("/auto-script")
    def auto_script_page(request: Request):
        _require_user(request)
        return templates.TemplateResponse("auto_script.html", {"request": request, "page": "auto_script", "username": request.session.get("username", "team")})

    @app.get("/api/auto-script/health")
    def auto_script_health(request: Request):
        _require_user(request)
        with _conn() as conn:
            channels = conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
            active = conn.execute("SELECT COUNT(*) FROM items WHERE status NOT IN ('DONE','ERROR')").fetchone()[0]
        return {"ok": True, "db": str(DB_PATH), "channels": channels, "active_items": active, "scheduler": bool(SCHEDULER_TASK and not SCHEDULER_TASK.done()), "contract_version": 2}

    @app.get("/api/auto-script/state")
    def auto_script_state(request: Request):
        _require_user(request)
        with _conn() as conn:
            channels = conn.execute("SELECT * FROM channels ORDER BY id").fetchall()
            items = conn.execute("SELECT * FROM items ORDER BY updated_at DESC LIMIT 250").fetchall()
        channel_out = []
        documents = []
        for ch in channels:
            d = _channel_public(ch)
            with _conn() as conn:
                d["today_count"] = conn.execute("SELECT COUNT(*) FROM items WHERE channel_id=? AND day_key=?", (int(ch["id"]), _local_day(ch))).fetchone()[0]
                d["done_count"] = conn.execute("SELECT COUNT(*) FROM items WHERE channel_id=? AND status='DONE'", (int(ch["id"]),)).fetchone()[0]
            channel_out.append(d)
            if ch["active_document_id"]:
                documents.append({"channel_id": ch["id"], "name": ch["name"], "kind": "Script", "document_id": ch["active_document_id"], "document_title": ch["active_document_title"], "document_index": ch["document_index"], "tabs_used": ch["tabs_used"], "max_tabs_per_doc": ch["max_tabs_per_doc"]})
            if ch["active_scene_document_id"]:
                documents.append({"channel_id": ch["id"], "name": ch["name"], "kind": "Scene Plan", "document_id": ch["active_scene_document_id"], "document_title": ch["active_scene_document_title"], "document_index": ch["scene_document_index"], "tabs_used": ch["scene_tabs_used"], "max_tabs_per_doc": ch["max_tabs_per_doc"]})
        return {"ok": True, "channels": channel_out, "items": [_item_public(r) for r in items], "documents": documents, "scheduler": bool(SCHEDULER_TASK and not SCHEDULER_TASK.done()), "contract_version": 2}

    @app.post("/api/auto-script/channels")
    async def save_channel(request: Request):
        _require_user(request)
        body = await request.json()
        channel_id = int(body.get("id") or 0)
        name = str(body.get("name") or "").strip()
        sheet_url = str(body.get("sheet_url") or "").strip()
        sheet_gid = str(body.get("sheet_gid") or "").strip()
        docs_folder_url = str(body.get("docs_folder_url") or "").strip()
        metadata_prompt = str(body.get("metadata_prompt_text") or "").strip()
        script_prompt = str(body.get("script_prompt_text") or body.get("prompt_text") or "").strip()
        scene_prompt = str(body.get("scene_prompt_text") or "").strip()
        preferred_worker_id = str(body.get("preferred_worker_id") or "").strip()
        timezone_name = str(body.get("timezone") or "Asia/Karachi").strip() or "Asia/Karachi"
        daily_limit = max(1, min(500, int(body.get("daily_limit") or 5)))
        max_tabs = max(1, min(500, int(body.get("max_tabs_per_doc") or 50)))
        enabled = int(bool(body.get("enabled", True)))
        if not name:
            raise HTTPException(400, "Channel name is required")
        if not _spreadsheet_id(sheet_url):
            raise HTTPException(400, "Valid Google Sheet URL/ID is required")
        if not sheet_gid.isdigit():
            raise HTTPException(400, "Sub-sheet GID must be numeric")
        if docs_folder_url and not _folder_id(docs_folder_url):
            raise HTTPException(400, "Google Docs folder URL/ID is invalid")
        if not metadata_prompt or not script_prompt or not scene_prompt:
            raise HTTPException(400, "Metadata, Script, and Scene Plan master prompts are all required")
        try:
            ZoneInfo(timezone_name)
        except Exception:
            raise HTTPException(400, "Invalid timezone")

        # Validate exact gid and unified sheet contract before saving.
        sid = _spreadsheet_id(sheet_url)
        service = _sheets_service()
        try:
            _sheet_name(service, sid, requested="", requested_gid=sheet_gid)
        except Exception as exc:
            raise HTTPException(400, f"Exact sub-sheet GID validation failed: {exc}")

        now = _now()
        with _conn() as conn:
            if channel_id:
                old = conn.execute("SELECT * FROM channels WHERE id=?", (channel_id,)).fetchone()
                if not old:
                    raise HTTPException(404, "Channel not found")
                version = int(old["prompt_version"] or 1)
                if any([
                    metadata_prompt != str(old["metadata_prompt_text"] or ""),
                    script_prompt != str(old["script_prompt_text"] or old["prompt_text"] or ""),
                    scene_prompt != str(old["scene_prompt_text"] or ""),
                ]):
                    version += 1
                conn.execute(
                    """UPDATE channels SET name=?,enabled=?,sheet_url=?,sheet_gid=?,docs_folder_url=?,prompt_text=?,
                       metadata_prompt_text=?,script_prompt_text=?,scene_prompt_text=?,prompt_version=?,daily_limit=?,max_tabs_per_doc=?,
                       preferred_worker_id=?,timezone=?,last_error='',updated_at=? WHERE id=?""",
                    (name, enabled, sheet_url, sheet_gid, docs_folder_url, script_prompt, metadata_prompt, script_prompt, scene_prompt,
                     version, daily_limit, max_tabs, preferred_worker_id, timezone_name, now, channel_id),
                )
                if version != int(old["prompt_version"] or 1):
                    conn.execute("INSERT OR IGNORE INTO prompt_versions(channel_id,version,prompt_text,created_at) VALUES(?,?,?,?)", (channel_id, version, json.dumps({"metadata": metadata_prompt, "script": script_prompt, "scene": scene_prompt}, ensure_ascii=False), now))
            else:
                cur = conn.execute(
                    """INSERT INTO channels
                       (name,enabled,sheet_url,sheet_gid,docs_folder_url,prompt_text,metadata_prompt_text,script_prompt_text,scene_prompt_text,prompt_version,
                        daily_limit,max_tabs_per_doc,preferred_worker_id,timezone,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?)""",
                    (name, enabled, sheet_url, sheet_gid, docs_folder_url, script_prompt, metadata_prompt, script_prompt, scene_prompt,
                     daily_limit, max_tabs, preferred_worker_id, timezone_name, now, now),
                )
                channel_id = int(cur.lastrowid)
                conn.execute("INSERT INTO prompt_versions(channel_id,version,prompt_text,created_at) VALUES(?,?,?,?)", (channel_id, 1, json.dumps({"metadata": metadata_prompt, "script": script_prompt, "scene": scene_prompt}, ensure_ascii=False), now))
            conn.commit()
        # Validate the required unified columns using the saved channel.
        with _conn() as conn:
            saved = conn.execute("SELECT * FROM channels WHERE id=?", (channel_id,)).fetchone()
        try:
            _sheet_context(saved)
        except Exception as exc:
            with _conn() as conn:
                conn.execute("UPDATE channels SET last_error=?,updated_at=? WHERE id=?", (str(exc)[:2000], _now(), channel_id)); conn.commit()
            raise HTTPException(400, str(exc))
        await _run_tick(channel_id, force_scan=True)
        with _conn() as conn:
            row = conn.execute("SELECT * FROM channels WHERE id=?", (channel_id,)).fetchone()
        return {"ok": True, "channel": _channel_public(row)}

    @app.post("/api/auto-script/channels/{channel_id}/toggle")
    async def toggle_channel(channel_id: int, request: Request):
        _require_user(request)
        body = await request.json()
        enabled = int(bool(body.get("enabled", True)))
        with _conn() as conn:
            cur = conn.execute("UPDATE channels SET enabled=?,updated_at=? WHERE id=?", (enabled, _now(), channel_id)); conn.commit()
        if not cur.rowcount:
            raise HTTPException(404, "Channel not found")
        if enabled:
            await _run_tick(channel_id, force_scan=True)
        return {"ok": True, "enabled": bool(enabled)}

    @app.post("/api/auto-script/channels/{channel_id}/run")
    async def run_channel(channel_id: int, request: Request):
        _require_user(request)
        with _conn() as conn:
            row = conn.execute("SELECT id FROM channels WHERE id=?", (channel_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Channel not found")
        return await _run_tick(channel_id, force_scan=True)

    @app.post("/api/auto-script/items/{item_id}/retry")
    async def retry_item(item_id: int, request: Request):
        _require_user(request)
        with _conn() as conn:
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if not row:
                raise HTTPException(404, "Item not found")
            if row["status"] == "DONE":
                raise HTTPException(409, "Completed item cannot be retried")
            if str(row["scene_plan_text"] or "").strip():
                next_status = "SCENE_WRITE_RETRY"
            elif str(row["script_document_url"] or "").strip():
                next_status = "SCENE_RETRY"
            elif str(row["script_text"] or "").strip():
                next_status = "SCRIPT_WRITE_RETRY"
            elif str(row["description"] or "").strip() and str(row["tags"] or "").strip():
                next_status = "SCRIPT_RETRY"
            else:
                next_status = "METADATA_RETRY"
            conn.execute("UPDATE items SET status=?,bridge_job_id='',error_text='',updated_at=? WHERE id=?", (next_status, _now(), item_id)); conn.commit()
        return await _run_tick(int(row["channel_id"]))

    @app.post("/api/auto-script/tick")
    async def run_all(request: Request):
        _require_user(request)
        return await _run_tick(force_scan=True)

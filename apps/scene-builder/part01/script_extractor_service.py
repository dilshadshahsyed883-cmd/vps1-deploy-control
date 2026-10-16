from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import string
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from fastapi import HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("SCRIPT_WORK_DATA_DIR", str(APP_DIR / "data" / "script_work"))).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "script_work.sqlite3"
GOOGLE_AUTH_FILE = Path(os.getenv("SCRIPT_WORK_GOOGLE_AUTH_FILE", "/etc/scene-builder-web/script-work-google-auth.json"))
GOOGLE_FOLDER_ID = os.getenv("SCRIPT_WORK_GOOGLE_FOLDER_ID", "").strip()
EXTRACTED_FOLDER_NAME = "Extracted Scripts"
PROCESSED_FOLDER_NAME = "Processed Scripts"
LINKS_FOLDER_NAME = "YouTube Links"
CDP_URL = os.getenv("SCRIPT_WORK_CDP_URL", "http://127.0.0.1:9223")
NOVNC_URL = os.getenv("SCRIPT_WORK_NOVNC_URL", "http://127.0.0.1:6081")
NOVNC_WS = os.getenv("SCRIPT_WORK_NOVNC_WS", "ws://127.0.0.1:6081/websockify")
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
]
YOUTUBE_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?v=|shorts/|live/|embed/)|youtu\.be/)([A-Za-z0-9_-]{6,})",
    re.I,
)

_worker_started = False
_worker_lock = threading.Lock()
_browser_lock = threading.RLock()
_browser_state: dict[str, Any] = {
    "state": "starting",
    "current_url": "",
    "last_title": "",
    "last_error": "",
    "updated_at": "",
}
_playwright = None
_browser = None
_browser_context = None
_browser_page = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _init_storage() -> None:
    with _conn() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                owner TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(owner, key)
            );
            CREATE TABLE IF NOT EXISTS groups (
                owner TEXT NOT NULL, name TEXT NOT NULL, document_id TEXT NOT NULL DEFAULT '',
                document_title TEXT NOT NULL DEFAULT '', document_url TEXT NOT NULL DEFAULT '',
                links_document_id TEXT NOT NULL DEFAULT '', links_document_url TEXT NOT NULL DEFAULT '',
                control TEXT NOT NULL DEFAULT 'idle', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(owner, name)
            );
            CREATE TABLE IF NOT EXISTS batches (
                owner TEXT NOT NULL, group_name TEXT NOT NULL, batch_id TEXT NOT NULL,
                document_id TEXT NOT NULL DEFAULT '', document_title TEXT NOT NULL DEFAULT '',
                document_url TEXT NOT NULL DEFAULT '', links_document_id TEXT NOT NULL DEFAULT '',
                links_document_url TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'ready',
                control TEXT NOT NULL DEFAULT 'ready', error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                completed_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(owner, batch_id),
                FOREIGN KEY(owner, group_name) REFERENCES groups(owner, name) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS batches_group_created
                ON batches(owner, group_name, created_at DESC);
            CREATE TABLE IF NOT EXISTS jobs (
                owner TEXT NOT NULL, group_name TEXT NOT NULL, permanent_id TEXT NOT NULL,
                video_id TEXT NOT NULL, url TEXT NOT NULL, source_title TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending', error TEXT NOT NULL DEFAULT '',
                retry_count INTEGER NOT NULL DEFAULT 0, doc_tab_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, completed_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(owner, group_name, permanent_id),
                UNIQUE(owner, group_name, video_id),
                FOREIGN KEY(owner, group_name) REFERENCES groups(owner, name) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS jobs_group_status ON jobs(owner, group_name, status);
            """
        )
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(groups)").fetchall()
        }
        if "links_document_id" not in columns:
            connection.execute(
                "ALTER TABLE groups ADD COLUMN links_document_id TEXT NOT NULL DEFAULT ''"
            )
        if "links_document_url" not in columns:
            connection.execute(
                "ALTER TABLE groups ADD COLUMN links_document_url TEXT NOT NULL DEFAULT ''"
            )
        job_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
        }
        if "batch_id" not in job_columns:
            connection.execute(
                "ALTER TABLE jobs ADD COLUMN batch_id TEXT NOT NULL DEFAULT ''"
            )
        legacy_groups = connection.execute(
            "SELECT * FROM groups ORDER BY created_at"
        ).fetchall()
        for group in legacy_groups:
            has_legacy = connection.execute(
                "SELECT 1 FROM jobs WHERE owner=? AND group_name=? LIMIT 1",
                (group["owner"], group["name"]),
            ).fetchone()
            if not has_legacy and not str(group["document_id"] or ""):
                continue
            legacy_id = f"{group['name']}-LEGACY"
            statuses = [
                str(row["status"])
                for row in connection.execute(
                    "SELECT status FROM jobs WHERE owner=? AND group_name=?",
                    (group["owner"], group["name"]),
                ).fetchall()
            ]
            legacy_status = (
                "completed"
                if statuses and all(value in {"completed", "failed"} for value in statuses)
                else "running" if str(group["control"] or "") == "running" else "ready"
            )
            connection.execute(
                """INSERT OR IGNORE INTO batches(
                     owner,group_name,batch_id,document_id,document_title,document_url,
                     links_document_id,links_document_url,status,control,created_at,updated_at,
                     completed_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    group["owner"], group["name"], legacy_id,
                    group["document_id"], group["document_title"], group["document_url"],
                    group["links_document_id"], group["links_document_url"],
                    legacy_status, group["control"] or "ready",
                    group["created_at"], group["updated_at"],
                    group["updated_at"] if legacy_status == "completed" else "",
                ),
            )
            connection.execute(
                """UPDATE jobs SET batch_id=?
                   WHERE owner=? AND group_name=? AND batch_id=''""",
                (legacy_id, group["owner"], group["name"]),
            )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS jobs_batch_status ON jobs(owner,batch_id,status)"
        )
        connection.execute(
            "UPDATE jobs SET status='pending', updated_at=? WHERE status='processing'",
            (_now(),),
        )
        connection.commit()


def _owner(request: Request) -> str:
    owner = str(request.session.get("username") or "team").strip()
    return owner[:120] or "team"


def _document_id(value: str) -> str:
    value = str(value or "").strip()
    match = re.search(r"/document/d/([A-Za-z0-9_-]+)", value)
    if match:
        return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{20,}", value):
        return value
    raise HTTPException(400, "Enter a valid Google Docs URL or document ID.")


def _video_id(url: str) -> str:
    match = YOUTUBE_RE.search(str(url or "").strip())
    if not match:
        raise ValueError("Not a supported YouTube URL")
    return match.group(1)


def _normal_url(url: str) -> str:
    return f"https://www.youtube.com/watch?v={_video_id(url)}"


def _load_creds(owner: str = ""):
    """Load the one server-managed Google identity; never accept web-user credentials."""
    if not GOOGLE_AUTH_FILE.is_file():
        raise RuntimeError("Server Google credential is not configured")
    try:
        data = json.loads(GOOGLE_AUTH_FILE.read_text(encoding="utf-8"))
        if str(data.get("type") or "") == "service_account":
            from google.oauth2.service_account import Credentials

            return Credentials.from_service_account_info(data, scopes=GOOGLE_SCOPES)
        from google.oauth2.credentials import Credentials

        return Credentials.from_authorized_user_info(data, GOOGLE_SCOPES)
    except Exception as exc:
        raise RuntimeError(f"Server Google credential is invalid: {exc}") from exc


def _docs_service(owner: str = ""):
    from googleapiclient.discovery import build

    return build("docs", "v1", credentials=_load_creds(), cache_discovery=False)


def _drive_service():
    from googleapiclient.discovery import build

    return build("drive", "v3", credentials=_load_creds(), cache_discovery=False)


def _drive_literal(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


def _ensure_drive_folder(name: str, parent_id: str = "") -> tuple[str, str]:
    parent_id = str(parent_id or GOOGLE_FOLDER_ID).strip()
    if not parent_id:
        raise RuntimeError("Script Work Google Drive folder is not configured")
    drive = _drive_service()
    query = (
        f"name = '{_drive_literal(name)}' and "
        "mimeType = 'application/vnd.google-apps.folder' and trashed = false and "
        f"'{_drive_literal(parent_id)}' in parents"
    )
    result = drive.files().list(q=query, fields="files(id,name,webViewLink)", pageSize=10).execute()
    files = result.get("files") or []
    if files:
        folder_id = str(files[0]["id"])
    else:
        folder = drive.files().create(
            body={
                "name": name,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent_id],
            },
            fields="id,webViewLink",
        ).execute()
        folder_id = str(folder["id"])
    return folder_id, f"https://drive.google.com/drive/folders/{folder_id}"


def _safe_group_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip())
    cleaned = re.sub(r"-+", "-", cleaned).strip("-._")
    return cleaned[:80]


def _new_batch_id(owner: str, group_name: str) -> str:
    alphabet = string.ascii_uppercase + string.digits
    with _conn() as connection:
        for _ in range(100):
            suffix = "".join(secrets.choice(alphabet) for _ in range(6))
            batch_id = f"{group_name}-{suffix}"
            exists = connection.execute(
                "SELECT 1 FROM batches WHERE owner=? AND batch_id=?",
                (owner, batch_id),
            ).fetchone()
            if not exists:
                return batch_id
    raise RuntimeError("Could not generate a unique Batch ID")


def _group_folder(root_name: str, group_name: str) -> tuple[str, str]:
    root_id, _ = _ensure_drive_folder(root_name)
    return _ensure_drive_folder(group_name, root_id)


def _ensure_group_layout(group_name: str) -> dict[str, tuple[str, str]]:
    extracted = _group_folder(EXTRACTED_FOLDER_NAME, group_name)
    processed = _group_folder(PROCESSED_FOLDER_NAME, group_name)
    links = _group_folder(LINKS_FOLDER_NAME, group_name)
    _ensure_drive_folder("Used", extracted[0])
    _ensure_drive_folder("Used", processed[0])
    return {"extracted": extracted, "processed": processed, "links": links}


def _batch_row(owner: str, batch_id: str) -> sqlite3.Row:
    with _conn() as connection:
        row = connection.execute(
            "SELECT * FROM batches WHERE owner=? AND batch_id=?",
            (owner, batch_id),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Batch not found")
    return row


def _move_root_file(file_id: str, target_folder_id: str) -> None:
    """Move only files currently in the configured Script Work root."""
    drive = _drive_service()
    metadata = drive.files().get(fileId=file_id, fields="parents").execute()
    parents = [str(value) for value in metadata.get("parents") or []]
    if target_folder_id in parents:
        return
    if GOOGLE_FOLDER_ID not in parents:
        return
    arguments: dict[str, Any] = {
        "fileId": file_id,
        "addParents": target_folder_id,
        "fields": "id,parents",
    }
    arguments["removeParents"] = GOOGLE_FOLDER_ID
    drive.files().update(**arguments).execute()


def _place_created_file(file_id: str, target_folder_id: str) -> None:
    drive = _drive_service()
    metadata = drive.files().get(fileId=file_id, fields="parents").execute()
    parents = [str(value) for value in metadata.get("parents") or []]
    if target_folder_id in parents:
        return
    arguments: dict[str, Any] = {
        "fileId": file_id,
        "addParents": target_folder_id,
        "fields": "id,parents",
    }
    if parents:
        arguments["removeParents"] = ",".join(parents)
    drive.files().update(**arguments).execute()


def _share_editor(file_id: str) -> None:
    try:
        _drive_service().permissions().create(
            fileId=file_id,
            body={"type": "anyone", "role": "writer"},
            fields="id",
        ).execute()
    except Exception:
        pass


def _doc_get(owner: str, document_id: str) -> dict[str, Any]:
    return (
        _docs_service()
        .documents()
        .get(documentId=document_id, includeTabsContent=True)
        .execute()
    )


def _tab_data(document: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(tabs: Any) -> None:
        for tab in tabs if isinstance(tabs, list) else []:
            if not isinstance(tab, dict):
                continue
            found.append(tab)
            walk(tab.get("childTabs"))

    walk(document.get("tabs"))
    return found or [
        {
            "tabProperties": {"tabId": "", "title": "Main document"},
            "documentTab": {"body": document.get("body", {})},
        }
    ]


def _tab_title(tab: dict[str, Any]) -> str:
    return str((tab.get("tabProperties") or {}).get("title") or tab.get("title") or "")


def _tab_id(tab: dict[str, Any]) -> str:
    return str((tab.get("tabProperties") or {}).get("tabId") or tab.get("tabId") or "")


def _tab_end(tab: dict[str, Any]) -> int:
    body = tab.get("documentTab", {}).get("body") or tab.get("body") or {}
    return max([int(item.get("endIndex", 1)) for item in body.get("content") or []] + [1])


def _append_doc(owner: str, document_id: str, tab_name: str, text: str) -> str:
    service = _docs_service()
    target = next(
        (tab for tab in _tab_data(_doc_get(owner, document_id)) if _tab_title(tab) == tab_name),
        None,
    )
    if target is None:
        result = (
            service.documents()
            .batchUpdate(
                documentId=document_id,
                body={
                    "requests": [
                        {"addDocumentTab": {"tabProperties": {"title": tab_name}}}
                    ]
                },
            )
            .execute()
        )
        reply = (result.get("replies") or [{}])[0].get("addDocumentTab") or {}
        tab_id = str(
            (reply.get("tabProperties") or {}).get("tabId")
            or reply.get("tabId")
            or ""
        )
        if not tab_id:
            target = next(
                (
                    tab
                    for tab in _tab_data(_doc_get(owner, document_id))
                    if _tab_title(tab) == tab_name
                ),
                None,
            )
            tab_id = _tab_id(target or {})
        insert_index = 1
    else:
        tab_id = _tab_id(target)
        insert_index = max(1, _tab_end(target) - 1)
    location: dict[str, Any] = {"index": insert_index}
    if tab_id:
        location["tabId"] = tab_id
    (
        service.documents()
        .batchUpdate(
            documentId=document_id,
            body={
                "requests": [
                    {"insertText": {"location": location, "text": text}}
                ]
            },
        )
        .execute()
    )
    return tab_id


def _replace_tab_text(document_id: str, tab_id: str, end_index: int, text: str) -> None:
    requests: list[dict[str, Any]] = []
    if end_index > 2:
        requests.append(
            {
                "deleteContentRange": {
                    "range": {
                        "startIndex": 1,
                        "endIndex": end_index - 1,
                        **({"tabId": tab_id} if tab_id else {}),
                    }
                }
            }
        )
    location: dict[str, Any] = {"index": 1}
    if tab_id:
        location["tabId"] = tab_id
    requests.append(
        {"insertText": {"location": location, "text": text.rstrip() + "\n"}}
    )
    _docs_service().documents().batchUpdate(
        documentId=document_id, body={"requests": requests}
    ).execute()


def _ensure_extracted_document(owner: str, batch_id: str) -> tuple[str, str]:
    with _conn() as connection:
        batch = connection.execute(
            "SELECT * FROM batches WHERE owner=? AND batch_id=?",
            (owner, batch_id),
        ).fetchone()
    if not batch:
        raise RuntimeError("Batch not found")
    layout = _ensure_group_layout(str(batch["group_name"]))
    document_id = str(batch["document_id"] or "")
    title = str(batch["document_title"] or batch_id)
    if document_id:
        try:
            document = _doc_get(owner, document_id)
            _move_root_file(document_id, layout["extracted"][0])
            title = str(document.get("title") or title)
        except Exception:
            document_id = ""
    if not document_id:
        created = _docs_service().documents().create(body={"title": batch_id}).execute()
        document_id = str(created["documentId"])
        title = batch_id
        _place_created_file(document_id, layout["extracted"][0])
        _share_editor(document_id)
    document_url = f"https://docs.google.com/document/d/{document_id}/edit"
    with _conn() as connection:
        connection.execute(
            """UPDATE batches SET document_id=?,document_title=?,document_url=?,updated_at=?
               WHERE owner=? AND batch_id=?""",
            (document_id, title, document_url, _now(), owner, batch_id),
        )
        connection.commit()
    return document_id, document_url


def _sync_links_document(owner: str, batch_id: str) -> tuple[str, str]:
    with _conn() as connection:
        batch = connection.execute(
            "SELECT * FROM batches WHERE owner=? AND batch_id=?",
            (owner, batch_id),
        ).fetchone()
        jobs = connection.execute(
            """SELECT permanent_id,url FROM jobs
               WHERE owner=? AND batch_id=? ORDER BY created_at""",
            (owner, batch_id),
        ).fetchall()
    if not batch:
        raise RuntimeError("Batch not found")
    layout = _ensure_group_layout(str(batch["group_name"]))
    folder_id = layout["links"][0]
    document_id = str(batch["links_document_id"] or "")
    document: dict[str, Any] | None = None
    if document_id:
        try:
            document = _doc_get(owner, document_id)
            _move_root_file(document_id, folder_id)
        except Exception:
            document_id = ""
    if not document_id:
        created = _docs_service().documents().create(
            body={"title": batch_id}
        ).execute()
        document_id = str(created["documentId"])
        _place_created_file(document_id, folder_id)
        _share_editor(document_id)
        document = _doc_get(owner, document_id)
    tabs = _tab_data(document or _doc_get(owner, document_id))
    first = tabs[0]
    tab_id = _tab_id(first)
    try:
        if tab_id and _tab_title(first) != "YouTube Links":
            _docs_service().documents().batchUpdate(
                documentId=document_id,
                body={
                    "requests": [
                        {
                            "updateDocumentTabProperties": {
                                "tabProperties": {
                                    "tabId": tab_id,
                                    "title": "YouTube Links",
                                },
                                "fields": "title",
                            }
                        }
                    ]
                },
            ).execute()
    except Exception:
        pass
    lines = ["Permanent ID\tYouTube URL"]
    lines.extend(f"{row['permanent_id']}\t{row['url']}" for row in jobs)
    _replace_tab_text(document_id, tab_id, _tab_end(first), "\n".join(lines))
    document_url = f"https://docs.google.com/document/d/{document_id}/edit"
    with _conn() as connection:
        connection.execute(
            """UPDATE batches SET links_document_id=?, links_document_url=?, updated_at=?
               WHERE owner=? AND batch_id=?""",
            (document_id, document_url, _now(), owner, batch_id),
        )
        connection.commit()
    return document_id, document_url


def _title_from_page(page: Any, video_id: str) -> str:
    try:
        title = str(page.title() or "").strip()
        if title and title.lower() not in {"youtube", "youtube studio"}:
            return re.sub(r"\s+-\s+YouTube$", "", title, flags=re.I).strip()
    except Exception:
        pass
    return video_id


def _connect_browser():
    global _playwright, _browser, _browser_context
    from playwright.sync_api import sync_playwright

    if _browser is None or not _browser.is_connected():
        _playwright = _playwright or sync_playwright().start()
        _browser = _playwright.chromium.connect_over_cdp(CDP_URL, timeout=15000)
        _browser_context = _browser.contexts[0] if _browser.contexts else None
        if _browser_context is None:
            raise RuntimeError("Dedicated browser context is unavailable")
    return _browser_context


def _browser_page_for(url: str):
    global _browser_page
    with _browser_lock:
        context = _connect_browser()
        if _browser_page is None or _browser_page.is_closed():
            _browser_page = context.new_page()
        _browser_state.update(
            {
                "state": "extracting",
                "current_url": url,
                "updated_at": _now(),
                "last_error": "",
            }
        )
        _browser_page.goto(url, wait_until="domcontentloaded", timeout=90000)
        _browser_page.wait_for_timeout(1800)
        _browser_state.update(
            {
                "last_title": _title_from_page(_browser_page, _video_id(url)),
                "updated_at": _now(),
            }
        )
        return _browser_page


def _browser_transcript(page: Any) -> str:
    """Open YouTube's visible transcript panel and read its rendered DOM text."""
    result = page.evaluate(
        """async () => {
          const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
          const visible = node => {
            if (!node) return false;
            const rect = node.getBoundingClientRect();
            const style = getComputedStyle(node);
            return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
          };
          const clean = text => String(text || '')
            .replace(/(^|\\s)\\d{1,2}:\\d{2}(?::\\d{2})?(?=\\s|$)/g, ' ')
            .replace(/\\b\\d{1,2}:\\d{2}(?::\\d{2})?\\b/g, ' ')
            .replace(/Search transcript/gi, ' ')
            .replace(/Follow along using the transcript/gi, ' ')
            .replace(/Show transcript/gi, ' ')
            .replace(/Show less/gi, ' ')
            .replace(/\\.\\.\\.more/gi, ' ')
            .replace(/\\b\\d+\\s+minutes?,\\s*\\d+\\s+seconds?\\b/gi, ' ')
            .replace(/\\b\\d+\\s+(?:seconds?|minutes?)\\b/gi, ' ')
            .replace(/\\s+/g, ' ')
            .trim();
          const buttons = () => Array.from(document.querySelectorAll(
            'button, tp-yt-paper-button, yt-button-shape button'
          ));
          const clickNode = node => {
            if (!node) return false;
            if (visible(node)) node.scrollIntoView({block: 'center', inline: 'nearest'});
            node.click();
            return true;
          };
          const findTranscriptButton = () => {
            const dedicated = Array.from(document.querySelectorAll(
              'ytd-video-description-transcript-section-renderer button, ytd-video-description-transcript-section-renderer tp-yt-paper-button'
            ));
            const candidates = buttons().filter(node => {
              const label = `${node.innerText || ''} ${node.getAttribute('aria-label') || ''}`.toLowerCase();
              return label.includes('transcript') && !label.includes('close transcript');
            });
            return dedicated.find(visible) || candidates.find(visible) || dedicated[0] || candidates[0];
          };
          const segmentText = () => {
            const segments = Array.from(document.querySelectorAll('ytd-transcript-segment-renderer'));
            return clean(segments.map(segment => {
              const lines = String(segment.innerText || segment.textContent || '').split('\\n');
              return lines.filter(line => !/^\\s*\\d{1,2}:\\d{2}(?::\\d{2})?\\s*$/.test(line)).join(' ');
            }).join(' '));
          };
          const panelText = () => {
            const panels = Array.from(document.querySelectorAll('ytd-engagement-panel-section-list-renderer'));
            const candidates = panels.map(panel => {
              const raw = panel.innerText || '';
              const count = panel.querySelectorAll('ytd-transcript-segment-renderer').length;
              const header = /Transcript|Search transcript/i.test(raw);
              const score = count * 10000 + (visible(panel) ? 1000 : 0) + (header ? 500 : 0) + Math.min(raw.length, 2000);
              return {raw, count, header, score};
            }).filter(item => item.count > 0 || item.header);
            candidates.sort((left, right) => right.score - left.score);
            return clean(candidates[0]?.raw || '');
          };

          const moreCandidates = buttons().filter(node => {
              const text = String(node.innerText || '').trim();
              const label = String(node.getAttribute('aria-label') || '').toLowerCase();
              return /^(?:\\.{3})?more$/i.test(text) || label === 'more';
          });
          const more = moreCandidates.find(visible) || moreCandidates[0];
          if (clickNode(more)) await sleep(1200);
          const transcriptButton = findTranscriptButton();
          const alreadyOpen = document.querySelectorAll('ytd-transcript-segment-renderer').length > 0;
          if (!alreadyOpen && !clickNode(transcriptButton)) {
            return {ok: false, error: 'YouTube did not show a transcript button for this video'};
          }
          if (!alreadyOpen) await sleep(2500);
          const deadline = Date.now() + 30000;
          let best = '';
          while (Date.now() < deadline) {
            const candidate = segmentText() || panelText();
            if (candidate.length > best.length) best = candidate;
            if (best.length >= 100) return {ok: true, text: best};
            await sleep(750);
          }
          return best.length >= 10
            ? {ok: true, text: best}
            : {ok: false, error: 'Transcript panel opened, but visible text could not be extracted'};
        }"""
    ) or {}
    if not result.get("ok"):
        raise RuntimeError(str(result.get("error") or "Visible transcript extraction failed"))
    return str(result.get("text") or "").strip()


def _extract(url: str) -> tuple[str, str]:
    video_id = _video_id(url)
    page = _browser_page_for(url)
    page.bring_to_front()
    title = _title_from_page(page, video_id)
    transcript = _browser_transcript(page)
    if len(transcript.strip()) < 10:
        raise RuntimeError("Transcript was empty")
    return title, transcript


def _refresh_batch_status(owner: str, batch_id: str) -> None:
    with _conn() as connection:
        rows = connection.execute(
            "SELECT status FROM jobs WHERE owner=? AND batch_id=?",
            (owner, batch_id),
        ).fetchall()
        statuses = [str(row["status"] or "") for row in rows]
        if statuses and not any(value in {"pending", "processing"} for value in statuses):
            connection.execute(
                """UPDATE batches SET status='completed',control='completed',
                   completed_at=?,updated_at=? WHERE owner=? AND batch_id=?""",
                (_now(), _now(), owner, batch_id),
            )
        connection.commit()


def _worker() -> None:
    while True:
        try:
            with _conn() as connection:
                row = connection.execute(
                    """SELECT j.*, b.document_id
                       FROM jobs j
                       JOIN batches b
                         ON b.owner=j.owner AND b.batch_id=j.batch_id
                       WHERE j.status='pending' AND b.control='running'
                       ORDER BY j.created_at LIMIT 1"""
                ).fetchone()
                if not row:
                    time.sleep(1)
                    continue
                connection.execute(
                    """UPDATE jobs SET status='processing', updated_at=?
                       WHERE owner=? AND batch_id=? AND permanent_id=?
                         AND status='pending'""",
                    (
                        _now(),
                        row["owner"],
                        row["batch_id"],
                        row["permanent_id"],
                    ),
                )
                connection.commit()
            try:
                title, transcript = _extract(row["url"])
                text = (
                    f"Source title: {title}\nSource URL: {row['url']}\n"
                    f"Video ID: {row['video_id']}\n"
                    f"Permanent ID: {row['permanent_id']}\n"
                    f"Extracted UTC: {_now()}\n\n{transcript}\n\n"
                )
                tab_id = _append_doc(
                    row["owner"], row["document_id"], row["permanent_id"], text
                )
                with _conn() as connection:
                    connection.execute(
                        """UPDATE jobs
                           SET status='completed', source_title=?, doc_tab_id=?,
                               error='', completed_at=?, updated_at=?
                           WHERE owner=? AND batch_id=? AND permanent_id=?""",
                        (
                            title,
                            tab_id,
                            _now(),
                            _now(),
                            row["owner"],
                            row["batch_id"],
                            row["permanent_id"],
                        ),
                    )
                    connection.commit()
                _refresh_batch_status(row["owner"], row["batch_id"])
                _browser_state.update(
                    {"state": "ready", "last_error": "", "updated_at": _now()}
                )
            except Exception as exc:
                message = str(exc).replace("\n", " ")[:1000]
                _browser_state.update(
                    {"state": "error", "last_error": message, "updated_at": _now()}
                )
                with _conn() as connection:
                    connection.execute(
                        """UPDATE jobs
                           SET status='failed', error=?,
                               retry_count=retry_count+1, updated_at=?
                           WHERE owner=? AND batch_id=? AND permanent_id=?""",
                        (
                            message,
                            _now(),
                            row["owner"],
                            row["batch_id"],
                            row["permanent_id"],
                        ),
                    )
                    connection.commit()
                # Phase 2: one failed item must never pause the batch.
                # The worker continues with the next pending item and the batch is
                # completed once every item has reached completed/failed.
                _refresh_batch_status(row["owner"], row["batch_id"])
        except Exception as exc:
            _browser_state.update(
                {"state": "error", "last_error": str(exc)[:1000], "updated_at": _now()}
            )
            time.sleep(2)


def _start_worker() -> None:
    global _worker_started
    with _worker_lock:
        if not _worker_started:
            _init_storage()
            threading.Thread(
                target=_worker, name="script-extractor-worker", daemon=True
            ).start()
            _worker_started = True


def _state(owner: str, group_name: str = "", batch_id: str = "") -> dict[str, Any]:
    with _conn() as connection:
        groups = [
            dict(row)
            for row in connection.execute(
                """SELECT g.*, COUNT(DISTINCT b.batch_id) AS batch_count,
                          COUNT(j.permanent_id) AS link_count
                   FROM groups g
                   LEFT JOIN batches b
                     ON b.owner=g.owner AND b.group_name=g.name
                   LEFT JOIN jobs j
                     ON j.owner=b.owner AND j.batch_id=b.batch_id
                   WHERE g.owner=?
                   GROUP BY g.owner,g.name
                   ORDER BY g.name COLLATE NOCASE""",
                (owner,),
            ).fetchall()
        ]
        selected_group = next(
            (group for group in groups if group["name"] == group_name),
            groups[0] if groups else None,
        )
        batches = (
            [
                dict(row)
                for row in connection.execute(
                    """SELECT b.*, COUNT(j.permanent_id) AS link_count
                       FROM batches b
                       LEFT JOIN jobs j
                         ON j.owner=b.owner AND j.batch_id=b.batch_id
                       WHERE b.owner=? AND b.group_name=?
                       GROUP BY b.owner,b.batch_id
                       ORDER BY b.created_at DESC,b.batch_id DESC""",
                    (owner, selected_group["name"]),
                ).fetchall()
            ]
            if selected_group
            else []
        )
        selected_batch = next(
            (batch for batch in batches if batch["batch_id"] == batch_id),
            batches[0] if batches else None,
        )
        jobs = (
            [
                dict(row)
                for row in connection.execute(
                    """SELECT * FROM jobs
                       WHERE owner=? AND batch_id=? ORDER BY created_at""",
                    (owner, selected_batch["batch_id"]),
                )
            ]
            if selected_batch
            else []
        )
    counts = {key: 0 for key in ("pending", "processing", "completed", "failed")}
    for job in jobs:
        counts[job["status"]] = counts.get(job["status"], 0) + 1
    return {
        "groups": groups,
        "batches": batches,
        "selected_group": selected_group,
        "selected_batch": selected_batch,
        "jobs": jobs,
        "counts": counts,
        "browser": dict(_browser_state),
    }


async def _json(request: Request) -> dict[str, Any]:
    try:
        value = await request.json()
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _no_vnc_path(path: str) -> str:
    value = path.strip("/") or "vnc.html"
    if ".." in value.split("/"):
        raise HTTPException(400, "Invalid browser asset path")
    return value


def register_script_work_api(app: Any, templates: Any = None) -> None:
    if os.getenv("VPS1_RUNTIME_CANDIDATE") == "1":
        _init_storage()  # schema for isolated fresh queue, never drain jobs
    else:
        _start_worker()

    @app.get("/api/script-work/state")
    def api_state(request: Request, group: str = "", batch: str = ""):
        return _state(_owner(request), group, batch)

    @app.post("/api/script-work/groups")
    async def api_add_group(request: Request):
        owner, data = _owner(request), await _json(request)
        name = _safe_group_name(str(data.get("name") or ""))
        if not name:
            raise HTTPException(400, "Use letters, numbers, dots, dashes, or underscores for the Group name")
        with _conn() as connection:
            try:
                connection.execute(
                    "INSERT INTO groups(owner,name,created_at,updated_at) VALUES(?,?,?,?)",
                    (owner, name, _now(), _now()),
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                raise HTTPException(409, "That group already exists") from exc
        return _state(owner, name)

    @app.delete("/api/script-work/groups/{group_name}")
    def api_remove_group(request: Request, group_name: str):
        owner = _owner(request)
        with _conn() as connection:
            group = connection.execute(
                "SELECT 1 FROM groups WHERE owner=? AND name=?",
                (owner, group_name),
            ).fetchone()
            if not group:
                raise HTTPException(404, "Group not found")
            running = connection.execute(
                """SELECT 1 FROM batches WHERE owner=? AND group_name=?
                   AND control='running' LIMIT 1""",
                (owner, group_name),
            ).fetchone()
            if running:
                raise HTTPException(409, "Stop every running Batch before removing this Group")
            connection.execute(
                "DELETE FROM groups WHERE owner=? AND name=?", (owner, group_name)
            )
            connection.commit()
        return _state(owner)

    @app.post("/api/script-work/groups/{group_name}/links")
    async def api_save_links(request: Request, group_name: str):
        owner, data = _owner(request), await _json(request)
        raw = (
            data.get("links")
            if isinstance(data.get("links"), list)
            else str(data.get("text") or "").splitlines()
        )
        links: list[tuple[str, str]] = []
        seen: set[str] = set()
        invalid: list[str] = []
        for value in raw:
            if not str(value).strip():
                continue
            try:
                url, video_id = _normal_url(str(value)), _video_id(str(value))
                if video_id not in seen:
                    seen.add(video_id)
                    links.append((url, video_id))
            except ValueError:
                invalid.append(str(value))
        if not links:
            raise HTTPException(400, "Add at least one valid YouTube link")
        batch_id = _new_batch_id(owner, group_name)
        duplicates: list[str] = []
        with _conn() as connection:
            group = connection.execute(
                "SELECT 1 FROM groups WHERE owner=? AND name=?",
                (owner, group_name),
            ).fetchone()
            if not group:
                raise HTTPException(404, "Group not found")
            existing_video_ids = {
                str(row["video_id"])
                for row in connection.execute(
                    "SELECT video_id FROM jobs WHERE owner=? AND group_name=?",
                    (owner, group_name),
                )
            }
            now = _now()
            connection.execute(
                """INSERT INTO batches(
                     owner,group_name,batch_id,status,control,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                (owner, group_name, batch_id, "ready", "ready", now, now),
            )
            next_index = 1
            for url, video_id in links:
                if video_id in existing_video_ids:
                    duplicates.append(url)
                    continue
                permanent_id = f"{batch_id}-{next_index:03d}"
                next_index += 1
                connection.execute(
                    """INSERT INTO jobs(
                         owner,group_name,batch_id,permanent_id,video_id,url,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        owner,
                        group_name,
                        batch_id,
                        permanent_id,
                        video_id,
                        url,
                        _now(),
                        _now(),
                    ),
                )
            if next_index == 1:
                connection.execute(
                    "DELETE FROM batches WHERE owner=? AND batch_id=?",
                    (owner, batch_id),
                )
                connection.commit()
                raise HTTPException(409, "All links already exist in this Group history")
            connection.execute(
                "UPDATE groups SET updated_at=? WHERE owner=? AND name=?",
                (_now(), owner, group_name),
            )
            connection.commit()
        try:
            _ensure_group_layout(group_name)
            _ensure_extracted_document(owner, batch_id)
            _sync_links_document(owner, batch_id)
        except Exception as exc:
            with _conn() as connection:
                connection.execute(
                    "UPDATE batches SET error=?,updated_at=? WHERE owner=? AND batch_id=?",
                    (str(exc)[:1000], _now(), owner, batch_id),
                )
                connection.commit()
            raise HTTPException(
                502, f"Batch was saved, but its Drive documents could not be prepared: {exc}"
            ) from exc
        return {
            **_state(owner, group_name, batch_id),
            "invalid_links": invalid,
            "duplicate_links": duplicates,
            "saved_links": next_index - 1,
            "created_batch_id": batch_id,
        }

    @app.post("/api/script-work/batches/{batch_id}/document/validate")
    async def api_validate_batch_doc(request: Request, batch_id: str):
        owner, data = _owner(request), await _json(request)
        batch = _batch_row(owner, batch_id)
        document_id = _document_id(
            str(data.get("document") or data.get("document_id") or "")
        )
        try:
            document = _doc_get(owner, document_id)
            layout = _ensure_group_layout(str(batch["group_name"]))
            _move_root_file(document_id, layout["extracted"][0])
        except Exception as exc:
            raise HTTPException(502, f"Google Docs validation failed: {exc}") from exc
        return {
            "valid": True,
            "document_id": document_id,
            "title": document.get("title") or "Untitled document",
            "tabs": [_tab_title(tab) for tab in _tab_data(document)],
        }

    @app.post("/api/script-work/batches/{batch_id}/document")
    async def api_save_batch_doc(request: Request, batch_id: str):
        owner, data = _owner(request), await _json(request)
        batch = _batch_row(owner, batch_id)
        document_id = _document_id(
            str(data.get("document") or data.get("document_id") or "")
        )
        try:
            document = _doc_get(owner, document_id)
            layout = _ensure_group_layout(str(batch["group_name"]))
            _move_root_file(document_id, layout["extracted"][0])
        except Exception as exc:
            raise HTTPException(502, f"Google Docs access check failed: {exc}") from exc
        with _conn() as connection:
            connection.execute(
                """UPDATE batches SET document_id=?,document_title=?,document_url=?,
                   error='',updated_at=? WHERE owner=? AND batch_id=?""",
                (
                    document_id,
                    document.get("title") or batch_id,
                    f"https://docs.google.com/document/d/{document_id}/edit",
                    _now(), owner, batch_id,
                ),
            )
            connection.commit()
        return _state(owner, str(batch["group_name"]), batch_id)

    @app.post("/api/script-work/batches/{batch_id}/document/create")
    async def api_create_batch_doc(request: Request, batch_id: str):
        owner = _owner(request)
        batch = _batch_row(owner, batch_id)
        try:
            _ensure_extracted_document(owner, batch_id)
        except Exception as exc:
            raise HTTPException(502, f"Google Docs creation failed: {exc}") from exc
        return _state(owner, str(batch["group_name"]), batch_id)

    def batch_control(request: Request, batch_id: str, value: str):
        owner = _owner(request)
        batch = _batch_row(owner, batch_id)
        if value == "running" and not str(batch["document_id"] or ""):
            raise HTTPException(409, "Assign and validate a Google Doc before starting")
        status = "running" if value == "running" else str(batch["status"] or "ready")
        with _conn() as connection:
            connection.execute(
                """UPDATE batches SET control=?,status=?,error='',updated_at=?
                   WHERE owner=? AND batch_id=?""",
                (value, status, _now(), owner, batch_id),
            )
            connection.commit()
        return _state(owner, str(batch["group_name"]), batch_id)

    @app.post("/api/script-work/batches/{batch_id}/start")
    def api_batch_start(request: Request, batch_id: str):
        return batch_control(request, batch_id, "running")

    @app.post("/api/script-work/batches/{batch_id}/pause")
    def api_batch_pause(request: Request, batch_id: str):
        return batch_control(request, batch_id, "paused")

    @app.post("/api/script-work/batches/{batch_id}/resume")
    def api_batch_resume(request: Request, batch_id: str):
        return batch_control(request, batch_id, "running")

    @app.post("/api/script-work/batches/{batch_id}/stop")
    def api_batch_stop(request: Request, batch_id: str):
        return batch_control(request, batch_id, "stopped")

    @app.post("/api/script-work/batches/{batch_id}/retry-failed")
    def api_batch_retry(request: Request, batch_id: str):
        owner = _owner(request)
        batch = _batch_row(owner, batch_id)
        with _conn() as connection:
            connection.execute(
                """UPDATE jobs SET status='pending',error='',updated_at=?
                   WHERE owner=? AND batch_id=? AND status='failed'""",
                (_now(), owner, batch_id),
            )
            connection.execute(
                """UPDATE batches SET control='running',status='running',error='',updated_at=?
                   WHERE owner=? AND batch_id=?""",
                (_now(), owner, batch_id),
            )
            connection.commit()
        return _state(owner, str(batch["group_name"]), batch_id)

    @app.post("/api/script-work/groups/{group_name}/document/validate")
    async def api_validate_doc(request: Request, group_name: str):
        owner, data = _owner(request), await _json(request)
        document_id = _document_id(
            str(data.get("document") or data.get("document_id") or "")
        )
        try:
            document = _doc_get(owner, document_id)
            extracted_folder_id, _ = _ensure_drive_folder(EXTRACTED_FOLDER_NAME)
            _move_root_file(document_id, extracted_folder_id)
        except Exception as exc:
            raise HTTPException(
                502, f"Google Docs validation failed: {exc}"
            ) from exc
        return {
            "valid": True,
            "document_id": document_id,
            "title": document.get("title") or "Untitled document",
            "tabs": [_tab_title(tab) for tab in _tab_data(document)],
        }

    @app.post("/api/script-work/groups/{group_name}/document")
    async def api_save_doc(request: Request, group_name: str):
        owner, data = _owner(request), await _json(request)
        document_id = _document_id(
            str(data.get("document") or data.get("document_id") or "")
        )
        try:
            document = _doc_get(owner, document_id)
        except Exception as exc:
            raise HTTPException(
                502, f"Google Docs access check failed: {exc}"
            ) from exc
        with _conn() as connection:
            result = connection.execute(
                """UPDATE groups
                   SET document_id=?, document_title=?, document_url=?, updated_at=?
                   WHERE owner=? AND name=?""",
                (
                    document_id,
                    document.get("title") or "Untitled document",
                    f"https://docs.google.com/document/d/{document_id}/edit",
                    _now(),
                    owner,
                    group_name,
                ),
            )
            if not result.rowcount:
                raise HTTPException(404, "Group not found")
            connection.commit()
        return _state(owner, group_name)

    @app.post("/api/script-work/groups/{group_name}/document/create")
    async def api_create_doc(request: Request, group_name: str):
        owner, data = _owner(request), await _json(request)
        title = str(data.get("title") or f"Script Work - {group_name}").strip()[:200]
        try:
            if not GOOGLE_FOLDER_ID:
                raise RuntimeError("Script Work Google Drive folder is not configured")
            document = _docs_service().documents().create(body={"title": title}).execute()
            document_id = str(document["documentId"])
            extracted_folder_id, _ = _ensure_drive_folder(EXTRACTED_FOLDER_NAME)
            _place_created_file(document_id, extracted_folder_id)
            _share_editor(document_id)
        except Exception as exc:
            raise HTTPException(
                502, f"Google Docs creation failed: {exc}"
            ) from exc
        with _conn() as connection:
            result = connection.execute(
                """UPDATE groups
                   SET document_id=?, document_title=?, document_url=?, updated_at=?
                   WHERE owner=? AND name=?""",
                (
                    document_id,
                    title,
                    f"https://docs.google.com/document/d/{document_id}/edit",
                    _now(),
                    owner,
                    group_name,
                ),
            )
            if not result.rowcount:
                raise HTTPException(404, "Group not found")
            connection.commit()
        return _state(owner, group_name)

    def control(request: Request, group_name: str, value: str):
        owner = _owner(request)
        with _conn() as connection:
            group = connection.execute(
                "SELECT document_id FROM groups WHERE owner=? AND name=?",
                (owner, group_name),
            ).fetchone()
            if not group:
                raise HTTPException(404, "Group not found")
            if value == "running" and not group["document_id"]:
                raise HTTPException(
                    409, "Assign and validate a Google Doc before starting"
                )
            connection.execute(
                "UPDATE groups SET control=?, updated_at=? WHERE owner=? AND name=?",
                (value, _now(), owner, group_name),
            )
            connection.commit()
        return _state(owner, group_name)

    @app.post("/api/script-work/groups/{group_name}/start")
    def api_start(request: Request, group_name: str):
        return control(request, group_name, "running")

    @app.post("/api/script-work/groups/{group_name}/pause")
    def api_pause(request: Request, group_name: str):
        return control(request, group_name, "paused")

    @app.post("/api/script-work/groups/{group_name}/resume")
    def api_resume(request: Request, group_name: str):
        return control(request, group_name, "running")

    @app.post("/api/script-work/groups/{group_name}/stop")
    def api_stop(request: Request, group_name: str):
        return control(request, group_name, "stopped")

    @app.post("/api/script-work/groups/{group_name}/retry-failed")
    def api_retry(request: Request, group_name: str):
        owner = _owner(request)
        with _conn() as connection:
            connection.execute(
                """UPDATE jobs SET status='pending', error='', updated_at=?
                   WHERE owner=? AND group_name=? AND status='failed'""",
                (_now(), owner, group_name),
            )
            connection.execute(
                """UPDATE groups SET control='running', updated_at=?
                   WHERE owner=? AND name=?""",
                (_now(), owner, group_name),
            )
            connection.commit()
        return _state(owner, group_name)

    @app.get("/api/script-work/browser/status")
    def api_browser_status(request: Request):
        try:
            response = requests.get(f"{CDP_URL}/json/version", timeout=2)
            response.raise_for_status()
            if _browser_state.get("state") in {"starting", "stopped"}:
                _browser_state["state"] = "ready"
            _browser_state.update({"updated_at": _now(), "last_error": ""})
        except Exception as exc:
            _browser_state.update(
                {
                    "state": "stopped",
                    "last_error": f"Dedicated browser unavailable: {exc}",
                    "updated_at": _now(),
                }
            )
        return dict(_browser_state)

    @app.post("/api/script-work/browser/refresh")
    def api_browser_refresh(request: Request):
        try:
            with _browser_lock:
                context = _connect_browser()
                pages = [page for page in context.pages if not page.is_closed()]
                page = pages[-1] if pages else context.new_page()
                page.reload(wait_until="domcontentloaded", timeout=45000)
                _browser_state.update(
                    {
                        "state": "ready",
                        "current_url": page.url,
                        "last_title": page.title(),
                        "last_error": "",
                        "updated_at": _now(),
                    }
                )
        except Exception as exc:
            raise HTTPException(503, f"Browser refresh failed: {exc}") from exc
        return dict(_browser_state)

    @app.get("/api/script-work/google/status")
    def api_google_status(request: Request):
        configured = GOOGLE_AUTH_FILE.is_file()
        valid = False
        error = ""
        if configured:
            try:
                _load_creds()
                valid = True
            except Exception as exc:
                error = str(exc)
        return {
            "configured": configured,
            "connected": valid,
            "server_managed": True,
            "error": error,
        }

    @app.get("/script-work/viewer/{viewer_path:path}")
    def script_work_viewer(request: Request, viewer_path: str):
        path = _no_vnc_path(viewer_path)
        if path == "websockify":
            raise HTTPException(426, "WebSocket connection required")
        try:
            upstream = requests.get(
                f"{NOVNC_URL}/{path}",
                params=request.query_params,
                timeout=15,
                stream=True,
            )
        except Exception as exc:
            raise HTTPException(
                503, f"Live Browser service unavailable: {exc}"
            ) from exc
        excluded = {
            "content-encoding",
            "content-length",
            "transfer-encoding",
            "connection",
        }
        headers = {
            key: value
            for key, value in upstream.headers.items()
            if key.lower() not in excluded
        }
        return StreamingResponse(
            upstream.iter_content(64 * 1024),
            status_code=upstream.status_code,
            headers=headers,
            media_type=upstream.headers.get("content-type"),
        )

    @app.websocket("/script-work/viewer/websockify")
    async def script_work_viewer_ws(websocket: WebSocket):
        if not websocket.session.get("username"):
            await websocket.close(code=4401)
            return
        requested_protocols = [
            value.strip()
            for value in str(
                websocket.headers.get("sec-websocket-protocol") or ""
            ).split(",")
            if value.strip()
        ]
        protocol = requested_protocols[0] if requested_protocols else None
        await websocket.accept(subprotocol=protocol)
        import asyncio
        import websockets

        try:
            async with websockets.connect(
                NOVNC_WS,
                max_size=None,
                subprotocols=([protocol] if protocol else None),
            ) as upstream:
                async def client_to_upstream() -> None:
                    while True:
                        message = await websocket.receive()
                        if message.get("type") == "websocket.disconnect":
                            break
                        if message.get("bytes") is not None:
                            await upstream.send(message["bytes"])
                        elif message.get("text") is not None:
                            await upstream.send(message["text"])

                async def upstream_to_client() -> None:
                    async for message in upstream:
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            await websocket.send_text(message)

                tasks = [
                    asyncio.create_task(client_to_upstream()),
                    asyncio.create_task(upstream_to_client()),
                ]
                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                for task in done:
                    task.result()
        except (WebSocketDisconnect, Exception):
            try:
                await websocket.close()
            except Exception:
                pass


_init_storage()

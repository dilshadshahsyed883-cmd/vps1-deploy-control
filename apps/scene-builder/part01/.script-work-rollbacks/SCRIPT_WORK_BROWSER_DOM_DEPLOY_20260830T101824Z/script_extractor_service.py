from __future__ import annotations

import json
import os
import re
import sqlite3
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
                control TEXT NOT NULL DEFAULT 'idle', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(owner, name)
            );
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


def _api_transcript(video_id: str) -> str:
    from youtube_transcript_api import YouTubeTranscriptApi

    api = YouTubeTranscriptApi()
    if hasattr(api, "fetch"):
        rows = list(api.fetch(video_id))
        return " ".join(
            str(
                getattr(row, "text", "")
                or (row.get("text", "") if isinstance(row, dict) else "")
            )
            for row in rows
        ).strip()
    rows = YouTubeTranscriptApi.get_transcript(video_id)
    return " ".join(str(row.get("text", "")) for row in rows).strip()


def _browser_transcript(page: Any) -> str:
    tracks = (
        page.evaluate(
            """() => {
              const raw = window.ytInitialPlayerResponse ||
                window.ytplayer?.config?.args?.player_response;
              const value = typeof raw === 'string' ? JSON.parse(raw) : raw;
              return value?.captions?.playerCaptionsTracklistRenderer?.captionTracks || [];
            }"""
        )
        or []
    )
    if not tracks:
        raise RuntimeError("This video has no accessible caption track")
    track = next(
        (
            item
            for item in tracks
            if str(item.get("languageCode", "")).lower().startswith("en")
        ),
        tracks[0],
    )
    response = page.context.request.get(
        str(track.get("baseUrl") or "") + "&fmt=json3", timeout=45000
    )
    if not response.ok:
        raise RuntimeError(f"Caption request failed ({response.status})")
    pieces: list[str] = []
    for event in response.json().get("events") or []:
        value = (
            "".join(str(segment.get("utf8") or "") for segment in event.get("segs") or [])
            .replace("\n", " ")
            .strip()
        )
        if value:
            pieces.append(value)
    return " ".join(pieces).strip()


def _transcript(video_id: str, page: Any) -> str:
    errors: list[str] = []
    try:
        value = _api_transcript(video_id)
        if value:
            return value
    except Exception as exc:
        errors.append(str(exc))
    try:
        value = _browser_transcript(page)
        if value:
            return value
    except Exception as exc:
        errors.append(str(exc))
    raise RuntimeError("Transcript unavailable: " + " | ".join(errors)[-900:])


def _extract(url: str) -> tuple[str, str]:
    video_id = _video_id(url)
    page = _browser_page_for(url)
    title = _title_from_page(page, video_id)
    transcript = _transcript(video_id, page)
    if len(transcript.strip()) < 10:
        raise RuntimeError("Transcript was empty")
    return title, transcript


def _worker() -> None:
    while True:
        try:
            with _conn() as connection:
                row = connection.execute(
                    """SELECT j.*, g.document_id
                       FROM jobs j
                       JOIN groups g
                         ON g.owner=j.owner AND g.name=j.group_name
                       WHERE j.status='pending' AND g.control='running'
                       ORDER BY j.created_at LIMIT 1"""
                ).fetchone()
                if not row:
                    time.sleep(1)
                    continue
                connection.execute(
                    """UPDATE jobs SET status='processing', updated_at=?
                       WHERE owner=? AND group_name=? AND permanent_id=?
                         AND status='pending'""",
                    (
                        _now(),
                        row["owner"],
                        row["group_name"],
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
                           WHERE owner=? AND group_name=? AND permanent_id=?""",
                        (
                            title,
                            tab_id,
                            _now(),
                            _now(),
                            row["owner"],
                            row["group_name"],
                            row["permanent_id"],
                        ),
                    )
                    connection.commit()
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
                           WHERE owner=? AND group_name=? AND permanent_id=?""",
                        (
                            message,
                            _now(),
                            row["owner"],
                            row["group_name"],
                            row["permanent_id"],
                        ),
                    )
                    connection.commit()
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


def _state(owner: str, group_name: str = "") -> dict[str, Any]:
    with _conn() as connection:
        groups = [
            dict(row)
            for row in connection.execute(
                """SELECT g.*, COUNT(j.permanent_id) AS link_count
                   FROM groups g
                   LEFT JOIN jobs j
                     ON j.owner=g.owner AND j.group_name=g.name
                   WHERE g.owner=?
                   GROUP BY g.owner,g.name
                   ORDER BY g.created_at""",
                (owner,),
            ).fetchall()
        ]
        selected = next(
            (group for group in groups if group["name"] == group_name),
            groups[0] if groups else None,
        )
        jobs = (
            [
                dict(row)
                for row in connection.execute(
                    """SELECT * FROM jobs
                       WHERE owner=? AND group_name=? ORDER BY created_at""",
                    (owner, selected["name"]),
                )
            ]
            if selected
            else []
        )
    counts = {key: 0 for key in ("pending", "processing", "completed", "failed")}
    for job in jobs:
        counts[job["status"]] = counts.get(job["status"], 0) + 1
    return {
        "groups": groups,
        "selected_group": selected,
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
    _start_worker()

    @app.get("/api/script-work/state")
    def api_state(request: Request, group: str = ""):
        return _state(_owner(request), group)

    @app.post("/api/script-work/groups")
    async def api_add_group(request: Request):
        owner, data = _owner(request), await _json(request)
        name = str(data.get("name") or "").strip()[:120]
        if not name:
            raise HTTPException(400, "Group name is required")
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
                "SELECT control FROM groups WHERE owner=? AND name=?",
                (owner, group_name),
            ).fetchone()
            if not group:
                raise HTTPException(404, "Group not found")
            if group["control"] == "running":
                raise HTTPException(409, "Stop this group before removing it")
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
        with _conn() as connection:
            group = connection.execute(
                "SELECT control FROM groups WHERE owner=? AND name=?",
                (owner, group_name),
            ).fetchone()
            if not group:
                raise HTTPException(404, "Group not found")
            if group["control"] == "running":
                raise HTTPException(
                    409, "Pause or stop this group before replacing links"
                )
            completed = {
                row["video_id"]: row
                for row in connection.execute(
                    """SELECT * FROM jobs
                       WHERE owner=? AND group_name=? AND status='completed'""",
                    (owner, group_name),
                )
            }
            connection.execute(
                """DELETE FROM jobs
                   WHERE owner=? AND group_name=? AND status!='completed'""",
                (owner, group_name),
            )
            suffixes = [
                int(str(row["permanent_id"]).rsplit("-", 1)[-1])
                for row in completed.values()
                if str(row["permanent_id"]).rsplit("-", 1)[-1].isdigit()
            ]
            next_index = max(suffixes + [0]) + 1
            for url, video_id in links:
                if video_id in completed:
                    continue
                permanent_id = f"{group_name}-{next_index:03d}"
                next_index += 1
                connection.execute(
                    """INSERT INTO jobs(
                         owner,group_name,permanent_id,video_id,url,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        owner,
                        group_name,
                        permanent_id,
                        video_id,
                        url,
                        _now(),
                        _now(),
                    ),
                )
            connection.execute(
                "UPDATE groups SET updated_at=? WHERE owner=? AND name=?",
                (_now(), owner, group_name),
            )
            connection.commit()
        return {
            **_state(owner, group_name),
            "invalid_links": invalid,
            "saved_links": len(links),
        }

    @app.post("/api/script-work/groups/{group_name}/document/validate")
    async def api_validate_doc(request: Request, group_name: str):
        owner, data = _owner(request), await _json(request)
        document_id = _document_id(
            str(data.get("document") or data.get("document_id") or "")
        )
        try:
            document = _doc_get(owner, document_id)
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
            drive = _drive_service()
            current = drive.files().get(fileId=document_id, fields="parents").execute()
            old_parents = ",".join(current.get("parents") or [])
            move_args = {
                "fileId": document_id,
                "addParents": GOOGLE_FOLDER_ID,
                "fields": "id,parents",
            }
            if old_parents:
                move_args["removeParents"] = old_parents
            drive.files().update(**move_args).execute()
            try:
                (
                    drive
                    .permissions()
                    .create(
                        fileId=document_id,
                        body={"type": "anyone", "role": "writer"},
                        fields="id",
                    )
                    .execute()
                )
            except Exception:
                pass
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

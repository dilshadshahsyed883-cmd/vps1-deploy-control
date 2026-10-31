from __future__ import annotations
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import DEFAULT_SETTINGS, PROJECT_DB, ensure_dirs

ACTIVE_STATES = {"DOWNLOADING", "SOURCE_READY", "UPLOAD_QUEUED", "UPLOADING", "VERIFYING_REMOTE", "CLEANING"}
TERMINAL_STATES = {"COMPLETED", "CANCELLED"}
WAITING_STATES = {"QUEUED", "WAITING_SLOT", "SCHEDULED"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_time(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class ProjectStore:
    def __init__(self, path: str | Path = PROJECT_DB):
        ensure_dirs()
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_username TEXT NOT NULL,
                    ui_mode TEXT NOT NULL DEFAULT 'simple',
                    source_mode TEXT NOT NULL DEFAULT 'drive_link',
                    drive_url TEXT NOT NULL,
                    staging_path TEXT,
                    thumbnail_path TEXT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    tags TEXT NOT NULL DEFAULT '',
                    category_id TEXT NOT NULL DEFAULT '22',
                    privacy_status TEXT NOT NULL DEFAULT 'private',
                    app_upload_at TEXT,
                    made_for_kids INTEGER NOT NULL DEFAULT 0,
                    playlist_id TEXT NOT NULL DEFAULT '',
                    channel_db_id INTEGER NOT NULL,
                    group_name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'QUEUED',
                    progress INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT NOT NULL DEFAULT '',
                    core_video_id INTEGER,
                    youtube_video_id TEXT,
                    youtube_url TEXT,
                    remote_verified_at TEXT,
                    cleanup_verified_at TEXT,
                    source_file_name TEXT,
                    source_file_size INTEGER,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_lum_projects_status ON projects(status,id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_lum_projects_owner ON projects(owner_username,id)")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_lum_project_core_video ON projects(core_video_id) WHERE core_video_id IS NOT NULL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS project_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER,
                    level TEXT NOT NULL DEFAULT 'INFO',
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_lum_events_project ON project_events(project_id,id)")
            conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            for key, value in DEFAULT_SETTINGS.items():
                conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (key, value))

    def setting(self, key: str, default: str = "") -> str:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        with self.connect() as conn:
            conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(key), str(value)))

    def settings(self) -> dict[str, str]:
        with self.connect() as conn:
            return {str(r["key"]): str(r["value"]) for r in conn.execute("SELECT key,value FROM settings ORDER BY key")}

    def create_project(self, data: dict[str, Any]) -> int:
        title = str(data.get("title") or "").strip()
        if not title:
            raise ValueError("Title is required")
        if len(title) > 100:
            raise ValueError("Title must be 100 characters or fewer")
        description = str(data.get("description") or "")
        if len(description) > 5000:
            raise ValueError("Description must be 5000 characters or fewer")
        privacy = str(data.get("privacy_status") or "private").lower().strip()
        if privacy not in {"private", "public", "unlisted"}:
            raise ValueError("Invalid privacy status")
        drive_url = str(data.get("drive_url") or "").strip()
        if not drive_url:
            raise ValueError("Google Drive video link is required")
        app_upload_at = str(data.get("app_upload_at") or "").strip() or None
        scheduled = parse_time(app_upload_at)
        status = "SCHEDULED" if scheduled and scheduled > datetime.now(timezone.utc) else "QUEUED"
        stamp = now_iso()
        values = (
            str(data.get("owner_username") or "").strip(), str(data.get("ui_mode") or "simple"),
            str(data.get("source_mode") or "drive_link"), drive_url, str(data.get("thumbnail_path") or "").strip() or None,
            title, description, str(data.get("tags") or ""), str(data.get("category_id") or "22"), privacy,
            app_upload_at, 1 if data.get("made_for_kids") else 0, str(data.get("playlist_id") or "").strip(),
            int(data.get("channel_db_id") or 0), str(data.get("group_name") or "").strip(), status, stamp, stamp,
        )
        if not values[0]:
            raise ValueError("Project owner is required")
        if values[13] <= 0:
            raise ValueError("Channel is required")
        with self.connect() as conn:
            cur = conn.execute("""
                INSERT INTO projects(owner_username,ui_mode,source_mode,drive_url,thumbnail_path,title,description,tags,
                    category_id,privacy_status,app_upload_at,made_for_kids,playlist_id,channel_db_id,group_name,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, values)
            project_id = int(cur.lastrowid)
            conn.execute("INSERT INTO project_events(project_id,level,message,created_at) VALUES(?,?,?,?)", (project_id,"INFO","Project accepted into VPS queue",stamp))
            return project_id

    def get(self, project_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM projects WHERE id=?", (int(project_id),)).fetchone()
        return dict(row) if row else None

    def list(self, owner: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        query = "SELECT * FROM projects"
        params: list[Any] = []
        if owner:
            query += " WHERE owner_username=? COLLATE NOCASE"
            params.append(owner)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(5000, int(limit))))
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(query, params).fetchall()]

    def update(self, project_id: int, **fields: Any) -> None:
        allowed = {
            "status","progress","error_message","staging_path","thumbnail_path","core_video_id","youtube_video_id","youtube_url",
            "remote_verified_at","cleanup_verified_at","source_file_name","source_file_size","retry_count","app_upload_at"
        }
        safe = {k:v for k,v in fields.items() if k in allowed}
        if not safe:
            return
        safe["updated_at"] = now_iso()
        sql = ",".join(f"{k}=?" for k in safe)
        with self.connect() as conn:
            conn.execute(f"UPDATE projects SET {sql} WHERE id=?", [*safe.values(), int(project_id)])

    def event(self, project_id: int | None, message: str, level: str = "INFO") -> None:
        with self.connect() as conn:
            conn.execute("INSERT INTO project_events(project_id,level,message,created_at) VALUES(?,?,?,?)", (project_id,level,str(message),now_iso()))

    def events(self, owner: str | None = None, limit: int = 300) -> list[dict[str, Any]]:
        query = """
            SELECT e.*, p.owner_username, p.title FROM project_events e
            LEFT JOIN projects p ON p.id=e.project_id
        """
        params: list[Any] = []
        if owner:
            query += " WHERE p.owner_username=? COLLATE NOCASE OR e.project_id IS NULL"
            params.append(owner)
        query += " ORDER BY e.id DESC LIMIT ?"
        params.append(max(1, min(5000, int(limit))))
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(query, params).fetchall()]

    def recover_wrapper_states(self) -> int:
        with self.connect() as conn:
            rows = conn.execute("SELECT id,status FROM projects WHERE status IN ('DOWNLOADING','SOURCE_READY','UPLOAD_QUEUED','UPLOADING','VERIFYING_REMOTE','CLEANING')").fetchall()
            for row in rows:
                conn.execute("UPDATE projects SET status='QUEUED', error_message='Recovered after service restart; state will be reconciled safely.', updated_at=? WHERE id=?", (now_iso(), int(row["id"])))
            return len(rows)

    def next_eligible(self) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc)
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM projects WHERE status IN ('QUEUED','WAITING_SLOT','SCHEDULED','PAUSED') ORDER BY id ASC").fetchall()
        for raw in rows:
            row = dict(raw)
            if row["status"] == "PAUSED":
                continue
            when = parse_time(row.get("app_upload_at"))
            if when and when > now:
                if row["status"] != "SCHEDULED":
                    self.update(int(row["id"]), status="SCHEDULED")
                continue
            return row
        return None

    def mark_other_waiting(self, active_id: int) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE projects SET status='WAITING_SLOT',updated_at=? WHERE id<>? AND status='QUEUED'", (now_iso(), int(active_id)))

    def release_waiting(self) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE projects SET status='QUEUED',updated_at=? WHERE status='WAITING_SLOT'", (now_iso(),))

    def retry(self, project_id: int) -> bool:
        with self.connect() as conn:
            row = conn.execute("SELECT status,retry_count FROM projects WHERE id=?", (int(project_id),)).fetchone()
            if not row or str(row["status"]) not in {"FAILED","PAUSED"}:
                return False
            conn.execute("UPDATE projects SET status='QUEUED',error_message='',retry_count=?,updated_at=? WHERE id=?", (int(row["retry_count"] or 0)+1, now_iso(), int(project_id)))
        self.event(project_id, "Project returned to queue for retry", "WARNING")
        return True

    def counts(self, owner: str | None = None) -> dict[str, int]:
        with self.connect() as conn:
            if owner:
                rows = conn.execute("SELECT status,COUNT(*) AS n FROM projects "
                                    "WHERE owner_username=? COLLATE NOCASE GROUP BY status",
                                    (owner,)).fetchall()
            else:
                rows = conn.execute("SELECT status,COUNT(*) AS n FROM projects GROUP BY status").fetchall()
        out = {str(r["status"]): int(r["n"]) for r in rows}
        out["TOTAL"] = sum(out.values())
        out["ACTIVE"] = sum(out.get(s, 0) for s in ACTIVE_STATES)
        out["WAITING"] = sum(out.get(s, 0) for s in WAITING_STATES)
        return out

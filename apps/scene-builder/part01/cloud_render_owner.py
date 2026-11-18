"""Additive owner isolation foundations for Concat Cloud Render.

Legacy rows are deliberately ownerless and inaccessible until an operator
independently verifies attribution. Nothing is assigned to the logged-in admin.
Only SQLite schema changes are made here; never touch media files or Drive.
"""
from __future__ import annotations

import re
import sqlite3

_OWNER = re.compile(r"^[a-z0-9][a-z0-9._@-]{0,127}$")
_PRODUCTION_COLUMNS = {
    "id", "remote_id", "drive_id", "name", "resource_keys", "drive_size",
    "drive_md5", "folder_id", "drive_path", "state", "error", "digest",
    "endpoint", "submitted_at", "cancel_requested", "rerender",
    "output_sha", "output_size", "owner_username",
}


def owner_name(value: str) -> str:
    name = str(value or "").strip().casefold()
    if not _OWNER.fullmatch(name):
        raise ValueError("Valid authenticated Concat owner required")
    return name


def migrate_files(con: sqlite3.Connection) -> None:
    columns = {r[1] for r in con.execute("PRAGMA table_info(files)")}
    if "owner_username" not in columns:
        con.execute("ALTER TABLE files ADD COLUMN owner_username TEXT NOT NULL DEFAULT ''")
    con.execute("CREATE INDEX IF NOT EXISTS files_owner_created_idx ON files(owner_username,created_at)")
    # Existing ownerless rows intentionally stay quarantined (owner_username='').


def migrate_production(con: sqlite3.Connection) -> None:
    """Upgrade global Drive-ID uniqueness to (owner, Drive ID) without losing rows.

    Refuse unknown schema extensions rather than silently losing them on rebuild.
    The caller's SQLite transaction handles rollback on any error.
    """
    columns = {r[1] for r in con.execute("PRAGMA table_info(production_jobs)")}
    if not columns:
        raise RuntimeError("Production table not initialized")
    unknown = columns - _PRODUCTION_COLUMNS
    if unknown:
        raise RuntimeError("Unknown production columns; migration requires explicit review: " + ",".join(sorted(unknown)))
    if "owner_username" not in columns:
        con.execute("ALTER TABLE production_jobs ADD COLUMN owner_username TEXT NOT NULL DEFAULT ''")
    # Old schema has a single-column UNIQUE(drive_id). New schema does not.
    single_drive_unique = False
    for idx in con.execute("PRAGMA index_list(production_jobs)"):
        if idx[2] and [r[2] for r in con.execute("PRAGMA index_info(" + '"' + idx[1].replace('"', '""') + '"' + ")")] == ["drive_id"]:
            single_drive_unique = True
    if single_drive_unique:
        con.execute("""CREATE TABLE production_jobs_owner_v2(
            id INTEGER PRIMARY KEY AUTOINCREMENT, remote_id TEXT NOT NULL UNIQUE,
            drive_id TEXT NOT NULL, name TEXT NOT NULL, resource_keys TEXT NOT NULL,
            drive_size INTEGER NOT NULL, drive_md5 TEXT NOT NULL,
            folder_id TEXT NOT NULL, drive_path TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'Queued', error TEXT NOT NULL DEFAULT '',
            digest TEXT NOT NULL DEFAULT '', endpoint TEXT NOT NULL DEFAULT '',
            submitted_at INTEGER NOT NULL DEFAULT 0, cancel_requested INTEGER NOT NULL DEFAULT 0,
            rerender INTEGER NOT NULL DEFAULT 0, output_sha TEXT NOT NULL DEFAULT '',
            output_size INTEGER NOT NULL DEFAULT 0,
            owner_username TEXT NOT NULL DEFAULT '',
            UNIQUE(owner_username,drive_id)
        )""")
        ordered = ("id","remote_id","drive_id","name","resource_keys","drive_size",
                   "drive_md5","folder_id","drive_path","state","error","digest",
                   "endpoint","submitted_at","cancel_requested","rerender",
                   "output_sha","output_size","owner_username")
        cols = ",".join(ordered)
        con.execute(f"INSERT INTO production_jobs_owner_v2({cols}) SELECT {cols} FROM production_jobs")
        before = con.execute("SELECT COUNT(*) FROM production_jobs").fetchone()[0]
        after = con.execute("SELECT COUNT(*) FROM production_jobs_owner_v2").fetchone()[0]
        if before != after:
            raise RuntimeError("Production migration row-count verification failed")
        con.execute("DROP TABLE production_jobs")
        con.execute("ALTER TABLE production_jobs_owner_v2 RENAME TO production_jobs")
    con.execute("CREATE INDEX IF NOT EXISTS production_owner_state_idx ON production_jobs(owner_username,state,id)")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS production_owner_drive_idx ON production_jobs(owner_username,drive_id)")
    con.execute("""CREATE TABLE IF NOT EXISTS production_owner_queues(
        owner_username TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'stopped'
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS cloud_owner_config(
        owner_username TEXT NOT NULL, key TEXT NOT NULL, val TEXT NOT NULL,
        PRIMARY KEY(owner_username,key)
    )""")


def get_owner_config(con: sqlite3.Connection, owner: str, key: str) -> str:
    row = con.execute("SELECT val FROM cloud_owner_config WHERE owner_username=? AND key=?",
                      (owner_name(owner), key)).fetchone()
    return row[0] if row else ""


def put_owner_config(con: sqlite3.Connection, owner: str, key: str, val: object) -> None:
    con.execute("""INSERT INTO cloud_owner_config(owner_username,key,val) VALUES(?,?,?)
                   ON CONFLICT(owner_username,key) DO UPDATE SET val=excluded.val""",
                (owner_name(owner), key, str(val)))


def queue_state(con: sqlite3.Connection, owner: str) -> str:
    row = con.execute("SELECT state FROM production_owner_queues WHERE owner_username=?",
                      (owner_name(owner),)).fetchone()
    return row[0] if row else "stopped"


def set_queue_state(con: sqlite3.Connection, owner: str, state: str) -> None:
    if state not in {"running", "paused", "stopped"}:
        raise ValueError("Invalid queue state")
    con.execute("""INSERT INTO production_owner_queues(owner_username,state) VALUES(?,?)
                   ON CONFLICT(owner_username) DO UPDATE SET state=excluded.state""",
                (owner_name(owner), state))

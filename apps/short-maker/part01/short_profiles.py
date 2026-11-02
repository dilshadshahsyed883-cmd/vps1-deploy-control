"""Persistent, owner-scoped Short Maker profile records (staged, no scheduler).

This module deliberately does not claim PC media has been imported, dispatch
render jobs, or touch legacy Short Maker jobs. A verified source-import receipt
is mandatory before a profile can be saved. Daily generation is wired only
after its separate media/renderer/recovery acceptance gates are complete.
"""
from __future__ import annotations

import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import HTTPException, Request

PKT = ZoneInfo("Asia/Karachi")


def _now():
    return datetime.now(timezone.utc).isoformat()


class ShortProfileStore:
    def __init__(self, data_root: Path):
        data_root = Path(data_root)
        data_root.mkdir(parents=True, exist_ok=True)
        self.path = data_root / "short_profiles.sqlite3"
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS short_profiles (
                    id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    name TEXT NOT NULL,
                    name_key TEXT NOT NULL,
                    source_import_id TEXT NOT NULL,
                    output_folder TEXT NOT NULL,
                    videos_per_day INTEGER NOT NULL,
                    schedule_hhmm TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS unique_active_short_profile_name
                    ON short_profiles(owner, name_key) WHERE deleted_at IS NULL;
                CREATE TABLE IF NOT EXISTS short_profile_runs (
                    id TEXT PRIMARY KEY,
                    profile_id TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    pkt_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    target INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(profile_id, pkt_date)
                );
                CREATE INDEX IF NOT EXISTS short_profile_runs_owner
                    ON short_profile_runs(owner, profile_id, pkt_date);
            """)

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=20000")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _owned(self, db, profile_id, owner, include_deleted=False):
        if not re.fullmatch(r"[0-9a-f]{32}", profile_id):
            raise HTTPException(404, detail="Short Profile not found")
        suffix = "" if include_deleted else " AND deleted_at IS NULL"
        row = db.execute(
            "SELECT * FROM short_profiles WHERE id=? AND owner=?" + suffix,
            (profile_id, owner),
        ).fetchone()
        if row is None:
            raise HTTPException(404, detail="Short Profile not found")
        return row

    @staticmethod
    def _validated(data, verify_source, owned_folder, owner):
        if not isinstance(data, dict):
            raise HTTPException(400, detail="Invalid Short Profile input")
        name = str(data.get("name") or "").strip()
        if (not name or len(name) > 120 or
                any(ord(c) < 32 or ord(c) == 127 for c in name)):
            raise HTTPException(400, detail="Profile name must be 1-120 printable characters")
        receipt = str(data.get("source_import_id") or "")
        if not re.fullmatch(r"[0-9a-f]{32}", receipt):
            raise HTTPException(400, detail="A verified VPS source import is required")
        inventory = verify_source(owner, receipt)
        if (not isinstance(inventory, dict) or
                int(inventory.get("audio_count", 0)) < 1 or
                int(inventory.get("image_count", 0)) < 1):
            raise HTTPException(409, detail="Verified voice and picture media must be imported first")
        folder = str(data.get("output_folder") or "")
        owned_folder(folder)
        count = data.get("videos_per_day")
        if type(count) is not int or count < 1:
            raise HTTPException(400, detail="Videos per day must be positive")
        schedule = str(data.get("schedule_hhmm") or "00:00")
        if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", schedule):
            raise HTTPException(400, detail="Schedule must be HH:MM Pakistan time")
        return (name, name.casefold(), receipt, folder, count, schedule)

    def create(self, owner, data, verify_source, owned_folder):
        values = self._validated(data, verify_source, owned_folder, owner)
        pid = uuid.uuid4().hex
        now = _now()
        with self._connect() as db:
            try:
                db.execute(
                    """INSERT INTO short_profiles
                    (id,owner,name,name_key,source_import_id,output_folder,
                     videos_per_day,schedule_hhmm,enabled,created_at,updated_at)
                     VALUES(?,?,?,?,?,?,?,?,0,?,?)""",
                    (pid, owner, *values, now, now),
                )
            except sqlite3.IntegrityError:
                raise HTTPException(409, detail="Profile name already exists for this user")
        return self.get(owner, pid)

    def get(self, owner, profile_id, include_deleted=False):
        with self._connect() as db:
            return dict(self._owned(db, profile_id, owner, include_deleted))

    def list(self, owner):
        with self._connect() as db:
            return [dict(row) for row in db.execute(
                """SELECT * FROM short_profiles WHERE owner=? AND deleted_at IS NULL
                   ORDER BY created_at DESC""", (owner,),
            )]

    def update(self, owner, profile_id, data, verify_source, owned_folder):
        # GET first to prevent validation callbacks disclosing another owner's data.
        old = self.get(owner, profile_id)
        if not isinstance(data, dict):
            raise HTTPException(400, detail="Invalid Short Profile input")
        updated = {key: data.get(key, old[key]) for key in
                   ("name", "source_import_id", "output_folder", "videos_per_day", "schedule_hhmm")}
        values = self._validated(updated, verify_source, owned_folder, owner)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._owned(db, profile_id, owner)
            try:
                db.execute(
                    """UPDATE short_profiles SET name=?,name_key=?,source_import_id=?,
                       output_folder=?,videos_per_day=?,schedule_hhmm=?,updated_at=?
                       WHERE id=? AND owner=? AND deleted_at IS NULL""",
                    (*values, _now(), profile_id, owner),
                )
            except sqlite3.IntegrityError:
                raise HTTPException(409, detail="Profile name already exists for this user")
        return self.get(owner, profile_id)

    def enable(self, owner, profile_id):
        # A profile can be enabled only by its owner. A separate guarded
        # scheduler activation gate decides whether Start is available.
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._owned(db, profile_id, owner)
            db.execute(
                "UPDATE short_profiles SET enabled=1,updated_at=? WHERE id=? AND owner=?",
                (_now(), profile_id, owner),
            )
        return self.get(owner, profile_id)

    def stop(self, owner, profile_id):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._owned(db, profile_id, owner)
            db.execute(
                "UPDATE short_profiles SET enabled=0,updated_at=? WHERE id=? AND owner=?",
                (_now(), profile_id, owner),
            )
        return self.get(owner, profile_id)

    def delete(self, owner, profile_id, confirm_name):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._owned(db, profile_id, owner)
            if confirm_name != row["name"]:
                raise HTTPException(409, detail="Confirm the exact profile name before deletion")
            # Tombstone preserves history, never touches source media or videos.
            db.execute(
                """UPDATE short_profiles SET enabled=0,deleted_at=?,updated_at=?
                   WHERE id=? AND owner=?""",
                (_now(), _now(), profile_id, owner),
            )
        return {"ok": True, "profile_id": profile_id, "media_preserved": True}

    def history(self, owner, profile_id):
        with self._connect() as db:
            self._owned(db, profile_id, owner, include_deleted=True)
            return [dict(row) for row in db.execute(
                """SELECT * FROM short_profile_runs
                   WHERE owner=? AND profile_id=? ORDER BY pkt_date DESC""",
                (owner, profile_id),
            )]

    def reserve_daily_record(self, owner, profile_id, when=None):
        """Idempotent date-key reservation only. Does NOT dispatch rendering.

        Call only from the future verified dedicated Short Maker scheduler after
        source-media availability and the renderer handoff have been established.
        """
        now = when or datetime.now(timezone.utc)
        day = now.astimezone(PKT).date().isoformat()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._owned(db, profile_id, owner)
            if not row["enabled"]:
                raise HTTPException(409, detail="Profile schedule is stopped")
            cur = db.execute(
                """INSERT OR IGNORE INTO short_profile_runs
                   (id,profile_id,owner,pkt_date,status,target,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (uuid.uuid4().hex, profile_id, owner, day, "reserved",
                 row["videos_per_day"], _now()),
            )
            record = db.execute(
                "SELECT * FROM short_profile_runs WHERE profile_id=? AND pkt_date=?",
                (profile_id, day),
            ).fetchone()
            return {"new": bool(cur.rowcount), "run": dict(record)}


def register_short_profile_store(app, data_root, short_identity, short_folder_owned,
                                 verify_source_import=None, start_callback=None, existing_store=None):
    """Register authenticated record routes, with fail-closed source verification."""
    store = existing_store if existing_store is not None else ShortProfileStore(Path(data_root))
    def owner(request):
        return short_identity(request)[0].casefold()
    def verified(who, receipt):
        return verify_source_import(who, receipt) if verify_source_import else None
    def own_folder(name, request):
        return short_folder_owned(name, request)

    @app.get("/api/short-profiles")
    def list_profiles(request: Request):
        return {"profiles": store.list(owner(request)),
                "scheduler_active": start_callback is not None}

    @app.post("/api/short-profiles")
    async def create_profile(request: Request):
        who = owner(request)
        data = await request.json()
        return store.create(who, data, verified, lambda name: own_folder(name, request))

    @app.patch("/api/short-profiles/{profile_id}")
    async def update_profile(profile_id: str, request: Request):
        who = owner(request)
        data = await request.json()
        return store.update(who, profile_id, data, verified,
                            lambda name: own_folder(name, request))

    @app.post("/api/short-profiles/{profile_id}/stop")
    def stop_profile(profile_id: str, request: Request):
        return store.stop(owner(request), profile_id)

    @app.post("/api/short-profiles/{profile_id}/start")
    def start_profile(profile_id: str, request: Request):
        who = owner(request)
        store.get(who, profile_id)
        if start_callback is None:
            raise HTTPException(503, detail="Short Profiles scheduler not yet activated")
        return start_callback(who, profile_id)

    @app.delete("/api/short-profiles/{profile_id}")
    async def delete_profile(profile_id: str, request: Request):
        who = owner(request)
        data = await request.json()
        return store.delete(who, profile_id, str(data.get("confirm_name") or ""))

    @app.get("/api/short-profiles/{profile_id}/history")
    def profile_history(profile_id: str, request: Request):
        return {"history": store.history(owner(request), profile_id)}

    return store

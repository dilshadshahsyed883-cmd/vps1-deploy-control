"""Staged Short Studio folder retirement with durable undo journal.

This module is NEVER registered as a live deletion route by default. Before
release it requires an independently verified, cooperating cross-service
lease covering Short Maker producers, Short Uploader scans, queue claims and
post-upload cleanup. An ordinary filesystem lock is NOT that lease.

Only owner-owned, SHA256-receipted MP4s may be retired. Source imports, Google
Drive originals, upload history, media metadata and schedule definitions are
preserved; linked owner profiles lose the deleted folder and are disabled.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import sqlite3
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

from fastapi import HTTPException

ALLOWED_DETACH_ISSUES = frozenset((
    "Short Profiles still link this folder",
    "Short Uploader profiles still link folder",
    "Short Uploader still indexes source MP4s",
))
FINAL = frozenset(("complete", "recovered", "aborted"))


def _sync_json(path, payload):
    temp = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        fd = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temp.unlink(missing_ok=True)


class ShortFolderRetirement:
    def __init__(self, *, root, journal_root, profile_store, inspector,
                 lease_factory=None):
        self.root = Path(root)
        self.journal_root = Path(journal_root)
        self.profiles = profile_store
        self.inspector = inspector
        # Never supply from untrusted HTTP. Only a deployment-verified
        # coordinator with both apps' worker/producer guarantees may set it.
        self.lease_factory = lease_factory

    @contextmanager
    def _exclusive(self):
        if self.lease_factory is None:
            raise HTTPException(503, detail="Cross-service retirement lock has not been verified")
        if self.root.is_symlink() or not self.root.is_dir():
            raise HTTPException(409, detail="Unsafe Short Studio folder root")
        if self.journal_root.is_symlink():
            raise HTTPException(409, detail="Unsafe retirement journal root")
        self.journal_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = self.journal_root / ".retirement.lock"
        if lock_path.is_symlink():
            raise HTTPException(409, detail="Unsafe retirement lock file")
        with self.lease_factory():  # MUST span all Short Maker/uploader writers
            with lock_path.open("a+b") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _database(filename):
        target = Path(filename)
        if not filename or not target.is_absolute() or target.is_symlink() or not target.is_file():
            raise HTTPException(503, detail="Uploader dependency database not verified")
        db = sqlite3.connect(str(target), timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        return db

    @staticmethod
    def _owner(owner):
        owner = str(owner or "").strip().casefold()
        if not re.fullmatch(r"[a-z0-9_.@+-]{1,160}", owner):
            raise HTTPException(403, detail="Valid verified account required")
        return owner

    def _folder(self, folder_name):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,63}", folder_name or ""):
            raise HTTPException(400, detail="Invalid owned folder name")
        folder = self.root / folder_name
        if folder.is_symlink() or not folder.is_dir():
            raise HTTPException(404, detail="Owner folder unavailable")
        return folder

    @staticmethod
    def _verify_entries(folder, owner, receipts, digest):
        marker = folder / ".studio_owner"
        if (marker.is_symlink() or not marker.is_file()
                or marker.read_text(encoding="utf-8").strip().casefold() != owner):
            raise HTTPException(403, detail="Folder owner marker mismatch")
        discovered = {}
        for entry in folder.iterdir():
            if entry.name == ".studio_owner":
                continue
            if (entry.is_symlink() or not entry.is_file()
                    or not entry.name.lower().endswith(".mp4")
                    or entry.name not in receipts):
                raise HTTPException(409, detail="Unverified files block folder retirement")
            actual = digest(entry)
            if actual != receipts[entry.name]:
                raise HTTPException(409, detail="Source media receipt no longer matches")
            discovered[entry.name] = actual
        if discovered != receipts:
            raise HTTPException(409, detail="Verified media inventory changed")
        return discovered

    def _snapshot(self, owner, folder, maker, uploader, engine):
        maker_rows = [dict(r) for r in maker.execute(
            """SELECT id,owner,enabled,output_folder FROM short_profiles
               WHERE output_folder=? AND deleted_at IS NULL""", (folder,))]
        if any(row["owner"].casefold() != owner for row in maker_rows):
            raise HTTPException(409, detail="Foreign Short Profile reference")
        sources = [dict(r) for r in engine.execute(
            "SELECT profile_id,short_folder_name FROM profile_sources WHERE short_folder_name=?",
            (folder,))]
        ownership = {int(r["entity_id"]): str(r["owner_username"]).casefold()
                     for r in uploader.execute(
                         """SELECT entity_id,owner_username FROM studio_entity_owners
                            WHERE entity_kind='profile'""")}
        app_profiles = []
        for row in sources:
            pid = int(row["profile_id"])
            if ownership.get(pid) != owner:
                raise HTTPException(409, detail="Unassigned or foreign uploader reference")
            record = uploader.execute(
                "SELECT id,enabled FROM auto_engine_profiles WHERE id=?", (pid,)).fetchone()
            if not record:
                raise HTTPException(409, detail="Dangling Short Uploader profile")
            app_profiles.append(dict(record))
        source_id = "shortstudio-local:" + folder
        index_rows = [dict(r) for r in engine.execute(
            "SELECT id,present FROM drive_videos WHERE source_folder_id=?",
            (source_id,))]
        nonterminal = engine.execute(
            """SELECT COUNT(*) FROM job_sources
               WHERE source_folder_id=?
               AND UPPER(state) NOT IN ('COMPLETE','CANCELLED')""",
            (source_id,)).fetchone()[0]
        if nonterminal:
            raise HTTPException(409, detail="Short Uploader queue has unresolved source claims")
        return {"maker_profiles": maker_rows, "uploader_profiles": app_profiles,
                "uploader_sources": sources, "indexed": index_rows}

    @staticmethod
    def _detach(owner, folder, snapshots, maker, uploader, engine):
        # Keep profile names, media receipts, schedules and history rows.
        maker.execute(
            """UPDATE short_profiles SET enabled=0,output_folder='',
               updated_at=CURRENT_TIMESTAMP
               WHERE owner=? AND output_folder=? AND deleted_at IS NULL""",
            (owner, folder))
        uploader_ids = [int(row["id"]) for row in snapshots["uploader_profiles"]]
        for pid in uploader_ids:
            uploader.execute("UPDATE auto_engine_profiles SET enabled=0 WHERE id=?", (pid,))
            engine.execute(
                """UPDATE profile_sources SET short_folder_name=''
                   WHERE profile_id=? AND short_folder_name=?""",
                (pid, folder))
        # Cached source rows are never deleted; only availability is revoked.
        engine.execute(
            "UPDATE drive_videos SET present=0 WHERE source_folder_id=?",
            ("shortstudio-local:" + folder,))
        # Safeguard against a late-added link during the snapshot (should be
        # excluded by the future verified cross-service lease).
        remaining = engine.execute(
            "SELECT COUNT(*) FROM profile_sources WHERE short_folder_name=?", (folder,)).fetchone()[0]
        if remaining:
            raise HTTPException(409, detail="New uploader profile linkage appeared")

    @staticmethod
    def _restore_db(journal, maker, uploader, engine):
        for row in journal["snapshots"]["maker_profiles"]:
            maker.execute(
                "UPDATE short_profiles SET enabled=?,output_folder=? WHERE id=? AND owner=?",
                (row["enabled"], row["output_folder"], row["id"], row["owner"]))
        for row in journal["snapshots"]["uploader_profiles"]:
            uploader.execute("UPDATE auto_engine_profiles SET enabled=? WHERE id=?",
                             (row["enabled"], row["id"]))
        for row in journal["snapshots"]["uploader_sources"]:
            engine.execute("UPDATE profile_sources SET short_folder_name=? WHERE profile_id=?",
                           (row["short_folder_name"], row["profile_id"]))
        for row in journal["snapshots"]["indexed"]:
            engine.execute("UPDATE drive_videos SET present=? WHERE id=?",
                           (row["present"], row["id"]))

    @staticmethod
    def _commit_all(databases):
        # SQLite WAL multi-file commits aren't distributed-atomic: the journal
        # remains the recovery source of truth across these distinct DBs.
        for db in databases:
            db.commit()

    @staticmethod
    def _rollback_open(databases):
        for db in databases:
            try:
                db.rollback()
            except sqlite3.Error:
                pass

    def _connections(self):
        maker = self.profiles._connect()
        uploader = self._database(self.inspector.uploader_app_db)
        engine = self._database(self.inspector.uploader_engine_db)
        return maker, uploader, engine

    def _recover_incomplete(self, journal):
        maker, uploader, engine = self._connections()
        databases = (maker, uploader, engine)
        try:
            for db in databases:
                db.execute("BEGIN IMMEDIATE")
            self._restore_db(journal, maker, uploader, engine)
            self._commit_all(databases)
            location = self.root / journal["folder"]
            quarantine = self.root / journal["quarantine"]
            if quarantine.is_dir() and not quarantine.is_symlink():
                if location.exists() or location.is_symlink():
                    raise HTTPException(409, detail="Cannot restore: folder path is occupied")
                quarantine.rename(location)
            journal["phase"] = "recovered"
        finally:
            self._rollback_open(databases)
            for db in databases:
                db.close()

    def _gc_committed(self, journal, digest):
        quarantine = self.root / journal["quarantine"]
        if quarantine.is_symlink():
            raise HTTPException(409, detail="Retirement quarantine symlink is forbidden")
        if quarantine.is_dir():
            self._verify_entries(quarantine, journal["owner"], journal["media"], digest)
            for filename in sorted(journal["media"]):
                (quarantine / filename).unlink()
            (quarantine / ".studio_owner").unlink()
            quarantine.rmdir()
        journal["phase"] = "complete"

    def recover(self, digest):
        """Explicit guarded recovery; does not run unattended without lease."""
        outcome = []
        with self._exclusive():
            for path in sorted(self.journal_root.glob("*.json")):
                if path.is_symlink():
                    raise HTTPException(409, detail="Unsafe journal path")
                journal = json.loads(path.read_text(encoding="utf-8"))
                if journal["phase"] in FINAL:
                    continue
                if journal["phase"] == "committed":
                    self._gc_committed(journal, digest)
                else:
                    self._recover_incomplete(journal)
                _sync_json(path, journal)
                outcome.append({"id": journal["id"], "phase": journal["phase"]})
        return outcome

    def retire(self, *, owner, folder_name, confirmation, digest,
               preserve_defaults=()):
        """Isolated-only executor; no route or production coordinator is wired."""
        owner = self._owner(owner)
        if confirmation != folder_name:
            raise HTTPException(409, detail="Confirm exact folder name")
        if folder_name in preserve_defaults:
            raise HTTPException(403, detail="Default folder is protected")
        with self._exclusive():
            if any(True for _ in self.journal_root.glob("*.json")
                   if json.loads(_.read_text(encoding="utf-8")).get("phase") not in FINAL):
                raise HTTPException(409, detail="Unrecovered previous retirement; run recovery first")
            folder = self._folder(folder_name)
            maker, uploader, engine = self._connections()
            databases = (maker, uploader, engine)
            journal = None
            path = None
            try:
                for db in databases:
                    db.execute("BEGIN IMMEDIATE")
                report = self.inspector.inspect(
                    owner=owner, folder_name=folder, folder=folder, include_private=True)
                unexpected = set(report["issues"]) - ALLOWED_DETACH_ISSUES
                if unexpected or not report["uploader_dependency_verified"]:
                    raise HTTPException(409, detail="Outstanding folder or uploader dependencies")
                receipts = dict(report["verified_media"])
                self._verify_entries(folder, owner, receipts, digest)
                snap = self._snapshot(owner, folder_name, maker, uploader, engine)
                id_ = uuid.uuid4().hex
                quarantine_name = ".short-retirement-" + id_
                journal = {"id": id_, "phase": "preparing", "owner": owner,
                           "folder": folder_name, "quarantine": quarantine_name,
                           "media": receipts, "snapshots": snap}
                path = self.journal_root / (id_ + ".json")
                _sync_json(path, journal)
                folder.rename(self.root / quarantine_name)
                journal["phase"] = "quarantined"
                _sync_json(path, journal)
                self._detach(owner, folder_name, snap, maker, uploader, engine)
                # Crash between any DB commits must replay the saved snapshot.
                self._commit_all(databases)
                journal["phase"] = "committed"
                _sync_json(path, journal)
            except Exception:
                self._rollback_open(databases)
                if journal is not None and journal["phase"] != "committed":
                    try:
                        self._recover_incomplete(journal)
                        _sync_json(path, journal)
                    except Exception:
                        # Never garbage-collect a quarantined folder on an
                        # uncertain partial database state.
                        raise HTTPException(503, detail="Retirement requires guarded recovery")
                raise
            finally:
                self._rollback_open(databases)
                for db in databases:
                    db.close()
            self._gc_committed(journal, digest)
            _sync_json(path, journal)
            return {"ok": True, "folder": folder_name,
                    "videos_deleted": len(journal["media"]),
                    "linked_profiles_detached": len(journal["snapshots"]["maker_profiles"])
                        + len(journal["snapshots"]["uploader_profiles"]),
                    "history_preserved": True}

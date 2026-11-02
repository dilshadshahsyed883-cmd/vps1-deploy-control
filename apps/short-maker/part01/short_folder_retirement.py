"""Approved Short Studio unused-only folder retirement.

NEVER detach Maker/Uploader profiles or mutate database rows: a reference of
any kind is a blocker. A caller may supply an exclusive, VERIFIED cross-service
lease only after all Maker and Short Uploader processes participate. Otherwise
this module refuses to act. No live HTTP route is enabled by default.

Successful operations affect only a SHA256-receipted owner VPS folder. The
journal and quarantine are same-filesystem crash/restart recovery evidence.
Google Drive originals and job/profile/upload history are never modified.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import uuid
from contextlib import contextmanager
from pathlib import Path

from fastapi import HTTPException

FINAL = frozenset(("complete", "recovered", "aborted"))
VALID_PHASES = frozenset(("preparing", "quarantined", "committed", *FINAL))
HASH = re.compile(r"[0-9a-f]{64}\Z")
FOLDER = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,63}\Z")
OWNER = re.compile(r"[a-z0-9_.@+-]{1,160}\Z")
JOURNAL_ID = re.compile(r"[0-9a-f]{32}\Z")


def _fsync_dir(directory):
    descriptor = os.open(str(directory), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_json(path, payload):
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as output:
            json.dump(payload, output, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _fsync_dir(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


class ShortFolderRetirement:
    def __init__(self, *, root, journal_root, profile_store, inspector,
                 lease_factory=None):
        self.root = Path(root)
        self.journal_root = Path(journal_root)
        # Keep constructor compatibility, but NEVER write either database.
        self.profiles = profile_store
        self.inspector = inspector
        self.lease_factory = lease_factory

    @contextmanager
    def _exclusive(self):
        if self.lease_factory is None:
            raise HTTPException(503, detail="Cross-service retirement lock has not been verified")
        if self.root.is_symlink() or not self.root.is_dir():
            raise HTTPException(409, detail="Unsafe Short Studio folder root")
        if self.journal_root.is_symlink():
            raise HTTPException(409, detail="Unsafe retirement journal root")
        # Production must pass an exclusive cooperating lock, not a mere local
        # lock. A second local lock serializes journal recovery and retirement.
        with self.lease_factory():
            self.journal_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            lock_file = self.journal_root / ".retirement.lock"
            if lock_file.is_symlink():
                raise HTTPException(409, detail="Unsafe retirement lock file")
            descriptor = os.open(str(lock_file), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    @staticmethod
    def _owner(value):
        owner = str(value or "").strip().casefold()
        if not OWNER.fullmatch(owner):
            raise HTTPException(403, detail="Valid verified account required")
        return owner

    def _folder(self, name):
        if not isinstance(name, str) or not FOLDER.fullmatch(name):
            raise HTTPException(400, detail="Invalid owned folder name")
        folder = self.root / name
        if folder.is_symlink() or not folder.is_dir():
            raise HTTPException(404, detail="Owner folder unavailable")
        return folder

    @staticmethod
    def _verify_entries(folder, owner, receipts, digest):
        marker = folder / ".studio_owner"
        if (marker.is_symlink() or not marker.is_file()
                or marker.read_text(encoding="utf-8").strip().casefold() != owner):
            raise HTTPException(403, detail="Folder owner marker mismatch")
        if not isinstance(receipts, dict):
            raise HTTPException(409, detail="Unverified media receipts")
        for filename, expected in receipts.items():
            if (not isinstance(filename, str)
                    or filename != Path(filename).name
                    or not filename.lower().endswith(".mp4")
                    or not isinstance(expected, str) or not HASH.fullmatch(expected)):
                raise HTTPException(409, detail="Unsafe media receipt")
        actual = {}
        for entry in folder.iterdir():
            if entry.name == ".studio_owner":
                continue
            if (entry.is_symlink() or not entry.is_file()
                    or entry.name not in receipts):
                raise HTTPException(409, detail="Unverified entries block folder deletion")
            result = digest(entry)
            if result != receipts[entry.name]:
                raise HTTPException(409, detail="Verified MP4 changed before deletion")
            actual[entry.name] = result
        if actual != receipts:
            raise HTTPException(409, detail="Verified media inventory changed")
        return actual

    @staticmethod
    def _validate_journal(data, path):
        if not isinstance(data, dict):
            raise HTTPException(409, detail="Invalid retirement journal")
        txn = data.get("id")
        if (not isinstance(txn, str) or not JOURNAL_ID.fullmatch(txn)
                or path.name != txn + ".json"
                or data.get("quarantine") != ".short-retirement-" + txn
                or not isinstance(data.get("folder"), str)
                or not FOLDER.fullmatch(data["folder"])
                or not isinstance(data.get("owner"), str)
                or not OWNER.fullmatch(data["owner"])
                or data.get("phase") not in VALID_PHASES):
            raise HTTPException(409, detail="Untrusted retirement journal")
        receipts = data.get("media")
        if not isinstance(receipts, dict) or any(
                not isinstance(name, str) or Path(name).name != name
                or not name.lower().endswith(".mp4")
                or not isinstance(value, str) or not HASH.fullmatch(value)
                for name, value in receipts.items()):
            raise HTTPException(409, detail="Invalid retirement media manifest")
        return data

    def _pending(self):
        for path in sorted(self.journal_root.glob("*.json")):
            if path.is_symlink() or not path.is_file():
                raise HTTPException(409, detail="Unsafe retirement journal path")
            journal = self._validate_journal(json.loads(path.read_text(encoding="utf-8")), path)
            if journal["phase"] not in FINAL:
                raise HTTPException(409, detail="Recover unfinished retirement before deleting")
        
    def _restore_uncommitted(self, journal):
        original = self.root / journal["folder"]
        quarantine = self.root / journal["quarantine"]
        if quarantine.is_symlink():
            raise HTTPException(409, detail="Unsafe retirement quarantine")
        if quarantine.is_dir():
            if original.exists() or original.is_symlink():
                raise HTTPException(409, detail="Cannot restore: folder name is occupied")
            self._verify_entries(quarantine, journal["owner"], journal["media"],
                                 self._recovery_digest)
            quarantine.rename(original)
            _fsync_dir(self.root)
        elif not original.is_dir() or original.is_symlink():
            raise HTTPException(503, detail="Retirement source is missing; manual recovery needed")
        else:
            # Crash may occur AFTER the journal is durable but BEFORE rename.
            # Never mark that transaction recovered just because the original
            # name exists: verify the exact owner/receipt inventory first.
            self._verify_entries(original, journal["owner"], journal["media"],
                                 self._recovery_digest)
        journal["phase"] = "recovered"

    def _gc_committed(self, journal, digest):
        original = self.root / journal["folder"]
        quarantine = self.root / journal["quarantine"]
        if original.exists() or original.is_symlink():
            raise HTTPException(409, detail="Retired folder name is unexpectedly occupied")
        if quarantine.is_symlink():
            raise HTTPException(409, detail="Unsafe retirement quarantine")
        if quarantine.is_dir():
            marker = quarantine / ".studio_owner"
            if (marker.is_symlink() or not marker.is_file()
                    or marker.read_text(encoding="utf-8").strip().casefold() != journal["owner"]):
                raise HTTPException(409, detail="Retirement owner marker mismatch")
            # A crash may have removed some, but not all, verified MP4s.
            for entry in quarantine.iterdir():
                if entry.name == ".studio_owner":
                    continue
                if (entry.is_symlink() or not entry.is_file()
                        or entry.name not in journal["media"]
                        or digest(entry) != journal["media"][entry.name]):
                    raise HTTPException(409, detail="Changed media blocks committed cleanup")
            for filename in sorted(journal["media"]):
                (quarantine / filename).unlink(missing_ok=True)
            marker.unlink()
            quarantine.rmdir()
            _fsync_dir(self.root)
        journal["phase"] = "complete"

    def recover(self, digest):
        """Explicit recovery only; never operates without verified lease."""
        result = []
        with self._exclusive():
            for path in sorted(self.journal_root.glob("*.json")):
                if path.is_symlink() or not path.is_file():
                    raise HTTPException(409, detail="Unsafe retirement journal")
                journal = self._validate_journal(json.loads(path.read_text(encoding="utf-8")), path)
                if journal["phase"] in FINAL:
                    continue
                if journal["phase"] == "committed":
                    self._gc_committed(journal, digest)
                else:
                    self._recovery_digest = digest
                    self._restore_uncommitted(journal)
                _sync_json(path, journal)
                result.append({"id": journal["id"], "phase": journal["phase"]})
        return result

    def retire(self, *, owner, folder_name, confirmation, digest,
               preserve_defaults=()):
        owner = self._owner(owner)
        if confirmation != folder_name:
            raise HTTPException(409, detail="Confirm exact folder name")
        if folder_name in preserve_defaults:
            raise HTTPException(403, detail="Default folder is protected")
        with self._exclusive():
            self._pending()
            folder = self._folder(folder_name)
            # Re-check ALL Maker and uploader dependencies *after* exclusive
            # cross-service lease acquisition, closing preflight/DELETE races.
            report = self.inspector.inspect(
                owner=owner, folder_name=folder_name, folder=folder, include_private=True)
            if (report.get("issues") or not report.get("uploader_dependency_verified")
                    or report.get("source_claims_total") is None
                    or report.get("source_claims_total") != 0
                    or report.get("linked_short_profiles")
                    or report.get("linked_profiles")
                    or report.get("indexed_uploader_videos")
                    or report.get("active_uploader_claims")
                    or report.get("foreign_or_unassigned_references")):
                raise HTTPException(409, detail="Folder is still used or cannot be verified")
            receipts = report.get("verified_media", {})
            self._verify_entries(folder, owner, receipts, digest)
            txn = uuid.uuid4().hex
            journal = {"id": txn, "phase": "preparing",
                       "owner": owner, "folder": folder_name,
                       "quarantine": ".short-retirement-" + txn,
                       "media": dict(receipts)}
            path = self.journal_root / (txn + ".json")
            _sync_json(path, journal)
            renamed = False
            try:
                folder.rename(self.root / journal["quarantine"])
                renamed = True
                _fsync_dir(self.root)
                journal["phase"] = "quarantined"
                _sync_json(path, journal)
                # Journal is durable and no source remains at original path.
                # No SQLite database, profile, queue or history is mutated.
                journal["phase"] = "committed"
                _sync_json(path, journal)
            except Exception:
                if journal["phase"] != "committed" or not path.is_file():
                    if renamed:
                        self._recovery_digest = digest
                        self._restore_uncommitted(journal)
                    else:
                        journal["phase"] = "aborted"
                    _sync_json(path, journal)
                raise
            self._gc_committed(journal, digest)
            _sync_json(path, journal)
            return {"ok": True, "folder": folder_name,
                    "videos_deleted": len(receipts),
                    "linked_profiles_detached": 0,
                    "profiles_modified": False, "history_preserved": True}

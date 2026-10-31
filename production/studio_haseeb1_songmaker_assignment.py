"""Guarded Song Maker ownership first-claim for haseeb1.

Foreign named owners are never reassigned. A production classification remains
read-only until source-account ownership is proven outside this script.
No media, thumbnail, OAuth, Drive or other Studio code is changed.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess

from production.studio_haseeb1_assignment import TARGET, check_scene_account, require
from production.runtime import atomic_json

TABLES = ("jobs", "settings", "intake", "sync", "exports",
          "queue_control", "song_folders")
EXPECTED = {"jobs": 11, "settings": 3, "intake": 1, "sync": 2,
            "exports": 1, "queue_control": 0, "song_folders": 1}
TERMINAL = frozenset(("completed", "failed", "cancelled", "canceled",
                      "waiting for drive", "needs attention"))
UNITS = tuple("vps1-production-song-maker" + (("-" + lane) if lane else "") + ".service"
              for lane in ("", "render", "upload", "sync"))


def _exists(db, table):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                      (table,)).fetchone() is not None


def _columns(db, table):
    return {r[1] for r in db.execute("PRAGMA table_info(" + table + ")")}


def _users(scene_db):
    check_scene_account(scene_db)
    with sqlite3.connect(scene_db.as_uri() + "?mode=ro", uri=True) as db:
        return {str(u).casefold(): bool(a) for u, a in
                db.execute("SELECT username,is_active FROM users")}


def _classification(owner, users):
    value = str(owner or "")
    if value == TARGET:
        return "target"
    if not value:
        return "unassigned"
    if value.casefold() == TARGET:
        return "ambiguous_target_case"
    if value.casefold() == "admin":
        return "legacy_admin"
    if value.casefold() not in users:
        return "other_unrecognized"
    return "other_active" if users[value.casefold()] else "other_inactive"


def inspect(db, root, users):
    """Read-only inventory; report no names, job IDs or hashed paths."""
    summary = {"total": 0, "tables": {}, "owner_classes": {},
               "nonterminal_jobs": 0, "unsafe_media_paths": 0,
               "missing_media_paths": 0, "folder_paths_verified": 0,
               "unresolved_foreign": 0, "eligible_unassigned": 0}
    for table in TABLES:
        require(_exists(db, table), "SONGMAKER_TABLE_MISSING")
        require("owner" in _columns(db, table), "SONGMAKER_OWNER_SCHEMA_DRIFT")
        entries = db.execute("SELECT owner,COUNT(*) FROM " + table +
                             " GROUP BY owner").fetchall()
        count = sum(int(n) for _, n in entries)
        summary["tables"][table] = count
        summary["total"] += count
        for owner, n in entries:
            label = _classification(owner, users)
            summary["owner_classes"][label] = summary["owner_classes"].get(label, 0) + n
            if label == "unassigned":
                summary["eligible_unassigned"] += n
            elif label != "target":
                summary["unresolved_foreign"] += n
    require({"stage", "lane", "id", "source_key", "data"} <= _columns(db, "jobs"),
            "SONGMAKER_JOBS_SCHEMA_DRIFT")
    for stage, lane in db.execute("SELECT stage,lane FROM jobs"):
        if str(stage or "").strip().casefold() not in TERMINAL or str(lane or ""):
            summary["nonterminal_jobs"] += 1
    require({"id", "name"} <= _columns(db, "song_folders"),
            "SONGMAKER_FOLDER_SCHEMA_DRIFT")
    for owner, fid in db.execute("SELECT owner,id FROM song_folders"):
        if not isinstance(owner, str) or not re.fullmatch(r"[a-f0-9]{32}", str(fid)):
            summary["unsafe_media_paths"] += 1
            continue
        parent = root / "folders"
        path = parent / hashlib.sha256(owner.encode()).hexdigest() / str(fid)
        if any(p.is_symlink() for p in (root, parent, path.parent, path)):
            summary["unsafe_media_paths"] += 1
        elif not path.is_dir():
            summary["missing_media_paths"] += 1
        else:
            summary["folder_paths_verified"] += 1
    if _exists(db, "song_folder_media"):
        require({"job_id","folder_id"} <= _columns(db,"song_folder_media"),
                "SONGMAKER_MEDIA_SCHEMA_DRIFT")
        missing = db.execute("""SELECT COUNT(*) FROM song_folder_media m
            LEFT JOIN jobs j ON j.id=m.job_id
            LEFT JOIN song_folders f ON f.id=m.folder_id
            WHERE j.id IS NULL OR f.id IS NULL""").fetchone()[0]
        require(missing == 0, "SONGMAKER_BROKEN_MEDIA_RELATION")
    return summary


def audit(root: Path, scene_db: Path) -> dict:
    """Current-state audit deliberately tolerates foreign owners and schema drift only by blocking."""
    dbpath = root / "state/jobs.sqlite3"
    require(dbpath.is_file() and not dbpath.is_symlink(), "SONGMAKER_DB_UNAVAILABLE")
    users = _users(scene_db)
    with sqlite3.connect(dbpath.as_uri() + "?mode=ro", uri=True, timeout=20) as db:
        db.execute("PRAGMA query_only=ON")
        state = inspect(db, root, users)
    state["inventory_matches"] = all(state["tables"][k] == v for k, v in EXPECTED.items())
    state["source_owner_verified"] = state["unresolved_foreign"] == 0
    state["media_references_safe"] = state["unsafe_media_paths"] == 0 and state["missing_media_paths"] == 0
    # Empty/foreign folder owners cannot safely be renamed: paths are
    # SHA256(original owner)/folder-id and media references depend on them.
    state["eligible_for_claim"] = (
        state["inventory_matches"] and state["source_owner_verified"] and
        state["media_references_safe"] and state["nonterminal_jobs"] == 0 and
        _non_target_folders(dbpath) == 0
    )
    return {"mode": "READ_ONLY", "target_verified": True,
            "song_maker": state, "auto_script": "OFFLINE",
            "shared_google_drive": "UNCHANGED", "other_studios": "UNTOUCHED"}


def _non_target_folders(dbpath):
    with sqlite3.connect(dbpath.as_uri() + "?mode=ro", uri=True) as db:
        return int(db.execute("SELECT COUNT(*) FROM song_folders WHERE owner!=?",
                              (TARGET,)).fetchone()[0])


def _backup(source: Path, target: Path):
    target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    with sqlite3.connect(source.as_uri()+"?mode=ro", uri=True, timeout=20) as src, \
         sqlite3.connect(target, timeout=20) as dst:
        src.backup(dst)
        require(dst.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
                "SONGMAKER_BACKUP_INTEGRITY")
    target.chmod(0o600)


def _assign(db, root, users):
    """Only empty owner strings; caller holds exclusive transaction and stopped units."""
    before = inspect(db, root, users)
    require(before["unresolved_foreign"] == 0, "SONGMAKER_FOREIGN_OWNER_UNVERIFIED")
    require(before["nonterminal_jobs"] == 0, "SONGMAKER_ACTIVE_JOB")
    require(before["unsafe_media_paths"] == 0 and before["missing_media_paths"] == 0,
            "SONGMAKER_MEDIA_PATH_UNSAFE")
    require(not db.execute("SELECT 1 FROM song_folders WHERE owner!=? LIMIT 1",
                           (TARGET,)).fetchone(), "SONGMAKER_HASHED_FOLDER_REQUIRES_MAPPING")
    for name, count in EXPECTED.items():
        require(before["tables"][name] == count, "SONGMAKER_INVENTORY_CHANGED")
    # A target settings row must not be overwritten by an empty-owner row.
    for name in TABLES:
        if name not in ("jobs", "song_folders"):
            if db.execute("SELECT 1 FROM " + name + " WHERE owner='' LIMIT 1").fetchone():
                require(not db.execute("SELECT 1 FROM " + name +
                                       " WHERE owner=? LIMIT 1", (TARGET,)).fetchone(),
                        "SONGMAKER_TARGET_RECORD_COLLISION")
    require(not db.execute("""SELECT 1 FROM jobs a JOIN jobs b
            ON a.source_key=b.source_key AND a.owner='' AND b.owner=? LIMIT 1""",
                           (TARGET,)).fetchone(), "SONGMAKER_JOB_KEY_COLLISION")
    require(not db.execute("""SELECT 1 FROM jobs j
            JOIN song_folder_media m ON m.job_id=j.id WHERE j.owner='' LIMIT 1""").fetchone()
            if _exists(db, "song_folder_media") else True,
            "SONGMAKER_MEDIA_JOB_REQUIRES_MAPPING")
    for table in TABLES:
        if table == "song_folders":
            continue
        db.execute("UPDATE " + table + " SET owner=? WHERE owner=''", (TARGET,))
    after = inspect(db, root, users)
    require(after["unresolved_foreign"] == 0 and after["eligible_unassigned"] == 0,
            "SONGMAKER_POST_CLAIM_VERIFY")
    require(after["tables"] == before["tables"], "SONGMAKER_RECORD_LOSS")
    require(after["folder_paths_verified"] == before["folder_paths_verified"],
            "SONGMAKER_FOLDER_PATH_DRIFT")
    return before, after


def install(revision: str, root: Path, scene_db: Path, journal_base: Path):
    """Run only under production root helper host FIFO; this never deploys code."""
    require(re.fullmatch(r"[0-9a-f]{40}", revision) is not None, "INVALID_REVISION")
    require(shutil.disk_usage("/").free >= 5_000_000_000, "INSUFFICIENT_DISK_RESERVE")
    pre = audit(root, scene_db)
    state = pre["song_maker"]
    # Current named/foreign owner conflict is a hard stop BEFORE stopping
    # services, creating backups or touching a single byte of live data.
    require(state["inventory_matches"], "SONGMAKER_INVENTORY_CHANGED")
    require(state["source_owner_verified"], "SONGMAKER_FOREIGN_OWNER_UNVERIFIED")
    require(state["media_references_safe"], "SONGMAKER_MEDIA_PATH_UNSAFE")
    require(state["nonterminal_jobs"] == 0, "SONGMAKER_ACTIVE_JOB")
    require(state["eligible_for_claim"], "SONGMAKER_HASHED_FOLDER_REQUIRES_MAPPING")
    journal = journal_base / revision
    require(not journal.exists(), "SONGMAKER_TRANSACTION_ALREADY_EXISTS")
    previous = {u: subprocess.run(["systemctl","is-active","--quiet",u],
                    capture_output=True, timeout=12).returncode == 0 for u in UNITS}
    require(all(previous.values()), "SONGMAKER_SERVICE_NOT_RUNNING")
    journal.mkdir(parents=True, mode=0o700)
    atomic_json(journal/"intent.json", {"revision": revision, "target": TARGET,
                "state": "PREFLIGHT", "auto_script": "OFFLINE"})
    dbpath = root / "state/jobs.sqlite3"
    stopped = False
    modified = False
    backup = journal / "backup/jobs.sqlite3"
    try:
        stopped = True  # Restore even if a stop halfway through fails.
        for unit in reversed(UNITS):
            subprocess.run(["systemctl","stop",unit], check=True, timeout=30,
                           capture_output=True)
        # A final static check after quiescence closes the TOCTOU gap.
        require(audit(root, scene_db)["song_maker"] == state,
                "SONGMAKER_OWNER_CHANGED_DURING_STOP")
        _backup(dbpath, backup)
        users = _users(scene_db)
        with sqlite3.connect(dbpath, timeout=20) as db:
            db.execute("BEGIN IMMEDIATE")
            before, after = _assign(db, root, users)
            db.commit()
            modified = True
        for unit in UNITS:
            subprocess.run(["systemctl","start",unit], check=True, timeout=30,
                           capture_output=True)
        final = audit(root, scene_db)["song_maker"]
        require(final["unresolved_foreign"] == 0 and final["eligible_unassigned"] == 0,
                "SONGMAKER_POST_RESTART_MISMATCH")
        atomic_json(journal/"receipt.json", {
            "state": "SONGMAKER_FIRST_CLAIM_PASS", "owner": TARGET,
            "counts": after["tables"], "services": "RESTORED",
            "media": "UNCHANGED", "shared_google_drive": "UNCHANGED",
            "auto_script": "OFFLINE"})
        return {"STUDIO_SONGMAKER_ASSIGNMENT": "PASS", "owner": TARGET,
                "counts": after["tables"], "media": "UNCHANGED",
                "auto_script": "OFFLINE"}
    except BaseException:
        # No process can write while restoring a full SQLite snapshot.
        for unit in reversed(UNITS):
            subprocess.run(["systemctl","stop",unit], timeout=30, capture_output=True)
        if modified and backup.is_file():
            with sqlite3.connect(backup.as_uri()+"?mode=ro", uri=True) as src, \
                 sqlite3.connect(dbpath) as dst:
                src.backup(dst)
        atomic_json(journal/"receipt.json", {
            "state": "COMPENSATED" if modified else "NO_OWNER_MUTATION",
            "auto_script": "OFFLINE"})
        raise
    finally:
        if stopped:
            for unit in UNITS:
                if previous[unit]:
                    subprocess.run(["systemctl","start",unit], timeout=30,
                                   capture_output=True)

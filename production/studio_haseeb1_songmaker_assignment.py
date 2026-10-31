"""Guarded consented haseeb/team Song Maker migration to haseeb1.

Source owner names and per-table counts come from an independently observed
read-only production audit and explicit source-owner transfer authorization.
A mismatched owner, job, schema, or folder path blocks before any mutation.
No media, thumbnails, shared Drive or other Studio code is modified.
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
APPROVED = ("haseeb", "team")
PRIMARY = "haseeb"  # Owns nine jobs, manual intake, exports and the existing folder.
SOURCE_ROWS = {
    "jobs": {"haseeb": 9, "team": 2},
    "settings": {"haseeb": 1, "team": 1, "__default__": 1},
    "intake": {"haseeb": 1}, "sync": {"haseeb": 1, "team": 1},
    "exports": {"haseeb": 1}, "queue_control": {},
    "song_folders": {"haseeb": 1},
}
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
    # Exact strings matter for hashed paths and immutable owner manifest.
    if owner == TARGET:
        return "target"
    if owner in APPROVED and users.get(owner) is True:
        return "approved_source"
    if owner == "__default__":
        return "legacy_default"
    if not owner:
        return "unassigned"
    return "unresolved_foreign"

def inspect(db, root, users):
    """Read-only inventory; report no names, job IDs or hashed paths."""
    summary = {"total": 0, "tables": {}, "owner_classes": {},
               "owner_counts_match": True,
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
        observed = {str(owner): int(n) for owner, n in entries}
        summary["owner_counts_match"] &= observed == SOURCE_ROWS[table]
        for owner, n in entries:
            label = _classification(owner, users)
            summary["owner_classes"][label] = summary["owner_classes"].get(label, 0) + n
            if label == "unassigned":
                summary["eligible_unassigned"] += n
            elif label == "unresolved_foreign":
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
    state["source_owner_verified"] = (
        state["owner_counts_match"] and state["unresolved_foreign"] == 0 and
        state["owner_classes"].get("approved_source", 0) == 18 and
        state["owner_classes"].get("legacy_default", 0) == 1
    )
    state["media_references_safe"] = state["unsafe_media_paths"] == 0 and state["missing_media_paths"] == 0
    # Empty/foreign folder owners cannot safely be renamed: paths are
    # SHA256(original owner)/folder-id and media references depend on them.
    state["eligible_for_claim"] = (
        state["inventory_matches"] and state["source_owner_verified"] and
        state["media_references_safe"] and state["nonterminal_jobs"] == 0 and
        _non_target_folders(dbpath) == 1
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
    """Move only the 18 exact authorized records; keep default untouched."""
    before = inspect(db, root, users)
    require(before["owner_counts_match"] and before["total"] == 19,
            "SONGMAKER_SOURCE_OWNER_MANIFEST_DRIFT")
    require(before["nonterminal_jobs"] == 0, "SONGMAKER_ACTIVE_JOB")
    require(before["unsafe_media_paths"] == 0 and before["missing_media_paths"] == 0,
            "SONGMAKER_MEDIA_PATH_UNSAFE")
    # App code must be deployed first. Old Folders.path would follow a new
    # owner hash and break existing thumbnails/video links.
    require(_exists(db,"song_folder_path_owners") and
            {"folder_id","owner","original_owner_hash"} <= _columns(db,"song_folder_path_owners"),
            "SONGMAKER_FOLDER_ADAPTER_NOT_INSTALLED")
    require(not db.execute("SELECT 1 FROM song_folder_path_owners LIMIT 1").fetchone(),
            "SONGMAKER_FOLDER_MAPPING_ALREADY_EXISTS")
    require(not db.execute("SELECT 1 FROM jobs WHERE owner=? LIMIT 1",(TARGET,)).fetchone(),
            "SONGMAKER_TARGET_JOBS_EXIST")
    for table in ("settings","intake","sync","exports","queue_control","song_folders"):
        require(not db.execute("SELECT 1 FROM "+table+" WHERE owner=? LIMIT 1",
                               (TARGET,)).fetchone(),"SONGMAKER_TARGET_RECORD_COLLISION")
    # The jobs UNIQUE(owner,source_key) index makes two owners sharing the same
    # key impossible to fold together. Never silently drop or rekey jobs.
    require(not db.execute("""SELECT 1 FROM jobs a JOIN jobs b ON a.source_key=b.source_key
                 WHERE a.owner='haseeb' AND b.owner='team' LIMIT 1""").fetchone(),
            "SONGMAKER_JOB_KEY_COLLISION")
    require(not db.execute("SELECT 1 FROM jobs WHERE source_key IS NULL OR source_key='' LIMIT 1").fetchone(),
            "SONGMAKER_INVALID_JOB_KEY")
    fid, original_owner = db.execute("SELECT id,owner FROM song_folders").fetchone()
    require(original_owner == PRIMARY and re.fullmatch(r"[a-f0-9]{32}",fid) is not None,
            "SONGMAKER_FOLDER_SOURCE_CHANGED")
    # Job data (including folder_id, source IDs, and output locations) remains
    # byte-for-byte unchanged; only the owner column changes.
    for (owner,data) in db.execute("SELECT owner,data FROM jobs"):
        require(owner in APPROVED and isinstance(data,str), "SONGMAKER_JOB_OWNER_DRIFT")
        try:
            payload=json.loads(data)
        except ValueError:
            require(False, "SONGMAKER_JOB_DATA_INVALID")
        folder_id=payload.get("song_folder_id") if isinstance(payload,dict) else None
        require(not folder_id or folder_id == fid, "SONGMAKER_JOB_FOLDER_REF_DRIFT")
    require(not db.execute("""SELECT 1 FROM song_folder_media m
            LEFT JOIN jobs j ON j.id=m.job_id WHERE j.id IS NULL LIMIT 1""").fetchone()
            if _exists(db,"song_folder_media") else True,
            "SONGMAKER_MEDIA_REFERENCE_DRIFT")
    for table in ("settings","sync"):
        require({"owner","data"} <= _columns(db,table),
                "SONGMAKER_CONFIGURATION_SCHEMA_DRIFT")
        for (owner,data) in db.execute("SELECT owner,data FROM "+table):
            require(owner in (*APPROVED,"__default__") and isinstance(data,str),
                    "SONGMAKER_CONFIGURATION_OWNER_DRIFT")
            try:
                json.loads(data)
            except ValueError:
                require(False, "SONGMAKER_CONFIGURATION_JSON_INVALID")
    # Full snapshots of *both* source configurations are owned by haseeb1.
    # One active configuration per table is a schema constraint. Select the
    # primary folder/intake owner's config; archive team config without loss.
    db.execute("""CREATE TABLE IF NOT EXISTS song_maker_ownership_archive(
        source_table TEXT NOT NULL, legacy_owner TEXT NOT NULL,
        target_owner TEXT NOT NULL, data TEXT NOT NULL,
        PRIMARY KEY(source_table,legacy_owner,target_owner))""")
    require(not db.execute("SELECT 1 FROM song_maker_ownership_archive LIMIT 1").fetchone(),
            "SONGMAKER_CONFIGURATION_ARCHIVE_ALREADY_EXISTS")
    for table in ("settings","sync"):
        db.execute("""INSERT INTO song_maker_ownership_archive
            (source_table,legacy_owner,target_owner,data)
            SELECT ?,owner,?,data FROM """+table+"""
            WHERE owner IN ('haseeb','team')""",(table,TARGET))
        expected = {row[0]:row[1] for row in db.execute(
            "SELECT owner,data FROM "+table+" WHERE owner IN ('haseeb','team')")}
        actual = {row[0]:row[1] for row in db.execute(
            "SELECT legacy_owner,data FROM song_maker_ownership_archive "
            "WHERE source_table=? AND target_owner=?",(table,TARGET))}
        require(expected == actual and len(expected) == 2,
                "SONGMAKER_CONFIG_ARCHIVE_MISMATCH")
        primary_data = expected[PRIMARY]
        db.execute("DELETE FROM "+table+" WHERE owner IN ('haseeb','team')")
        db.execute("INSERT INTO "+table+"(owner,data) VALUES(?,?)",(TARGET,primary_data))
    db.execute("UPDATE jobs SET owner=? WHERE owner IN ('haseeb','team')",(TARGET,))
    for table in ("intake","exports","queue_control"):
        db.execute("UPDATE "+table+" SET owner=? WHERE owner=?",(TARGET,PRIMARY))
    path_hash=hashlib.sha256(original_owner.encode()).hexdigest()
    db.execute("""INSERT INTO song_folder_path_owners
         (folder_id,owner,original_owner_hash) VALUES(?,?,?)""",
         (fid,TARGET,path_hash))
    db.execute("UPDATE song_folders SET owner=? WHERE owner=?",(TARGET,PRIMARY))
    for table in TABLES:
        require(not db.execute("SELECT 1 FROM "+table+" WHERE owner IN ('haseeb','team') LIMIT 1").fetchone(),
                "SONGMAKER_SOURCE_RETAINED_UNEXPECTEDLY")
    # Original "__default__" is the shared fallback, never transferred.
    require(db.execute("SELECT COUNT(*) FROM settings WHERE owner='__default__'").fetchone()[0]==1,
            "SONGMAKER_DEFAULT_SETTINGS_CHANGED")
    for table,n in (("jobs",11),("settings",2),("intake",1),("sync",1),
                    ("exports",1),("queue_control",0),("song_folders",1)):
        require(db.execute("SELECT COUNT(*) FROM "+table).fetchone()[0] == n,
                "SONGMAKER_POST_TRANSFER_RECORD_COUNT")
    require(db.execute("SELECT COUNT(*) FROM song_maker_ownership_archive "
            "WHERE target_owner=?",(TARGET,)).fetchone()[0] == 4,
            "SONGMAKER_CONFIG_ARCHIVE_INCOMPLETE")
    require(db.execute("SELECT COUNT(*) FROM song_folder_path_owners WHERE owner=? "
            "AND original_owner_hash=?",(TARGET,path_hash)).fetchone()[0] == 1,
            "SONGMAKER_MEDIA_PATH_MAP_MISSING")
    return {"approved_source_records":18,"legacy_default_preserved":1,
            "active_target_rows":16,"archived_config_snapshots":4,
            "configuration_selection":"PRIMARY_HASEEB",
            "folder_path_hash_preserved":True}


def post_verify(db, root):
    for table,n in (("jobs",11),("settings",2),("intake",1),("sync",1),
                    ("exports",1),("queue_control",0),("song_folders",1)):
        require(db.execute("SELECT COUNT(*) FROM "+table).fetchone()[0] == n,
                "SONGMAKER_POST_TRANSFER_RECORD_COUNT")
        require(not db.execute("SELECT 1 FROM "+table+" WHERE owner IN ('haseeb','team') LIMIT 1").fetchone(),
                "SONGMAKER_FOREIGN_ROWS_AFTER_TRANSFER")
    require(db.execute("SELECT COUNT(*) FROM jobs WHERE owner=?",(TARGET,)).fetchone()[0] == 11,
            "SONGMAKER_TARGET_JOB_COUNT")
    require(db.execute("SELECT COUNT(*) FROM song_maker_ownership_archive "
            "WHERE target_owner=?",(TARGET,)).fetchone()[0] == 4,
            "SONGMAKER_ARCHIVE_NOT_ACCESSIBLE")
    fid,owner,original_hash = db.execute("""SELECT f.id,f.owner,p.original_owner_hash
        FROM song_folders f JOIN song_folder_path_owners p ON p.folder_id=f.id""").fetchone()
    require(owner==TARGET and original_hash==hashlib.sha256(PRIMARY.encode()).hexdigest(),
            "SONGMAKER_MEDIA_HASH_CHANGED")
    path=root/"folders"/original_hash/fid
    require(path.is_dir() and not path.is_symlink(),"SONGMAKER_ORIGINAL_MEDIA_PATH_MISSING")
    return {"target_jobs":11,"target_config_records":5,
            "archived_snapshots":4,"legacy_default_preserved":1,
            "existing_media_folder_preserved":True}


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
    require(state["eligible_for_claim"], "SONGMAKER_SOURCE_MAPPING_UNVERIFIED")
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
            result = _assign(db, root, users)
            db.commit()
            modified = True
        for unit in UNITS:
            subprocess.run(["systemctl","start",unit], check=True, timeout=30,
                           capture_output=True)
        with sqlite3.connect(dbpath.as_uri()+"?mode=ro",uri=True,timeout=20) as db:
            final = post_verify(db,root)
        atomic_json(journal/"receipt.json", {
            "state": "SONGMAKER_FIRST_CLAIM_PASS", "owner": TARGET,
            "counts": result, "post_verify": final, "services": "RESTORED",
            "media": "UNCHANGED", "shared_google_drive": "UNCHANGED",
            "auto_script": "OFFLINE"})
        return {"STUDIO_SONGMAKER_ASSIGNMENT": "PASS", "owner": TARGET,
                "counts": result, "post_verify": final, "media": "UNCHANGED",
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

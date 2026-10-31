"""Guarded first-owner assignment for Song and Short Uploader legacy data.

No cross-tenant transfers: only unassigned uploader entities are claimed.
Foreign-owned entities or project rows block the whole operation. This module
does not touch Copyright Studio, Google Drive, OAuth files, or upload statuses.
The caller must independently prove both application services are quiescent,
verify the canonical Scene account, and take consistent SQLite backups.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

TARGET = "haseeb1"
ENTITY_TABLES = (
    ("channel", "channels"),
    ("group", "channel_groups"),
    ("sheet", "sheet_sources"),
    ("profile", "auto_engine_profiles"),
)
ACTIVE = ("CLAIMED", "UPLOADING", "THUMBNAIL_UPLOADING", "PROCESSING", "IN_PROGRESS")


class MigrationBlocked(RuntimeError):
    pass


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise MigrationBlocked("HASEEB1_STUDIO_ASSIGNMENT_BLOCKED: " + reason)


def exists(con: sqlite3.Connection, table: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def schema(con: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in con.execute("PRAGMA table_info(" + table + ")")}


def check_scene_account(scene_db: Path, target: str = TARGET) -> dict:
    """Read-only proof: never create an account or guess by display name."""
    require(scene_db.is_file() and not scene_db.is_symlink(), "SCENE_DB_UNAVAILABLE")
    with sqlite3.connect(scene_db.as_uri() + "?mode=ro", uri=True, timeout=5) as con:
        require(exists(con, "users"), "SCENE_USERS_TABLE_MISSING")
        fields = schema(con, "users")
        require({"username", "is_active", "studio_access"} <= fields, "SCENE_AUTH_SCHEMA_DRIFT")
        rows = con.execute(
            "SELECT username,is_active,studio_access,is_admin FROM users WHERE username=? COLLATE NOCASE",
            (target,)
        ).fetchall()
        require(len(rows) == 1, "TARGET_USERNAME_NOT_UNIQUE_OR_MISSING")
        username, active, studios, admin = rows[0]
        require(str(username).casefold() == target and int(active) == 1, "TARGET_NOT_ACTIVE")
        permitted = set(str(studios or "").split(",")) if studios is not None else {"song", "short"}
        require(bool(admin) or {"song", "short"} <= permitted, "TARGET_STUDIO_ACCESS_MISSING")
    return {"target": target, "active": True, "studio_access": "PASS"}


def inspect_core(con: sqlite3.Connection, target: str = TARGET) -> dict:
    for _, table in ENTITY_TABLES:
        require(exists(con, table), "MISSING_" + table.upper())
        require("id" in schema(con, table), "INVALID_ID_" + table.upper())
    require(exists(con, "videos"), "VIDEOS_TABLE_MISSING")
    require("status" in schema(con, "videos"), "VIDEOS_STATUS_MISSING")
    marks = ",".join("?" for _ in ACTIVE)
    busy = con.execute("SELECT COUNT(*) FROM videos WHERE UPPER(COALESCE(status,'')) IN (" + marks + ")", ACTIVE).fetchone()[0]
    require(busy == 0, "ACTIVE_UPLOAD_PRESENT")
    if exists(con, "studio_entity_owners"):
        require({"entity_kind", "entity_id", "owner_username"} <= schema(con, "studio_entity_owners"),
                "OWNER_REGISTRY_SCHEMA_DRIFT")
    counts = {}
    for kind, table in ENTITY_TABLES:
        rows = con.execute("SELECT id FROM " + table).fetchall()
        require(all(isinstance(r[0], int) and r[0] > 0 for r in rows), "INVALID_ENTITY_ID")
        assigned = {}
        if exists(con, "studio_entity_owners"):
            assigned = {int(r[0]): str(r[1]).casefold() for r in con.execute(
                "SELECT entity_id,owner_username FROM studio_entity_owners WHERE entity_kind=?", (kind,)
            )}
        require(not (set(assigned) - {r[0] for r in rows}), "ORPHAN_OWNER_" + kind.upper())
        require(all(value == target for value in assigned.values()), "FOREIGN_OWNER_" + kind.upper())
        counts[kind] = {"total": len(rows), "already_owned": len(assigned),
                        "to_assign": len(rows) - len(assigned)}
    if exists(con, "channel_group_members"):
        bad = con.execute("""SELECT COUNT(*) FROM channel_group_members m
            LEFT JOIN channel_groups g ON g.id=m.group_id
            LEFT JOIN channels c ON c.id=m.channel_db_id
            WHERE g.id IS NULL OR c.id IS NULL""").fetchone()[0]
        require(bad == 0, "GROUP_CHANNEL_MEMBERSHIP_INVALID")
    return counts


def assign_core(con: sqlite3.Connection, target: str = TARGET) -> dict:
    """Caller owns BEGIN IMMEDIATE / COMMIT; never commit in this function."""
    counts = inspect_core(con, target)
    con.execute("""CREATE TABLE IF NOT EXISTS studio_entity_owners(
       entity_kind TEXT NOT NULL, entity_id INTEGER NOT NULL,
       owner_username TEXT NOT NULL COLLATE NOCASE,
       created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
       PRIMARY KEY(entity_kind,entity_id))""")
    con.execute("""CREATE INDEX IF NOT EXISTS idx_studio_entity_owner
                   ON studio_entity_owners(owner_username,entity_kind)""")
    for kind, table in ENTITY_TABLES:
        con.execute(
            "INSERT INTO studio_entity_owners(entity_kind,entity_id,owner_username) "
            "SELECT ?,t.id,? FROM " + table + " t WHERE NOT EXISTS "
            "(SELECT 1 FROM studio_entity_owners o WHERE o.entity_kind=? AND o.entity_id=t.id)",
            (kind, target, kind),
        )
    verified = inspect_core(con, target)
    require(all(row["to_assign"] == 0 for row in verified.values()), "ASSIGNMENT_INCOMPLETE")
    return counts


def inspect_projects(con: sqlite3.Connection, target: str = TARGET) -> dict:
    require(exists(con, "projects"), "PROJECTS_TABLE_MISSING")
    columns = schema(con, "projects")
    require({"id", "owner_username", "status", "channel_db_id"} <= columns, "PROJECTS_SCHEMA_DRIFT")
    rows = con.execute("SELECT owner_username,COUNT(*) FROM projects GROUP BY owner_username").fetchall()
    # Approved transfer is limited to unassigned legacy rows and the default
    # administrator's historic projects. Never claim another named account.
    owners = {str(r[0] or "").strip().casefold(): int(r[1]) for r in rows}
    require(all(k in ("", "admin", target) for k in owners),
            "PROJECT_OWNER_REQUIRES_SEPARATE_TRANSFER")
    busy = con.execute("""SELECT COUNT(*) FROM projects WHERE UPPER(COALESCE(status,'')) IN
              ('DOWNLOADING','SOURCE_READY','UPLOADING','VERIFYING_REMOTE','CLEANING')""").fetchone()[0]
    require(busy == 0, "ACTIVE_PROJECT_PRESENT")
    return {"total": sum(owners.values()), "to_assign": owners.get("", 0) + owners.get("admin", 0), "already_owned": owners.get(target, 0)}


def assign_projects(con: sqlite3.Connection, target: str = TARGET) -> dict:
    counts = inspect_projects(con, target)
    con.execute("UPDATE projects SET owner_username=? WHERE TRIM(owner_username)='' OR LOWER(TRIM(owner_username))='admin'", (target,))
    require(inspect_projects(con, target)["to_assign"] == 0, "PROJECT_ASSIGNMENT_INCOMPLETE")
    return counts


def read_only_audit(data_root: Path, scene_db: Path) -> dict:
    """Do not mutate state; reports only aggregate counts."""
    check_scene_account(scene_db)
    results = {}
    for mode, leaf in (("song", "lum-song"), ("short", "lum-shortstudio")):
        root = data_root / "local-upload-manager" / leaf
        core, project = root / "core/data/uploader.db", root / "projects.sqlite3"
        require(core.is_file() and not core.is_symlink(), mode.upper() + "_CORE_MISSING")
        require(project.is_file() and not project.is_symlink(), mode.upper() + "_PROJECTS_MISSING")
        with sqlite3.connect(core.as_uri() + "?mode=ro", uri=True) as con:
            counters = inspect_core(con)
        with sqlite3.connect(project.as_uri() + "?mode=ro", uri=True) as con:
            projects = inspect_projects(con)
        results[mode] = {"entities": counters, "projects": projects}
    return {"target_verified": True, "mode": "READ_ONLY", "instances": results,
            "related_stores": inspect_related_stores(data_root),
            "shared_google_drive": "UNCHANGED", "auto_script": "OFFLINE"}

def install(revision: str) -> None:
    """Serialized production root-helper entry; uploader-state ownership only.

    Never restart a busy uploader. Backup every SQLite database in each
    isolated Song/Short state, stop only those two instances, and restore
    databases + service states if any post-write assertion fails.
    """
    import json
    import re
    import shutil
    import subprocess
    from production.runtime import BASE, DATA, backup_databases, atomic_json
    from production.song_uploader_group_summary_release import idle as song_idle, health as song_health
    from production.short_uploader_group_release import idle as short_idle, health as short_health

    require(bool(re.fullmatch(r"[0-9a-f]{40}", revision)), "INVALID_REVISION")
    require(shutil.disk_usage("/").free >= 5_000_000_000, "INSUFFICIENT_DISK_RESERVE")
    scene_db = DATA / "scene-builder/web/web.sqlite3"
    roots = {mode: DATA / "local-upload-manager" / leaf for mode, leaf in
             (("song", "lum-song"), ("short", "lum-shortstudio"))}
    units = {
        mode: tuple(f"vps1-production-lum-{name}-{part}.service"
                    for part in ("scheduler", "worker", "web"))
        for mode, name in (("song", "song"), ("short", "shortstudio"))
    }
    journal = BASE / "transactions/haseeb1-song-short-assignment" / revision
    require(not journal.exists(), "TRANSACTION_ALREADY_EXISTS")
    check_scene_account(scene_db)
    # This fence checks the live health identity and upload/remote-ledger state.
    song_idle()
    short_idle(allow_waiting_network=True)
    before = read_only_audit(DATA, scene_db)
    previous = {}
    for names in units.values():
        for name in names:
            state = subprocess.run(["systemctl", "is-active", "--quiet", name],
                                   timeout=12, capture_output=True).returncode == 0
            require(state, "EXPECTED_ACTIVE_UNIT_NOT_RUNNING")
            previous[name] = state

    journal.mkdir(parents=True, mode=0o700)
    atomic_json(journal / "intent.json",
                {"revision": revision, "target": TARGET, "units": list(previous),
                 "state": "PREFLIGHT_PASS", "auto_script": "OFFLINE"})
    # Backups taken before changing any state, including committed WAL data.
    backups = {mode: backup_databases(root, journal / "backup" / mode)
               for mode, root in roots.items()}
    require(all(backups.values()), "NO_DATABASE_BACKUP")
    stopped = False
    changed = False
    try:
        song_idle()
        short_idle(allow_waiting_network=True)
        # Protect in-memory queues from races; only Song and Short are stopped.
        stopped = True
        for mode in ("song", "short"):
            for name in units[mode]:
                subprocess.run(["systemctl", "stop", name], check=True,
                               capture_output=True, text=True, timeout=90)
        check_scene_account(scene_db)
        read_only_audit(DATA, scene_db)
        results = {}
        for mode, root in roots.items():
            core = root / "core/data/uploader.db"
            project = root / "projects.sqlite3"
            require(core.is_file() and not core.is_symlink(), "CORE_DB_CHANGED")
            require(project.is_file() and not project.is_symlink(), "PROJECT_DB_CHANGED")
            with sqlite3.connect(core, timeout=15) as con:
                con.execute("BEGIN IMMEDIATE")
                results[mode] = {"entities": assign_core(con)}
            with sqlite3.connect(project, timeout=15) as con:
                con.execute("BEGIN IMMEDIATE")
                results[mode]["projects"] = assign_projects(con)
            changed = True
        after = read_only_audit(DATA, scene_db)
        require(all(
            all(entry["to_assign"] == 0 for entry in value["entities"].values()) and
            value["projects"]["to_assign"] == 0
            for value in after["instances"].values()), "POST_MIGRATION_OWNER_COUNT")
        for mode in ("short", "song"):
            for name in reversed(units[mode]):
                subprocess.run(["systemctl", "start", name], check=True,
                               capture_output=True, text=True, timeout=90)
        stopped = False
        require(song_health().get("ok") is True and short_health().get("ok") is True,
                "POST_RESTART_HEALTH_FAILED")
        atomic_json(journal / "receipt.json",
                    {"state": "UPLOADER_DATA_ASSIGNED", "revision": revision,
                     "owner": TARGET, "before": before, "after": after,
                     "backups": {key: len(value) for key, value in backups.items()},
                     "google_drive": "UNCHANGED", "auto_script": "OFFLINE"})
        print(json.dumps({"STUDIO_UPLOADER_ASSIGNMENT": "PASS",
                          "owner": TARGET, "scope": "SONG_SHORT_UPLOADERS_ONLY",
                          "instances": after["instances"], "rollback": "BACKUPS_PRESENT",
                          "other_studio_data": "NOT_MIGRATED"},sort_keys=True),flush=True)
    except BaseException:
        # Retry stopping, then SQLite backup-API restore. Never edit media,
        # token files, Drive, per-account records or Copyright Studio.
        if changed or stopped:
            for names in units.values():
                for name in names:
                    subprocess.run(["systemctl", "stop", name], check=True,
                                   capture_output=True, text=True, timeout=90)
            for mode, root in roots.items():
                for relative in backups[mode]:
                    saved = journal / "backup" / mode / relative
                    original = root / relative
                    require(original.is_file() and not original.is_symlink(),
                            "ROLLBACK_DB_PATH_CHANGED")
                    with sqlite3.connect(saved) as src, sqlite3.connect(original) as dst:
                        src.backup(dst)
                        require(dst.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
                                "ROLLBACK_INTEGRITY_FAILURE")
            for mode in ("short", "song"):
                for name in reversed(units[mode]):
                    if previous[name]:
                        subprocess.run(["systemctl", "start", name], check=True,
                                       capture_output=True, text=True, timeout=90)
            require(song_health().get("ok") is True and short_health().get("ok") is True,
                    "ROLLBACK_HEALTH_FAILURE")
        atomic_json(journal / "receipt.json", {"state": "BLOCKED_OR_ROLLED_BACK",
                    "revision": revision, "auto_script": "OFFLINE"})
        raise

def inspect_related_stores(data_root: Path, target: str = TARGET) -> dict:
    """Read-only aggregate inventory of independent Song/Short Studio owners.

    This must run before claiming COMPLETE ownership of either Studio. Never
    emit usernames, account emails, filenames, folder names, tokens or IDs.
    """
    import json
    root = Path(data_root)
    results = {}
    for kind, path, tables in (
        ("song_maker", root / "song-maker/state/jobs.sqlite3",
         (("jobs","owner"),("settings","owner"),("intake","owner"),
          ("sync","owner"),("exports","owner"),("queue_control","owner"),
          ("song_folders","owner"))),
        ("audio_downloader", root / "voice-studio/audio-downloader/data/jobs.sqlite3",
         (("jobs","owner_username"),("audio_preferences","owner"))),
    ):
        if not path.exists():
            results[kind] = {"present": False, "records": 0, "non_target": 0}
            continue
        require(path.is_file() and not path.is_symlink(), "RELATED_DB_UNTRUSTED")
        entries = {}
        with sqlite3.connect(path.as_uri() + "?mode=ro",uri=True,timeout=5) as con:
            con.execute("PRAGMA query_only=ON")
            for table, field in tables:
                if not exists(con,table):
                    entries[table] = {"total":0,"non_target":0}
                    continue
                require(field in schema(con,table),"RELATED_SCHEMA_DRIFT")
                total, not_target = con.execute(
                    f"SELECT COUNT(*),SUM(CASE WHEN LOWER(TRIM(COALESCE({field},'')))<>? THEN 1 ELSE 0 END) FROM {table}",
                    (target,),
                ).fetchone()
                entries[table] = {"total":int(total or 0),
                                  "non_target":int(not_target or 0)}
        results[kind] = {"present":True,
                         "records":sum(v["total"] for v in entries.values()),
                         "non_target":sum(v["non_target"] for v in entries.values()),
                         "tables":entries}
    short = root / "short-maker"
    jobs_dir = short / "jobs"
    folders_dir = short / "vps_short_folders"
    job_total = job_other = folder_total = folder_other = 0
    if jobs_dir.is_dir():
        require(not jobs_dir.is_symlink(), "SHORT_JOBS_UNTRUSTED")
        for item in jobs_dir.iterdir():
            require(item.is_dir() and not item.is_symlink(), "SHORT_JOB_UNTRUSTED")
            state=item / "state.json"
            if not state.exists():
                continue
            require(state.is_file() and not state.is_symlink(), "SHORT_JOB_STATE_UNTRUSTED")
            raw=json.loads(state.read_text(encoding="utf-8"))
            require(isinstance(raw,dict), "SHORT_JOB_STATE_INVALID")
            job_total+=1
            job_other+=int(str(raw.get("owner") or "").casefold()!=target)
    if folders_dir.is_dir():
        require(not folders_dir.is_symlink(),"SHORT_FOLDERS_UNTRUSTED")
        for item in folders_dir.iterdir():
            require(item.is_dir() and not item.is_symlink(),"SHORT_FOLDER_UNTRUSTED")
            folder_total+=1
            marker=item / ".studio_owner"
            require(not marker.is_symlink(),"SHORT_FOLDER_MARKER_UNTRUSTED")
            owner=marker.read_text(encoding="utf-8").strip().casefold() if marker.is_file() else ""
            folder_other+=int(owner!=target)
    results["short_maker"]={"present":short.is_dir(),"jobs":job_total,"jobs_non_target":job_other,
                              "folders":folder_total,"folders_non_target":folder_other}
    return results

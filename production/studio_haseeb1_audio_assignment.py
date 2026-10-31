"""Evidence-gated, online-safe first-owner claim for Audio Downloader legacy rows.

An authenticated Song Maker handoff owner is the only automatic positive
job-level provenance. Unassigned jobs without that evidence stay unassigned.
Nonterminal jobs stay untouched regardless of provenance. No services are
stopped, no worker recovery invoked, and no media or settings are rewritten.
This module is called only by the SHA-pinned VPS production root helper.
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
from pathlib import Path

from production.studio_haseeb1_assignment import TARGET, check_scene_account, require

DB_REL = Path("voice-studio/audio-downloader/data/jobs.sqlite3")
SCENE_REL = Path("scene-builder/web/web.sqlite3")
TERMINAL = frozenset(("completed", "delivered", "failed", "cancelled", "canceled"))
REQUIRED_JOBS = {
    "job_id", "video_id", "status", "owner_username", "song_maker_owner",
    "auto_song_maker", "local_file", "local_thumbnail", "drive_path",
    "thumbnail_drive_path", "song_maker_job_id", "song_folder_id",
}
REQUIRED_PREFS = {"owner", "format", "quality", "auto_upload", "auto_song_maker"}


class AudioClaimBlocked(RuntimeError):
    pass


def guard(ok: bool, code: str) -> None:
    if not ok:
        raise AudioClaimBlocked("HASEEB1_AUDIO_ASSIGNMENT_BLOCKED: " + code)


def columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in con.execute("PRAGMA table_info(" + table + ")")}


def trusted_db(data_root: Path) -> Path:
    db_path = data_root / DB_REL
    guard(db_path.is_file() and not db_path.is_symlink(), "AUDIO_DB_NOT_TRUSTED")
    guard(not any(p.is_symlink() for p in db_path.parents if p != Path("/")),
          "AUDIO_PARENT_SYMLINK")
    return db_path


def open_db(db_path: Path, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        con = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=10)
        con.execute("PRAGMA query_only=ON")
    else:
        con = sqlite3.connect(db_path, timeout=10, isolation_level=None)
        con.execute("PRAGMA busy_timeout=10000")
    con.row_factory = sqlite3.Row
    return con


def classify(row: sqlite3.Row) -> str:
    """A username is not proof unless the job already bears it or the
    explicit authenticated handoff recorded it when the job was created."""
    owner = str(row["owner_username"] or "").strip().casefold()
    handoff = str(row["song_maker_owner"] or "").strip().casefold()
    status = str(row["status"] or "").strip().casefold()
    if owner and owner != TARGET:
        return "foreign_owner"
    if handoff and handoff != TARGET:
        return "handoff_conflict"
    if owner == TARGET:
        return "already_owned"
    if not handoff or int(row["auto_song_maker"] or 0) != 1:
        return "unproven_legacy"
    if status not in TERMINAL:
        return "nonterminal_deferred"
    return "eligible"


def inspect(con: sqlite3.Connection) -> tuple[dict, list[dict]]:
    guard(REQUIRED_JOBS <= columns(con, "jobs"), "AUDIO_JOB_SCHEMA_DRIFT")
    guard(REQUIRED_PREFS <= columns(con, "audio_preferences"), "AUDIO_PREF_SCHEMA_DRIFT")
    rows = con.execute("SELECT * FROM jobs ORDER BY job_id").fetchall()
    counts = {name: 0 for name in (
        "already_owned", "foreign_owner", "handoff_conflict", "unproven_legacy",
        "nonterminal_deferred", "eligible",
    )}
    nonterminal = 0
    media = {"audio_paths_present": 0, "thumbnail_paths_present": 0,
             "drive_receipts_present": 0, "handoff_receipts_present": 0}
    eligible = []
    for row in rows:
        cls = classify(row)
        counts[cls] += 1
        if str(row["status"] or "").strip().casefold() not in TERMINAL:
            nonterminal += 1
        if row["local_file"]:
            media["audio_paths_present"] += 1
        if row["local_thumbnail"]:
            media["thumbnail_paths_present"] += 1
        if row["drive_path"]:
            media["drive_receipts_present"] += 1
        if row["song_maker_job_id"]:
            media["handoff_receipts_present"] += 1
        if cls == "eligible":
            eligible.append(dict(row))
    preferences = con.execute(
        "SELECT owner FROM audio_preferences"
    ).fetchall()
    pref_counts = {"target": 0, "other": 0}
    for pref in preferences:
        pref_counts["target" if str(pref["owner"] or "").strip().casefold()
                    == TARGET else "other"] += 1
    return {
        "total_jobs": len(rows),
        "nonterminal_jobs": nonterminal,
        "owners": counts,
        "preferences": pref_counts,
        "media_references": media,
    }, eligible


def read_only(data_root: Path, scene_db: Path) -> dict:
    check_scene_account(scene_db)
    path = trusted_db(data_root)
    with open_db(path, readonly=True) as con:
        job_columns = columns(con, "jobs")
        pref_columns = columns(con, "audio_preferences")
        schema_missing_jobs = sorted(REQUIRED_JOBS - job_columns)
        schema_missing_preferences = sorted(REQUIRED_PREFS - pref_columns)
        if not schema_missing_jobs and not schema_missing_preferences:
            summary, _ = inspect(con)
            schema_status = "READY_FOR_PROVEN_TERMINAL_ONLY"
        else:
            # Legacy schemas are audited without ALTER TABLE, service
            # restart or guessing missing owner columns. No migration is
            # authorized until production schema matches reviewed source.
            total = int(con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]) if job_columns else 0
            nonterminal = int(con.execute(
                "SELECT COUNT(*) FROM jobs WHERE lower(trim(coalesce(status,''))) NOT IN ("
                + ",".join("?" for _ in TERMINAL) + ")", tuple(sorted(TERMINAL))
            ).fetchone()[0]) if "status" in job_columns else None
            proven = None
            foreign_handoff = None
            unproven = None
            if {"song_maker_owner", "auto_song_maker"} <= job_columns:
                evidence = con.execute("""
                    SELECT
                      SUM(CASE WHEN lower(trim(coalesce(song_maker_owner,'')))=?
                                AND auto_song_maker=1 THEN 1 ELSE 0 END),
                      SUM(CASE WHEN trim(coalesce(song_maker_owner,''))<>''
                                AND lower(trim(song_maker_owner))<>? THEN 1 ELSE 0 END),
                      SUM(CASE WHEN trim(coalesce(song_maker_owner,''))=''
                                OR auto_song_maker<>1 THEN 1 ELSE 0 END)
                    FROM jobs
                """, (TARGET, TARGET)).fetchone()
                proven, foreign_handoff, unproven = [int(v or 0) for v in evidence]
            pref_target = pref_foreign = None
            if "owner" in pref_columns:
                prefs = con.execute("""SELECT
                    SUM(CASE WHEN lower(trim(owner))=? THEN 1 ELSE 0 END),
                    SUM(CASE WHEN lower(trim(owner))<>? THEN 1 ELSE 0 END)
                    FROM audio_preferences""", (TARGET, TARGET)).fetchone()
                pref_target, pref_foreign = [int(v or 0) for v in prefs]
            summary = {
                "total_jobs": total, "nonterminal_jobs": nonterminal,
                "owners": "UNVERIFIED_SCHEMA",
                "authenticated_handoff_matches": proven,
                "foreign_handoff_records": foreign_handoff,
                "unproven_handoff_records": unproven,
                "preferences": {"target": pref_target, "other": pref_foreign},
                "media_references": "UNCHANGED",
            }
            schema_status = "APPLY_BLOCKED_SCHEMA_DRIFT"
    # Classify foreign handoff account activity without exposing identities.
    # A non-target handoff is always protected, even if its named account is
    # inactive or absent from the current Scene user table.
    foreign_status = {"active_account": 0, "inactive_account": 0, "unknown_account": 0}
    if "song_maker_owner" in job_columns:
        with sqlite3.connect(scene_db.as_uri() + "?mode=ro", uri=True, timeout=5) as identity:
            registered = {
                str(name or "").strip().casefold(): bool(int(active or 0))
                for name, active in identity.execute("SELECT username,is_active FROM users")
            }
        with open_db(path, readonly=True) as con:
            linked = con.execute("""
                SELECT song_maker_owner FROM jobs
                WHERE trim(coalesce(song_maker_owner,''))<>''
                  AND lower(trim(song_maker_owner))<>?
            """, (TARGET,)).fetchall()
            for row in linked:
                owner = str(row[0]).strip().casefold()
                category = ("unknown_account" if owner not in registered
                            else "active_account" if registered[owner]
                            else "inactive_account")
                foreign_status[category] += 1
    summary["foreign_handoff_account_status"] = foreign_status
    return {
        "mode": "READ_ONLY", "target_verified": True, "inventory": summary,
        "schema_status": schema_status,
        "schema_missing_jobs": schema_missing_jobs,
        "schema_missing_preferences": schema_missing_preferences,
        "unproven_jobs_remain_unassigned": True, "other_users_untouched": True,
        "google_drive": "UNCHANGED", "auto_script": "OFFLINE",
    }


def backup_sqlite(source: Path, destination: Path) -> None:
    """SQLite backup API includes committed WAL pages, without stopping workers."""
    guard(not destination.exists(), "BACKUP_ALREADY_EXISTS")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open_db(source, readonly=True) as src:
        with sqlite3.connect(destination) as dst:
            src.backup(dst)
            guard(dst.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
                  "BACKUP_INTEGRITY_FAILED")
    destination.chmod(0o600)


def install(revision: str) -> dict:
    """Only proven *terminal* rows are claimed; never restart a live service.

    Writer races are closed by BEGIN IMMEDIATE and exact before-image checks.
    Preferences and all fields except owner_username remain byte-for-byte
    unchanged at commit. Any precommit error rolls back atomically.
    """
    from production.runtime import BASE, DATA, atomic_json
    guard(bool(re.fullmatch("[a-f0-9]{40}", revision)), "INVALID_REVISION")
    guard(shutil.disk_usage("/").free >= 5_000_000_000, "DISK_RESERVE_UNAVAILABLE")
    scene = DATA / SCENE_REL
    check_scene_account(scene)
    source = trusted_db(DATA)
    journal = BASE / "transactions/haseeb1-audio-downloader-owner" / revision
    guard(not journal.exists(), "TRANSACTION_ALREADY_EXISTS")
    # A no-mutation preflight makes unproven and foreign owners visible first.
    with open_db(source, readonly=True) as con:
        before, _ = inspect(con)
    journal.mkdir(parents=True, exist_ok=False, mode=0o700)
    atomic_json(journal / "intent.json", {
        "target": TARGET, "revision": revision, "state": "PREFLIGHT",
        "inventory": before, "service": "UNCHANGED", "auto_script": "OFFLINE",
    })
    backup_sqlite(source, journal / "backup/jobs.sqlite3")
    con = open_db(source)
    claimed: list[str] = []
    try:
        con.execute("BEGIN IMMEDIATE")
        # Recheck the account while the DB writer lock is held.
        check_scene_account(scene)
        locked_before, candidates = inspect(con)
        for candidate in candidates:
            key = candidate["job_id"]
            # Owner and exact status are compared; queues may have changed
            # between preflight and acquisition of the SQLite writer lock.
            cur = con.execute(
                """UPDATE jobs SET owner_username=?
                   WHERE job_id=? AND trim(owner_username)=''
                     AND lower(trim(song_maker_owner))=?
                     AND auto_song_maker=1 AND status=?""",
                (TARGET, key, TARGET, candidate["status"]),
            )
            guard(cur.rowcount == 1, "CONCURRENT_OWNER_OR_STATUS_CHANGE")
            claimed.append(key)
        after, _ = inspect(con)
        guard(after["total_jobs"] == locked_before["total_jobs"],
              "TOTAL_CHANGED_DURING_TRANSACTION")
        guard(after["owners"]["already_owned"] ==
              locked_before["owners"]["already_owned"] + len(claimed),
              "OWNER_VERIFICATION_FAILED")
        guard(after["owners"]["eligible"] == 0, "ELIGIBLE_OWNER_REMAINED")
        # Every original job column other than owner must match; this also
        # protects YouTube IDs, queue positions, local paths and Drive receipts.
        for candidate in candidates:
            row = con.execute("SELECT * FROM jobs WHERE job_id=?",
                              (candidate["job_id"],)).fetchone()
            guard(row is not None and all(
                row[field] == value for field, value in candidate.items()
                if field != "owner_username"
            ), "SOURCE_FIELD_MODIFIED")
        atomic_json(journal / "claimed.json", {
            "job_ids": claimed, "state": "PREPARED", "target": TARGET,
        })
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        atomic_json(journal / "receipt.json", {
            "state": "ROLLED_BACK_BEFORE_COMMIT", "auto_script": "OFFLINE",
        })
        raise
    finally:
        con.close()
    # The receipt is sanitized; IDs remain in the root-only journal.
    report = {
        "state": "PARTIAL" if (
            after["owners"]["foreign_owner"] or after["owners"]["handoff_conflict"]
            or after["owners"]["unproven_legacy"]
            or after["owners"]["nonterminal_deferred"]
        ) else "OWNERSHIP_VERIFIED",
        "claimed_now": len(claimed), "inventory": after,
        "media": "UNCHANGED", "preferences": "UNCHANGED",
        "services": "UNCHANGED", "google_drive": "UNCHANGED",
        "auto_script": "OFFLINE", "rollback": "OWNER_ONLY_COMPENSATION_AVAILABLE",
    }
    atomic_json(journal / "receipt.json", report)
    print(json.dumps({"HASEEB1_AUDIO_ASSIGNMENT": report}, sort_keys=True), flush=True)
    return report


def rollback(revision: str) -> dict:
    """Compensate *only* our claims, and only when no status/owner has moved.

    Never restore a whole SQLite backup over concurrent downloader changes.
    The saved WAL-consistent database is for offline recovery, not live replace.
    """
    from production.runtime import BASE, DATA, atomic_json
    guard(bool(re.fullmatch("[a-f0-9]{40}", revision)), "INVALID_REVISION")
    journal = BASE / "transactions/haseeb1-audio-downloader-owner" / revision
    record = journal / "claimed.json"
    guard(record.is_file() and not record.is_symlink(), "CLAIM_RECEIPT_MISSING")
    payload = json.loads(record.read_text())
    guard(payload.get("target") == TARGET, "CLAIM_TARGET_MISMATCH")
    ids = payload.get("job_ids")
    guard(isinstance(ids, list) and len(set(ids)) == len(ids), "CLAIM_IDS_INVALID")
    source = trusted_db(DATA)
    with open_db(source) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            # Do not revoke access to a job that has been restarted or changed.
            for jid in ids:
                row = con.execute("SELECT owner_username,status,song_maker_owner FROM jobs WHERE job_id=?",
                                  (jid,)).fetchone()
                guard(row is not None and str(row["owner_username"]).casefold() == TARGET
                      and str(row["status"]).casefold() in TERMINAL
                      and str(row["song_maker_owner"]).casefold() == TARGET,
                      "ROLLBACK_OWNER_OR_STATE_CONFLICT")
            for jid in ids:
                cur = con.execute(
                    "UPDATE jobs SET owner_username='' WHERE job_id=? AND owner_username=?",
                    (jid, TARGET),
                )
                guard(cur.rowcount == 1, "ROLLBACK_CONCURRENT_CHANGE")
            con.execute("COMMIT")
        except BaseException:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
    receipt = {"state": "COMPENSATED", "claims_reverted": len(ids),
               "media": "UNCHANGED", "queues": "UNCHANGED", "auto_script": "OFFLINE"}
    atomic_json(journal / "rollback.json", receipt)
    return receipt

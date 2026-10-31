"""One-time, consented ownership of 20 legacy Audio Downloader jobs.

All 20 were observed terminal on 2026-10-31. Fifteen have no handoff
provenance and are explicitly assigned to haseeb1. Five authenticated handoff
records belong to other ACTIVE accounts and retain that ownership. Do not
rewrite their settings, media, statuses, uploads or Song Maker references.
The strict observed-count fence prevents grabbing a newly created job.
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
from pathlib import Path

from production import studio_haseeb1_audio_assignment as audio
from production.studio_haseeb1_assignment import TARGET, check_scene_account

EXPECTED_TOTAL = 20
EXPECTED_UNPROVEN = 15
EXPECTED_FOREIGN = 5


def _guard(ok: bool, code: str) -> None:
    audio.guard(ok, code)


def _accounts(scene: Path) -> dict[str, bool]:
    with sqlite3.connect(scene.as_uri() + "?mode=ro", uri=True, timeout=5) as con:
        con.execute("PRAGMA query_only=ON")
        return {str(n or "").strip().casefold(): bool(int(active or 0))
                for n, active in con.execute("SELECT username,is_active FROM users")}


def _preflight(source: Path, scene: Path) -> dict:
    check_scene_account(scene)
    _guard(source.is_file() and not source.is_symlink(), "LEGACY_AUDIO_DB_UNTRUSTED")
    with audio.open_db(source, readonly=True) as con:
        cols = audio.columns(con, "jobs")
        _guard("owner_username" not in cols, "LEGACY_OWNER_SCHEMA_ALREADY_PRESENT")
        _guard((audio.REQUIRED_JOBS - {"owner_username"}) <= cols, "LEGACY_OTHER_SCHEMA_DRIFT")
        _guard(audio.REQUIRED_PREFS <= audio.columns(con, "audio_preferences"),
               "LEGACY_PREF_SCHEMA_DRIFT")
        rows = con.execute("SELECT status,song_maker_owner FROM jobs").fetchall()
    registered = _accounts(scene)
    _guard(len(rows) == EXPECTED_TOTAL, "LEGACY_TOTAL_CHANGED")
    _guard(all(str(r["status"] or "").strip().casefold() in audio.TERMINAL for r in rows),
           "LEGACY_ACTIVE_JOBS")
    foreign = [str(r["song_maker_owner"] or "").strip().casefold()
               for r in rows if str(r["song_maker_owner"] or "").strip().casefold() not in ("", TARGET)]
    target_handoff = sum(str(r["song_maker_owner"] or "").strip().casefold() == TARGET for r in rows)
    _guard(len(foreign) == EXPECTED_FOREIGN and
           len(rows) - len(foreign) == EXPECTED_UNPROVEN and target_handoff == 0,
           "LEGACY_HANDOFF_EVIDENCE_CHANGED")
    _guard(all(registered.get(owner) is True for owner in foreign),
           "LEGACY_FOREIGN_OWNER_NOT_ACTIVE")
    return {"total": len(rows), "target": EXPECTED_UNPROVEN,
            "foreign_preserved": len(foreign), "nonterminal": 0}


def install_legacy(revision: str) -> dict:
    """Add missing column and claim owners atomically under a SQLite writer lock."""
    from production.runtime import BASE, DATA, atomic_json
    _guard(bool(re.fullmatch("[a-f0-9]{40}", revision)), "INVALID_REVISION")
    _guard(shutil.disk_usage("/").free >= 5_000_000_000, "DISK_RESERVE_UNAVAILABLE")
    scene = DATA / audio.SCENE_REL
    source = audio.trusted_db(DATA)
    plan = _preflight(source, scene)
    journal = BASE / "transactions/haseeb1-audio-downloader-owner" / revision
    _guard(not journal.exists(), "TRANSACTION_ALREADY_EXISTS")
    journal.mkdir(parents=True, mode=0o700, exist_ok=False)
    atomic_json(journal / "intent.json", {
        "target": TARGET, "revision": revision, "mode": "LEGACY_SCHEMA_CLAIM",
        "plan": plan, "services": "UNCHANGED", "auto_script": "OFFLINE",
    })
    # Online SQLite backup includes WAL; preserve original missing-column schema.
    audio.backup_sqlite(source, journal / "backup/jobs.sqlite3")
    con = audio.open_db(source)
    try:
        con.execute("BEGIN IMMEDIATE")
        locked = _preflight(source, scene)
        _guard(locked == plan, "LEGACY_PREFLIGHT_CHANGED")
        before = {str(r["job_id"]): dict(r) for r in con.execute("SELECT * FROM jobs")}
        _guard(len(before) == EXPECTED_TOTAL, "LEGACY_DUPLICATE_JOB_ID")
        # Additive ALTER is transactional in SQLite. Nothing is dropped or reset.
        con.execute("ALTER TABLE jobs ADD COLUMN owner_username TEXT NOT NULL DEFAULT ''")
        target_count = 0
        foreign_count = 0
        claims = []
        for jid, original in before.items():
            handoff = str(original["song_maker_owner"] or "").strip().casefold()
            target_owner = handoff if handoff and handoff != TARGET else TARGET
            if target_owner == TARGET:
                target_count += 1
            else:
                foreign_count += 1
            status = original["status"]
            row = con.execute(
                """UPDATE jobs SET owner_username=?
                   WHERE job_id=? AND owner_username=''
                     AND status=? AND coalesce(song_maker_owner,'')=coalesce(?,'')""",
                (target_owner, jid, status, original["song_maker_owner"]),
            )
            _guard(row.rowcount == 1, "LEGACY_CONCURRENT_STATUS_OR_OWNER")
            claims.append({"job_id": jid, "status": status,
                           "handoff": original["song_maker_owner"], "owner": target_owner})
        _guard(target_count == EXPECTED_UNPROVEN and foreign_count == EXPECTED_FOREIGN,
               "LEGACY_CLAIM_COUNTS_CHANGED")
        after = {str(r["job_id"]): dict(r) for r in con.execute("SELECT * FROM jobs")}
        _guard(set(before) == set(after), "LEGACY_JOB_KEYS_CHANGED")
        for jid in before:
            _guard(all(after[jid][key] == value for key, value in before[jid].items()),
                   "LEGACY_NONOWNER_FIELD_CHANGED")
        _guard(all(after[c["job_id"]]["owner_username"] == c["owner"] for c in claims),
               "LEGACY_OWNER_READBACK_FAILED")
        # Root-only exact before-images for guarded owner-only compensation.
        atomic_json(journal / "legacy-claims.json", {"claims": claims, "target": TARGET})
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        atomic_json(journal / "failure.json", {"state": "ROLLED_BACK_BEFORE_COMMIT",
                                              "auto_script": "OFFLINE"})
        raise
    finally:
        con.close()
    receipt = {
        "state": "OWNERSHIP_VERIFIED", "total_jobs": EXPECTED_TOTAL,
        "claimed_now": EXPECTED_UNPROVEN, "preserved_foreign": EXPECTED_FOREIGN,
        "nonterminal_jobs": 0, "schema_status": "OWNER_COLUMN_ADDED",
        "services": "UNCHANGED", "media": "UNCHANGED", "preferences": "UNCHANGED",
        "google_drive": "UNCHANGED", "auto_script": "OFFLINE",
        "rollback": "OWNER_ONLY_COMPENSATION_AVAILABLE",
    }
    atomic_json(journal / "receipt.json", receipt)
    print(json.dumps({"HASEEB1_AUDIO_ASSIGNMENT": receipt}, sort_keys=True), flush=True)
    return receipt


def rollback_legacy(revision: str) -> dict:
    """Compensate only if all 20 jobs still match their claimed status/handoff."""
    from production.runtime import BASE, DATA, atomic_json
    _guard(bool(re.fullmatch("[a-f0-9]{40}", revision)), "INVALID_REVISION")
    journal = BASE / "transactions/haseeb1-audio-downloader-owner" / revision
    path = journal / "legacy-claims.json"
    _guard(path.is_file() and not path.is_symlink(), "LEGACY_CLAIM_RECEIPT_MISSING")
    record = json.loads(path.read_text())
    claims = record.get("claims")
    _guard(record.get("target") == TARGET and isinstance(claims, list) and
           len(claims) == EXPECTED_TOTAL, "LEGACY_ROLLBACK_CLAIMS_INVALID")
    ids = [c["job_id"] for c in claims]
    _guard(len(set(ids)) == EXPECTED_TOTAL, "LEGACY_ROLLBACK_DUPLICATE")
    source = audio.trusted_db(DATA)
    with audio.open_db(source) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            for claim in claims:
                current = con.execute(
                    "SELECT owner_username,status,song_maker_owner FROM jobs WHERE job_id=?",
                    (claim["job_id"],),
                ).fetchone()
                _guard(current is not None and current["owner_username"] == claim["owner"]
                       and current["status"] == claim["status"]
                       and current["song_maker_owner"] == claim["handoff"]
                       and str(current["status"]).strip().casefold() in audio.TERMINAL,
                       "LEGACY_ROLLBACK_OWNER_OR_STATUS_CONFLICT")
            for claim in claims:
                updated = con.execute(
                    "UPDATE jobs SET owner_username='' WHERE job_id=? AND owner_username=?",
                    (claim["job_id"], claim["owner"]),
                )
                _guard(updated.rowcount == 1, "LEGACY_ROLLBACK_CONCURRENT_CHANGE")
            con.execute("COMMIT")
        except BaseException:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
    receipt = {"state": "COMPENSATED", "claims_reverted": EXPECTED_TOTAL,
               "schema": "ADDITIVE_COLUMN_RETAINED", "services": "UNCHANGED",
               "media": "UNCHANGED", "auto_script": "OFFLINE"}
    atomic_json(journal / "rollback.json", receipt)
    return receipt

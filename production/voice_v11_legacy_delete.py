#!/usr/bin/env python3
"""Voice Studio V1.1: approved exact-scope legacy terminal job cleanup.

No imports from the Voice app (avoids startup migrations/workers). This is a
self-contained script for the approved VPS. Starts in read-only preview mode.
"""
import argparse
import json
import re
import shutil
import sqlite3
from pathlib import Path

EXPECTED = {
    "Kokoro": {"db": "kokoro/data/kokoro_jobs.sqlite3", "count": 7,
               "status": {"completed": 7}, "prefix": "KOJ-",
               "files": ("input_file", "wav_file")},
    "Edge": {"db": "edge/data/edge_jobs.sqlite3", "count": 6,
             "status": {"completed": 5, "failed": 1}, "prefix": "EDJ-",
             "files": ("input_file", "mp3_file", "wav_file", "srt_file")},
}
IN_FLIGHT = {"pending", "transferring", "uploading", "retry_pending", "blocked",
             "queued", "waiting_generation", "waiting_worker", "processing"}
COMMIT_PHRASE = "VOICE-V1.1-KOKORO-7-EDGE-6"


class GuardError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise GuardError(message)


def scan(root: Path):
    """Open exclusively pre-existing databases. No DB file is created."""
    require(root.is_dir() and not root.is_symlink(), "VOICE_ROOT_MISSING_OR_SYMLINK")
    summary = {}
    for engine, spec in EXPECTED.items():
        db_path = root / spec["db"]
        require(db_path.is_file() and not db_path.is_symlink(), f"{engine}:DB_MISSING_OR_SYMLINK")
        job_root = db_path.parent / "jobs"
        require(job_root.is_dir() and not job_root.is_symlink(), f"{engine}:JOBS_ROOT_MISSING_OR_SYMLINK")
        db = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=4)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA query_only=ON")
            cols = {r[1] for r in db.execute("PRAGMA table_info(jobs)")}
            require(cols and "owner_username" not in cols, f"{engine}:SCHEMA_NOT_LEGACY")
            required = {"job_id", "status", "delivery_status", *spec["files"]}
            require(required.issubset(cols), f"{engine}:UNEXPECTED_SCHEMA")
            rows = [dict(x) for x in db.execute("SELECT * FROM jobs ORDER BY job_id")]
            require(len(rows) == spec["count"], f"{engine}:COUNT_MISMATCH")
            states = {}
            ids = set()
            folders = []
            for r in rows:
                state = str(r["status"] or "")
                states[state] = states.get(state, 0) + 1
                jid = str(r["job_id"] or "")
                require(re.fullmatch(re.escape(spec["prefix"]) + r"[A-F0-9]{16}", jid),
                        f"{engine}:UNEXPECTED_JOB_ID")
                require(jid not in ids, f"{engine}:DUPLICATE_ID")
                ids.add(jid)
                delivery = str(r["delivery_status"] or "").lower()
                require(delivery not in IN_FLIGHT,
                        f"{engine}:DELIVERY_PENDING_OR_RETRY")
                require(delivery in {"delivered", "cleaned", "not_applicable", "legacy_local", ""},
                        f"{engine}:UNRECOGNIZED_DELIVERY_STATE")
                folder = job_root / jid
                require(not folder.is_symlink(), f"{engine}:JOB_FOLDER_SYMLINK")
                for key in spec["files"]:
                    val = str(r.get(key) or "")
                    if val:
                        p = Path(val)
                        require(p.is_absolute() and ".." not in p.parts and p.is_relative_to(folder),
                                f"{engine}:MEDIA_REFERENCE_OUTSIDE_OWN_JOB")
                if folder.exists():
                    require(folder.is_dir(), f"{engine}:JOB_FOLDER_NOT_DIR")
                    for item in folder.rglob("*"):
                        require(not item.is_symlink(), f"{engine}:MEDIA_SYMLINK")
                        require(item.is_file() or item.is_dir(), f"{engine}:NONSTANDARD_MEDIA")
                    folders.append(folder)
            require(states == spec["status"], f"{engine}:STATUS_MISMATCH")
            for table, in db.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                if table != "jobs":
                    fks = db.execute(f'PRAGMA foreign_key_list("{table.replace(chr(34), chr(34)*2)}")')
                    require(not any(row[2] == "jobs" for row in fks),
                            f"{engine}:OTHER_TABLE_REFERENCES_JOBS")
            summary[engine] = {"db": db_path, "job_ids": ids, "job_folders": folders,
                               "count": len(rows), "statuses": states, "media_folders": len(folders)}
        finally:
            db.close()
    return summary


def apply(root, original):
    for engine, spec in EXPECTED.items():
        db_path = original[engine]["db"]
        db = sqlite3.connect(str(db_path), timeout=3, isolation_level=None)
        try:
            db.execute("PRAGMA busy_timeout=3000")
            db.execute("BEGIN IMMEDIATE")
            cols = {r[1] for r in db.execute("PRAGMA table_info(jobs)")}
            require("owner_username" not in cols, f"{engine}:SCHEMA_CHANGED")
            rows = list(db.execute("SELECT job_id,status,delivery_status FROM jobs"))
            require(len(rows) == spec["count"] and
                    {x[0] for x in rows} == original[engine]["job_ids"],
                    f"{engine}:JOBS_CHANGED_SINCE_PREFLIGHT")
            states = {}
            for _, status, delivery in rows:
                states[status] = states.get(status, 0) + 1
                require(str(delivery or "").lower() not in IN_FLIGHT,
                        f"{engine}:TRANSFER_STARTED_SINCE_PREFLIGHT")
            require(states == spec["status"], f"{engine}:STATUSES_CHANGED")
            for jid in sorted(original[engine]["job_ids"]):
                cur = db.execute("DELETE FROM jobs WHERE job_id=?", (jid,))
                require(cur.rowcount == 1, f"{engine}:DELETE_COUNT_MISMATCH")
            require(db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0,
                    f"{engine}:VERIFICATION_FAILED")
            db.execute("COMMIT")
        except Exception:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()
    removed = {}
    for engine, info in original.items():
        removed[engine] = 0
        for folder in info["job_folders"]:
            require(folder.is_dir() and not folder.is_symlink(),
                    f"{engine}:MEDIA_CHANGED_AFTER_DB_CLEANUP")
            for item in folder.rglob("*"):
                require(not item.is_symlink(), f"{engine}:SYMLINK_INSERTED")
            shutil.rmtree(folder)
            removed[engine] += 1
    return removed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/var/lib/fresh-website/voice-studio"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    try:
        require(not args.apply or args.confirm == COMMIT_PHRASE, "EXPLICIT_CONFIRMATION_REQUIRED")
        result = scan(args.root)
        print(json.dumps({"preflight": "PASS", "mode": "APPLY" if args.apply else "READ_ONLY",
                          "jobs": {e: {k: v for k, v in d.items() if k in ("count", "statuses", "media_folders")}
                                   for e, d in result.items()}}, sort_keys=True), flush=True)
        if args.apply:
            removed = apply(args.root, result)
            print(json.dumps({"legacy_v11_cleanup": "COMPLETE", "deleted_rows": {"Kokoro": 7, "Edge": 6},
                              "removed_verified_job_folders": removed}, sort_keys=True))
    except (GuardError, sqlite3.Error, OSError) as exc:
        print(json.dumps({"legacy_v11_cleanup": "BLOCKED", "reason": str(exc)}))
        raise SystemExit(2)


if __name__ == "__main__":
    main()

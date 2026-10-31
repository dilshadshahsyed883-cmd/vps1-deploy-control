"""Read-only, sanitized Song Uploader worker diagnosis, via protected-main lane.

Never outputs raw job descriptions, credentials, token strings, file paths,
channel identities, user data or exception messages. Does not restart services.
"""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

UNITS = ("web", "worker", "scheduler")
CATEGORY_PATTERNS = (
    ("DB_SCHEMA", r"no such (column|table)|has no column|schema mismatch"),
    ("DB_BUSY", r"database is locked|database is busy|busy timeout"),
    ("DB_ERROR", r"sqlite|database|foreign key|constraint failed"),
    ("AUTH", r"unauthorized|oauth|credentials|refresh.token|authentication|invalid.grant|token"),
    ("NETWORK", r"connection|timeout|network|proxy|socket|dns|ssl|tls|temporary failure"),
    ("FILES", r"no such file|not a directory|file not found|folder does not exist|permission denied"),
    ("CODE_TYPE", r"nonetype|attributeerror|typeerror|missing .*argument|not iterable|unsupported operand"),
    ("CODE_NAME", r"nameerror|unboundlocalerror|is not defined"),
    ("RATE_LIMIT", r"quota|rate.limit|too many requests|429"),
    ("SOURCE_STATE", r"already claimed|source|waiting.network|not in queue|profile|songmaker|song maker"),
)


def classify(message: str) -> str:
    lowered=str(message or "").lower()
    if not lowered: return "NONE"
    for name,pattern in CATEGORY_PATTERNS:
        if re.search(pattern,lowered): return name
    return "OTHER"



# Non-sensitive, allowlisted refinements of the original SOURCE_STATE bucket.
# Never print the exception itself, filenames, IDs, user text or arbitrary hashes.
SOURCE_DETAIL_PATTERNS = (
    ("SOURCE_MAPPING_MISSING", r"source mapping.{0,50}miss|source reference.{0,50}miss"),
    ("SONGMAKER_MEDIA_MISSING", r"local song maker.{0,60}missing|songmaker.{0,40}missing"),
    ("SOURCE_FILE_MISSING", r"source.{0,70}(missing|not found|unavailable)|missing.{0,30}source"),
    ("SOURCE_CHANGED", r"source.{0,60}chang|source.{0,60}mismatch"),
    ("PROFILE_MISSING", r"profile.{0,60}(missing|not found|deleted)"),
    ("PROFILE_DISABLED", r"profile.{0,60}(disabled|paused|inactive)"),
    ("CLAIM_CONFLICT", r"already claimed|claim.{0,35}(lost|held|lease)"),
    ("NETWORK_QUEUE_STATE", r"waiting.network|network wait"),
    ("QUEUE_STATE", r"not in queue|not queued|queue"),
    ("PROFILE_OTHER", r"profile"),
    ("SOURCE_OTHER", r"source|songmaker|song maker"),
)


def classify_source_detail(message: str) -> str:
    if classify(message) != "SOURCE_STATE":
        return "NOT_SOURCE_STATE"
    lowered = str(message or "").lower()
    for code, pattern in SOURCE_DETAIL_PATTERNS:
        if re.search(pattern, lowered):
            return code
    return "UNMAPPED_SOURCE_STATE"



def heartbeat_age_bucket(value: str) -> str:
    # Only a bounded age category; do not expose exact timestamps, host details.
    if not str(value or "").strip():
        return "ABSENT"
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return "UNKNOWN"
        seconds = (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()
        if seconds <= 15:
            return "RECENT"
        if seconds <= 60:
            return "DELAYED"
        return "STALE"
    except (ValueError, TypeError, OverflowError):
        return "UNKNOWN"


def unit_state(suffix: str) -> str:
    if suffix not in UNITS: return "UNKNOWN"
    unit=f"vps1-production-lum-song-{suffix}.service"
    try:
        p=subprocess.run(["systemctl","is-active",unit],
                         capture_output=True,text=True,timeout=5,check=False)
        return "ACTIVE" if p.returncode==0 else "INACTIVE"
    except (OSError,subprocess.SubprocessError):
        return "UNKNOWN"


def collect(state: Path) -> dict:
    report={"mode":"READ_ONLY","instance":"song","units":{n:unit_state(n) for n in UNITS},
            "db":"UNAVAILABLE","worker_error":"UNKNOWN","scheduler_error":"UNKNOWN"}
    db=state/"auto_engine_v6/auto_engine_v6.sqlite3"
    if db.is_symlink() or not db.is_file():
        return report
    try:
        with sqlite3.connect(db.as_uri()+"?mode=ro",uri=True,timeout=4) as con:
            con.execute("PRAGMA query_only=ON")
            cols={str(row[1]) for row in con.execute("PRAGMA table_info(engine_state)")}
            if not {"last_worker_error","last_scheduler_error","running","scheduler_armed","last_worker_heartbeat","last_scheduler_heartbeat"}.issubset(cols):
                report["db"]="MISSING_COLUMNS"
                return report
            row=con.execute(
                "SELECT last_worker_error,last_scheduler_error,running,scheduler_armed, "
                "last_worker_heartbeat,last_scheduler_heartbeat "
                "FROM engine_state WHERE singleton=1").fetchone()
            if row is None:
                report["db"]="MISSING_STATE"
                return report
            report.update(db="READ_ONLY_OK",worker_error=classify(row[0]),
                          worker_source_detail=classify_source_detail(row[0]),
                          scheduler_error=classify(row[1]),
                          engine_running=bool(row[2]),scheduler_armed=bool(row[3]),
                          worker_error_present=bool(row[0]),scheduler_error_present=bool(row[1]),
                          worker_heartbeat_age=heartbeat_age_bucket(row[4]),
                          scheduler_heartbeat_age=heartbeat_age_bucket(row[5]))
            core=state/"core/data/uploader.db"
            if core.is_file() and not core.is_symlink():
                with sqlite3.connect(core.as_uri()+"?mode=ro",uri=True,timeout=4) as q:
                    q.execute("PRAGMA query_only=ON")
                    counts=q.execute(
                        "SELECT UPPER(status),COUNT(*) FROM videos "
                        "WHERE UPPER(status) IN ('CLAIMED','UPLOADING','WAITING_NETWORK','QUEUED') "
                        "GROUP BY UPPER(status)").fetchall()
                    report["job_counts"]={status:int(n) for status,n in counts}
    except (sqlite3.Error,OSError,ValueError,TypeError):
        report["db"]="UNREADABLE"
    return report


def run(state: Path) -> None:
    print("SONG_WORKER_READONLY_DIAGNOSTIC="+json.dumps(collect(state),sort_keys=True),flush=True)

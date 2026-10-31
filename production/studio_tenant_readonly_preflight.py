"""VPS_1 three-Studio read-only owner/isolation deployment preflight.

No database, OAuth, media or Drive mutation. Output only aggregate counts.
The deployment must separately guard active upload workers and write rollback
receipts; passing this audit never authorizes a release by itself.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

INSTANCE_RELATIVE = {
    "song": "local-upload-manager/lum-song/core/data/uploader.db",
    "short": "local-upload-manager/lum-shortstudio/core/data/uploader.db",
    "copyright": "copyright-uploader/lum-copyright/core/data/uploader.db",
}
TERMINAL = ("UPLOADED", "PUBLISHED", "COMPLETED", "FAILED", "CANCELLED", "SKIPPED")


def _has_table(connection, name):
    return bool(connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone())


def _count(connection, sql):
    return int(connection.execute(sql).fetchone()[0])


def audit_database(path):
    """Read only. Fail closed on missing/unreadable schemas, never guess owners."""
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        return {"present": False, "ready": False, "reason": "DB_MISSING_OR_SYMLINK"}
    con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)
    try:
        con.execute("PRAGMA query_only=ON")
        required = ("channels", "channel_groups", "sheet_sources", "videos")
        if any(not _has_table(con, tab) for tab in required):
            return {"present": True, "ready": False, "reason": "CORE_SCHEMA_DRIFT"}
        owner_present = _has_table(con, "studio_entity_owners")
        video_columns = {row[1] for row in con.execute("PRAGMA table_info(videos)")}
        if "status" not in video_columns:
            return {"present": True, "ready": False, "reason": "VIDEO_SCHEMA_DRIFT"}
        busy = _count(con, """SELECT COUNT(*) FROM videos
            WHERE UPPER(COALESCE(status,'')) IN
            ('CLAIMED','UPLOADING','THUMBNAIL_UPLOADING','PROCESSING','IN_PROGRESS')""")
        pending = _count(con, """SELECT COUNT(*) FROM videos
            WHERE UPPER(COALESCE(status,'')) NOT IN
            ('UPLOADED','PUBLISHED','COMPLETED','FAILED','CANCELLED','SKIPPED')""")
        totals = {key: _count(con,"SELECT COUNT(*) FROM "+table)
                  for key,table in (("channels","channels"),
                                    ("groups","channel_groups"),
                                    ("sheets","sheet_sources"))}
        if _has_table(con,"auto_engine_profiles"):
            totals["profiles"] = _count(con,"SELECT COUNT(*) FROM auto_engine_profiles")
        else:
            totals["profiles"] = 0
        unassigned = {}
        for kind,table in (("channel","channels"),("group","channel_groups"),
                           ("sheet","sheet_sources"),
                           ("profile","auto_engine_profiles")):
            if not _has_table(con,table):
                unassigned[kind] = 0
            elif not owner_present:
                unassigned[kind] = totals[kind+"s"] if kind!="channel" else totals["channels"]
            else:
                unassigned[kind] = _count(con, f"""
                    SELECT COUNT(*) FROM {table} t
                    LEFT JOIN studio_entity_owners o ON o.entity_kind='{kind}'
                      AND o.entity_id=t.id
                    WHERE o.owner_username IS NULL""")
        if owner_present and _has_table(con,"channel_group_members"):
            mismatched = _count(con, """
                SELECT COUNT(*) FROM channel_group_members gm
                JOIN studio_entity_owners g ON g.entity_kind='group'
                  AND g.entity_id=gm.group_id
                LEFT JOIN studio_entity_owners c ON c.entity_kind='channel'
                  AND c.entity_id=gm.channel_db_id
                WHERE c.owner_username IS NULL OR
                  LOWER(g.owner_username)<>LOWER(c.owner_username)""")
        else:
            mismatched = 0
        return {"present": True, "ready": busy == 0 and mismatched == 0,
                "schema": "PASS", "registry": "PRESENT" if owner_present else "LEGACY",
                "busy": busy, "nonterminal": pending, "totals": totals,
                "unassigned": unassigned, "owned_group_mismatch": mismatched,
                "legacy_requires_explicit_admin_assignment": any(unassigned.values())}
    finally:
        con.close()


def audit_all(data_root):
    root = Path(data_root)
    findings = {name: audit_database(root / relative)
                for name,relative in INSTANCE_RELATIVE.items()}
    return {"preflight": "PASS" if all(v.get("ready") for v in findings.values())
            else "BLOCKED", "mode": "READ_ONLY_NO_MUTATION",
            "shared_google_drive": "UNCHANGED", "auto_script": "OFFLINE",
            "instances": findings}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--require-ready", action="store_true")
    args = parser.parse_args()
    result = audit_all(args.data_root)
    print(json.dumps(result, sort_keys=True))
    if args.require_ready and result["preflight"] != "PASS":
        raise SystemExit(2)


if __name__ == "__main__":
    main()

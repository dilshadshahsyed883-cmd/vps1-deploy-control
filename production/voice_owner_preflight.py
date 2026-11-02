#!/usr/bin/env python3
"""Read-only Voice Studio legacy tenant audit.

Supply the three verified production database paths via --clone, --kokoro
and --edge. No database migrations, ownership assignment, or media writes.
A release remains blocked while any Voice job/profile has unverified ownership.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from urllib.parse import quote


def audit(path: Path, engine: str) -> dict:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"{engine}: database missing or symlinked")
    uri = "file:" + quote(str(path.resolve()), safe="/") + "?mode=ro"
    result = {"engine": engine, "tables": {}}
    with sqlite3.connect(uri, uri=True, timeout=5) as con:
        con.execute("PRAGMA query_only=ON")
        for table in ("jobs", "profiles") if engine == "clone" else ("jobs",):
            columns = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
            if not columns:
                raise RuntimeError(f"{engine}: {table} table missing")
            counts = {"total": int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])}
            if "owner_username" not in columns:
                counts["owner_column_present"] = False
                counts["unassigned"] = counts["total"]
            else:
                counts["owner_column_present"] = True
                counts["unassigned"] = int(con.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE TRIM(COALESCE(owner_username,''))=''"
                ).fetchone()[0])
            result["tables"][table] = counts
        if engine == "clone":
            # Old global categories had no owner. "General" is an
            # automatically seeded system default; all other legacy entries
            # need independent attribution before a tenant cutover.
            category_cols = {row[1] for row in con.execute("PRAGMA table_info(categories)")}
            result["unassigned_legacy_categories"] = (
                int(con.execute("SELECT COUNT(*) FROM categories WHERE name<>'General' COLLATE NOCASE").fetchone()[0])
                if "name" in category_cols else 0
            )
            # A nonempty owner string alone is not enough. Jobs referencing a
            # different user's profile would disappear in the scoped join.
            job_cols = {row[1] for row in con.execute("PRAGMA table_info(jobs)")}
            profile_cols = {row[1] for row in con.execute("PRAGMA table_info(profiles)")}
            if {"owner_username", "profile_id"}.issubset(job_cols) and {"owner_username", "profile_id"}.issubset(profile_cols):
                result["job_profile_owner_conflicts"] = int(con.execute(
                    "SELECT COUNT(*) FROM jobs j LEFT JOIN profiles p ON j.profile_id=p.profile_id "
                    "WHERE p.profile_id IS NULL OR j.owner_username<>p.owner_username"
                ).fetchone()[0])
            else:
                result["job_profile_owner_conflicts"] = None
    return result


def report(databases: dict[str, Path]) -> tuple[dict, bool]:
    findings = [audit(path, name) for name, path in databases.items()]
    blocked = any(
        # V1.1: an empty legacy jobs table has no owners to attribute.
        # A missing owner column is not evidence of unowned DATA when count=0.
        entry["unassigned"] > 0
        for engine in findings for entry in engine["tables"].values()
    ) or any(
        item["engine"] == "clone" and (
            item["unassigned_legacy_categories"] > 0 or
            (item["job_profile_owner_conflicts"] is None and (
                item["tables"]["jobs"]["total"] > 0 or
                item["tables"]["profiles"]["total"] > 0
            )) or
            (item["job_profile_owner_conflicts"] or 0) > 0
        ) for item in findings
    )
    return {"voice_owner_preflight": "BLOCKED" if blocked else "READY",
            "engines": findings,
            "action": "V1.1: zero-row legacy tables need no data attribution, even if owner column is absent; verify all nonempty owners/categories and profile links. No data writes or autoassignment. Structural READY alone is not release approval."}, blocked


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("clone", "kokoro", "edge"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    try:
        output, blocked = report({name: getattr(args,name) for name in ("clone","kokoro","edge")})
    except (RuntimeError, sqlite3.Error) as exc:
        print(json.dumps({"voice_owner_preflight": "BLOCKED", "reason": str(exc)}))
        return 2
    print(json.dumps(output, sort_keys=True))
    return 2 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())

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
    return result


def report(databases: dict[str, Path]) -> tuple[dict, bool]:
    findings = [audit(path, name) for name, path in databases.items()]
    blocked = any(
        not entry["owner_column_present"] or entry["unassigned"] > 0
        for engine in findings for entry in engine["tables"].values()
    )
    return {"voice_owner_preflight": "BLOCKED" if blocked else "READY",
            "engines": findings,
            "action": "Verify legacy owners from independent records; do not autoassign or delete."}, blocked


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

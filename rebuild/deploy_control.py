#!/usr/bin/env python3
"""Non-executing VPS1 app impact planner and persistent FIFO deployment intents.

This tool never connects to VPS, executes a deployment, or authorizes cutover.
"""
import argparse
import json
import re
import sqlite3
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
SHA = re.compile(r"^[a-f0-9]{40}$")
IDENT = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
ALIASES = {"kokoro": "voice-studio"}
# Independently reviewed new-app lanes must not join legacy ten-app `all`
# builds/cutover or acquire intent-queue execution privileges implicitly.
DEDICATED_APPS = {
    "song-maker": "production.song_maker_release; HOST_HELPER_UPGRADE_REQUIRED; real Drive acceptance pending"
}
FLAGS = ("production_deploy_enabled", "fresh_install_enabled",
         "automatic_database_migrations", "legacy_database_import",
         "legacy_queue_import", "legacy_media_import")
OUTCOMES = {"verified": "VERIFIED", "failed": "FAILED", "rolled_back": "ROLLED_BACK"}
# Changes to these packaging/registry contracts require fresh source artifacts
# for every active app, never an automatic production deployment.
PACKAGING_ALL = {
    "rebuild/builder.py", "rebuild/fresh_vps.py", "rebuild/groups.json",
    "rebuild/install-profiles.json", "scripts/stage_fresh_app.py",
    ".github/workflows/fresh-vps-app-artifacts.yml",
}


def registry():
    cfg = json.loads((ROOT / "rebuild/groups.json").read_text())
    profiles = json.loads((ROOT / "rebuild/install-profiles.json").read_text())["groups"]
    if any(cfg.get(flag) is not False for flag in FLAGS):
        raise ValueError("cutover controls are not disabled: fail closed")
    groups = {g["id"]: g for g in cfg["groups"]}
    if len(groups) != len(cfg["groups"]) or set(groups) != set(profiles):
        raise ValueError("invalid application registry or service coverage")
    if not all(IDENT.fullmatch(x) and IDENT.fullmatch(g["lock"]) for x, g in groups.items()):
        raise ValueError("invalid application or concurrency lock")
    offline = {x for x, g in groups.items() if g.get("deployment_mode") == "offline"}
    active = set(groups) - offline
    if offline != set(cfg["offline_applications"]) or len(active) != cfg["active_application_target"]:
        raise ValueError("offline/active application registry mismatch")
    for app, group in groups.items():
        if not set(group["needs"]) <= set(groups):
            raise ValueError("unregistered dependency")
        if app in active and any(x in offline for x in group["needs"]):
            raise ValueError("active application depends on offline application")
    return groups, profiles, active, offline


def ordered(groups, active):
    left, result = set(active), []
    while left:
        ready = sorted(x for x in left if set(groups[x]["needs"]) <= set(result))
        if not ready:
            raise ValueError("cyclic or unresolved dependency graph")
        result.extend(ready)
        left.difference_update(ready)
    return result


def safe_path(value):
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("invalid git path")
    if value.startswith("/") or any(x in (".", "..", "") for x in value.split("/")):
        raise ValueError("unsafe git path")
    return str(PurePosixPath(value))


def plan(paths):
    groups, profiles, active, offline = registry()
    changed = sorted({safe_path(x.strip()) for x in paths if x.strip()})
    direct, inactive, snapshots, dedicated = set(), set(), set(), set()
    validate_all, shell = False, False
    for path in changed:
        parts = PurePosixPath(path).parts
        if parts[0] == "apps":
            if len(parts) < 3:
                raise ValueError("ambiguous app source change")
            # Standalone Kokoro is a source snapshot. The deployed Voice
            # Studio release reads apps/voice-studio/, NOT apps/kokoro/.
            # Block stale releases until an explicit sync updates app-owned code.
            if parts[1] in ALIASES:
                snapshots.add(ALIASES[parts[1]])
                continue
            app = parts[1]
            if app in DEDICATED_APPS:
                dedicated.add(app)
                continue
            if app not in groups:
                raise ValueError("unregistered app source: " + path)
            (inactive if app in offline else direct).add(app)
        elif parts[0] in ("shared", "infrastructure", "rebuild"):
            validate_all = True
            shell |= path.startswith("rebuild/shell/") or path == "rebuild/website_shell.py"
        elif path.startswith(".github/workflows/fresh-vps-"):
            validate_all = True
    validation = set(active) if validate_all else (set(direct) | snapshots)
    while True:
        more = {app for app in active - validation
                if set(groups[app]["needs"]) & validation}
        if not more:
            break
        validation |= more
    package_all = bool(set(changed) & PACKAGING_ALL)
    releases = (set(active) if package_all else set(direct)) - snapshots
    rank = {app: n for n, app in enumerate(ordered(groups, active))}
    sort = lambda x: sorted(x, key=lambda name: rank[name])
    return {
        "schema_version": 1,
        "changed_paths": changed,
        "source_release_candidates": sort(releases),
        "all_app_source_packaging_required": package_all,
        "validation_apps": sort(validation),
        "offline_app_changes": sorted(inactive),
        "dedicated_app_changes": sorted(dedicated),
        "source_snapshot_sync_required": sorted(snapshots),
        "shell_build_required": shell,
        "runtime_blockers": {
            **{app: profiles[app].get("pending", "no services configured")
               for app in sort(direct)
               if profiles[app].get("pending") or not profiles[app]["services"]},
            **{app: DEDICATED_APPS[app] for app in sorted(dedicated)},
        },
        "production_deployment_authorized": False,
        "deploy_command_executed": False,
    }


class IntentQueue:
    """One host-wide in-flight intent; SQLite survives multiple chat requests."""

    def __init__(self, path):
        self.path = Path(path).resolve()
        if not self.path.is_relative_to(Path("/tmp")) or self.path == Path("/tmp"):
            raise ValueError("prototype intent queue must live below /tmp")
        registry()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                app TEXT NOT NULL,
                revision TEXT NOT NULL,
                lock_group TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'QUEUED',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                claimed_at TEXT,
                finished_at TEXT,
                UNIQUE(app,revision))""")
            db.execute("CREATE INDEX IF NOT EXISTS req_status ON requests(status,id)")

    def db(self):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=15000")
        return db

    def enqueue(self, app, revision):
        groups, _, active, _ = registry()
        if app not in active or groups[app].get("parallel_owner"):
            raise ValueError("unregistered, offline, or externally owned app")
        if not SHA.fullmatch(revision):
            raise ValueError("commit must be full immutable Git SHA")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO requests(app,revision,lock_group) VALUES(?,?,?)",
                       (app, revision, groups[app]["lock"]))
            row = db.execute("SELECT * FROM requests WHERE app=? AND revision=?",
                             (app, revision)).fetchone()
            db.commit()
            return dict(row)

    def claim(self):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM requests WHERE status='CLAIMED' LIMIT 1").fetchone():
                db.commit()
                return None
            row = db.execute("SELECT * FROM requests WHERE status='QUEUED' ORDER BY id LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE requests SET status='CLAIMED', claimed_at=CURRENT_TIMESTAMP WHERE id=?",
                           (row["id"],))
                row = db.execute("SELECT * FROM requests WHERE id=?", (row["id"],)).fetchone()
            db.commit()
            return dict(row) if row else None

    def finish(self, job_id, outcome):
        if outcome not in OUTCOMES:
            raise ValueError("unknown verification outcome")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE requests SET status=?,finished_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND status='CLAIMED'", (OUTCOMES[outcome], job_id)).rowcount
            if changed != 1:
                db.rollback()
                raise ValueError("job is not currently claimed")
            row = db.execute("SELECT * FROM requests WHERE id=?", (job_id,)).fetchone()
            db.commit()
            return dict(row)

    def list(self):
        with self.db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM requests ORDER BY id")]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan").add_argument("--paths", required=True)
    for name in ("enqueue", "claim", "finish", "list"):
        child = sub.add_parser(name)
        child.add_argument("--db", required=True)
        if name == "enqueue":
            child.add_argument("--app", required=True)
            child.add_argument("--sha", required=True)
        if name == "finish":
            child.add_argument("--job-id", type=int, required=True)
            child.add_argument("--result", choices=sorted(OUTCOMES), required=True)
    args = parser.parse_args()
    if args.command == "plan":
        result = plan(Path(args.paths).read_text().splitlines())
    else:
        queue = IntentQueue(args.db)
        if args.command == "enqueue":
            result = queue.enqueue(args.app, args.sha)
        elif args.command == "claim":
            result = queue.claim()
        elif args.command == "finish":
            result = queue.finish(args.job_id, args.result)
        else:
            result = queue.list()
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

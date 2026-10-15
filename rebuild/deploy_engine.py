#!/usr/bin/env python3
"""Durable VPS1 deployment executor: safe-by-default, simulation-only backend.

Many processes may enqueue against the same persistent SQLite file. Claiming is
atomic and HOST-WIDE, regardless of app or originating ChatGPT tab. A stale
claim freezes the host queue until the actual release is reconciled.
NO production executor, SSH, service restart or cutover is shipped here.
"""
import argparse
import json
from contextlib import contextmanager
import os
import re
import sqlite3
import stat
import time
import uuid
from pathlib import Path

from rebuild.deploy_control import registry
from rebuild import release_transaction

SHA = re.compile(r"^[a-f0-9]{40}$")
TERMINAL = {"VERIFIED", "FAILED", "ROLLED_BACK"}
BLOCKING = {"CLAIMED", "NEEDS_ATTENTION"}
PENDING = {"QUEUED", "RETRY_WAIT"}
ALL_STATES = TERMINAL | BLOCKING | PENDING
RETRY_BASE_SECONDS = 30
RETRY_MAX_SECONDS = 900


def verify_release_candidate(app, revision, manifest_path):
    """Integrity admission for a locally obtained artifact; NOT GitHub provenance.

    The Tab 4 host handoff must first independently authenticate the GitHub
    run/commit and download provenance. This guards tampering and wrong-app
    binaries after that retrieval; it does not replace that trust decision.
    """
    from rebuild import builder
    if not SHA.fullmatch(revision):
        raise ValueError("full immutable Git revision required")
    groups, _, active, _ = registry()
    if app not in active or groups[app].get("parallel_owner"):
        raise ValueError("unregistered or offline app release")
    manifest = Path(manifest_path)
    expected_name = f"{app}-{revision[:12]}.tar.gz"
    archive = manifest.parent / expected_name
    if (manifest.name != expected_name + ".manifest.json"
            or manifest.is_symlink() or archive.is_symlink()
            or not manifest.is_file() or not archive.is_file()):
        raise ValueError("unsafe or missing release files")
    report = json.loads(manifest.read_text())
    if (report.get("app") != app or report.get("commit_sha") != revision
            or report.get("archive") != expected_name
            or not re.fullmatch("[a-f0-9]{64}", report.get("sha256", ""))):
        raise ValueError("release artifact does not match app and commit")
    if builder.verify(manifest) != app:
        raise ValueError("release manifest verification failed")
    return report["sha256"]


def deployment_contracts():
    """Registry-checked per-app services, workers, dependencies and migrations."""
    source = Path(__file__).with_name("deploy-contracts.json")
    doc = json.loads(source.read_text())
    groups, profiles, active, offline = registry()
    contracts = doc["apps"]
    if doc.get("schema_version") != 1 or set(contracts) != active:
        raise ValueError("contract coverage does not match active app registry")
    for app, item in contracts.items():
        if (item.get("dependencies") != groups[app]["needs"]
                or item.get("services") != profiles[app]["services"]
                or not isinstance(item.get("workers"), list)
                or item.get("migrations", {}).get("automatic") is not False
                or item.get("runtime_verified") is not False):
            raise ValueError("runtime contract cannot be presumed verified: " + app)
    return contracts


class QueueStore:
    """SQLite FIFO with collision-free idempotence and crash-safe terminal states."""

    def __init__(self, path, *, lease_seconds=1800, max_attempts=3):
        self.path = Path(path).absolute()
        if self.path == Path("/") or any(p.is_symlink() for p in (self.path, *self.path.parents)):
            raise ValueError("unsafe queue location or symlink")
        if lease_seconds < 1 or max_attempts < 1:
            raise ValueError("invalid retry or lease configuration")
        registry()
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
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
            existing = {x[1] for x in db.execute("PRAGMA table_info(requests)")}
            extras = {
                "attempts": "INTEGER NOT NULL DEFAULT 0",
                "ready_at": "REAL NOT NULL DEFAULT 0",
                "lease_until": "REAL",
                "token": "TEXT",
                "previous_sha": "TEXT",
                "error": "TEXT",
                "archive_sha256": "TEXT",
            }
            for field, definition in extras.items():
                if field not in existing:
                    db.execute("ALTER TABLE requests ADD COLUMN " + field + " " + definition)
            db.execute("CREATE INDEX IF NOT EXISTS deploy_fifo ON requests(status,id)")
            # Legacy CLAIMED lacked a verifiable lease: never auto-run it.
            db.execute("""UPDATE requests SET status='NEEDS_ATTENTION',
                          error='legacy claimed intent requires reconciliation'
                          WHERE status='CLAIMED' AND (token IS NULL OR lease_until IS NULL)""")
        try:
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    @contextmanager
    def _db(self):
        db = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=30000")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA foreign_keys=ON")
            yield db
        finally:
            db.close()

    @staticmethod
    def _row(db, job_id):
        row = db.execute("SELECT * FROM requests WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def enqueue(self, app, revision):
        groups, _, active, _ = registry()
        if app not in active or groups[app].get("parallel_owner"):
            raise ValueError("unknown, offline or externally owned application")
        if not SHA.fullmatch(revision):
            raise ValueError("only a full immutable Git commit SHA may be enqueued")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO requests(app,revision,lock_group) VALUES(?,?,?)",
                       (app, revision, groups[app]["lock"]))
            row = db.execute("SELECT * FROM requests WHERE app=? AND revision=?",
                             (app, revision)).fetchone()
            db.commit()
            return dict(row)

    def enqueue_verified(self, app, revision, manifest_path):
        """Register verified bytes of an app artifact under the immutable SHA."""
        digest = verify_release_candidate(app, revision, manifest_path)
        item = self.enqueue(app, revision)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, item["id"])
            if row["archive_sha256"] not in (None, digest):
                db.rollback()
                raise ValueError("conflicting archived bytes for immutable app SHA")
            db.execute("UPDATE requests SET archive_sha256=? WHERE id=?",
                       (digest, item["id"]))
            item = self._row(db, item["id"])
            db.commit()
            return item

    def claim(self, *, now=None):
        now = time.time() if now is None else now
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""UPDATE requests SET status='NEEDS_ATTENTION',
                          error='lease expired; inspect live release and reconcile',
                          token=NULL WHERE status='CLAIMED' AND lease_until <= ?""", (now,))
            if db.execute("SELECT id FROM requests WHERE status IN ('CLAIMED','NEEDS_ATTENTION') LIMIT 1").fetchone():
                db.commit()
                return None
            # Strict FIFO: later requests MUST NOT bypass an earlier retry delay.
            job = db.execute("""SELECT * FROM requests WHERE status IN ('QUEUED','RETRY_WAIT')
                                ORDER BY id LIMIT 1""").fetchone()
            if job is None or job["ready_at"] > now:
                db.commit()
                return None
            token = uuid.uuid4().hex
            db.execute("""UPDATE requests SET status='CLAIMED',
                          attempts=attempts+1, token=?, lease_until=?,
                          claimed_at=CURRENT_TIMESTAMP, error=NULL WHERE id=?""",
                       (token, now + self.lease_seconds, job["id"]))
            result = self._row(db, job["id"])
            db.commit()
            return result

    def previous(self, job_id, token, old_sha):
        if not SHA.fullmatch(old_sha):
            raise ValueError("unknown previous immutable revision")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            n = db.execute("""UPDATE requests SET previous_sha=?
                            WHERE id=? AND status='CLAIMED' AND token=?""",
                           (old_sha, job_id, token)).rowcount
            if n != 1:
                db.rollback()
                raise RuntimeError("deployment lease was lost")
            db.commit()

    def heartbeat(self, job_id, token, *, now=None):
        now = time.time() if now is None else now
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            n = db.execute("""UPDATE requests SET lease_until=?
                            WHERE id=? AND token=? AND status='CLAIMED'
                              AND lease_until>?""",
                           (now+self.lease_seconds, job_id, token, now)).rowcount
            if not n:
                db.rollback()
                raise RuntimeError("deployment lease expired or lost")
            db.commit()

    def finish(self, job_id, token, outcome, *, error="", retryable=False, now=None):
        if outcome not in (TERMINAL | {"NEEDS_ATTENTION"}):
            raise ValueError("invalid outcome")
        now = time.time() if now is None else now
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, job_id)
            if not row or row["status"] != "CLAIMED" or row["token"] != token:
                db.rollback()
                raise RuntimeError("stale worker cannot finalize this job")
            # A timed-out worker must not assert a verified outcome.
            if row["lease_until"] <= now:
                outcome = "NEEDS_ATTENTION"
                error = "worker timed out during activation; reconciliation required"
                retryable = False
            next_ready = 0
            if retryable and outcome == "FAILED" and row["attempts"] < self.max_attempts:
                outcome = "RETRY_WAIT"
                next_ready = now + min(RETRY_MAX_SECONDS,
                                       RETRY_BASE_SECONDS * (2 ** (row["attempts"] - 1)))
            db.execute("""UPDATE requests SET status=?, ready_at=?, token=NULL,
                        lease_until=NULL, error=?, finished_at=CASE WHEN ? IN
                        ('VERIFIED','FAILED','ROLLED_BACK') THEN CURRENT_TIMESTAMP ELSE NULL END
                        WHERE id=?""", (outcome, next_ready, str(error)[:1000], outcome, job_id))
            result = self._row(db, job_id)
            db.commit()
            return result

    def reconcile(self, job_id, inspected_sha, healthy, *, evidence):
        """Reconcile ONLY with independently inspected actual active SHA + health."""
        if not SHA.fullmatch(inspected_sha) or healthy is not True or not evidence:
            raise ValueError("verified release, healthy status and evidence are required")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, job_id)
            if not row or row["status"] != "NEEDS_ATTENTION":
                db.rollback()
                raise ValueError("job does not require reconciliation")
            if inspected_sha == row["revision"]:
                state = "VERIFIED"
            elif inspected_sha == row["previous_sha"]:
                state = "ROLLED_BACK"
            else:
                db.rollback()
                raise ValueError("active release does not match requested or prior SHA")
            db.execute("""UPDATE requests SET status=?, error=?, token=NULL,
                        lease_until=NULL, finished_at=CURRENT_TIMESTAMP WHERE id=?""",
                       (state, "reconciled: " + evidence[:900], job_id))
            result = self._row(db, job_id)
            db.commit()
            return result

    def list(self):
        with self._db() as db:
            return [dict(x) for x in db.execute("SELECT * FROM requests ORDER BY id")]


class RetryablePreparationError(RuntimeError):
    """Only pre-activation transient failures are automatically retried."""


class SimulatedBackend:
    """Existing synthetic atomic activation/rollback; root must stay under /tmp."""

    requires_verified_artifact = False  # CI rehearsal only, never production

    def __init__(self, root, *, restart, healthy):
        from rebuild.fresh_vps import root_dir
        self.root = root_dir(root)
        self.restart = restart
        self.healthy = healthy

    def preflight(self, app, revision):
        from rebuild.release_transaction import _managed_pointer
        _, current = _managed_pointer(self.root, app)
        if self.healthy(app, current.name) is not True:
            raise ValueError("existing release unhealthy; blocked")
        return current.name

    def activate(self, app, revision):
        return release_transaction.transact(self.root, app, revision,
                    self.restart, self.healthy, snapshot_confirmed=True)

    def inspect(self, app):
        from rebuild.release_transaction import _managed_pointer
        _, active = _managed_pointer(self.root, app)
        return active.name, self.healthy(app, active.name) is True


class Engine:
    def __init__(self, queue, backend):
        self.queue = queue
        self.backend = backend
        deployment_contracts()  # always fail closed on registry drift

    def once(self, *, now=None):
        job = self.queue.claim(now=now)
        if job is None:
            return None
        job_id, token = job["id"], job["token"]
        if (getattr(self.backend, "requires_verified_artifact", True)
                and not job["archive_sha256"]):
            return self.queue.finish(job_id, token, "FAILED",
                                     error="missing authenticated and verified artifact", now=now)
        try:
            previous_sha = self.backend.preflight(job["app"], job["revision"])
            self.queue.previous(job_id, token, previous_sha)
            # Refresh the lease before the side-effectful transaction.
            self.queue.heartbeat(job_id, token, now=now)
            self.backend.activate(job["app"], job["revision"])
        except RetryablePreparationError as exc:
            # Even a transient-looking exception after capturing the old
            # release may originate in activation: never blindly retry.
            before_activation = self.queue._snapshot(job_id)["previous_sha"] is None
            return self.queue.finish(job_id, token,
                                     "FAILED" if before_activation else "NEEDS_ATTENTION",
                                     error=str(exc), retryable=before_activation, now=now)
        except release_transaction.PromotionFailed as exc:
            status = ("ROLLED_BACK" if "prior healthy revision verified restored" in str(exc)
                      else "NEEDS_ATTENTION")
            return self.queue.finish(job_id, token, status, error=str(exc), now=now)
        except ValueError as exc:
            # ValueError during activation may follow a side effect. Without an
            # independently verified prior version, stop all subsequent jobs.
            if self.queue._snapshot(job_id)["previous_sha"]:
                return self.queue.finish(job_id, token, "NEEDS_ATTENTION",
                                         error=str(exc), now=now)
            return self.queue.finish(job_id, token, "FAILED", error=str(exc), now=now)
        except Exception as exc:
            return self.queue.finish(job_id, token, "NEEDS_ATTENTION",
                                     error=type(exc).__name__+": "+str(exc), now=now)
        return self.queue.finish(job_id, token, "VERIFIED", now=now)

    def run_forever(self, *, poll_seconds=1.0, stop_event=None, max_cycles=None):
        """Single-claim polling daemon; stopping never cancels an in-flight app."""
        if poll_seconds <= 0 or (max_cycles is not None and max_cycles < 1):
            raise ValueError("invalid worker polling configuration")
        cycles = 0
        while stop_event is None or not stop_event.is_set():
            self.once()
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                return cycles
            if stop_event is None:
                time.sleep(poll_seconds)
            else:
                stop_event.wait(poll_seconds)
        return cycles

    def reconcile(self, job_id):
        row = self.queue._snapshot(job_id)
        if not row or row["status"] != "NEEDS_ATTENTION":
            raise ValueError("no job requiring reconciliation")
        sha, healthy = self.backend.inspect(row["app"])
        return self.queue.reconcile(job_id, sha, healthy,
                                    evidence="backend active revision + health inspected")


def _snapshot(self, job_id):
    with self._db() as db:
        return self._row(db, job_id)


QueueStore._snapshot = _snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="shared persistent SQLite queue file")
    sub = parser.add_subparsers(dest="command", required=True)
    e = sub.add_parser("enqueue")
    e.add_argument("--app", required=True)
    e.add_argument("--sha", required=True)
    v = sub.add_parser("enqueue-verified", help="integrity check a trusted GitHub candidate")
    v.add_argument("--app", required=True)
    v.add_argument("--sha", required=True)
    v.add_argument("--manifest", required=True)
    sub.add_parser("list")
    sim = sub.add_parser("simulate-worker", help="execute ONLY isolated /tmp rehearsal")
    sim.add_argument("--root", required=True)
    sim.add_argument("--once", action="store_true")
    args = parser.parse_args()
    q = QueueStore(args.db)
    if args.command == "enqueue":
        result = q.enqueue(args.app, args.sha)
    elif args.command == "enqueue-verified":
        result = q.enqueue_verified(args.app, args.sha, args.manifest)
    elif args.command == "simulate-worker":
        # These callbacks deliberately model success; never use this CLI as a
        # production health monitor or a service restart mechanism.
        backend = SimulatedBackend(args.root, restart=lambda app: None,
                                   healthy=lambda app, sha: True)
        worker = Engine(q, backend)
        result = worker.once() if args.once else {"cycles": worker.run_forever()}
    else:
        result = q.list()
    print(json.dumps(result, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()

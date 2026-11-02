"""Durable PKT daily Short Profile batch planning, without autonomous execution.

The planner is deliberately distinct from the existing Maker executor and
Auto Script. Its output is durable queue intent; a later verified bridge must
reconcile those job IDs with the existing Short Maker render queue before
the dedicated clock worker or the UI Start control is enabled.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException

PKT = ZoneInfo("Asia/Karachi")


def local_now(when=None):
    current = when or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("Scheduler requires an aware timestamp")
    return current.astimezone(PKT)


class DailyBatchPlanner:
    def __init__(self, store, verified_catalog, check_owned_folder):
        self.store = store
        self.verified_catalog = verified_catalog
        self.check_owned_folder = check_owned_folder
        with self.store._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS short_profile_jobs (
                    job_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    profile_id TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    pkt_date TEXT NOT NULL,
                    voice_sha256 TEXT NOT NULL,
                    voice_path TEXT NOT NULL,
                    pictures_json TEXT NOT NULL,
                    source_import_id TEXT NOT NULL,
                    output_folder TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'reserved',
                    created_at TEXT NOT NULL,
                    UNIQUE(profile_id, voice_sha256),
                    FOREIGN KEY(profile_id) REFERENCES short_profiles(id),
                    FOREIGN KEY(run_id) REFERENCES short_profile_runs(id)
                );
                CREATE INDEX IF NOT EXISTS short_profile_jobs_owner
                    ON short_profile_jobs(owner, profile_id, pkt_date);
            """)

    def eligible_profiles(self, when=None):
        """Due today only. Never replay missed calendar dates automatically."""
        moment = local_now(when)
        hhmm = moment.strftime("%H:%M")
        with self.store._connect() as db:
            return [dict(row) for row in db.execute("""
                SELECT p.* FROM short_profiles p
                WHERE p.enabled=1 AND p.deleted_at IS NULL
                  AND p.schedule_hhmm<=?
                  AND NOT EXISTS(
                    SELECT 1 FROM short_profile_runs r
                    WHERE r.profile_id=p.id AND r.pkt_date=?
                  )
                ORDER BY p.created_at,p.id
            """, (hhmm, moment.date().isoformat()))]

    def _result(self, db, owner, profile_id, day):
        run = db.execute(
            """SELECT * FROM short_profile_runs
               WHERE profile_id=? AND owner=? AND pkt_date=?""",
            (profile_id, owner, day),
        ).fetchone()
        jobs = [dict(row) for row in db.execute(
            """SELECT * FROM short_profile_jobs
               WHERE profile_id=? AND owner=? AND pkt_date=?
               ORDER BY created_at, job_id""",
            (profile_id, owner, day),
        )]
        if run is None:
            return None
        output = dict(run)
        output["planned"] = len(jobs)
        output["shortage"] = max(0, output["target"] - len(jobs))
        output["jobs"] = jobs
        return output

    def plan(self, owner, profile_id, when=None, *, explicit_start=False):
        """Freeze one day's job intents once, in an atomic SQLite transaction.

        A profile Start can claim the current day immediately. A normal
        scheduler tick waits until the configured local PKT time.
        """
        owner = str(owner).strip().casefold()
        moment = local_now(when)
        day = moment.date().isoformat()
        prior = self.store.get(owner, profile_id)
        if not prior["enabled"]:
            raise HTTPException(409, detail="Short Profile is stopped")
        if not explicit_start and moment.strftime("%H:%M") < prior["schedule_hhmm"]:
            return {"due": False, "pkt_date": day}

        # Existing daily intentions are immutable. Reconcile their already
        # staged job IDs without rehashing an entire PC import every clock tick.
        with self.store._connect() as db:
            previous = self._result(db, owner, profile_id, day)
            if previous:
                return {"due": True, "created": False, **previous}

        # Reading an imported catalog is deliberately outside the SQLite write
        # lock: SHA256 on large audio/picture sources could take noticeable time.
        catalog = self.verified_catalog(owner, prior["source_import_id"])
        if not isinstance(catalog, dict) or not catalog.get("picture") or not catalog.get("audio"):
            raise HTTPException(409, detail="Verified VPS voice and pictures are unavailable")
        self.check_owned_folder(owner, prior["output_folder"])
        audio = sorted(catalog["audio"], key=lambda a: a["sha256"])
        pictures = sorted(catalog["picture"], key=lambda a: a["sha256"])
        for asset in audio + pictures:
            if (not isinstance(asset, dict)
                    or not re.fullmatch(r"[0-9a-f]{64}", str(asset.get("sha256", "")))
                    or not isinstance(asset.get("path"), str)
                    or not asset["path"]):
                raise HTTPException(409, detail="Invalid verified media receipt")
        pictures_json = json.dumps(pictures, sort_keys=True, separators=(",", ":"))
        now = datetime.now(timezone.utc).isoformat()

        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            p = self.store._owned(db, profile_id, owner)
            if not p["enabled"]:
                raise HTTPException(409, detail="Short Profile stopped during planning")
            if any(p[field] != prior[field] for field in (
                    "source_import_id", "output_folder", "videos_per_day", "schedule_hhmm")):
                raise HTTPException(409, detail="Profile changed; retry planning")
            existing = self._result(db, owner, profile_id, day)
            if existing:
                return {"due": True, "created": False, **existing}

            used = {row["voice_sha256"] for row in db.execute(
                "SELECT voice_sha256 FROM short_profile_jobs WHERE profile_id=?",
                (profile_id,))}
            choices = []
            for item in audio:
                if item["sha256"] not in used:
                    choices.append(item)
                    used.add(item["sha256"])
                    if len(choices) >= p["videos_per_day"]:
                        break
            run_id = uuid.uuid4().hex
            expected = int(p["videos_per_day"])
            run_status = "planned" if len(choices) == expected else "media_shortage"
            db.execute("""
                INSERT INTO short_profile_runs
                    (id,profile_id,owner,pkt_date,status,target,created_at)
                VALUES(?,?,?,?,?,?,?)
            """, (run_id, profile_id, owner, day, run_status, expected, now))
            for voice in choices:
                job_id = hashlib.sha256(
                    (profile_id + ":" + day + ":" + voice["sha256"]).encode()
                ).hexdigest()[:16]
                db.execute("""
                    INSERT INTO short_profile_jobs
                      (job_id,run_id,profile_id,owner,pkt_date,voice_sha256,
                       voice_path,pictures_json,source_import_id,output_folder,
                       status,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """, (job_id, run_id, profile_id, owner, day,
                      voice["sha256"], voice["path"], pictures_json,
                      p["source_import_id"], p["output_folder"], "reserved", now))
            return {"due": True, "created": True,
                    **self._result(db, owner, profile_id, day)}

    def list_claims(self, owner, profile_id, *, pkt_date=None):
        owner = str(owner).strip().casefold()
        self.store.get(owner, profile_id, include_deleted=True)
        with self.store._connect() as db:
            return [dict(row) for row in db.execute(
                """SELECT * FROM short_profile_jobs WHERE owner=? AND profile_id=?
                   AND (? IS NULL OR pkt_date=?) ORDER BY created_at,job_id""",
                (owner, profile_id, pkt_date, pkt_date))]

    def reconcile(self, owner, profile_id, job_lookup, enqueue, *, pkt_date=None):
        """Reconcile durable intents, never submit an existing queue job twice.

        job_lookup returns current persisted Maker status or None. The enqueue
        callback MUST atomically create a deterministic job and be idempotent.
        No automatic retry of failed/cancelled or already-delivering jobs.
        """
        owner = str(owner).strip().casefold()
        profile = self.store.get(owner, profile_id)
        for claim in self.list_claims(owner, profile_id, pkt_date=pkt_date):
            if claim["status"] == "enqueued":
                continue
            state = job_lookup(claim["job_id"])
            if state:
                if str(state.get("owner", "")).casefold() != owner:
                    raise HTTPException(409, detail="Job ownership collision")
            elif not profile["enabled"]:
                continue  # Stop forbids dispatching new claims
            else:
                enqueue(claim)
            with self.store._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("""UPDATE short_profile_jobs SET status='enqueued'
                    WHERE job_id=? AND owner=? AND status='reserved'""",
                    (claim["job_id"], owner))
        return self.list_claims(owner, profile_id)

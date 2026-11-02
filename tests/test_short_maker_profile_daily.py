"""Daily PKT planning, unique voice consumption, and crash reconciliation tests."""
import importlib.util
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1] / "apps/short-maker/part01"


def load(name):
    p = ROOT / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, p)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DailyBatchPlannerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = load("short_profiles").ShortProfileStore(Path(self.tmp.name))
        self.module = load("short_profile_daily")
        self.a = "a" * 64
        self.b = "b" * 64
        self.image = "f" * 64
        self.catalog = {
            "audio": [
                {"sha256": self.a, "path": "/verified/alice/audio/a.mp3"},
                {"sha256": self.b, "path": "/verified/alice/audio/b.mp3"},
            ],
            "picture": [{"sha256": self.image, "path": "/verified/alice/picture/x.png"}],
        }
        def verified(owner, sid):
            return {"audio_count": 2, "image_count": 1} if owner == "alice" else None
        self.profile = self.store.create("alice", {
            "name": "Daily", "source_import_id": "a" * 32,
            "output_folder": "Alice Folder", "videos_per_day": 3,
            "schedule_hhmm": "00:00",
        }, verified, lambda name: name)
        self.pid = self.profile["id"]
        self.checked = []
        def check_folder(owner, name):
            self.checked.append((owner, name))
            if (owner, name) != ("alice", "Alice Folder"):
                raise HTTPException(403, detail="Cross-user or missing VPS folder")
        def list_media(owner, sid):
            return self.catalog if (owner, sid) == ("alice", "a" * 32) else None
        self.planner = self.module.DailyBatchPlanner(
            self.store, list_media, check_folder)
        self.queued = {}
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def enable(self):
        with self.store._connect() as db:
            db.execute("UPDATE short_profiles SET enabled=1 WHERE id=?", (self.pid,))

    def at(self, utc_iso):
        return datetime.fromisoformat(utc_iso).replace(tzinfo=timezone.utc)

    def test_one_day_one_batch_voice_consumed_and_shortage(self):
        self.enable()
        start = self.at("2026-11-01T19:01:00")  # 00:01 on Nov 2 PKT
        made = self.planner.plan("alice", self.pid, start)
        self.assertTrue(made["created"])
        self.assertEqual(made["pkt_date"], "2026-11-02")
        self.assertEqual(made["status"], "media_shortage")
        self.assertEqual(made["target"], 3)
        self.assertEqual(made["planned"], 2)
        self.assertEqual(made["shortage"], 1)
        self.assertEqual(len({r["voice_sha256"] for r in made["jobs"]}), 2)
        self.assertEqual(len({r["job_id"] for r in made["jobs"]}), 2)
        self.assertEqual(made["jobs"][0]["output_folder"], "Alice Folder")
        repeat = self.planner.plan("alice", self.pid, self.at("2026-11-02T12:00:00"))
        self.assertFalse(repeat["created"])
        self.assertEqual([x["job_id"] for x in made["jobs"]],
                         [x["job_id"] for x in repeat["jobs"]])
        tomorrow = self.planner.plan("alice", self.pid,
                                     self.at("2026-11-02T19:01:00"))
        self.assertEqual(tomorrow["planned"], 0)
        self.assertEqual(tomorrow["shortage"], 3)
        self.assertEqual(tomorrow["status"], "media_shortage")
        self.assertEqual(len(self.store.history("alice", self.pid)), 2)

    def test_scheduler_waits_until_pkt_time_and_avoids_backfill(self):
        self.enable()
        with self.store._connect() as db:
            db.execute("UPDATE short_profiles SET schedule_hhmm='03:15' WHERE id=?",
                       (self.pid,))
        before = self.at("2026-11-01T21:14:00")  # 02:14 PKT, Nov 2
        self.assertEqual(self.planner.eligible_profiles(before), [])
        self.assertFalse(self.planner.plan("alice",self.pid,before)["due"])
        after = self.at("2026-11-01T22:16:00")  # 03:16 PKT
        self.assertEqual(len(self.planner.eligible_profiles(after)), 1)
        self.assertEqual(self.planner.plan("alice",self.pid,after)["pkt_date"],
                         "2026-11-02")
        self.assertEqual(self.planner.eligible_profiles(after), [])
        # Starting before the time explicitly requests the current day only.
        next_day_before = self.at("2026-11-02T19:01:00")
        self.assertTrue(self.planner.plan("alice",self.pid,next_day_before,
                                          explicit_start=True)["due"])
        self.assertEqual(len(self.store.history("alice",self.pid)),2)
        self.assertEqual(self.planner.eligible_profiles(after), [])

    def test_stopped_missing_media_foreign_or_naive_timestamp_fail_closed(self):
        with self.assertRaises(HTTPException) as stopped:
            self.planner.plan("alice", self.pid, self.at("2026-11-01T19:00:00"))
        self.assertEqual(stopped.exception.status_code, 409)
        self.enable()
        with self.assertRaises(HTTPException) as foreign:
            self.planner.plan("bob", self.pid, self.at("2026-11-01T19:00:00"))
        self.assertEqual(foreign.exception.status_code, 404)
        with self.assertRaises(ValueError):
            self.planner.plan("alice",self.pid,datetime(2026,11,2))
        self.catalog = None
        with self.assertRaises(HTTPException) as unverified:
            self.planner.plan("alice",self.pid,self.at("2026-11-01T19:00:00"))
        self.assertEqual(unverified.exception.status_code, 409)
        self.assertEqual(self.store.history("alice",self.pid),[])

    def test_crash_safe_claim_reconciliation_never_duplicates_queue(self):
        self.enable()
        made = self.planner.plan("alice",self.pid,self.at("2026-11-01T19:00:00"))
        def lookup(job_id):
            return self.queued.get(job_id)
        def enqueue(claim):
            self.calls.append(claim["job_id"])
            self.queued[claim["job_id"]] = {"owner":"alice","status":"queued"}
        self.planner.reconcile("alice",self.pid,lookup,enqueue)
        self.planner.reconcile("alice",self.pid,lookup,enqueue)
        self.assertEqual(len(self.calls),2)
        self.assertEqual(set(self.calls),{p["job_id"] for p in made["jobs"]})
        self.assertEqual(
            {x["status"] for x in self.planner.list_claims("alice",self.pid)},
            {"enqueued"})

    def test_existing_job_is_recovered_but_stopped_profile_doesnt_create_new(self):
        self.enable()
        made = self.planner.plan("alice",self.pid,self.at("2026-11-01T19:00:00"))
        known = made["jobs"][0]["job_id"]
        self.queued[known] = {"owner":"alice","status":"running"}
        self.store.stop("alice",self.pid)
        def enqueue(claim):
            self.calls.append(claim["job_id"])
        self.planner.reconcile("alice",self.pid,self.queued.get,enqueue)
        self.assertEqual(self.calls,[])
        statuses = {x["job_id"]:x["status"] for x in
                    self.planner.list_claims("alice",self.pid)}
        self.assertEqual(statuses[known],"enqueued")
        self.assertEqual(sum(s=="reserved" for s in statuses.values()),1)

    def test_reject_cross_owner_job_id_collision(self):
        self.enable()
        claim=self.planner.plan("alice",self.pid,self.at("2026-11-01T19:00:00"))["jobs"][0]
        with self.assertRaises(HTTPException) as collision:
            self.planner.reconcile(
                "alice",self.pid,
                lambda job_id:{"owner":"bob","status":"queued"} if job_id==claim["job_id"] else None,
                lambda job:None,
            )
        self.assertEqual(collision.exception.status_code,409)


if __name__ == "__main__":
    unittest.main()

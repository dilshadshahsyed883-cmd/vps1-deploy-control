"""Dedicated Short Profile clock: PKT schedules, per-profile isolation, no backfill."""
import importlib.util
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1] / "apps/short-maker/part01"


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name+".py"))
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


class ClockBridge:
    def __init__(self):
        self.jobs = {}
        self.submits = []
    def lookup(self, jobid):
        return self.jobs.get(jobid)
    def enqueue(self, claim):
        self.submits.append(claim["job_id"])
        self.jobs[claim["job_id"]] = {"owner":claim["owner"],"status":"queued",
                                      "short_profile_id":claim["profile_id"]}


class ShortProfileClockTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=load("short_profiles").ShortProfileStore(Path(self.tmp.name))
        self.daily=load("short_profile_daily")
        self.bridge=ClockBridge()
        self.catalog={"audio":[{"sha256":"a"*64,"path":"/fake/voice.mp3"}],
                      "picture":[{"sha256":"f"*64,"path":"/fake/cover.png"}]}
        def verifier(owner,sid):
            return {"audio_count":1,"image_count":1}
        self.profiles=[]
        for name in ("good","bad"):
            profile=self.store.create("alice",{
                "name":name,"source_import_id":"a"*32,
                "output_folder":"Own Folder","videos_per_day":1,
                "schedule_hhmm":"03:15"
            },verifier,lambda folder:folder)
            self.profiles.append(profile)
        self.allowed={self.profiles[0]["id"]}
        def catalog(owner,sid):
            return self.catalog
        def folder(owner,name):
            if owner!="alice" or name!="Own Folder":
                raise HTTPException(403,detail="Folder access denied")
        self.planner=self.daily.DailyBatchPlanner(self.store,catalog,folder)
        self.clock=load("short_profile_clock").ShortProfileDailyClock(
            self.store,self.planner,self.bridge,
            now=lambda:self.at("2026-11-01T22:16:00"))
        with self.store._connect() as db:
            db.execute("UPDATE short_profiles SET enabled=1")

    def tearDown(self):
        self.clock.stop()
        self.tmp.cleanup()

    @staticmethod
    def at(value):
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)

    def test_midnight_boundary_schedule_and_once_day(self):
        early=self.clock.tick(self.at("2026-11-01T22:14:00"))
        self.assertEqual(early,[])
        rows=self.clock.tick(self.at("2026-11-01T22:16:00"))
        self.assertEqual(len(rows),2)
        self.assertEqual(len(self.bridge.submits),2)
        again=self.clock.tick(self.at("2026-11-01T23:00:00"))
        self.assertEqual(len(self.bridge.submits),2)
        self.assertEqual(rows[0]["pkt_date"],"2026-11-02")
        self.assertEqual(len(self.store.history("alice",self.profiles[0]["id"])),1)

    def test_explicit_start_before_schedule_and_stop(self):
        pid=self.profiles[0]["id"]
        self.store.stop("alice",pid)
        self.clock.now=lambda:self.at("2026-11-01T19:02:00")
        result=self.clock.start_profile("alice",pid)
        self.assertEqual(result["pkt_date"],"2026-11-02")
        self.assertEqual(result["queued_or_existing"],1)
        self.store.stop("alice",pid)
        self.clock.tick(self.at("2026-11-02T22:16:00"))
        self.assertEqual(len(self.store.history("alice",pid)),1)

    def test_missing_daily_media_isolated_and_does_not_abort_other_profiles(self):
        with self.store._connect() as db:
            db.execute("UPDATE short_profiles SET source_import_id=? WHERE id=?",
                       ("b"*32,self.profiles[1]["id"]))
        self.planner.verified_catalog=lambda owner,sid:(
            self.catalog if sid=="a"*32 else None)
        result=self.clock.tick(self.at("2026-11-01T22:16:00"))
        self.assertEqual([r["status"] for r in result],["planned","error"])
        self.assertEqual(len(self.bridge.submits),1)

    def test_future_dates_not_burst_after_missed_days(self):
        self.clock.tick(self.at("2026-11-01T22:16:00"))
        self.bridge.submits.clear()
        # No intermediate tick for Nov 3/4. Scheduler only claims Nov 5.
        self.clock.tick(self.at("2026-11-04T22:16:00"))
        days=[x["pkt_date"] for x in self.store.history(
            "alice",self.profiles[0]["id"])]
        self.assertEqual(days,["2026-11-05","2026-11-02"])
        self.assertNotIn("2026-11-03",days)
        self.assertNotIn("2026-11-04",days)
        # Voice exhausted on the new date; no duplicate video is created.
        self.assertEqual(self.bridge.submits,[])


if __name__=="__main__":
    unittest.main()

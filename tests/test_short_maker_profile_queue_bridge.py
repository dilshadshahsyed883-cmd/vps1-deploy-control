"""Existing Maker render queue bridge tests: atomic owner jobs, crash-safe replay."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1] / "apps/short-maker/part01"


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeExecutor:
    def __init__(self):
        self.calls = []
    def submit(self, handler, job_id, cfg):
        self.calls.append((handler, job_id, cfg))


class ShortProfileQueueBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.jobs_root = root / "jobs"
        self.media_root = root / "media"
        self.media_root.mkdir()
        self.voice_path = self.media_root / "voice.mp3"
        self.image_path = self.media_root / "photo.png"
        self.voice_path.write_bytes(b"voice"*300)
        self.image_path.write_bytes(b"image"*300)
        self.voice_hash = hashlib.sha256(self.voice_path.read_bytes()).hexdigest()
        self.image_hash = hashlib.sha256(self.image_path.read_bytes()).hexdigest()
        self.voice = {"path": str(self.voice_path), "sha256": self.voice_hash,
                      "size": self.voice_path.stat().st_size}
        self.picture = {"path": str(self.image_path), "sha256": self.image_hash,
                        "size": self.image_path.stat().st_size}
        self.profiles = load("short_profiles")
        self.daily = load("short_profile_daily")
        self.bridge_module = load("short_profile_queue_bridge")
        self.store = self.profiles.ShortProfileStore(root / "data")
        self.profile = self.store.create(
            "alice", {"name":"Daily Voice", "source_import_id":"a"*32,
                      "output_folder":"Alice Output", "videos_per_day":1,
                      "schedule_hhmm":"00:00"},
            lambda owner,sid:{"audio_count":1,"image_count":1},
            lambda name:name)
        self.pid = self.profile["id"]
        with self.store._connect() as db:
            db.execute("UPDATE short_profiles SET enabled=1 WHERE id=?", (self.pid,))
        def catalog(owner,sid):
            if (owner,sid) == ("alice","a"*32):
                return {"audio":[self.voice], "picture":[self.picture]}
            return None
        def owned(owner,folder):
            if (owner,folder) != ("alice","Alice Output"):
                raise HTTPException(403, detail="Folder is not owned")
        self.planner = self.daily.DailyBatchPlanner(self.store,catalog,owned)
        self.executor = FakeExecutor()
        self.jobs = {}
        self.reserved = []
        def reserve(folder,count):
            self.reserved.append((folder,count))
            return ["Alice Output 001_abcdef.mp4"]
        def job_lookup(job_id):
            if job_id in self.jobs:
                return self.jobs[job_id]
            state=self.jobs_root/job_id/"state.json"
            return json.loads(state.read_text()) if state.exists() else None
        self.bridge = self.bridge_module.ExistingMakerQueueBridge(
            self.jobs_root,catalog,owned,reserve,job_lookup,
            self.executor,lambda *args:None,self.jobs,Lock())
        self.at = datetime(2026,11,1,19,0,tzinfo=timezone.utc)
        self.run = self.planner.plan("alice",self.pid,self.at)
        self.claim = self.run["jobs"][0]

    def tearDown(self):
        self.tmp.cleanup()

    def test_reconcile_copies_exact_media_and_reuses_maker_executor(self):
        with patch.object(self.bridge_module.subprocess,"run",
                          return_value=SimpleNamespace(returncode=0,stdout="21.75\n")):
            claims = self.planner.reconcile("alice",self.pid,
                                            self.bridge.lookup,self.bridge.enqueue)
            self.planner.reconcile("alice",self.pid,
                                   self.bridge.lookup,self.bridge.enqueue)
        self.assertEqual(len(claims),1)
        self.assertEqual(claims[0]["status"],"enqueued")
        self.assertEqual(len(self.executor.calls),1)
        self.assertEqual(self.reserved,[("Alice Output",1)])
        handler,job_id,cfg=self.executor.calls[0]
        self.assertEqual(cfg["audio"],[str(self.jobs_root/job_id/"input"/"audio" /
                         ("0001_"+self.voice_hash[:12]+".mp3"))])
        self.assertEqual(cfg["video_duration"],21.75)
        self.assertEqual(cfg["output_count"],1)
        self.assertEqual(cfg["short_vps_folder"],"Alice Output")
        self.assertTrue(cfg["available_to_short_uploader"])
        self.assertFalse(cfg["upload_to_drive"])
        self.assertEqual(cfg["fps"],30)
        self.assertEqual(cfg["image_duration"],3.0)
        self.assertEqual(len(cfg["images"]),1)
        self.assertEqual(hashlib.sha256(Path(cfg["images"][0]).read_bytes()).hexdigest(),
                         self.image_hash)
        saved=json.loads((self.jobs_root/job_id/"state.json").read_text())
        self.assertEqual(saved["owner"],"alice")
        self.assertEqual(saved["short_profile_id"],self.pid)
        self.assertEqual(saved["short_profile_pkt_date"],"2026-11-02")

    def test_restart_after_state_write_does_not_dispatch_duplicate(self):
        # Simulate crash after durable state file rename but before planner status change.
        with patch.object(self.bridge_module.subprocess,"run",
                          return_value=SimpleNamespace(returncode=0,stdout="12\n")):
            self.bridge.enqueue(self.claim)
        self.assertEqual(len(self.executor.calls),1)
        self.jobs.clear()  # new process has not run the Maker startup recovery
        self.planner.reconcile("alice",self.pid,
                               self.bridge.lookup,self.bridge.enqueue)
        self.assertEqual(len(self.executor.calls),1)
        self.assertEqual(self.planner.list_claims("alice",self.pid)[0]["status"],
                         "enqueued")

    def test_source_mutation_blocks_claim_without_staging_or_reserving(self):
        self.voice_path.write_bytes(b"modified media"*200)
        with self.assertRaises(HTTPException) as exc:
            self.bridge.enqueue(self.claim)
        self.assertEqual(exc.exception.status_code,409)
        self.assertEqual(self.executor.calls,[])
        self.assertEqual(self.reserved,[])
        self.assertFalse((self.jobs_root/self.claim["job_id"]).exists())
        self.assertEqual(list(self.jobs_root.glob(".short-profile-stage-*")),[])

    def test_wrong_owner_folder_and_collision_are_fail_closed(self):
        foreign={**self.claim,"owner":"bob"}
        with self.assertRaises(HTTPException) as invalid:
            self.bridge.enqueue(foreign)
        self.assertEqual(invalid.exception.status_code,403)
        self.assertEqual(self.executor.calls,[])
        with self.jobs_root.joinpath(self.claim["job_id"]).open("w") as output:
            output.write("unexpected")
        with self.assertRaises(HTTPException) as conflict:
            self.bridge.enqueue(self.claim)
        self.assertEqual(conflict.exception.status_code,409)

    def test_disk_reserve_blocks_before_unsafe_output(self):
        small = SimpleNamespace(free=self.bridge_module.DISK_RESERVE_BYTES+100)
        with patch.object(self.bridge_module.shutil,"disk_usage",return_value=small):
            with self.assertRaises(HTTPException) as exc:
                self.bridge.enqueue(self.claim)
        self.assertEqual(exc.exception.status_code,507)
        self.assertEqual(self.executor.calls,[])

    def test_bad_voice_duration_not_submitted(self):
        with patch.object(self.bridge_module.subprocess,"run",
                          return_value=SimpleNamespace(returncode=0,stdout="nan\n")):
            with self.assertRaises(HTTPException) as exc:
                self.bridge.enqueue(self.claim)
        self.assertEqual(exc.exception.status_code,422)
        self.assertEqual(self.executor.calls,[])
        self.assertEqual(list(self.jobs_root.glob(".short-profile-stage-*")),[])


if __name__=="__main__":
    unittest.main()

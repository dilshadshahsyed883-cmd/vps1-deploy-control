"""Offline regression for Short Uploader network fairness and 10-minute slot grace."""
from __future__ import annotations
import ast
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parents[1]
PART=ROOT/"apps/local-upload-manager/part01"
SCHED=PART/"lum_engine/auto_engine_v6_scheduler.py"
WORKER=PART/"lum_engine/auto_engine_v6_worker.py"
REPO=PART/"desktop_core/modules/db/queue_repository.py"
PKT=ZoneInfo("Asia/Karachi")

def load_fn(file,name,ns):
    node=next(n for n in ast.parse(file.read_text()).body if isinstance(n,ast.FunctionDef) and n.name==name)
    module=ast.Module(body=[ast.ImportFrom(module="__future__",names=[ast.alias(name="annotations")],level=0),node],type_ignores=[])
    exec(compile(ast.fix_missing_locations(module),str(file),"exec"),ns)
    return ns[name]

class ShortScheduleTest(unittest.TestCase):
    def test_missed_slot_19h_skipped_until_next_day(self):
        clock=[datetime(2026,10,30,19,0,tzinfo=PKT)]
        class Clock:
            @staticmethod
            def now(tz): return clock[0].astimezone(tz)
        runs=set();created=[]
        db=SimpleNamespace(
            auto_engine_run_exists=lambda pid,key:(pid,key) in runs,
            record_auto_engine_run=lambda pid,key,count:runs.add((pid,key)),
            add_log=lambda *a:None)
        store=SimpleNamespace(set_engine=lambda **kw:None)
        def generate(db,store,profile,**kw):
            runs.add((profile["id"],kw["run_key"]))
            created.append(kw)
            return {"created":kw["max_jobs_override"]}
        ns=dict(datetime=Clock,SCHEDULE_TZ=PKT,GRACE_MINUTES=6,SHORT_STUDIO_GRACE_MINUTES=10,
                channel_profile_scheduler_tick=lambda *a:-1,
                parse_schedule_times=lambda t:t.split(","),
                target_channels=lambda *a:[{"id":1},{"id":2}],generate_queue=generate)
        tick=load_fn(SCHED,"check_profile",ns)
        p=dict(id=1,category_name="test",enabled=1,schedule_enabled=1,work_mode="auto",
               schedule_times="02:00,20:00",daily_upload_count=2,
               upload_scope="per_channel",missed_schedule_policy="run_missed_today")
        with patch.dict(os.environ,{"LUM_ENGINE_INSTANCE":"shortstudio"}):
            self.assertEqual(tick(db,store,p),0)
            self.assertFalse(created)
            self.assertIn((1,"2026-10-30 slot-1 02:00"),runs)
            clock[0]=datetime(2026,10,31,2,10,tzinfo=PKT)
            self.assertEqual(tick(db,store,p),2)
            self.assertEqual(len(created),1)
            clock[0]=datetime(2026,11,1,2,10,1,tzinfo=PKT)
            self.assertEqual(tick(db,store,p),0)
            self.assertEqual(len(created),1)

    def test_non_shortstudio_policy_unchanged(self):
        self.assertIn('policy=="run_missed_today"',SCHED.read_text())
        self.assertIn('not shortstudio and (delay<=GRACE_MINUTES',SCHED.read_text())

class ShortNetworkTest(unittest.TestCase):
    def test_wait_does_not_enter_infinite_parent_loop(self):
        class Stop(Exception): pass
        class Parent:
            def _wait_for_connection(self,*a): raise AssertionError("blocking base wait")
            def internet_available(self,*a,**kw): return False
        tree=ast.parse(WORKER.read_text())
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=="AutoEngineYouTubeUploader")
        wait=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=="_wait_for_connection")
        node=ast.ClassDef(name="TestUploader",bases=[ast.Name(id="Parent",ctx=ast.Load())],
                          keywords=[],body=[wait],decorator_list=[])
        module=ast.Module(body=[ast.ImportFrom(module="__future__",names=[ast.alias(name="annotations")],level=0),node],type_ignores=[])
        updates=[]
        ns=dict(Parent=Parent,os=os,UploadStopped=Stop,datetime=datetime,timedelta=timedelta,timezone=timezone,
                clean=lambda x:str(x or "").strip(),_proxy_group_key=lambda x:"",
                _proxy_delay=lambda i:120,_proxy_marker=lambda *a:"PROXY",
                _network_marker=lambda *a:"AUTO_NETWORK_WAIT|1",
                UploadStatus=SimpleNamespace(WAITING_NETWORK="WAITING_NETWORK"))
        exec(compile(ast.fix_missing_locations(module),str(WORKER),"exec"),ns)
        obj=ns["TestUploader"]()
        obj._building_service=True
        obj.db=SimpleNamespace(get_video=lambda vid:{"auto_engine_profile_id":3},
                               update_video_status=lambda *a,**kw:updates.append(kw),
                               add_log=lambda *a:None)
        with patch.dict(os.environ,{"LUM_ENGINE_INSTANCE":"shortstudio"}):
            with self.assertRaises(Stop) as e:
                obj._wait_for_connection(1,{},0,"network lost",None,None)
        self.assertIn("AUTO_NETWORK_WAIT",str(e.exception))
        self.assertEqual(updates[0]["status"],"WAITING_NETWORK")
        self.assertIn("retry_network_waits(db,store)",WORKER.read_text())

    def test_parked_wait_not_turned_into_review_required_after_restart(self):
        self.assertIn("error_message LIKE 'AUTO_NETWORK_WAIT|%'",REPO.read_text())
        self.assertIn("error_message LIKE 'AUTO_PROXY_WAIT|%'",REPO.read_text())

if __name__=="__main__": unittest.main()

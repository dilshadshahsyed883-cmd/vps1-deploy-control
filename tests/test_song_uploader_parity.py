"""Song Uploader parity regressions: offline, no production mutations."""
from __future__ import annotations

import ast
import os
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from jinja2 import Environment

ROOT=Path(__file__).resolve().parents[1]
PART=ROOT/"apps/local-upload-manager/part01"
APP=(PART/"templates/app.html").read_text()
ENGINE=(PART/"lum_engine/auto_engine_v6.py").read_text()
SCHEDULER=(PART/"lum_engine/auto_engine_v6_scheduler.py").read_text()
WORKER=(PART/"lum_engine/auto_engine_v6_worker.py").read_text()
ROUTES=(PART/"lum_engine/standalone_app.py").read_text()
CSS=(PART/"static/local_upload_manager.css").read_text()


class SongParity(unittest.TestCase):
    def test_template_is_valid_and_song_ui_has_safe_profile_controls(self):
        Environment().parse(APP)
        self.assertIn("data-song-group-summary", APP)
        self.assertIn('id="song-profile-metadata-source"', APP)
        self.assertIn('id="song-profile-video-source"', APP)
        self.assertIn('song-profile-privacy-status', APP)
        self.assertIn('data-short-schedule-slots data-short-profile-schedule', APP)
        self.assertIn("engine_instance in ['shortstudio','song']", APP)
        self.assertIn("data-ae-open-schedules>Manage Schedules", APP)
        self.assertIn('data-short-profile-action="start"', APP)
        self.assertIn('data-ae-panel-target="queue"', APP)
        self.assertIn("data-song-group-channel-count", APP)
        self.assertIn('value="songfolder"', APP)
        self.assertIn("Preserve existing visibility (Sheets / Private)", APP)

    def test_song_automation_no_queue_generate_control_and_still_keeps_sync(self):
        part=APP.split('<section class="lum-card lum-ae-automation-controls"',1)[1].split("</section>",1)[0]
        self.assertNotIn('Generate Queue Now',part)
        self.assertIn('Profile Schedules',part)
        self.assertIn('Preview Next Jobs',part)
        self.assertIn('Start All Profiles',part)
        self.assertIn('Sync All Channels',part)

    def test_song_responsive_layout_and_runtime_status_are_song_scoped(self):
        self.assertIn('.lum-shell[data-engine-instance="song"] .lum-short-engine-tabbar',CSS)
        self.assertIn('.lum-shell[data-engine-instance="song"] #lum-ae-profile-form .lum-short-profile-grid',CSS)
        self.assertIn('grid-template-columns:repeat(2,minmax(0,1fr))',CSS)
        self.assertIn('grid-template-columns:minmax(0,1fr)',CSS)
        self.assertIn('data-engine-instance="shortstudio"',CSS)
        self.assertIn('data-ae-panel-target="records"',APP)

    def test_song_profile_start_and_stop_are_real_scoped_backend_actions(self):
        self.assertIn('Individual Start is only available in Song/Short Uploader.',ROUTES)
        self.assertIn('Individual Pause is only available in Song/Short Uploader.',ROUTES)
        self.assertIn('allowed_auto_engine_profile_ids',WORKER)
        self.assertIn('manual_profile_ids',WORKER)
        self.assertIn('in {"shortstudio","song"}',WORKER)
        self.assertIn('in {"shortstudio","song"}: v6_store.set_manual_profile_ids(None)',ROUTES)

    def test_existing_legacy_privacy_is_unchanged_until_explicit_override(self):
        self.assertIn('if privacy!="inherit": settings["privacy_status"]=privacy',ENGINE)
        self.assertIn('if song_group and settings.get("privacy_status"):',ENGINE)
        self.assertIn('meta["privacy_status"]=settings["privacy_status"]',ENGINE)
        self.assertIn('"privacy_status":settings.get("privacy_status") or "private"',ENGINE)
        self.assertIn('expected one original matching thumbnail.',ENGINE)
        self.assertNotIn('Delete VPS source after verified upload</option>',APP.split("{% if engine_instance == 'song' %}",1)[0])

    def test_song_scheduler_skips_old_slot_and_runs_recent_one(self):
        module=ast.parse(SCHEDULER)
        fn=next(n for n in module.body if isinstance(n,ast.FunctionDef) and n.name=="check_profile")
        new=ast.Module(body=[ast.ImportFrom(module="__future__",names=[ast.alias(name="annotations")],level=0),fn],type_ignores=[])
        class Frozen:
            current=datetime(2026,10,30,19,0)
            @classmethod
            def now(cls,*args):
                return cls.current
        class DB:
            def __init__(self):self.skips=[];self.logs=[]
            def auto_engine_run_exists(self,*args):return False
            def record_auto_engine_run(self,*args):self.skips.append(args)
            def add_log(self,*args):self.logs.append(args)
        calls=[]
        def gen(*args,**kwargs):
            calls.append(kwargs)
            return {"created":1}
        ns=dict(os=os,datetime=Frozen,SCHEDULE_TZ=None,GRACE_MINUTES=6,
                SHORT_STUDIO_GRACE_MINUTES=10,
                channel_profile_scheduler_tick=lambda *args:-1,
                parse_schedule_times=lambda times:times.split(","),
                target_channels=lambda *args:[{"id":1}],generate_queue=gen)
        exec(compile(ast.fix_missing_locations(new),SCHEDULER,"exec"),ns)
        profile=dict(id=8,enabled=1,schedule_enabled=1,work_mode="auto",
                     category_name="Music",schedule_times="06:00",daily_upload_count=1)
        with patch.dict(os.environ,{"LUM_ENGINE_INSTANCE":"song"}):
            db=DB()
            self.assertEqual(ns["check_profile"](db,object(),profile),0)
            self.assertEqual(len(db.skips),1)
            self.assertEqual(len(calls),0)
            profile["schedule_times"]="18:55"
            self.assertEqual(ns["check_profile"](DB(),object(),profile),1)
            self.assertEqual(len(calls),1)
            profile["schedule_times"]="18:49"
            self.assertEqual(ns["check_profile"](DB(),object(),profile),0)
            self.assertEqual(len(calls),1)


if __name__=="__main__":
    unittest.main()

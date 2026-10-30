"""Offline group queue tests: no network, scheduling activation or uploads."""
import ast
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jinja2 import Environment

ROOT=Path(__file__).resolve().parents[1]
ENGINE=ROOT/'apps/local-upload-manager/part01/lum_engine/auto_engine_v6.py'
TEMPLATE=ROOT/'apps/local-upload-manager/part01/templates/app.html'


def load_functions(names, ns, first=False):
    tree=ast.parse(ENGINE.read_text())
    nodes=[]
    for name in names:
        found=[n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name==name]
        nodes.append(found[0] if first else found[-1])
    exec(compile(ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), *nodes], type_ignores=[])),str(ENGINE),'exec'),ns)


class PC:
    def __init__(self): self.profiles={}; self.jobs=[]; self.claimed=set()
    def get_auto_engine_profile(self,pid): return self.profiles.get(pid)
    def list_channel_groups(self): return [{'id':7}]
    def list_channel_group_members(self,gid,**kw): return [{'id':1},{'id':2}] if gid==7 else []
    def save_auto_engine_profile(self,data,pid=None):
        pid=pid or len(self.profiles)+1
        self.profiles[pid]={'id':pid,**data}; return pid
    def connect(self): return sqlite3.connect(':memory:')
    def get_auto_engine_thumbnail_cursor(self,pid): return 0
    def advance_auto_engine_thumbnail_cursor(self,*args): pass
    def is_channel_filename_duplicate(self,cid,filename): return filename=='duplicate.mp4'
    def add_video(self,payload): self.jobs.append(payload); return len(self.jobs)
    def claim_auto_engine_run(self,pid,key):
        if key in self.claimed:return False
        self.claimed.add(key);return True
    def record_auto_engine_run(self,*args): pass


class Store:
    def __init__(self,root): self.path=root/'v6.sqlite'; self.saved_source={}; self.reserved=[]
    def connect(self): return sqlite3.connect(self.path)
    def source(self,pid): return self.saved_source
    def save_source(self,pid,**kw): self.saved_source=kw
    def channel_binding(self,pid):return {}
    def import_pc_filename_history(self,pc):pass
    def scan_drive_videos(self,pid):return {'videos':5}
    def thumbs(self,pid):return []
    def is_duplicate(self,*args):return (False,'')
    def reserve_job_source(self,**kw):self.reserved.append(kw)
    def drive_videos(self,*args):
        return [dict(id=i,drive_file_id=f'id-{i}',filename=f,script_key=f,normalized_filename=f,
                     source_folder_id='shortstudio-local:Test',size_bytes=1)
                for i,f in enumerate(['duplicate.mp4','short 1.mp4','short 2.mp4','short 3.mp4','short 4.mp4'],1)]


class ShortGroupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);(self.root/'Test').mkdir()
        self.pc=PC();self.store=Store(self.root)
        self.env=patch.dict(os.environ,{'LUM_ENGINE_INSTANCE':'shortstudio'});self.env.start();self.addCleanup(self.env.stop)
        self.ns={'os':os,'json':json,'Path':Path,'clean':lambda x:str(x or '').strip(),
                 'shortstudio_folder_path':lambda x:self.root/x,'thumbnail_required_for_mode':lambda:False,
                 'target_channels':lambda *args:[{'id':1,'channel_id':'UC1'},{'id':2,'channel_id':'UC2'}]}
        load_functions(['short_group_metadata','save_short_group_profile','category_metadata','metadata_for_job','apply_vars'],self.ns)
        self.ns['_CHANNEL_LEGACY_SAVE_PROFILE']=lambda db,store,fields,pid:db.save_auto_engine_profile(fields,pid)
        self.form=dict(category_name='Test',channel_group_id='7',metadata_source='manual',
                       manual_title='Short {video_stem}',manual_description='Saved',manual_category_id='24',
                       short_vps_folder='Test',daily_upload_count=2,upload_scope='per_channel',work_mode='auto',
                       schedule_times='09:00,14:00',enabled='1',schedule_enabled='0')
    def save(self,form=None,pid=None):return self.ns['save_short_group_profile'](self.pc,self.store,form or self.form,pid)
    def test_group_only_validation_and_legacy_channel_preserved(self):
        for changes in ({'channel_group_id':''},{'channel_group_id':'999'},{'channel_db_id':'1'},{'profile_kind':'channel'}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):self.save({**self.form,**changes})
        self.assertEqual(self.pc.profiles,{})
        load_functions(['save_profile_from_form'],self.ns)
        self.ns['save_short_group_profile']=lambda *a:77
        self.assertEqual(self.ns['save_profile_from_form'](self.pc,self.store,self.form),77)
    def test_persistent_settings_switching_retains_both_sources_without_arming(self):
        pid=self.save()
        self.assertEqual(self.pc.profiles[pid]['schedule_enabled'],'0')
        self.assertEqual(self.ns['short_group_metadata'](self.store,pid)['privacy_status'],'public')
        sheet={**self.form,'metadata_source':'sheet','metadata_sheet_url_or_id':'sheet-id','metadata_sheet_tab':'Tab','metadata_sheet_gid':'12'}
        self.save(sheet,pid)
        # Simulate a process restart, and a partial mode change that omits inactive fields.
        reopened=Store(self.root)
        saved=self.ns['short_group_metadata'](reopened,pid)
        self.assertEqual(saved['manual_title'],'Short {video_stem}')
        self.assertEqual(saved['privacy_status'],'public')
        self.save({k:v for k,v in self.form.items() if not k.startswith('manual_')},pid)
        self.assertEqual(self.pc.profiles[pid]['metadata_sheet_gid'],'12')
        self.assertEqual(self.ns['short_group_metadata'](reopened,pid)['metadata_source'],'manual')
    def test_group_planner_uses_manual_metadata_quota_and_duplicate_guards(self):
        pid=self.save();profile=self.pc.profiles[pid]
        self.store.saved_source={'short_folder_name':'Test'}
        load_functions(['planner'],self.ns,first=True)
        self.ns['fetch_sheet_csv_gid']=self.ns['fetch_sheet_csv_tab']=lambda *a,**kw: self.fail('Sheet read in manual mode')
        plan=self.ns['planner'](self.pc,self.store,profile,preview_only=False)
        self.assertTrue(plan['ready']);self.assertEqual(len(plan['jobs']),4)
        self.assertEqual([j['channel_db_id'] for j in plan['jobs']],[1,1,2,2])
        self.assertEqual(plan['jobs'][0]['title'],'Short short 1')
        self.assertTrue(all(j['category_id']=='24' for j in plan['jobs']))
        self.assertTrue(all(j['privacy_status']=='public' for j in plan['jobs']))
        self.assertGreater(plan['duplicates_blocked'],0)
        self.assertEqual(len({j['drive_file_id'] for j in plan['jobs']}),4)
        load_functions(['generate_queue'],self.ns,first=True)
        self.ns.update(STAGING_ROOT=self.root,safe_slug=lambda x,*a:x,DuplicateUploadError=RuntimeError)
        result=self.ns['generate_queue'](self.pc,self.store,profile,run_key='slot-1')
        self.assertEqual(result['created'],4)
        again=self.ns['generate_queue'](self.pc,self.store,profile,run_key='slot-1')
        self.assertTrue(again['already_claimed']);self.assertEqual(len(self.pc.jobs),4)
        self.assertTrue(all('metadata_sheet_url' not in j for j in self.pc.jobs))
        self.assertTrue(all(j['privacy_status']=='public' for j in self.pc.jobs))
        self.assertTrue(all(j['source_folder_id']=='shortstudio-local:Test' for j in [r['drive_video'] for r in self.store.reserved]))
    def test_saved_metadata_is_loaded_into_profile_editor(self):
        pid=self.save()
        profile=self.pc.profiles[pid]
        self.pc.list_channels=lambda **kw:[]
        self.ns['_CHANNEL_LEGACY_PROFILE_CONTEXT']=lambda *a:{'profiles':[profile], 'selected':profile, 'queue':[]}
        load_functions(['profile_context'],self.ns)
        ctx=self.ns['profile_context'](self.pc,self.store,pid)
        self.assertEqual(ctx['selected']['manual_title'],'Short {video_stem}')
        self.assertEqual(ctx['selected']['metadata_source'],'manual')
        self.assertEqual(ctx['selected']['privacy_status'],'public')

    def test_profile_schedule_requires_explicit_opt_in_and_does_not_arm_engine(self):
        pid=self.save()
        self.assertEqual(self.pc.profiles[pid]['schedule_enabled'],'0')
        self.save({**self.form,'schedule_enabled':'1'},pid)
        self.assertEqual(self.pc.profiles[pid]['schedule_enabled'],'1')
        self.save({**self.form,'schedule_enabled':'0'},pid)
        self.assertEqual(self.pc.profiles[pid]['schedule_enabled'],'0')

    def test_group_scheduler_honors_opt_in_channel_count_and_alarm_idempotency(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        scheduler=ROOT/'apps/local-upload-manager/part01/lum_engine/auto_engine_v6_scheduler.py'
        fn=next(n for n in ast.parse(scheduler.read_text()).body if isinstance(n,ast.FunctionDef) and n.name=='check_profile')
        class Clock:
            @staticmethod
            def now(tz):return datetime(2026,10,30,9,0,tzinfo=tz)
        runs=set();calls=[]
        self.pc.auto_engine_run_exists=lambda pid,key:key in runs
        self.pc.add_log=lambda *args:None
        self.store.set_engine=lambda **kw:None
        def generate(db,store,profile,**kw):
            calls.append(kw);runs.add(kw['run_key']);return {'created':kw['max_jobs_override']}
        ns=dict(Database=PC,V6Store=Store,datetime=Clock,SCHEDULE_TZ=ZoneInfo('Asia/Karachi'),GRACE_MINUTES=6,SHORT_STUDIO_GRACE_MINUTES=10,
                channel_profile_scheduler_tick=lambda *a:-1,parse_schedule_times=lambda s:s.split(','),
                target_channels=lambda *a:[{'id':1},{'id':2}],generate_queue=generate)
        exec(compile(ast.Module(body=[fn],type_ignores=[]),str(scheduler),'exec'),ns)
        profile=dict(id=1,category_name='Group',enabled=1,work_mode='auto',schedule_enabled=0,
                     schedule_times='09:00',daily_upload_count=4,upload_scope='per_channel',missed_schedule_policy='run_missed_today')
        self.assertEqual(ns['check_profile'](self.pc,self.store,profile),0)
        self.assertFalse(calls)
        profile['schedule_enabled']=1
        self.assertEqual(ns['check_profile'](self.pc,self.store,profile),2)
        self.assertEqual(calls[0]['max_jobs_override'],2)
        self.assertEqual(ns['check_profile'](self.pc,self.store,profile),0)
        self.assertEqual(len(calls),1)

    def test_sheet_mode_keeps_existing_metadata_reader(self):
        self.ns['extract_gid']=lambda *a,**kw:''
        calls=[]
        self.ns['fetch_sheet_csv_gid']=lambda *a,**kw: calls.append(a) or [{'title':'Sheet title'}]
        self.ns['fetch_sheet_csv_tab']=lambda *a,**kw:self.fail('GID must win')
        meta,_=self.ns['category_metadata']({'metadata_sheet_url_or_id':'id','metadata_sheet_tab':'Tab','metadata_sheet_gid':'12'})
        self.assertEqual(meta['title'],'Sheet title');self.assertEqual(calls,[('id','12')])
    def test_visibility_is_profile_scoped_persistent_and_applies_to_all_metadata_sources(self):
        pid=self.save({**self.form,'privacy_status':'unlisted'})
        saved=self.ns['short_group_metadata'](self.store,pid)
        self.assertEqual(saved['privacy_status'],'unlisted')
        self.store.saved_source={'short_folder_name':'Test'}
        load_functions(['planner'],self.ns,first=True)
        plan=self.ns['planner'](self.pc,self.store,self.pc.profiles[pid],preview_only=False)
        self.assertTrue(all(j['privacy_status']=='unlisted' for j in plan['jobs']))
        # Sheet visibility must not override the selected Short Uploader profile.
        self.ns['extract_gid']=lambda *a,**kw:'12'
        self.ns['fetch_sheet_csv_gid']=lambda *a,**kw:[{'title':'Sheet title','privacy_status':'private'}]
        sheet_profile={'metadata_sheet_url_or_id':'sheet-id','metadata_sheet_tab':'Tab',
                      'metadata_sheet_gid':'12','_short_group_metadata':{**saved,'metadata_source':'sheet'}}
        metadata,_=self.ns['category_metadata'](sheet_profile)
        self.assertEqual(metadata['privacy_status'],'unlisted')
        self.save({**self.form,'privacy_status':'private'},pid)
        self.assertEqual(self.ns['short_group_metadata'](Store(self.root),pid)['privacy_status'],'private')
        self.assertEqual(self.ns['category_metadata']({
            '_short_group_metadata':self.ns['short_group_metadata'](self.store,pid)})[0]['privacy_status'],'private')
        for invalid in ('scheduled','friends',''):
            with self.subTest(invalid=invalid),self.assertRaises(ValueError):
                self.save({**self.form,'privacy_status':invalid},pid)

    def test_new_short_group_approved_defaults_and_existing_edits(self):
        # When a field is omitted on first creation, the server chooses the
        # requested defaults; an existing saved profile is never rewritten.
        minimal=dict(category_name='New',channel_group_id='7',
                     short_vps_folder='Test',manual_title='Test {video_stem}')
        pid=self.save(minimal)
        profile=self.pc.profiles[pid]
        settings=self.ns['short_group_metadata'](self.store,pid)
        self.assertEqual(settings['metadata_source'],'manual')
        self.assertEqual(settings['privacy_status'],'public')
        for field, expected in (
            ('enabled','1'),('daily_upload_count','1'),
            ('schedule_times','06:00'),('upload_scope','per_channel'),
            ('work_mode','auto'),('after_upload_action','delete'),
            ('schedule_enabled','1')):
            self.assertEqual(profile[field],expected,field)
        # Editing with explicit alternatives must retain each chosen value.
        edited={**self.form,'daily_upload_count':'2',
                'schedule_times':'09:00,14:00','enabled':'0',
                'upload_scope':'total_category','work_mode':'manual_review',
                'after_upload_action':'keep','schedule_enabled':'0',
                'metadata_source':'sheet','metadata_sheet_url_or_id':'sheet-id',
                'metadata_sheet_tab':'Tab'}
        self.save(edited,pid)
        profile=self.pc.profiles[pid]
        self.assertEqual(profile['daily_upload_count'],'2')
        self.assertEqual(profile['schedule_times'],'09:00,14:00')
        self.assertEqual(profile['work_mode'],'manual_review')
        self.assertEqual(profile['after_upload_action'],'keep')
        self.assertEqual(profile['schedule_enabled'],'0')
        self.assertEqual(profile['enabled'],'0')
        self.assertEqual(self.ns['short_group_metadata'](self.store,pid)['metadata_source'],'sheet')

    def test_engine_status_badges_share_shortstudio_subtab_row(self):
        source=TEMPLATE.read_text()
        self.assertIn('class="lum-short-engine-tabbar"',source)
        self.assertIn('class="lum-ae-health lum-short-engine-status"',source)
        nav=source.index('aria-label="Auto Engine workspace"')
        badges=source.index('class="lum-ae-health lum-short-engine-status"')
        self.assertGreater(badges,nav)
        self.assertLess(badges,source.index('data-ae-panel="profiles"',nav))
        for label in ('Worker {{ ae.scheduler_ui.worker_status }}',
                      'Scheduler {{ ae.scheduler_ui.scheduler_status }}',
                      "Engine {{ 'RUNNING' if ae.engine.running else 'STOPPED' }}"):
            self.assertIn(label,source[badges:badges+1500])
        styles=(ROOT/'apps/local-upload-manager/part01/static/local_upload_manager.css').read_text()
        self.assertIn('.lum-short-engine-tabbar{display:flex;',styles)
        self.assertIn('.lum-short-engine-status{display:flex;',styles)
        self.assertIn('flex:1 1 100%',styles)

    def test_redundant_auto_engine_header_removed_from_short_uploader(self):
        source=TEMPLATE.read_text()
        start=source.index('    <section class="lum-ae-head">')
        begin=source.rfind("{% if engine_instance not in ['song','shortstudio'] %}",0,start)
        tabs=source.index('    <nav class="lum-ae-tabs lum-ae-tabs-primary" aria-label="Auto Engine workspace">',start)
        header=source[begin:tabs]
        self.assertIn('class="lum-ae-head"',header)
        self.assertIn('Worker {{ ae.scheduler_ui.worker_status }}',header)
        self.assertIn('Scheduler {{ ae.scheduler_ui.scheduler_status }}',header)
        self.assertIn('{% endif %}',header)
        self.assertLess(header.index('</section>'),header.rindex('{% endif %}'))
        self.assertIn('data-ae-panel-target="automation"',source[tabs:])
        self.assertIn('data-ae-panel-target="profiles"',source[tabs:])
        self.assertIn('data-ae-panel-target="queue"',source[tabs:])
        self.assertIn('data-ae-panel-target="records"',source[tabs:])

    def test_short_group_form_uses_two_fields_per_row_and_selected_defaults(self):
        source=TEMPLATE.read_text()
        start=source.index('    <form id="lum-ae-profile-form"')
        end=source.index('</form>',start)+len('</form>')
        template=Environment().from_string(source[start:end])
        context=dict(engine_instance='shortstudio',p=None,src={},csrf='test',
                     base_path='/short-uploader',
                     ae=SimpleNamespace(groups=[dict(id=7,group_name='Test',channel_count=2)],
                                        short_folders=['Test']))
        html=template.render(**context)
        self.assertEqual(html.count('data-short-profile-grid'),1)
        self.assertEqual(html.count('class="lum-short-profile-grid"'),0)
        self.assertEqual(html.count('lum-short-profile-grid'),2)
        self.assertLess(html.index('name="channel_group_id"'),html.index('name="category_name"'))
        self.assertIn('name="daily_upload_count" type="number" min="1" max="100" value="1"',html)
        self.assertIn('name="schedule_times" data-short-schedule-csv value="06:00"',html)
        self.assertIn('<option value="auto" selected>',html)
        self.assertIn('<option value="delete" selected>',html)
        self.assertIn('name="schedule_enabled" value="1" checked',html)
        self.assertIn('<option value="manual" selected>Manual Metadata',html)
        self.assertIn('<option value="per_channel" selected>',html)
        self.assertIn('<option value="public" selected>',html)
        self.assertIn('name="enabled" value="1" checked',html)
        css=(ROOT/'apps/local-upload-manager/part01/static/local_upload_manager.css').read_text()
        self.assertIn('.lum-short-profile-grid{display:grid!important;grid-template-columns:repeat(2,minmax(0,1fr))!important',css)
        self.assertIn('@media(max-width:760px)',css)
        legacy=template.render(**{**context,'engine_instance':'song'})
        self.assertNotIn('data-short-profile-grid',legacy)
        self.assertIn('name="privacy_status"',legacy)  # Song group receives its own saved visibility choice.

    def test_new_group_template_and_legacy_profile_edit(self):
        source=TEMPLATE.read_text();start=source.index('    <form id="lum-ae-profile-form"');end=source.index('</form>',start)+len('</form>')
        template=Environment().from_string(source[start:end])
        data=dict(engine_instance='shortstudio',p=None,src={},csrf='test',base_path='/short-uploader',
                  ae=SimpleNamespace(groups=[dict(id=7,group_name='Test',channel_count=2)],short_folders=['Test']))
        html=template.render(**data)
        self.assertNotIn('Profile Type',html);self.assertNotIn('name="channel_db_id"',html)
        for field in ('channel_group_id','short_vps_folder','drive_video_folder','metadata_source','manual_title','privacy_status','schedule_enabled','daily_upload_count','schedule_times','upload_scope','work_mode'):
            self.assertIn('name="'+field+'"',html)
        self.assertNotIn('manual_upload_date',html) # Existing group alarm scheduler owns times.
        self.assertIn('id="short-profile-privacy-status"',html)
        self.assertIn('lum-short-compact-selector',html)
        self.assertEqual(html.count('name="privacy_status"'),1)
        self.assertLess(html.index('name="privacy_status"'),html.index('name="daily_upload_count"'))
        self.assertIn('<option value="public" selected>',html)
        old=template.render(**{**data,'p':dict(id=3,channel_db_id=1,enabled=True),'ae':SimpleNamespace(all_channels=[dict(id=1,channel_id='UC1')],short_folders=[])})
        self.assertIn('name="channel_db_id"',old);self.assertNotIn('short-profile-metadata-source',old)
    def test_group_save_keeps_short_folder_and_scheduling_uses_existing_lane(self):
        # Compile the actual original group saver and inspect its source delegation.
        tree=ast.parse(ENGINE.read_text());func=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='save_profile_from_form')
        self.assertIn('short_vps_folder',ast.unparse(func))
        scheduler=(ROOT/'apps/local-upload-manager/part01/lum_engine/auto_engine_v6_scheduler.py').read_text()
        self.assertIn('if int(profile.get("schedule_enabled") or 0)!=1:',scheduler)
        self.assertIn('run_key=run_key,max_jobs_override=per_alarm_jobs',scheduler)

if __name__=='__main__':unittest.main()

class ShortGroupReleaseTests(unittest.TestCase):
    def test_scoped_release_and_failure_rollback_preserve_other_units(self):
        from production import short_uploader_group_release as release
        for fail in (False,True):
            with self.subTest(fail=fail),tempfile.TemporaryDirectory() as td:
                root=Path(td);base=root/'host';units=root/'units';units.mkdir()
                old=base/'releases/local-upload-manager/old/part01';old.mkdir(parents=True)
                for relative in release.FILES:
                    dst=old/relative;dst.parent.mkdir(parents=True,exist_ok=True);dst.write_text('old')
                other=units/'song.service';other.write_text('do not modify')
                originals={}
                for name in release.UNITS:
                    text=f'[Service]\nWorkingDirectory={old}\nExecStart=/old/venv/bin/python -m worker\nEnvironment=KEEP=1\n'
                    (units/name).write_text(text);originals[name]=text
                calls=[]
                verify=lambda:None
                if fail:
                    def verify(): raise RuntimeError('verification failed')
                with patch.multiple(release,BASE=base,UNIT_DIR=units,REPO=ROOT,STATE=root/'state',
                                    idle=lambda:None,health=lambda:{'ok':True},verify_live=verify,
                                    backup_databases=lambda *a:[],command=lambda *a:calls.append(a)), \
                     patch.object(release.subprocess,'run',return_value=SimpleNamespace(returncode=0)), \
                     patch.object(release.shutil,'disk_usage',return_value=SimpleNamespace(free=10_000_000_000)):
                    if fail:
                        with self.assertRaisesRegex(RuntimeError,'verification failed'):release.install('a'*40)
                    else:release.install('a'*40)
                self.assertEqual(other.read_text(),'do not modify')
                self.assertTrue(all(c[-1] in release.UNITS or c==('systemctl','daemon-reload') for c in calls))
                for name in release.UNITS:
                    text=(units/name).read_text()
                    if fail:self.assertEqual(text,originals[name])
                    else:
                        self.assertIn('a'*40+'-short-group-metadata/part01',text)
                        self.assertIn('Environment=KEEP=1',text)
                        self.assertIn('/old/venv/bin/python',text)
    def test_network_release_includes_existing_ui_and_retry_actions(self):
        from production import short_uploader_group_release as release
        self.assertEqual(set(release.NETWORK_ONLY_FILES),set(release.FILES)|{'lum_engine/auto_engine_v6_scheduler.py'})
        self.assertIn('lum_engine/auto_engine_v6_worker.py',release.NETWORK_ONLY_FILES)
        self.assertIn('lum_engine/auto_engine_v6_scheduler.py',release.NETWORK_ONLY_FILES)
        self.assertIn('desktop_core/modules/db/queue_repository.py',release.NETWORK_ONLY_FILES)
        self.assertIn('lum_engine/standalone_app.py',release.NETWORK_ONLY_FILES)
        self.assertIn('static/auto_engine_v6.js',release.NETWORK_ONLY_FILES)
        self.assertIn('templates/app.html',release.NETWORK_ONLY_FILES)

    def test_busy_upload_blocks_scoped_release(self):
        from production import short_uploader_group_release as release
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);db=root/'core/data/uploader.db';db.parent.mkdir(parents=True)
            with sqlite3.connect(db) as con:
                con.execute('CREATE TABLE videos (status TEXT)');con.execute("INSERT INTO videos VALUES ('UPLOADING')")
            with patch.multiple(release,STATE=root,health=lambda:{'active_project_id':None}):
                with self.assertRaisesRegex(RuntimeError,'upload in progress'):release.idle()


class ShortAutomationGroupSummaryTests(unittest.TestCase):
    def render(self, instance='shortstudio', profiles=None):
        source=TEMPLATE.read_text()
        start=source.index('<section class="lum-card lum-ae-automation-controls"')
        end=source.index('</section>',start)+len('</section>')
        return Environment(autoescape=True).from_string(source[start:end]).render(
            engine_instance=instance, ae=SimpleNamespace(profiles=profiles or []), p=None,
            base_path='/short-uploader', csrf='offline-test')
    def test_group_name_channel_count_and_saved_times_are_visible(self):
        html=self.render(profiles=[dict(id=7,category_name='Shorts',group_name='Aina Asif',
            group_channel_count=12,enabled=1,v6_video_count=8,schedule_times='09:00,14:30',schedule_enabled=1)])
        for text in ('<th>Group Name</th>','<th>Channels</th>','<th>Schedule (PKT)</th>',
                     'Aina Asif','data-short-group-channel-count="7">12</td>','09:00 AM','02:30 PM'):
            self.assertIn(text,html)
        for text in ('<th>Channel ID</th>','<th>Thumbnails</th>','<th>Auto Sync</th>','Legacy'):
            self.assertNotIn(text,html)
        self.assertNotIn('data-ae-sync-scan',html)
        self.assertNotIn('Sync Channel</button>',html)
        self.assertEqual(html.count('>Sync All Channels</button>'),1)
        self.assertEqual(html.count('/actions/auto-v6/sync-all'),1)
        toolbar=html.split('lum-ae-bulk-profile-controls',1)[1].split('</div>',1)[0]
        self.assertIn('Profile Schedules',toolbar)
        self.assertNotIn('Generate Queue Now',toolbar)
        self.assertNotIn('/actions/auto-v6/generate-all',toolbar)
        self.assertEqual(toolbar.count('data-ae-open-schedules'),1)
        self.assertLess(toolbar.index('Profile Schedules'),toolbar.index('Preview Next Jobs'))
        self.assertIn('Start All Profiles',toolbar)
        self.assertIn('Sync All Channels',toolbar)
        self.assertLess(toolbar.index('Start All Profiles'),toolbar.index('Sync All Channels'))
        self.assertNotIn('auto_profile_id=7',html)
        self.assertNotIn('>Pause</button>',html)
        self.assertNotIn('>Edit</a>',html)
        self.assertEqual(html.count('>Start</button>'),1)
    def test_zero_count_and_disabled_schedule_are_not_misrepresented(self):
        html=self.render(profiles=[dict(id=9,category_name='Empty group',group_name='Empty',
            group_channel_count=0,enabled=1,schedule_times='18:00',schedule_enabled=0)])
        self.assertIn('data-short-group-channel-count="9">0</td>',html)
        self.assertIn('06:00 PM',html);self.assertNotIn('Schedule OFF',html)
        self.assertNotIn('15 min',html)
    def test_legacy_single_profile_stays_readable_without_fake_group_schedule(self):
        html=self.render(profiles=[dict(id=1,category_name='Older profile',channel_db_id=4,
            enabled=1,schedule_times='09:00')])
        self.assertIn('Single channel',html)
        self.assertIn('data-short-group-channel-count="1">1</td>',html)
        self.assertIn('Per-video schedule',html)
        self.assertNotIn('09:00',html)
    def test_schedule_dialog_and_song_compact_controls_are_preserved(self):
        template=TEMPLATE.read_text()
        self.assertIn('id="lum-ae-schedules-dialog"',template)
        self.assertIn('/actions/auto-v6/profile-schedules/save',template)
        self.assertIn('name="schedule_times_{{ pr.id }}"',template)
        self.assertIn('data-short-schedule-slots data-short-count=',template)
        self.assertIn('data-ae-open-schedules>Manage Schedules</button>',template)
        song=self.render(instance='song',profiles=[dict(id=3,category_name='Song',group_name='Song',
                group_channel_count=2,enabled=1,schedule_times='08:00',schedule_enabled=1)])
        row=song.split('lum-ae-bulk-profile-controls',1)[1].split('</div>',1)[0]
        self.assertNotIn('Generate Queue Now',row)
        self.assertNotIn('/actions/auto-v6/generate-all',row)
        self.assertIn('Profile Schedules',row)

    def test_other_uploaders_retain_their_existing_columns_and_values(self):
        for instance in ('engine1','engine2','shorts'):
            with self.subTest(instance=instance):
                html=self.render(instance,[dict(id=3,enabled=1,category_name='Old',
                    bound_channel_name='Channel',bound_channel_id='UC123',sync_interval_minutes=15)])
                for text in ('<th>Channel ID</th>','<th>Thumbnails</th>','<th>Auto Sync</th>','UC123','15 min'):
                    self.assertIn(text,html)
                self.assertNotIn('data-short-group-summary',html)
                self.assertIn('data-ae-sync-scan',html)
                self.assertIn('Sync Channel</button>',html)
    def test_empty_table_and_escaped_group_names(self):
        self.assertIn('colspan="9"',self.render())
        html=self.render(profiles=[dict(id=1,enabled=1,category_name='Test',group_name='<script>bad</script>')])
        self.assertIn('&lt;script&gt;bad&lt;/script&gt;',html)
        self.assertNotIn('<script>bad</script>',html)


class ShortUploaderIntroOnlyReleaseTests(unittest.TestCase):
    def test_ui_only_release_targets_template_without_sibling_restart(self):
        runtime=(ROOT/'production/runtime.py').read_text()
        overlay=(ROOT/'production/short_uploader_group_release.py').read_text()
        workflow=(ROOT/'.github/workflows/vps1-short-studio-release.yml').read_text()
        self.assertIn("if '[vps1-shortstudio-intro-only]' in intent:",runtime)
        self.assertIn("args.apps != ['local-upload-manager']",runtime)
        self.assertIn('install_shortstudio_ui(args.revision, ui_only=True)',runtime)
        self.assertIn('for relative in (NETWORK_ONLY_FILES if network_only else ("templates/app.html",) if ui_only else FILES):',overlay)
        self.assertIn("'short-uploader-network-fairness' if network_only else",overlay)
        self.assertIn('elif ui_only:verify_live(ui_only=True)',overlay)
        self.assertIn("apps=(local-upload-manager)",workflow)
        self.assertIn('for app in "${apps[@]}"; do',workflow)
        self.assertIn("AUTO_SCRIPT=OFFLINE",workflow)

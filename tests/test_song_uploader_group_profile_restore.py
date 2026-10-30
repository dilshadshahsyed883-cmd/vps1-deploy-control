"""Exercise group source combinations against real isolated SQLite stores."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from jinja2 import Environment

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'apps/local-upload-manager/part01'))
from desktop_core.modules.database import Database
from lum_engine import auto_engine_v6 as engine


class GroupProfileRegression(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        for key in ('DATA_ROOT','THUMB_ROOT','TRASH_ROOT','STAGING_ROOT','REPORT_ROOT'):
            p=patch.object(engine,key,self.root/key);p.start();self.addCleanup(p.stop)
        p=patch.dict(os.environ,{'LUM_ENGINE_INSTANCE':'song'});p.start();self.addCleanup(p.stop)
        self.db=Database(self.root/'pc.sqlite')
        self.store=engine.V6Store(self.root/'engine.sqlite')
        self.channels=[self.db.add_or_update_channel(channel_id='UC'+str(i)*22,
            channel_name=f'Channel {i}',google_account_email='',token_file_path=f'token{i}') for i in range(1,4)]
        self.group=self.db.save_channel_group('Music',self.channels)
        self.db.set_channel_status(self.channels[-1],'INACTIVE')
        p=patch.object(engine,'V6Store',return_value=self.store);p.start();self.addCleanup(p.stop)
        p=patch.object(engine,'classify_google_json',return_value=engine.VALID_TOKEN);p.start();self.addCleanup(p.stop)
        self.form=dict(profile_kind='group',channel_group_id=str(self.group),category_name='Daily Music',
            daily_upload_count='1',upload_scope='per_channel',schedule_times='09:00,14:00',
            work_mode='manual_review',enabled='1',video_source_mode='songmaker',metadata_source='manual',
            manual_title='Song {video_stem}',manual_description='Original audio',manual_category_id='10')

    def save(self,**fields):
        return engine.save_profile_from_form(self.db,self.store,{**self.form,**fields})

    def seed(self,pid,local=True,missing=False):
        folder='songmaker-local' if local else 'drivevideos'
        with self.store.connect() as con:
            for i in range(2):
                fid=f'songmaker:{pid}_{i}' if local else f'video{pid}_{i}'
                con.execute('''INSERT INTO drive_videos(profile_id,drive_file_id,filename,normalized_filename,
                    script_key,size_bytes,source_folder_id) VALUES(?,?,?,?,?,?,?)''',
                    (pid,fid,f'YouTubeID{pid}_{i}.mp4',f'youtubeid{pid}_{i}.mp4',f'key{pid}_{i}',9,folder))
                if not missing:
                    con.execute('''INSERT INTO drive_thumbnails(profile_id,drive_file_id,filename,normalized_filename,
                        size_bytes,source_folder_id) VALUES(?,?,?,?,?,?)''',
                        (pid,fid if local else f'thumb{pid}_{i}',f'YouTubeID{pid}_{i}.jpg',f'youtubeid{pid}_{i}.jpg',9,
                         folder if local else 'drivethumbs'))
                if local:
                    stage=engine.STAGING_ROOT/f'profile_{pid:04d}'/('drive_'+engine.safe_slug(fid,'file'))
                    (stage/'thumbnail').mkdir(parents=True)
                    (stage/f'YouTubeID{pid}_{i}.mp4').write_bytes(b'original!')
                    (stage/'thumbnail'/f'YouTubeID{pid}_{i}.jpg').write_bytes(b'original!')

    def test_all_four_source_combinations_plan_and_queue(self):
        for video in ('songmaker','drive'):
            for meta in ('manual','sheet'):
                with self.subTest(video=video,meta=meta):
                    pid=self.save(category_name=video+meta,video_source_mode=video,metadata_source=meta,
                        metadata_sheet_url_or_id='sheet',metadata_sheet_tab='Music',
                        drive_video_folder='drivevideos',drive_thumbnail_folder='drivethumbs')
                    self.seed(pid,local=video=='songmaker')
                    profile=self.db.get_auto_engine_profile(pid)
                    self.assertEqual(profile['channel_group_id'],self.group)
                    self.assertEqual(profile['schedule_times'],'09:00,14:00')
                    def listing(folder):
                        if video=='songmaker': raise AssertionError('Local workflow contacted Drive')
                        return [{'ID':f'video{pid}_{i}' if folder=='drivevideos' else f'thumb{pid}_{i}',
                                 'Name':f'YouTubeID{pid}_{i}'+('.mp4' if folder=='drivevideos' else '.jpg'),'Size':9} for i in range(2)]
                    with patch.object(engine,'drive_lsjson',side_effect=listing), patch.object(engine,'fetch_sheet_csv_tab',return_value=[{'title':'Sheet Song','category_id':'10'}]) as fetch:
                        plan=engine.planner(self.db,self.store,profile,preview_only=False)
                        self.assertTrue(plan['ready'])
                        self.assertEqual(plan['eligible_channels'],2)
                        result=engine.generate_queue(self.db,self.store,profile,run_key='first')
                        self.assertEqual(result['created'],2)
                        self.assertEqual(fetch.called,meta=='sheet')
                    with self.db.connect() as con:
                        jobs=[dict(con.execute('SELECT * FROM videos WHERE id=?',(vid,)).fetchone()) for vid in result['video_ids']]
                    self.assertEqual({j['channel_db_id'] for j in jobs},set(self.channels[:2]))
                    for j in jobs:
                        snap=self.store.channel_job_meta(j['id'])
                        self.assertEqual(snap['strict_thumbnail'],1)
                        self.assertEqual(snap['metadata_sheet_url'],'')
                        self.assertTrue(j['thumbnail_path'].endswith('.jpg'))
                    self.assertEqual(self.store.group_settings(pid)['metadata_source'],meta)
                    self.assertFalse(self.store.channel_binding(pid))

    def test_missing_original_thumbnail_blocks_queue(self):
        pid=self.save();self.seed(pid,missing=True)
        with self.assertRaisesRegex(ValueError,'thumbnail'):
            engine.generate_queue(self.db,self.store,self.db.get_auto_engine_profile(pid))
        with self.db.connect() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM videos').fetchone()[0],0)

    def test_no_new_individual_or_automatic_conversion(self):
        with self.assertRaisesRegex(ValueError,'channel group'):
            self.save(profile_kind='channel',channel_db_id=str(self.channels[0]))
        pid=self.save()
        with self.assertRaisesRegex(ValueError,'Group profile'):
            engine.save_profile_from_form(self.db,self.store,{**self.form,
                'profile_kind':'channel','channel_db_id':str(self.channels[0])},pid)
        self.assertEqual(self.db.get_auto_engine_profile(pid)['channel_group_id'],self.group)

    def test_invalid_group_and_manual_metadata_do_not_create_profiles(self):
        for values in ({'channel_group_id':''},{'channel_group_id':'999'},
                       {'manual_title':''},{'manual_category_id':'bad'}, {'video_source_mode':'bad'}):
            with self.subTest(values=values),self.assertRaises(ValueError): self.save(**values)
        self.assertEqual(self.db.list_auto_engine_profiles(),[])

    def test_group_settings_survive_reload_and_source_switch(self):
        pid=self.save(); settings=self.store.group_settings(pid)
        engine.save_profile_from_form(self.db,self.store,{**self.form,'video_source_mode':'drive',
            'metadata_source':'sheet','metadata_sheet_url_or_id':'sheet','metadata_sheet_tab':'Music',
            'drive_video_folder':'drivevideos','drive_thumbnail_folder':'drivethumbs'},pid)
        self.assertFalse(self.store.source(pid)['local_songmaker_enabled'])
        self.assertEqual(self.store.group_settings(pid)['metadata_source'],'sheet')
        self.assertEqual(self.store.group_settings(pid)['manual_title'],settings['manual_title'])

    def test_group_form_renders_without_individual_selector(self):
        html=(ROOT/'apps/local-upload-manager/part01/templates/app.html').read_text()
        start=html.index('          <div class="lum-card-head"><div><h2>{{ \'Group · Multiple Channels\'')
        end=html.index('        </section>',start)
        template=Environment().from_string(html[start:end])
        rendered=template.render(engine_instance='song',p=None,ae={'groups':[]})
        self.assertNotIn('name="channel_db_id"',rendered)
        self.assertNotIn('Profile Type',rendered)
        self.assertIn('name="channel_group_id" required',rendered)
        self.assertIn('value="1"',rendered)
        script=(ROOT/'apps/local-upload-manager/part01/static/auto_engine_v6.js').read_text()
        self.assertNotIn("videoSource.value='drive'",script)

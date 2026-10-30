"""Real offline folder handoff: confirmed download -> FFmpeg -> group queue -> verified cleanup."""
import ast
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'apps/song-maker/part01'),str(ROOT/'apps/local-upload-manager/part01')]
try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from PIL import Image
    from songmaker.app import create_app
    from songmaker.store import Store
    from songmaker.folders import Folders
    from songmaker.worker import process
    from lum_engine import auto_engine_v6 as engine, songmaker_local as local
    from desktop_core.modules.database import Database
except ImportError as error:
    if os.getenv('SONG_MAKER_REQUIRE_DEPS')=='1': raise
    raise unittest.SkipTest('Folder media dependencies unavailable') from error

VIDEO='BaW_jenozKc'


class FolderWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=Store(self.root/'maker')
        self.client=TestClient(create_app(self.store),client=('127.0.0.1',50000))
        self.addCleanup(self.client.close)
        self.headers={'X-Auth-User':'alice','X-Song-Maker':'1','X-Audio-Downloader-Handoff':'1'}
        self.folder=self.client.post('/song-maker/api/folders',headers=self.headers,json={'name':'Ronaldo'}).json()
        self.other=self.client.post('/song-maker/api/folders',headers=self.headers,json={'name':'Aina Asif'}).json()
        for key in ('DATA_ROOT','THUMB_ROOT','TRASH_ROOT','STAGING_ROOT','REPORT_ROOT'):
            self.start(patch.object(engine,key,self.root/key))
        self.start(patch.object(local,'STAGING_ROOT',engine.STAGING_ROOT))
        self.start(patch.dict(os.environ,{'LUM_ENGINE_INSTANCE':'song','LUM_SUITE_AUTH':'1'}))
        self.pc=Database(self.root/'pc.sqlite');self.v6=engine.V6Store(self.root/'v6.sqlite')
        with self.pc.connect() as con: con.execute("ALTER TABLE videos ADD COLUMN auth_token_file_path TEXT")
        cid=self.pc.add_or_update_channel(channel_id='UC'+'a'*22,channel_name='Music',google_account_email='',token_file_path='test-token')
        self.group=self.pc.save_channel_group('Music',[cid])
        self.start(patch.object(engine,'V6Store',return_value=self.v6))
        self.start(patch.object(engine,'classify_google_json',return_value=engine.VALID_TOKEN))
        self.start(patch.object(local,'_get',side_effect=self.get))
        self.start(patch.object(local,'transfer',side_effect=self.transfer))
        image=io.BytesIO();Image.new('RGB',(1280,720),(40,70,100)).save(image,format='JPEG');self.image=image.getvalue()
        audio=io.BytesIO()
        with wave.open(audio,'wb') as f:
            f.setnchannels(1);f.setsampwidth(2);f.setframerate(16000);f.writeframes(b'\0\0'*16000)
        self.audio=audio.getvalue()

    def start(self,p):
        result=p.start();self.addCleanup(p.stop);return result

    def get(self,owner,path,target=None,maximum=1048576):
        response=self.client.get(path,headers={'X-Auth-User':owner})
        if response.status_code!=200: raise ValueError('Unavailable for this owner')
        if target is None:return response.json()
        self.assertLessEqual(len(response.content),maximum);target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(response.content);return len(response.content)

    def transfer(self,owner,fid,jid,action,body):
        if action=='consume':
            with self.v6.connect() as con:
                self.assertTrue(con.execute('SELECT 1 FROM upload_records').fetchone(), 'Receipt must precede deletion')
        response=self.client.post(f'/internal/song-folders/{fid}/{jid}/{action}',headers={'X-Auth-User':owner,'X-Song-Maker':'1'},json=body)
        if response.status_code!=200:raise ValueError(response.text)
        return response.json()

    def submit_render(self):
        response=self.client.post('/song-maker/api/jobs',headers=self.headers,
            data={'song_folder_id':self.folder['id'],'resolution':'720p'},
            files={'image':(VIDEO+'.jpg',self.image,'image/jpeg'),'audio':(VIDEO+'.wav',self.audio,'audio/wav')})
        self.assertEqual(response.status_code,200,response.text)
        job=response.json()
        process(self.store,self.store.claim('render'),'render',drive_factory=lambda *a: self.fail('Drive forbidden'))
        ready=self.store.get(job['id']);self.assertEqual(ready['stage'],'Completed',ready.get('error'))
        return ready

    def save_profile(self):
        return engine.save_profile_from_form(self.pc,self.v6,dict(profile_kind='group',category_name='Ronaldo Daily',
            channel_group_id=str(self.group),video_source_mode='songfolder',song_folder_id=self.folder['id'],_song_owner='alice',
            metadata_source='manual',manual_title='Song {video_stem}',manual_category_id='10',
            daily_upload_count='1',upload_scope='per_channel',schedule_times='09:00',work_mode='manual_review',enabled='1'))

    def test_real_render_folder_scan_group_queue_and_dependency_safe_cleanup(self):
        job=self.submit_render();registry=Folders(self.store)
        self.assertEqual(registry.outputs('alice',self.other['id']),[])
        media=registry.outputs('alice',self.folder['id'])[0]
        self.assertEqual(media['thumbnail_sha256'],hashlib.sha256(self.image).hexdigest())
        self.assertAlmostEqual(job['duration'],1,places=2)
        pid=self.save_profile();profile=self.pc.get_auto_engine_profile(pid)
        result=engine.generate_queue(self.pc,self.v6,profile)
        self.assertEqual(result['created'],1)
        vid=result['video_ids'][0];source=self.v6.job_source(vid)
        self.assertEqual(self.v6.group_settings(pid)['song_folder_id'],self.folder['id'])
        self.assertEqual(hashlib.sha256(Path(self.pc.get_video(vid)['thumbnail_path']).read_bytes()).hexdigest(),media['thumbnail_sha256'])
        self.assertEqual(engine.planner(self.pc,self.v6,profile)['jobs'],[])
        registry.reserve('alice',self.folder['id'],job['id'],'song-uploader:profile:999')
        with self.pc.connect() as con:
            con.execute("UPDATE videos SET status='UPLOADED',youtube_video_id=?,youtube_url=?,thumbnail_stage_state='SUCCEEDED' WHERE id=?",('v'*11,'https://www.youtube.com/watch?v='+'v'*11,vid))
        worker=ROOT/'apps/local-upload-manager/part01/lum_engine/auto_engine_v6_worker.py'
        fn=next(n for n in ast.parse(worker.read_text()).body if isinstance(n,ast.FunctionDef) and n.name=='reconcile_completed')
        verified=[False]
        ns={'SHORT_FOLDER_PREFIX':engine.SHORT_FOLDER_PREFIX,'os':os,'__package__':'lum_engine','Database':object,'V6Store':object,'Path':Path,'clean':engine.clean,
            'YouTubeUploader':lambda **kw:object(),'remote_verify':lambda *a:verified[0],
            'emit_channel_sheet_writeback':engine.emit_channel_sheet_writeback,
            'set_channel_job_post_state':engine.set_channel_job_post_state}
        exec(compile(ast.Module(body=[fn],type_ignores=[]),str(worker),'exec'),ns)
        ns['reconcile_completed'](self.pc,self.v6,vid)
        self.assertTrue((self.store.root/'outputs'/job['id']).is_dir())
        with self.v6.connect() as con:self.assertEqual(con.execute('SELECT COUNT(*) FROM upload_records').fetchone()[0],0)
        verified[0]=True;ns['reconcile_completed'](self.pc,self.v6,vid)
        self.assertEqual(self.v6.job_source(vid)['state'],'COMPLETE',self.v6.job_source(vid).get('last_error'))
        self.assertTrue((self.store.root/'outputs'/job['id']).is_dir(),'Pending dependent must retain original')
        self.assertFalse(registry.outputs('alice',self.folder['id']),'Consumed media must not enter new queues')
        done=registry.finish('alice',self.folder['id'],job['id'],'song-uploader:profile:999','w'*11)
        self.assertTrue(done['cleaned'])
        self.assertFalse((self.store.root/'outputs'/job['id']).exists())
        self.assertTrue(registry.finish('alice',self.folder['id'],job['id'],'song-uploader:profile:999','w'*11)['cleaned'])

    def test_failed_folder_render_keeps_retry_inputs_and_publishes_nothing(self):
        response=self.client.post('/song-maker/api/jobs',headers=self.headers,data={'song_folder_id':self.folder['id']},
            files={'image':(VIDEO+'.jpg',self.image,'image/jpeg'),'audio':(VIDEO+'.wav',self.audio,'audio/wav')})
        self.assertEqual(response.status_code,200,response.text);job=response.json()
        with patch('songmaker.worker.render',side_effect=RuntimeError('synthetic render failure')):
            process(self.store,self.store.claim('render'),'render')
        self.assertEqual(self.store.get(job['id'])['stage'],'Failed')
        self.assertTrue((self.store.root/'inputs'/job['id']/'image.jpg').is_file())
        self.assertTrue((self.store.root/'inputs'/job['id']/'audio.wav').is_file())
        self.assertEqual(Folders(self.store).outputs('alice',self.folder['id']),[])

    def test_folder_security_owner_identity_and_original_preservation(self):
        for name in ('../outside','a/b','a\\b','..','bad\x00name'):
            self.assertEqual(self.client.post('/song-maker/api/folders',headers=self.headers,json={'name':name}).status_code,422)
        self.assertEqual(self.client.get('/song-maker/api/folders',headers={'X-Auth-User':'bob'}).json()['folders'],[])
        self.assertEqual(self.client.get('/song-maker/api/folders/'+self.folder['id'],headers={'X-Auth-User':'bob'}).status_code,404)
        with patch.object(local,'_get',side_effect=self.get),self.assertRaises(ValueError):
            local.folder_selection('bob',self.folder['id'])
        registry=Folders(self.store);path=registry.path('alice',self.folder['id'])
        path.rmdir();path.symlink_to(self.root,target_is_directory=True)
        with self.assertRaises(ValueError):registry.get('alice',self.folder['id'])

    def test_download_confirmation_preferences_and_immutable_folder_snapshot(self):
        spec=importlib.util.spec_from_file_location('folder_audio_test',ROOT/'apps/voice-studio/part01/audio_downloader_api.py')
        module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;self.addCleanup(sys.modules.pop,spec.name,None)
        with patch.dict(os.environ,{'AUDIO_DOWNLOADER_ROOT':str(self.root/'audio')}):spec.loader.exec_module(module)
        module.BatchRequest.model_rebuild(_types_namespace=vars(module));module.AudioPreferences.model_rebuild(_types_namespace=vars(module))
        self.start(patch.object(module,'deps',return_value={'ready':True,'upload_ready':False}))
        self.start(patch.object(module,'_song_folder',side_effect=lambda owner,fid:self.get(owner,'/song-maker/api/folders/'+fid)))
        app=FastAPI();app.include_router(module.router)
        with TestClient(app) as client:
            data=dict(links=['https://youtu.be/'+VIDEO],format='wav',permission_confirmed=True,download_thumbnail=True,
                      auto_upload=False,auto_song_maker=True,song_folder_id=self.folder['id'])
            self.assertEqual(client.post('/tts/api/audio-downloader/jobs',headers=self.headers,json=data).status_code,422)
            data['folder_confirmed']=True
            response=client.post('/tts/api/audio-downloader/jobs',headers=self.headers,json=data)
            self.assertEqual(response.status_code,201,response.text)
            jid=response.json()['job_ids'][0]
            self.assertEqual(client.get('/tts/api/audio-downloader/jobs/'+jid,headers={'X-Auth-User':'bob'}).status_code,404)
            self.assertEqual(client.get('/tts/api/audio-downloader/jobs',headers={'X-Auth-User':'bob'}).json()['jobs'],[])
            prefs={k:v for k,v in data.items() if k not in ('links','permission_confirmed','folder_confirmed')}
            self.assertEqual(client.put('/tts/api/audio-downloader/preferences',headers=self.headers,json=prefs).status_code,200)
            prefs['song_folder_id']=self.other['id']
            client.put('/tts/api/audio-downloader/preferences',headers=self.headers,json=prefs)
            self.assertEqual(client.get('/tts/api/audio-downloader/preferences',headers=self.headers).json()['preferences']['song_folder_id'],self.other['id'])
            with module.db() as con:row=con.execute('SELECT * FROM jobs WHERE job_id=?',(jid,)).fetchone()
            self.assertEqual(row['song_folder_id'],self.folder['id'])
            self.assertEqual(row['song_folder_name'],'Ronaldo')
            local_dir=module.JOBS/jid;local_dir.mkdir(parents=True)
            wav=local_dir/(VIDEO+'.wav');jpg=local_dir/(VIDEO+'.jpg')
            wav.write_bytes(self.audio);jpg.write_bytes(self.image)
            module._update(jid,'completed',local_file=str(wav),local_thumbnail=str(jpg))
            with module.db() as con:row=con.execute('SELECT * FROM jobs WHERE job_id=?',(jid,)).fetchone()
            maker=self.client
            class Connection:
                def __init__(self,*args,**kw): self.headers={};self.body=bytearray()
                def putrequest(self,method,path):self.path=path
                def putheader(self,key,value):self.headers[key]=value
                def endheaders(self):pass
                def send(self,data):self.body.extend(data)
                def getresponse(self):
                    response=maker.post(self.path,headers=self.headers,content=bytes(self.body))
                    class Response:
                        status=response.status_code
                        def read(self,size):return response.content
                    return Response()
                def close(self):pass
            self.store.record('exports','alice',{'resolution':'720p','quality':'balanced'})
            with patch.object(module.http.client,'HTTPConnection',Connection):
                song_id=module._post_local_pair_to_song_maker(row)
            self.assertEqual(self.store.get(song_id)['song_folder_id'],self.folder['id'])
            process(self.store,self.store.claim('render'),'render',drive_factory=lambda *a:self.fail('Drive forbidden'))
            self.assertEqual(self.store.get(song_id)['stage'],'Completed')
            self.assertEqual(len(Folders(self.store).outputs('alice',self.folder['id'])),1)
            self.assertEqual(Folders(self.store).outputs('alice',self.other['id']),[])


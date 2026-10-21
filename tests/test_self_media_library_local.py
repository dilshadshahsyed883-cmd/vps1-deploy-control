"""Real local intake, consecutive review, safe cleanup and editor compatibility."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
APP=Path(__file__).resolve().parents[1]/'apps/self-media-library/part01'
sys.path.insert(0,str(APP))

@unittest.skipUnless(importlib.util.find_spec('fastapi') and shutil.which('ffmpeg'), 'FFmpeg/FastAPI required')
class LocalLibrary(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.before={k:os.environ.get(k) for k in ('SML_DATA_ROOT','SML_AI_WORKER','SML_EDITOR_API_TOKEN')}
        os.environ.update(SML_DATA_ROOT=str(self.root),SML_AI_WORKER='0',SML_EDITOR_API_TOKEN='local-test')
        sys.modules.pop('app',None)
        import app
        from fastapi.testclient import TestClient
        self.m=app;self.c=TestClient(app.app);self.c.__enter__();self.h={'X-SML-Editor-Token':'local-test'}
        self.c.patch('/api/settings',json={'values':{'quality_filter':'false','ai_auto':'false','duplicate_detection':'false','drive_enabled':'true'}})
    def tearDown(self):
        self.c.__exit__(None,None,None);sys.modules.pop('app',None)
        for k,v in self.before.items():
            if v is None:os.environ.pop(k,None)
            else:os.environ[k]=v
        self.temp.cleanup()
    def source(self):
        sample=self.root/'sample.mp4'
        subprocess.run(['ffmpeg','-y','-v','error','-f','lavfi','-i','testsrc2=size=96x64:rate=10:duration=10','-c:v','libx264','-threads','1',str(sample)],check=True,timeout=30)
        up=self.c.post('/api/sources/upload',files={'file':('forest_walk.mp4',sample.read_bytes(),'video/mp4')})
        self.assertEqual(up.status_code,200,up.text);ident=up.json()['id']
        with patch.object(self.m,'scene_boundaries',return_value=[]):self.m.process_source(ident)
        review=self.c.get('/api/local/reviews/'+ident).json()
        self.assertEqual(len(review['pairs']),2,review)
        self.assertEqual([len(p['items']) for p in review['pairs']],[2,1])
        return ident,review['pairs']
    def choose(self,ident,pair,keep):return self.c.put(f'/api/local/reviews/{ident}/pairs/{pair}',json={'keep_id':keep})
    def original(self,ident):
        with self.m.db() as conn:row=conn.execute('SELECT stored_name FROM sources WHERE id=?',(ident,)).fetchone()
        return self.m.SOURCES_DIR/row['stored_name']
    def image(self,name='lake.png',color='red'):
        from PIL import Image
        import io
        f=io.BytesIO();Image.new('RGB',(64,48),color).save(f,format='PNG')
        r=self.c.post('/api/v1/images/upload',files={'file':(name,f.getvalue(),'image/png')});self.assertEqual(r.status_code,200,r.text)
        ident=r.json()['id'];self.c.patch('/api/v1/images/'+ident,json={'status':'approved','keywords':['lake']});return ident
    def test_pair_odd_clip_cleanup_and_editor_content(self):
        ident,pairs=self.source();keep=pairs[0]['items'][1]['id'];reject=pairs[0]['items'][0]['id']
        self.assertTrue(self.choose(ident,0,keep).json()['cleanup_pending']);self.assertTrue(self.original(ident).exists())
        self.assertTrue((self.m.CLIPS_DIR/(reject+'.mp4')).exists())
        self.assertEqual(self.c.post('/api/clips/bulk/status',json={'ids':[keep],'status':'approved'}).status_code,409)
        self.assertEqual(self.c.delete('/api/clips/'+keep).status_code,409)
        self.assertEqual(self.choose(ident,1,None).json()['state'],'cleaned')
        self.assertFalse(self.original(ident).exists());self.assertFalse((self.m.CLIPS_DIR/(reject+'.mp4')).exists());self.assertTrue((self.m.CLIPS_DIR/(keep+'.mp4')).exists())
        self.assertEqual(self.c.get('/api/v1/media/video/'+keep+'/content',headers=self.h).status_code,200)
        self.assertEqual(self.c.get('/api/v1/media/video/'+keep+'/content').status_code,401)
        self.assertEqual(self.c.get('/api/clips?q=forest_walk').json()['total'],1)
        self.assertEqual(self.c.post('/api/local/reviews/'+ident+'/finalize').status_code,200)
        lease=self.c.post('/api/v1/media/reservations',headers=self.h,json={'project_id':'render','scene_id':'one','preferred_ids':[keep]}).json()['items'][0]['reservation_id']
        for route in ('/api/clips/'+keep,'/api/clips/'+keep+'/permanent'):self.assertEqual(self.c.delete(route).status_code,409)
        self.c.post('/api/v1/media/reservations/'+lease+'/commit',headers=self.h)
        self.assertEqual(self.c.post('/api/local/delete',json={'media_type':'video','ids':[keep]}).status_code,409)
    def test_incomplete_failure_corruption_and_active_job_retain_source(self):
        ident,p=self.source();keep=p[0]['items'][0]['id'];self.choose(ident,0,keep)
        with self.m.db() as conn:conn.execute("UPDATE sources SET status='failed' WHERE id=?",(ident,))
        self.assertTrue(self.choose(ident,1,None).json()['cleanup_pending']);self.assertTrue(self.original(ident).exists())
        self.assertEqual(self.c.post('/api/sources/'+ident+'/manual-cleanup').status_code,409)
        with self.m.db() as conn:conn.execute("UPDATE sources SET status='completed' WHERE id=?",(ident,))
        path=self.m.CLIPS_DIR/(keep+'.mp4');content=path.read_bytes();path.write_bytes(b'corrupt')
        self.assertEqual(self.c.post('/api/local/reviews/'+ident+'/finalize').status_code,409);self.assertTrue(self.original(ident).exists());path.write_bytes(content)
        with self.m.db() as conn:conn.execute("INSERT INTO media_reservations VALUES('busy','p','s','video',?,?,'reserved',?,?,NULL)",(keep,ident,int(time.time()),int(time.time())+3600))
        self.assertEqual(self.c.post('/api/local/reviews/'+ident+'/finalize').status_code,409)
        self.c.post('/api/v1/media/reservations/busy/release',headers=self.h)
        self.assertEqual(self.c.post('/api/local/reviews/'+ident+'/finalize').status_code,200)
    def test_active_ai_job_defers_cleanup_and_retries(self):
        ident,p=self.source();keep=p[0]['items'][0]['id'];self.choose(ident,0,keep)
        from ai_workspace import enqueue
        job=enqueue(self.root,'analyze','video',keep)
        self.assertTrue(self.choose(ident,1,None).json()['cleanup_pending']);self.assertTrue(self.original(ident).exists())
        self.c.post('/api/v1/ai/jobs/'+job+'/cancel')
        self.m.local_housekeeping();self.assertFalse(self.original(ident).exists())
    def test_replacement_requires_verified_new_assets_and_protects_references(self):
        old=self.image();r=self.c.post('/api/local/replacements').json()['id']
        self.assertEqual(self.c.post('/api/local/replacements/'+r+'/commit').status_code,409)
        new=self.image('new.png','blue')
        lease=self.c.post('/api/v1/media/reservations',headers=self.h,json={'project_id':'p','scene_id':'s','media_type':'image','preferred_ids':[old]}).json()['items'][0]['reservation_id']
        self.assertEqual(self.c.post('/api/local/replacements/'+r+'/commit').status_code,409)
        self.assertEqual(self.c.delete('/api/v1/images/'+old).status_code,409)
        self.c.post('/api/v1/media/reservations/'+lease+'/release',headers=self.h)
        self.assertEqual(self.c.post('/api/local/replacements/'+r+'/commit').status_code,200)
        self.assertEqual(self.c.get('/api/v1/images/'+old+'/content').status_code,404)
        self.assertEqual(self.c.get('/api/v1/images/'+new+'/content').status_code,200)
        self.assertEqual(self.c.get('/api/local/storage').json()['images'],1)
        self.assertEqual(self.c.get('/api/local/catalog?media_type=image&q=new').json()['total'],1)
        self.assertEqual(self.c.get('/api/v1/images?date_from=2999-01-01').json()['total'],0)
    def test_decisions_survive_restart_and_invalid_pair_is_rejected(self):
        ident,p=self.source();keep=p[0]['items'][0]['id']
        self.assertEqual(self.choose(ident,0,None).status_code,400)
        self.assertEqual(self.choose(ident,0,p[1]['items'][0]['id']).status_code,400)
        self.choose(ident,0,keep)
        self.m.startup()
        saved=self.c.get('/api/local/reviews/'+ident).json()['pairs'][0]
        self.assertTrue(saved['decided']);self.assertEqual(saved['keep_id'],keep)
        self.assertTrue(self.original(ident).exists())
        self.assertEqual(self.choose(ident,1,None).json()['state'],'cleaned')
    def test_partial_ffmpeg_failure_protects_original_and_partial_clips(self):
        sample=self.root/'partial.mp4'
        subprocess.run(['ffmpeg','-y','-v','error','-f','lavfi','-i','testsrc2=size=96x64:rate=10:duration=10','-c:v','libx264','-threads','1',str(sample)],check=True,timeout=30)
        up=self.c.post('/api/sources/upload',files={'file':('partial.mp4',sample.read_bytes(),'video/mp4')});ident=up.json()['id']
        run=self.m.run_ffmpeg_clip
        def partial(src,out,start,duration):
            if start==4:raise RuntimeError('simulated FFmpeg failure')
            return run(src,out,start,duration)
        with patch.object(self.m,'scene_boundaries',return_value=[]),patch.object(self.m,'run_ffmpeg_clip',side_effect=partial):self.m.process_source(ident)
        source=next(s for s in self.c.get('/api/sources').json()['items'] if s['id']==ident)
        self.assertEqual(source['status'],'failed');self.assertTrue(self.original(ident).exists())
        clips=self.c.get('/api/clips?source_id='+ident).json()['items']
        self.assertTrue(clips)
        for clip in clips:self.assertEqual(self.c.delete('/api/clips/'+clip['id']+'/permanent').status_code,409)
        self.assertEqual(self.c.post('/api/local/reviews/'+ident+'/finalize').status_code,404)

    def test_drive_removed_not_reenabled_and_legacy_receipts_retained(self):
        self.assertFalse(self.c.get('/api/health').json()['drive_enabled'])
        self.assertEqual(self.c.get('/api/settings').json()['values']['drive_enabled'],'false')
        self.assertEqual(self.c.post('/api/drive/test').status_code,404)
        self.assertEqual(self.c.post('/api/v1/media/imports',json={'source':'drive','value':'file.mp4'}).status_code,400)
        with self.m.db() as conn:self.assertIn('drive_clip_path',[r['name'] for r in conn.execute('PRAGMA table_info(clips)')])

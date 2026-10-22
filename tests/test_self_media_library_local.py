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
    def test_owner_deletes_media_without_replacement_and_references_are_protected(self):
        old=self.image()
        self.assertEqual(self.c.post('/api/local/replacements').status_code,404)
        lease=self.c.post('/api/v1/media/reservations',headers=self.h,json={'project_id':'p','scene_id':'s','media_type':'image','preferred_ids':[old]}).json()['items'][0]['reservation_id']
        body={'media_type':'image','ids':[old]}
        self.assertEqual(self.c.post('/api/local/delete',json=body).status_code,409)
        self.c.post('/api/v1/media/reservations/'+lease+'/release',headers=self.h)
        self.assertEqual(self.c.post('/api/local/delete',json=body).status_code,200)
        self.assertEqual(self.c.get('/api/v1/images/'+old+'/content').status_code,404)
        self.assertEqual(self.c.get('/api/local/storage').json()['images'],0)

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

    def test_legacy_clips_bulk_trash_restore_and_purge_with_unrelated_ai_job(self):
        source_id, pairs = self.source()
        keep = pairs[0]['items'][0]['id']
        self.choose(source_id, 0, keep)
        self.assertEqual(self.choose(source_id, 1, None).status_code, 200)
        path = self.m.CLIPS_DIR / (keep + '.mp4')
        self.assertTrue(path.exists())
        with self.m.db() as conn:
            conn.execute("UPDATE clips SET local_selection='legacy' WHERE id=?", (keep,))
            conn.execute("""INSERT INTO ai_jobs(id,kind,media_type,media_id,payload,state,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?)""", ('unrelated','analyze','video','other-clip','{}','queued',time.time(),time.time()))
        # An unrelated stuck/queued AI job must not block deleting older clips.
        gone = self.c.post('/api/clips/bulk/trash', json={'ids':[keep,keep]})
        self.assertEqual(gone.status_code, 200, gone.text)
        self.assertEqual(gone.json()['trashed'], 1)
        self.assertNotIn(keep, [x['id'] for x in self.c.get('/api/clips').json()['items']])
        self.assertEqual(self.c.post('/api/clips/'+keep+'/restore').status_code, 200)
        self.assertEqual(self.c.post('/api/clips/bulk/trash', json={'ids':[keep]}).status_code, 200)
        self.assertEqual(self.c.delete('/api/clips/'+keep+'/permanent').status_code, 200)
        self.assertFalse(path.exists())
        self.assertNotIn(keep, [x['id'] for x in self.c.get('/api/clips?trash=true').json()['items']])
        with self.m.db() as conn:
            self.assertIsNone(conn.execute('SELECT id FROM clips WHERE id=?',(keep,)).fetchone())
            self.assertIsNotNone(conn.execute('SELECT id FROM sources WHERE id=?',(source_id,)).fetchone())
            self.assertEqual(conn.execute("SELECT state FROM ai_jobs WHERE id='unrelated'").fetchone()[0], 'queued')

    def test_legacy_failed_source_owner_can_delete_but_pending_source_is_protected(self):
        source_id, pairs = self.source()
        keep = pairs[0]['items'][0]['id']
        pending = pairs[0]['items'][1]['id']
        self.assertEqual(self.c.post('/api/clips/bulk/trash', json={'ids':[keep,pending]}).status_code, 409)
        with self.m.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM clips WHERE source_id=? AND deleted_at IS NOT NULL',(source_id,)).fetchone()[0], 0)
        self.choose(source_id,0,keep)
        self.choose(source_id,1,None)
        with self.m.db() as conn:
            conn.execute("UPDATE clips SET local_selection='legacy' WHERE id=?",(keep,))
            conn.execute("UPDATE sources SET status='failed' WHERE id=?",(source_id,))
        gone = self.c.post('/api/local/delete', json={'media_type':'video','ids':[keep]})
        self.assertEqual(gone.status_code,200,gone.text)
        self.assertFalse((self.m.CLIPS_DIR/(keep+'.mp4')).exists())
        with self.m.db() as conn:
            self.assertIsNone(conn.execute('SELECT id FROM clips WHERE id=?',(keep,)).fetchone())

    def test_clip_permanent_delete_actions_visible_in_video_and_trash_sections(self):
        html = (APP/'static/index.html').read_text()
        js = (APP/'static/app.js').read_text()
        library = html.split('<h2>Video Clips</h2>')[0].split('id="bulkBar"')[-1]
        self.assertIn('id="deleteSelectedClips"', library)
        images = html.split('<h2>Images</h2>')[-1].split('id="imagesGrid"')[0]
        self.assertNotIn('id="deleteSelectedClips"', images)
        self.assertIn('data-restore-clip', js)
        self.assertIn('data-purge-clip', js)
        self.assertIn("api('/api/clips/bulk/trash'", js)

    def test_old_image_thumb_is_rebuilt_from_original_without_upsampling(self):
        from PIL import Image
        import hashlib
        ident = self.image('legacy_sharpness.png')
        with self.m.db() as conn:
            row = conn.execute('SELECT stored_name,thumb_name FROM image_assets WHERE id=?', (ident,)).fetchone()
            source = self.root/'images'/row['stored_name']
            original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            highres = self.root/'image_previews'/(ident+'-sharp-v2.jpg')
            legacy = self.root/'image_previews'/(ident+'.jpg')
            highres.unlink(missing_ok=True)
            Image.new('RGB', (12, 8), 'blue').save(legacy, 'JPEG')
            conn.execute('UPDATE image_assets SET thumb_name=? WHERE id=?', (legacy.name, ident))
        res = self.c.get('/api/v1/images/'+ident+'/preview')
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.headers['content-type'], 'image/jpeg')
        with Image.open(highres) as preview:
            self.assertEqual(preview.size, (64, 48))  # Never upsample the 64x48 original.
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original_hash)
        self.assertTrue(legacy.exists())  # Legacy evidence remains until owner delete.
        first_stat = highres.stat().st_mtime_ns
        self.assertEqual(self.c.get('/api/v1/images/'+ident+'/preview').status_code, 200)
        self.assertEqual(highres.stat().st_mtime_ns, first_stat)
        catalog = self.c.get('/api/v1/images').json()['items'][0]
        self.assertIn('preview?v=sharp-v2', catalog['thumb_url'])
        self.assertEqual(self.c.post('/api/local/delete', json={'media_type':'image','ids':[ident]}).status_code, 200)
        self.assertFalse(source.exists())
        self.assertFalse(legacy.exists())
        self.assertFalse(highres.exists())

    def test_image_cards_contain_long_names_and_show_uncropped_sharp_preview(self):
        html = (APP/'static/index.html').read_text()
        js = (APP/'static/app.js').read_text()
        css = (APP/'static/sml_shared_theme.css').read_text()
        self.assertIn('image-sharp-v2', html)
        for token in ('image-tile','image-preview-frame','image-card-name','image-card-meta','decoding="async"'):
            self.assertIn(token, js)
        for token in ('#imagesGrid .image-tile','#imagesGrid .image-card img','object-fit:contain!important',
                      'text-overflow:ellipsis','min-width:0','grid-template-columns:repeat(2,minmax(0,1fr))!important'):
            self.assertIn(token, css)

    def test_drive_removed_not_reenabled_and_legacy_receipts_retained(self):
        self.assertFalse(self.c.get('/api/health').json()['drive_enabled'])
        self.assertEqual(self.c.get('/api/settings').json()['values']['drive_enabled'],'false')
        self.assertEqual(self.c.post('/api/drive/test').status_code,404)
        self.assertEqual(self.c.post('/api/v1/media/imports',json={'source':'drive','value':'file.mp4'}).status_code,400)
        with self.m.db() as conn:self.assertIn('drive_clip_path',[r['name'] for r in conn.execute('PRAGMA table_info(clips)')])

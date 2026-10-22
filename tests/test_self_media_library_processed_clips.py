"""Processed clip direct-save regression: no cutting, no review, editor ready."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

APP=Path(__file__).resolve().parents[1]/'apps/self-media-library/part01'
sys.path.insert(0,str(APP))


@unittest.skipUnless(importlib.util.find_spec('fastapi') and shutil.which('ffmpeg'), 'FastAPI/FFmpeg required')
class ProcessedClipImport(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.env={x:os.environ.get(x) for x in ('SML_DATA_ROOT','SML_AI_WORKER','SML_EDITOR_API_TOKEN')}
        os.environ.update(SML_DATA_ROOT=str(self.root),SML_AI_WORKER='0',SML_EDITOR_API_TOKEN='processed-test')
        sys.modules.pop('app',None)
        import app
        from fastapi.testclient import TestClient
        self.module=app
        self.client=TestClient(app.app)
        self.client.__enter__()
        self.editor={'X-SML-Editor-Token':'processed-test'}

    def tearDown(self):
        self.client.__exit__(None,None,None)
        sys.modules.pop('app',None)
        for key,value in self.env.items():
            if value is None:os.environ.pop(key,None)
            else:os.environ[key]=value
        self.tmp.cleanup()

    def sample(self,ext='.mp4'):
        path=self.root/('ready'+ext)
        subprocess.run(['ffmpeg','-y','-v','error','-f','lavfi','-i',
                        'testsrc2=size=96x64:rate=10:duration=2',
                        '-c:v','libx264','-threads','1',str(path)],check=True,timeout=30)
        return path.read_bytes()

    def upload(self,data,filename='already_edited.mp4'):
        return self.client.post('/api/clips/processed/upload',
                                files={'file':(filename,data,'video/mp4')})

    def test_directly_approved_searchable_previewable_and_not_reprocessed(self):
        video=self.sample()
        first=self.upload(video)
        self.assertEqual(first.status_code,200,first.text)
        payload=first.json()
        self.assertEqual(payload['status'],'approved')
        self.assertFalse(payload['duplicate'])
        cid=payload['id']
        with self.module.db() as conn:
            clip=conn.execute('SELECT * FROM clips WHERE id=?',(cid,)).fetchone()
            source=conn.execute('SELECT * FROM sources WHERE id=?',(clip['source_id'],)).fetchone()
            self.assertEqual(clip['status'],'approved')
            self.assertEqual(clip['local_selection'],'kept')
            self.assertEqual(clip['ai_state'],'pending')
            self.assertEqual(source['source_kind'],'processed_clip')
            self.assertEqual(source['stage'],'ready_clip_saved')
            self.assertEqual(source['clip_count'],1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM local_reviews').fetchone()[0],0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM ai_jobs').fetchone()[0],0)
            self.assertFalse((self.root/'sources'/source['stored_name']).exists())
        self.assertEqual((self.root/'clips'/clip['filename']).read_bytes(),video)
        self.assertTrue((self.root/'thumbs'/clip['thumb_filename']).stat().st_size>0)
        catalog=self.client.get('/api/clips?status=approved&orientation=landscape').json()
        self.assertEqual(catalog['total'],1)
        self.assertEqual(catalog['items'][0]['source_filename'],'already_edited.mp4')
        editor=self.client.get('/api/v1/media/search?media_type=video',headers=self.editor)
        self.assertEqual(editor.status_code,200,editor.text)
        self.assertEqual([x['id'] for x in editor.json()['items']],[cid])
        self.assertEqual(self.client.get('/api/v1/media/video/'+cid+'/content',
                                         headers=self.editor).content,video)
        reserved=self.client.post('/api/v1/media/reservations',headers=self.editor,json={
            'project_id':'ready-job','scene_id':'scene-01','media_type':'video',
            'count':1,'query':'','preferred_ids':[cid]})
        self.assertEqual(reserved.status_code,200,reserved.text)
        self.assertEqual(reserved.json()['count'],1)
        again=self.upload(video)
        self.assertEqual(again.status_code,200,again.text)
        self.assertTrue(again.json()['duplicate'])
        self.assertEqual(again.json()['id'],cid)
        self.assertEqual(self.client.get('/api/stats').json()['clips'],1)

    def test_bad_files_leave_no_catalog_or_final_assets(self):
        self.assertEqual(self.upload(b'x','not.txt').status_code,400)
        self.assertEqual(self.upload(b'').status_code,400)
        self.assertEqual(self.upload(b'not video').status_code,422)
        self.assertEqual(self.client.get('/api/stats').json()['clips'],0)
        self.assertEqual(list((self.root/'clips').iterdir()),[])
        self.assertEqual(list((self.root/'thumbs').iterdir()),[])

    def test_web_input_is_separate_from_normal_sources(self):
        html=(APP/'static/index.html').read_text()
        js=(APP/'static/app.js').read_text()
        self.assertIn('data-sml-upload="processed"',html)
        self.assertIn('id="processedClipFiles"',html)
        self.assertIn('Automatically Approved',html)
        self.assertIn("api('/api/clips/processed/upload'",js)
        self.assertIn("api('/api/sources/upload'",js)


if __name__=='__main__':
    unittest.main()

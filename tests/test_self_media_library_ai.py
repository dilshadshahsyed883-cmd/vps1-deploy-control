"""Actual CPU visual inference + durable API tests. Real inference is opt-in in CI."""
from pathlib import Path
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
APP=Path(__file__).resolve().parents[1]/'apps/self-media-library/part01'
sys.path.insert(0,str(APP))

@unittest.skipUnless(importlib.util.find_spec('fastapi'),'FastAPI required')
class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.before={k:os.environ.get(k) for k in ('SML_DATA_ROOT','SML_AI_WORKER','SML_EDITOR_API_TOKEN')}
        os.environ.update(SML_DATA_ROOT=str(self.root),SML_AI_WORKER='0',SML_EDITOR_API_TOKEN='test-token')
        sys.modules.pop('app',None)
        import app
        from fastapi.testclient import TestClient
        self.module=app;self.client=TestClient(app.app);self.client.__enter__()
    def tearDown(self):
        self.client.__exit__(None,None,None);sys.modules.pop('app',None)
        for k,v in self.before.items():
            if v is None:os.environ.pop(k,None)
            else:os.environ[k]=v
        self.temp.cleanup()
    def image(self,name='sample.png',color='blue'):
        from PIL import Image
        import io
        b=io.BytesIO();Image.new('RGB',(64,48),color).save(b,format='PNG')
        r=self.client.post('/api/v1/images/upload',files={'file':(name,b.getvalue(),'image/png')})
        self.assertEqual(r.status_code,200,r.text);return r.json()['id']
    def test_unavailable_is_truthful_and_manual_metadata_survives(self):
        c=self.client;i=self.image('forest_car_running.png')
        self.assertFalse(c.get('/api/v1/ai/status').json()['ready'])
        self.assertEqual(c.post('/api/v1/ai/analyze',json={'media_type':'image','ids':[i]}).status_code,503)
        self.assertEqual(c.get('/api/v1/ai/search?query=a%20tiger').status_code,503)
        self.assertEqual(self.module.analyze_clip(Path('ignored'),'forest_car.png'),('',[],0.))
        r=c.patch('/api/v1/ai/assets/image/'+i,json={'fields':{'description':'Owner caption','keywords':['my tag']},'expected_revision':0})
        self.assertEqual(r.status_code,200,r.text);rev=r.json()['revision']
        from ai_workspace import connect
        with connect(self.root) as db:
            db.execute("UPDATE ai_metadata SET machine_json=?,state='complete',revision=revision+1 WHERE media_id=?",(json.dumps({'description':'AI caption'}),i))
        detail=c.get('/api/v1/ai/assets/image/'+i).json()
        self.assertEqual(detail['effective']['description'],'Owner caption')
        self.assertEqual(c.patch('/api/v1/ai/assets/image/'+i,json={'fields':{'description':'stale'},'expected_revision':rev}).status_code,409)
        self.assertEqual(c.get('/api/v1/ai/assets/image/'+i+'/history').json()['items'][0]['origin'],'manual')
        self.assertEqual(c.get('/api/v1/media/search').status_code,401)
        self.assertEqual(c.get('/api/v1/ai/search?orientation=landscape&min_width=60').json()['total'],1)
        c.delete('/api/v1/images/'+i)
        self.assertEqual(c.get('/api/v1/ai/search').json()['total'],0)
    def test_atomic_jobs_restart_cancel_and_retry(self):
        i=self.image();from ai_workspace import enqueue,connect,initialize
        first=enqueue(self.root,'analyze','image',i);second=enqueue(self.root,'analyze','image',i)
        self.assertEqual(first,second)
        initialize(self.root)
        self.assertEqual(self.client.get('/api/v1/ai/jobs').json()['items'][0]['state'],'queued')
        self.assertEqual(self.client.post('/api/v1/ai/jobs/'+first+'/cancel').status_code,200)
        self.assertEqual(self.client.post('/api/v1/ai/jobs/'+first+'/retry').status_code,200)
        self.assertEqual(self.client.patch('/api/v1/ai/settings',json={'enabled':True,'batch_limit':1000}).status_code,422)
    def test_scene_no_repeat_idempotency_expiry_and_manual_image_edits(self):
        c=self.client;i=self.image();h={'X-SML-Editor-Token':'test-token'}
        c.patch('/api/v1/images/'+i,json={'status':'approved','keywords':['sky'],'description':'Owner sky'})
        self.assertEqual(c.get('/api/v1/ai/assets/image/'+i).json()['manual']['description'],'Owner sky')
        body={'project_id':'project','scene_id':'scene1','media_type':'image','query':'sky'}
        r=c.post('/api/v1/media/reservations',headers=h,json=body);lease=r.json()['items'][0]['reservation_id']
        self.assertEqual(c.post('/api/v1/media/reservations',headers=h,json=body).json()['items'][0]['reservation_id'],lease)
        self.assertEqual(c.post('/api/v1/media/reservations/'+lease+'/commit',headers=h).status_code,200)
        self.assertTrue(c.post('/api/v1/media/reservations/'+lease+'/commit',headers=h).json()['idempotent'])
        body['scene_id']='scene2'
        self.assertEqual(c.post('/api/v1/media/reservations',headers=h,json=body).json()['count'],0)
    def test_source_cleanup_requires_full_original_and_no_active_dependents(self):
        m=self.module;ts=m.now_iso()
        self.assertEqual(m.drive_path({'drive_remote':'gdrive','drive_root':'library'},'sources','source.mp4'),'gdrive:library/sources/source.mp4')
        with m.db() as c:
            c.execute("INSERT INTO sources(id,filename,stored_name,created_at,updated_at) VALUES('source','original.mp4','source.mp4',?,?)",(ts,ts))
            c.execute("INSERT INTO clips(id,source_id,filename,created_at,updated_at,drive_state,drive_clip_path,drive_meta_path,drive_verified_at) VALUES('clip','source','clip.mp4',?,?,'verified','gdrive:clip','gdrive:meta',?)",(ts,ts,ts))
        original=m.SOURCES_DIR/'source.mp4';original.write_bytes(b'original protected')
        self.assertFalse(m.source_drive_gate('source')['all_verified'])
        self.assertFalse(m.cleanup_source_if_drive_safe('source')['cleaned']);self.assertTrue(original.is_file())
        with m.db() as c:c.execute("UPDATE sources SET drive_source_path='gdrive:original',drive_source_verified_at=? WHERE id='source'",(ts,))
        from ai_workspace import enqueue
        job=enqueue(self.root,'analyze','source','source')
        self.assertFalse(m.cleanup_source_if_drive_safe('source')['cleaned']);self.assertTrue(original.is_file())
        self.client.post('/api/v1/ai/jobs/'+job+'/cancel')
        self.assertTrue(m.source_drive_gate('source')['cleanup_ready'])

    def test_real_adaptive_scene_cut_and_default_segments(self):
        video=self.root/'cut.mp4'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=red:s=64x64:d=3[r];color=c=blue:s=64x64:d=3[b];[r][b]concat=n=2:v=1:a=0','-threads','1',str(video)],check=True,timeout=30)
        cuts=self.module.scene_boundaries(video,.32)
        self.assertTrue(any(abs(t-3)<.4 for t in cuts),cuts)
        segments=self.module.build_segments(6,cuts,2,4)
        self.assertTrue(all(2<=b-a<=4 for a,b in segments),segments)
        with self.assertRaises(ValueError):self.module.build_segments(6,[],0,0)

    def test_durable_imports_and_duration_validation(self):
        c=self.client
        response=c.post('/api/v1/media/imports/batch',json=[{'source':'youtube','value':'https://www.youtube.com/watch?v=fixture','rights_confirmed':False} for _ in range(2)])
        self.assertEqual(response.status_code,200,response.text)
        ids=[x['job_id'] for x in response.json()['items']]
        from ai_workspace import connect
        with connect(self.root) as db:
            rows=db.execute('SELECT request_json FROM media_imports WHERE id IN (?,?)',ids).fetchall()
            self.assertEqual(len(rows),2);self.assertTrue(all(r['request_json'] for r in rows))
            db.execute("UPDATE media_imports SET status='failed' WHERE id=?",(ids[0],))
        self.assertEqual(c.post('/api/v1/media/imports/'+ids[0]+'/retry').status_code,200)
        self.assertEqual(c.patch('/api/settings',json={'values':{'clip_min_seconds':'0','clip_max_seconds':'4'}}).status_code,400)
        self.assertEqual(c.patch('/api/settings',json={'values':{'clip_min_seconds':'2','clip_max_seconds':'4'}}).status_code,200)

    def test_collections_and_quality_are_non_destructive(self):
        i=self.image();c=self.client
        s=c.post('/api/v1/ai/collections',json={'name':'Approved wides','query':'','filters':{'status':'approved','orientation':'landscape'}})
        self.assertEqual(s.status_code,200)
        self.assertEqual(len(c.get('/api/v1/ai/collections').json()['items']),1)
        self.assertFalse(c.get('/api/v1/ai/duplicates').json()['automatic_deletion'])
        self.assertEqual(c.get('/api/v1/images').json()['total'],1)

@unittest.skipUnless(os.getenv('SML_REAL_AI_TEST')=='1','Pinned real model integration opt-in')
class RealInferenceTests(unittest.TestCase):
    def test_real_ocr_and_speech(self):
        import urllib.request
        from PIL import Image,ImageDraw,ImageFont
        from ai_text import recognize_text,SpeechEngine,prepare_speech
        image=Image.new('RGB',(1000,200),'white')
        font=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',52)
        ImageDraw.Draw(image).text((30,60),'MOUNTAIN LIBRARY 2048',fill='black',font=font)
        ocr=recognize_text(image)
        self.assertEqual(ocr['state'],'complete');self.assertIn('MOUNTAIN LIBRARY 2048',ocr['text'])
        folder=Path(os.environ.get('SML_TEST_MODEL_CACHE','/tmp/sml-ai-model')).parent/'sml-speech-model'
        prepare_speech(folder)
        with tempfile.TemporaryDirectory() as temp:
            audio=Path(temp)/'opaque.wav'
            audio.write_bytes(urllib.request.urlopen('https://raw.githubusercontent.com/ggml-org/whisper.cpp/master/samples/jfk.wav',timeout=60).read())
            speech=SpeechEngine(folder).transcribe(audio,11)
            self.assertEqual(speech['state'],'complete',speech)
            self.assertIn('fellow Americans',speech['text'])
        print('PASS_REAL_OCR_AND_SPEECH')

    def test_actual_pixels_semantic_query_and_reference(self):
        import urllib.request
        import numpy as np
        from PIL import Image
        from ai_engine import ClipEngine,prepare_model,VERSION
        folder=Path(os.environ.get('SML_TEST_MODEL_CACHE',str(APP.parent/'.ai-test-model')))
        prepare_model(folder);engine=ClipEngine(folder)
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);vectors=[];reports=[]
            files=['tiger.jpg','moraine-lake.png','city-streets.jpg']
            for idx,name in enumerate(files):
                path=root/f'opaque-{idx}.jpg'
                path.write_bytes(urllib.request.urlopen('https://huggingface.co/datasets/Xenova/transformers.js-docs/resolve/main/'+name,timeout=60).read())
                report=engine.analyze(path,'image');vectors.append(report.pop('embedding'));reports.append(report)
            # Controlled relevance independent of names; no mocked provider data.
            queries=['a striped tiger animal','a peaceful mountain lake landscape','an urban city street with buildings']
            qvectors=engine.embed_text(queries)
            winners=np.argmax(np.asarray(qvectors)@np.asarray(vectors).T,axis=1).tolist()
            self.assertEqual(winners,[0,1,2],{'winners':winners})
            self.assertEqual(reports[0]['fields']['objects'][0]['value'],'tiger')
            self.assertEqual(len(reports[0]['crop_recommendations']),3)
            self.assertTrue(all(c['width']>0 and c['height']>0 and c['uncertain'] for c in reports[0]['crop_recommendations']))
            with Image.open(root/'opaque-0.jpg') as im:ref=engine.embed_image(im)
            self.assertEqual(int(np.argmax(np.asarray(vectors)@ref)),0)
            # Real short video uses independently sampled frames with timestamps.
            video=root/'opaque-video.mp4'
            subprocess.run(['ffmpeg','-v','error','-loop','1','-i',str(root/'opaque-0.jpg'),'-t','2','-vf','scale=320:-2','-pix_fmt','yuv420p','-threads','1',str(video)],check=True,timeout=40)
            vid=engine.analyze(video,'video',2,root/'contact.jpg')
            self.assertEqual(len(vid['frames']),3);self.assertTrue((root/'contact.jpg').is_file())
            self.assertEqual(vid['fields']['objects'][0]['value'],'tiger')
            print(json.dumps({'actual_visual_test':'PASS','model':VERSION,'semantic_retrieval':winners,'image_seconds':[r['elapsed_seconds'] for r in reports],'video_seconds':vid['elapsed_seconds']}))

if __name__=='__main__':unittest.main()

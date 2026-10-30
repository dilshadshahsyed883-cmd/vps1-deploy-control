"""PC single/batch folder delivery uses the existing registry, queue and renderer."""
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'apps/song-maker/part01'))
try:
    from fastapi.testclient import TestClient
    from PIL import Image
    from songmaker.app import create_app
    from songmaker.store import Store
    from songmaker.folders import Folders
    from songmaker.worker import process
except ImportError as error:
    if os.getenv('SONG_MAKER_REQUIRE_DEPS')=='1':raise
    raise unittest.SkipTest('Song Maker media dependencies unavailable') from error


class PCFolderTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name));self.client=TestClient(create_app(self.store));self.addCleanup(self.client.close)
        self.headers={'X-Auth-User':'alice','X-Song-Maker':'1'}
        self.folder=self.client.post('/song-maker/api/folders',headers=self.headers,json={'name':'PC Music'}).json()
        self.other=self.client.post('/song-maker/api/folders',headers=self.headers,json={'name':'Other'}).json()
        image=io.BytesIO();Image.new('RGB',(320,180),(15,45,85)).save(image,format='PNG');self.image=image.getvalue()
        audio=io.BytesIO()
        with wave.open(audio,'wb') as f:
            f.setnchannels(1);f.setsampwidth(2);f.setframerate(16000);f.writeframes(b'\0\0'*16000)
        self.audio=audio.getvalue()
        self.store.record('exports','alice',{'resolution':'720p','fit':'blur','quality':'compact','overlay_enabled':False})

    def upload(self,name='Song',fid=None,headers=None):
        data={} if fid is None else {'song_folder_id':fid}
        return self.client.post('/song-maker/api/jobs',headers=headers or self.headers,data=data,
            files={'image':(name+'.png',self.image,'image/png'),'audio':(name+'.wav',self.audio,'audio/wav')})

    def test_pc_render_enters_selected_shared_folder_and_skips_drive(self):
        response=self.upload(fid=self.folder['id']);self.assertEqual(response.status_code,200,response.text)
        job=response.json()
        self.assertFalse(job['audio_downloader_handoff'])
        self.assertTrue(job['send_to_song_uploader'])
        self.assertEqual((job['resolution'],job['fit'],job['quality']),('720p','blur','compact'))
        self.client.put('/song-maker/api/manual-folder',headers=self.headers,json={'song_folder_id':self.other['id']})
        process(self.store,self.store.claim('render'),'render',drive_factory=lambda *a:self.fail('PC folder delivery must not contact Drive'))
        result=self.store.get(job['id']);self.assertEqual(result['stage'],'Completed',result.get('error'))
        self.assertEqual(result['song_folder_id'],self.folder['id'])
        registry=Folders(Store(self.store.root))
        self.assertEqual([r['id'] for r in registry.outputs('alice',self.folder['id'])],[job['id']])
        self.assertEqual(registry.outputs('alice',self.other['id']),[])
        self.assertEqual(len(self.client.get('/song-maker/api/folders',headers=self.headers).json()['folders']),2)

    def test_selection_persists_per_owner_and_only_explicit_changes_update_it(self):
        endpoint='/song-maker/api/manual-folder'
        self.assertEqual(self.client.get(endpoint,headers=self.headers).json()['song_folder_id'],'')
        self.assertEqual(self.client.put(endpoint,headers=self.headers,json={'song_folder_id':self.folder['id']}).status_code,200)
        self.assertEqual(Store(self.store.root).record('intake','alice')['song_folder_id'],self.folder['id'])
        self.client.get('/song-maker/api/state',headers=self.headers)
        self.client.get('/song-maker/api/folders',headers=self.headers)
        self.assertEqual(self.client.get(endpoint,headers=self.headers).json()['song_folder_id'],self.folder['id'])
        bob={'X-Auth-User':'bob','X-Song-Maker':'1'}
        self.assertEqual(self.client.get(endpoint,headers=bob).json()['song_folder_id'],'')
        self.assertEqual(self.client.put(endpoint,headers=bob,json={'song_folder_id':self.folder['id']}).status_code,422)
        self.assertEqual(self.client.put(endpoint,headers=self.headers,json={'song_folder_id':''}).status_code,200)
        self.assertEqual(self.client.get(endpoint,headers=self.headers).json()['song_folder_id'],'')

    def test_batch_pair_destinations_and_duplicate_safety_use_existing_intake(self):
        jobs=[self.upload(name=name,fid=self.folder['id']).json() for name in ('Song One','Song Two')]
        self.assertEqual(len({j['id'] for j in jobs}),2)
        self.assertTrue(all(j['song_folder_id']==self.folder['id'] for j in jobs))
        self.assertEqual(self.upload(name='Song One',fid=self.folder['id']).json()['id'],jobs[0]['id'])
        self.assertNotEqual(self.upload(name='Song One',fid=self.other['id']).json()['id'],jobs[0]['id'])
        self.assertEqual(self.upload(fid=self.folder['id'],headers={'X-Auth-User':'bob','X-Song-Maker':'1'}).status_code,422)
        self.assertEqual(self.upload(fid='../outside').status_code,422)

    def test_legacy_manual_and_original_downloader_contract_remain_available(self):
        legacy=self.upload().json()
        self.assertEqual(legacy['song_folder_id'],'')
        self.assertFalse(legacy['send_to_song_uploader'])
        self.assertEqual(self.upload(fid=self.folder['id'],headers={**self.headers,'X-Audio-Downloader-Handoff':'1'}).status_code,422)

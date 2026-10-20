"""Real CPU model + durable queue verification in an isolated disposable catalog.

Never accesses production catalog, media or credentials. Approved source samples
come from the Transformers.js documentation dataset and use opaque filenames.
"""
from __future__ import annotations
import argparse
import importlib
import json
import os
from pathlib import Path
import resource
import sys
import tempfile
import time
import urllib.request
ROOT=Path(__file__).resolve().parents[1]
APP=ROOT/'apps/self-media-library/part01'


def verify(model_cache):
    model_cache=Path(model_cache).resolve()
    with tempfile.TemporaryDirectory(prefix='sml-ai-verification-') as temp:
        root=Path(temp);(root/'ai_models').symlink_to(model_cache,target_is_directory=True)
        speech_cache=model_cache.parent/'speech_models'
        if speech_cache.is_dir():(root/'speech_models').symlink_to(speech_cache,target_is_directory=True)
        saved={key:os.environ.get(key) for key in ('SML_DATA_ROOT','SML_AI_WORKER','SML_EDITOR_API_TOKEN')}
        os.environ.update(SML_DATA_ROOT=str(root),SML_AI_WORKER='1',SML_EDITOR_API_TOKEN='test-token')
        sys.path.insert(0,str(APP));sys.modules.pop('app',None)
        try:
            from fastapi.testclient import TestClient
            module=importlib.import_module('app')
            start=time.monotonic()
            with TestClient(module.app) as client:
                ids=[]
                for i,name in enumerate(('tiger.jpg','moraine-lake.png','city-streets.jpg')):
                    data=urllib.request.urlopen('https://huggingface.co/datasets/Xenova/transformers.js-docs/resolve/main/'+name,timeout=60).read()
                    ext=Path(name).suffix
                    result=client.post('/api/v1/images/upload',files={'file':('opaque-'+str(i)+ext,data,'image/png' if ext=='.png' else 'image/jpeg')})
                    assert result.status_code==200,result.text
                    ids.append(result.json()['id'])
                queued=client.post('/api/v1/ai/analyze',json={'media_type':'image','ids':ids})
                assert queued.status_code==200,queued.text
                until=time.monotonic()+120
                while time.monotonic()<until:
                    status=client.get('/api/v1/ai/status').json()
                    if status['counts'].get('complete')==3:break
                    failed=client.get('/api/v1/ai/jobs').json()['items']
                    assert not any(j['state']=='failed' for j in failed),failed
                    time.sleep(.3)
                assert status['counts'].get('complete')==3,status
                winners=[]
                for query in ('a striped tiger animal','a peaceful mountain lake landscape','an urban city street with buildings'):
                    response=client.get('/api/v1/ai/search',params={'query':query})
                    assert response.status_code==200,response.text
                    winners.append(response.json()['items'][0]['id'])
                assert winners==ids,{'expected':ids,'actual':winners}
                detail=client.get('/api/v1/ai/assets/image/'+ids[0]).json()
                assert detail['machine']['fields']['objects'][0]['value']=='tiger',detail
                # Human corrections, model version, metadata history and reindex.
                edit=client.patch('/api/v1/ai/assets/image/'+ids[0],json={'fields':{'description':'Owner correction','rights_confirmed':True},'expected_revision':detail['revision']})
                assert edit.status_code==200,edit.text
                client.post('/api/v1/ai/analyze',json={'media_type':'image','ids':[ids[0]]})
                until=time.monotonic()+30
                while time.monotonic()<until:
                    d=client.get('/api/v1/ai/assets/image/'+ids[0]).json()
                    if d['state']=='complete':break
                    time.sleep(.2)
                assert d['effective']['description']=='Owner correction',d
                similar=client.get('/api/v1/ai/assets/image/'+ids[0]+'/similar')
                assert similar.status_code==200,similar.text
                # Machine API still requires a token; semantic delivery is opt-in.
                assert client.get('/api/v1/media/search?semantic=true').status_code==401
                client.patch('/api/v1/images/'+ids[0],json={'status':'approved'})
                token={'X-SML-Editor-Token':'test-token'}
                delivery=client.get('/api/v1/media/search',params={'query':'tiger','semantic':'true'},headers=token)
                assert delivery.status_code==200 and delivery.json()['items'][0]['id']==ids[0],delivery.text
                # Trash invalidates search immediately without deleting the original.
                client.delete('/api/v1/images/'+ids[0])
                assert all(a['id']!=ids[0] for a in client.get('/api/v1/ai/search').json()['items'])
                text_validation={}
                from ai_text import recognize_text,SpeechEngine,speech_ready
                from PIL import Image,ImageDraw,ImageFont
                if __import__('shutil').which('tesseract'):
                    image=Image.new('RGB',(1000,200),'white')
                    ImageDraw.Draw(image).text((30,60),'MOUNTAIN LIBRARY 2048',fill='black',font=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',52))
                    assert 'MOUNTAIN LIBRARY 2048' in recognize_text(image)['text']
                    text_validation['ocr']='PASS'
                if speech_ready(speech_cache):
                    audio=root/'speech-fixture.wav'
                    audio.write_bytes(urllib.request.urlopen('https://raw.githubusercontent.com/ggml-org/whisper.cpp/master/samples/jfk.wav',timeout=60).read())
                    speech=SpeechEngine(speech_cache).transcribe(audio,11)
                    assert speech['state']=='complete' and 'fellow Americans' in speech['text'],speech['state']
                    text_validation['speech']='PASS'
                summary={'text_validation':text_validation,'result':'PASS_REAL_AI_PIPELINE','sample_count':3,'semantic_top1':3,'metadata_persistence':True,'manual_precedence':True,'delete_invalidation':True,'editor_auth':True,'elapsed_seconds':round(time.monotonic()-start,2),'model_version':detail['model_version'],'worker_memory_budget_mb':1536,'production_data':'UNTOUCHED'}
                print(json.dumps(summary),flush=True)
                return summary
        finally:
            sys.modules.pop('app',None);sys.path.remove(str(APP))
            for key,value in saved.items():
                if value is None:os.environ.pop(key,None)
                else:os.environ[key]=value


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--model-cache',required=True,type=Path);args=p.parse_args();verify(args.model_cache)

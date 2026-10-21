"""Functional regressions: existing HTTP contract, real media and owned rollback."""
import hashlib
import http.server
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT/relative)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


class FunctionalTests(unittest.TestCase):
    def test_existing_audit_routes_pass_only_authorized_editing_gate(self):
        policy = load('sml_policy', 'apps/scene-builder/part01/precutover_policy.py')
        job = 'a'*32
        for path in (f'/api/audit-media/{job}/sml-auto-match',
                     f'/api/audit-media/{job}/SCENE_001/sml-select',
                     f'/api/audit-media/{job}/approve-all', f'/api/audit-media/{job}/continue'):
            self.assertTrue(policy.mutation_allowed('POST', path, True))
            self.assertFalse(policy.mutation_allowed('POST', path, False))
            self.assertFalse(policy.mutation_allowed('DELETE', path, True))
        self.assertFalse(policy.mutation_allowed('POST', f'/api/audit-media/{job}/delete-all', True))

    def test_filename_match_consistent_with_existing_editor_search(self):
        web = load('sml_web', 'apps/scene-builder/part01/sml_scene_bridge.py')
        worker = load('sml_client', 'apps/scene-builder/part02/sml_worker_bridge.py')
        row = dict(id='CLP-FILENAME', type='video', keywords=[], description='',
                   source_filename='mountain documentary.mp4',
                   content_url='/api/v1/media/video/CLP-FILENAME/content')
        self.assertEqual(web.rank_clips({'original_query': 'mountain'}, [row]), [row])
        client = worker.SMLWorkerClient(token='fixture')
        with patch.object(client, '_json', return_value={'items': [row]}):
            self.assertEqual(client.ranked('mountain'), [row])

    def test_busy_renderer_never_changes_private_environment(self):
        from production.scene_sml_worker import SceneSMLWorker
        from production import scene_sml_worker as module
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); old = root/'old'; new = root/'new'
            old.mkdir(); new.mkdir()
            env, unit, record = root/'env', root/'unit', root/'record'
            original = b'SCENE_WORKER_TOKEN=private\nSCENE_WORKER_DATA='+str(module.STATE).encode()+b'\n'
            env.write_bytes(original); env.chmod(0o600)
            unit.write_text('old-unit')
            record.write_text(json.dumps({'owner':'scene-production-worker-v1', 'unit':module.UNIT,
                                         'state':str(module.STATE), 'source':str(old)})); record.chmod(0o600)
            runner = root/'releases/scene-production-worker'/new.name
            runner.mkdir(parents=True)
            (runner/'vps_engine').symlink_to(new/'part02'); (runner/'entry.py').write_text(module.ENTRYPOINT)
            with patch.multiple(module, ENV=env, UNIT_FILE=unit, RECORD=record, BASE=root), \
                 patch.object(module, 'expected_units', side_effect=[('old-unit','old-unit'),('new-unit','new-unit')]), \
                 patch.object(module, 'health', return_value={'ok':True,'admission_only':False,'queue_counts':{'rendering':1}}):
                transaction = SceneSMLWorker(new, 'T'*64)
                with self.assertRaises(RuntimeError): transaction.install()
                self.assertFalse(transaction.changed)
                self.assertEqual(env.read_bytes(), original)
                self.assertEqual(unit.read_text(), 'old-unit')

    def test_renderer_rollback_restores_only_owned_env_unit_record(self):
        from production import scene_sml_worker as module
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); env=root/'env'; unit=root/'unit'; record=root/'record'
            for p in (env,unit,record): p.write_text('new'); p.chmod(0o600)
            txn=module.SceneSMLWorker(root, 'T'*64)
            txn.original_env=b'old-env'; txn.original_unit='old-unit'; txn.original_record=b'old-record'
            txn.changed=True; txn.stopped=True
            with patch.multiple(module,ENV=env,UNIT_FILE=unit,RECORD=record), \
                 patch.object(module,'database_idle'), patch.object(module,'_atomic_unit',side_effect=unit.write_text), \
                 patch.object(txn,'_run'), patch.object(txn,'_ready'):
                txn.rollback()
            self.assertEqual(env.read_bytes(), b'old-env')
            self.assertEqual(unit.read_text(), 'old-unit')
            self.assertEqual(record.read_bytes(), b'old-record')

    def test_real_http_acquisition_and_three_scene_renderer(self):
        # Only the upstream SML fixture is synthetic. Actual HTTP authentication,
        # checksum, reserve/commit, clip validation, trim and V2 rendering run.
        proof = load('sml_proof', 'production/scene_sml_render_proof.py')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); media={}; items=[]; actions=[]
            for index, color in enumerate(('red','green','blue')):
                path=root/f'{color}.mp4'
                subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-f','lavfi','-i',
                                f'color=c={color}:s=640x360:d=4','-c:v','libx264','-pix_fmt','yuv420p',str(path)],
                               check=True,capture_output=True,timeout=30)
                ident=f'CLP-{color.upper()}'
                media[ident]=path.read_bytes()
                items.append(dict(id=ident,type='video',keywords=[color],description=color,
                                  duration=4,quality=90,sha256=hashlib.sha256(media[ident]).hexdigest(),
                                  content_url=f'/api/v1/media/video/{ident}/content'))
            class Handler(http.server.BaseHTTPRequestHandler):
                def log_message(self,*args): pass
                def do_GET(self):
                    if self.headers.get('X-SML-Editor-Token') != 'fixture-token':
                        self.send_error(401); return
                    if self.path.startswith('/api/v1/media/search'):
                        self.send_response(200);self.end_headers();self.wfile.write(json.dumps({'items':items}).encode())
                    else:
                        ident=self.path.split('/')[-2]
                        self.send_response(200);self.end_headers();self.wfile.write(media[ident])
                def do_POST(self):
                    if self.headers.get('X-SML-Editor-Token') != 'fixture-token':
                        self.send_error(401); return
                    if self.path == '/api/v1/media/reservations':
                        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                        ident=body['preferred_ids'][0]
                        response={'items':[{'id':ident,'reservation_id':'RES-'+hashlib.md5(ident.encode()).hexdigest()}]}
                    else:
                        actions.append(self.path.split('/')[-1]); response={'ok':True}
                    self.send_response(200);self.end_headers();self.wfile.write(json.dumps(response).encode())
            server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                state=root/'state'; state.mkdir()
                with patch.dict(os.environ, {'SCENE_SML_URL':f'http://127.0.0.1:{server.server_port}',
                                            'SCENE_SML_EDITOR_TOKEN':'fixture-token',
                                            'SCENE_WORKER_DATA':str(state),'MIN_FREE_SPACE_GB':'1'}):
                    result=proof.verify(ROOT/'apps/scene-builder/part02',state)
                self.assertEqual(result['sml_scenes'],3)
                self.assertEqual(result['distinct_clips'],3)
                self.assertEqual(result['video_codec'],'h264')
                self.assertEqual(result['audio_codec'],'aac')
                self.assertEqual(actions.count('commit'),3)
                self.assertTrue(Path(result['output']).is_file())
                self.assertFalse((state/'queue.sqlite3').exists())
                self.assertLess(abs(result['duration']-6),.5)
            finally:
                server.shutdown(); server.server_close();thread.join()

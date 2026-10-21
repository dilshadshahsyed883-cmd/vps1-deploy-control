"""Real Scene HTTP handlers + isolated worker queue, no production state/loops."""
import os
import importlib.util
import shutil
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]

@unittest.skipUnless(all(importlib.util.find_spec(m) for m in ("fastapi", "httpx", "PIL", "jinja2", "multipart", "itsdangerous")) and shutil.which("ffprobe"), "real HTTP/media dependencies installed in dedicated backend CI")
class SceneBackendRepair(unittest.TestCase):
    def test_real_http_key_persistence_and_txt_wav_admission(self):
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--isolated'],
                                cwd=ROOT, capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('SCENE_BACKEND_ISOLATED=PASS', result.stdout)


def isolated():
    import io
    import json
    import re
    import secrets
    import shutil
    import sqlite3
    import tempfile
    import types
    import wave
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    sys.dont_write_bytecode = True
    with tempfile.TemporaryDirectory(prefix='scene-backend-regression-') as temp:
        root = Path(temp)
        access, worker_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        env = {'FRESH_WEBSITE_INSTALL':'1', 'VPS1_RUNTIME_CANDIDATE':'1',
               'VPS1_SCENE_PRECUTOVER_EDITING':'1', 'ATOMIC_FLOW_ENABLED':'0',
               'FRESH_AUTO_SCRIPT_READY':'0', 'WEB_USERNAME':'team',
               'WEB_ACCESS_KEY':access, 'SESSION_SECRET':secrets.token_urlsafe(32),
               'WEB_DATA_DIR':str(root/'web'), 'SCENE_PERMANENT_ROOT':str(root/'worker/permanent'),
               'SCRIPT_WORK_DATA_DIR':str(root/'script'), 'SCRIPT_PROCESSOR_DATA_DIR':str(root/'processor'),
               'MASTER_CONTROLLER_DB_PATH':str(root/'master.sqlite3'),
               'ATOMIC_FLOW_DB_PATH':str(root/'atomic.sqlite3'),
               'AUTOFLOW_FLOW_RESULT_ROOT':str(root/'results'), 'SCENE_CHANNELS_FILE':str(root/'channels.json'),
               'SCENE_WORKER_DATA':str(root/'worker'), 'SCENE_WORKER_TOKEN':worker_token,
               'WORKER_TOKEN':worker_token, 'WORKER_URL':'http://isolated-worker',
               'SCENE_WORKER_ADMISSION_ONLY':'0', 'DRIVE_UPLOAD_ENABLED':'0',
               'AUTO_CLEAN_MEDIA_AFTER_RENDER':'0', 'MIN_FREE_SPACE_GB':'1',
               'SCENE_INTERNAL_AUDIO_ENGINE_ROOT':str(root/'audio')}
        with patch.dict(os.environ, env):
            part1, part2 = ROOT/'apps/scene-builder/part01', ROOT/'apps/scene-builder/part02'
            sys.path.insert(0, str(part2))
            pkg = types.ModuleType('vps_engine'); pkg.__path__ = [str(part2)]
            sys.modules['vps_engine'] = pkg
            from vps_engine import api_server as worker
            worker.init_db()
            # No lifespan context: real queue handlers and SQLite run, no pipeline
            # or retention thread starts. Production guards remain unmodified.
            worker_client = TestClient(worker.app)
            sys.path.insert(0, str(part1))
            import smart_scroll
            smart_scroll.__path__.insert(0, str(part1/'smart_scroll'))
            import runtime_guard
            # CI state is disposable /tmp and may run as root. The existing
            # runtime_guard suite separately verifies production's filesystem gate.
            with patch.object(runtime_guard, 'validate_candidate_environment'):
                import app
            app.init_db()
            app.FONT_DIR = root/'fonts'; app.FONT_DIR.mkdir()
            def transport(method, url, **kwargs):
                kwargs.pop('timeout', None)
                return worker_client.request(method, url.replace('http://isolated-worker',''), **kwargs)
            with patch.object(app.requests, 'request', side_effect=transport):
                client = TestClient(app.app, base_url='https://localhost', follow_redirects=False)
                test_key = secrets.token_hex(16) + '1234'
                assert client.post('/queue/submit', data={}).status_code == 303
                assert client.post('/api/settings/pexels-keys', json={'action':'add','key':test_key}).status_code == 303
                assert client.post('/login', data={'username':'team','secret_key':access}).status_code == 303
                page = client.get('/settings'); assert page.status_code == 200, page.text[:200]
                csrf = re.search(r'id="sb-pexels-key-manager"[^>]*data-csrf="([^"]+)"', page.text).group(1)
                assert client.post('/api/settings/pexels-keys', json={'action':'add','key':test_key}).status_code == 403
                headers = {'X-Settings-CSRF':csrf}
                original = secrets.token_hex(16) + '5678'
                app.save_managed_pexels_keys([original])
                key = test_key
                added = client.post('/api/settings/pexels-keys', headers=headers, json={'action':'add','key':key})
                assert added.status_code == 200, added.text
                assert added.json()['total_key_count'] == 2, added.text
                assert key not in added.text and original not in added.text
                assert worker._current_pexels_keys() == [original,key]
                again = client.post('/api/settings/pexels-keys', headers=headers, json={'action':'add','key':key})
                assert again.json()['managed_count'] == 2
                assert app.PEXELS_KEYS_FILE.stat().st_mode & 0o777 == 0o600
                assert '2</b> active' in client.get('/settings').text
                # A fresh interpreter reads the durable file after a process reload.
                saved = subprocess.check_output([sys.executable,'-c',
                    'import pathlib; p=pathlib.Path(__import__("sys").argv[1]); print(len(p.read_text().splitlines()))',str(app.PEXELS_KEYS_FILE)],text=True)
                assert saved.strip() == '2'
                # An authenticated non-admin cannot read or mutate provider keys.
                with app.db() as conn:
                    conn.execute('UPDATE users SET is_admin=0 WHERE username=?',('team',))
                assert client.get('/api/settings/pexels-keys').status_code == 403
                assert client.post('/api/settings/pexels-keys',headers=headers,json={'action':'add','key':secrets.token_hex(16)}).status_code == 403
                assert app.managed_pexels_keys() == [original,key]
                with app.db() as conn:
                    conn.execute('UPDATE users SET is_admin=1 WHERE username=?',('team',))
                snapshot = client.get('/api/settings/pexels-keys').json()
                kid = next(k['key_id'] for k in snapshot['keys'] if k['masked'].endswith('1234'))
                assert client.post('/api/settings/pexels-keys',headers=headers,json={'action':'remove','key_id':kid}).status_code == 200
                assert app.managed_pexels_keys() == [original]
                assert worker._current_pexels_keys() == [original]
                for path in ('/queue/other/start','/queue/submit/extra','/api/settings/pexels-keys/extra','/api/admin/storage/cleanup'):
                    assert client.post(path,json={}).status_code == 403, path
                assert client.post('/queue/submit',data={}).status_code == 422
                audio = io.BytesIO()
                with wave.open(audio,'wb') as wav:
                    wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000); wav.writeframes(b'\0\0'*16000)
                data = {'project_name':'Disposable_Backend_Regression', 'audio_engine_enabled':'0'}
                script = b'SCENE_001|This is a disposable project.|media_type=video;primary_keyword=nature;backup_keywords=forest;visual_direction=wide landscape;shot_preference=wide;pace=normal;visual_mood=calm;transition_intent=cut;effect_intent=none;allow_media_fallback=true\n'
                files = {'script_file':('scene.txt',script,'text/plain'), 'audio_file':('voice.wav',audio.getvalue(),'audio/wav')}
                admitted = client.post('/queue/submit',data=data,files=files,headers={'Accept':'application/json'})
                assert admitted.status_code == 200, admitted.text[:1000]
                job_id = admitted.json()['job_id']
                assert admitted.json()['audio_engine_job_id'] == ''
                row = worker.get_job(job_id)
                assert row['status'] == 'queued' and row['client_job_key']
                with app.db() as conn:
                    assert conn.execute('SELECT owner_username FROM job_ownership WHERE job_id=?',(job_id,)).fetchone()[0] == 'team'
                replay = client.post('/queue/submit',data=data,files=files,headers={'Accept':'application/json'})
                assert replay.status_code == 200 and replay.json()['job_id'] == job_id and replay.json()['duplicate'], replay.text
                with sqlite3.connect(worker.DB_PATH) as db:
                    assert db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 1
                worker_client.close(); client.close()
    print('SCENE_BACKEND_ISOLATED=PASS KEYS_PERSIST=PASS TXT_WAV_QUEUE_SQLITE_DEDUP=PASS')

if __name__ == '__main__':
    if '--isolated' in sys.argv: isolated()
    else: unittest.main()

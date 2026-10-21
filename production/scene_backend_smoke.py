"""Authenticated Scene backend release admission; only owns its disposable job.

Never prints keys/cookies or changes workers, presets or pre-existing jobs.
"""
from __future__ import annotations
import hashlib
import http.client
import io
import json
from pathlib import Path
import re
import socket
import sqlite3
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave

DOMAIN = 'app.scenebuilder.online'
ENV = Path('/etc/fresh-website/scene-builder.env')


def require(condition, reason):
    if not condition:
        raise RuntimeError('SCENE_BACKEND_SMOKE: ' + reason)


def verify():
    env = dict(line.split('=', 1) for line in ENV.read_text().splitlines()
               if '=' in line and not line.startswith('#'))
    env = {k: v.strip().strip('\"\'') for k, v in env.items()}
    cookie = ''

    def web(method, path, body=None, headers=None):
        nonlocal cookie
        sock = socket.create_connection(('127.0.0.1', 443), timeout=90)
        tls = ssl.create_default_context().wrap_socket(sock, server_hostname=DOMAIN)
        conn = http.client.HTTPSConnection(DOMAIN, timeout=90)
        conn.sock = tls
        try:
            values = {'Host': DOMAIN, 'Connection': 'close', **(headers or {})}
            if cookie: values['Cookie'] = cookie
            conn.request(method, path, body=body, headers=values)
            response = conn.getresponse()
            if response.getheader('Set-Cookie', '').startswith('session='):
                cookie = response.getheader('Set-Cookie').split(';', 1)[0]
            return response.status, response.read(2_000_000)
        finally:
            conn.close()

    def worker(method, path):
        headers = {'x-scene-worker-token': env.get('WORKER_TOKEN', '')}
        req = urllib.request.Request(env.get('WORKER_URL', 'http://127.0.0.1:8000').rstrip('/')+path,
                                     headers=headers, method=method)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=30) as response: return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, {}

    status, _ = web('POST', '/queue/submit', b'')
    require(status in (303,401), 'anonymous project submission was not denied')
    login = urllib.parse.urlencode({'username':env.get('WEB_USERNAME','team'), 'secret_key':env['WEB_ACCESS_KEY']}).encode()
    status, _ = web('POST','/login',login,{'Content-Type':'application/x-www-form-urlencoded'})
    require(status == 303 and cookie, 'administrator login failed')
    status, settings = web('GET','/settings')
    token = re.search(rb'id="sb-pexels-key-manager"[^>]*data-csrf="([^"]+)"', settings)
    require(status == 200 and token is not None, 'Pexels settings CSRF missing')
    csrf = token.group(1).decode()
    status, health = worker('GET','/health')
    require(status == 200 and health.get('ok') is True and not health.get('admission_only'), 'configured project worker is not writable/healthy')
    key_file = Path(env['SCENE_PERMANENT_ROOT'])/'pexels_keys.txt'
    worker_file = str(health.get('dynamic_pexels_key_file') or '')
    require(worker_file and Path(worker_file).resolve() == key_file.resolve(), 'web/worker Pexels permanent file paths differ')
    original = key_file.read_bytes() if key_file.exists() else b''
    keys = list(dict.fromkeys(re.split(r'[,;\s]+',original.decode().strip()))) if original.strip() else []
    status, _ = web('POST','/api/settings/pexels-keys',b'{"action":"add","key":"invalid-probe"}',{'Content-Type':'application/json'})
    require(status == 403, 'Pexels mutation did not enforce CSRF')
    # Duplicate an existing managed key: actual Add Key + disk persistence,
    # without introducing a fake provider key into an active production worker.
    if keys:
        status, payload = web('POST','/api/settings/pexels-keys',json.dumps({'action':'add','key':keys[0]}).encode(),
                              {'Content-Type':'application/json','X-Settings-CSRF':csrf})
        require(status == 200, 'authorized Pexels Add Key failed')
        state = json.loads(payload)
        require(state['managed_count'] == len(keys), 'duplicate Add Key changed the existing key set')
        require(state.get('total_key_count',0) >= len(keys), 'active-key count excludes saved keys')
        require(set(re.split(r'[,;\s]+', key_file.read_text().strip())) == set(keys), 'persisted existing keys changed')
        status, payload = web('GET','/api/settings/pexels-keys')
        require(status == 200 and json.loads(payload)['managed_count'] == len(keys), 'Pexels reload did not retain keys')
        pexels_result = 'pass'
    else:
        # With no configured key, exercise only an idle worker so the temporary
        # fixture cannot replace a provider credential used by existing jobs.
        terminal = {'completed','failed','cancelled','cleaned'}
        require(not any(int(count) for state,count in health.get('queue_counts',{}).items()
                        if state not in terminal), 'Pexels fixture requires an idle queue when no managed key exists')
        fixture = uuid.uuid4().hex
        fixture_id = hashlib.sha256(fixture.encode()).hexdigest()[:16]
        try:
            status, payload = web('POST','/api/settings/pexels-keys',json.dumps({'action':'add','key':fixture}).encode(),
                                  {'Content-Type':'application/json','X-Settings-CSRF':csrf})
            require(status == 200, 'disposable Pexels Add Key failed')
            state = json.loads(payload)
            require(any(row['key_id'] == fixture_id for row in state['keys']), 'added key absent from response')
            require(state.get('managed_count') == 1 and state.get('total_key_count',0) >= 1, 'active-key count did not update')
            code, updated_health = worker('GET','/health')
            require(code == 200 and updated_health.get('pexels_key_count',0) >= 1, 'production worker did not reload the saved key')
            require(fixture in re.split(r'[,;\s]+',key_file.read_text()), 'added key was not saved on disk')
            status, payload = web('GET','/api/settings/pexels-keys')
            require(status == 200 and any(row['key_id'] == fixture_id for row in json.loads(payload)['keys']), 'added key absent after API reload')
            status, page = web('GET','/settings')
            require(status == 200 and fixture_id.encode() in page, 'added key absent after settings reload')
        finally:
            # Remove only this generated fixture; never restore a stale snapshot
            # over keys added concurrently by an administrator.
            status, _ = web('POST','/api/settings/pexels-keys',json.dumps({'action':'remove','key_id':fixture_id}).encode(),
                            {'Content-Type':'application/json','X-Settings-CSRF':csrf})
            require(status in (200,404) and fixture not in key_file.read_text(), 'disposable Pexels key cleanup failed')
        pexels_result = 'pass'
    status, page = web('GET','/settings')
    require(status == 200 and b'id="sb-pexels-total"' in page, 'active-key count missing on reload')

    name = 'SC_Backend_Disposable_' + uuid.uuid4().hex[:16]
    audio = io.BytesIO()
    with wave.open(audio,'wb') as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000); wav.writeframes(b'\0\0'*16000)
    script = b'SCENE_001|This is a disposable backend test.|media_type=video;primary_keyword=nature;backup_keywords=forest;visual_direction=wide landscape;shot_preference=wide;pace=normal;visual_mood=calm;transition_intent=cut;effect_intent=none;allow_media_fallback=true\n'
    boundary = 'scene' + uuid.uuid4().hex
    parts = []
    def field(key, content, filename='', mime='text/plain'):
        disposition = 'form-data; name="'+key+'"' + ('; filename="'+filename+'"' if filename else '')
        parts.append(('--'+boundary+'\r\nContent-Disposition: '+disposition+'\r\nContent-Type: '+mime+'\r\n\r\n').encode()+content+b'\r\n')
    field('project_name',name.encode()); field('audio_engine_enabled',b'0')
    field('script_file',script,'disposable.txt'); field('audio_file',audio.getvalue(),'disposable.wav','audio/wav')
    body = b''.join(parts)+('--'+boundary+'--\r\n').encode()
    job_id = ''
    try:
        status, response = web('POST','/queue/submit',body,{'Content-Type':'multipart/form-data; boundary='+boundary,'Accept':'application/json'})
        # Response details are not logged: they may contain credentialed upstream URLs.
        require(status == 200, 'production TXT+WAV project submission returned HTTP '+str(status))
        result = json.loads(response); job_id = str(result.get('job_id') or '')
        require(result.get('ok') is True and re.fullmatch('[a-f0-9]{32}',job_id), 'project admission did not return a job ID')
        status, job = worker('GET','/jobs/'+job_id)
        require(status == 200 and job.get('project_name') == name, 'disposable project not persisted in worker')
        db_path = Path(env['WEB_DATA_DIR'])/'web.sqlite3'
        with sqlite3.connect('file:'+str(db_path)+'?mode=ro',uri=True) as conn:
            row = conn.execute('SELECT owner_username FROM job_ownership WHERE job_id=?',(job_id,)).fetchone()
        require(row and row[0] == env.get('WEB_USERNAME','team'), 'disposable project ownership not persisted')
        print('SCENE_BACKEND_PROJECT=PASS_HTTPS_TXT_WAV_QUEUE_OWNERSHIP job_id='+job_id,flush=True)
    finally:
        if not job_id:
            # Worker acceptance can precede a failed web response. Recover only
            # our unique test name, so this failure path also cleans its job.
            code, listing = worker('GET','/jobs?limit=1000')
            owned = [row for row in listing.get('jobs',[]) if row.get('project_name') == name] if code == 200 else []
            require(len(owned) <= 1, 'ambiguous disposable project ownership')
            if owned: job_id = str(owned[0].get('id') or '')
        if job_id:
            # DELETE refuses active jobs. Never cancel a running job: cancellation
            # may modify the next user's queue slot. Wait only for this test job.
            cleaned = False
            for attempt in range(20):
                status, job = worker('GET','/jobs/'+job_id)
                require(status == 200 and job.get('project_name') == name, 'disposable cleanup ownership mismatch')
                status, _ = worker('DELETE','/jobs/'+job_id)
                if status == 200:
                    cleaned = True
                    break
                require(status == 409, 'disposable cleanup returned unexpected status')
                time.sleep(1)
            require(cleaned, 'disposable project is active; retained safely: '+job_id)
            with sqlite3.connect(Path(env['WEB_DATA_DIR'])/'web.sqlite3') as conn:
                conn.execute('DELETE FROM job_ownership WHERE job_id=? AND owner_username=? AND project_name=?',
                             (job_id,env.get('WEB_USERNAME','team'),name))
            print('SCENE_BACKEND_DISPOSABLE_CLEANUP=PASS',flush=True)
    print('SCENE_BACKEND_PEXELS='+pexels_result,flush=True)
    print('SCENE_BACKEND_GUARDED_PRODUCTION=PASS',flush=True)
    return {'project_submission':'PASS','pexels':pexels_result,'disposable_cleanup':'PASS'}

if __name__ == '__main__': verify()

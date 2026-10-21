"""Own the writable Scene worker; retain the separate read-only admission service.

Only the guarded Scene release calls this module. No existing queue is moved,
no admission flag is changed, and no unrelated unit or credential is replaced.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import stat
import subprocess
import time
import urllib.error
import urllib.request

BASE = Path('/srv/fresh-website')
STATE = Path('/var/lib/fresh-website/scene-builder')
WEB_ENV = Path('/etc/fresh-website/scene-builder.env')
ENV = Path('/etc/fresh-website/scene-production-worker.env')
UNIT = 'vps1-scene-production-worker.service'
UNIT_FILE = Path('/etc/systemd/system')/UNIT
RECORD = BASE/'control/state/scene-production-worker.json'
VENV = BASE/'venvs/scene-production-worker'
PORT = 18081
URL = 'http://127.0.0.1:'+str(PORT)
DEPENDENCIES = ('fastapi==0.115.12','uvicorn==0.34.2','python-multipart==0.0.20',
                'requests==2.32.3','pillow==11.1.0','faster-whisper==1.2.0','av==16.1.0')


def require(ok, message):
    if not ok: raise RuntimeError('Scene production worker: '+message)


def entries(raw):
    result = {}
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith('#') or '=' not in line: continue
        key,value = line.split('=',1)
        require(key not in result, 'duplicate environment setting')
        result[key] = value.strip().strip('\"\'')
    return result


def bind_web(raw, token):
    require(re.fullmatch(r'[A-Za-z0-9_-]{32,256}',token), 'invalid private token')
    entries(raw)  # Reject ambiguous duplicate settings before touching anything.
    lines = [line for line in raw.splitlines() if line.split('=',1)[0] not in {'WORKER_URL','WORKER_TOKEN'}]
    return '\n'.join(lines+['WORKER_URL='+URL,'WORKER_TOKEN='+token])+'\n'


def health(url, token=''):
    try:
        req = urllib.request.Request(url+'/health',headers={'x-scene-worker-token':token})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=5) as response:
            return json.load(response)
    except (OSError,ValueError): return {}


def unit_text(runner):
    return f'''[Unit]
Description=Scene Builder authenticated production queue and pipeline
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=vps1scene
Group=vps1scene
WorkingDirectory={runner}
EnvironmentFile={ENV}
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PYTHONPATH={runner}:{runner}/vps_engine
ExecStart={VENV}/bin/uvicorn entry:app --host 127.0.0.1 --port {PORT} --workers 1
Restart=on-failure
RestartSec=4
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=true
ReadWritePaths={STATE}
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
UMask=0077
[Install]
WantedBy=multi-user.target
'''


ENTRYPOINT = """from pathlib import Path
import os
fresh = not (Path(os.environ['SCENE_WORKER_DATA'])/'queue.sqlite3').exists()
from vps_engine import api_server as worker
worker.init_db()
if fresh:
    worker._pause_pipeline('Production release admission')
app = worker.app
"""


class ProductionWorker:
    def __init__(self, source, journal):
        self.source, self.journal = Path(source), Path(journal)
        self.started = False
        self.managed = False

    def ensure(self):
        from production.runtime import run, atomic_json
        from production.scene_candidate_release import _atomic_scene_env
        require(os.geteuid() == 0, 'protected host release required')
        raw = WEB_ENV.read_text(); web = entries(raw)
        previous_url = web.get('WORKER_URL','http://127.0.0.1:8000').rstrip('/')
        require(re.fullmatch(r'http://127\.0\.0\.1:[0-9]+',previous_url), 'unreviewed configured worker endpoint')
        current = health(previous_url, web.get('WORKER_TOKEN',''))
        if current.get('ok') is True and current.get('admission_only') is False:
            return None  # Preserve an already authorized independent production worker.
        require(previous_url in {'http://127.0.0.1:1','http://127.0.0.1:8000',URL},
                'refuse to replace an unknown worker binding')
        admission = health('http://127.0.0.1:8000')
        require(admission.get('admission_only') is True and not any(admission.get('queue_counts',{}).values()),
                'existing admission worker must remain read-only with no projects to migrate')
        require(Path(web.get('SCENE_PERMANENT_ROOT','')).resolve() == STATE/'permanent',
                'permanent key/audio library must remain in the existing Scene root')
        self.managed = True
        queue = STATE/'queue.sqlite3'
        if RECORD.exists():
            record = json.loads(RECORD.read_text())
            require(record.get('owner') == 'scene-production-worker-v1' and record.get('unit') == UNIT
                    and record.get('state') == str(STATE) and UNIT_FILE.is_file() and ENV.is_file(),
                    'existing worker resources have unknown ownership')
        else:
            require(not UNIT_FILE.exists() and not ENV.exists() and not queue.exists(),
                    'refuse to overwrite any existing worker resources or queue')
            with socket.socket() as sock: sock.bind(('127.0.0.1',PORT))
            require((self.source/'part02/api_server.py').is_file(), 'production API source missing')
            runner = BASE/'releases/scene-production-worker'/self.source.name
            if runner.exists():
                require((runner/'vps_engine').is_symlink() and (runner/'vps_engine').resolve() == self.source/'part02', 'unowned production source alias')
            else:
                runner.mkdir(parents=True)
                (runner/'vps_engine').symlink_to(self.source/'part02',target_is_directory=True)
            (runner/'entry.py').write_text(ENTRYPOINT)

            print('SCENE_PRODUCTION_WORKER=PREPARING_ISOLATED_VENV',flush=True)
            if not (VENV/'bin/python').is_file():
                run(['/usr/bin/python3','-m','venv',str(VENV)],timeout=90,quiet=True)
            run([str(VENV/'bin/python'),'-m','pip','install','--disable-pip-version-check',*DEPENDENCIES],timeout=600,quiet=True)
            require(subprocess.run(['systemctl','is-active','--quiet','vps1-scene-worker-admission.service']).returncode == 0,
                    'admission service changed during preparation')
            token = secrets.token_urlsafe(48)
            values = {'SCENE_WORKER_DATA':str(STATE),'SCENE_WORKER_TOKEN':token,
                      'SCENE_WORKER_ADMISSION_ONLY':'0','DRIVE_UPLOAD_ENABLED':'0',
                      'AUTO_CLEAN_MEDIA_AFTER_RENDER':'1','MIN_FREE_SPACE_GB':'5',
                      'SCENE_INTERNAL_AUDIO_ENGINE_ROOT':str(STATE/'internal-audio-engine'),
                      'HF_HOME':str(STATE/'model-cache'),'XDG_CACHE_HOME':str(STATE/'cache')}
            # Retain any preconfigured app-specific Drive, stock and SML settings.
            for key in ('PEXELS_API_KEY','PEXELS_API_KEYS','SCENE_SML_URL','SCENE_SML_EDITOR_TOKEN',
                        'DRIVE_UPLOAD_ENABLED','DRIVE_REMOTE','RCLONE_CONFIG'):
                if key in web: values[key] = web[key]
            fd = os.open(ENV,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w') as out:
                out.write(''.join(k+'='+v+'\n' for k,v in values.items()))
            UNIT_FILE.write_text(unit_text(runner)); UNIT_FILE.chmod(0o644)
            atomic_json(RECORD,{'owner':'scene-production-worker-v1','unit':UNIT,'state':str(STATE),
                                'source':str(self.source),'endpoint':URL},0o600)
            run(['systemctl','daemon-reload'],timeout=20,quiet=True)
        token = entries(ENV.read_text())['SCENE_WORKER_TOKEN']
        active = subprocess.run(['systemctl','is-active','--quiet',UNIT]).returncode == 0
        if not active:
            self.started = True
            run(['systemctl','enable','--now',UNIT],timeout=45,quiet=True)
        for attempt in range(60):
            result = health(URL,token)
            if result.get('ok') is True and result.get('admission_only') is False: break
            time.sleep(.5)
        else: raise RuntimeError('Scene production worker failed authenticated runtime health')
        require(Path(result.get('dynamic_pexels_key_file','')).resolve() == STATE/'permanent/pexels_keys.txt',
                'web/production worker key path mismatch')
        updated = bind_web(raw,token)
        if updated != raw:
            _atomic_scene_env(updated.encode())
            print('SCENE_PRODUCTION_WORKER=READY ADMISSION_WORKER=UNCHANGED KEY_PATH=SHARED',flush=True)
            return raw.encode()
        return None

    def commit(self):
        if not self.managed: return
        token = entries(ENV.read_text())['SCENE_WORKER_TOKEN']
        result = health(URL,token)
        pause = result.get('pipeline_pause') or {}
        if pause.get('paused') and pause.get('reason') == 'Production release admission':
            req = urllib.request.Request(URL+'/pipeline/resume',method='POST',headers={'x-scene-worker-token':token})
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=10) as response:
                require(response.status == 200 and json.load(response).get('pipeline_paused') is False,
                        'new production pipeline did not resume')
        print('SCENE_PRODUCTION_PIPELINE=RELEASED AFTER_DISPOSABLE_ADMISSION=PASS',flush=True)

    def rollback(self):
        # Stop only our newly started service, and only if it owns no jobs.
        # Concurrent user submissions are never cancelled, deleted or migrated.
        if not self.started: return
        queue = STATE/'queue.sqlite3'
        try:
            if queue.is_file():
                with sqlite3.connect('file:'+str(queue)+'?mode=ro',uri=True) as conn:
                    if conn.execute('SELECT count(*) FROM jobs').fetchone()[0]:
                        print('SCENE_PRODUCTION_WORKER=RETAINED_WITH_PROJECTS',flush=True)
                        return
            subprocess.run(['systemctl','disable','--now',UNIT],check=True,capture_output=True,timeout=45)
            print('SCENE_PRODUCTION_WORKER_ROLLBACK=STOPPED_EMPTY_OWNED_SERVICE',flush=True)
        except Exception:
            print('SCENE_PRODUCTION_WORKER_ROLLBACK=RETAINED_STATE_FOR_RECOVERY',flush=True)
            raise

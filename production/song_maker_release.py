"""New-app release contract. Requires reviewed Song Maker helper capability.

No generic cutover or existing application reinstall. Existing Scene source is
copied to an immutable overlay; only its SSO allowlist and shared shell JS change.
Persistent databases, credentials and render workers are never replaced.
"""
from __future__ import annotations
import hashlib
import fcntl
import sqlite3
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import time
from urllib import request
from production.runtime import BASE, REPO, CONFIG, DATA, atomic_json, atomic_link, check_host, run, backup_databases, deployment_lock
from production.scene_public_https import SITE
from production.route_uploader_scene_https import scene_cookie, https_get

APP='song-maker'
PORT=8150
STATE=DATA/APP
CURRENT=BASE/'current'/APP
SCENE_CURRENT=BASE/'current/scene-builder'
ROOT=Path('/etc/systemd/system')
TRANSACTIONS=BASE/'transactions/song-maker'
LANES=('web','render','upload','sync')
MARKER=' location = /internal/runtime-ready { return 404; }'
INCLUDE=' include /etc/nginx/snippets/vps1-song-maker.conf;\n'
SNIPPET=Path('/etc/nginx/snippets/vps1-song-maker.conf')


def require(ok,message):
    if not ok:raise RuntimeError('SONG_MAKER_BLOCKED: '+message)


def unit_name(lane):
    return 'vps1-production-song-maker'+('' if lane=='web' else '-'+lane)+'.service'


def route_config():
    return '''# VPS1_SONG_MAKER_SSO_V1
location = /_vps1_song_maker_auth {
    internal;
    proxy_pass http://127.0.0.1:18080/internal/workspace-auth?app=song-maker;
    proxy_pass_request_body off;
    proxy_set_header Content-Length "";
    proxy_set_header Cookie $http_cookie;
    proxy_set_header X-Auth-User "";
    proxy_set_header X-Is-Admin "";
    proxy_set_header X-App-Access "";
}
location = /song-maker { return 308 /song-maker/; }
location ^~ /song-maker/ {
    auth_request /_vps1_song_maker_auth;
    auth_request_set $song_user $upstream_http_x_auth_user;
    auth_request_set $song_admin $upstream_http_x_is_admin;
    proxy_set_header X-Auth-User $song_user;
    proxy_set_header X-Is-Admin $song_admin;
    proxy_set_header X-App-Access "";
    proxy_set_header Authorization "";
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto https;
    proxy_pass http://127.0.0.1:8150;
    proxy_read_timeout 300s;
}
'''


def scene_allowlist(text):
    start=text.index('SCENE_SSO_APP_IDS = frozenset({')
    end=text.index('})',start)
    group=text[start:end]
    require('"short-maker"' in group and '"video-merger"' in group,'unrecognized Scene SSO allowlist')
    if '"song-maker"' not in group:
        text=text[:end]+'    "song-maker",\n'+text[end:]
    return text


def source_files():
    # Explicit app ownership; no central ten-app group/automatic migration switches.
    base=REPO/'apps/song-maker'
    files={}
    for path in sorted(base.rglob('*')):
        if not path.is_file() or '__pycache__' in path.parts:continue
        require(not path.is_symlink(),'source symlink')
        require(path.suffix in {'.py','.html','.css','.js','.txt','.md'},'unexpected source type')
        files[str(path.relative_to(base))]=path.read_bytes()
    require('part01/songmaker/app.py' in files,'incomplete Song Maker source')
    return files


def stage(revision):
    release=BASE/'releases'/APP/revision
    files=source_files()
    manifest={'app':APP,'revision':revision,'files':{n:hashlib.sha256(v).hexdigest() for n,v in files.items()}}
    if release.exists():
        require(json.loads((release/'_production_manifest.json').read_text())==manifest,'immutable manifest drift')
        for name,data in files.items():require((release/name).read_bytes()==data,'immutable source drift')
    else:
        release.mkdir(parents=True)
        for name,data in files.items():
            path=release/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data);path.chmod(0o644)
        atomic_json(release/'_production_manifest.json',manifest,0o644)
    return release


def unit(lane,revision):
    python=BASE/'venvs'/APP/revision/'bin/python'
    command=(f'{python} -m uvicorn songmaker.app:app --host 127.0.0.1 --port {PORT} --proxy-headers --forwarded-allow-ips=127.0.0.1'
             if lane=='web' else f'{python} -m songmaker.worker {lane}')
    return f'''[Unit]
Description=VPS_1 Song Maker {lane}
After=network-online.target
[Service]
User=ubuntu
Group=ubuntu
WorkingDirectory={CURRENT}/part01
EnvironmentFile={CONFIG}/production-song-maker.env
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart={command}
Restart=on-failure
RestartSec=3
TimeoutStopSec=15
KillMode=control-group
UMask=0077
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths={STATE} -/home/ubuntu/.config/masterctl
PrivateTmp=yes
CPUQuota=150%
MemoryMax=1536M
TasksMax=64
[Install]
WantedBy=multi-user.target
'''


def verify():
    deadline=time.monotonic()+40
    while time.monotonic()<deadline:
        try:
            with request.urlopen(f'http://127.0.0.1:{PORT}/health',timeout=3) as response:
                health=json.load(response)
            if all(health['workers'].get(lane) for lane in LANES[1:]):break
        except (OSError,ValueError,KeyError):pass
        time.sleep(.5)
    else:raise RuntimeError('Song Maker worker readiness failed')
    cookie=scene_cookie()
    for path in ('/song-maker/','/song-maker/api/state'):
        code,_,body=https_get(path,cookie)
        require(code==200,'authenticated Song Maker route failed')
        code,_,_=https_get(path)
        require(code in {401,403},'anonymous Song Maker route not denied')
    for path in ('/scene-builder/','/short-maker/','/auto-caption-studio/','/video-merger/'):
        require(https_get(path,cookie)[0]==200,'neighbor regression '+path)
    require(https_get('/_vps1_song_maker_auth',cookie)[0]==404,'SSO introspection exposed')
    return {'service':'PASS','workers':'PASS','authentication':'PASS','neighbors':'PASS',
            'media_drive_e2e':'NOT_VERIFIED_BY_HEALTH'}


def verify_pc_folder_controls():
    cookie=scene_cookie()
    code,_,body=https_get('/song-maker/',cookie)
    require(code==200 and all(marker in body for marker in
            (b'id="pcSongFolder"',b'id="pcCreateFolder"',b'id="pcRefreshFolders"')),
            'PC folder controls are not live')
    for path,key in (('/song-maker/api/manual-folder','song_folder_id'),('/song-maker/api/folders','folders')):
        code,kind,body=https_get(path,cookie)
        require(code==200 and 'json' in kind.lower() and key in json.loads(body),
                'PC folder selection/registry readback failed')
    print('SONG_MAKER_PC_FOLDERS_READBACK=PASS',flush=True)
    return {'pc_folder_controls':'PASS','shared_registry':'PASS','selection_persistence_api':'PASS'}


def restore(previous):
    # Caller holds host deployment lock; restore only exact scoped preimages.
    for lane in LANES:subprocess.run(['systemctl','stop',unit_name(lane)],capture_output=True)
    for lane in LANES:
        file=ROOT/unit_name(lane)
        if previous['units'][lane] is None:
            subprocess.run(['systemctl','disable',unit_name(lane)],capture_output=True)
            file.unlink(missing_ok=True)
        else:file.write_text(previous['units'][lane])
    if previous['current']:atomic_link(CURRENT,previous['current'])
    elif CURRENT.is_symlink():CURRENT.unlink()
    atomic_link(SCENE_CURRENT,previous['scene'])
    SITE.write_text(previous['site'])
    if previous['snippet'] is None:SNIPPET.unlink(missing_ok=True)
    else:SNIPPET.write_text(previous['snippet'])
    run(['nginx','-t'],quiet=True)
    run(['systemctl','daemon-reload'],quiet=True)
    run(['systemctl','restart','vps1-scene-candidate.service'],quiet=True)
    from production.scene_candidate_release import _health
    _health()
    run(['systemctl','reload','nginx'],quiet=True)
    for lane in LANES:
        if previous['active'][lane]:run(['systemctl','start',unit_name(lane)],quiet=True)


def verify_shared_shell(installed: bytes, reviewed: bytes) -> None:
    # Accept either the original audited shell or the exact protected-main
    # shell already installed by the coordinated Song Studio release.
    allowed = {'d7b66595009c905307df4beb4064545262cbdca8c13e4d94200c774cb4cc8fd4',
               hashlib.sha256(reviewed).hexdigest()}
    require(hashlib.sha256(installed).hexdigest() in allowed,
            'shared shell drift; reconcile before overlay')


def require_idle_jobs(database):
    """Fail closed if a render, upload, pending item or worker claim exists."""
    require(database.is_file() and not database.is_symlink(),
            'Song Maker database missing or untrusted')
    with sqlite3.connect('file:' + str(database) + '?mode=ro', uri=True) as db:
        require(db.execute('PRAGMA quick_check').fetchone()[0] == 'ok',
                'Song Maker database integrity failed')
        pending = db.execute(
            "SELECT 1 FROM jobs WHERE deleted=0 AND "
            "(lane != '' OR stage NOT IN ('Completed','Failed','Cancelled',"
            "'Waiting for Drive','Needs Attention')) LIMIT 1").fetchone()
        require(pending is None, 'active or queued Song Maker work; leave production unchanged')


def restore_update(previous):
    """Update rollback touches only the Song Maker pointer and its four units."""
    for lane in LANES:
        subprocess.run(['systemctl', 'stop', unit_name(lane)], check=False,
                       capture_output=True)
    atomic_link(CURRENT, previous['current'])
    for lane in LANES:
        name = unit_name(lane)
        (ROOT/name).write_text(previous['units'][lane])
    run(['systemctl', 'daemon-reload'], quiet=True)
    for lane in LANES:
        if previous['active'][lane]:
            run(['systemctl', 'start', unit_name(lane)], quiet=True)
    return verify()


def update_existing(revision):
    """Idle-fenced app-only upgrade; state, Scene, Nginx and other workers stay put."""
    require(CURRENT.is_symlink(), 'Song Maker has no owned install')
    old = CURRENT.resolve(strict=True)
    require(old.is_relative_to(BASE/'releases'/APP) and
            re.fullmatch(r'[a-f0-9]{40}', old.name),
            'installed Song Maker release not owned')
    require(SNIPPET.is_file() and not SNIPPET.is_symlink() and
            SNIPPET.read_text() == route_config(),
            'Song Maker route drift')
    require(SITE.is_file() and not SITE.is_symlink() and
            SITE.read_text().count(INCLUDE) == 1,
            'Song Maker site route drift')
    require((CONFIG/'production-song-maker.env').is_file(),
            'Song Maker environment missing')
    units = {lane: (ROOT/unit_name(lane)).read_text() for lane in LANES}
    for lane in LANES:
        require(units[lane] == unit(lane,old.name),
                'Song Maker service drift: '+lane)
    require(old.name != revision, 'already installed; refusing redundant restart')
    release = stage(revision)
    venv = BASE/'venvs'/APP/revision
    run(['python3','-m','venv',venv],quiet=True)
    run([venv/'bin/pip','install','--no-cache-dir','-r',
         release/'part01/requirements.txt','httpx==0.28.1'],timeout=600,quiet=True)
    # Disposable real FFmpeg/API/Drive-fake tests before touching live services.
    run([venv/'bin/python','-m','unittest','discover','-s','tests',
         '-p','test_song_maker.py'],cwd=REPO,timeout=240,quiet=True)
    previous = {'kind':'update','current':str(old), 'units':units,
                'active':{lane:subprocess.run(
                    ['systemctl','is-active','--quiet',unit_name(lane)],
                    capture_output=True).returncode == 0 for lane in LANES}}
    require(all(previous['active'].values()),
            'Song Maker service not fully active; do not repair through release')
    journal = TRANSACTIONS/revision
    require(not journal.exists(), 'Song Maker update journal already exists')
    journal.mkdir(parents=True)
    atomic_json(journal/'previous.json',previous)
    database = STATE/'state/jobs.sqlite3'
    backup_databases(STATE,journal/'sqlite-backup')
    touched = False
    admission_lock = None
    try:
        # The scanner is read-only until pair admission; stop it before fencing.
        run(['systemctl','stop',unit_name('sync')],quiet=True)
        submission = STATE/'state/submission.lock'
        require(submission.is_file() and not submission.is_symlink(),
                'Song Maker submission lock missing/untrusted')
        admission_lock = submission.open('a')
        fcntl.flock(admission_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        require_idle_jobs(database)
        # Fence any new web jobs and worker claims until verification completes.
        for lane in ('web','render','upload'):
            run(['systemctl','stop',unit_name(lane)],quiet=True)
        require_idle_jobs(database)
        touched = True
        atomic_link(CURRENT,release)
        for lane in LANES:
            (ROOT/unit_name(lane)).write_text(unit(lane,revision))
        run(['systemctl','daemon-reload'],quiet=True)
        for lane in LANES:
            run(['systemctl','start',unit_name(lane)],quiet=True)
        evidence=verify()
        if '[vps1-song-maker-pc-folders]' in run(['git','log','-1','--format=%B'],cwd=REPO,quiet=True):
            evidence.update(verify_pc_folder_controls())
        atomic_json(journal/'result.json',
                    {'revision':revision,'kind':'update','state':'LIVE_HEALTH_VERIFIED',
                     'evidence':evidence})
        return evidence
    except BaseException:
        # The admission lock stays held through rollback, preventing worker races.
        if touched:
            evidence=restore_update(previous)
        else:
            for lane in LANES:
                if previous['active'][lane]:
                    run(['systemctl','start',unit_name(lane)],quiet=True)
            evidence={'services':'RESTORED_BEFORE_CUTOVER'}
        atomic_json(journal/'result.json',
                    {'revision':revision,'kind':'update',
                     'state':'ROLLED_BACK_DATA_PRESERVED','evidence':evidence})
        raise
    finally:
        if admission_lock is not None:
            admission_lock.close()


def install(revision):
    check_host(revision)
    require(hashlib.sha256(Path('/usr/local/sbin/vps1-production-deploy').read_bytes()).digest()==
            hashlib.sha256((REPO/'production/remote_deploy.py').read_bytes()).digest(),
            'HOST_HELPER_UPGRADE_REQUIRED; do not route Song Maker through another app')
    require(shutil.which('ffmpeg') and shutil.which('ffprobe') and shutil.which('rclone'),'existing media tools required')
    require(SCENE_CURRENT.is_symlink() and SITE.is_file() and not SITE.is_symlink(),'existing Scene ownership unavailable')
    if CURRENT.is_symlink():
        return update_existing(revision)
    with socket.socket() as sock:sock.bind(('127.0.0.1',PORT))
    require(SITE.read_text().count(MARKER)==1 and '/song-maker' not in SITE.read_text(),'route conflict/drift')
    require(not SNIPPET.exists(),'unowned Song Maker route snippet')
    require(not STATE.exists(),'unowned existing Song Maker state')
    previous={'current':None,'scene':str(SCENE_CURRENT.resolve(strict=True)), 'site':SITE.read_text(),
              'snippet':None,'units':{},'active':{}}
    for lane in LANES:
        path=ROOT/unit_name(lane)
        require(not path.exists(),'unowned existing Song Maker service')
        previous['units'][lane]=None
        previous['active'][lane]=False
    release=stage(revision)
    venv=BASE/'venvs'/APP/revision
    run(['python3','-m','venv',venv],quiet=True)
    run([venv/'bin/pip','install','--no-cache-dir','-r',release/'part01/requirements.txt','httpx==0.28.1'],timeout=600,quiet=True)
    # Full real-media suite in disposable state, never real queues/credentials.
    run([venv/'bin/python','-m','unittest','discover','-s','tests','-p','test_song_maker.py'],cwd=REPO,timeout=180,quiet=True)
    journal=TRANSACTIONS/revision
    journal.mkdir(parents=True,exist_ok=False)
    atomic_json(journal/'previous.json',previous)
    backup_databases(DATA/'scene-builder',journal/'scene-sqlite-backup')
    scene=Path(previous['scene'])
    require(scene.is_relative_to(BASE/'releases/scene-builder'),'Scene pointer outside owned releases')
    overlay=BASE/'releases/scene-builder'/(revision+'-song-maker-shell')
    require(not overlay.exists(),'immutable Scene overlay already exists')
    verify_shared_shell(
        (scene/'part01/static/vps1_native_site_shell.js').read_bytes(),
        (REPO/'apps/scene-builder/part01/static/vps1_native_site_shell.js').read_bytes())
    shutil.copytree(scene,overlay,symlinks=True)
    web=overlay/'part01/app.py'
    require(web.is_file() and not web.is_symlink(),'Scene app ownership')
    web.write_text(scene_allowlist(web.read_text()))
    shared=overlay/'part01/static/vps1_native_site_shell.js'
    require(shared.is_file() and not shared.is_symlink(),'shared shell ownership')
    shared.write_bytes((REPO/'apps/scene-builder/part01/static/vps1_native_site_shell.js').read_bytes())
    subprocess.run([venv/'bin/python','-m','py_compile',web],check=True)
    # Source manifest records exact changed bytes while all other installed code stays copied.
    manifest=overlay/'_scene_candidate_manifest.json'
    require(manifest.is_file() and not manifest.is_symlink(),'Scene candidate manifest missing/linked')
    if manifest.exists():
        data=json.loads(manifest.read_text())
        for file in (web,shared):data['files'][str(file.relative_to(overlay))]=hashlib.sha256(file.read_bytes()).hexdigest()
        data.update(revision=revision,overlay='song-maker-shell')
        atomic_json(manifest,data,0o644)
    STATE.mkdir(parents=True,mode=0o700);os.chown(STATE,1000,1000)
    env=CONFIG/'production-song-maker.env'
    require(not env.exists(),'unowned Song Maker environment')
    env.write_text(f'SONG_MAKER_DATA={STATE}\nRCLONE_CONFIG=/home/ubuntu/.config/masterctl/rclone.conf\n')
    env.chmod(0o600)
    try:
        atomic_link(CURRENT,release)
        for lane in LANES:(ROOT/unit_name(lane)).write_text(unit(lane,revision))
        run(['systemctl','daemon-reload'],quiet=True)
        for lane in LANES:run(['systemctl','enable','--now',unit_name(lane)],quiet=True)
        atomic_link(SCENE_CURRENT,overlay)
        run(['systemctl','restart','vps1-scene-candidate.service'],quiet=True)
        from production.scene_candidate_release import _health
        _health()
        SNIPPET.write_text(route_config())
        SITE.write_text(previous['site'].replace(MARKER,INCLUDE+MARKER,1))
        run(['nginx','-t'],quiet=True)
        run(['systemctl','reload','nginx'],quiet=True)
        evidence=verify()
        atomic_json(journal/'result.json',{'revision':revision,'evidence':evidence,'state':'LIVE_HEALTH_VERIFIED'})
    except BaseException:
        restore(previous)
        # Keep Song Maker state for diagnosis and recovery; never delete data.
        atomic_json(journal/'result.json',{'revision':revision,'state':'ROLLED_BACK_DATA_PRESERVED'})
        raise


def main(operation,revision):
    with deployment_lock():
        if operation=='install':install(revision)
        elif operation=='rollback':
            path=TRANSACTIONS/revision/'previous.json'
            require(path.is_file(),'rollback journal absent')
            previous=json.loads(path.read_text())
            if previous.get('kind') == 'update':
                require(CURRENT.is_symlink() and CURRENT.resolve().name == revision,
                        'Song Maker revision drift blocks update rollback')
                for lane in LANES:
                    require((ROOT/unit_name(lane)).read_text() == unit(lane,revision),
                            'Song Maker service drift blocks rollback')
                submission=STATE/'state/submission.lock'
                require(submission.is_file() and not submission.is_symlink(),
                        'Song Maker submission lock missing')
                try:
                    run(['systemctl','stop',unit_name('sync')],quiet=True)
                    with submission.open('a') as admission:
                        fcntl.flock(admission, fcntl.LOCK_EX|fcntl.LOCK_NB)
                        require_idle_jobs(STATE/'state/jobs.sqlite3')
                        evidence=restore_update(previous)
                        atomic_json(TRANSACTIONS/revision/'result.json',
                                    {'revision':revision,'kind':'update',
                                     'state':'MANUAL_ROLLBACK_DATA_PRESERVED',
                                     'evidence':evidence})
                except BaseException:
                    run(['systemctl','start',unit_name('sync')],quiet=True)
                    raise
                return
            require(SCENE_CURRENT.resolve().name==revision+'-song-maker-shell','Scene drift blocks rollback')
            require(SITE.read_text()==previous['site'].replace(MARKER,INCLUDE+MARKER,1),'Nginx drift blocks rollback')
            restore(previous)
        else:raise ValueError('Unsupported Song Maker release operation')

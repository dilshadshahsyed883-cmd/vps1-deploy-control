"""Video Merger backend + isolated dependencies; preserve active UI and state."""
import hashlib
import json
import os
import stat
from pathlib import Path
import re
import shutil
import urllib.request
import zipfile
from production.runtime import BASE, DATA, REPO, atomic_json, atomic_link, event, http_check, run
from production.video_template_release import idle, require


SYSTEMD=Path('/etc/systemd/system')
DENO_URL='https://github.com/denoland/deno/releases/download/v2.9.7/deno-x86_64-unknown-linux-gnu.zip'
DENO_SHA256='c6527f24f4b16031d3ae4fa9f658d5f11534c8d84ce7dc8502420280919c3490'

def install_js(venv):
    archive=venv/'deno.zip'
    with urllib.request.urlopen(DENO_URL,timeout=120) as response:
        payload=response.read(100_000_001)
    require(len(payload)<=100_000_000 and hashlib.sha256(payload).hexdigest()==DENO_SHA256,'Deno artifact digest mismatch')
    archive.write_bytes(payload)
    with zipfile.ZipFile(archive) as bundle:
        binary=venv/'bin/deno'
        binary.write_bytes(bundle.read('deno'))
        binary.chmod(0o755)
    archive.unlink()
    run([binary,'--version'],quiet=True)

def live_paths(default_state):
    pid=run(['systemctl','show','vps1-production-video-merger.service','-p','MainPID','--value'],quiet=True).strip()
    require(pid.isdigit() and int(pid)>1,'Video Merger process missing')
    values={entry.split(b'=',1)[0].decode():entry.split(b'=',1)[1].decode()
            for entry in (Path('/proc')/pid/'environ').read_bytes().split(b'\0') if b'=' in entry}
    state=Path(values.get('VIDEO_MERGER_ROOT',str(default_state)))
    require(state.is_relative_to(DATA/'video-merger'),'unowned data path')
    config=Path(values.get('VIDEO_MERGER_PROXY_CONFIG_DIR','/home/ubuntu/.config/video-merger'))/'proxy_profiles.json'
    return state,config


def planned_proxy_activation(config):
    """Enable only an already selected, credentialed Video Merger profile."""
    require(config.is_file() and not config.is_symlink(), 'untrusted saved proxy settings')
    st=config.stat(follow_symlinks=False)
    require(stat.S_ISREG(st.st_mode) and st.st_size <= 65536
            and st.st_mode & 0o077 == 0, 'private Video Merger proxy config invalid')
    before=config.read_bytes()
    try:
        obj=json.loads(before)
    except (UnicodeDecodeError, ValueError):
        raise RuntimeError('invalid saved Video Merger proxy config') from None
    require(isinstance(obj,dict), 'invalid proxy config data')
    if obj.get('enabled') is True: return None
    require(obj.get('enabled') is False, 'ambiguous proxy enabled value')
    profiles=obj.get('profiles')
    require(isinstance(profiles,list), 'proxy profiles missing')
    selected=next((p for p in profiles if isinstance(p,dict)
                   and p.get('id') == obj.get('active_id')),None)
    require(selected is not None, 'no active saved Video Merger proxy')
    require(bool(selected.get('host')) and bool(selected.get('username'))
            and bool(selected.get('password')), 'selected saved proxy credentials missing')
    obj['enabled']=True
    return {'before':before,'after':(json.dumps(obj,indent=2)+'\\n').encode('utf-8'),
            'path':config,'uid':st.st_uid,'gid':st.st_gid}


def swap_proxy(plan, data):
    path=plan['path']
    staging=path.with_name('.'+path.name+'.vm-restore-'+str(os.getpid()))
    fd=os.open(staging,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        os.fchown(fd,plan['uid'],plan['gid'])
        with os.fdopen(fd,'wb') as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(staging,path)
    finally:
        staging.unlink(missing_ok=True)


def install(revision):
    require(re.fullmatch('[a-f0-9]{40}',revision),'invalid revision')
    app='video-merger'
    unit_name='vps1-production-video-merger.service'
    unit=SYSTEMD/unit_name
    previous_unit=unit.read_text()
    current=BASE/'current'/app
    previous=current.resolve(strict=True)
    require(current.is_symlink() and previous.is_relative_to(BASE/'releases'/app),'unowned release')
    idle(app)
    http_check(app)
    match=re.search(r'^ExecStart=(\S+/bin/python)\s+app.py\s*$',previous_unit,re.M)
    require(match is not None,'unexpected Video Merger service command')
    old_python=Path(match.group(1))
    require(old_python.is_file() and old_python.is_relative_to(BASE/'venvs'/app),'unowned Python')
    event(app=app,preflight='PASS',service_python=str(old_python),legacy_path_used=False)
    venv=BASE/'venvs'/app/(revision+'-imports')
    target=BASE/'releases'/app/(revision+'-imports')
    require(not venv.exists() and not target.exists(),'immutable candidate already exists')
    run(['python3','-m','venv',venv],quiet=True)
    python=venv/'bin/python'
    run([python,'-m','pip','install','--disable-pip-version-check','--no-cache-dir',
         'yt-dlp[default]','gdown==5.2.0','requests==2.32.3'],timeout=900,quiet=True)
    run([python,'-m','yt_dlp','--version'],quiet=True)
    run([python,'-m','gdown','--version'],quiet=True)
    install_js(venv)
    # Source is overlaid on the actual active release, retaining parallel UI work.
    shutil.copytree(previous,target,symlinks=True)
    name='part01/app.py'
    require(not (target/name).is_symlink(),'linked app source')
    source=(REPO/'apps'/app/name).read_bytes()
    compile(source,name,'exec')
    (target/name).write_bytes(source)
    manifest=json.loads((target/'_production_manifest.json').read_text())
    require(manifest.get('app')==app,'wrong manifest owner')
    manifest.update(revision=revision,import_overlay=str(previous))
    manifest['files'][name]=hashlib.sha256(source).hexdigest()
    atomic_json(target/'_production_manifest.json',manifest,0o644)
    state,proxy_config=live_paths(DATA/app/'video-merger')
    activation=planned_proxy_activation(proxy_config)
    # Both real-source downloads happen in a temporary directory. Existing Drive
    # files and SQLite records are only read. Fail before any live mutation.
    run(['/usr/sbin/runuser','-u','ubuntu','--',python,'-m','production.video_merger_import_smoke',
         '--source',target/name,'--state',state,'--proxy-config',proxy_config],cwd=REPO,timeout=1800,quiet=False)
    if activation:
        require(proxy_config.read_bytes()==activation['before'],
                'saved proxy changed during read-only candidate; refuse switch')
    journal=BASE/'transactions/video-merger-imports'/revision
    journal.mkdir(parents=True,exist_ok=False)
    atomic_json(journal/'previous.json',{'current':str(previous),'unit':previous_unit})
    new_unit=previous_unit.replace(str(old_python),str(python))
    new_unit=new_unit.replace(str(old_python.parent)+':',str(python.parent)+':')
    idle(app)
    changed=False
    proxy_changed=False
    try:
        idle(app)
        if activation:
            require(proxy_config.read_bytes()==activation['before'],
                    'saved proxy changed after candidate; no activation')
            (journal/'proxy-before.json').write_bytes(activation['before'])
            (journal/'proxy-before.json').chmod(0o600)
            # Protected host-wide deploy lock and idle fence are held; do not
            # change any profile data or unrelated apps.
            swap_proxy(activation,activation['after'])
            proxy_changed=True
            event(app=app,proxy_activation='SAVED_PROFILE_CANARY_PASS_ENABLED',secrets_logged=False)
        changed=True
        atomic_link(current,target)
        unit.write_text(new_unit)
        run(['systemctl','daemon-reload'],quiet=True)
        run(['systemctl','restart',unit_name],timeout=90,quiet=True)
        http_check(app)
        from production.route_uploader_scene_https import https_get, scene_cookie
        code,kind,page=https_get('/video-merger/',scene_cookie())
        require(code==200 and 'html' in kind.lower(),'authenticated UI readback failed')
        atomic_json(journal/'result.json',{'status':'ACTIVE','youtube':'PASS','drive':'PASS','data':'PRESERVED'})
        event(app=app,result='PASS_VIDEO_MERGER_IMPORTS',revision=revision,data='PRESERVED',proxy_activation='ENABLED' if proxy_changed else 'UNCHANGED',rollback=str(journal))
    except BaseException:
        if changed:
            atomic_link(current,previous)
            unit.write_text(previous_unit)
            run(['systemctl','daemon-reload'],quiet=True)
        if proxy_changed:
            swap_proxy(activation,activation['before'])
            event(app=app,proxy_activation='ROLLED_BACK',secrets_logged=False)
        if changed:
            run(['systemctl','restart',unit_name],timeout=90,quiet=True)
            http_check(app)
            event(app=app,rollback='PASS_VIDEO_MERGER_IMPORTS')
        raise

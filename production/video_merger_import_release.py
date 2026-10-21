"""Video Merger backend + isolated dependencies; preserve active UI and state."""
import hashlib
import json
from pathlib import Path
import re
import shutil
from production.runtime import BASE, DATA, REPO, atomic_json, atomic_link, event, http_check, run
from production.video_template_release import idle, require


SYSTEMD=Path('/etc/systemd/system')

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
    state=DATA/app/'video-merger'
    # Both real-source downloads happen in a temporary directory. Existing Drive
    # files and SQLite records are only read. Fail before any live mutation.
    run(['runuser','-u','ubuntu','--',python,'-m','production.video_merger_import_smoke',
         '--source',target/name,'--state',state],cwd=REPO,timeout=1800,quiet=False)
    journal=BASE/'transactions/video-merger-imports'/revision
    journal.mkdir(parents=True,exist_ok=False)
    atomic_json(journal/'previous.json',{'current':str(previous),'unit':previous_unit})
    new_unit=previous_unit.replace(str(old_python),str(python))
    new_unit=new_unit.replace(str(old_python.parent)+':',str(python.parent)+':')
    idle(app)
    changed=False
    try:
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
        event(app=app,result='PASS_VIDEO_MERGER_IMPORTS',revision=revision,data='PRESERVED',rollback=str(journal))
    except BaseException:
        if changed:
            atomic_link(current,previous)
            unit.write_text(previous_unit)
            run(['systemctl','daemon-reload'],quiet=True)
            run(['systemctl','restart',unit_name],timeout=90,quiet=True)
            http_check(app)
            event(app=app,rollback='PASS_VIDEO_MERGER_IMPORTS')
        raise

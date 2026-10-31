"""Video Merger backend + isolated dependencies; preserve active UI and state."""
import hashlib
import json
import os
import stat
from pathlib import Path
import re
import shutil
import sqlite3
import urllib.request
import zipfile
from production.runtime import BASE, DATA, REPO, atomic_json, atomic_link, event, http_check, run
from production.video_template_release import require, TERMINAL


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
    return {'before':before,'after':(json.dumps(obj,indent=2)+'\n').encode('utf-8'),
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



def idle_database(state):
    """Read-only strict idle check without serializing the entire Library."""
    path=Path(state)/'data/video_merger.db'
    require(path.is_file() and not path.is_symlink(), 'Video Merger DB missing')
    with sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True,timeout=6) as db:
        db.execute('PRAGMA query_only=ON')
        allowed=sorted(TERMINAL)
        placeholder=','.join('?' for _ in allowed)
        for table in ('jobs','library_items'):
            fields={row[1] for row in db.execute('PRAGMA table_info('+table+')')}
            require('status' in fields, 'Video Merger '+table+' schema invalid')
            count=db.execute(
                f"SELECT COUNT(*) FROM {table} WHERE lower(coalesce(status,'')) NOT IN ({placeholder})",
                allowed).fetchone()[0]
            require(count==0, 'Video Merger '+table+' has unfinished jobs')
    event(app='video-merger',idle='SQLITE_READ_ONLY_PASS',owner_media_mutated=False)


def overlay_failed_import_retry_ui(live, source):
    """Surgically add Retry support without replacing independent active UI changes."""
    action='''<button class="btn secondary mini" onclick="renameLibrary('${x.id}')"'''
    added='''${x.status==='failed'?`<button class="btn secondary mini" onclick="retryLibrary('${x.id}')" ${x.in_use?'disabled':''}>Retry</button>`:''}'''
    function="function renderProxy(){"
    new_handler='''async function retryLibrary(id){let x=state.library.find(v=>v.id===id);if(!x||x.status!=='failed')return;if(!confirm(`Retry failed Library import “${x.name}” from the saved original URL?`))return;try{await api(`/api/library/${id}/retry`,{method:'POST',body:'{}'});toast('Retry started using saved source');await refresh()}catch(e){toast(e.message)}}\n'''
    require(source.count(added+action)==1 and source.count(new_handler+function)==1,
            'retry source UI contract changed')
    if added+action not in live:
        require(live.count(action)==1,'active Library action UI changed; refuse overwrite')
        live=live.replace(action,added+action,1)
    if new_handler+function not in live:
        require(live.count(function)==1,'active Library script changed; refuse overwrite')
        live=live.replace(function,new_handler+function,1)
    return live



def overlay_queue_layout_ui(live, source, baseline):
    """Apply only reviewed Merge/Queue changes; preserve live Library, Proxy and shell."""
    marker = '/* VIDEO_MERGER_TWO_COLUMN_QUEUE_V2'
    require(source.count(marker) == 1, 'Queue UI marker missing or duplicated')
    require(baseline.count(marker) == 0, 'Queue baseline already contains patch')
    # Prior verified releases may already have this Queue UI. In that case,
    # preserve it and apply only the new owner-folder callbacks below.
    if live.count(marker)==1:
        require(live.count('id="tab-queue"')==1 and live.count('id="queue"')==1,
                'Existing Video Merger Queue UI incomplete')
        return live
    require(live.count(marker)==0, 'Ambiguous Queue patch state')

    def block(page, first, last):
        require(page.count(first) == 1 and page.count(last) == 1,
                'Video Merger layout anchor drift')
        start = page.index(first)
        end = page.index(last, start)
        require(end > start, 'Video Merger layout anchor order changed')
        return page[start:end]

    # No changes to Library, Proxy, native shell or identity.
    start = '  <div class="tabs"'
    end = '  <div id="tab-library"'
    old_panel = block(baseline, start, end)
    new_panel = block(source, start, end)
    require(live.count(old_panel) == 1, 'Active Merge panel drift; refusing overwrite')
    for control in ('fname', 'librarySelect', 'urlVideos', 'aspect', 'quality',
                    'resolution', 'fit', 'uploadDrive', 'addCopyrightFolder',
                    'folder', 'queueMergeButton'):
        token = 'id="' + control + '"'
        require(old_panel.count(token) == 1 and new_panel.count(token) == 1,
                'Merge control drift: ' + control)
    require('id="tab-queue"' in new_panel and 'id="queue"' in new_panel,
            'Queue panel missing')
    live = live.replace(old_panel, new_panel, 1)

    style_end = '</style>'
    require(source.count(style_end) == 1 and live.count(style_end) == 1,
            'Merger style boundary drift')
    css = block(source, marker, style_end)
    live = live.replace(style_end, css + style_end, 1)

    def js_line(page, name):
        lines = [line for line in page.splitlines() if line.startswith(name)]
        require(len(lines) == 1, 'Merger JS anchor drift: ' + name)
        return lines[0]

    for name in ('function showTab(tab)', 'async function queueMerge()',
                 'function activeStatus(s)', 'function renderQueue()'):
        old = js_line(baseline, name)
        new = js_line(source, name)
        require(old != new, 'Expected Queue JS change missing: ' + name)
        require(live.count(old) == 1, 'Active Queue JS drift: ' + name)
        live = live.replace(old, new, 1)

    anchor = 'async function del(id){'
    callback_start = 'async function cancelJob(id){'
    require(source.count(callback_start) == 1 and source.count(anchor) == 1,
            'Queue callbacks missing')
    require(live.count(anchor) == 1 and callback_start not in live,
            'Active Queue actions drift')
    callbacks = block(source, callback_start, anchor)
    require('async function retryJob(id)' in callbacks, 'Retry callback missing')
    live = live.replace(anchor, callbacks + anchor, 1)
    require(live.count(marker) == 1 and live.count('id="tab-queue"') == 1,
            'Queue UI verification failed')
    return live


def overlay_owner_handover_ui(live, source):
    """Apply only two reviewed Drive folder client callbacks to the live UI.

    The guarded release retains the active sidebar, unrelated tabs, and
    deployment overlay. Refuse ambiguous or missing anchors.
    """
    for prefix in ('function renderFolders()', 'async function addFolder()'):
        candidates=[line for line in source.splitlines() if line.startswith(prefix)]
        existing=[line for line in live.splitlines() if line.startswith(prefix)]
        require(len(candidates)==1 and len(existing)==1,
                'Video Merger owner folder UI anchor drift: '+prefix)
        if candidates[0]!=existing[0]:
            live=live.replace(existing[0],candidates[0],1)
    return live


def install(revision):
    require(re.fullmatch('[a-f0-9]{40}',revision),'invalid revision')
    app='video-merger'
    unit_name='vps1-production-video-merger.service'
    unit=SYSTEMD/unit_name
    previous_unit=unit.read_text()
    current=BASE/'current'/app
    previous=current.resolve(strict=True)
    require(current.is_symlink() and previous.is_relative_to(BASE/'releases'/app),'unowned release')
    state,proxy_config=live_paths(DATA/app/'video-merger')
    http_check(app)
    idle_database(state)
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
         'yt-dlp[default]==2026.8.19','gdown==5.2.0','requests==2.32.3'],timeout=900,quiet=True)
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
    # Patch only the two canonical retry anchors; preserve current active HTML/CSS/shell.
    template='part01/templates/index.html'
    live_template=target/template
    source_html=(REPO/'apps'/app/template).read_text(encoding='utf-8')
    patched=overlay_failed_import_retry_ui(live_template.read_text(encoding='utf-8'),source_html)
    if 'VIDEO_MERGER_TWO_COLUMN_QUEUE_V2' in source_html:
        # The immutable deployment checkout is shallow and has no revision^.
        # Use the audited pre-Queue panel anchor stored in this exact Git SHA.
        baseline_path=REPO/'production/video_merger_pre_queue_baseline.txt'
        require(baseline_path.is_file() and not baseline_path.is_symlink(),
                'reviewed pre-Queue UI baseline unavailable')
        baseline=baseline_path.read_text(encoding='utf-8')
        patched=overlay_queue_layout_ui(patched,source_html,baseline)
    patched=overlay_owner_handover_ui(patched,source_html)
    live_template.write_text(patched,encoding='utf-8')
    manifest=json.loads((target/'_production_manifest.json').read_text())
    require(manifest.get('app')==app,'wrong manifest owner')
    manifest.update(revision=revision,import_overlay=str(previous))
    manifest['files'][name]=hashlib.sha256(source).hexdigest()
    manifest['files'][template]=hashlib.sha256(patched.encode('utf-8')).hexdigest()
    atomic_json(target/'_production_manifest.json',manifest,0o644)
    # Handover must not silently activate or edit shared administrator proxy credentials.
    activation=None
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
    changed=False
    proxy_changed=False
    try:
        http_check(app)
        idle_database(state)
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
        if 'VIDEO_MERGER_TWO_COLUMN_QUEUE_V2' in source_html:
            require(b'VIDEO_MERGER_TWO_COLUMN_QUEUE_V2' in page and
                    b'id="tab-queue"' in page and b'class="vm-merge-layout"' in page,
                    'authenticated Queue/two-column UI readback failed')
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

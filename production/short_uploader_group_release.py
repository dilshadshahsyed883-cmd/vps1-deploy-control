"""Short Uploader-only immutable overlay, entered under the existing host lock."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from urllib import request

from production.runtime import BASE, DATA, REPO, atomic_json, backup_databases

FILES=("lum_engine/auto_engine_v6.py", "lum_engine/standalone_app.py",
       "lum_engine/auto_engine_v6_worker.py", "desktop_core/modules/db/queue_repository.py",
       "templates/app.html", "templates/base.html", "static/auto_engine_v6.js",
       "static/local_upload_manager.css", "static/song_uploader_kokoro_ui.css")
UNITS=tuple(f"vps1-production-lum-shortstudio-{kind}.service" for kind in ("scheduler","worker","web"))
STATE=DATA/'local-upload-manager/lum-shortstudio'
UNIT_DIR=Path('/etc/systemd/system')
NETWORK_ONLY_FILES=('lum_engine/auto_engine_v6_worker.py', 'lum_engine/auto_engine_v6_scheduler.py',
                    'desktop_core/modules/db/queue_repository.py')


def require(condition, detail):
    if not condition: raise RuntimeError('SHORT_GROUP_RELEASE_BLOCKED: '+detail)


def command(*args):
    return subprocess.run(args,check=True,capture_output=True,text=True,timeout=70).stdout.strip()


def health():
    opener=request.build_opener(request.ProxyHandler({}))
    with opener.open('http://127.0.0.1:8019/short-uploader/health',timeout=5) as response:
        result=json.load(response)
    require(result.get('ok') is True and result.get('engine_instance')=='shortstudio', 'Short Uploader identity mismatch')
    return result


def idle(*, allow_waiting_network=False):
    require(health().get('active_project_id') in (None,'',0), 'web upload in progress')
    db=STATE/'core/data/uploader.db'
    require(db.is_file() and not db.is_symlink(),'Short Uploader database unavailable')
    with sqlite3.connect(db.as_uri()+'?mode=ro',uri=True,timeout=3) as con:
        statuses="'CLAIMED','UPLOADING','THUMBNAIL_UPLOADING','PROCESSING','IN_PROGRESS'"
        if not allow_waiting_network:
            statuses+=",'WAITING_NETWORK'"
        busy=con.execute(f"SELECT 1 FROM videos WHERE UPPER(status) IN ({statuses}) LIMIT 1").fetchone()
        require(not busy,'upload in progress')
        if allow_waiting_network:
            # Reuse runtime's strict proof; do not interrupt a claimed
            # network wait or an ambiguous/resumable media upload.
            from production.runtime import lum_parked_network_idle
            lum_parked_network_idle(con)


def verify_live(*, ui_only=False):
    from production.route_uploader_scene_https import https_get, scene_cookie
    for _ in range(40):
        try:
            health();break
        except (OSError, ValueError, RuntimeError):time.sleep(.5)
    else:raise RuntimeError('Short Uploader health timeout')
    cookie=scene_cookie()
    require(cookie,'authenticated verification session unavailable')
    status,_,body=https_get('/short-uploader/app?tab=auto_engine&new_profile=1',cookie)
    require(status==200,'authenticated Short Uploader GET failed')
    html=body.decode('utf-8')
    require('data-short-group-summary' in html and '<th>Group Name</th>' in html and
            '<th>Channels</th>' in html and '<th>Schedule (PKT)</th>' in html,
            'live automation group summary is missing')
    if not ui_only:
        require('data-short-schedule-slots' in html,
                'Short Studio individual PKT time selectors missing')
        require('<h2>Thumbnail Library' not in html,
                'Short Uploader should not show thumbnail rotation library')
        require('class="lum-ae-saved-times"' in html or 'No saved group profiles yet.' in html,
                'Short Uploader Automation saved PKT times are missing')
        require('data-short-inline-schedule' not in html,
                'Short Uploader Automation still contains editable schedule fields')
        require('data-short-profile-action="start"' in html or 'No saved group profiles yet.' in html,
                'Short Uploader individual Start form is missing from live HTML')
        css_status,_,css_body=https_get('/short-uploader/static/local_upload_manager.css',cookie)
        require(css_status==200 and b'form[data-short-profile-action]' in css_body and
                b'display:inline-flex!important' in css_body,
                'Live CSS still hides the profile Start form')

    start=html.find('<form id="lum-ae-profile-form"')
    require(start>=0,'profile form absent')
    form=html[start:html.index('</form>',start)]
    require('short-profile-metadata-source' in form and 'name="channel_group_id"' in form and
            'name="short_vps_folder"' in form and 'name="manual_title"' in form and
            'type="checkbox" name="schedule_enabled"' in form and
            'name="channel_db_id"' not in form and 'Profile Type' not in form,
            'live group profile form does not match release')
    if ui_only:
        require('Group profiles, sources and upload queue.' not in html and
                'independent uploader workspace' not in html and
                '<h2>Auto Engine</h2>' in html,
                'Short Uploader duplicate introduction persists')
    require(https_get('/short-uploader/app')[0]==401,'anonymous route protection failed')
    print('SHORT_UPLOADER_LIVE=PASS AUTH=PASS GROUP_FORM=PASS GROUP_SUMMARY=PASS NO_UPLOAD_OR_ARM_ACTIONS',flush=True)


def install(revision, *, ui_only=False, network_only=False):
    require(not (ui_only and network_only), 'release modes are exclusive')
    require(re.fullmatch('[a-f0-9]{40}',revision),'invalid revision')
    require(shutil.disk_usage('/').free>=5_000_000_000,'disk reserve')
    idle(allow_waiting_network=True) if network_only else idle()
    previous={}
    old_dirs=set()
    for name in UNITS:
        path=UNIT_DIR/name
        require(path.is_file() and not path.is_symlink(),'owned unit unavailable')
        text=path.read_text()
        matches=re.findall(r'^WorkingDirectory=(.+)$',text,re.M)
        require(len(matches)==1,'working directory drift')
        old=Path(matches[0]).resolve(strict=True)
        require(old.is_relative_to(BASE/'releases/local-upload-manager') and old.name=='part01','source ownership drift')
        old_dirs.add(old)
        active=subprocess.run(['systemctl','is-active','--quiet',name],capture_output=True,timeout=10).returncode==0
        previous[name]={'text':text,'active':active}
    require(len(old_dirs)==1,'Short Uploader units use different releases')
    suffix=('-short-network-fairness' if network_only else '-short-ui-intro' if ui_only else '-short-group-metadata')
    target=BASE/'releases/local-upload-manager'/(revision+suffix)
    journal=BASE/'transactions'/('short-uploader-network-fairness' if network_only else 'short-uploader-ui-intro' if ui_only else 'short-uploader-group-metadata')/revision
    require(not target.exists() and not target.is_symlink() and not journal.exists(),'immutable release already exists')
    shutil.copytree(next(iter(old_dirs)).parent,target,symlinks=True)
    hashes={}
    for relative in (NETWORK_ONLY_FILES if network_only else ("templates/app.html",) if ui_only else FILES):
        src=REPO/'apps/local-upload-manager/part01'/relative
        dst=target/'part01'/relative
        require(src.is_file() and not src.is_symlink() and dst.is_file() and not dst.is_symlink(),'source file drift')
        shutil.copy2(src,dst)
        hashes[relative]=hashlib.sha256(dst.read_bytes()).hexdigest()
    journal.mkdir(parents=True,mode=0o700)
    atomic_json(journal/'previous.json',previous)
    changed=False
    try:
        idle(allow_waiting_network=True) if network_only else idle()
        # Stop only the isolated Short Uploader; keep every other engine and app running.
        changed=True
        for name in UNITS:
            command('systemctl','stop',name)
        backups=backup_databases(STATE,journal/'sqlite-backup')
        for name,item in previous.items():
            text=re.sub(r'^WorkingDirectory=.+$',f'WorkingDirectory={target}/part01',item['text'],flags=re.M)
            (UNIT_DIR/name).write_text(text)
        command('systemctl','daemon-reload')
        for name in reversed(UNITS):
            if previous[name]['active']:command('systemctl','start',name)
        if network_only:
            require(health().get('engine_instance')=='shortstudio','Short Uploader health not restored')
            print('SHORT_NETWORK_RELEASE=PASS HEALTH=PASS OTHER_UPLOADERS=UNTOUCHED',flush=True)
        elif ui_only:verify_live(ui_only=True)
        else:verify_live()
        atomic_json(journal/'state.json',dict(status='ACTIVE',revision=revision,source=str(target),files=hashes,
                                           sqlite_backups=len(backups),scheduler='PRESERVED',auto_script='UNTOUCHED'))
        print('SHORT_GROUP_RELEASE=PASS OTHER_ENGINES=UNTOUCHED ROLLBACK=READY',flush=True)
    except BaseException:
        if changed:
            for name in UNITS:command('systemctl','stop',name)
            for name,item in previous.items():(UNIT_DIR/name).write_text(item['text'])
            command('systemctl','daemon-reload')
            for name in reversed(UNITS):
                if previous[name]['active']:command('systemctl','start',name)
            for _ in range(40):
                try:health();break
                except (OSError, ValueError, RuntimeError):time.sleep(.5)
            else:raise RuntimeError('Short Uploader rollback health failed')
            atomic_json(journal/'state.json',dict(status='ROLLED_BACK',revision=revision))
            print('SHORT_GROUP_ROLLBACK=PASS DATA=PRESERVED',flush=True)
        raise


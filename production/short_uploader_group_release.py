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
       "lum_engine/studio_tenants.py", "lum_engine/project_store.py",
       "lum_engine/channels_web.py",
       "lum_engine/auto_engine_v6_worker.py", "desktop_core/modules/db/queue_repository.py",
       "templates/app.html", "templates/base.html", "static/auto_engine_v6.js",
       "static/local_upload_manager.css", "static/song_uploader_kokoro_ui.css")
NEW_FILES=frozenset({"lum_engine/studio_tenants.py"})
UNITS=tuple(f"vps1-production-lum-shortstudio-{kind}.service" for kind in ("scheduler","worker","web"))
STATE=DATA/'local-upload-manager/lum-shortstudio'
UNIT_DIR=Path('/etc/systemd/system')
# Co-deploy the already merged Short Uploader UI (#493) and queue actions
# (#497) alongside network fairness (#496). These are copies into only the
# isolated Short Studio release; no Song/legacy uploader unit is touched.
NETWORK_ONLY_FILES=FILES+('lum_engine/auto_engine_v6_scheduler.py',)


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


def verify_live(*, ui_only=False, assignment_only=False, fixed_modes_only=False, member_controls_only=False, owner_parity_only=False):
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
    if owner_parity_only:
        status,_,body=https_get('/short-uploader/app?tab=channels',cookie)
        require(status==200,'owner parity Channels readback failed')
        channels=body.decode('utf-8')
        require('Accounts &amp; Tokens' in channels and
                'Set / Replace Shared Client' not in channels and
                'Shared OAuth client is managed by an administrator' not in channels and
                'data-lum-auth-section="verify"' in channels,
                'Short Channels role parity or OAuth guard missing')
        status,_,body=https_get('/short-uploader/app?tab=auto_engine',cookie)
        require(status==200,'owner parity Auto Engine readback failed')
        automation=body.decode('utf-8')
        for marker in ('Retry Failed','Resume Paused','Clear Done','Remove Selected',
                       'Profile Schedules','Start All Profiles','Sync All Channels',
                       'Queue Preflight'):
            require(marker in automation,'Short owner control missing: '+marker)
        require('>Start / Resume Engine</button>' not in automation and
                '>Stop After Current</button>' not in automation and
                'Set / Replace Shared Client' not in automation,
                'global Short administrator controls must not appear in user workspace')
        require(https_get('/short-uploader/app')[0] in (302,303,401,403),
                'Short owner-parity anonymous authentication regressed')
        print('SHORT_OWNER_PARITY_LIVE=PASS ADMIN_UI=OWNER_SCOPED '
              'MEMBER_TWO_USER_READBACK=REQUIRED NO_UPLOAD_STARTED',flush=True)
        return
    if member_controls_only:
        status,_,body=https_get('/short-uploader/app?tab=auto_engine',cookie)
        require(status==200,'Short Automation page unavailable')
        page=body.decode('utf-8')
        for marker in ('Profile Schedules', 'Preview Next Jobs', 'Start All Profiles',
                       'Sync All Channels', 'Queue Preflight',
                       'data-ae-open-schedules', 'data-ae-preview-all',
                       'data-ae-jump-start-all', 'data-ae-queue-preflight',
                       '/actions/auto-v6/start-all', '/actions/auto-v6/profile-schedules/save'):
            require(marker in page,'Short Automation member control absent: '+marker)
        require(https_get('/short-uploader/app')[0] in (302,303,401,403),
                'Short Uploader anonymous authentication regressed')
        print('SHORT_MEMBER_AUTOMATION_LIVE=PASS ACTIONS=VISIBLE '
              'OTHER_UPLOADERS=UNTOUCHED',flush=True)
        return
    if fixed_modes_only:
        def checked_tab(tab):
            status, _, body = https_get('/short-uploader/app?tab=' + tab, cookie)
            require(status == 200, 'Short Uploader ' + tab + ' route failed')
            return body.decode('utf-8')
        dashboard = checked_tab('dashboard')
        channels = checked_tab('channels')
        sheets = checked_tab('sheets')
        require('Your YouTube channels &amp; groups' not in channels and
                '/actions/studio-tenant/channel-create' not in channels and
                '/actions/studio-tenant/group-create' not in channels,
                'manual channel/group form still rendered')
        require('Future modes' not in dashboard and 'Uploader modes' not in dashboard and
                'Save modes' not in dashboard and
                '/actions/settings' not in dashboard,
                'obsolete Short Uploader mode controls still rendered')
        require('Sync Enabled' in sheets and 'Sync Mode OFF' not in sheets,
                'Sheets Sync is not enabled')
        require('Auto Engine' in html and
                https_get('/short-uploader/app')[0] in (302, 303, 401, 403),
                'Short Uploader engine tab or anonymous auth regressed')
        print('SHORT_FIXED_MODES_LIVE=PASS CHANNEL_FORM=ABSENT MODES=ON '
              'SCHEDULE_AND_UPLOAD_ACTIONS=UNTOUCHED', flush=True)
        return
    if assignment_only:
        require('Assign Studio resources to a user' not in html and
                'User workspace ownership' not in html and
                '/actions/studio-tenant/grant' not in html,
                'obsolete owner assignment panel still visible')
        require(https_get('/short-uploader/app')[0] in (302, 303, 401, 403),
                'anonymous route protection failed')
        print('SHORT_ASSIGNMENT_UI_LIVE=PASS AUTH=PASS FORM_REMOVED=PASS',flush=True)
        return
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

    if not ui_only:
        require('data-ae-open-schedules>Profile Schedules</button>' in html,
                'Automation Profile Schedules button missing')
        require('>Generate Queue Now</button>' not in html,
                'Removed Short Uploader Generate Queue Now button is still visible')
    start=html.find('<form id="lum-ae-profile-form"')
    require(start>=0,'profile form absent')
    form=html[start:html.index('</form>',start)]
    require('short-profile-metadata-source' in form and 'name="channel_group_id"' in form and
            'name="short_vps_folder"' in form and 'name="manual_title"' in form and
            'type="checkbox" name="schedule_enabled"' in form and
            'name="channel_db_id"' not in form and 'Profile Type' not in form,
            'live group profile form does not match release')
    if not ui_only:
        require('id="short-profile-privacy-status"' in form and
                re.search(r'<option value="public"[^>]*selected',form),
                'New Short Uploader profile does not default to Public')
        verify_final_ui(html, form, cookie, https_get)
    if ui_only:
        require('Group profiles, sources and upload queue.' not in html and
                'independent uploader workspace' not in html and
                '<h2>Auto Engine</h2>' in html,
                'Short Uploader duplicate introduction persists')
    require(https_get('/short-uploader/app')[0]==401,'anonymous route protection failed')
    print('SHORT_UPLOADER_LIVE=PASS AUTH=PASS GROUP_FORM=PASS GROUP_SUMMARY=PASS NO_UPLOAD_OR_ARM_ACTIONS',flush=True)


def verify_final_ui(html, form, cookie, https_get):
    """Read-only acceptance; never submit a profile, arm, sync or upload."""
    require('lum-short-engine-tabbar' in html and 'lum-short-engine-status' in html
            and '<section class="lum-ae-head">' not in html,
            'runtime badges/tab row or removed header regressed')
    require('data-short-profile-grid' in form and
            'name="schedule_times" data-short-schedule-csv value="06:00"' in form,
            'new profile layout or first PKT slot regressed')
    for field, value in (('work_mode','auto'), ('upload_scope','per_channel'),
                         ('after_upload_action','delete'), ('metadata_source','manual')):
        select=re.search(r'<select[^>]*name="'+field+r'"[^>]*>(.*?)</select>',form,re.S)
        require(select and re.search(r'<option value="'+value+r'"[^>]*selected',select[1]),
                'new profile default regressed: '+field)
    for field in ('enabled','schedule_enabled'):
        require(re.search(r'<input[^>]*type="checkbox"[^>]*name="'+field+r'"[^>]*checked',form),
                'new profile default regressed: '+field)
    require(re.search(r'name="daily_upload_count"[^>]*value="1"',form),
            'new profile daily count regressed')
    status,_,body=https_get('/short-uploader/app?tab=sheets',cookie)
    sheets=body.decode('utf-8')
    require(status==200 and '<summary>Sheet format</summary>' in sheets and
            '<h2>Manual Update</h2>' not in sheets and
            'PC-app Sheets engine' not in sheets and
            'lum-short-sheet-header-actions' in sheets and
            'Update All Enabled Sheets' in sheets,
            'compact Sheets Sync readback failed')
    status,_,body=https_get('/short-uploader/app?tab=bulk_import',cookie)
    require(status==200 and 'Bulk Import' not in body.decode('utf-8'),
            'Short Uploader direct Bulk Import page exposed')
    status,_,body=https_get('/short-uploader/static/local_upload_manager.css',cookie)
    require(status==200 and all(marker in body for marker in (
            b'.lum-short-engine-tabbar', b'.lum-short-engine-status',
            b'.lum-short-profile-grid', b'.lum-short-sheet-header-actions')),
            'current compact CSS missing')
    from production.song_uploader_group_summary_release import health as song_health
    require(song_health().get('ok') is True,
            'Song Uploader health readback failed')
    print('SHORT_FINAL_UI=PASS DEFAULTS=PASS SHEETS=PASS BULK_HIDDEN=PASS CSS=PASS SONG_HEALTH=PASS',flush=True)


def install(revision, *, ui_only=False, network_only=False, assignment_only=False, fixed_modes_only=False, member_controls_only=False, owner_parity_only=False):
    require(sum(bool(x) for x in (ui_only, network_only, assignment_only, fixed_modes_only, member_controls_only, owner_parity_only)) <= 1,
            'release modes are exclusive')
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
    suffix=('-short-owner-parity' if owner_parity_only else '-short-member-controls' if member_controls_only else '-short-fixed-modes' if fixed_modes_only else '-short-owner-form' if assignment_only else '-short-network-fairness' if network_only else '-short-ui-intro' if ui_only else '-short-group-metadata')
    target=BASE/'releases/local-upload-manager'/(revision+suffix)
    journal=BASE/'transactions'/('short-uploader-owner-parity' if owner_parity_only else 'short-uploader-member-controls' if member_controls_only else 'short-uploader-fixed-modes' if fixed_modes_only else 'short-uploader-owner-form' if assignment_only else 'short-uploader-network-fairness' if network_only else 'short-uploader-ui-intro' if ui_only else 'short-uploader-group-metadata')/revision
    require(not target.exists() and not target.is_symlink() and not journal.exists(),'immutable release already exists')
    shutil.copytree(next(iter(old_dirs)).parent,target,symlinks=True)
    hashes={}
    for relative in (NETWORK_ONLY_FILES if network_only else
                     ('templates/app.html','lum_engine/standalone_app.py','static/auto_engine_v6.js') if member_controls_only else
                     ('templates/app.html', 'lum_engine/standalone_app.py') if fixed_modes_only else
                     ("templates/app.html",) if (ui_only or assignment_only) else FILES):
        src=REPO/'apps/local-upload-manager/part01'/relative
        dst=target/'part01'/relative
        require(src.is_file() and not src.is_symlink() and not dst.is_symlink() and
                (dst.is_file() or (relative in NEW_FILES and not dst.exists())),
                'source file drift')
        dst.parent.mkdir(parents=True,exist_ok=True)
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
            # Confirm the combined #493/#497/#496 HTML is served over the
            # authenticated route before committing the immutable release.
            verify_live()
            print('SHORT_NETWORK_RELEASE=PASS HEALTH=PASS UI=PASS OTHER_UPLOADERS=UNTOUCHED',flush=True)
        elif assignment_only:verify_live(assignment_only=True)
        elif fixed_modes_only:verify_live(fixed_modes_only=True)
        elif member_controls_only:verify_live(member_controls_only=True)
        elif owner_parity_only:verify_live(owner_parity_only=True)
        elif ui_only:verify_live(ui_only=True)
        else:verify_live()
        atomic_json(journal/'state.json',dict(status='ACTIVE',revision=revision,source=str(target),files=hashes,
                                           sqlite_backups=len(backups),scheduler='PRESERVED',auto_script='UNTOUCHED'))
        print(('SHORT_OWNER_PARITY_RELEASE' if owner_parity_only else 'SHORT_MEMBER_CONTROLS_RELEASE' if member_controls_only else 'SHORT_FIXED_MODES_RELEASE' if fixed_modes_only else 'SHORT_ASSIGNMENT_UI_RELEASE' if assignment_only else 'SHORT_GROUP_RELEASE')
              +'=PASS OTHER_ENGINES=UNTOUCHED ROLLBACK=READY',flush=True)
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


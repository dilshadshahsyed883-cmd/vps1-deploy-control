"""Song Uploader-only immutable overlay, entered under the existing host lock."""
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

from production.runtime import BASE, DATA, REPO, atomic_json, backup_databases, lum_parked_network_idle

FILES=(
    "templates/app.html",
    "templates/base.html",
    "static/local_upload_manager.css",
    "static/song_uploader_kokoro_ui.css",
    "lum_engine/auto_engine_v6.py",
    "lum_engine/auto_engine_v6_scheduler.py",
    "lum_engine/auto_engine_v6_worker.py",
    "lum_engine/standalone_app.py",
)
UNITS=tuple(f"vps1-production-lum-song-{kind}.service" for kind in ("scheduler","worker","web"))
STATE=DATA/'local-upload-manager/lum-song'
UNIT_DIR=Path('/etc/systemd/system')


def require(condition, detail):
    if not condition: raise RuntimeError('SONG_GROUP_SUMMARY_RELEASE_BLOCKED: '+detail)


def command(*args):
    return subprocess.run(args,check=True,capture_output=True,text=True,timeout=70).stdout.strip()


def health():
    opener=request.build_opener(request.ProxyHandler({}))
    with opener.open('http://127.0.0.1:8018/local-upload-manager/modes/song/health',timeout=5) as response:
        result=json.load(response)
    require(result.get('ok') is True and result.get('engine_instance')=='song', 'Song Uploader identity mismatch')
    return result


def idle():
    require(health().get('active_project_id') in (None,'',0), 'web upload in progress')
    db=STATE/'core/data/uploader.db'
    require(db.is_file() and not db.is_symlink(),'Song Uploader database unavailable')
    with sqlite3.connect(db.as_uri()+'?mode=ro',uri=True,timeout=3) as con:
        con.execute("PRAGMA query_only=ON")
        # Only proven pre-media, unclaimed network waits are parked. Never
        # ignore a claimed, transmitting or remotely ambiguous upload.
        busy_states=('CLAIMED','UPLOADING','THUMBNAIL_UPLOADING','PROCESSING','IN_PROGRESS')
        busy=con.execute(
            "SELECT UPPER(status), COUNT(*) FROM videos WHERE UPPER(status) IN ("
            + ",".join("?" for _ in busy_states) + ") GROUP BY UPPER(status)",
            busy_states,
        ).fetchall()
        if busy:
            aggregate=", ".join(f"{state}={count}" for state,count in busy)
            require(False,'upload in progress ('+aggregate+')')
        try:
            parked=lum_parked_network_idle(con)
        except (RuntimeError, ValueError, TypeError, sqlite3.Error) as exc:
            # A non-mutating, aggregate-only diagnostic for safely distinguishing
            # claimed jobs from unbound/manual waits; no IDs or secrets leave VPS.
            try:
                counters=con.execute("""
                    SELECT COUNT(*),
                           SUM(CASE WHEN TRIM(COALESCE(claim_worker_id,''))<>'' THEN 1 ELSE 0 END),
                           SUM(CASE WHEN COALESCE(auto_engine_profile_id,0)=0 THEN 1 ELSE 0 END),
                           SUM(CASE WHEN COALESCE(progress,0)>0 OR COALESCE(upload_bytes_confirmed,0)>0 THEN 1 ELSE 0 END),
                           SUM(CASE WHEN TRIM(COALESCE(youtube_video_id,''))<>'' OR TRIM(COALESCE(resumable_uri,''))<>'' THEN 1 ELSE 0 END)
                    FROM videos WHERE UPPER(status)='WAITING_NETWORK'
                """).fetchone()
                count,claimed,non_auto,progress,remote=(int(v or 0) for v in counters)
                print(
                    f'SONG_IDLE_NETWORK_DIAGNOSTIC total={count} claimed={claimed} non_auto={non_auto} '
                    f'progress_evidence={progress} remote_identity={remote} safe_release=BLOCKED',
                    flush=True
                )
            except (sqlite3.Error, ValueError, TypeError):
                print('SONG_IDLE_NETWORK_DIAGNOSTIC=SCHEMA_UNAVAILABLE safe_release=BLOCKED',flush=True)
            raise RuntimeError(
                'SONG_GROUP_SUMMARY_RELEASE_BLOCKED: network wait is not proven idle ('
                +type(exc).__name__+')'
            ) from exc
        if parked:
            print(f'SONG_PARKED_PRE_MEDIA_NETWORK_WAITS={parked} SAFE_IDLE_PROOF=PASS',flush=True)


def verify_live():
    from production.route_uploader_scene_https import https_get, scene_cookie
    for _ in range(40):
        try:
            health();break
        except (OSError, ValueError, RuntimeError):time.sleep(.5)
    else:raise RuntimeError('Song Uploader health timeout')
    cookie=scene_cookie()
    require(cookie,'authenticated verification session unavailable')
    status,_,body=https_get('/song-uploader/app?tab=auto_engine&new_profile=1',cookie)
    require(status==200,'authenticated Song Uploader GET failed')
    html=body.decode('utf-8')
    require('data-song-group-summary' in html and '<th>Group Name</th>' in html and
            '<th>Channels</th>' in html and '<th>Schedule (PKT)</th>' in html,
            'live automation group summary is missing')
    # Verify the UI-only release, not merely service health: global sync
    # must be on the planner row, with no single-profile sync buttons.
    toolbar_marker='lum-ae-bulk-profile-controls'
    require(html.count(toolbar_marker)==1, 'Song bulk toolbar missing')
    toolbar=html.split(toolbar_marker,1)[1].split('</div>',1)[0]
    require('Profile Schedules' in toolbar and
            'Generate Queue Now' not in toolbar and
            'Start All Profiles' in toolbar and
            'Sync All Channels' in toolbar and
            toolbar.index('Start All Profiles')<toolbar.index('Sync All Channels'),
            'global sync is not on the bulk-action row')
    # An empty/new Song account has no group row and therefore no per-profile
    # Start form yet. Verify the real new-profile controls and nested workspace;
    # the conditional Start form remains covered by offline Jinja contract tests.
    required_controls=(
        'song-profile-privacy-status',
        'lum-short-engine-status',
        '20261030-status-cards-v2',
        'data-ae-panel-target="automation"',
        'data-ae-panel-target="profiles"',
        'data-ae-panel-target="queue"',
    )
    missing=[marker for marker in required_controls if marker not in html]
    require(not missing, 'Song profile parity controls missing from live HTML: '+', '.join(missing))
    require(html.count('>Sync All Channels</button>')==1 and
            'Sync Channel</button>' not in html and
            'data-ae-sync-scan' not in html,
            'individual sync UI persists or global sync is duplicated')
    # Authenticated, read-only acceptance: report each component's displayed
    # state independently. Never infer a healthy worker from Engine RUNNING.
    for component, choices in (('WORKER', ('Idle','Processing','Error','Offline')),
                               ('SCHEDULER', ('Waiting','Paused','Processing','Error','Offline'))):
        label=next((v for v in choices if '>'+component.title()+' '+v+'</span>' in html), 'Unknown')
        print(f'SONG_RUNTIME_{component}_STATUS={label.upper()}',flush=True)
    anonymous_status, _, anonymous_body = https_get('/song-uploader/app')
    # The public Scene SSO gate may redirect unauthenticated users to login
    # rather than return FastAPI's 401; it must never serve protected app HTML.
    require(anonymous_status in (302, 303, 401, 403) and
            b'data-song-group-summary' not in anonymous_body and
            b'lum-ae-automation-profiles' not in anonymous_body,
            f'anonymous route protection failed (status {anonymous_status})')
    print('SONG_UPLOADER_LIVE=PASS AUTH=PASS PROFILES=PASS SCHEDULES=PASS NO_UPLOAD_OR_ARM_ACTIONS',flush=True)


def install(revision):
    require(re.fullmatch('[a-f0-9]{40}',revision),'invalid revision')
    require(shutil.disk_usage('/').free>=5_000_000_000,'disk reserve')
    idle()
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
    require(len(old_dirs)==1,'Song Uploader units use different releases')
    target=BASE/'releases/local-upload-manager'/(revision+'-song-group-summary')
    journal=BASE/'transactions/song-uploader-group-summary'/revision
    require(not target.exists() and not target.is_symlink() and not journal.exists(),'immutable release already exists')
    shutil.copytree(next(iter(old_dirs)).parent,target,symlinks=True)
    hashes={}
    for relative in FILES:
        src=REPO/'apps/local-upload-manager/part01'/relative
        dst=target/'part01'/relative
        require(src.is_file() and not src.is_symlink() and dst.is_file() and not dst.is_symlink(),'source file drift')
        shutil.copy2(src,dst)
        hashes[relative]=hashlib.sha256(dst.read_bytes()).hexdigest()
    journal.mkdir(parents=True,mode=0o700)
    atomic_json(journal/'previous.json',previous)
    changed=False
    try:
        idle()
        # Stop only the isolated Song Uploader; keep every other engine and app running.
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
        verify_live()
        atomic_json(journal/'state.json',dict(status='ACTIVE',revision=revision,source=str(target),files=hashes,
                                           sqlite_backups=len(backups),scheduler='PRESERVED',auto_script='UNTOUCHED'))
        print('SONG_GROUP_SUMMARY_RELEASE=PASS OTHER_ENGINES=UNTOUCHED ROLLBACK=READY',flush=True)
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
            else:raise RuntimeError('Song Uploader rollback health failed')
            atomic_json(journal/'state.json',dict(status='ROLLED_BACK',revision=revision))
            print('SONG_GROUP_SUMMARY_ROLLBACK=PASS DATA=PRESERVED',flush=True)
        raise


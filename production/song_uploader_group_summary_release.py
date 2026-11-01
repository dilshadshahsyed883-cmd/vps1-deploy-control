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
    "lum_engine/oauth_web.py",
    "lum_engine/song_vault.py",
    "lum_engine/project_store.py",
    "lum_engine/studio_tenants.py",
    "lum_engine/channels_web.py",
)
NEW_FILES=frozenset({"lum_engine/studio_tenants.py","lum_engine/song_vault.py"})
UNITS=tuple(f"vps1-production-lum-song-{kind}.service" for kind in ("scheduler","worker","web"))
STATE=DATA/'local-upload-manager/lum-song'
UNIT_DIR=Path('/etc/systemd/system')


QUEUE_REPOSITORY = "desktop_core/modules/db/queue_repository.py"


def queue_claim_overlay(original: bytes, reviewed: bytes) -> bytes:
    """Patch only the reviewed claim method; retain all other live queue code."""
    import ast
    def method(text):
        tree = ast.parse(text)
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "QueueRepository")
        return next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "claim_available_jobs")
    live = original.decode()
    source = reviewed.decode()
    old = method(live)
    new = method(source)
    expected = ast.parse(ast.get_source_segment(source, new)).body[0]
    idx = next(i for i, arg in enumerate(expected.args.kwonlyargs)
               if arg.arg == "allowed_auto_engine_profile_ids")
    del expected.args.kwonlyargs[idx]
    del expected.args.kw_defaults[idx]
    claim_loop = next(n for n in expected.body if isinstance(n, ast.Try))
    claim_loop.body = [n for n in claim_loop.body if not (
        isinstance(n, ast.If) and ast.unparse(n.test) == "allowed_auto_engine_profile_ids is not None")]
    old_dump = ast.dump(old, include_attributes=False)
    require(old_dump in (ast.dump(new, include_attributes=False), ast.dump(expected, include_attributes=False)),
            "queue claim method drift; scoped repair refused")
    lines = live.splitlines(keepends=True)
    replacement = source.splitlines(keepends=True)[new.lineno-1:new.end_lineno]
    result = "".join(lines[:old.lineno-1] + replacement + lines[old.end_lineno:]).encode()
    compile(result, QUEUE_REPOSITORY, "exec")
    print("SONG_QUEUE_SCOPED_CLAIM_SUPPORT=PASS", flush=True)
    return result


def queue_resume_owner_overlay(original: bytes, reviewed: bytes) -> bytes:
    """Overlay only exact-ID paused-job retry; preserve live claim and ledger code.

    This must be added alongside claim_available_jobs. A user retry passes a
    video_ids=[id] filter; running the old method would wake every user's
    paused video in the shared per-instance worker database.
    """
    import ast
    def method(text):
        tree=ast.parse(text)
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef)
                 and n.name=="QueueRepository")
        return next(n for n in cls.body if isinstance(n,ast.FunctionDef)
                    and n.name=="resume_paused")
    live=original.decode()
    source=reviewed.decode()
    old=method(live)
    new=method(source)
    if ast.dump(old,include_attributes=False)==ast.dump(new,include_attributes=False):
        return original
    old_args=[a.arg for a in old.args.args]
    new_args=[a.arg for a in new.args.args]
    require(old_args==["self","auto_engine_only","exclude_auto_engine"] and
            new_args==old_args+["video_ids"] and
            "recovery_state<>'REVIEW_REQUIRED'" in ast.get_source_segment(live,old) and
            "SELECT id, resumable_uri FROM videos" in ast.get_source_segment(live,old),
            "paused-job claim interface drift; release refused")
    original_lines=live.splitlines(keepends=True)
    new_lines=source.splitlines(keepends=True)[new.lineno-1:new.end_lineno]
    result="".join(original_lines[:old.lineno-1]+new_lines+original_lines[old.end_lineno:]).encode()
    compile(result,QUEUE_REPOSITORY,"exec")
    print("SONG_OWNER_SCOPED_PAUSED_RETRY=PASS",flush=True)
    return result


def verify_worker_recovery():
    from production.song_uploader_worker_readonly_diagnostic import collect
    for _ in range(3):
        time.sleep(4)
        report = collect(STATE)
        require(report.get("db") == "READ_ONLY_OK" and
                report.get("worker_error") == "NONE" and
                report.get("worker_heartbeat_age") == "RECENT" and
                report.get("units", {}).get("worker") == "ACTIVE",
                "worker recovery acceptance failed")
    print("SONG_WORKER_RECOVERY=PASS ERROR=NONE HEARTBEAT=RECENT", flush=True)


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
    require('Assign Studio resources to a user' not in html and
            'User workspace ownership' not in html and
            '/actions/studio-tenant/grant' not in html,
            'obsolete resource owner assignment form still visible')
    require('data-song-group-summary' in html and '<th>Group Name</th>' in html and
            '<th>Channels</th>' in html and '<th>Schedule (PKT)</th>' in html,
            'live automation group summary is missing')
    auth_status,_,auth_body=https_get('/song-uploader/app?tab=channels',cookie)
    require(auth_status==200,'Song Uploader authenticated Channels GET failed')
    auth_html=auth_body.decode('utf-8')
    require('data-song-shared-client' in auth_html and
            '1. Shared OAuth Client' in auth_html and
            'Permanent OAuth &amp; Token Vault' in auth_html and
            '/song-uploader/actions/song/credentials/upload' in auth_html and
            '/song-uploader/actions/oauth-client' in auth_html and
            'My channel tokens' in auth_html,
            'Song OAuth/Vault owner controls missing from authenticated UI')
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
        'class="lum-short-profile-toolbar"',
        'lum-short-scheduler-state',
        'song-profile=20261031-pr505-v1',
        '20261030-status-cards-v2',
        'data-ae-panel-target="automation"',
        'data-ae-panel-target="profiles"',
        'data-ae-panel-target="queue"',
    )
    missing=[marker for marker in required_controls if marker not in html]
    require(not missing, 'Song profile parity controls missing from live HTML: '+', '.join(missing))
    require(html.count('id="lum-ae-profile-select"') == 1 and
            'class="lum-ae-profile-manager-top"' not in html and
            'class="lum-ae-profile-browser"' not in html,
            'Song PR #505 compact Profiles toolbar did not replace legacy manager')
    require(html.count('>Sync All Channels</button>')==1 and
            'Sync Channel</button>' not in html and
            'data-ae-sync-scan' not in html,
            'individual sync UI persists or global sync is duplicated')
    # Verify the actual Song Dashboard (not just Auto Engine) no longer exposes
    # VPS report generation, listing or download controls; all stored reports remain.
    dashboard_status,_,dashboard_body=https_get('/song-uploader/app?tab=dashboard',cookie)
    require(dashboard_status==200, 'authenticated Song Uploader Dashboard GET failed')
    dashboard_html=dashboard_body.decode('utf-8')
    # Song Uploader retired the shared "Save modes" card for both roles.
    # Verify the current dashboard rather than requiring obsolete controls.
    require('data-engine-instance="song"' in dashboard_html and
            'class="lum-metrics"' in dashboard_html and
            '<h2>Serial engine</h2>' in dashboard_html and
            '>Save modes</button>' not in dashboard_html and
            '<h2>Uploader modes</h2>' not in dashboard_html,
            'Song Uploader Dashboard owner-parity contract mismatch')
    require('<h2>VPS Reports</h2>' not in dashboard_html and
            '/actions/report-health' not in dashboard_html and
            'class="report-list"' not in dashboard_html,
            'Song Uploader Dashboard still displays VPS Reports')

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


def install(revision, *, ui_only=False):
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
    target=BASE/'releases/local-upload-manager'/(revision+('-song-assignment-ui' if ui_only else '-song-group-summary'))
    journal=BASE/'transactions'/('song-uploader-assignment-ui' if ui_only else 'song-uploader-group-summary')/revision
    require(not target.exists() and not target.is_symlink() and not journal.exists(),'immutable release already exists')
    shutil.copytree(next(iter(old_dirs)).parent,target,symlinks=True)
    hashes={}
    for relative in (("templates/app.html",) if ui_only else FILES):
        src=REPO/'apps/local-upload-manager/part01'/relative
        dst=target/'part01'/relative
        require(src.is_file() and not src.is_symlink() and not dst.is_symlink() and
                (dst.is_file() or (relative in NEW_FILES and not dst.exists())),
                'source file drift')
        dst.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(src,dst)
        hashes[relative]=hashlib.sha256(dst.read_bytes()).hexdigest()
    if not ui_only:
        queue_source=REPO/'apps/local-upload-manager/part01'/QUEUE_REPOSITORY
        queue_target=target/'part01'/QUEUE_REPOSITORY
        require(queue_source.is_file() and not queue_source.is_symlink() and
                queue_target.is_file() and not queue_target.is_symlink(), 'queue repository ownership')
        queue_target.write_bytes(queue_resume_owner_overlay(
            queue_claim_overlay(queue_target.read_bytes(), queue_source.read_bytes()),
            queue_source.read_bytes()))
        hashes[QUEUE_REPOSITORY]=hashlib.sha256(queue_target.read_bytes()).hexdigest()
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
        if not ui_only:
            verify_worker_recovery()
        atomic_json(journal/'state.json',dict(status='ACTIVE',revision=revision,source=str(target),files=hashes,
                                           sqlite_backups=len(backups),scheduler='PRESERVED',auto_script='UNTOUCHED'))
        print(('SONG_ASSIGNMENT_UI_RELEASE' if ui_only else 'SONG_GROUP_SUMMARY_RELEASE')
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
            else:raise RuntimeError('Song Uploader rollback health failed')
            atomic_json(journal/'state.json',dict(status='ROLLED_BACK',revision=revision))
            print('SONG_GROUP_SUMMARY_ROLLBACK=PASS DATA=PRESERVED',flush=True)
        raise


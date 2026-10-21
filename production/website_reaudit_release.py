"""Approved RA-01–17 web overlay. Preserve independent workers, state and dependencies."""
from __future__ import annotations
import hashlib
import json
import re
import shutil
import sqlite3
import time
from pathlib import Path
from urllib.request import urlopen
from production.runtime import BASE, DATA, REPO, CATALOG, runtime_name, atomic_json, atomic_link, event, http_check, run, require_sml_idle

FILES = {
    'auto-caption-studio': ('part01/web_backend/acs_studio.html', 'part01/web_backend/app.py'),
    'voice-studio': ('part01/templates/voice_studio.html', 'part01/vps1_omnivoice_app.py'),
    'self-media-library': ('part01/app.py', 'part01/media_extensions.py', 'part01/static/app.js', 'part01/static/index.html', 'part01/static/styles.css'),
    'short-stream': ('part01/auto_stream_ui.py',),
    'video-merger': ('part01/templates/index.html',),
}

def require(ok, message):
    if not ok:
        raise RuntimeError('WEBSITE_REAUDIT_RELEASE_BLOCKED: ' + message)


def idle(app):
    if app == 'self-media-library':
        state = DATA / runtime_name(app)
        require_sml_idle(state)
        database = state / 'sml/library.db'
        require(database.is_file(), 'SML database missing')
        with sqlite3.connect(database.as_uri()+'?mode=ro', uri=True, timeout=5) as db:
            db.execute('PRAGMA query_only=ON')
            for table in ('clips', 'sources'):
                require(not db.execute("SELECT 1 FROM "+table+" WHERE drive_state='uploading' LIMIT 1").fetchone(), 'SML Drive transfer in progress')
    elif app == 'voice-studio':
        from production.voice_template_release import require_idle_audio_downloader
        require_idle_audio_downloader()
    elif app == 'video-merger':
        from production.video_template_release import idle as video_idle
        # A transient stalled status read must never bypass the idle guard.
        # Retry boundedly; repeated timeout or any unfinished job remains blocked.
        for attempt in range(3):
            try:
                video_idle(app)
                break
            except TimeoutError:
                if attempt == 2:
                    raise
                time.sleep(2)
    elif app == 'short-stream':
        with urlopen('http://127.0.0.1:8145/engine/health', timeout=15) as response:
            value = json.load(response)
        require(value.get('encoder_sessions') == 0 and value.get('render_tasks') == 0, 'Auto Stream encoder/render active or unknown')
    # ACS rendering executes in independent worker processes, which are untouched.


def install(app, revision):
    require(app in FILES and re.fullmatch('[a-f0-9]{40}', revision), 'invalid app/revision')
    current = BASE/'current'/runtime_name(app)
    releases = BASE/'releases'/runtime_name(app)
    require(current.is_symlink(), 'current app pointer missing')
    previous = current.resolve(strict=True)
    require(previous.is_relative_to(releases), 'unowned release pointer')
    http_check(app)
    idle(app)
    target = releases/(revision+'-website-reaudit')
    if previous == target:
        event(app=app, result='PASS_WEBSITE_REAUDIT_ALREADY_INSTALLED')
        return
    require(not target.exists(), 'immutable target exists')
    manifest_path = previous/'_production_manifest.json'
    require(manifest_path.is_file() and not manifest_path.is_symlink(), 'manifest missing')
    manifest = json.loads(manifest_path.read_text())
    require(manifest.get('app') == app and isinstance(manifest.get('files'), dict), 'manifest ownership invalid')
    patches = {}
    for name in FILES[app]:
        source = REPO/'apps'/app/name
        require(source.is_file() and not source.is_symlink(), 'unsafe source '+name)
        require((previous/name).is_file() and not (previous/name).is_symlink(), 'unsafe active file '+name)
        data = source.read_bytes()
        if name.endswith('.py'):
            compile(data, name, 'exec')
        patches[name] = data
    journal = BASE/'transactions/website-reaudit'/app/revision
    journal.mkdir(parents=True, exist_ok=False)
    atomic_json(journal/'previous.json', {'previous': str(previous), 'files': list(patches)})
    shutil.copytree(previous, target, symlinks=True)
    for name, data in patches.items():
        destination = target/name
        require(not destination.parent.is_symlink(), 'linked destination directory')
        destination.write_bytes(data)
        manifest['files'][name] = hashlib.sha256(data).hexdigest()
    manifest.update(revision=revision, website_reaudit_overlay=str(previous))
    atomic_json(target/'_production_manifest.json', manifest, 0o644)
    unit = 'vps1-production-'+app+'.service'
    idle(app)
    changed = False
    try:
        atomic_link(current, target)
        changed = True
        run(['systemctl','restart',unit],timeout=90,quiet=True)
        http_check(app)
        # Verify authenticated same-origin app readback, not just service health.
        from production.route_uploader_scene_https import scene_cookie, https_get
        code, kind, page = https_get('/'+app+('/app' if app=='voice-studio' else '/'), scene_cookie())
        require(code == 200 and 'html' in kind.lower() and len(page)>1000, 'authenticated UI readback failed')
        atomic_json(journal/'result.json', {'status':'ACTIVE','workers':'PRESERVED','data':'UNCHANGED'})
        event(app=app,result='PASS_WEBSITE_REAUDIT_OVERLAY',workers='PRESERVED',data='UNCHANGED')
    except BaseException:
        if changed:
            atomic_link(current, previous)
            run(['systemctl','restart',unit],timeout=90,quiet=True)
            http_check(app)
            event(app=app,rollback='PASS_WEBSITE_REAUDIT',data='PRESERVED')
        raise

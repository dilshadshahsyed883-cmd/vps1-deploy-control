"""Song Studio Voice HTML overlay; synthesis workers and persistent data stay live."""
import hashlib
import json
from pathlib import Path
import shutil

from production.runtime import BASE, REPO, atomic_json, atomic_link, event, http_check, run
from production.voice_template_release import require, require_idle_audio_downloader, verify_audio_downloader_https, read_full_authenticated_voice_html

CURRENT = BASE / 'current/voice-studio'
RELEASES = BASE / 'releases/voice-studio'
PAGE = Path('part01/templates/voice_studio.html')
UNIT = 'vps1-production-voice-studio.service'
JOURNALS = BASE / 'transactions/song-studio-voice'


def validate_template(payload):
    for marker in (b'data-workspace="voice"', b'vps1_native_site_shell.js?v=20261029-song-studio-v1',
                   b'id="vsEngineOmni"', b'id="vsEngineKokoro"', b'id="vsEngineEdge"', b'id="vsEngineAudio"',
                   b'id="audio-downloader-v1-js"', b'id="audio-downloader-proxy-v1"'):
        require(payload.count(marker) == 1, 'Song Studio Voice source marker missing/duplicated')
    require(b'aria-selected="false" hidden>Audio Downloader' in payload and
            b'vps1:voice-engine-changed' in payload, 'Song Studio Voice navigation missing')


def worker_pids():
    from production.workers import unit_names
    return {name: run(['systemctl', 'show', '--property=MainPID', '--value', name], quiet=True)
            for name in unit_names('voice-studio')}


def install_voice(revision):
    require(len(revision) == 40 and all(c in '0123456789abcdef' for c in revision), 'invalid revision')
    require(CURRENT.is_symlink(), 'Voice pointer unavailable')
    previous = CURRENT.resolve(strict=True)
    require(previous.is_relative_to(RELEASES), 'Voice release ownership changed')
    source = REPO / 'apps/voice-studio' / PAGE
    require(source.is_file() and not source.is_symlink(), 'Voice source missing/linked')
    payload = source.read_bytes(); validate_template(payload)
    require_idle_audio_downloader()
    pids = worker_pids()
    target = RELEASES / (revision + '-song-studio')
    journal = JOURNALS / revision
    require(not target.exists() and not journal.exists(), 'immutable Voice release/journal exists')
    require((previous / PAGE).is_file() and not (previous / PAGE).is_symlink(), 'Voice template ownership')
    journal.mkdir(parents=True, mode=0o700)
    atomic_json(journal / 'previous.json', {'release': str(previous), 'worker_pids': pids})
    shutil.copytree(previous, target, symlinks=True)
    (target / PAGE).write_bytes(payload)
    path = target / '_production_manifest.json'
    require(path.is_file() and not path.is_symlink(), 'Voice manifest missing/linked')
    manifest = json.loads(path.read_text())
    require(manifest.get('app') == 'voice-studio' and str(PAGE) in manifest.get('files', {}), 'Voice manifest ownership')
    manifest['revision'] = revision
    manifest['files'][str(PAGE)] = hashlib.sha256(payload).hexdigest()
    manifest['ui_only_overlay'] = str(previous)
    atomic_json(path, manifest, 0o644)
    switched = False
    try:
        require_idle_audio_downloader()
        atomic_link(CURRENT, target); switched = True
        run(['systemctl', 'restart', UNIT], timeout=60, quiet=True)
        http_check('voice-studio')
        from production.route_uploader_scene_https import scene_cookie, https_get
        cookie = scene_cookie()
        html = read_full_authenticated_voice_html(cookie)
        require(b'vps1_native_site_shell.js?v=20261029-song-studio-v1' in html and
                b'vps1:voice-engine-changed' in html, 'public Song Studio Voice HTML not live')
        require(https_get('/voice-studio/app')[0] in (401, 403, 303), 'anonymous Voice access')
        verify_audio_downloader_https(cookie)
        require(worker_pids() == pids, 'independent Voice worker PID changed')
        atomic_json(journal / 'result.json', {'state': 'ACTIVE', 'workers': 'PRESERVED', 'https': 'PASS'})
        event(app='voice-studio', result='PASS_SONG_STUDIO_VOICE_UI', workers='PRESERVED', rollback=str(journal))
    except BaseException:
        if switched:
            atomic_link(CURRENT, previous)
            run(['systemctl', 'restart', UNIT], timeout=60, quiet=True)
            http_check('voice-studio')
            event(app='voice-studio', rollback='PASS', workers='UNCHANGED', data='PRESERVED')
        raise

SCENE_CURRENT = BASE / 'current/scene-builder'
SCENE_RELEASES = BASE / 'releases/scene-builder'
SCENE_UNIT = 'vps1-scene-candidate.service'
SCENE_JOURNALS = BASE / 'transactions/song-studio-scene-web'
SCENE_FILES = ('part01/app.py', 'part01/templates/base.html',
               'part01/static/vps1_native_site_shell.js', 'part01/static/vps1_native_site_shell.css')


def patch_scene_payloads(original, source):
    from production.song_maker_release import scene_allowlist, verify_shared_shell
    marker = '# SONG_STUDIO_NAV_V1:'
    anchor = '@app.get("/experiment-studio/")'
    reviewed = source['part01/app.py'].decode()
    start = reviewed.index(marker); end = reviewed.index(anchor, start)
    routes = reviewed[start:end]
    app = original['part01/app.py'].decode()
    require(app.count(anchor) == 1 and app.count(marker) <= 1, 'Scene route ownership drift')
    if marker in app:
        old_start = app.index(marker); old_end = app.index(anchor, old_start)
        app = app[:old_start] + routes + app[old_end:]
    else:
        app = app.replace(anchor, routes + anchor, 1)
    app = scene_allowlist(app)
    for ext in ('js', 'css'):
        app = app.replace('b"vps1_native_site_shell.'+ext+'?v=20261020-upload-studio-v1"',
                          'b"vps1_native_site_shell.'+ext+'?v=20261029-song-studio-v1"')
    compile(app, 'Scene app.py', 'exec')
    base = original['part01/templates/base.html'].decode()
    reviewed_base = source['part01/templates/base.html'].decode()
    song_line = next(line for line in reviewed_base.splitlines(True) if 'href="/song-studio/"' in line)
    tabs_line = next(line for line in reviewed_base.splitlines(True) if 'vps1-song-studio-tabs' in line)
    if 'href="/song-studio/"' not in base:
        voice_lines = [line for line in base.splitlines(True) if 'href="/voice-studio/app#vps1-production"' in line]
        require(len(voice_lines) == 1, 'Scene sidebar ownership drift')
        base = base.replace(voice_lines[0], voice_lines[0] + song_line, 1)
    if 'vps1-song-studio-tabs' not in base:
        lines = [line for line in base.splitlines(True) if 'aria-label="Experiment Studio"' in line and '<nav' in line]
        require(len(lines) == 1, 'Scene tab anchor drift')
        base = base.replace(lines[0], tabs_line + lines[0], 1)
    for ext in ('js', 'css'):
        old = 'vps1_native_site_shell.'+ext+'?v=20261020-sidebar-stable-v4'
        if old+'&song=20261029' not in base: base = base.replace(old, old+'&song=20261029')
    key = 'part01/static/vps1_native_site_shell.js'
    verify_shared_shell(original[key], source[key])
    css = original['part01/static/vps1_native_site_shell.css'].decode()
    reviewed_css = source['part01/static/vps1_native_site_shell.css'].decode()
    css_start = reviewed_css.index('/* Audio Downloader keeps its existing engine')
    css_end = reviewed_css.index('body.vps1-native-voice .vps3-main-sidebar', css_start)
    rules = reviewed_css[css_start:css_end]
    if '/* Audio Downloader keeps its existing engine' not in css: css += '\n'+rules
    else: require(rules in css, 'Song Studio CSS drift')
    return {'part01/app.py': app.encode(), 'part01/templates/base.html': base.encode(),
            key: source[key], 'part01/static/vps1_native_site_shell.css': css.encode()}


def install_scene(revision):
    require(SCENE_CURRENT.is_symlink(), 'Scene web pointer unavailable')
    previous = SCENE_CURRENT.resolve(strict=True)
    require(previous.is_relative_to(SCENE_RELEASES), 'Scene web ownership changed')
    original, source = {}, {}
    for name in SCENE_FILES:
        live = previous/name; reviewed = REPO/'apps/scene-builder'/name
        require(live.is_file() and not live.is_symlink() and reviewed.is_file() and not reviewed.is_symlink(), 'Scene web file missing/linked')
        original[name] = live.read_bytes(); source[name] = reviewed.read_bytes()
    changes = patch_scene_payloads(original, source)
    worker = 'vps1-scene-production-worker.service'
    pid = run(['systemctl', 'show', '--property=MainPID', '--value', worker], quiet=True)
    target = SCENE_RELEASES/(revision+'-song-studio-web')
    journal = SCENE_JOURNALS/revision
    require(not target.exists() and not journal.exists(), 'immutable Scene web target exists')
    journal.mkdir(parents=True, mode=0o700)
    atomic_json(journal/'previous.json', {'release':str(previous),'worker_pid':pid,'files':list(changes)})
    shutil.copytree(previous,target,symlinks=True)
    manifest_path = target/'_production_manifest.json'
    # The pre-GitOps Scene candidate is a verified, root-owned immutable release
    # but has no generic _production_manifest.json. Its known release-pointer
    # identity and four reviewed existing files are already checked above.
    # Generate a LIMITED overlay inventory rather than misrepresenting a full
    # generic application manifest or modifying the prior release.
    require(not manifest_path.is_symlink(), 'Scene manifest symlink denied')
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text())
        require(isinstance(manifest, dict) and isinstance(manifest.get('files'), dict),
                'Scene manifest format invalid')
        require(manifest.get('app') in (None, 'scene-builder'),
                'Scene manifest app ownership')
    elif not manifest_path.exists():
        manifest = {'app':'scene-builder', 'revision':revision,
                    'files':{name:hashlib.sha256(payload).hexdigest()
                             for name,payload in original.items()},
                    'partial_source_inventory':True,
                    'overlay_prior_release':str(previous)}
    else:
        raise RuntimeError('Scene manifest is not a regular file')
    for name,payload in changes.items():
        require(name in manifest.get('files',{}), 'Scene manifest file missing')
        (target/name).write_bytes(payload)
        manifest['files'][name] = hashlib.sha256(payload).hexdigest()
    manifest.update(revision=revision, overlay='song-studio-web-only')
    atomic_json(manifest_path,manifest,0o644)
    switched = False
    try:
        atomic_link(SCENE_CURRENT,target); switched = True
        run(['systemctl','restart',SCENE_UNIT],timeout=70,quiet=True)
        from production.route_uploader_scene_https import scene_cookie, https_get
        cookie = scene_cookie()
        require(https_get('/scene-builder/',cookie)[0] == 200, 'Scene web unavailable')
        for route in ('/song-studio/','/song-studio/song-maker/','/song-studio/song-uploader/'):
            require(https_get(route,cookie)[0] == 303, 'Song Studio redirect unavailable')
            require(https_get(route)[0] in (303,401,403), 'Song Studio anonymous access')
        require(https_get('/static/vps1_native_site_shell.js',cookie)[2] == source['part01/static/vps1_native_site_shell.js'], 'Scene shared shell not live')
        require(run(['systemctl','show','--property=MainPID','--value',worker],quiet=True) == pid, 'Scene render worker changed')
        atomic_json(journal/'result.json', {'state':'ACTIVE','worker':'PRESERVED','https':'PASS'})
        event(app='scene-builder',result='PASS_SONG_STUDIO_SCENE_WEB',worker='PRESERVED',data='UNCHANGED',rollback=str(journal))
    except BaseException:
        if switched:
            atomic_link(SCENE_CURRENT,previous)
            run(['systemctl','restart',SCENE_UNIT],timeout=70,quiet=True)
            event(app='scene-builder',rollback='PASS',worker='PRESERVED',data='UNCHANGED')
        raise

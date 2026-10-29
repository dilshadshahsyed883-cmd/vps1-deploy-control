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

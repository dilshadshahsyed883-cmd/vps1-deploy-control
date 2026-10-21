"""Audio Downloader module-only release; existing HTML and all TTS workers preserved."""
from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import signal
import shutil
import sqlite3
import subprocess

from production.runtime import BASE, REPO, atomic_json, atomic_link, event, http_check, run
from production.voice_queue_release import local_json

CURRENT = BASE / 'current/voice-studio'
RELEASES = BASE / 'releases/voice-studio'
UNIT = 'vps1-production-voice-studio.service'
MODULE = Path('part01/audio_downloader_api.py')
BUSY = ('queued', 'downloading', 'downloaded', 'processing', 'converting', 'uploading')


def require(value, message):
    if not value:
        raise RuntimeError('AUDIO_PROXY_RELEASE_BLOCKED: ' + message)


def live_environment():
    pid = run(['systemctl', 'show', UNIT, '-p', 'MainPID', '--value'], quiet=True).strip()
    require(pid.isdigit() and int(pid) > 1, 'Voice service not active')
    # Read privately; never log service environment or credentials.
    entries = (Path('/proc') / pid / 'environ').read_bytes().split(b'\0')
    return {k.decode(): v.decode() for entry in entries if b'=' in entry for k, v in [entry.split(b'=', 1)]}


def lock_idle(path):
    require(path.is_file() and not path.is_symlink(), 'Audio queue missing/untrusted')
    con = sqlite3.connect('file:' + str(path) + '?mode=rw', uri=True, timeout=10)
    try:
        con.execute('BEGIN IMMEDIATE')
        require(not con.execute('SELECT 1 FROM jobs WHERE status IN (' + ','.join('?' for _ in BUSY) + ') LIMIT 1', BUSY).fetchone(),
                'Audio queue busy; no restart permitted')
        return con
    except BaseException:
        con.close()
        raise


def verify():
    from production.route_uploader_scene_https import scene_cookie, https_get
    cookie = scene_cookie()
    for endpoint in ('status', 'proxy', 'jobs?limit=1'):
        status, data = local_json('http://127.0.0.1:8143/tts/api/audio-downloader/' + endpoint)
        require(status == 200, 'Audio local API unavailable')
        if endpoint == 'status':
            require(data.get('ready') and data.get('download_contract') == 'explicit-proxy-private-session-v2', 'worker dependencies/contract unavailable')
            require(data.get('proxy', {}).get('valid_selection'), 'enabled proxy selection invalid')
        path = '/voice-studio/tts/api/audio-downloader/' + endpoint
        code, kind, body = https_get(path, cookie)
        require(code == 200 and 'json' in kind.lower(), 'authenticated Audio API unavailable')
        public = json.loads(body)
        require(public.get('download_contract') == data.get('download_contract') if endpoint == 'status' else isinstance(public, dict), 'public worker contract mismatch')
        code, _, _ = https_get(path)
        require(code in (401, 403, 303), 'anonymous Audio API unexpectedly allowed')


def smoke(target, environment):
    python = Path(environment['PATH'].split(':')[0]) / 'python'
    require(python.is_file(), 'service Python missing')
    runner = next((path for path in (Path('/usr/sbin/runuser'), Path('/usr/bin/runuser'))
                   if path.is_file()), None)
    require(runner is not None, 'system runuser missing')
    process = subprocess.Popen([str(runner), '-u', 'ubuntu', '--', str(python),
                                str(REPO / 'production/audio_downloader_smoke.py'), str(target / MODULE)],
                               env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               start_new_session=True)
    try:
        out, _ = process.communicate(timeout=240)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        return {'result': 'EXTERNAL_SMOKE_TIMEOUT', 'audio': 'NOT_VERIFIED', 'drive': 'NOT_VERIFIED'}
    try:
        value = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise RuntimeError('Audio disposable smoke produced no sanitized receipt') from None
    event(app='audio-downloader', smoke=value)
    require(process.returncode == 0, 'Audio disposable smoke runtime failed')
    return value


def install(revision):
    require(len(revision) == 40 and all(c in '0123456789abcdef' for c in revision), 'invalid SHA')
    require(CURRENT.is_symlink(), 'unmanaged current Voice release')
    previous = CURRENT.resolve(strict=True)
    require(previous.is_relative_to(RELEASES), 'release path escaped')
    http_check('voice-studio')
    environment = live_environment()
    root = Path(environment['AUDIO_DOWNLOADER_ROOT'])
    require(root.is_absolute(), 'unknown Audio data root')
    old = previous / MODULE
    source = REPO / 'apps/voice-studio' / MODULE
    require(old.is_file() and not old.is_symlink() and not source.is_symlink(), 'untrusted Audio module')
    payload = source.read_bytes()
    ast.parse(payload)
    require(b'def youtube_execution' in payload, 'reviewed proxy implementation missing')
    target = RELEASES / (revision + '-audio-proxy-only')
    journal = BASE / 'transactions/audio-proxy-only' / revision
    require(not target.exists() and not journal.exists(), 'immutable release already exists')
    # Transactional idle fence closes the enqueue/claim race before web shutdown.
    fence = lock_idle(root / 'data/jobs.sqlite3')
    switched = False
    stopped = False
    try:
        journal.mkdir(mode=0o700, parents=True)
        atomic_json(journal / 'before.json', {'previous': str(previous), 'revision': revision,
                    'audio_sha256': hashlib.sha256(old.read_bytes()).hexdigest()})
        run(['systemctl', 'stop', UNIT], quiet=True, timeout=60)
        stopped = True
        fence.close()
        fence = None
        with sqlite3.connect(str(root / 'data/jobs.sqlite3')) as live:
            with sqlite3.connect(str(journal / 'jobs.sqlite3')) as backup:
                live.backup(backup)
                require(backup.execute('PRAGMA integrity_check').fetchone()[0] == 'ok', 'Audio backup corrupt')
        shutil.copytree(previous, target, symlinks=True)
        (target / MODULE).write_bytes(payload)
        manifest_file = target / '_production_manifest.json'
        require(manifest_file.is_file() and not manifest_file.is_symlink(), 'manifest missing')
        manifest = json.loads(manifest_file.read_text())
        require(manifest.get('app') == 'voice-studio' and str(MODULE) in manifest.get('files', {}), 'manifest identity invalid')
        manifest['files'][str(MODULE)] = hashlib.sha256(payload).hexdigest()
        manifest.update(revision=revision, audio_proxy_overlay=str(previous))
        atomic_json(manifest_file, manifest, 0o644)
        atomic_link(CURRENT, target)
        switched = True
        run(['systemctl', 'start', UNIT], quiet=True, timeout=60)
        http_check('voice-studio')
        verify()
        result = smoke(target, environment)
        atomic_json(journal / 'result.json', {'state': 'ACTIVE', 'revision': revision, 'smoke': result})
        event(app='voice-studio', result='PASS_AUDIO_PROXY_ONLY', workers='PRESERVED', data='PRESERVED',
              https='PASS', sqlite_backup='PASS', smoke=result, rollback=str(journal))
    except BaseException:
        if fence is not None:
            fence.close()
            fence = None
        if switched:
            atomic_link(CURRENT, previous)
        if stopped:
            run(['systemctl', 'start' if not switched else 'restart', UNIT], quiet=True, timeout=60)
            http_check('voice-studio')
            event(app='voice-studio', rollback='PASS', data='PRESERVED')
        raise
    finally:
        if fence is not None:
            fence.close()

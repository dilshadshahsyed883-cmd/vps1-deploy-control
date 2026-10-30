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
from production.audio_proxy_restore import plan as plan_proxy_restore, replace_private
from production.video_merger_import_release import install_js as install_verified_deno

CURRENT = BASE / 'current/voice-studio'
RELEASES = BASE / 'releases/voice-studio'
UNIT = 'vps1-production-voice-studio.service'
MODULE = Path('part01/audio_downloader_api.py')
PAGE = Path('part01/templates/voice_studio.html')
BUSY = ('queued', 'downloading', 'downloaded', 'processing', 'converting', 'uploading')


def require(value, message):
    if not value:
        raise RuntimeError('AUDIO_PROXY_RELEASE_BLOCKED: ' + message)


def ensure_audio_js(environment):
    """Bring proven Video Merger Deno+EJS prerequisites into Voice's own venv."""
    bin_path = Path(environment['PATH'].split(':')[0])
    python = bin_path / 'python'
    require(python.is_file() and bin_path.name == 'bin', 'Voice service Python missing')
    # Use the same yt-dlp release already running in Voice; add EJS scripts and
    # default extras, never upgrade its core downloader to an unreviewed version.
    run([python, '-m', 'pip', 'install', '--disable-pip-version-check', '--no-cache-dir',
         'yt-dlp[default]==2026.8.19'], quiet=True, timeout=540)
    run([python, '-c', 'import yt_dlp; import yt_dlp_ejs'], quiet=True)
    deno = bin_path / 'deno'
    newly_installed = not deno.exists()
    if newly_installed:
        install_verified_deno(bin_path.parent)
    require(deno.is_file() and os.access(deno, os.X_OK), 'app-isolated Deno unavailable')
    run([deno, '--version'], quiet=True)
    event(app='audio-downloader', js_runtime='DENO_EJS_READY', credentials_logged=False)
    return newly_installed


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


def prepare_audio_runtime(root, environment):
    """Stage one pinned Audio-only venv; never change shared Voice packages."""
    runtime = root / 'runtimes/yt-dlp-2026.10.26'
    require(not runtime.is_symlink(), 'isolated runtime cannot be a symlink')
    if runtime.exists():
        require((runtime / 'bin/yt-dlp').is_file(), 'incomplete isolated runtime')
        return runtime, False
    runner = next(p for p in (Path('/usr/sbin/runuser'), Path('/usr/bin/runuser')) if p.is_file())
    python = Path(environment['PATH'].split(':')[0]) / 'python'
    prefix = [str(runner), '-u', 'ubuntu', '--']
    try:
        run(prefix + [str(python), '-m', 'venv', str(runtime)], quiet=True, timeout=120)
        run(prefix + [str(runtime / 'bin/python'), '-m', 'pip', 'install', '--disable-pip-version-check',
                      '--no-cache-dir', 'yt-dlp[default]==2026.10.26'], quiet=True, timeout=360)
        version = run(prefix + [str(runtime / 'bin/yt-dlp'), '--version'], quiet=True)
        require(version.strip() == '2026.10.26', 'isolated yt-dlp version mismatch')
        event(app='audio-downloader', isolated_runtime='2026.10.26', shared_packages='UNCHANGED')
        return runtime, True
    except BaseException:
        shutil.rmtree(runtime, ignore_errors=True)
        raise


def restore_smoke(source, environment, baseline=None, *, allow_download_failure=False):
    """Strict same-ID receipt: provider/auth errors never count as a pass."""
    python = Path(environment['PATH'].split(':')[0]) / 'python'
    runner = next(p for p in (Path('/usr/sbin/runuser'), Path('/usr/bin/runuser')) if p.is_file())
    command = [str(runner), '-u', 'ubuntu', '--', str(python),
               str(REPO / 'production/audio_downloader_restore_smoke.py'), str(source)]
    if baseline:
        command.append(str(baseline))
    proc = subprocess.Popen(command, env=environment, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        out, _ = proc.communicate(timeout=900)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL); proc.communicate()
        raise RuntimeError('Audio restore comparison timed out; no download PASS') from None
    try:
        report = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise RuntimeError('Audio restore comparison missing sanitized receipt') from None
    event(app='audio-downloader', restore_comparison=report)
    if not allow_download_failure:
        require(proc.returncode == 0 and report.get('result') == 'PASS',
                'same-ID real MP3/thumbnail download failed on tested routes')
    else:
        require(proc.returncode in (0, 1) and report.get('video_id') == 'sQ74S7cZ3Kg'
                and isinstance(report.get('routes'), dict) and 'direct' in report['routes'],
                'missing real download diagnostic receipt')
    return report


def error_label_overlay(installed):
    """Only failure-stage persistence changes; retain the deployed downloader engine."""
    old = b"next_retry_at=None, progress_stage='manual retry required')"
    new = b"next_retry_at=None, progress_stage='download failed' if isinstance(error, DownloadFailure) else 'manual retry required')"
    if new in installed:
        require(installed.count(new) == 1, 'duplicate failure-stage anchor')
        return installed
    require(installed.count(old) == 1, 'deployed failure-stage ownership drift')
    updated = installed.replace(old, new, 1)
    return updated.replace(b'Configure fresh private YouTube cookies; no automatic retry or bypass guarantee.',
                           b'Check the selected proxy/direct route and downloader runtime; an authorized session may be needed for restricted content. No automatic retry.', 1)


def audio_thumbnail_overlay(installed: bytes, source: bytes) -> bytes:
    """Copy ONLY Audio Downloader markup and controller, retaining other Voice UIs."""
    regions = (
        (b'<!-- AUDIO_DOWNLOADER_TAB_V1 -->', b'<!-- /AUDIO_DOWNLOADER_TAB_V1 -->'),
        (b'<script id="audio-downloader-v1-js">', b'</script>'),
    )
    updated = installed
    for begin, end in regions:
        require(source.count(begin) == 1 and updated.count(begin) == 1,
                'Audio Downloader source/installed region missing or duplicated')
        start = source.index(begin)
        stop = source.index(end, start) + len(end)
        block = source[start:stop]
        old_start = updated.index(begin)
        old_stop = updated.index(end, old_start) + len(end)
        updated = updated[:old_start] + block + updated[old_stop:]
    require(updated.count(b'id="vsaThumbnail"') == 1 and
            updated.count(b'download_thumbnail:') == 1,
            'thumbnail browser control contract missing')
    return updated


def install(revision, *, restore_proxy=True, include_thumbnail=False, restore_download=False, diagnostic_only=False):
    require(not (restore_proxy and include_thumbnail), 'thumbnail overlay cannot restore proxy')
    require(len(revision) == 40 and all(c in '0123456789abcdef' for c in revision), 'invalid SHA')
    require(CURRENT.is_symlink(), 'unmanaged current Voice release')
    previous = CURRENT.resolve(strict=True)
    require(previous.is_relative_to(RELEASES), 'release path escaped')
    http_check('voice-studio')
    environment = live_environment()
    root = Path(environment['AUDIO_DOWNLOADER_ROOT'])
    require(root.is_absolute(), 'unknown Audio data root')
    proxy_plan = plan_proxy_restore(root) if restore_proxy else None
    old = previous / MODULE
    source = REPO / 'apps/voice-studio' / MODULE
    require(old.is_file() and not old.is_symlink() and not source.is_symlink(), 'untrusted Audio module')
    require(not (restore_download and diagnostic_only), 'conflicting release modes')
    payload = error_label_overlay(old.read_bytes()) if diagnostic_only else source.read_bytes()
    ast.parse(payload)
    require(b'def youtube_execution' in payload, 'reviewed proxy implementation missing')
    kind = 'audio-error-label-only' if diagnostic_only else 'audio-restore-only' if restore_download else ('audio-thumbnail-only' if include_thumbnail else ('audio-proxy-only' if restore_proxy else 'audio-filename-only'))
    target = RELEASES / (revision + '-' + kind)
    journal = BASE / 'transactions' / kind / revision
    require(not target.exists() and not journal.exists(), 'immutable release already exists')
    if restore_download:
        require(not restore_proxy and include_thumbnail, 'restore must preserve independent workers and HTML')
        runtime, runtime_new = prepare_audio_runtime(root, environment)
        try:
            comparison = restore_smoke(source, environment, baseline=old)
        except BaseException:
            if runtime_new:
                shutil.rmtree(runtime)
            raise
        proxy_path = root / 'data/proxy_settings.json'
        before = proxy_path.read_bytes()
        require(hashlib.sha256(before).hexdigest() == comparison['config_sha256'],
                'proxy changed during controlled comparison')
        cfg = json.loads(before)
        enabled = comparison['verified_route'] == 'selected_proxy'
        if cfg.get('enabled') != enabled:
            cfg['enabled'] = enabled
            st = proxy_path.stat()
            proxy_plan = {'path': proxy_path, 'before': before,
                          'after': (json.dumps(cfg) + '\n').encode(),
                          'uid': st.st_uid, 'gid': st.st_gid}
    # Transactional idle fence closes the enqueue/claim race before web shutdown.
    fence = lock_idle(root / 'data/jobs.sqlite3')
    switched = False
    stopped = False
    proxy_changed = False
    deno_new = False
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
        # Proxy changes occur only after idle fence, service stop and backup.
        if proxy_plan is not None:
            require(proxy_plan['path'].read_bytes() == proxy_plan['before'],
                    'saved proxy changed since canary')
            (journal / 'proxy-before.json').write_bytes(proxy_plan['before'])
            (journal / 'proxy-before.json').chmod(0o600)
            replace_private(proxy_plan, proxy_plan['after'])
            proxy_changed = True
            event(app='audio-downloader', proxy_restore='VERIFIED_ROUTE_SELECTED' if restore_download else 'CANARY_PASS_ENABLED', credentials_logged=False)
        deno_new = ensure_audio_js(environment) if restore_proxy else False
        shutil.copytree(previous, target, symlinks=True)
        (target / MODULE).write_bytes(payload)
        if include_thumbnail:
            installed_page = previous / PAGE
            source_page = REPO / 'apps/voice-studio' / PAGE
            require(installed_page.is_file() and not installed_page.is_symlink() and
                    source_page.is_file() and not source_page.is_symlink(),
                    'Voice HTML not a trusted regular file')
            html = audio_thumbnail_overlay(installed_page.read_bytes(), source_page.read_bytes())
            (target / PAGE).write_bytes(html)
        manifest_file = target / '_production_manifest.json'
        require(manifest_file.is_file() and not manifest_file.is_symlink(), 'manifest missing')
        manifest = json.loads(manifest_file.read_text())
        require(manifest.get('app') == 'voice-studio' and str(MODULE) in manifest.get('files', {}), 'manifest identity invalid')
        manifest['files'][str(MODULE)] = hashlib.sha256(payload).hexdigest()
        if include_thumbnail:
            require(str(PAGE) in manifest.get('files', {}), 'Voice template not in release manifest')
            manifest['files'][str(PAGE)] = hashlib.sha256(html).hexdigest()
        overlay_name = 'audio_thumbnail_overlay' if include_thumbnail else ('audio_proxy_overlay' if restore_proxy else 'audio_filename_overlay')
        manifest.update(revision=revision, **{overlay_name: str(previous)})
        atomic_json(manifest_file, manifest, 0o644)
        atomic_link(CURRENT, target)
        switched = True
        run(['systemctl', 'start', UNIT], quiet=True, timeout=60)
        http_check('voice-studio')
        verify()
        if diagnostic_only:
            result = restore_smoke(target / MODULE, environment, allow_download_failure=True)
        elif restore_download:
            result = restore_smoke(target / MODULE, environment)
            route = 'selected_proxy' if json.loads((root / 'data/proxy_settings.json').read_text()).get('enabled') else 'direct'
            require(result['routes'][route]['candidate']['result'] == 'PASS', 'active deployed route download failed')
        else:
            result = smoke(target, environment)
        atomic_json(journal / 'result.json', {'state': 'ACTIVE', 'revision': revision, 'smoke': result})
        result_name = 'PASS_AUDIO_ERROR_LABEL_ONLY' if diagnostic_only else 'PASS_AUDIO_RESTORE_ONLY' if restore_download else 'PASS_AUDIO_THUMBNAIL_ONLY' if include_thumbnail else ('PASS_AUDIO_PROXY_ONLY' if restore_proxy else 'PASS_AUDIO_FILENAME_ONLY')
        event(app='voice-studio', result=result_name, workers='PRESERVED', data='PRESERVED',
              https='PASS', sqlite_backup='PASS', proxy_restore=('VERIFIED_ROUTE_SELECTED' if restore_download else 'ENABLED') if proxy_changed else 'UNCHANGED', smoke=result, rollback=str(journal))
    except BaseException:
        if restore_download and runtime_new:
            shutil.rmtree(runtime, ignore_errors=True)
        if proxy_changed:
            replace_private(proxy_plan, proxy_plan['before'])
            event(app='audio-downloader', proxy_restore='ROLLED_BACK', credentials_logged=False)
        if deno_new:
            (Path(environment['PATH'].split(':')[0]) / 'deno').unlink(missing_ok=True)
            event(app='audio-downloader', js_runtime='NEW_BINARY_REMOVED_ON_ROLLBACK')
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

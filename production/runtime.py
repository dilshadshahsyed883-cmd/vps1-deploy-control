#!/usr/bin/env python3
"""Install immutable application releases on VPS_1 through its Drive agent.

Runtime data and credentials stay on the host. A failed app is restored before
the next app runs. This module deliberately does not change GitHub SSH jobs,
the existing Scene Builder pilot, Storage Manager or Auto Script.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

REPO = Path(__file__).resolve().parents[1]
BASE = Path('/srv/fresh-website')
DATA = Path('/var/lib/fresh-website')
CONFIG = Path('/etc/fresh-website')
RCLONE = '/home/ubuntu/.config/masterctl/rclone.conf'
CORE = ['fastapi==0.115.12', 'uvicorn==0.34.2', 'python-multipart==0.0.20',
        'pillow==11.1.0', 'requests==2.32.3', 'jinja2==3.1.6',
        'itsdangerous==2.2.0', 'httpx==0.28.1']
CATALOG = {
    'scene-builder': dict(port=8080, module='production.scene_entry:app', health='/health',
                          ui='/', runtime='scene-builder-production',
                          packages=CORE + ['playwright==1.56.0', 'websockets==15.0.1']),
    'short-maker': dict(port=8146, module='app:app', health='/health', ui='/', packages=CORE),
    'short-stream': dict(port=8145, module='app:app', health='/health', ui='/',
                         packages=CORE + ['cryptography==44.0.3']),
    'self-media-library': dict(port=8147, module='app:app', health='/api/health', ui='/', packages=CORE + ['numpy==2.2.6', 'onnxruntime==1.22.1', 'tokenizers==0.21.2', 'faster-whisper==1.1.1']),
    'video-merger': dict(port=8148, script='app.py', health='/video-merger/health',
                         ui='/video-merger/', packages=['yt-dlp[default]', 'gdown==5.2.0', 'requests==2.32.3']),
    'voice-studio': dict(port=8143, module='vps1_omnivoice_app:app', health='/healthz',
                         ui='/', packages=CORE + ['edge-tts==7.3.1', 'kokoro-onnx==0.6.0', 'soundfile', 'numpy', 'yt-dlp']),
    'auto-caption-studio': dict(port=8144, module='web_backend.app:app', health='/health',
                                ui='/', packages=CORE + ['PySide6==6.8.3', 'faster-whisper==1.1.1']),
    'local-upload-manager': dict(port=8015, module='lum_engine.standalone_app:app',
                                 health='/local-upload-manager/health', ui='/local-upload-manager/app',
                                 requirements='part01/requirements.txt'),
}


def runtime_name(app):
    return CATALOG[app].get('runtime', app)


def event(**values):
    print(json.dumps(values, sort_keys=True), flush=True)


def run(args, *, cwd=None, timeout=300, quiet=False):
    result = subprocess.run([str(a) for a in args], cwd=cwd, timeout=timeout,
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode:
        # Package-install errors contain no environment values; never log env files.
        raise RuntimeError('command failed: ' + ' '.join(str(x) for x in args[:3]) +
                           '\n' + result.stdout[-3500:])
    if not quiet and result.stdout.strip():
        event(command=str(args[0]), output=result.stdout[-1200:])
    return result.stdout


def atomic_json(path, value, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name('.' + path.name + '.new')
    with tmp.open('w') as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write('\n')
    tmp.chmod(mode)
    os.replace(tmp, path)


def atomic_link(path, target):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name('.' + path.name + '.new')
    if tmp.is_symlink():
        tmp.unlink()
    elif tmp.exists():
        raise RuntimeError('unexpected temporary path: ' + str(tmp))
    tmp.symlink_to(target)
    os.replace(tmp, path)


def free_port(port):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', port))


def check_host(revision):
    if os.geteuid() != 0 or socket.gethostname() != 'vps-01a23801':
        raise RuntimeError('installer requires approved VPS_1 root execution via Bridge')
    if not re.fullmatch('[a-f0-9]{40}', revision):
        raise ValueError('an exact 40-character Git revision is required')
    if run(['git', 'rev-parse', 'HEAD'], cwd=REPO, quiet=True).strip() != revision:
        raise RuntimeError('checkout does not match approved source revision')
    if shutil.disk_usage('/').free < 5_000_000_000:
        raise RuntimeError('5 GB operational reserve is unavailable')
    if pwd.getpwnam('ubuntu').pw_uid != 1000:
        raise RuntimeError('unexpected existing runtime account')


def environment(app, state):
    env = {'PYTHONDONTWRITEBYTECODE': '1', 'FRESH_WEBSITE_INSTALL': '1',
           'RCLONE_CONFIG': RCLONE}
    if app == 'scene-builder':
        credentials = Path('/root/vps1-production-login.json')
        password = json.loads(credentials.read_text())['password'] if os.geteuid() == 0 and credentials.is_file() else secrets.token_urlsafe(32)
        env.update(WEB_DATA_DIR=str(state / 'web'), SCENE_PERMANENT_ROOT=str(state / 'permanent'),
                   SCENE_CHANNELS_FILE=str(state / 'scene_channels.json'),
                   SCRIPT_WORK_DATA_DIR=str(state / 'script-work'), SCRIPT_PROCESSOR_DATA_DIR=str(state / 'script-processor'),
                   MASTER_CONTROLLER_DB_PATH=str(state / 'master-controller.sqlite3'),
                   AUTOFLOW_FLOW_RESULT_ROOT=str(state / 'flow-results'), FRESH_AUTO_SCRIPT_READY='0',
                   WEB_USERNAME='admin', WEB_ACCESS_KEY=password, SESSION_SECRET=secrets.token_urlsafe(48),
                   PYTHONPATH=str(REPO), STORAGE_MANAGER_URL='http://127.0.0.1:8149')
        token_file = CONFIG / 'vps-storage-manager.env'
        if os.geteuid() == 0 and token_file.is_file():
            for line in token_file.read_text().splitlines():
                if line.startswith('STORAGE_MANAGER_TOKEN='):
                    env['STORAGE_MANAGER_TOKEN'] = line.split('=', 1)[1]
    elif app == 'short-maker':
        env['SHORT_MAKER_DATA'] = str(state)
    elif app == 'short-stream':
        env['SHORT_STREAM_DATA'] = str(state)
    elif app == 'self-media-library':
        env['SML_DATA_ROOT'] = str(state / 'sml')
    elif app == 'video-merger':
        env.update(VIDEO_MERGER_ROOT=str(state / 'video-merger'),
                   VIDEO_MERGER_PROXY_CONFIG_DIR=str(state / 'video-merger/config'),
                   VIDEO_MERGER_PORT='8148')
    elif app == 'voice-studio':
        for key, child in [('VOICE_CLONE_ROOT', 'omnivoice'), ('KOKORO_STUDIO_ROOT', 'kokoro'),
                           ('EDGE_STUDIO_ROOT', 'edge'), ('AUDIO_DOWNLOADER_ROOT', 'audio-downloader')]:
            env[key] = str(state / child)
    elif app == 'auto-caption-studio':
        env.update(AUTO_CAPTION_APP_ROOT=str(state / 'acs'),
                   AUTO_CAPTION_DB_PATH=str(state / 'acs/data/database/auto_caption.db'),
                   AUTO_CAPTION_CODE_ROOT=str(BASE / 'current' / app / 'part01'))
    elif app == 'local-upload-manager':
        env.update(LUM_DATA_DIR=str(state / 'lum1'),
                   YOUTUBE_UPLOAD_MANAGER_DATA_DIR=str(state / 'lum1/core'),
                   LUM_AUTO_ENGINE_V6_ROOT=str(state / 'lum1/auto_engine_v6'),
                   LUM_GOOGLE_OAUTH_ROOT=str(state / 'lum1/secure'),
                   LUM_SESSION_SECRET=secrets.token_urlsafe(48),
                   LUM_BASE_PATH='/local-upload-manager', LUM_SUITE_AUTH='1',
                   LUM_SUITE_LOGIN_URL='/', LUM_COOKIE_SECURE='0')
    return env


def service_unit(app, revision):
    spec = CATALOG[app]
    python = BASE / 'venvs' / app / revision / 'bin/python'
    command = (f'{python} {spec["script"]}' if 'script' in spec else
               f'{python} -m uvicorn {spec["module"]} --host 127.0.0.1 --port {spec["port"]}')
    return f'''[Unit]
Description=VPS1 production {app}
After=network-online.target
StartLimitIntervalSec=120
StartLimitBurst=5

[Service]
Type=simple
User=ubuntu
Group=ubuntu
WorkingDirectory={BASE}/current/{runtime_name(app)}/part01
EnvironmentFile={CONFIG}/production-{app}.env
Environment=PATH={BASE}/venvs/{app}/{revision}/bin:/usr/local/bin:/usr/bin:/bin
ExecStart={command}
Restart=on-failure
RestartSec=3
TimeoutStopSec=35
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths={DATA}/{runtime_name(app)} -/home/ubuntu/.config/masterctl

[Install]
WantedBy=multi-user.target
'''


def http_check(app, timeout=45):
    spec = CATALOG[app]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{spec["port"]}{spec["health"]}', timeout=3) as r:
                body = json.loads(r.read(131072))
                if r.status == 200 and body.get('ok') is True:
                    return body
        except (OSError, ValueError):
            pass
        time.sleep(.4)
    raise RuntimeError('app health failed: ' + app)


def require_voice_release_idle(state: Path) -> None:
    """Fail closed on Voice synthesis and active uploads before any worker restart.

    Completed, failed and cancelled historical rows are preserved. A pending
    Drive upload may survive worker replacement; a transferring file may not.
    """
    engines = (
        (state / 'kokoro/data/kokoro_jobs.sqlite3', 'Kokoro'),
        (state / 'edge/data/edge_jobs.sqlite3', 'Edge'),
    )
    for database, label in engines:
        if not database.is_file():
            raise RuntimeError(label + ' DB unavailable for release preflight')
        with sqlite3.connect('file:' + str(database) + '?mode=ro', uri=True, timeout=4) as db:
            db.execute('PRAGMA query_only=ON')
            columns = {row[1] for row in db.execute('PRAGMA table_info(jobs)')}
            if not {'status', 'delivery_status'} <= columns:
                raise RuntimeError(label + ' job schema invalid for release preflight')
            count = db.execute("""SELECT count(*) FROM jobs
                  WHERE status IN ('queued','processing','paused','pause_requested',
                    'cancel_requested','retry_wait')
                    OR delivery_status='transferring'""").fetchone()[0]
            if count:
                raise RuntimeError(label + ' has pending/in-flight synthesis or upload; release postponed')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = 'http://127.0.0.1:8143/tts/api/audio-downloader/status'
    try:
        with opener.open(url, timeout=5) as response:
            status = json.loads(response.read(32768))
            if response.status != 200 or not isinstance(status, dict) or not isinstance(status.get('queue'), dict):
                raise RuntimeError('Audio Downloader preflight malformed')
            queue = status['queue']
            if any(int(queue.get(x) or 0) for x in (
                    'queued','downloading','downloaded','processing','converting','uploading','retry_pending')):
                raise RuntimeError('Audio Downloader has active/pending jobs; release postponed')
    except (OSError, ValueError) as exc:
        raise RuntimeError('Audio Downloader preflight unreachable') from exc


def backup_databases(state, destination):
    """SQLite backup includes committed WAL transactions; media stays in place."""
    outputs = []
    for source in sorted(state.rglob('*')):
        if source.suffix not in {'.db', '.sqlite', '.sqlite3'} or not source.is_file():
            continue
        if source.is_symlink():
            raise RuntimeError('database symlink outside app state')
        relative = source.relative_to(state)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
            src.backup(dst)
            if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('SQLite backup integrity failed')
        target.chmod(0o600)
        outputs.append(str(relative))
    return outputs


def stage_source(app, revision):
    # Reuse the repository's tested source-only export and credential exclusion.
    from rebuild.fresh_vps import sources
    release = BASE / 'releases' / runtime_name(app) / revision
    files = sources(app)
    manifest = {'app': app, 'revision': revision,
                'files': {n: hashlib.sha256(b).hexdigest() for n, b in files}}
    if release.exists():
        if not (release / '_production_manifest.json').is_file():
            raise RuntimeError('refusing to adopt another installer release')
        if json.loads((release / '_production_manifest.json').read_text()) != manifest:
            raise RuntimeError('immutable source manifest mismatch')
        for name, digest in manifest['files'].items():
            if hashlib.sha256((release / name).read_bytes()).hexdigest() != digest:
                raise RuntimeError('installed source drift')
    else:
        release.mkdir(parents=True)
        for name, content in files:
            target = release / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            target.chmod(0o644)
        atomic_json(release / '_production_manifest.json', manifest, 0o644)
    return release


def restore(app, previous):
    unit_name = f'vps1-production-{app}.service'
    unit = Path('/etc/systemd/system') / unit_name
    current = BASE / 'current' / runtime_name(app)
    from production import workers
    workers.stop(app)
    subprocess.run(['systemctl', 'stop', unit_name], capture_output=True)
    if previous['unit'] is None:
        subprocess.run(['systemctl', 'disable', unit_name], capture_output=True)
        unit.unlink(missing_ok=True)
    else:
        unit.write_text(previous['unit'])
    if previous['current']:
        atomic_link(current, previous['current'])
    elif current.is_symlink():
        current.unlink()
    run(['systemctl', 'daemon-reload'], quiet=True)
    if previous['active']:
        run(['systemctl', 'start', unit_name], quiet=True)
        http_check(app)
    workers.restore(previous.get('workers', {}))
    event(app=app, rollback='PASS', data='PRESERVED')


def require_lum_release_idle(state: Path) -> None:
    """Fail closed before restarting any uploader web/worker process.

    The three web instances expose in-flight project IDs. Check their exact
    installed loopback routes, then read the separate SQLite video queues
    without modifying them. Queued/scheduled work remains preserved on disk.
    """
    modes = (
        ("engine1", 8015, "/local-upload-manager/health", "lum1"),
        ("engine2", 8016, "/local-upload-manager/modes/engine2/health", "lum2"),
        ("shorts", 8017, "/local-upload-manager/modes/shorts/health", "lum3"),
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    in_flight = ("CLAIMED", "UPLOADING", "THUMBNAIL_UPLOADING",
                 "PROCESSING", "IN_PROGRESS", "WAITING_NETWORK")
    for mode, port, route, leaf in modes:
        try:
            with opener.open(f"http://127.0.0.1:{port}{route}", timeout=3) as response:
                if response.status != 200:
                    raise RuntimeError("engine did not return HTTP 200")
                health = json.loads(response.read(4096))
            if (health.get("ok") is not True or
                    health.get("engine_instance") != mode):
                raise RuntimeError("engine identity/readiness mismatch")
            if health.get("active_project_id") not in (None, "", 0):
                raise RuntimeError("in-flight web upload")
            database = state / leaf / "core" / "data" / "uploader.db"
            if database.is_file():
                with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True,
                                     timeout=3) as conn:
                    conn.execute("PRAGMA query_only=ON")
                    has_videos = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='videos'"
                    ).fetchone()
                    if has_videos:
                        matches = conn.execute(
                            "SELECT 1 FROM videos WHERE UPPER(status) IN (" +
                            ",".join("?" for _ in in_flight) + ") LIMIT 1",
                            in_flight,
                        ).fetchone()
                        if matches:
                            raise RuntimeError("in-flight SQLite upload")
        except (OSError, ValueError, TypeError, sqlite3.Error, RuntimeError) as exc:
            raise RuntimeError(
                "LUM_RELEASE_BLOCKED: uploader " + mode +
                " is busy or its idle state cannot be proven (" +
                type(exc).__name__ + ")"
            ) from exc
    event(app="local-upload-manager", uploader_idle_preflight="PASS",
          checked_modes=[mode for mode, *_ in modes])


def require_sml_idle(state):
    database = Path(state) / 'sml/library.db'
    if not database.is_file():return
    with sqlite3.connect(database.as_uri() + '?mode=ro',uri=True,timeout=5) as c:
        c.execute('PRAGMA query_only=ON')
        for table,where in (('sources',"status='processing'"),('media_imports',"status='processing'"),('ai_jobs',"state='running'")):
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():
                if c.execute('SELECT 1 FROM '+table+' WHERE '+where+' LIMIT 1').fetchone():
                    raise RuntimeError('SML_RELEASE_BLOCKED: active '+table+' work; retry after completion')


def install_app(app, revision):
    if app not in CATALOG:
        raise ValueError('app is not owned by this installer')
    if app == 'video-merger':
        intent = run(['git', 'log', '-1', '--format=%B'], cwd=REPO, quiet=True)
        if '[vps1-video-merger-import-release]' in intent:
            from production.video_merger_import_release import install
            install(revision)
            return
    # The public Scene service is the original isolated candidate on port 18080,
    # not the later generic port-8080 service.  Keep its unit/env/data ownership
    # and update only its immutable source pointer through the same root helper.
    if app == 'scene-builder' and (BASE / 'current/scene-builder').is_symlink():
        from production.scene_candidate_release import install as install_scene_candidate
        install_scene_candidate(revision)
        return
    if app in {'short-maker', 'video-merger', 'auto-caption-studio'}:
        commit_message = run(['git', 'log', '-1', '--format=%B'], cwd=REPO, quiet=True)
        if '[vps1-video-template-only]' in commit_message:
            from production.video_template_release import install as install_video_template
            install_video_template(app, revision)
            return
    # Explicit protected-main Voice template-only intent: never restart or
    # provision independent synthesis workers just to change sidebar markup.
    if app == 'voice-studio':
        commit_message = run(['git', 'log', '-1', '--format=%B'], cwd=REPO, quiet=True)
        if '[vps1-voice-edge-recovery-only]' in commit_message:
            from production.voice_edge_worker_recovery import install as repair_edge_worker
            repair_edge_worker(revision)
            return
        if '[vps1-voice-kokoro-recovery-only]' in commit_message:
            from production.voice_kokoro_worker_recovery import install as repair_kokoro_worker
            repair_kokoro_worker(revision)
            return
        if '[vps1-audio-proxy-only]' in commit_message:
            from production.audio_downloader_release import install as install_audio_proxy
            install_audio_proxy(revision)
            return
        if '[vps1-voice-tts-controls-only]' in commit_message:
            from production.voice_tts_controls_release import install as install_voice_tts_controls
            install_voice_tts_controls(revision)
            return
        if '[vps1-voice-queue-api-only]' in commit_message:
            from production.voice_queue_release import install as install_voice_queue_api
            install_voice_queue_api(revision)
            return
        if '[vps1-voice-template-only]' in commit_message:
            from production.voice_template_release import install as install_voice_template
            install_voice_template(revision)
            return
    # Owner-approved website re-audit touches exact web files only. Workers stay live.
    if app in {'auto-caption-studio','voice-studio','self-media-library','short-stream','video-merger'}:
        commit_message = run(['git', 'log', '-1', '--format=%B'], cwd=REPO, quiet=True)
        if '[vps1-website-reaudit-release]' in commit_message:
            from production.website_reaudit_release import install as install_website_reaudit
            install_website_reaudit(app, revision)
            return
    unit_name = f'vps1-production-{app}.service'
    unit = Path('/etc/systemd/system') / unit_name
    current = BASE / 'current' / runtime_name(app)
    state = DATA / runtime_name(app)
    record = BASE / 'control/state' / (app + '.json')
    if current.exists() and not current.is_symlink():
        raise RuntimeError('existing app path is not a release pointer')
    if current.is_symlink() and not record.is_file():
        raise RuntimeError('existing app belongs to a different installer')
    old = str(current.resolve(strict=True)) if current.is_symlink() else None
    if old and not Path(old).is_relative_to(BASE / 'releases' / runtime_name(app)):
        raise RuntimeError('existing pointer escaped app releases')
    active = subprocess.run(['systemctl', 'is-active', '--quiet', unit_name]).returncode == 0
    if not active:
        free_port(CATALOG[app]['port'])
    previous = {'current': old, 'unit': unit.read_text() if unit.exists() else None, 'active': active}
    from production import workers
    previous['workers'] = workers.snapshot(app)
    if old == str(BASE / 'releases' / runtime_name(app) / revision) and active:
        stage_source(app, revision)
        http_check(app)
        event(app=app, result='PASS_ALREADY_INSTALLED', revision=revision)
        return
    release = stage_source(app, revision)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chown(state, 1000, 1000)
    env_file = CONFIG / f'production-{app}.env'
    CONFIG.mkdir(parents=True, exist_ok=True)
    if not env_file.exists():
        env = environment(app, state)
        fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(''.join(k + '=' + v + '\n' for k, v in env.items()))
    if app == 'short-stream':
        from production.auto_stream_oauth_env import ensure_key
        key_status = ensure_key(env_file, state)
        event(app=app, oauth_vault_key=key_status, secrets_logged=False)
    venv = BASE / 'venvs' / app / revision
    venv.parent.mkdir(parents=True, exist_ok=True)
    if not (venv / 'bin/python').is_file():
        venv.mkdir(exist_ok=True)
        os.chown(venv, 1000, 1000)
        run(['runuser', '-u', 'ubuntu', '--', 'python3', '-m', 'venv', venv], quiet=True)
    pip = [venv / 'bin/python', '-m', 'pip', 'install', '--disable-pip-version-check', '--no-cache-dir']
    packages = CATALOG[app].get('packages')
    args = packages if packages else ['-r', release / CATALOG[app]['requirements']]
    run(['runuser', '-u', 'ubuntu', '--', *pip, *args, 'httpx==0.28.1'], timeout=900, quiet=True)
    if app == 'video-merger':
        from production.video_merger_import_release import install_js
        install_js(venv)
    freeze = run([venv / 'bin/python', '-m', 'pip', 'freeze'], quiet=True)
    (venv / 'installed-requirements.txt').write_text(freeze)
    # Exercise real startup/UI and a bounded local render before changing live pointers.
    run(['runuser', '-u', 'ubuntu', '--', venv / 'bin/python', '-m',
         'production.smoke', '--app', app], cwd=REPO, timeout=180, quiet=True)
    if app == 'self-media-library':
        intent = run(['git', 'log', '-1', '--format=%B'], cwd=REPO, quiet=True)
        if '[vps1-sml-ai-release]' in intent:
            models = state / 'sml' / 'ai_models'
            models.mkdir(parents=True, exist_ok=True)
            os.chown(models.parent, 1000, 1000)
            os.chown(models, 1000, 1000)
            run(['runuser','-u','ubuntu','--',venv / 'bin/python',
                 release / 'part01/ai_engine.py','--prepare',models],timeout=600,quiet=True)
            if not shutil.which('tesseract') or not Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf').is_file():
                run(['apt-get','update','-qq'],timeout=300,quiet=True)
                run(['apt-get','install','-y','--no-install-recommends','tesseract-ocr','fonts-dejavu-core'],timeout=300,quiet=True)
            speech_models = state / 'sml/speech_models'
            speech_models.mkdir(exist_ok=True)
            os.chown(speech_models,1000,1000)
            run(['runuser','-u','ubuntu','--',venv / 'bin/python',
                 release / 'part01/ai_text.py','--prepare',speech_models],timeout=600,quiet=True)
            run(['runuser','-u','ubuntu','--',venv / 'bin/python','-m',
                 'production.sml_ai_verify','--model-cache',models],cwd=REPO,timeout=240,quiet=True)
    if app == 'video-merger':
        media = state / 'video-merger'
        media.mkdir(exist_ok=True)
        os.chown(media, 1000, 1000)
        if not (media / 'venv').exists():
            (media / 'venv').symlink_to(venv)
    journal = BASE / 'transactions' / app / (revision + '-' + str(time.time_ns()))
    journal.mkdir(parents=True)
    atomic_json(journal / 'previous.json', previous)
    # Never interrupt an upload in any of the three independently running engines.
    if app == 'local-upload-manager' and active:
        require_lum_release_idle(state)
    if app == 'voice-studio' and active:
        require_voice_release_idle(state)
    # Only the app being replaced is stopped. Other apps and all agents stay up.
    if app == 'self-media-library' and active:
        require_sml_idle(state)
    if active:
        run(['systemctl', 'stop', unit_name], quiet=True)
    workers.stop(app)
    changed = False
    try:
        backups = backup_databases(state, journal / 'sqlite-backup')
        changed = True
        atomic_link(current, release)
        unit.write_text(service_unit(app, revision))
        unit.chmod(0o644)
        run(['systemctl', 'daemon-reload'], quiet=True)
        run(['systemd-analyze', 'verify', unit], quiet=True)
        run(['systemctl', 'enable', unit_name], timeout=60, quiet=True)
        run(['systemctl', 'restart', unit_name], timeout=60, quiet=True)
        health = http_check(app)
        workers.install(app, revision)
        if app == 'scene-builder':
            from production.browser_runtime import install as install_browser
            install_browser()
        if shutil.disk_usage('/').free < 5_000_000_000:
            raise RuntimeError('operational disk reserve breached')
        atomic_json(record, {'revision': revision, 'unit': unit_name, 'previous': previous,
                             'transaction': str(journal), 'status': 'ACTIVE'})
        event(app=app, result='PASS_SERVICE_HEALTH', revision=revision, port=CATALOG[app]['port'],
              sqlite_backups=len(backups), health=health, rollback=str(journal))
    except BaseException:
        if changed or active:
            restore(app, previous)
        raise


@contextlib.contextmanager
def deployment_lock():
    with open('/run/lock/vps1-production.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['install', 'status', 'rollback'])
    parser.add_argument('--revision', required=True)
    parser.add_argument('--apps', nargs='+', choices=sorted(CATALOG), default=list(CATALOG))
    args = parser.parse_args()
    check_host(args.revision)
    with deployment_lock():
        for app in args.apps:
            if args.action == 'install':
                intent = run(['git', 'log', '-1', '--format=%B'], cwd=REPO, quiet=True)
                activate_sml_scene = (
                    app == 'self-media-library'
                    and '[vps1-sml-scene-activate]' in intent
                )
                if not activate_sml_scene:
                    install_app(app, args.revision)
                else:
                    # One narrow intent: install SML source, then link only
                    # SML + Scene web Editor credentials using a VPS-minted
                    # token. Roll back SML source if activation fails.
                    install_app(app, args.revision)
                    try:
                        from production.sml_scene_activation import activate
                        activated = activate()
                        event(sml_scene_link="EDITOR_API_READY",
                              runtime_render_worker="NOT_VERIFIED",
                              secrets_logged=False, **activated)
                    except BaseException:
                        record_path = BASE / 'control/state' / (app + '.json')
                        if record_path.is_file():
                            record = json.loads(record_path.read_text())
                            if record.get('revision') == args.revision:
                                restore(app, record['previous'])
                        raise
            elif args.action == 'status':
                event(app=app, health=http_check(app))
            else:
                record = json.loads((BASE / 'control/state' / (app + '.json')).read_text())
                restore(app, record['previous'])


if __name__ == '__main__':
    main()

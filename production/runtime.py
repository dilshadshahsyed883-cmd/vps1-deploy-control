#!/usr/bin/env python3
"""Install immutable application releases on VPS_1 through its Drive agent.

Runtime data and credentials stay on the host. A failed app is restored before
the next app runs. This module deliberately does not change GitHub SSH jobs,
Scene Builder, the existing Storage Manager or Auto Script.
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
    'short-maker': dict(port=8146, module='app:app', health='/health', ui='/', packages=CORE),
    'short-stream': dict(port=8145, module='app:app', health='/health', ui='/', packages=CORE),
    'self-media-library': dict(port=8147, module='app:app', health='/api/health', ui='/', packages=CORE),
    'video-merger': dict(port=8148, script='app.py', health='/video-merger/health',
                         ui='/video-merger/', packages=['yt-dlp']),
    'voice-studio': dict(port=8143, module='vps1_omnivoice_app:app', health='/healthz',
                         ui='/', packages=CORE + ['edge-tts==7.2.8', 'soundfile', 'numpy', 'yt-dlp']),
    'auto-caption-studio': dict(port=8144, module='web_backend.app:app', health='/health',
                                ui='/', packages=CORE),
    'local-upload-manager': dict(port=8015, module='lum_engine.standalone_app:app',
                                 health='/local-upload-manager/health', ui='/local-upload-manager/app',
                                 requirements='part01/requirements.txt'),
}


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
    if app == 'short-maker':
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
WorkingDirectory={BASE}/current/{app}/part01
EnvironmentFile={CONFIG}/production-{app}.env
ExecStart={command}
Restart=on-failure
RestartSec=3
TimeoutStopSec=35
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths={DATA}/{app} -/home/ubuntu/.config/masterctl

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
    release = BASE / 'releases' / app / revision
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
    current = BASE / 'current' / app
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
    event(app=app, rollback='PASS', data='PRESERVED')


def install_app(app, revision):
    if app not in CATALOG:
        raise ValueError('app is not owned by this installer')
    unit_name = f'vps1-production-{app}.service'
    unit = Path('/etc/systemd/system') / unit_name
    current = BASE / 'current' / app
    state = DATA / app
    record = BASE / 'control/state' / (app + '.json')
    if current.exists() and not current.is_symlink():
        raise RuntimeError('existing app path is not a release pointer')
    if current.is_symlink() and not record.is_file():
        raise RuntimeError('existing app belongs to a different installer')
    old = str(current.resolve(strict=True)) if current.is_symlink() else None
    if old and not Path(old).is_relative_to(BASE / 'releases' / app):
        raise RuntimeError('existing pointer escaped app releases')
    active = subprocess.run(['systemctl', 'is-active', '--quiet', unit_name]).returncode == 0
    if not active:
        free_port(CATALOG[app]['port'])
    previous = {'current': old, 'unit': unit.read_text() if unit.exists() else None, 'active': active}
    if old == str(BASE / 'releases' / app / revision) and active:
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
    freeze = run([venv / 'bin/python', '-m', 'pip', 'freeze'], quiet=True)
    (venv / 'installed-requirements.txt').write_text(freeze)
    # Exercise real startup/UI and a bounded local render before changing live pointers.
    run(['runuser', '-u', 'ubuntu', '--', venv / 'bin/python', '-m',
         'production.smoke', '--app', app], cwd=REPO, timeout=180, quiet=True)
    if app == 'video-merger':
        media = state / 'video-merger'
        media.mkdir(exist_ok=True)
        os.chown(media, 1000, 1000)
        if not (media / 'venv').exists():
            (media / 'venv').symlink_to(venv)
    journal = BASE / 'transactions' / app / (revision + '-' + str(time.time_ns()))
    journal.mkdir(parents=True)
    atomic_json(journal / 'previous.json', previous)
    # Only the app being replaced is stopped. Other apps and all agents stay up.
    if active:
        run(['systemctl', 'stop', unit_name], quiet=True)
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
                install_app(app, args.revision)
            elif args.action == 'status':
                event(app=app, health=http_check(app))
            else:
                record = json.loads((BASE / 'control/state' / (app + '.json')).read_text())
                restore(app, record['previous'])


if __name__ == '__main__':
    main()

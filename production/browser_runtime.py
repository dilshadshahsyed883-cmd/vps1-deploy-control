"""Dedicated Script Work Chrome display, CDP and noVNC services."""
from __future__ import annotations

import os
from pathlib import Path
import pwd
import secrets
import shutil
import socket
import subprocess
import time
import urllib.request

from production.runtime import DATA, event, run

ROOT = DATA / 'script-work-production'
USER = 'vps1browser'
NAMES = ['script-xvfb', 'script-chrome', 'script-vnc', 'script-novnc']


def service(name, command, *, after='', browser=False):
    dependency = f'After=vps1-production-{after}.service\nRequires=vps1-production-{after}.service\n' if after else ''
    return f'''[Unit]
Description=VPS1 Script Work {name}
After=network-online.target
{dependency}
[Service]
User={USER}
Group={USER}
Environment=DISPLAY=:91
Environment=XAUTHORITY={ROOT}/Xauthority
Environment=XDG_CACHE_HOME={ROOT}/cache
ExecStart={command}
Restart=on-failure
RestartSec=3
TimeoutStopSec=25
UMask=0077
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths={ROOT} /tmp
PrivateTmp=no
{'NoNewPrivileges=yes' if not browser else '# Chrome uses its vendor-provided sandbox helper.'}
[Install]
WantedBy=multi-user.target
'''


def install():
    if not shutil.which('google-chrome-stable'):
        package = Path('/tmp/vps1-google-chrome-stable.deb')
        run(['curl', '--fail', '--silent', '--show-error', '--location', '--proto', '=https',
             '--max-time', '180', 'https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb',
             '-o', package], timeout=190, quiet=True)
        run(['apt-get', '-o', 'DPkg::Lock::Timeout=180', 'install', '-y', str(package)], timeout=600, quiet=True)
        package.unlink(missing_ok=True)
    run(['apt-get', '-o', 'DPkg::Lock::Timeout=180', 'install', '-y', '--no-install-recommends',
         'xvfb', 'x11vnc', 'novnc', 'websockify', 'xauth', 'fonts-dejavu-core'], timeout=600, quiet=True)
    try:
        account = pwd.getpwnam(USER)
    except KeyError:
        run(['useradd', '--system', '--user-group', '--home-dir', ROOT,
             '--shell', '/usr/sbin/nologin', USER], quiet=True)
        account = pwd.getpwnam(USER)
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    ROOT.chmod(0o700)
    os.chown(ROOT, account.pw_uid, account.pw_gid)
    for name in ['profile', 'cache', 'downloads']:
        path = ROOT / name
        path.mkdir(exist_ok=True)
        os.chown(path, account.pw_uid, account.pw_gid)
    authority = ROOT / 'Xauthority'
    if not authority.is_file():
        authority.touch(mode=0o600)
        run(['xauth', '-f', authority, 'add', ':91', '.', secrets.token_hex(16)], quiet=True)
        os.chown(authority, account.pw_uid, account.pw_gid)
    run(['install', '-d', '-m', '1777', '/tmp/.X11-unix'], quiet=True)
    commands = {
      'script-xvfb': (f'/usr/bin/Xvfb :91 -screen 0 1440x900x24 -auth {authority} -nolisten tcp', ''),
      'script-chrome': (f'/usr/bin/google-chrome-stable --user-data-dir={ROOT}/profile '
                         '--remote-debugging-address=127.0.0.1 --remote-debugging-port=9223 '
                         '--disable-dev-shm-usage --no-first-run --no-default-browser-check about:blank', 'script-xvfb'),
      'script-vnc': (f'/usr/bin/x11vnc -display :91 -auth {authority} -localhost '
                      '-rfbport 5901 -forever -shared -nopw', 'script-xvfb'),
      'script-novnc': ('/usr/bin/websockify --web=/usr/share/novnc 127.0.0.1:6081 127.0.0.1:5901', 'script-vnc'),
    }
    previous = {}
    for name in NAMES:
        path = Path('/etc/systemd/system') / ('vps1-production-' + name + '.service')
        previous[name] = {'text': path.read_text() if path.is_file() else None,
                          'active': subprocess.run(['systemctl', 'is-active', '--quiet', path.name]).returncode == 0}
    try:
        for name, (command, after) in commands.items():
            path = Path('/etc/systemd/system') / ('vps1-production-' + name + '.service')
            path.write_text(service(name, command, after=after, browser=name == 'script-chrome'))
            run(['systemd-analyze', 'verify', path], quiet=True)
        run(['systemctl', 'daemon-reload'], quiet=True)
        for name in NAMES:
            unit = 'vps1-production-' + name + '.service'
            run(['systemctl', 'enable', unit], quiet=True)
            run(['systemctl', 'restart', unit], quiet=True)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for url in ['http://127.0.0.1:9223/json/version', 'http://127.0.0.1:6081/vnc.html']:
            deadline = time.monotonic() + 50
            while time.monotonic() < deadline:
                try:
                    with opener.open(url, timeout=3) as response:
                        if response.status == 200:
                            break
                except OSError:
                    pass
                time.sleep(.5)
            else:
                raise RuntimeError('dedicated browser endpoint unavailable')
        event(script_work_browser='PASS', chrome_cdp='LOOPBACK', novnc='LOOPBACK', profile='ISOLATED')
    except BaseException:
        for name in reversed(NAMES):
            unit = 'vps1-production-' + name + '.service'
            subprocess.run(['systemctl', 'stop', unit], capture_output=True)
            path = Path('/etc/systemd/system') / unit
            if previous[name]['text'] is None:
                subprocess.run(['systemctl', 'disable', unit], capture_output=True)
                path.unlink(missing_ok=True)
            else:
                path.write_text(previous[name]['text'])
        run(['systemctl', 'daemon-reload'], quiet=True)
        for name in NAMES:
            if previous[name]['active']:
                run(['systemctl', 'start', 'vps1-production-' + name + '.service'], quiet=True)
        raise

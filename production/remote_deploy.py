#!/usr/bin/python3
"""Root-owned, narrow command for the existing vps1deploy SSH account.

Only the exact current protected main revision and this fixed application
allowlist can run. No user-selected URL, path, executable or shell is accepted.
"""
import fcntl
import os
from pathlib import Path
import re
import socket
import subprocess
import sys

REMOTE = 'https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control.git'
APPS = ('scene-builder', 'short-maker', 'short-stream', 'self-media-library', 'video-merger',
        'voice-studio', 'auto-caption-studio', 'local-upload-manager')
BASE = Path('/srv/fresh-website/control/releases')


def command(args, *, cwd=None):
    return subprocess.run(args, cwd=cwd, check=True, text=True, capture_output=True).stdout.strip()


def main():
    if os.geteuid() != 0 or socket.gethostname() != 'vps-01a23801':
        raise RuntimeError('approved VPS_1 root command required')
    if len(sys.argv) != 4:
        raise ValueError('expected operation, exact main revision and application')
    operation, revision, selected = sys.argv[1:]
    if operation not in {'install', 'rollback', 'nginx-route-only', 'nginx-route-rollback'} or not re.fullmatch('[a-f0-9]{40}', revision):
        raise ValueError('invalid production request')
    if selected != 'all' and selected not in APPS:
        raise ValueError('application not allowed')
    if operation.startswith('nginx-route-') and selected in {'all', 'scene-builder'}:
        raise ValueError('route-only operation requires one approved independent app')
    apps = list(APPS) if selected == 'all' else [selected]
    # No arbitrary ref or historic commit may execute with root privileges.
    current = command(['git', 'ls-remote', REMOTE, 'refs/heads/main']).split()[0]
    if current != revision:
        raise RuntimeError('requested revision is not current protected main')
    checkout = BASE / revision
    if checkout.is_symlink():
        raise RuntimeError('unexpected control symlink')
    if not (checkout / '.git').is_dir():
        checkout.mkdir(parents=True, exist_ok=True)
        command(['git', 'init', '-q'], cwd=checkout)
        command(['git', 'remote', 'add', 'origin', REMOTE], cwd=checkout)
        command(['git', 'fetch', '--depth=1', 'origin', revision], cwd=checkout)
        command(['git', 'checkout', '--detach', 'FETCH_HEAD'], cwd=checkout)
    if command(['git', 'rev-parse', 'HEAD'], cwd=checkout) != revision:
        raise RuntimeError('control checkout revision mismatch')
    if operation.startswith('nginx-route-'):
        # No application restart, gateway reconstruction, queue or database touch.
        subprocess.run(['/usr/bin/python3', '-m', 'production.nginx_route_only',
                        operation, '--revision', revision, '--app', selected],
                       cwd=checkout, check=True)
        return
    subprocess.run(['/usr/bin/python3', '-m', 'production.runtime', operation,
                    '--revision', revision, '--apps', *apps], cwd=checkout, check=True)
    # Keep every still-active app in the gateway when updating one app.
    active = [app for app in APPS if subprocess.run(
        ['systemctl', 'is-active', '--quiet', f'vps1-production-{app}.service']).returncode == 0]
    if active:
        subprocess.run(['/usr/bin/python3', '-m', 'production.gateway', '--revision', revision,
                        '--apps', *active], cwd=checkout, check=True)


if __name__ == '__main__':
    with open('/run/lock/vps1-production-control.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        main()

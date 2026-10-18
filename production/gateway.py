"""Verified per-app routes for the existing shared website shell."""
from __future__ import annotations

import argparse
import base64
import grp
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import time
import urllib.error
import urllib.request

from production.runtime import BASE, CATALOG, CONFIG, REPO, atomic_json, check_host, deployment_lock, event, http_check, run

SITE = Path('/etc/nginx/sites-available/vps1-production.conf')
ENABLED = Path('/etc/nginx/sites-enabled/vps1-production.conf')
AUTH = CONFIG / 'production.htpasswd'
LOGIN = Path('/root/vps1-production-login.json')


def nginx_config(apps, shell, auth, listen='80 default_server', loopback=True):
    lines = [f'server {{\n    listen {listen};']
    if loopback:
        lines.append('    listen 127.0.0.1:8888;')
    lines += ['    server_name _;', '    client_max_body_size 2048m;',
              '    auth_basic "VPS1 Workspace";', f'    auth_basic_user_file {auth};',
              '    location = / { alias ' + str(shell / 'index.html') + '; }',
              '    location ^~ /shell-assets/ { alias ' + str(shell / 'assets') + '/; }']
    for app in apps:
        spec = CATALOG[app]
        prefix = '/' + app
        native = app in {'video-merger', 'local-upload-manager'}
        destination = f'http://127.0.0.1:{spec["port"]}' + ('' if native else '/')
        lines += [f'    location = {prefix} {{ return 302 {prefix}/; }}',
                  f'    location ^~ {prefix}/ {{', f'        proxy_pass {destination};',
                  '        proxy_http_version 1.1;', '        proxy_set_header Host $host;',
                  '        proxy_set_header Upgrade $http_upgrade;',
                  '        proxy_set_header Connection "upgrade";',
                  '        proxy_set_header X-Forwarded-Proto $scheme;',
                  '        proxy_set_header X-Auth-User $remote_user;',
                  '        proxy_set_header X-App-Access both;',
                  '        proxy_set_header X-Is-Admin 1;',
                  '        proxy_set_header Authorization "";',
                  '        proxy_set_header Accept-Encoding "";',
                  '        proxy_read_timeout 300s;']
        if not native:
            # The imported apps use root-relative URLs in inline JS and in JSON
            # media responses. Rewrite only their known app endpoints, preserving
            # external URLs and filesystem/config strings in the API.
            roots = ['api/', 'static/', 'media/', 'outputs/', 'jobs', 'live']
            if app == 'scene-builder':
                roots += ['app', 'login', 'logout', 'dashboard', 'script-work/', 'storage-manager', 'scene-builder']
            if app == 'voice-studio':
                roots.append('tts/')
            lines += ['        sub_filter_once off;',
                      '        sub_filter_types application/javascript text/css application/json;']
            for root in roots:
                for quote in ('"', "'", '`'):
                    old = quote + '/' + root
                    new = quote + prefix + '/' + root
                    # Nginx strings use double quotes and escape literal quotes.
                    esc = lambda s: s.replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$')
                    lines.append(f'        sub_filter "{esc(old)}" "{esc(new)}";')
            lines += [f'        proxy_redirect ~^(/.*)$ {prefix}$1;',
                      f'        proxy_cookie_path / {prefix}/;']
        lines.append('    }')
    if 'scene-builder' in apps:
        lines += ['    location = /script-work/ { return 302 /scene-builder/script-work; }',
                  '    location = /script-work { return 302 /scene-builder/script-work; }',
                  '    location = /vps-storage-manager/ { return 302 /scene-builder/admin/storage; }',
                  '    location = /vps-storage-manager { return 302 /scene-builder/admin/storage; }']
    lines += ['    location / { return 404; }', '}']
    return '\n'.join(lines) + '\n'


def shell_files(apps, target):
    source = REPO / 'rebuild/shell'
    (target / 'assets').mkdir(parents=True, exist_ok=True)
    for relative in ('index.html', 'assets/shell.css', 'assets/shell.js'):
        text = (source / relative).read_text()
        text = text.replace('Preview shell', 'Workspace ready').replace('Runtime not verified', 'Service online')
        text = text.replace('Shared shell', 'Workspace').replace('Preview', 'Workspace')
        text = text.replace('Use the sidebar to open each application\'s own interface, without replacing its backend or deployment lifecycle.',
                            'Choose a tool from the sidebar to start working.')
        text = text.replace('A configured route is not a live-health verification. The fresh VPS cutover remains disabled.',
                            'Your applications are ready to open.')
        text = text.replace('Opening isolated app route. Runtime availability has not been verified.',
                            'Opening application…')
        text = text.replace('Application pages, queues, data and releases stay independent.',
                            'Your creative tools in one place.')
        (target / relative).write_text(text)
    manifest = {'schema_version': 1, 'apps': [
        {'id': app, 'title': app.replace('-', ' ').title(), 'path': '/' + app + '/',
         'route_configured': True, 'runtime_verified': True} for app in apps]}
    if 'scene-builder' in apps:
        manifest['apps'] += [{'id': app, 'title': title, 'path': '/' + app + '/',
                             'route_configured': True, 'runtime_verified': True}
                            for app, title in [('script-work', 'Script Work'), ('vps-storage-manager', 'VPS Storage Manager')]]
    atomic_json(target / 'assets/apps.json', manifest, 0o644)


def auth_credentials():
    if AUTH.exists() != LOGIN.exists():
        raise RuntimeError('existing workspace credentials require their matching local record')
    if not AUTH.exists():
        password = secrets.token_urlsafe(32)
        result = subprocess.run(['htpasswd', '-Bci', str(AUTH), 'admin'],
                                input=password + '\n', text=True, capture_output=True)
        if result.returncode:
            raise RuntimeError('workspace credential provisioning failed')
        os.chown(AUTH, 0, grp.getgrnam('www-data').gr_gid)
        AUTH.chmod(0o640)
        atomic_json(LOGIN, {'username': 'admin', 'password': password})
    # Nginx worker needs to traverse the config directory, not read other envs.
    CONFIG.chmod(0o751)
    return json.loads(LOGIN.read_text())


def verify_gateway(apps, credentials, port=8888):
    auth = base64.b64encode((credentials['username'] + ':' + credentials['password']).encode()).decode()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for app in apps:
        spec = CATALOG[app]
        path = spec['health'] if app in {'video-merger', 'local-upload-manager'} else '/' + app + spec['health']
        url = f'http://127.0.0.1:{port}' + path
        try:
            opener.open(url, timeout=5)
            raise RuntimeError('anonymous app route exposed: ' + app)
        except urllib.error.HTTPError as exc:
            if exc.code != 401:
                raise
        req = urllib.request.Request(url, headers={'Authorization': 'Basic ' + auth})
        with opener.open(req, timeout=10) as response:
            if response.status != 200 or json.loads(response.read()).get('ok') is not True:
                raise RuntimeError('authenticated route failed: ' + app)
        ui_path = spec['ui'] if app in {'video-merger', 'local-upload-manager'} else '/' + app + spec['ui']
        with opener.open(urllib.request.Request(f'http://127.0.0.1:{port}' + ui_path,
                                              headers={'Authorization': 'Basic ' + auth}), timeout=10) as response:
            if response.status != 200 or 'text/html' not in response.headers.get('Content-Type', ''):
                raise RuntimeError('browser document route failed: ' + app)
        event(app=app, gateway='PASS', anonymous='DENIED', html_ui='PASS')


def public_apps_for_release(requested, *, previous=None, is_active=None):
    """Preserve public routes; never auto-publish every active internal app.

    A separately installed Scene/Storage candidate may intentionally be kept
    off the public gateway. Only previously public apps and this release's
    explicitly selected apps are eligible for a new gateway cutover.
    """
    if previous is None:
        state_path = BASE / 'production-status.json'
        state = json.loads(state_path.read_text()) if state_path.is_file() else {}
        if state and state.get('gateway') != 'ACTIVE':
            raise RuntimeError('previous public gateway state is not active')
        previous = state.get('apps', [])
    if not isinstance(previous, (list, tuple)) or not isinstance(requested, (list, tuple)):
        raise RuntimeError('invalid gateway app set')
    for name in [*previous, *requested]:
        if name not in CATALOG:
            raise RuntimeError('unknown public app: ' + str(name))
    selected = [app for app in CATALOG if app in previous or app in requested]
    if not selected:
        raise RuntimeError('refusing empty public gateway release')
    if is_active is None:
        is_active = lambda app: subprocess.run(
            ['systemctl', 'is-active', '--quiet', 'vps1-production-' + app + '.service']
        ).returncode == 0
    for app in selected:
        if not is_active(app):
            raise RuntimeError('required public service is not active: ' + app)
    return selected


def install(apps):
    for app in apps:
        http_check(app)
    shell = BASE / 'production-shell'
    credentials = auth_credentials()
    shell_files(apps, shell)
    journal = BASE / 'transactions/gateway' / str(time.time_ns())
    journal.mkdir(parents=True)
    previous = SITE.read_bytes() if SITE.exists() else None
    apt_default = Path('/etc/nginx/sites-enabled/default')
    default_target = os.readlink(apt_default) if apt_default.is_symlink() else None
    if apt_default.exists() and default_target != '/etc/nginx/sites-available/default':
        raise RuntimeError('refusing to replace an unknown existing default site')
    try:
        if previous is not None:
            (journal / 'previous.conf').write_bytes(previous)
        if default_target:
            apt_default.unlink()
        SITE.write_text(nginx_config(apps, shell, AUTH))
        if not ENABLED.is_symlink():
            if ENABLED.exists():
                raise RuntimeError('unexpected gateway enabled path')
            ENABLED.symlink_to(SITE)
        run(['nginx', '-t'], quiet=True)
        run(['systemctl', 'enable', '--now', 'nginx'], quiet=True)
        run(['systemctl', 'reload', 'nginx'], quiet=True)
        verify_gateway(apps, credentials)
        atomic_json(BASE / 'production-status.json', {'apps': apps, 'gateway': 'ACTIVE',
                    'authentication': 'ENFORCED', 'auto_script': 'OFFLINE'}, 0o644)
        event(gateway='PASS', applications=len(apps), login_record=str(LOGIN), credentials_logged=False)
    except BaseException:
        if previous is None:
            SITE.unlink(missing_ok=True)
            ENABLED.unlink(missing_ok=True)
        else:
            SITE.write_bytes(previous)
        if default_target and not apt_default.exists():
            apt_default.symlink_to(default_target)
        subprocess.run(['nginx', '-t'], capture_output=True, check=True)
        subprocess.run(['systemctl', 'reload', 'nginx'], capture_output=True)
        event(gateway='ROLLED_BACK', applications='PRESERVED')
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--revision', required=True)
    parser.add_argument('--apps', nargs='+', choices=sorted(CATALOG), required=True)
    args = parser.parse_args()
    check_host(args.revision)
    with deployment_lock():
        install(args.apps)


if __name__ == '__main__':
    main()

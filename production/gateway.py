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
         'route_configured': True, 'runtime_verified': False} for app in apps]}
    if 'scene-builder' in apps:
        manifest['apps'] += [{'id': app, 'title': title, 'path': '/' + app + '/',
                             'route_configured': True, 'runtime_verified': False}
                            for app, title in [('script-work', 'Script Work'), ('vps-storage-manager', 'VPS Storage Manager')]]
    atomic_json(target / 'assets/apps.json', manifest, 0o644)


def mark_shell_verified(shell, apps):
    """Publish verified status only after primary routes pass authenticated probes."""
    path = shell / 'assets/apps.json'
    manifest = json.loads(path.read_text())
    if manifest.get('schema_version') != 1 or not isinstance(manifest.get('apps'), list):
        raise RuntimeError('invalid gateway shell registry')
    expected, found = set(apps), set()
    for item in manifest['apps']:
        if item.get('id') in expected:
            item['runtime_verified'] = True
            found.add(item['id'])
    if found != expected:
        raise RuntimeError('gateway verification missing registered apps')
    # Scene aliases are not independently health-probed and remain unverified.
    atomic_json(path, manifest, 0o644)


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


def open_during_reload(opener, request, *, timeout, retries=12, pause=0.6):
    """Allow bounded Nginx worker handover; never accept a missing route as PASS.

    A graceful reload may briefly route a request to the old configuration.
    Only transient 404/502/503 or connection errors are retried; 401 is still
    required for anonymous access and 200 for authenticated access.
    """
    for attempt in range(retries):
        try:
            return opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code not in (404, 502, 503) or attempt + 1 == retries:
                raise
        except urllib.error.URLError:
            if attempt + 1 == retries:
                raise
        time.sleep(pause)


def verify_gateway(apps, credentials, port=8888):
    auth = base64.b64encode((credentials['username'] + ':' + credentials['password']).encode()).decode()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for app in apps:
        spec = CATALOG[app]
        path = spec['health'] if app in {'video-merger', 'local-upload-manager'} else '/' + app + spec['health']
        url = f'http://127.0.0.1:{port}' + path
        try:
            with open_during_reload(opener, url, timeout=5):
                raise RuntimeError('anonymous app route exposed: ' + app)
        except urllib.error.HTTPError as exc:
            if exc.code != 401:
                raise
        req = urllib.request.Request(url, headers={'Authorization': 'Basic ' + auth})
        with open_during_reload(opener, req, timeout=10) as response:
            if response.status != 200 or json.loads(response.read()).get('ok') is not True:
                raise RuntimeError('authenticated route failed: ' + app)
        ui_path = spec['ui'] if app in {'video-merger', 'local-upload-manager'} else '/' + app + spec['ui']
        with open_during_reload(opener, urllib.request.Request(f'http://127.0.0.1:{port}' + ui_path,
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


def public_apps_for_revision(candidates, revision, *, previous=None, is_active=None):
    """Publish current-release apps, preserving previous routes.

    Older remote helpers pass all running services including unpublished pilots.
    Require a matching release record for each newly exposed app.
    """
    updated = []
    for app in candidates:
        if app not in CATALOG:
            raise RuntimeError('unknown gateway candidate')
        record = BASE / 'control/state' / (app + '.json')
        if record.is_file():
            recorded = json.loads(record.read_text())
            if recorded.get('status') == 'ACTIVE' and recorded.get('revision') == revision:
                updated.append(app)
    return public_apps_for_release(updated, previous=previous, is_active=is_active)


def install(apps):
    for app in apps:
        http_check(app)
    # Immutable per-attempt assets: restoring prior Nginx config must restore
    # prior shell content as well, never the last failed cutover's assets.
    shell = BASE / 'production-shell-releases' / str(time.time_ns())
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
        mark_shell_verified(shell, apps)
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


def legacy_voice_publication_required(requested, selected, revision):
    """Run the old standalone publisher only for a current Voice release.

    Once Voice owns a Scene-session snippet, adding the historical standalone
    location would create a duplicate Nginx route.  Other app releases must
    never republish Voice merely because its service is still active.
    """
    if 'voice-studio' not in requested or 'voice-studio' not in selected:
        return False
    record = BASE / 'control/state/voice-studio.json'
    state = json.loads(record.read_text()) if record.is_file() else {}
    if state.get('status') != 'ACTIVE' or state.get('revision') != revision:
        return False
    from production.scene_public_https import SITE
    from production.scene_session_routes import active
    return not active('voice-studio', SITE.read_text())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--revision', required=True)
    parser.add_argument('--apps', nargs='+', choices=sorted(CATALOG), required=True)
    args = parser.parse_args()
    check_host(args.revision)
    # The exact-job cancellation release performs no application install.
    # Do not reload Nginx or republish unrelated routes after that operation.
    intent = run(['git', 'log', '-1', '--format=%B'], cwd=REPO, quiet=True)
    if '[vps1-scene-job-terminate-3327-20261027]' in intent:
        if args.apps != ['scene-builder']:
            raise RuntimeError('cancel-only release cannot modify application gateway')
        print('SCENE_TARGET_CANCEL_GATEWAY_MUTATIONS=NONE', flush=True)
        return
    with deployment_lock():
        selected = public_apps_for_revision(args.apps, args.revision)
        install(selected)
        # Only an actual uploader release at this exact SHA may update the
        # independent Scene candidate route + Scene HTTPS uploader route.
        # Short Studio's coordinated workflow installs the full Scene candidate
        # LAST, including new Python modules and pinned proxy dependencies.
        # The legacy partial overlay copies app.py alone and can crash the old
        # candidate midway through an otherwise healthy Short Maker release.
        if '[vps1-short-studio-release]' in intent:
            print('SHORT_STUDIO_SCENE_OVERLAY=DEFERRED_TO_FULL_SCENE_INSTALL', flush=True)
        elif 'local-upload-manager' in args.apps and 'local-upload-manager' in selected:
            record = BASE / 'control/state/local-upload-manager.json'
            state = json.loads(record.read_text()) if record.is_file() else {}
            if state.get('status') == 'ACTIVE' and state.get('revision') == args.revision:
                from production.scene_uploader_ui_release import activate
                activate(args.revision)
        if legacy_voice_publication_required(args.apps, selected, args.revision):
            from production.scene_public_https import publish_voice_on_scene_https
            publish_voice_on_scene_https()


if __name__ == '__main__':
    main()

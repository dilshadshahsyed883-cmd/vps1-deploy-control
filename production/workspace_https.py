"""Publish installed workspace apps using the existing Scene HTTPS certificate.
Run only through the owner-selected VPS1 Bridge agent after protected-main merge.
No app restart, dependency installation, queue operation or credential change.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import html
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request

from production import gateway
from production.runtime import CATALOG, check_host, deployment_lock
from production.scene_public_https import SITE, nginx_https, VOICE_ANCHOR, VOICE_EXTRA
from production.route_auto_caption_scene_https import ROUTE as ACS_ROUTE, add_route as add_acs_route

APPS = tuple(a for a in CATALOG if a != 'scene-builder')
INCLUDE = Path('/etc/nginx/snippets/vps1-workspace-https.conf')
PAGE = Path('/srv/fresh-website/workspace-https.html')
ORIGIN = 'https://app.scenebuilder.online'
MARKER = ' location = /internal/runtime-ready { return 404; }'


def known_scene_sites():
    """Exactly the reviewed Scene site and prior Voice/Caption route releases."""
    base = nginx_https()
    voice = base.replace(VOICE_ANCHOR, VOICE_EXTRA + VOICE_ANCHOR, 1)
    caption = add_acs_route(base)
    return (base, voice, caption,
            add_acs_route(voice),
            caption.replace(VOICE_ANCHOR, VOICE_EXTRA + VOICE_ANCHOR, 1))


def config(previous=None):
    if previous is not None and previous not in known_scene_sites():
        raise RuntimeError('Scene HTTPS configuration changed; preserve and reconcile it')
    # Reuse deployed, authenticated Voice/Caption locations. An Nginx duplicate
    # location would fail the reload and must not disrupt existing users.
    already = set()
    if previous is not None:
        if VOICE_EXTRA in previous:
            already.add('voice-studio')
        if ACS_ROUTE in previous:
            already.add('auto-caption-studio')
    selected = [app for app in APPS if app not in already]
    source = gateway.nginx_config(selected, PAGE.parent, gateway.AUTH)
    start = source.index('    location = /' + selected[0])
    end = source.index('    location / { return 404; }')
    routes = source[start:end]
    # App backend routes retain their established URI/WS/redirect contracts.
    # Authentication must be explicit: the surrounding Scene site uses sessions.
    auth = ('        auth_basic "VPS1 Workspace";\n'
            f'        auth_basic_user_file {gateway.AUTH};\n')
    routes = routes.replace('/ {\n', '/ {\n' + auth)
    return ('# Installed workspace apps; Scene root/login remain unchanged.\n'
            'location = /workspace { return 302 /workspace/; }\n'
            'location = /workspace/ {\n' + auth +
            f'    alias {PAGE};\n    default_type text/html;\n}}\n' + routes)


def site_config(previous):
    if previous not in known_scene_sites() or previous.count(MARKER) != 1:
        raise RuntimeError('Scene HTTPS configuration changed; preserve and reconcile it')
    return previous.replace(MARKER, ' include ' + str(INCLUDE) + ';\n' + MARKER, 1)


def page():
    entries = [(a.replace('-', ' ').title(), '/' + a + '/') for a in APPS]
    entries += [('Scene Builder', '/app'), ('Script Work', '/script-work'),
                ('Storage Manager', '/admin/storage')]
    cards = ''.join('<a href="' + path + '"><strong>' + html.escape(title) +
                    '</strong><span>Open application →</span></a>' for title, path in entries)
    return ('<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>VPS1 Workspace</title><style>'
            'body{margin:0;background:#101722;color:#eef3fa;font:16px system-ui}'
            'main{max-width:1080px;margin:8vh auto;padding:24px}h1{font-size:44px;margin:12px 0}'
            'p{color:#aebed1;line-height:1.6}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:16px;margin-top:36px}'
            'a{display:block;padding:28px;background:#1c293c;border:1px solid #35465e;border-radius:14px;color:inherit;text-decoration:none}'
            'a:hover{border-color:#73b7ff;background:#23344c}strong{display:block;font-size:20px}span{display:block;margin-top:20px;color:#89c4ff}'
            '</style><main><p>YOUR CREATIVE TOOLS</p><h1>VPS1 Workspace</h1>'
            '<p>Choose an application to begin. Scene Builder, Script Work and Storage Manager use the Scene Builder login.</p>'
            '<div class="grid">' + cards + '</div></main></html>')


def verify(credentials):
    token = base64.b64encode((credentials['username'] + ':' + credentials['password']).encode()).decode()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    paths = [('/workspace/', False)]
    for app in APPS:
        spec = CATALOG[app]
        native = app in {'video-merger', 'local-upload-manager'}
        paths += [(spec['ui'] if native else '/' + app + spec['ui'], False),
                  (spec['health'] if native else '/' + app + spec['health'], True)]
    for path, is_health in paths:
        try:
            gateway.open_during_reload(opener, ORIGIN + path, timeout=10)
        except urllib.error.HTTPError as exc:
            if exc.code != 401: raise
        else: raise RuntimeError('anonymous route unexpectedly available')
        request = urllib.request.Request(ORIGIN + path, headers={'Authorization': 'Basic ' + token})
        with gateway.open_during_reload(opener, request, timeout=15) as response:
            if response.status != 200: raise RuntimeError('HTTPS route unavailable')
            if is_health:
                if json.load(response).get('ok') is not True: raise RuntimeError('service unavailable')
            elif 'text/html' not in response.headers.get('Content-Type', ''):
                raise RuntimeError('app HTML unavailable')
        print('HTTPS_AVAILABLE=' + ORIGIN + path, flush=True)
    with opener.open(ORIGIN + '/login', timeout=10) as response:
        if response.status != 200: raise RuntimeError('Scene login changed')
    print('SCENE_LOGIN=PRESERVED USER_TESTING=PENDING', flush=True)


def install(revision):
    check_host(revision)
    with deployment_lock():
        previous = SITE.read_bytes()
        desired = site_config(previous.decode())
        if INCLUDE.exists() or PAGE.exists():
            raise RuntimeError('workspace files already exist; no overwrite')
        state = json.loads((gateway.BASE / 'production-status.json').read_text())
        if state.get('gateway') != 'ACTIVE' or set(state.get('apps', [])) != set(APPS):
            raise RuntimeError('public app baseline differs from WP1')
        for app in APPS:
            subprocess.run(['systemctl', 'is-active', '--quiet', 'vps1-production-' + app + '.service'], check=True)
        credentials = json.loads(gateway.LOGIN.read_text())
        journal = gateway.BASE / 'transactions/workspace-https' / str(time.time_ns())
        journal.mkdir(parents=True)
        (journal / 'previous-scene.conf').write_bytes(previous)
        print('BACKUP=' + str(journal), flush=True)
        try:
            INCLUDE.parent.mkdir(parents=True, exist_ok=True)
            INCLUDE.write_text(config(previous.decode())); INCLUDE.chmod(0o644)
            PAGE.write_text(page()); PAGE.chmod(0o644)
            if SITE.read_bytes() != previous: raise RuntimeError('concurrent Scene site change')
            SITE.write_text(desired)
            subprocess.run(['nginx', '-t'], check=True)
            subprocess.run(['systemctl', 'reload', 'nginx'], check=True)
            verify(credentials)
        except BaseException:
            # Do not overwrite a concurrent unrelated site's update.
            if SITE.read_text() == desired:
                SITE.write_bytes(previous)
                subprocess.run(['nginx', '-t'], check=True)
                subprocess.run(['systemctl', 'reload', 'nginx'], check=True)
            elif SITE.read_bytes() != previous:
                print('CONCURRENT_SITE_CHANGE=PRESERVE_WORKSPACE_FILES_FOR_RECONCILIATION', flush=True)
                raise
            INCLUDE.unlink(missing_ok=True); PAGE.unlink(missing_ok=True)
            print('WORKSPACE_HTTPS=ROLLED_BACK', flush=True)
            raise
        (journal / 'release.json').write_text(json.dumps({'revision': revision,
            'previous_site_sha256': hashlib.sha256(previous).hexdigest(),
            'apps': APPS, 'user_testing': 'PENDING'}))
        print('RESULT=PASS_HTTPS_WEB_LIVE', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--revision', required=True)
    install(parser.parse_args().revision)

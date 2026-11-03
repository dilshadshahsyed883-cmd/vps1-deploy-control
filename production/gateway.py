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


def verify_voice_scene_sso_ui(*, readonly_gateway=False):
    """Prove signed Scene access, never downgrade a protected Voice UI 401.

    A Voice-only release must not rewrite the historical Basic gateway. For
    that *read-only* path, validate the exact owned Voice SSO snippet instead
    of matching the entire unrelated Scene vhost against old site snapshots.
    Runtime HTTPS/session/anonymous probes remain mandatory in both modes.
    """
    from production.scene_public_https import SITE as scene_site
    from production import scene_session_routes as scene_sso
    from production.runtime import verify_voice_release_anonymous_denial

    if scene_site.is_symlink() or not scene_site.is_file():
        raise RuntimeError('Voice Scene SSO site is unavailable')
    site = scene_site.read_text()
    if readonly_gateway:
        from production import nginx_route_only as routes
        names = routes.managed_re().findall(site)
        if (names.count('voice-studio') != 1
                or len(names) != len(set(names))
                or any(name not in routes.APPS for name in names)
                or routes.standalone(site, 'voice-studio')):
            raise RuntimeError('Voice Scene SSO include ownership invalid')
        fragment = routes.snippet_path('voice-studio')
        if (fragment.is_symlink() or not fragment.is_file()
                or fragment.read_text() not in (
                    scene_sso.config('voice-studio'),
                    scene_sso.legacy_voice_config())):
            raise RuntimeError('Voice Scene SSO snippet drift')
    elif not scene_sso.active('voice-studio', site):
        raise RuntimeError('Voice Scene SSO route is not active')
    cookie = scene_sso.ready_cookie('voice-studio')
    scene_sso.verify('voice-studio', cookie)
    scene_sso.verify_voice_shell(cookie)
    verify_voice_release_anonymous_denial()


def verify_short_maker_existing_https(revision):
    """Scoped Maker-only post-install proof; never mutate an unrelated gateway.

    The app is already installed by the guarded root helper. Require its
    exact current release record and one reviewed, unchanged public route.
    Do not reinterpret arbitrary Scene vhost drift as permission to rewrite.
    """
    from production import nginx_route_only as routes
    from production import scene_session_routes as sso
    from production.scene_public_https import SITE as scene_site

    record = BASE / 'control/state/short-maker.json'
    if record.is_symlink() or not record.is_file():
        raise RuntimeError('Short Maker release record unavailable')
    state = json.loads(record.read_text())
    if state.get('status') != 'ACTIVE' or state.get('revision') != revision:
        raise RuntimeError('Short Maker release record revision mismatch')
    http_check('short-maker')
    run(['nginx', '-t'], quiet=True)
    if scene_site.is_symlink() or not scene_site.is_file():
        raise RuntimeError('Short Maker Scene HTTPS site unavailable')
    site = scene_site.read_text()
    names = routes.managed_re().findall(site)
    if (names.count('short-maker') != 1 or len(names) != len(set(names))
            or any(name not in routes.APPS for name in names)):
        raise RuntimeError('Short Maker managed HTTPS ownership invalid')
    base = routes.managed_re().sub('', site)
    if ('location = /short-maker ' in base or
            'location ^~ /short-maker/' in base):
        raise RuntimeError('Short Maker duplicate direct HTTPS route')
    fragment = routes.snippet_path('short-maker')
    if fragment.is_symlink() or not fragment.is_file():
        raise RuntimeError('Short Maker managed HTTPS snippet missing')
    current = fragment.read_text()
    if current == sso.config('short-maker'):
        cookie = sso.ready_cookie('short-maker')
        sso.verify('short-maker', cookie)
        auth_mode = 'SCENE_SSO_VERIFIED'
    elif current == routes.route_config('short-maker'):
        # Read the established Basic credential record; never create or
        # rotate it. Real HTTPS checks require anonymous 401 and admin 200.
        routes.check_app('short-maker', routes.credentials())
        auth_mode = 'EXISTING_BASIC_VERIFIED'
    else:
        raise RuntimeError('Short Maker HTTPS snippet drift')
    routes.check_scene()
    return auth_mode


def verify_cloud_render_scene_only_https(revision):
    """Scoped Cloud Render app-only release: inspect live HTTPS, never edit Nginx.

    This is intentionally NOT an Nginx drift whitelist. A separately managed
    Scene SSO vhost may contain unknown additions; the app release must neither
    normalize nor replace them. Only the already-installed Scene candidate and
    its existing catch-all HTTPS route are being read/probed.
    """
    from production import nginx_route_only as routes
    from production.route_uploader_scene_https import scene_cookie
    from production.scene_public_https import SITE as scene_site

    record = BASE / "control/state/scene-candidate.json"
    if record.is_symlink() or not record.is_file():
        raise RuntimeError("Cloud Render Scene candidate release record unavailable")
    state = json.loads(record.read_text())
    if state.get("status") != "ACTIVE":
        raise RuntimeError("Cloud Render Scene candidate is not active")
    if scene_site.is_symlink() or not scene_site.is_file():
        raise RuntimeError("Cloud Render Scene HTTPS vhost unavailable")

    # Strictly read-only gateway inspection; a scoped Scene source update does
    # not need to regenerate a Basic gateway or silently accept configuration drift.
    before = scene_site.read_bytes()
    site = before.decode("utf-8")
    if site.count("location / {\n    proxy_pass http://127.0.0.1:18080;") != 1:
        raise RuntimeError("Cloud Render Scene HTTPS fallback ownership changed")
    if ("location = /cloud-render " in site or
            "location ^~ /cloud-render/" in site or
            "location /cloud-render/" in site):
        raise RuntimeError("Cloud Render HTTPS route shadows Scene fallback")
    run(["nginx", "-t"], quiet=True)
    routes.check_scene()
    # No anonymous browser or API access, including when rollback restored
    # an older Scene revision with no Cloud Render app installed.
    anon_page, _, _ = routes.tls_get("/cloud-render/", accept="text/html")
    if anon_page not in (302, 303, 401, 403, 404):
        raise RuntimeError("Cloud Render anonymous browser route exposed")
    anon_api, _, _ = routes.tls_get("/api/cloud-render/media")
    if anon_api not in (302, 303, 401, 403, 404):
        raise RuntimeError("Cloud Render anonymous API exposed")

    if state.get("revision") == revision:
        cookie = scene_cookie()
        status, content_type, html = routes.tls_get("/cloud-render/", cookie=cookie,
                                                    accept="text/html")
        if status != 200 or "html" not in content_type.lower() or not (
                b"Cloud Render" in html and b"GPU Config" in html and b"Media" in html):
            raise RuntimeError("Cloud Render authenticated admin HTML proof failed")
        for resource in ("/api/cloud-render/media", "/api/cloud-render/config"):
            code, content_type, body = routes.tls_get(resource, cookie=cookie)
            if code != 200 or "json" not in content_type.lower():
                raise RuntimeError("Cloud Render authenticated admin API proof failed")
            result = json.loads(body)
            if resource.endswith("/media") and not isinstance(result.get("items"), list):
                raise RuntimeError("Cloud Render media API response invalid")
            if resource.endswith("/config") and not (
                    result.get("source_lock") == "cloud_render/concat/source.lock.json"
                    and result.get("image_ready") is False):
                raise RuntimeError("Cloud Render GPU image/source contract invalid")
        evidence = "ADMIN_HTML_API_VERIFIED"
    else:
        # The root helper rechecks the gateway after an app rollback. Require
        # a live old Scene app and anonymous denial without claiming new UI.
        evidence = "PRIOR_SCENE_REVISION_SAFE_ROLLBACK"

    if scene_site.read_bytes() != before:
        raise RuntimeError("Cloud Render read-only gateway vhost changed concurrently")
    return {"mode": "NO_NGINX_MUTATION", "scene": evidence,
            "anonymous": "DENIED", "nginx_syntax": "PASS"}


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
        try:
            with open_during_reload(opener, urllib.request.Request(f'http://127.0.0.1:{port}' + ui_path,
                                                  headers={'Authorization': 'Basic ' + auth}), timeout=10) as response:
                if response.status != 200 or 'text/html' not in response.headers.get('Content-Type', ''):
                    raise RuntimeError('browser document route failed: ' + app)
        except urllib.error.HTTPError as exc:
            if app != 'voice-studio' or exc.code != 401:
                raise
            # Never turn 401 into a bypass: prove the independent, signed
            # Scene session on live HTTPS, native HTML, and direct API denial.
            verify_voice_scene_sso_ui()
            event(app=app, gateway='PASS', anonymous='DENIED', html_ui='SCENE_SSO_VERIFIED')
            continue
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
    # Inspect protected-main release intent before any route mutation.
    intent = run(['git', 'log', '-1', '--format=%B'], cwd=REPO, quiet=True)
    # Only the exact reviewed admin-only Cloud Render Scene release may use
    # the read-only HTTPS proof. The root deploy helper has already installed
    # the Scene candidate with its own idle-job, SSO and rollback transaction.
    # Crucially, do not regenerate the unrelated legacy Nginx gateway.
    # The root helper's gateway phase receives only legacy production-* units,
    # not the independently managed Scene candidate. Proof of Scene installation
    # comes from its root-owned ACTIVE revision record inside the scoped checker.
    if '[vps1-cloud-render-scene-only]' in intent:
        if ('[vps1-experiment-management-release]' not in intent or
                '[vps1-production-release:voice-studio]' in intent or
                '[vps1-auto-script-online]' in intent):
            raise RuntimeError('conflicting or unauthorized Cloud Render gateway intent')
        with deployment_lock():
            proof = verify_cloud_render_scene_only_https(args.revision)
            event(app='scene-builder', gateway='NO_MUTATION',
                  cloud_render=proof)
        return
    # The Three-Studio transaction owns and verifies the existing SSO Nginx site.
    if '[vps1-studio-permissions-release]' in intent:
        # remote_deploy passes only legacy production-* units here. The live
        # Scene candidate has a separate unit and may be absent from args.apps.
        # Runtime has already enforced its exact Scene-only deployment lane.
        print('THREE_STUDIO_PERMISSIONS_GATEWAY=NO_MUTATION', flush=True)
        return
    # The one-off exact Scene job reset does not deploy or republish the gateway.
    if '[vps1-scene-exact-reset-3327-20261031]' in intent:
        # The trusted remote helper passes all currently active app routes
        # here even after a scene-builder-only runtime operation. The exact
        # reset already completed; do not republish any gateway routes.
        if 'scene-builder' not in args.apps:
            raise RuntimeError('exact Scene reset gateway readback missing Scene Builder')
        print('SCENE_EXACT_RESET_GATEWAY_MUTATIONS=NONE', flush=True)
        return
    # The exact-job cancellation release performs no application install.
    # Do not reload Nginx or republish unrelated routes after that operation.
    if '[vps1-scene-job-terminate-3327-20261027]' in intent:
        if args.apps != ['scene-builder']:
            raise RuntimeError('cancel-only release cannot modify application gateway')
        print('SCENE_TARGET_CANCEL_GATEWAY_MUTATIONS=NONE', flush=True)
        return
    # Song Audio's coordinated cutover already verifies the live Scene SSO
    # route, the sole 8152 worker, and the split Voice service. The older
    # Basic-Auth shared-shell publisher cannot authenticate a Scene session
    # and must NOT rewrite an unrelated Nginx gateway after this transaction.
    # The separate read-only diagnostic must never publish a gateway either.
    song_audio_cutover = '[vps1-song-audio-separate]' in intent
    song_audio_audit = '[vps1-song-audio-voice-readonly-diagnose]' in intent
    if song_audio_cutover and song_audio_audit:
        raise RuntimeError('conflicting Song Audio gateway release intentions')
    if song_audio_audit:
        event(song_audio_gateway='NO_MUTATION', mode='READ_ONLY_DIAGNOSTIC')
        return
    if song_audio_cutover:
        from production.song_audio_cutover import _health_scene, _live_http_acceptance
        with deployment_lock():
            _health_scene()
            acceptance = _live_http_acceptance()
        # Never turn a failed health / owner-scoped Scene authentication probe
        # into success. The source transaction retains its own rollback gates.
        event(song_audio_gateway='NO_MUTATION', mode='SCENE_SSO_VERIFIED',
              canonical_page=acceptance['canonical_page'],
              anonymous_access=acceptance['anonymous_access'])
        return

    # Protected Voice acceptance ran real SSO checks in runtime's read-only
    # lane. Do not republish or edit the unrelated legacy Basic gateway.
    if '[vps1-voice-readonly-acceptance]' in intent:
        if ('voice-studio' not in args.apps
                or '[vps1-song-audio-separate]' in intent
                or '[vps1-song-studio-release]' in intent):
            raise RuntimeError('conflicting Voice read-only gateway intent')
        event(app='voice-studio', gateway='NO_MUTATION',
              mode='READ_ONLY_ENGINE_ACCEPTANCE')
        return

    # Voice V1.1 owns an independent, signed Scene HTTPS route. After the
    # app-scoped runtime release finishes, DO NOT republish the unrelated
    # legacy Basic gateway, whose UI probe cannot carry a Scene cookie.
    # Exact Voice-only intent and currently installed revision are mandatory;
    # this branch makes no Nginx or session-policy mutations.
    if '[vps1-production-release:voice-studio]' in intent:
        if 'voice-studio' not in args.apps or '[vps1-song-studio-release]' in intent:
            raise RuntimeError('conflicting Voice-only gateway release intent')
        with deployment_lock():
            record = BASE / 'control/state/voice-studio.json'
            if record.is_symlink() or not record.is_file():
                raise RuntimeError('Voice release record unavailable')
            state = json.loads(record.read_text())
            if state.get('status') != 'ACTIVE' or state.get('revision') != args.revision:
                raise RuntimeError('Voice release record revision mismatch')
            http_check('voice-studio')
            run(['nginx', '-t'], quiet=True)
            verify_voice_scene_sso_ui(readonly_gateway=True)
            event(app='voice-studio', gateway='NO_MUTATION',
                  scene_sso='LIVE_VERIFIED', anonymous='DENIED')
        return
    # Short Maker-only source updates must not regenerate the unrelated
    # legacy Basic gateway or overwrite the Scene-owned HTTPS vhost.
    if ('[vps1-short-studio-release]' in intent and
            '[vps1-short-maker-only]' in intent):
        if ('short-maker' not in args.apps or
                '[vps1-shortstudio-readonly-diagnose]' in intent or
                '[vps1-short-maker-drain-release]' in intent):
            raise RuntimeError('conflicting Short Maker gateway release intent')
        with deployment_lock():
            mode = verify_short_maker_existing_https(args.revision)
            event(app='short-maker', gateway='NO_MUTATION',
                  existing_https=mode, anonymous='DENIED')
        return
    with deployment_lock():
        selected = public_apps_for_revision(args.apps, args.revision)
        install(selected)
        # Only an actual uploader release at this exact SHA may update the
        # independent Scene candidate route + Scene HTTPS uploader route.
        if 'local-upload-manager' in args.apps and 'local-upload-manager' in selected:
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

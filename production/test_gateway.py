from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from production.gateway import nginx_config, public_apps_for_release, public_apps_for_revision
from production.runtime import CATALOG


class GatewaySelectionTest(unittest.TestCase):
    def test_voice_release_keeps_old_six_and_excludes_unpublished_scene(self):
        public = ['short-maker', 'short-stream', 'self-media-library',
                  'video-merger', 'auto-caption-studio', 'local-upload-manager']
        ready = set(CATALOG)  # Scene may be running internally but is not public.
        selected = public_apps_for_release(['voice-studio'], previous=public,
                                           is_active=lambda app: app in ready)
        self.assertEqual(set(selected), set(public) | {'voice-studio'})
        self.assertNotIn('scene-builder', selected)

    def test_legacy_helper_does_not_publish_independent_scene(self):
        from unittest.mock import patch
        import json
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'control/state').mkdir(parents=True)
            for app, revision in [('voice-studio', 'b' * 40), ('scene-builder', 'a' * 40)]:
                (root / 'control/state' / (app + '.json')).write_text(
                    json.dumps({'revision': revision, 'status': 'ACTIVE'}))
            with patch('production.gateway.BASE', root):
                selected = public_apps_for_revision(['short-maker', 'voice-studio', 'scene-builder'],
                    'b' * 40, previous=['short-maker'], is_active=lambda _: True)
            self.assertEqual(set(selected), {'short-maker', 'voice-studio'})

    def test_missing_existing_public_app_blocks_release(self):
        with self.assertRaisesRegex(RuntimeError, 'required public service'):
            public_apps_for_release(['voice-studio'], previous=['short-maker'],
                                    is_active=lambda app: app == 'voice-studio')

    def test_empty_and_unknown_public_release_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'empty public'):
            public_apps_for_release([], previous=[], is_active=lambda _: True)
        with self.assertRaisesRegex(RuntimeError, 'unknown public'):
            public_apps_for_release(['random-service'], previous=[], is_active=lambda _: True)



class GatewayReloadRetryTests(unittest.TestCase):
    def test_transient_404_recovers_to_authentication_challenge(self):
        from urllib.error import HTTPError
        from production.gateway import open_during_reload
        class StaleNginx:
            def __init__(self): self.calls = 0
            def open(self, url, timeout):
                self.calls += 1
                raise HTTPError(url, 404 if self.calls == 1 else 401,
                                'route in handover', {}, None)
        client = StaleNginx()
        with self.assertRaises(HTTPError) as raised:
            open_during_reload(client, '/voice-studio/healthz', timeout=2,
                               retries=3, pause=0)
        self.assertEqual(raised.exception.code, 401)
        self.assertEqual(client.calls, 2)

    def test_persistent_404_stays_failed(self):
        from urllib.error import HTTPError
        from production.gateway import open_during_reload
        class MissingRoute:
            def __init__(self): self.calls = 0
            def open(self, url, timeout):
                self.calls += 1
                raise HTTPError(url, 404, 'route missing', {}, None)
        client = MissingRoute()
        with self.assertRaises(HTTPError) as raised:
            open_during_reload(client, '/voice-studio/healthz', timeout=2,
                               retries=3, pause=0)
        self.assertEqual(raised.exception.code, 404)
        self.assertEqual(client.calls, 3)

    def test_auth_401_never_downgraded_or_retried(self):
        from urllib.error import HTTPError
        from production.gateway import open_during_reload
        class Denied:
            def __init__(self): self.calls = 0
            def open(self, url, timeout):
                self.calls += 1
                raise HTTPError(url, 401, 'auth required', {}, None)
        client = Denied()
        with self.assertRaises(HTTPError) as raised:
            open_during_reload(client, '/voice-studio/healthz', timeout=2,
                               retries=5, pause=0)
        self.assertEqual(raised.exception.code, 401)
        self.assertEqual(client.calls, 1)


class GatewaySyntaxTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which('nginx'), 'Nginx is exercised in clean Ubuntu CI')
    def test_real_nginx_parser_accepts_all_native_and_prefixed_routes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = nginx_config(list(CATALOG), root, root / 'auth',
                                  listen='127.0.0.1:18890', loopback=False)
            config = root / 'nginx.conf'
            config.write_text(f'pid {root}/pid;\nerror_log {root}/error.log;\n'
                              'events {}\nhttp { access_log off;\n' + server + '}\n')
            result = subprocess.run(['nginx', '-t', '-c', str(config)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


class WorkspaceHttpsTest(unittest.TestCase):
    def test_scene_routes_preserved_and_drift_rejected(self):
        from production.workspace_https import site_config, INCLUDE
        from production.scene_public_https import nginx_https
        expected = nginx_https()
        updated = site_config(expected)
        self.assertEqual(updated.replace(' include ' + str(INCLUDE) + ';\n', ''), expected)
        with self.assertRaises(RuntimeError):
            site_config(expected + '# concurrent update')

    def test_existing_voice_and_caption_routes_are_preserved_once(self):
        from production.workspace_https import config, site_config, INCLUDE, known_scene_sites
        from production.scene_public_https import VOICE_EXTRA
        from production.route_auto_caption_scene_https import ROUTE as ACS_ROUTE
        for site in known_scene_sites():
            updated = site_config(site)
            self.assertEqual(updated.replace(' include ' + str(INCLUDE) + ';\\n', ''), site)
            self.assertEqual(updated.count(' include ' + str(INCLUDE) + ';'), 1)
            snippet = config(site)
            for app, marker in (('voice-studio', VOICE_EXTRA),
                                ('auto-caption-studio', ACS_ROUTE)):
                if marker in site:
                    self.assertNotIn('location ^~ /' + app + '/ {', snippet)
                else:
                    self.assertIn('location ^~ /' + app + '/ {', snippet)
            self.assertIn('location ^~ /short-maker/ {', snippet)
        with self.assertRaises(RuntimeError):
            config(known_scene_sites()[0] + '\\n# unexpected route')

    def test_all_backend_routes_require_workspace_auth(self):
        from production.workspace_https import config, APPS
        generated = config()
        self.assertEqual(generated.count('auth_basic_user_file'), len(APPS) + 1)
        self.assertNotIn('18080', generated)
        self.assertNotIn('8000', generated)
        for app in APPS:
            route = generated.split('location ^~ /' + app + '/ {')[1].split('}')[0]
            self.assertIn('auth_basic_user_file', route)
            self.assertIn('proxy_pass', route)

    @unittest.skipUnless(shutil.which('nginx'), 'real Nginx parser in Ubuntu CI')
    def test_workspace_routes_with_real_nginx_parser(self):
        from production.workspace_https import config
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conf = root / 'nginx.conf'
            conf.write_text(f'pid {root}/pid; error_log {root}/error.log;\n'
                'events {} http { access_log off; server { listen 127.0.0.1:18891;\n'
                + config() + 'location / { proxy_pass http://127.0.0.1:18080; } } }')
            result = subprocess.run(['nginx', '-t', '-c', str(conf)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()

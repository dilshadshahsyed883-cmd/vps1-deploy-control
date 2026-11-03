from pathlib import Path
import json
import shutil
import subprocess
import tempfile
import unittest

from production.gateway import (legacy_voice_publication_required, nginx_config,
                                public_apps_for_release, public_apps_for_revision)
from production.runtime import CATALOG


class GatewayShellContractTests(unittest.TestCase):
    def test_gateway_manifest_is_fail_closed_then_verified(self):
        from production.gateway import shell_files, mark_shell_verified
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "immutable-shell"
            shell_files(["scene-builder", "voice-studio"], target)
            self.assertNotIn("<iframe", (target / "index.html").read_text())
            path = target / "assets/apps.json"
            before = json.loads(path.read_text())["apps"]
            self.assertTrue(all(not entry["runtime_verified"] for entry in before))
            mark_shell_verified(target, ["scene-builder", "voice-studio"])
            after = {entry["id"]: entry for entry in json.loads(path.read_text())["apps"]}
            self.assertTrue(after["scene-builder"]["runtime_verified"])
            self.assertTrue(after["voice-studio"]["runtime_verified"])
            self.assertFalse(after["script-work"]["runtime_verified"])
            self.assertFalse(after["vps-storage-manager"]["runtime_verified"])

    def test_missing_route_proof_does_not_publish_verification(self):
        from production.gateway import shell_files, mark_shell_verified
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "immutable-shell"
            shell_files(["voice-studio"], target)
            with self.assertRaisesRegex(RuntimeError, "missing registered apps"):
                mark_shell_verified(target, ["voice-studio", "scene-builder"])
            self.assertFalse(json.loads((target / "assets/apps.json").read_text())
                             ["apps"][0]["runtime_verified"])


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

    def test_legacy_voice_publisher_skips_existing_sso_and_unrelated_release(self):
        from unittest.mock import patch
        import json
        revision = 'a' * 40
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'control/state').mkdir(parents=True)
            (root / 'control/state/voice-studio.json').write_text(json.dumps({
                'revision': revision, 'status': 'ACTIVE'}))
            site = root / 'scene.conf'
            site.write_text('reviewed scene')
            with patch('production.gateway.BASE', root), \
                 patch('production.scene_public_https.SITE', site), \
                 patch('production.scene_session_routes.active', return_value=True):
                self.assertFalse(legacy_voice_publication_required(
                    ['voice-studio'], ['voice-studio'], revision))
                self.assertFalse(legacy_voice_publication_required(
                    ['auto-caption-studio'], ['voice-studio', 'auto-caption-studio'], revision))
            with patch('production.gateway.BASE', root), \
                 patch('production.scene_public_https.SITE', site), \
                 patch('production.scene_session_routes.active', return_value=False):
                self.assertTrue(legacy_voice_publication_required(
                    ['voice-studio'], ['voice-studio'], revision))



class GatewayVoiceSceneSSOTests(unittest.TestCase):
    """A legacy Basic UI 401 is never a substitute for signed Scene SSO."""

    @staticmethod
    def legacy_response(url, *, ui_status=401):
        from urllib.error import HTTPError
        from io import BytesIO

        if isinstance(url, str):  # anonymous health must be denied by Basic
            raise HTTPError(url, 401, 'Basic required', {}, None)
        if url.full_url.endswith('/healthz'):
            response = BytesIO(b'{"ok": true}')
            response.status = 200
            response.headers = {'Content-Type': 'application/json'}
            return response
        raise HTTPError(url.full_url, ui_status, 'Voice needs Scene session', {}, None)

    def test_basic_voice_ui_401_needs_real_scene_sso_proof(self):
        from unittest.mock import patch
        from production import gateway
        from urllib.error import HTTPError

        def open_legacy(opener, url, **kwargs):
            return self.legacy_response(url)
        with patch.object(gateway.urllib.request, 'build_opener'), \
             patch.object(gateway, 'open_during_reload', side_effect=open_legacy), \
             patch.object(gateway, 'verify_voice_scene_sso_ui') as verify, \
             patch.object(gateway, 'event') as event:
            gateway.verify_gateway(['voice-studio'], {'username': 'admin', 'password': 'test'})
        verify.assert_called_once_with()
        self.assertEqual(event.call_args.kwargs['html_ui'], 'SCENE_SSO_VERIFIED')

    def test_scene_verification_failure_does_not_accept_basic_401(self):
        from unittest.mock import patch
        from production import gateway

        def open_legacy(opener, url, **kwargs):
            return self.legacy_response(url)
        with patch.object(gateway.urllib.request, 'build_opener'), \
             patch.object(gateway, 'open_during_reload', side_effect=open_legacy), \
             patch.object(gateway, 'verify_voice_scene_sso_ui',
                          side_effect=RuntimeError('Scene login invalid')), \
             patch.object(gateway, 'event') as event:
            with self.assertRaisesRegex(RuntimeError, 'Scene login invalid'):
                gateway.verify_gateway(['voice-studio'], {'username': 'admin', 'password': 'test'})
        event.assert_not_called()

    def test_unrelated_app_401_must_not_be_accepted(self):
        from unittest.mock import patch
        from urllib.error import HTTPError
        from production import gateway

        def open_legacy(opener, url, **kwargs):
            if isinstance(url, str):
                raise HTTPError(url, 401, 'Basic required', {}, None)
            from io import BytesIO
            if '/health' in url.full_url:
                response = BytesIO(b'{"ok": true}')
                response.status = 200
                response.headers = {'Content-Type': 'application/json'}
                return response
            raise HTTPError(url.full_url, 401, 'denied', {}, None)

        with patch.object(gateway.urllib.request, 'build_opener'), \
             patch.object(gateway, 'open_during_reload', side_effect=open_legacy), \
             patch.object(gateway, 'verify_voice_scene_sso_ui') as verify:
            with self.assertRaises(HTTPError) as caught:
                gateway.verify_gateway(['short-maker'], {'username': 'admin', 'password': 'test'})
        self.assertEqual(caught.exception.code, 401)
        verify.assert_not_called()

    def test_signed_scene_proof_checks_route_https_and_direct_anonymous(self):
        from unittest.mock import patch
        from production import gateway
        import production.scene_session_routes as sso
        import production.scene_public_https as https
        import production.runtime as runtime

        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp) / 'scene.conf'
            site.write_text('known scene owned snippet')
            with patch.object(https, 'SITE', site), \
                 patch.object(sso, 'active', return_value=True) as active, \
                 patch.object(sso, 'ready_cookie', return_value='signed=test') as cookie, \
                 patch.object(sso, 'verify') as verified, \
                 patch.object(sso, 'verify_voice_shell') as shell, \
                 patch.object(runtime, 'verify_voice_release_anonymous_denial') as denial:
                gateway.verify_voice_scene_sso_ui()
            active.assert_called_once_with('voice-studio', 'known scene owned snippet')
            cookie.assert_called_once_with('voice-studio')
            verified.assert_called_once_with('voice-studio', 'signed=test')
            shell.assert_called_once_with('signed=test')
            denial.assert_called_once_with()

    def test_absent_or_unowned_scene_route_fails_closed(self):
        from unittest.mock import patch
        from production import gateway
        import production.scene_session_routes as sso
        import production.scene_public_https as https

        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp) / 'scene.conf'
            with patch.object(https, 'SITE', site):
                with self.assertRaisesRegex(RuntimeError, 'site is unavailable'):
                    gateway.verify_voice_scene_sso_ui()
                site.write_text('not owned')
                with patch.object(sso, 'active', return_value=False):
                    with self.assertRaisesRegex(RuntimeError, 'route is not active'):
                        gateway.verify_voice_scene_sso_ui()


class ShortMakerNoRewriteGatewayTests(unittest.TestCase):
    """Require actual scoped HTTPS proof, not a global Nginx republish."""

    def _layout(self, root, *, fragment="basic"):
        from production import nginx_route_only as routes
        from production import scene_session_routes as sso
        path = root / 'snippets'
        path.mkdir()
        site = root / 'scene.conf'
        site.write_text('UNRELATED SCENE VHOST DRIFT\n include '
                        + str(path / 'vps1-route-short-maker.conf') + ';\n')
        snippet = path / 'vps1-route-short-maker.conf'
        snippet.write_text(routes.route_config('short-maker') if fragment == 'basic'
                           else sso.config('short-maker') if fragment == 'sso'
                           else 'MALFORMED')
        return site, path, snippet

    def test_existing_basic_route_is_verified_without_site_mutation(self):
        from unittest.mock import patch
        from production import gateway
        from production import nginx_route_only as routes
        import production.scene_public_https as https
        revision = 'a' * 40
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = root / 'control/state/short-maker.json'
            state.parent.mkdir(parents=True)
            state.write_text(json.dumps({'status':'ACTIVE','revision':revision}))
            site, snippets, _ = self._layout(root)
            before = site.read_bytes()
            with patch.object(gateway, 'BASE', root), \
                 patch.object(gateway, 'http_check') as health, \
                 patch.object(gateway, 'run') as run, \
                 patch.object(https, 'SITE', site), \
                 patch.object(routes, 'SNIPPET_DIR', snippets), \
                 patch.object(routes, 'credentials', return_value='Basic real') as creds, \
                 patch.object(routes, 'check_app') as check_app, \
                 patch.object(routes, 'check_scene') as check_scene:
                mode = gateway.verify_short_maker_existing_https(revision)
            self.assertEqual(mode, 'EXISTING_BASIC_VERIFIED')
            self.assertEqual(site.read_bytes(), before)
            health.assert_called_once_with('short-maker')
            run.assert_called_once_with(['nginx', '-t'], quiet=True)
            creds.assert_called_once_with()
            check_app.assert_called_once_with('short-maker', 'Basic real')
            check_scene.assert_called_once_with()

    def test_scene_sso_route_must_verify_real_cookie(self):
        from unittest.mock import patch
        from production import gateway, nginx_route_only as routes
        from production import scene_session_routes as sso
        import production.scene_public_https as https
        revision = 'a' * 40
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            record = root / 'control/state/short-maker.json'
            record.parent.mkdir(parents=True)
            record.write_text(json.dumps({'status':'ACTIVE','revision':revision}))
            site, snippets, _ = self._layout(root, fragment='sso')
            with patch.object(gateway, 'BASE', root), \
                 patch.object(gateway, 'http_check'), \
                 patch.object(gateway, 'run'), \
                 patch.object(https, 'SITE', site), \
                 patch.object(routes, 'SNIPPET_DIR', snippets), \
                 patch.object(routes, 'check_scene'), \
                 patch.object(routes, 'check_app') as basic, \
                 patch.object(sso, 'ready_cookie', return_value='signed-cookie') as login, \
                 patch.object(sso, 'verify') as verified:
                mode = gateway.verify_short_maker_existing_https(revision)
            self.assertEqual(mode, 'SCENE_SSO_VERIFIED')
            login.assert_called_once_with('short-maker')
            verified.assert_called_once_with('short-maker','signed-cookie')
            basic.assert_not_called()

    def test_unknown_snippet_and_revision_mismatch_fail_closed(self):
        from unittest.mock import patch
        from production import gateway, nginx_route_only as routes
        import production.scene_public_https as https
        revision='a'*40
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            record=root/'control/state/short-maker.json'
            record.parent.mkdir(parents=True)
            record.write_text(json.dumps({'status':'ACTIVE','revision':revision}))
            site,snippets,snippet=self._layout(root,fragment='unknown')
            with patch.object(gateway,'BASE',root), \
                 patch.object(gateway,'http_check'), \
                 patch.object(gateway,'run'), \
                 patch.object(https,'SITE',site), \
                 patch.object(routes,'SNIPPET_DIR',snippets), \
                 patch.object(routes,'check_scene') as scene, \
                 patch.object(routes,'check_app') as basic:
                with self.assertRaisesRegex(RuntimeError,'snippet drift'):
                    gateway.verify_short_maker_existing_https(revision)
                record.write_text(json.dumps({'status':'ACTIVE','revision':'b'*40}))
                with self.assertRaisesRegex(RuntimeError,'revision mismatch'):
                    gateway.verify_short_maker_existing_https(revision)
            scene.assert_not_called()
            basic.assert_not_called()

    def test_short_maker_only_dispatch_never_calls_legacy_install(self):
        from contextlib import nullcontext
        from unittest.mock import patch
        from production import gateway
        intent='[vps1-short-studio-release] [vps1-short-maker-only]'
        revision='a'*40
        with patch('sys.argv',['gateway','--revision',revision,'--apps','short-maker']), \
             patch.object(gateway,'check_host'), \
             patch.object(gateway,'run',return_value=intent), \
             patch.object(gateway,'deployment_lock',return_value=nullcontext()), \
             patch.object(gateway,'verify_short_maker_existing_https',
                          return_value='EXISTING_BASIC_VERIFIED') as verify, \
             patch.object(gateway,'install') as legacy, \
             patch.object(gateway,'event') as event:
            gateway.main()
        legacy.assert_not_called()
        verify.assert_called_once_with(revision)
        self.assertEqual(event.call_args.kwargs['gateway'],'NO_MUTATION')


class VoiceReadOnlyGatewayDispatchTests(unittest.TestCase):
    """Do not rebuild legacy Nginx after a signed Scene Voice release."""

    def test_voice_release_proves_live_sso_without_gateway_mutation(self):
        from contextlib import nullcontext
        from unittest.mock import patch
        from production import gateway

        revision = 'a' * 40
        argv = ['gateway', '--revision', revision, '--apps', 'voice-studio']
        intent = 'guarded Voice [vps1-production-release:voice-studio]'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = root / 'control/state/voice-studio.json'
            record.parent.mkdir(parents=True)
            record.write_text(json.dumps({'status': 'ACTIVE', 'revision': revision}))
            with patch('sys.argv', argv), \
                 patch.object(gateway, 'BASE', root), \
                 patch.object(gateway, 'check_host'), \
                 patch.object(gateway, 'run', return_value=intent) as command, \
                 patch.object(gateway, 'deployment_lock', return_value=nullcontext()), \
                 patch.object(gateway, 'http_check') as health, \
                 patch.object(gateway, 'verify_voice_scene_sso_ui') as sso, \
                 patch.object(gateway, 'install') as legacy, \
                 patch.object(gateway, 'event') as event:
                gateway.main()
        legacy.assert_not_called()
        health.assert_called_once_with('voice-studio')
        sso.assert_called_once_with(readonly_gateway=True)
        self.assertEqual(event.call_args.kwargs['gateway'], 'NO_MUTATION')
        self.assertEqual(event.call_args.kwargs['scene_sso'], 'LIVE_VERIFIED')
        self.assertTrue(any(c.args[0] == ['nginx', '-t'] for c in command.call_args_list))

    def test_voice_release_rejects_missing_current_revision(self):
        from contextlib import nullcontext
        from unittest.mock import patch
        from production import gateway

        argv = ['gateway', '--revision', 'a' * 40, '--apps', 'voice-studio']
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = root / 'control/state/voice-studio.json'
            record.parent.mkdir(parents=True)
            record.write_text(json.dumps({'status': 'ACTIVE', 'revision': 'b' * 40}))
            with patch('sys.argv', argv), \
                 patch.object(gateway, 'BASE', root), \
                 patch.object(gateway, 'check_host'), \
                 patch.object(gateway, 'run',
                              return_value='[vps1-production-release:voice-studio]'), \
                 patch.object(gateway, 'deployment_lock', return_value=nullcontext()), \
                 patch.object(gateway, 'install') as legacy, \
                 patch.object(gateway, 'verify_voice_scene_sso_ui') as sso:
                with self.assertRaisesRegex(RuntimeError, 'revision mismatch'):
                    gateway.main()
        legacy.assert_not_called()
        sso.assert_not_called()

    def test_unrelated_vhost_drift_never_rewrites_and_voice_fragment_exact(self):
        from unittest.mock import patch
        from production import gateway
        from production import nginx_route_only as routes
        from production import scene_session_routes as sso
        import production.scene_public_https as https
        import production.runtime as runtime

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snippet_dir = root / 'snippets'
            snippet_dir.mkdir()
            site = root / 'scene.conf'
            site.write_text('UNRELATED REVIEW REQUIRED' + chr(10) +
                            ' include ' + str(snippet_dir) +
                            '/vps1-route-voice-studio.conf;' + chr(10))
            route = snippet_dir / 'vps1-route-voice-studio.conf'
            route.write_text(sso.config('voice-studio'))
            with patch.object(https, 'SITE', site), \
                 patch.object(routes, 'SNIPPET_DIR', snippet_dir), \
                 patch.object(sso, 'ready_cookie', return_value='real-session') as cookie, \
                 patch.object(sso, 'verify') as verify, \
                 patch.object(sso, 'verify_voice_shell') as shell, \
                 patch.object(runtime, 'verify_voice_release_anonymous_denial') as denial:
                gateway.verify_voice_scene_sso_ui(readonly_gateway=True)
                route.write_text('INVALID')
                with self.assertRaisesRegex(RuntimeError, 'snippet drift'):
                    gateway.verify_voice_scene_sso_ui(readonly_gateway=True)
        cookie.assert_called_once_with('voice-studio')
        verify.assert_called_once_with('voice-studio', 'real-session')
        shell.assert_called_once_with('real-session')
        denial.assert_called_once_with()

    def test_scene_session_denial_still_fails_closed(self):
        from unittest.mock import patch
        from production import gateway
        from production import nginx_route_only as routes
        from production import scene_session_routes as sso
        import production.scene_public_https as https

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snippets = root / 'snippets'
            snippets.mkdir()
            site = root / 'scene.conf'
            site.write_text(' include ' + str(snippets) +
                            '/vps1-route-voice-studio.conf;' + chr(10))
            (snippets / 'vps1-route-voice-studio.conf').write_text(
                sso.config('voice-studio'))
            with patch.object(https, 'SITE', site), \
                 patch.object(routes, 'SNIPPET_DIR', snippets), \
                 patch.object(sso, 'ready_cookie', return_value='real-session'), \
                 patch.object(sso, 'verify',
                              side_effect=RuntimeError('Scene login rejected')):
                with self.assertRaisesRegex(RuntimeError, 'Scene login rejected'):
                    gateway.verify_voice_scene_sso_ui(readonly_gateway=True)


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
            self.assertEqual(updated.replace(' include ' + str(INCLUDE) + ';\n', ''), site)
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
            config(known_scene_sites()[0] + '\n# unexpected route')

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


class SongAudioGatewayDispatchTests(unittest.TestCase):
    """Song's protected Scene SSO must never be replaced by legacy Basic Auth."""

    def test_readonly_diagnostic_does_not_republish_or_touch_legacy_gateway(self):
        from unittest.mock import patch
        from production import gateway
        argv = ['gateway', '--revision', 'a' * 40, '--apps', 'voice-studio']
        intent = 'audit [vps1-song-studio-release] [vps1-song-audio-voice-readonly-diagnose]'
        with patch('sys.argv', argv), \
             patch.object(gateway, 'check_host'), \
             patch.object(gateway, 'run', return_value=intent), \
             patch.object(gateway, 'deployment_lock') as lock, \
             patch.object(gateway, 'install') as legacy, \
             patch.object(gateway, 'event') as event:
            gateway.main()
        lock.assert_not_called()
        legacy.assert_not_called()
        self.assertEqual(event.call_args.kwargs['mode'], 'READ_ONLY_DIAGNOSTIC')

    def test_cutover_verifies_real_scene_sso_without_legacy_gateway_mutation(self):
        from contextlib import nullcontext
        from unittest.mock import patch
        from production import gateway
        argv = ['gateway', '--revision', 'a' * 40, '--apps', 'voice-studio']
        intent = 'release [vps1-song-studio-release] [vps1-song-audio-separate]'
        with patch('sys.argv', argv), \
             patch.object(gateway, 'check_host'), \
             patch.object(gateway, 'run', return_value=intent), \
             patch.object(gateway, 'deployment_lock', return_value=nullcontext()) as lock, \
             patch.object(gateway, 'install') as legacy, \
             patch.object(gateway, 'event') as event, \
             patch('production.song_audio_cutover._health_scene') as health, \
             patch('production.song_audio_cutover._live_http_acceptance',
                   return_value={'canonical_page': 'HTTP_200_SCENE_SSO',
                                 'anonymous_access': 'DENIED'}) as acceptance:
            gateway.main()
        lock.assert_called_once()
        health.assert_called_once()
        acceptance.assert_called_once()
        legacy.assert_not_called()
        self.assertEqual(event.call_args.kwargs['mode'], 'SCENE_SSO_VERIFIED')

    def test_cutover_authentication_failure_remains_fail_closed(self):
        from contextlib import nullcontext
        from unittest.mock import patch
        from production import gateway
        argv = ['gateway', '--revision', 'a' * 40, '--apps', 'voice-studio']
        intent = 'release [vps1-song-studio-release] [vps1-song-audio-separate]'
        with patch('sys.argv', argv), \
             patch.object(gateway, 'check_host'), \
             patch.object(gateway, 'run', return_value=intent), \
             patch.object(gateway, 'deployment_lock', return_value=nullcontext()), \
             patch.object(gateway, 'install') as legacy, \
             patch('production.song_audio_cutover._health_scene',
                   side_effect=RuntimeError('Scene verification unavailable')), \
             patch('production.song_audio_cutover._live_http_acceptance') as acceptance:
            with self.assertRaisesRegex(RuntimeError, 'verification unavailable'):
                gateway.main()
        acceptance.assert_not_called()
        legacy.assert_not_called()

    def test_conflicting_song_audio_intents_fail_before_gateway_mutation(self):
        from unittest.mock import patch
        from production import gateway
        argv = ['gateway', '--revision', 'a' * 40, '--apps', 'voice-studio']
        intent = ('[vps1-song-audio-separate] '
                  '[vps1-song-audio-voice-readonly-diagnose]')
        with patch('sys.argv', argv), \
             patch.object(gateway, 'check_host'), \
             patch.object(gateway, 'run', return_value=intent), \
             patch.object(gateway, 'install') as legacy:
            with self.assertRaisesRegex(RuntimeError, 'conflicting Song Audio'):
                gateway.main()
        legacy.assert_not_called()


if __name__ == '__main__':
    unittest.main()

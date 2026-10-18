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


if __name__ == '__main__':
    unittest.main()

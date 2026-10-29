from pathlib import Path
import unittest
from unittest.mock import patch
import tempfile
from production import song_maker_release as release
from production import remote_deploy

class SongMakerReleaseTests(unittest.TestCase):
    def test_independent_source_has_no_state_or_secrets(self):
        files=release.source_files()
        self.assertIn('part01/songmaker/app.py',files)
        self.assertFalse(any('__pycache__' in n or n.endswith('.sqlite3') for n in files))
        self.assertNotIn('song-maker',remote_deploy.APPS) # generic all must remain unchanged

    def test_trusted_nginx_identity_and_native_prefix(self):
        config=release.route_config()
        self.assertIn('internal;',config)
        self.assertIn('auth_request /_vps1_song_maker_auth;',config)
        self.assertIn('X-Auth-User $song_user',config)
        self.assertIn('X-Is-Admin $song_admin',config)
        self.assertIn('proxy_pass http://127.0.0.1:8150;',config)
        self.assertNotIn('X-Is-Admin 1',config)

    def test_scene_patch_is_bounded_and_idempotent(self):
        source='before\nSCENE_SSO_APP_IDS = frozenset({\n"short-maker", "video-merger",\n})\nafter'
        updated=release.scene_allowlist(source)
        self.assertEqual(release.scene_allowlist(updated),updated)
        self.assertTrue(updated.startswith('before\n'))
        self.assertTrue(updated.endswith('\nafter'))
        self.assertIn('"song-maker"',updated)

    def test_workers_have_limits_and_only_owned_paths(self):
        for lane in release.LANES:
            unit=release.unit(lane,'a'*40)
            self.assertIn('User=ubuntu',unit)
            self.assertIn('KillMode=control-group',unit)
            self.assertIn('CPUQuota=150%',unit)
            self.assertIn('MemoryMax=1536M',unit)
            self.assertNotIn('auto-caption',unit)
            self.assertNotIn('auto-script',unit)

    def test_immutable_source_drift_rejected(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(release,'BASE',Path(tmp)):
            target=release.stage('a'*40)
            (target/'part01/songmaker/app.py').write_text('drift')
            with self.assertRaisesRegex(RuntimeError,'immutable source drift'):
                release.stage('a'*40)

    def test_protected_release_never_targets_all(self):
        workflow=(release.REPO/'.github/workflows/vps1-song-maker-release.yml').read_text()
        self.assertIn('environment: production',workflow)
        self.assertIn('APP_SELECTION: song-maker',workflow)
        self.assertIn('StrictHostKeyChecking=yes',workflow)
        self.assertIn('tags: tag:ci',workflow)
        self.assertNotIn('options: [all',workflow)
        self.assertNotIn('pull_request_target',workflow)

if __name__=='__main__':unittest.main()

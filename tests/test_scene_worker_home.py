"""Non-destructive service HOME repair tests for existing Scene worker."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from production import scene_worker_home as home

SOURCE = Path('/srv/fresh-website/releases/scene-builder/' + 'a' * 40)
IDLE = {'ok': True, 'admission_only': False, 'queue_counts': {'failed': 1}}


class SceneWorkerHomeTests(unittest.TestCase):
    def test_owned_unit_preserves_existing_sandbox_and_protected_ports(self):
        legacy, desired = home.expected_units(SOURCE)
        self.assertEqual(desired.replace(home.HOME_LINES, ''), legacy)
        for item in ('User=vps1scene', 'ProtectSystem=strict', 'ProtectHome=true',
                     'ReadWritePaths=/var/lib/fresh-website/scene-builder',
                     '--host 127.0.0.1 --port 18081', 'EnvironmentFile='):
            self.assertIn(item, desired)
        for item in ('Environment=HOME=/var/lib/fresh-website/scene-builder/worker-home',
                     'Environment=XDG_CONFIG_HOME=/var/lib/fresh-website/scene-builder/worker-home/.config',
                     'Environment=TMPDIR=/var/lib/fresh-website/scene-builder/tmp'):
            self.assertIn(item, desired)
        with self.assertRaises(RuntimeError):
            home.expected_units(Path('/tmp/unowned-worker'))

    def _fixture(self, temp):
        root = Path(temp)
        record = root / 'record.json'
        record.write_text(json.dumps({
            'owner': 'scene-production-worker-v1',
            'unit': home.UNIT,
            'state': str(home.STATE),
            'source': str(SOURCE),
        }))
        env = root / 'private.env'
        env.write_text('\n'.join([
            'SCENE_WORKER_TOKEN=' + 's'*48,
            'HF_HOME=' + str(home.STATE / 'model-cache'),
            'XDG_CACHE_HOME=' + str(home.STATE / 'cache'),
            'PRIVATE_KEY=must-not-be-printed', ''
        ]))
        unit = root / 'worker.service'
        legacy, desired = home.expected_units(SOURCE)
        unit.write_text(legacy)
        return record, env, unit, desired

    def _patch_paths(self, record, env, unit):
        return (
            patch.object(home, 'RECORD', record),
            patch.object(home, 'ENV', env),
            patch.object(home, 'UNIT_FILE', unit),
            patch.object(home.os, 'geteuid', return_value=0),
            patch.object(home, 'prepare_home_dirs'),
        )

    def test_active_queue_refuses_stop_and_unit_change(self):
        with tempfile.TemporaryDirectory() as temp:
            record, env, unit, desired = self._fixture(temp)
            state = home.SceneWorkerHome(Path(temp))
            with self._patch_paths(record, env, unit)[0], \
                 self._patch_paths(record, env, unit)[1], \
                 self._patch_paths(record, env, unit)[2], \
                 patch.object(home.os, 'geteuid', return_value=0), \
                 patch.object(home, 'prepare_home_dirs'), \
                 patch.object(home, 'health', return_value={
                     'ok': True, 'admission_only': False,
                     'queue_counts': {'rendering': 1}}), \
                 patch.object(state, '_run') as runner:
                with self.assertRaisesRegex(RuntimeError, 'active/queued'):
                    state.install()
                runner.assert_not_called()
                self.assertNotIn(home.HOME_LINES, unit.read_text())

    def test_idle_transaction_and_cached_unit_rollback(self):
        with tempfile.TemporaryDirectory() as temp:
            record, env, unit, desired = self._fixture(temp)
            state = home.SceneWorkerHome(Path(temp))
            with patch.object(home, 'RECORD', record), \
                 patch.object(home, 'ENV', env), \
                 patch.object(home, 'UNIT_FILE', unit), \
                 patch.object(home.os, 'geteuid', return_value=0), \
                 patch.object(home, 'prepare_home_dirs'), \
                 patch.object(home, 'health', return_value=IDLE), \
                 patch.object(home, 'database_idle'), \
                 patch.object(state, '_run') as runner, \
                 patch.object(state, '_verify', return_value={'actual_process_env': 'PASS'}), \
                 patch.object(state, '_ready'):
                self.assertEqual(state.install()['actual_process_env'], 'PASS')
                self.assertEqual(unit.read_text(), desired)
                self.assertEqual([x.args[0][:2] for x in runner.call_args_list],
                                 [['systemctl', 'stop'], ['systemctl', 'daemon-reload'],
                                  ['systemd-analyze', 'verify'], ['systemctl', 'start']])
                self.assertEqual((state.journal / 'original-unit').read_text(),
                                 desired.replace(home.HOME_LINES, ''))
                runner.reset_mock()
                state.rollback()
                self.assertEqual(unit.read_text(), desired.replace(home.HOME_LINES, ''))
                self.assertEqual([x.args[0][:2] for x in runner.call_args_list],
                                 [['systemctl', 'stop'], ['systemctl', 'daemon-reload'],
                                  ['systemctl', 'start']])

    def test_racing_submission_keeps_original_unit_and_restores_worker(self):
        with tempfile.TemporaryDirectory() as temp:
            record, env, unit, desired = self._fixture(temp)
            state = home.SceneWorkerHome(Path(temp))
            with patch.object(home, 'RECORD', record), \
                 patch.object(home, 'ENV', env), \
                 patch.object(home, 'UNIT_FILE', unit), \
                 patch.object(home.os, 'geteuid', return_value=0), \
                 patch.object(home, 'prepare_home_dirs'), \
                 patch.object(home, 'health', return_value=IDLE), \
                 patch.object(home, 'database_idle', side_effect=RuntimeError('raced')), \
                 patch.object(state, '_run') as runner, \
                 patch.object(state, '_ready'):
                with self.assertRaisesRegex(RuntimeError, 'raced'):
                    state.install()
                state.rollback()
                self.assertNotIn(home.HOME_LINES, unit.read_text())
                self.assertEqual([x.args[0][:2] for x in runner.call_args_list],
                                 [['systemctl', 'stop'], ['systemctl', 'start']])

    def test_process_environment_and_exact_dependency_versions_are_required(self):
        state = home.SceneWorkerHome(Path('/tmp/nonexistent-test-only'))
        environ = b'\0'.join([
            ('HOME=' + str(home.HOME)).encode(),
            ('XDG_CONFIG_HOME=' + str(home.CONFIG)).encode(),
            ('TMPDIR=' + str(home.TMP)).encode(),
            ('HF_HOME=' + str(home.STATE / 'model-cache')).encode(),
            ('XDG_CACHE_HOME=' + str(home.STATE / 'cache')).encode(),
            b'SCENE_WORKER_TOKEN=private', b''
        ])
        def command(args, timeout=50):
            if args[0] == 'systemctl':
                return '1234'
            return '{"av":"16.1.0","faster_whisper":"1.2.0"}'
        with patch.object(state, '_ready'), patch.object(state, '_run', side_effect=command), \
             patch.object(Path, 'read_bytes', return_value=environ):
            self.assertEqual(state._verify()['actual_process_env'], 'PASS')
        with patch.object(state, '_ready'), patch.object(state, '_run', side_effect=command), \
             patch.object(Path, 'read_bytes', return_value=environ.replace(
                 str(home.HOME).encode(), b'/nonexistent')):
            with self.assertRaisesRegex(RuntimeError, 'actual process'):
                state._verify()

    def test_release_integrates_rollback_and_future_worker_dirs(self):
        root = Path(__file__).resolve().parents[1]
        candidate = (root / 'production/scene_candidate_release.py').read_text()
        worker = (root / 'production/scene_production_worker.py').read_text()
        self.assertIn('worker_home.install()', candidate)
        self.assertIn('worker_home.rollback()', candidate)
        self.assertLess(candidate.index('worker_home.install()'),
                        candidate.index('atomic_link(CURRENT, target)'))
        self.assertIn('prepare_home_dirs()', worker)
        self.assertNotIn('UPDATE jobs', (root / 'production/scene_worker_home.py').read_text())
        self.assertNotIn('DELETE FROM jobs', (root / 'production/scene_worker_home.py').read_text())


if __name__ == '__main__':
    unittest.main()

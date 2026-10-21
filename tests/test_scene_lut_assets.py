"""Real builtin Scene LUT cube + FFmpeg smoke without production/project mutation."""
import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from production import scene_lut_assets as assets

ROOT = Path(__file__).resolve().parents[1]
SCENE = ROOT / 'apps/scene-builder/part02'
sys.path.insert(0, str(SCENE))
from smart_scroll.lut_engine import (
    PACK_DEFINITIONS, _parse_cube, _infer_cube_size, LUT_SIZE,
    assign_scene_luts, resolve_lut_source,
)
from types import SimpleNamespace


class SceneLUTAssetsTests(unittest.TestCase):
    def _worker(self, root: Path):
        (root / 'worker-home').mkdir(parents=True)
        (root / 'tmp').mkdir()
        environment = dict(os.environ)
        environment.update({
            'SCENE_WORKER_DATA': str(root),
            'HOME': str(root / 'worker-home'),
            'TMPDIR': str(root / 'tmp'),
            'PYTHONPATH': str(SCENE),
        })
        environment.pop('XDG_DATA_HOME', None)
        process = subprocess.run([sys.executable, '-c', assets.LUT_PROBE],
                                 cwd=str(ROOT), env=environment, capture_output=True,
                                 text=True, timeout=90)
        self.assertEqual(process.returncode, 0, process.stderr[-2200:])
        return json.loads(process.stdout.strip().splitlines()[-1])

    def test_all_23_presets_and_real_graded_scene_001_ffmpeg(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self._worker(root)
            self.assertEqual(first['builtins'], 23)
            self.assertEqual(first['new_files'], 23)
            self.assertEqual(first['scene_001_subtle_contrast'], 'PASS')
            self.assertEqual(first['real_ffmpeg_lut_filter'], 'PASS')
            folder = root / 'worker-home/.local/share/Stock Media Scene Builder/assets/luts'
            cube = folder / 'neutral_correction/subtle_contrast.cube'
            headers, values = _parse_cube(cube)
            self.assertEqual(_infer_cube_size(headers, len(values)), LUT_SIZE)
            self.assertEqual(len(values), 4913)
            self.assertEqual(
                cube.stat().st_size > 50000, True,
                'Real preset must not be silently replaced by a dummy cube')
            with patch.dict(os.environ, {'HOME': str(root / 'worker-home')}, clear=False):
                with patch.dict(os.environ, {'XDG_DATA_HOME': ''}):
                    choice = assign_scene_luts([{'scene_code': 'SCENE_001',
                                                 'text': 'technical documentary factory'}],
                                              SimpleNamespace(lut_enabled=True, lut_mode='auto',
                                                              lut_pack='cinematic',
                                                              lut_scene_aware_enabled=True,
                                                              lut_continuity_enabled=False))[0]
                    self.assertEqual(choice['pack'], 'neutral_correction')
                    self.assertEqual(choice['preset'], 'subtle_contrast')
                    self.assertEqual(resolve_lut_source(choice, None), cube)
            # Existing valid material, including harmless user annotations, must
            # remain byte-for-byte unchanged through repeated releases.
            with cube.open('a', encoding='utf-8') as stream:
                stream.write('# preserve existing LUT comments\n')
            before = hashlib.sha256(cube.read_bytes()).hexdigest()
            second = self._worker(root)
            self.assertEqual(second['new_files'], 0)
            self.assertEqual(second['existing_preserved'], 23)
            self.assertEqual(hashlib.sha256(cube.read_bytes()).hexdigest(), before)

    def test_missing_custom_lut_remains_error_not_fake_builtin(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {'HOME': directory, 'XDG_DATA_HOME': ''}):
                choice = {'pack': 'custom', 'preset': 'private-grade',
                          'label': 'Custom • private-grade'}
                self.assertIsNone(resolve_lut_source(choice,
                    SimpleNamespace(lut_custom_path=str(Path(directory) / 'missing.cube'))))

    def test_worker_source_and_live_home_guards(self):
        with self.assertRaisesRegex(RuntimeError, 'ownership'):
            assets._worker_source({'owner': 'another-app', 'unit': assets.UNIT,
                                   'state': str(assets.STATE)})
        with self.assertRaisesRegex(RuntimeError, 'unknown Scene production worker'):
            assets._worker_source({'owner': 'scene-production-worker-v1',
                                   'unit': 'other-unit', 'state': str(assets.STATE)})
        with patch.object(assets.subprocess, 'run',
                          return_value=SimpleNamespace(stdout='9999')):
            with patch.object(Path, 'read_bytes',
                              return_value=b'HOME=/nonexistent\0SCENE_WORKER_DATA=/tmp\0'):
                with self.assertRaisesRegex(RuntimeError, 'HOME'):
                    assets._process_env()

    def test_scene_release_is_app_scoped_and_does_not_retry_job(self):
        release = (ROOT / 'production/scene_candidate_release.py').read_text()
        lut = (ROOT / 'production/scene_lut_assets.py').read_text()
        renderer = (ROOT / 'apps/scene-builder/part02/smart_scroll/v2_renderer.py').read_text()
        ast.parse(release); ast.parse(lut)
        self.assertIn('lut_result = provision_builtins(target)', release)
        self.assertLess(release.index('provision_builtins(target)'),
                        release.index('atomic_link(CURRENT, target)'))
        self.assertIn('integrations["built_in_lut_assets"] = lut_result', release)
        self.assertIn("Missing LUT:", renderer)
        self.assertNotIn('UPDATE jobs', lut)
        self.assertNotIn('DELETE FROM jobs', lut)
        self.assertNotIn('restart-stage', lut)
        self.assertNotIn('restart-job', lut)


if __name__ == '__main__':
    unittest.main()

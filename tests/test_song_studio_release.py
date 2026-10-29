from pathlib import Path
import unittest
from production.song_studio_release import validate_template

class SongStudioReleaseTests(unittest.TestCase):
    def test_reviewed_template_and_drift(self):
        source=(Path(__file__).resolve().parents[1]/'apps/voice-studio/part01/templates/voice_studio.html').read_bytes()
        validate_template(source)
        for changed in (source+source, source.replace(b'vps1:voice-engine-changed', b'unknown'),
                        source.replace(b'data-workspace="voice"', b'data-workspace="wrong"')):
            with self.assertRaises(RuntimeError): validate_template(changed)

    def test_guarded_coordinated_release(self):
        root=Path(__file__).resolve().parents[1]
        source=(root/'.github/workflows/vps1-song-studio-release.yml').read_text()
        for marker in ('environment: production', 'StrictHostKeyChecking=yes', 'tags: tag:ci',
                       '[vps1-song-studio-release]', 'cancel-in-progress: false',
                       'for app in scene-builder voice-studio local-upload-manager song-maker'):
            self.assertIn(marker,source)
        self.assertNotIn('pull_request_target',source)
        self.assertIn('install_voice(revision)',(root/'production/runtime.py').read_text())

    def test_voice_cutover_and_failure_restore_only_web_pointer(self):
        import json
        import tempfile
        from unittest.mock import patch
        from production import song_studio_release as release
        source=(release.REPO/'apps/voice-studio'/release.PAGE).read_bytes()
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp); releases=root/'releases'; old=releases/'old'
                (old/release.PAGE).parent.mkdir(parents=True)
                (old/release.PAGE).write_bytes(b'prior template')
                (old/'_production_manifest.json').write_text(json.dumps({'app':'voice-studio','files':{str(release.PAGE):'prior'}}))
                current=root/'current'; current.symlink_to(old,target_is_directory=True)
                with patch.object(release,'CURRENT',current), patch.object(release,'RELEASES',releases), \
                     patch.object(release,'JOURNALS',root/'journals'), patch.object(release,'require_idle_audio_downloader'), \
                     patch.object(release,'worker_pids',return_value={'synthesis':'123'}), \
                     patch.object(release,'run') as commands, patch.object(release,'http_check'), \
                     patch.object(release,'read_full_authenticated_voice_html',return_value=source if not fail else b'wrong'), \
                     patch.object(release,'verify_audio_downloader_https'), \
                     patch('production.route_uploader_scene_https.scene_cookie',return_value='fixture'), \
                     patch('production.route_uploader_scene_https.https_get',return_value=(401,'','')):
                    if fail:
                        with self.assertRaisesRegex(RuntimeError,'not live'): release.install_voice('a'*40)
                        self.assertEqual(current.resolve(),old)
                    else:
                        release.install_voice('a'*40)
                        self.assertEqual((current/release.PAGE).read_bytes(),source)
                    self.assertEqual((old/release.PAGE).read_bytes(),b'prior template')
                    self.assertTrue(all(call.args[0]==['systemctl','restart',release.UNIT] for call in commands.call_args_list))

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

    def test_scene_patch_preserves_other_web_and_renderer_bytes(self):
        from production import song_studio_release as release
        root=Path(__file__).resolve().parents[1]/'apps/scene-builder'
        source={name:(root/name).read_bytes() for name in release.SCENE_FILES}
        original=dict(source)
        app=source['part01/app.py'].decode()
        start=app.index('# SONG_STUDIO_NAV_V1:');end=app.index('@app.get("/experiment-studio/")',start)
        original['part01/app.py']=(app[:start]+app[end:]+'\n# KEEP_RENDERER_CONFIGURATION\n').encode()
        original['part01/templates/base.html']=b''.join(line for line in source['part01/templates/base.html'].splitlines(True)
            if b'href="/song-studio/"' not in line and b'vps1-song-studio-tabs' not in line)
        original['part01/static/vps1_native_site_shell.css']+=b'\n/* KEEP_OTHER_APP_STYLE */\n'
        changed=release.patch_scene_payloads(original,source)
        self.assertIn(b'KEEP_RENDERER_CONFIGURATION',changed['part01/app.py'])
        self.assertIn(b'KEEP_OTHER_APP_STYLE',changed['part01/static/vps1_native_site_shell.css'])
        self.assertEqual(changed['part01/app.py'].count(b'# SONG_STUDIO_NAV_V1:'),1)
        self.assertIn(b'/modes/song/app',changed['part01/app.py'])
        self.assertEqual(release.patch_scene_payloads(changed,source),changed)
        original['part01/static/vps1_native_site_shell.js']=b'unreviewed live drift'
        with self.assertRaisesRegex(RuntimeError,'shared shell drift'):release.patch_scene_payloads(original,source)

    def test_scene_web_rollback_never_restarts_render_worker(self):
        import json,tempfile
        from unittest.mock import patch
        from production import song_studio_release as release
        source={name:(release.REPO/'apps/scene-builder'/name).read_bytes() for name in release.SCENE_FILES}
        for fail in (False,True):
            with self.subTest(fail=fail),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);releases=root/'releases';old=releases/'old';old.mkdir(parents=True)
                for name,payload in source.items():
                    (old/name).parent.mkdir(parents=True,exist_ok=True);(old/name).write_bytes(payload)
                (old/'renderer-sentinel').write_bytes(b'keep worker state')
                (old/'_scene_candidate_manifest.json').write_text(json.dumps({'app':'scene-builder','files':{name:'old' for name in source}}))
                current=root/'current';current.symlink_to(old,target_is_directory=True)
                def response(path,cookie=''):
                    if path.startswith('/static/'):return (200,'application/javascript',b'wrong' if fail else source['part01/static/vps1_native_site_shell.js'])
                    return (200 if path=='/scene-builder/' else 303,'',b'')
                with patch.object(release,'SCENE_CURRENT',current),patch.object(release,'SCENE_RELEASES',releases), \
                     patch.object(release,'SCENE_JOURNALS',root/'journals'),patch.object(release,'run',return_value='123') as commands, \
                     patch('production.scene_candidate_release._health',return_value={'ok':True}) as readiness, \
                     patch('production.route_uploader_scene_https.scene_cookie',return_value='fixture'), \
                     patch('production.route_uploader_scene_https.https_get',side_effect=response):
                    if fail:
                        with self.assertRaisesRegex(RuntimeError,'not live'):release.install_scene('a'*40)
                        self.assertEqual(current.resolve(),old)
                    else:release.install_scene('a'*40)
                    self.assertEqual(readiness.call_count,2 if fail else 1)
                    self.assertEqual((current/'renderer-sentinel').read_bytes(),b'keep worker state')
                    for call in commands.call_args_list:
                        if 'restart' in call.args[0]:self.assertEqual(call.args[0],['systemctl','restart',release.SCENE_UNIT])

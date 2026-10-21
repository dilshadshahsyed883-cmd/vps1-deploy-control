import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from production import video_merger_import_release as release

class Release(unittest.TestCase):
    def exercise(self, failure):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);base=root/'host';repo=root/'repo';units=root/'units';units.mkdir()
            old=base/'releases/video-merger/old';(old/'part01/templates').mkdir(parents=True)
            (old/'part01/app.py').write_text('old = True')
            (old/'part01/templates/index.html').write_text('active parallel UI')
            (old/'_production_manifest.json').write_text(json.dumps({'app':'video-merger','files':{'part01/app.py':'old'}}))
            pointer=base/'current/video-merger';pointer.parent.mkdir();pointer.symlink_to(old)
            python=base/'venvs/video-merger/old/bin/python';python.parent.mkdir(parents=True);python.touch()
            unit=units/'vps1-production-video-merger.service'
            original=f'ExecStart={python} app.py\nEnvironment=PATH={python.parent}:/usr/bin\n'
            unit.write_text(original)
            src=repo/'apps/video-merger/part01/app.py';src.parent.mkdir(parents=True);src.write_text('new = True')
            def command(args,**kw):
                if args[:3]==['python3','-m','venv']:
                    path=Path(args[3]);(path/'bin').mkdir(parents=True);(path/'bin/python').touch()
                if failure=='smoke' and 'production.video_merger_import_smoke' in args:raise RuntimeError('source failure')
                return ''
            checks=[None,RuntimeError('health failure'),None] if failure=='health' else [None,None]
            with patch.object(release,'BASE',base),patch.object(release,'DATA',root/'data'),patch.object(release,'REPO',repo), \
                 patch.object(release,'SYSTEMD',units),patch.object(release,'idle'),patch.object(release,'event'), \
                 patch.object(release,'run',side_effect=command) as commands,patch.object(release,'http_check',side_effect=checks), \
                 patch('production.route_uploader_scene_https.scene_cookie',return_value='cookie'), \
                 patch('production.route_uploader_scene_https.https_get',return_value=(200,'text/html',b'page')):
                if failure:
                    with self.assertRaises(RuntimeError):release.install('a'*40)
                    self.assertEqual(pointer.resolve(),old);self.assertEqual(unit.read_text(),original)
                    if failure=='smoke':
                        self.assertFalse(any(call.args[0][0]=='systemctl' for call in commands.call_args_list))
                else:
                    release.install('a'*40)
                    self.assertEqual((pointer/'part01/templates/index.html').read_text(),'active parallel UI')
                    self.assertEqual((pointer/'part01/app.py').read_text(),'new = True')
                    self.assertIn('a'*40+'-imports/bin/python',unit.read_text())
    def test_workflow_intent_and_smoke_syntax(self):
        root=Path(__file__).resolve().parents[1]
        workflow=(root/'.github/workflows/vps1-video-merger-import-release.yml').read_text()
        self.assertIn("contains(github.event.head_commit.message, '[vps1-video-merger-import-release]')",workflow)
        self.assertNotIn('vps1-voice-template-only',workflow)
        self.assertIn('install $SOURCE_SHA video-merger',workflow)
        self.assertIn('StrictHostKeyChecking=yes',workflow)
        compile((root/'production/video_merger_import_smoke.py').read_text(),'smoke','exec')
    def test_failure_before_promotion_preserves_live_service(self):self.exercise('smoke')
    def test_failed_health_rolls_back_pointer_and_python(self):self.exercise('health')
    def test_success_preserves_active_ui(self):self.exercise(None)

if __name__=='__main__':unittest.main()

"""Exercise the scoped web release and rollback against temporary immutable releases."""
from contextlib import ExitStack
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from production import website_reaudit_release as release
from production import route_uploader_scene_https as https


class WebOverlayRelease(unittest.TestCase):
    def exercise(self, fail=False):
        with tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
            root=Path(tmp);base=root/'host';repo=root/'repo';app='video-merger';revision='a'*40
            previous=base/'releases'/app/'old';previous.mkdir(parents=True)
            page=Path(release.FILES[app][0]);(previous/page).parent.mkdir(parents=True);(previous/page).write_text('old page')
            (previous/'worker.py').write_text('preserved-worker')
            (previous/'_production_manifest.json').write_text(json.dumps({'app':app,'revision':'old','files':{str(page):'old','worker.py':hashlib.sha256(b'preserved-worker').hexdigest()}}))
            current=base/'current'/app;current.parent.mkdir(parents=True);current.symlink_to(previous)
            source=repo/'apps'/app/page;source.parent.mkdir(parents=True);source.write_text('new approved page')
            for key,value in [('BASE',base),('REPO',repo)]:stack.enter_context(patch.object(release,key,value))
            stack.enter_context(patch.object(release,'idle'));stack.enter_context(patch.object(release,'http_check'));stack.enter_context(patch.object(release,'event'))
            commands=stack.enter_context(patch.object(release,'run'))
            stack.enter_context(patch.object(https,'scene_cookie',return_value='fixture'))
            stack.enter_context(patch.object(https,'https_get',return_value=(503,'text/html',b'bad') if fail else (200,'text/html',b'x'*2000)))
            if fail:
                with self.assertRaises(RuntimeError):release.install(app,revision)
                self.assertEqual(current.resolve(),previous)
                self.assertEqual(commands.call_count,2)
            else:
                release.install(app,revision)
                self.assertEqual((current.resolve()/page).read_text(),'new approved page')
                self.assertEqual((current.resolve()/'worker.py').read_text(),'preserved-worker')
                self.assertEqual(commands.call_count,1)
            self.assertEqual((previous/page).read_text(),'old page')
            for call in commands.call_args_list:self.assertEqual(call.args[0],['systemctl','restart','vps1-production-video-merger.service'])

    def test_active_stream_render_blocks_web_restart(self):
        for data in ({'encoder_sessions':1,'render_tasks':0},{'encoder_sessions':0,'render_tasks':1},{'encoder_sessions':0}):
            with patch.object(release,'urlopen',return_value=io.BytesIO(json.dumps(data).encode())):
                with self.assertRaises(RuntimeError):release.idle('short-stream')
        with patch.object(release,'urlopen',return_value=io.BytesIO(b'{"encoder_sessions":0,"render_tasks":0}')):release.idle('short-stream')

    def test_exact_web_overlay_preserves_workers_and_original(self):self.exercise()
    def test_failed_readback_restores_previous_pointer_and_web(self):self.exercise(fail=True)


if __name__=='__main__':unittest.main()

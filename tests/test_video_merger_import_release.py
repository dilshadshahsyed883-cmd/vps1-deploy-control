import json
import sqlite3
import hashlib
import io
import zipfile
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
            old_html=('''<button class="btn secondary mini" onclick="renameLibrary('${x.id}')"'''
                      + '\\nfunction renderProxy(){' + '\\n<!-- active parallel UI -->')
            (old/'part01/templates/index.html').write_text(old_html)
            canonical=(Path(__file__).resolve().parents[1]/'apps/video-merger/part01/templates/index.html').read_text()
            canonical_path=repo/'apps/video-merger/part01/templates/index.html'
            canonical_path.parent.mkdir(parents=True)
            canonical_path.write_text(canonical)
            (old/'_production_manifest.json').write_text(json.dumps({'app':'video-merger','files':{'part01/app.py':'old'}}))
            pointer=base/'current/video-merger';pointer.parent.mkdir();pointer.symlink_to(old)
            python=base/'venvs/video-merger/old/bin/python';python.parent.mkdir(parents=True);python.touch()
            unit=units/'vps1-production-video-merger.service'
            original=f'ExecStart={python} app.py\nEnvironment=PATH={python.parent}:/usr/bin\n'
            unit.write_text(original)
            src=repo/'apps/video-merger/part01/app.py';src.parent.mkdir(parents=True);src.write_text('new = True')
            # Independent baseline fixture: an already-enabled production proxy
            # remains unchanged; disabled saved profile is covered separately.
            cfg=root/'config/proxy_profiles.json'
            cfg.parent.mkdir(parents=True)
            cfg.write_text(json.dumps({'enabled':True,'active_id':'p1',
                'profiles':[{'id':'p1','host':'proxy.example.com','port':2222,
                             'username':'owner','password':'secret'}]}))
            cfg.chmod(0o600)
            def command(args,**kw):
                if args[:3]==['python3','-m','venv']:
                    path=Path(args[3]);(path/'bin').mkdir(parents=True);(path/'bin/python').touch()
                if failure=='smoke' and 'production.video_merger_import_smoke' in args:raise RuntimeError('source failure')
                return ''
            checks=[None,RuntimeError('health failure'),None,None] if failure=='health' else [None,None,None]
            with patch.object(release,'BASE',base),patch.object(release,'DATA',root/'data'),patch.object(release,'REPO',repo), \
                 patch.object(release,'SYSTEMD',units),patch.object(release,'idle_database'),patch.object(release,'event'), \
                 patch.object(release,'install_js'),patch.object(release,'live_paths',return_value=(root/'data/video-merger/video-merger',root/'config/proxy_profiles.json')), \
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
                    patched_html=(pointer/'part01/templates/index.html').read_text()
                    self.assertIn('active parallel UI',patched_html)
                    self.assertIn('retryLibrary(',patched_html)
                    self.assertIn('onclick="renameLibrary(',patched_html)
                    self.assertEqual((pointer/'part01/app.py').read_text(),'new = True')
                    self.assertIn('a'*40+'-imports/bin/python',unit.read_text())

    def test_retry_ui_guard_preserves_unrelated_active_markup(self):
        canonical=(Path(__file__).resolve().parents[1]/'apps/video-merger/part01/templates/index.html').read_text()
        old='''<button class="btn secondary mini" onclick="renameLibrary('${x.id}')"''' + '\\nfunction renderProxy(){' + '\\n<style>/* keep parallel UI */</style>'
        patched=release.overlay_failed_import_retry_ui(old,canonical)
        self.assertIn('keep parallel UI',patched)
        self.assertIn('onclick="retryLibrary(',patched)
        self.assertEqual(release.overlay_failed_import_retry_ui(patched,canonical),patched)
        with self.assertRaisesRegex(RuntimeError,'active Library action UI changed'):
            release.overlay_failed_import_retry_ui('function renderProxy(){',canonical)

    def test_read_only_idle_database_rejects_active_jobs_and_library_items(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);dbpath=root/'data/video_merger.db';dbpath.parent.mkdir()
            with sqlite3.connect(dbpath) as db:
                db.execute('CREATE TABLE jobs(status TEXT)')
                db.execute('CREATE TABLE library_items(status TEXT)')
                db.execute("INSERT INTO jobs VALUES('complete')")
                db.execute("INSERT INTO library_items VALUES('ready')")
            with patch.object(release,'event'):
                release.idle_database(root)
            with sqlite3.connect(dbpath) as db:
                db.execute("INSERT INTO library_items VALUES('importing')")
            with self.assertRaisesRegex(RuntimeError,'unfinished'):
                release.idle_database(root)
            with sqlite3.connect(dbpath) as db:
                db.execute("DELETE FROM library_items WHERE status='importing'")
                db.execute("INSERT INTO jobs VALUES('waiting')")
            with self.assertRaisesRegex(RuntimeError,'unfinished'):
                release.idle_database(root)

    def test_idle_gate_missing_or_incomplete_schema_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with self.assertRaisesRegex(RuntimeError,'DB missing'):
                release.idle_database(root)
            dbpath=root/'data/video_merger.db';dbpath.parent.mkdir()
            with sqlite3.connect(dbpath) as db:
                db.execute('CREATE TABLE jobs(status TEXT)')
            with self.assertRaisesRegex(RuntimeError,'schema invalid'):
                release.idle_database(root)

    def test_isolated_js_runtime_checksum_and_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            venv=Path(directory);(venv/'bin').mkdir()
            payload=io.BytesIO()
            with zipfile.ZipFile(payload,'w') as z:z.writestr('deno',b'official-runtime-fixture')
            data=payload.getvalue()
            with patch.object(release.urllib.request,'urlopen',return_value=io.BytesIO(data)), \
                 patch.object(release,'DENO_SHA256',hashlib.sha256(data).hexdigest()),patch.object(release,'run'):
                release.install_js(venv)
            self.assertEqual((venv/'bin/deno').read_bytes(),b'official-runtime-fixture')
            self.assertTrue((venv/'bin/deno').stat().st_mode & 0o111)
            with patch.object(release.urllib.request,'urlopen',return_value=io.BytesIO(b'invalid')):
                with self.assertRaisesRegex(RuntimeError,'digest mismatch'):release.install_js(venv)
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

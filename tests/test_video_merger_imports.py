import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SOURCE=Path(__file__).resolve().parents[1]/'apps/video-merger/part01/app.py'

class Imports(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        with patch.dict(os.environ,VIDEO_MERGER_ROOT=str(self.root),VIDEO_MERGER_PROXY_CONFIG_DIR=str(self.root/'config')):
            spec=importlib.util.spec_from_file_location('vm_test',SOURCE)
            self.app=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.app)
        self.app.init_db()
        self.dst=self.root/'test.bin'
    def tearDown(self):self.temp.cleanup()
    def test_module_launcher_uses_service_python(self):
        with patch.object(self.app.importlib.util,'find_spec',return_value=object()):
            self.assertEqual(self.app.downloader_command('yt_dlp'),[self.app.sys.executable,'-m','yt_dlp'])
            self.assertEqual(self.app.downloader_command('gdown'),[self.app.sys.executable,'-m','gdown'])
    def test_js_runtime_is_explicit_and_no_remote_solver_required(self):
        binary=self.root/'deno';binary.touch();self.app.DENO=binary
        args=self.app.yt_common_args()
        self.assertIn('deno:'+str(binary),args)
        self.assertNotIn('--remote-components',args)
    def test_private_cookie_file_opt_in_and_secret_redaction(self):
        cookies=self.root/'private-cookies.txt';cookies.write_text('# Netscape HTTP Cookie File')
        with patch.dict(os.environ,{'VIDEO_MERGER_YOUTUBE_COOKIES_FILE':str(cookies)}):
            args=self.app.yt_common_args()
            self.assertEqual(args[args.index('--cookies')+1],str(cookies))
            self.assertNotIn(str(cookies),self.app.scrub_proxy_error('private path '+str(cookies)))
        cookies.unlink()
        with patch.dict(os.environ,{'VIDEO_MERGER_YOUTUBE_COOKIES_FILE':str(cookies)}):
            with self.assertRaisesRegex(RuntimeError,'cookies file is unavailable'):
                self.app.yt_common_args()
    def test_proxy_test_requires_actual_media_bytes(self):
        profile={'id':'p','name':'p','host':'proxy.example','port':8080,'username':'a','password':'b'}
        self.app.save_proxy_config({'enabled':False,'active_id':'p','profiles':[profile]})
        calls=[]
        def fake_run(args,**kwargs):
            calls.append(args)
            if args[0]=='/usr/bin/curl': return '203.0.113.1'
            return 'jNQXAC9IVRw'
        with patch.object(self.app,'run',side_effect=fake_run), \
             patch.object(self.app,'downloader_command',return_value=['python','-m','yt_dlp']):
            with self.assertRaisesRegex(RuntimeError,'no media'):
                self.app.test_proxy_profile('p')
        self.assertFalse(any('--simulate' in x for x in calls))
        def media_run(args,**kwargs):
            if args[0]=='/usr/bin/curl': return '203.0.113.1'
            outfile=next(x for x in args if x.endswith('sample.%(ext)s'))
            Path(outfile.replace('%(ext)s','mp4')).write_bytes(b'video')
            return ''
        with patch.object(self.app,'run',side_effect=media_run), \
             patch.object(self.app,'downloader_command',return_value=['python','-m','yt_dlp']), \
             patch.object(self.app,'validate_downloaded_file'), \
             patch.object(self.app,'verify_library_media',return_value=1) as verify:
            result=self.app.test_proxy_profile('p')
            self.assertTrue(result['download_verified'])
            verify.assert_called_once()
        self.assertEqual(list(self.app.WORK.glob('vm-proxy-test-*')),[])
    def test_missing_dependency_actionable(self):
        with patch.object(self.app.importlib.util,'find_spec',return_value=None):
            with self.assertRaisesRegex(RuntimeError,'service Python'):self.app.downloader_command('gdown')
    def test_proxy_selection_encoding_and_direct_mode(self):
        profile={'id':'p','name':'p','host':'proxy.example','port':8080,'username':'a@b','password':'s:e/c'}
        self.app.save_proxy_config({'enabled':True,'active_id':'p','profiles':[profile]})
        args=self.app.yt_common_args()
        self.assertIn('http://a%40b:s%3Ae%2Fc@proxy.example:8080',args)
        self.assertNotIn('s:e/c',self.app.scrub_proxy_error('failed s:e/c'))
        self.app.set_proxy_enabled(False)
        args=self.app.yt_common_args();self.assertEqual(args[args.index('--proxy')+1],'')
    def test_invalid_enabled_proxy_fails_closed(self):
        self.app.save_proxy_config({'enabled':True,'active_id':'missing','profiles':[]})
        with self.assertRaisesRegex(RuntimeError,'profile is missing'):self.app.yt_common_args()
    def test_drive_launch_failure_still_uses_connected_account(self):
        with patch.object(self.app,'downloader_command',side_effect=RuntimeError('missing gdown')), \
             patch.object(self.app,'find_drive_item',return_value={'Path':'original.mp4'}), \
             patch.object(self.app,'rclone',side_effect=lambda *a,**k:self.dst.write_bytes(b'video')), \
             patch.object(self.app,'validate_downloaded_file'),patch.object(self.app,'verify_library_media') as valid:
            self.app.source_to_file('https://drive.google.com/file/d/abcdefghijk/view',self.dst,None,1)
            valid.assert_called_once_with(self.dst)
    def test_drive_public_html_rejected_and_fallback_validated(self):
        def run(*a,**k):self.dst.write_text('<html>permission required</html>')
        with patch.object(self.app,'downloader_command',return_value=['python','-m','gdown']), \
             patch.object(self.app,'run',side_effect=run),patch.object(self.app,'validate_downloaded_file'), \
             patch.object(self.app,'verify_library_media',side_effect=[RuntimeError('not media'),1]), \
             patch.object(self.app,'find_drive_item',return_value={'Path':'file.mp4'}), \
             patch.object(self.app,'rclone',side_effect=lambda *a,**k:self.dst.write_bytes(b'video')):
            self.app.source_to_file('https://drive.google.com/file/d/abcdefghijk/view',self.dst,None,1)
            self.assertEqual(self.dst.read_bytes(),b'video')
    def test_youtube_download_uses_proxy_and_media_validation(self):
        def run(args,**kw):
            self.assertEqual(args[:3],['python','-m','yt_dlp'])
            self.dst.with_suffix('.mp4').write_bytes(b'video')
            self.dst.with_suffix('.part').write_bytes(b'partial')
        with patch.object(self.app,'downloader_command',return_value=['python','-m','yt_dlp']), \
             patch.object(self.app,'run',side_effect=run),patch.object(self.app,'validate_downloaded_file'), \
             patch.object(self.app,'verify_library_media') as valid:
            self.app.source_to_file('https://youtu.be/jNQXAC9IVRw',self.dst,None,1)
            self.assertEqual(self.dst.read_bytes(),b'video');valid.assert_called_once()
    def test_library_persistence_and_existing_entry_preserved(self):
        now=self.app.utcnow()
        with self.app.db() as c:
            for identity in ['existing','new']:
                c.execute('INSERT INTO library_items(id,name,filename,created_at,updated_at) VALUES(?,?,?,?,?)',(identity,identity,identity+'.mp4',now,now))
        def source(url,dst,*a,**kw):dst.write_bytes(b'raw')
        def transcode(src,dst,*a):dst.write_bytes(b'final-video')
        def remote(args,**kw):
            return json.dumps({'ID':'new-drive-id','Size':11}) if args[0]=='lsjson' else ''
        before=self.app.get_library_item('existing')
        with patch.object(self.app,'source_to_file',side_effect=source),patch.object(self.app,'probe',return_value={}), \
             patch.object(self.app,'media_duration',return_value=2),patch.object(self.app,'transcode_library',side_effect=transcode), \
             patch.object(self.app,'verify_library_media',return_value=2),patch.object(self.app,'rclone',side_effect=remote):
            self.app.process_library_import('new','https://drive.google.com/file/d/abcdefghijk/view',{'url':'https://example.com/video.mp4','trim_enabled':False})
        item=self.app.get_library_item('new')
        self.assertEqual(item['status'],'ready');self.assertEqual(item['size_bytes'],11)
        self.assertEqual(self.app.get_library_item('existing'),before)
        self.assertEqual(list(self.app.WORK.iterdir()),[])
    def test_upload_mismatched_size_not_marked_ready(self):
        now=self.app.utcnow()
        with self.app.db() as c:c.execute('INSERT INTO library_items(id,name,filename,created_at,updated_at) VALUES(?,?,?,?,?)',('new','new','new.mp4',now,now))
        with patch.object(self.app,'source_to_file'),patch.object(self.app,'probe',return_value={}), \
             patch.object(self.app,'media_duration',return_value=2),patch.object(self.app,'transcode_library',side_effect=lambda s,d,*a:d.write_bytes(b'abc')), \
             patch.object(self.app,'verify_library_media',return_value=2),patch.object(self.app,'rclone',return_value=json.dumps({'ID':'x','Size':99})):
            self.app.process_library_import('new','https://example.com/file',{'url':'https://example.com/video.mp4','trim_enabled':False})
        self.assertEqual(self.app.get_library_item('new')['status'],'failed')


class VideoMergerSavedProxyActivationTests(unittest.TestCase):
    def _config(self, directory, *, enabled=False, active='saved'):
        path=Path(directory)/'proxy_profiles.json'
        data={'enabled':enabled,'active_id':active,
              'profiles':[{'id':'saved','name':'Previously working',
                           'host':'proxy.example.com','port':2222,
                           'username':'owner','password':'private-pass'}]}
        path.write_text(json.dumps(data),encoding='utf-8')
        path.chmod(0o600)
        return path

    def test_existing_saved_proxy_enables_only_after_canary_and_can_rollback(self):
        from production.video_merger_import_release import planned_proxy_activation,swap_proxy
        with tempfile.TemporaryDirectory() as directory:
            path=self._config(directory)
            original=path.read_bytes()
            activation=planned_proxy_activation(path)
            self.assertEqual(original,path.read_bytes(),'planning must not change live config')
            self.assertEqual(json.loads(activation['after'])['profiles'],
                             json.loads(original)['profiles'])
            swap_proxy(activation,activation['after'])
            self.assertTrue(json.loads(path.read_text())['enabled'])
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            self.assertEqual(path.stat().st_uid,activation['uid'])
            swap_proxy(activation,activation['before'])
            self.assertEqual(path.read_bytes(),original)

    def test_saved_proxy_selection_and_private_file_guards(self):
        from production.video_merger_import_release import planned_proxy_activation
        with tempfile.TemporaryDirectory() as directory:
            path=self._config(directory,active='missing')
            with self.assertRaisesRegex(RuntimeError,'no active saved'):
                planned_proxy_activation(path)
            path.unlink()
            path.symlink_to('/etc/passwd')
            with self.assertRaisesRegex(RuntimeError,'untrusted saved'):
                planned_proxy_activation(path)

    def test_already_enabled_proxy_preserves_current_preference(self):
        from production.video_merger_import_release import planned_proxy_activation
        with tempfile.TemporaryDirectory() as directory:
            path=self._config(directory,enabled=True)
            original=path.read_bytes()
            self.assertIsNone(planned_proxy_activation(path))
            self.assertEqual(path.read_bytes(),original)

    def test_smoke_uses_disposable_config_and_guards_media_drive_and_library(self):
        import production.video_merger_import_smoke as smoke
        code=Path(smoke.__file__).read_text()
        self.assertIn("trial['enabled'] = True",code)
        self.assertIn('app.PROXY_CONFIG_PATH = trial_path',code)
        self.assertIn("results['youtube'] = 'PASS'",code)
        self.assertIn("results['drive'] = 'PASS'",code)
        self.assertIn("results['library_persistence']",code)
        self.assertIn("results.get('proxy_test')!='REAL_DOWNLOAD_PASS'",code)

if __name__=='__main__':unittest.main()

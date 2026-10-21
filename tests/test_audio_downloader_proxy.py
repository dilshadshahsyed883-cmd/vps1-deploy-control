"""VS-ISS-20261021-AD-01: real worker, yt-dlp parser and isolated HTTP contracts."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class AudioProxyTests(unittest.TestCase):
    def setUp(self):
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest('FastAPI/httpx are installed and required by scoped Audio CI')
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'AUDIO_DOWNLOADER_ROOT': self.tmp.name})
        self.env.start()
        os.environ.pop('AUDIO_DOWNLOADER_COOKIES_FILE', None)
        spec = importlib.util.spec_from_file_location('proxy_test_audio', ROOT / 'apps/voice-studio/part01/audio_downloader_api.py')
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)
        self.m.init_storage()
        self.profile = {'id': 'qa', 'name': 'Test', 'host': 'proxy.example.com', 'port': 8080,
                        'username': 'u@ser', 'password': 'p:a ss'}
        self.config(enabled=True)
        app = FastAPI()
        app.include_router(self.m.router)
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.env.stop()
        self.tmp.cleanup()

    def config(self, enabled=True, active='qa'):
        self.m._write_proxy_config({'enabled': enabled, 'active_id': active, 'profiles': [self.profile]})

    def cookies(self, expiry=None):
        value = '# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t' + str(expiry or int(time.time()) + 3600) + '\tSID\tprivate-session-value\n'
        path = self.m.DATA / 'youtube-cookies.txt'
        path.write_text(value)
        path.chmod(0o600)
        return path

    def test_explicit_selected_proxy_snapshot_no_credential_argv_and_cleanup(self):
        with patch.dict(os.environ, {'NO_PROXY': '*', 'ALL_PROXY': 'http://unrelated:99'}):
            with self.m.youtube_execution() as (options, env):
                path = Path(options[-1])
                text = path.read_text()
                self.assertIn('--proxy http://u%40ser:p%3Aa%20ss@proxy.example.com:8080', text)
                self.assertNotIn('private-session', ' '.join(options))
                self.assertNotIn('proxy.example', ' '.join(options))
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertNotIn('NO_PROXY', env)
                self.assertEqual(env['HTTPS_PROXY'], 'http://u%40ser:p%3Aa%20ss@proxy.example.com:8080')
                self.config(active='missing')
                self.assertEqual(path.read_text(), text)
            self.assertFalse(path.exists())

    def test_disabled_proxy_is_explicit_direct_and_scrubs_inherited_environment(self):
        self.config(enabled=False)
        with patch.dict(os.environ, {'HTTPS_PROXY': 'http://unrelated:123', 'no_proxy': '*'}):
            with self.m.youtube_execution() as (options, env):
                self.assertIn("--proxy ''", Path(options[-1]).read_text())
                self.assertNotIn('HTTPS_PROXY', env)
                self.assertNotIn('no_proxy', env)

    def test_enabled_missing_profile_fails_closed_without_subprocess(self):
        self.config(active='missing')
        with self.assertRaisesRegex(self.m.DownloadFailure, 'PROXY_CONFIGURATION'):
            with self.m.youtube_execution():
                self.fail('must not enter')

    def test_corrupt_saved_config_never_falls_back_to_direct(self):
        self.m._PROXY_PATH.write_text('broken')
        with self.assertRaisesRegex(self.m.DownloadFailure, 'PROXY_CONFIGURATION'):
            self.m._read_proxy_config()

    def test_private_cookie_snapshot_never_overwrites_source(self):
        original = self.cookies()
        before = original.read_bytes()
        with self.m.youtube_execution() as (options, _):
            content = Path(options[-1]).read_text()
            import shlex
            line = next(x for x in content.splitlines() if x.startswith('--cookies'))
            copy = Path(shlex.split(line)[1])
            self.assertEqual(copy.read_bytes(), before)
            self.assertEqual(copy.stat().st_mode & 0o777, 0o600)
            copy.write_text('yt-dlp rotated session')
            self.assertNotIn('private-session-value', json.dumps(self.m.cookie_status()))
        self.assertFalse(copy.exists())
        self.assertEqual(original.read_bytes(), before)

    def test_cookie_expired_shared_symlink_and_malformed_rejected(self):
        path = self.cookies(expiry=1)
        with self.assertRaisesRegex(self.m.DownloadFailure, 'SESSION_CONFIGURATION'):
            self.m._cookie_bytes()
        self.cookies().chmod(0o644)
        with self.assertRaises(self.m.DownloadFailure):
            self.m._cookie_bytes()
        path.unlink()
        path.symlink_to('/etc/passwd')
        with self.assertRaises(self.m.DownloadFailure):
            self.m._cookie_bytes()

    def test_connectivity_http_never_claims_verified_download(self):
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
        with patch.object(self.m.urllib.request, 'build_opener') as builder:
            builder.return_value.open.return_value = Response()
            response = self.client.post('/tts/api/audio-downloader/proxy/profiles/qa/test')
        value = response.json()
        self.assertTrue(value['connectivity_ok'])
        self.assertTrue(value['used_by_worker'])
        self.assertFalse(value['download_verified'])
        self.assertIn('NOT verified', value['message'])
        status = self.client.get('/tts/api/audio-downloader/status').json()
        self.assertEqual(status['proxy']['worker_profile_id'], 'qa')
        self.assertEqual(status['youtube_session']['state'], 'not_configured')

    def job(self):
        return {'job_id': 'ADJ-' + 'A'*16, 'video_id': 'BaW_jenozKc',
                'url': 'https://www.youtube.com/watch?v=BaW_jenozKc', 'format': 'mp3',
                'quality': 128, 'keep_metadata': False, 'auto_upload': False}

    def test_worker_uses_private_config_and_auth_failure_runs_once(self):
        result = subprocess.CompletedProcess([], 1, '', "Sign in to confirm you're not a bot: p:a ss")
        calls = []
        def execute(args, **kw):
            calls.append(args)
            self.assertIn('--config-locations', args)
            text = Path(args[args.index('--config-locations')+1]).read_text()
            self.assertIn('--extractor-retries 0', text)
            self.assertNotIn('p:a ss', ' '.join(args))
            return result
        with patch.object(self.m, 'deps', return_value={'ytdlp': '/fake/yt-dlp', 'ffmpeg': '/fake/ffmpeg'}), patch.object(self.m.subprocess, 'run', side_effect=execute):
            with self.assertRaisesRegex(self.m.DownloadFailure, 'YOUTUBE_AUTH_REQUIRED') as error:
                self.m.run_job(self.job())
        self.assertEqual(len(calls), 1)
        self.assertNotIn('p:a ss', str(error.exception))

    def test_successful_worker_preserves_audio_output_and_completion(self):
        job = self.job()
        updates = []
        def execute(args, **kw):
            output = Path(kw['cwd']) / 'test.mp3'
            output.write_bytes(b'ID3' + b'x'*2048)
            return subprocess.CompletedProcess(args, 0, str(output) + '\n', '')
        with patch.object(self.m, 'deps', return_value={'ytdlp': '/fake/yt-dlp', 'ffmpeg': '/fake/ffmpeg'}), patch.object(self.m.subprocess, 'run', side_effect=execute), patch.object(self.m, '_update', side_effect=lambda jid, state, **kw: updates.append((state, kw))):
            self.m.run_job(job)
        self.assertEqual([x[0] for x in updates], ['downloaded', 'completed'])
        self.assertTrue(Path(updates[0][1]['local_file']).is_file())

    def test_error_categories_are_truthful_and_do_not_leak_credentials(self):
        for raw, code in [("Sign in to confirm you’re not a bot", 'YOUTUBE_AUTH_REQUIRED'),
                          ('HTTP Error 407: Proxy Authentication Required', 'PROXY_AUTH_FAILED'),
                          ('PO Token required', 'YOUTUBE_PROVIDER_CHALLENGE'),
                          ('HTTP Error 429', 'YOUTUBE_RATE_LIMIT'), ('HTTP Error 403', 'YOUTUBE_ACCESS_DENIED')]:
            error = self.m.download_failure(raw + ' p:a ss')
            self.assertEqual(error.code, code)
            self.assertNotIn('p:a ss', str(error))

    def test_real_ytdlp_parser_respects_explicit_private_config(self):
        try:
            import yt_dlp
        except ImportError:
            self.skipTest('yt-dlp is installed and required by scoped Audio CI')
        with self.m.youtube_execution() as (args, _):
            opts = yt_dlp.parse_options(args + ['--simulate', self.job()['url']]).ydl_opts
            self.assertEqual(opts['proxy'], 'http://u%40ser:p%3Aa%20ss@proxy.example.com:8080')
            self.assertEqual(opts['extractor_retries'], 0)
            self.assertEqual(opts['retries'], 1)


class AudioReleaseFenceTests(unittest.TestCase):
    def test_failed_health_restores_previous_release(self):
        import production.audio_downloader_release as m
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            releases = base / 'releases'
            old = releases / 'previous'
            old.mkdir(parents=True)
            (old / m.MODULE).parent.mkdir(parents=True)
            (old / m.MODULE).write_text('def previous(): pass\n')
            (old / '_production_manifest.json').write_text(json.dumps({'app': 'voice-studio', 'files': {str(m.MODULE): 'old'}}))
            current = base / 'current'
            current.symlink_to(old)
            root = base / 'owner-data'
            (root / 'data').mkdir(parents=True)
            with sqlite3.connect(root / 'data/jobs.sqlite3') as db:
                db.execute('CREATE TABLE jobs (job_id TEXT, status TEXT)')
                db.execute("INSERT INTO jobs VALUES('owner-job','failed')")
            with patch.multiple(m, CURRENT=current, RELEASES=releases, BASE=base), patch.object(m, 'live_environment', return_value={'AUDIO_DOWNLOADER_ROOT': str(root)}), patch.object(m, 'run'), patch.object(m, 'http_check'), patch.object(m, 'verify', side_effect=RuntimeError('injected health failure')), patch.object(m, 'event'):
                with self.assertRaisesRegex(RuntimeError, 'injected health'):
                    m.install('a'*40)
            self.assertEqual(current.resolve(), old)
            with sqlite3.connect(root / 'data/jobs.sqlite3') as db:
                self.assertEqual(db.execute('SELECT * FROM jobs').fetchall(), [('owner-job', 'failed')])
            with sqlite3.connect(base / 'transactions/audio-proxy-only' / ('a'*40) / 'jobs.sqlite3') as db:
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_busy_queue_vetoes_release_without_job_changes(self):
        from production.audio_downloader_release import lock_idle
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'jobs.sqlite3'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE jobs (job_id TEXT, status TEXT)')
                db.execute("INSERT INTO jobs VALUES('owner-job','downloading')")
            with self.assertRaisesRegex(RuntimeError, 'busy'):
                lock_idle(path)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute('SELECT * FROM jobs').fetchall(), [('owner-job', 'downloading')])
                db.execute("UPDATE jobs SET status='failed'")
            fence = lock_idle(path)
            with sqlite3.connect(path, timeout=0.01) as db:
                with self.assertRaises(sqlite3.OperationalError):
                    db.execute("INSERT INTO jobs VALUES('racing-job','queued')")
            fence.close()

    def test_installer_mutates_only_audio_module_preserving_template_and_workers(self):
        import production.audio_downloader_release as release
        self.assertEqual(str(release.MODULE), 'part01/audio_downloader_api.py')
        source = Path(release.__file__).read_text()
        self.assertIn('live.backup(backup)', source)
        self.assertIn('atomic_link(CURRENT, previous)', source)
        self.assertNotIn('kokoro-worker.service', source)
        self.assertNotIn('edge-worker.service', source)


    def test_smoke_uses_absolute_runuser_even_with_service_restricted_path(self):
        """Reproduce protected release: live service PATH excludes /usr/sbin."""
        import production.audio_downloader_release as release
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / 'python'
            exe.write_text('#! test executable placeholder')
            report = '{"result":"YOUTUBE_PROVIDER_CHALLENGE","audio":"NOT_VERIFIED","drive":"NOT_VERIFIED"}'
            class FakeProcess:
                returncode = 0
                pid = 12345
                def communicate(self, timeout):
                    return (report + '\n', '')
            with patch.object(release.subprocess, 'Popen', return_value=FakeProcess()) as start:
                with patch.object(release, 'event'):
                    output = release.smoke(ROOT, {'PATH': tmp})
            command = start.call_args.args[0]
            self.assertTrue(Path(command[0]).is_absolute())
            self.assertEqual(Path(command[0]).name, 'runuser')
            self.assertEqual(command[1:4], ['-u', 'ubuntu', '--'])
            self.assertEqual(command[4], str(exe))
            self.assertEqual(start.call_args.kwargs['env']['PATH'], tmp)
            self.assertEqual(output['result'], 'YOUTUBE_PROVIDER_CHALLENGE')

if __name__ == '__main__':
    unittest.main()

"""Real FFmpeg/API tests. FakeDrive tests cover failure semantics, never live Drive proof."""
import concurrent.futures
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / 'apps/song-maker/part01'
sys.path.insert(0, str(SOURCE))
# Keep module-level app initialization isolated from production/default state.
_MODULE_STATE = tempfile.TemporaryDirectory()
os.environ['SONG_MAKER_DATA'] = _MODULE_STATE.name
try:
    from songmaker.store import Store
    from songmaker.media import pairs, output_name, render, validate, run, Cancelled, fingerprint, digest
    from songmaker.drive import sync_owner, Drive
    from songmaker.worker import process, cleanup
    from songmaker.app import create_app
    from fastapi.testclient import TestClient
    from PIL import Image
except ImportError as exc:
    if os.environ.get('SONG_MAKER_REQUIRE_DEPS') == '1':
        raise
    raise unittest.SkipTest('Song Maker media/API dependencies are exercised by dedicated Song Maker CI') from exc



class SongMakerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.client = TestClient(create_app(self.store))
        self.headers = {'X-Auth-User': 'alice', 'X-Is-Admin': '1', 'X-Song-Maker': '1'}

    def test_pause_is_durable_owner_scoped_and_does_not_cancel_active(self):
        first = self.store.enqueue('alice', {'n': 1}, {'local_name': 'one.mp4'})
        self.assertEqual(self.client.post('/song-maker/api/queue/pause', headers=self.headers).status_code, 200)
        self.assertIsNone(self.store.claim('render'))
        self.assertTrue(Store(self.root).paused('alice'))
        other = self.store.enqueue('bob', {'n': 2}, {'local_name': 'two.mp4'})
        self.assertEqual(self.store.claim('render')['id'], other['id'])
        self.assertEqual(self.store.get(first['id'])['cancel'], 0)
        self.client.post('/song-maker/api/queue/resume', headers=self.headers)
        self.assertEqual(self.store.claim('render')['id'], first['id'])
        self.assertEqual(self.client.get('/song-maker/api/state', headers=self.headers).json()['queue_paused'], False)

    def test_shared_input_scan_is_one_listing_and_respects_selected_interval(self):
        from songmaker.drive import default_settings
        settings = default_settings()
        class Counting:
            calls = []
            def listing(self, folder):
                self.calls.append(folder)
                return []
        drive = Counting()
        sync_owner(self.store, 'alice', settings, drive)
        self.assertEqual(len(drive.calls), 1)
        self.assertEqual(self.client.post('/song-maker/api/settings', headers=self.headers,
            json={**settings, 'sync_interval': 300}).status_code, 200)
        self.assertEqual(self.client.post('/song-maker/api/settings', headers=self.headers,
            json={**settings, 'sync_interval': 1}).status_code, 422)
        # Authenticated non-admin members may scan the fixed defaults, never
        # change the configured Drive remote/folder or scan interval.
        self.assertEqual(self.client.post('/song-maker/api/sync',
            headers={**self.headers, 'X-Is-Admin': '0'}).status_code, 200)
        self.assertEqual(self.store.record('settings','alice')['image_folder'],
                         default_settings()['image_folder'])

    def test_drive_lookup_filters_names_and_retains_hash_metadata(self):
        with patch('songmaker.drive.run', return_value='[]') as execute:
            Drive({'remote': 'gdrive'}).lookup('imageFolder', 'song [1]*.jpg')
            args = execute.call_args.args[0]
            self.assertIn('--hash', args)
            self.assertIn('--include', args)
            self.assertIn(r'/song \[1\]\*.jpg', args)

    def test_ffmpeg_performance_is_measured_from_process_output(self):
        values = []
        run([sys.executable, '-u', '-c',
             'import time; print("frame=25\\nfps=42.5\\nspeed=1.7x\\nout_time_us=1000000", flush=True); time.sleep(1)'],
             metrics=values.append)
        self.assertTrue(any(v.get('render_fps') == 42.5 and v.get('render_speed') == 1.7 for v in values), values)

    def test_drive_rate_limit_backoff_is_read_only_and_redacts_private_errors(self):
        drive = Drive({'remote': 'gdrive'})
        secret = 'https://private.example/token-secret userRateLimitExceeded'
        with patch('songmaker.drive.run', side_effect=[RuntimeError(secret), '[]']) as execute, \
             patch('songmaker.drive.time.sleep'):
            self.assertEqual(drive.listing('imageFolder'), [])
            self.assertEqual(execute.call_count, 2)
            self.assertIn('--tpslimit', execute.call_args.args[0])
            self.assertIn('--tpslimit-burst', execute.call_args.args[0])
        for operation in (['moveto', 'gdrive:a', 'gdrive:b', '--ignore-existing'],
                          ['copyto', 'a', 'gdrive:b', '--ignore-existing']):
            with patch('songmaker.drive.run', side_effect=RuntimeError(secret)) as execute:
                with self.assertRaisesRegex(RuntimeError, 'userRateLimitExceeded') as caught:
                    drive.call(operation)
                self.assertNotIn('token-secret', str(caught.exception))
                self.assertEqual(execute.call_count, 1)  # no blind mutation replay
        with patch('songmaker.drive.run', side_effect=RuntimeError('storageQuotaExceeded')) as execute:
            with self.assertRaisesRegex(RuntimeError, 'storageQuotaExceeded'):
                drive.listing('imageFolder')
            self.assertEqual(execute.call_count, 1)  # a full Drive is not a transient rate limit
        with patch('songmaker.drive.run', side_effect=RuntimeError(secret)) as execute, \
             patch('songmaker.drive.time.sleep'):
            with self.assertRaises(RuntimeError):
                drive.listing('imageFolder')
            self.assertEqual(execute.call_count, 3)  # bounded metadata retry
        with patch('songmaker.drive.run', side_effect=RuntimeError(secret)):
            with self.assertRaises(Cancelled):
                Drive({'remote':'gdrive'}, lambda: True).listing('imageFolder')

    def test_drive_cooldown_is_durable_and_keeps_local_rendering_available(self):
        first = self.store.enqueue('alice', {'drive':1}, {'origin':'drive','local_name':'drive.mp4'})
        manual = self.store.enqueue('bob', {'manual':1}, {'origin':'manual','local_name':'local.mp4'})
        self.store.drive_after(280)
        self.store.drive_after(200)  # another lane cannot shorten an existing cooldown
        self.assertEqual(Store(self.root).drive_after(), 280)
        with patch('songmaker.store.time.time', return_value=100):
            self.assertEqual(self.store.claim('render')['id'], manual['id'])
            self.assertIsNone(self.store.claim('render'))
        with patch('songmaker.store.time.time', return_value=281):
            self.assertEqual(self.store.claim('render')['id'], first['id'])

    def test_provider_limit_automatically_resumes_saved_upload_after_restart(self):
        job = self.submit()
        process(self.store, self.store.claim('render'), 'render')
        class Limited:
            def __init__(self, *args): pass
            def upload(self, *args):
                raise RuntimeError('Drive operation failed (rateLimitExceeded); provider quota')
        with patch('songmaker.worker.time.time', return_value=100):
            process(self.store, self.store.claim('upload'), 'upload', drive_factory=Limited)
            waiting = self.store.get(job['id'])
            self.assertEqual(waiting['stage'], 'Waiting for Drive')
            self.assertEqual(waiting['next_retry_at'], 280)
            self.assertEqual(waiting['retry_lane'], 'upload')
            self.assertEqual(waiting['drive_retry_count'], 1)
            self.assertIsNone(self.store.claim('upload'))
        restarted = Store(self.root)
        output = self.root/'outputs'/job['id']/waiting['local_name']
        before = digest(output)
        class Good:
            def __init__(self, *args): pass
            def upload(self, *args): return {'remote_id':'verified-qa','verified_at':281,'drive_status':'Verified'}
        with patch('songmaker.worker.time.time', return_value=281):
            claim = restarted.claim('upload')
            self.assertEqual(claim['id'], job['id'])
            process(restarted, claim, 'upload', drive_factory=Good)
        complete = restarted.get(job['id'])
        self.assertEqual(complete['stage'], 'Completed')
        self.assertIsNone(complete['next_retry_at'])
        self.assertEqual(digest(output), before)

    def test_cancelled_provider_retry_cannot_be_claimed_when_due(self):
        job = self.store.enqueue('alice', {'retry':1}, {'origin':'drive','local_name':'test.mp4'})
        self.store.update(job['id'], stage='Waiting for Drive', retry_lane='render', next_retry_at=100)
        self.assertEqual(self.client.post('/song-maker/api/jobs/'+job['id']+'/cancel',headers=self.headers).status_code,200)
        with patch('songmaker.store.time.time', return_value=101):
            self.assertIsNone(self.store.claim('render'))

    def test_song_studio_shared_drive_defaults_and_explicit_overrides(self):
        from songmaker.drive import DEFAULT_INPUT, DEFAULT_OUTPUT, folder_ref, default_settings
        self.assertEqual(DEFAULT_INPUT, 'Song Studio/Audio downloader')
        self.assertEqual(DEFAULT_OUTPUT, 'Song Studio/song maker')
        defaults = default_settings()
        self.assertEqual(defaults['image_folder'], defaults['audio_folder'])
        self.assertEqual(defaults['output_folder'], DEFAULT_OUTPUT)
        response = self.client.get('/song-maker/api/state', headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['settings']['audio_folder'], DEFAULT_INPUT)
        self.assertEqual(self.store.record('settings', 'alice'), {})  # read never mutates settings
        response = self.client.post('/song-maker/api/sync', headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.store.record('settings', 'alice')['image_folder'], DEFAULT_INPUT)

        response = self.client.post('/song-maker/api/settings', headers=self.headers,
            json={**defaults, 'auto_sync': True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.store.record('settings', 'alice')['audio_folder'], DEFAULT_INPUT)
        legacy = {**defaults, 'image_folder': 'imageFolder',
                  'audio_folder': 'audioFolder', 'output_folder': 'outputFolder'}
        response = self.client.post('/song-maker/api/settings', headers=self.headers, json=legacy)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.store.record('settings', 'alice')['image_folder'], 'imageFolder')
        for unsafe in ('../escape', '/absolute/folder', 'Song Studio/../escape',
                       'Song Studio//Audio downloader', 'gdrive:Song Studio/Audio downloader'):
            with self.assertRaises(ValueError):
                folder_ref(unsafe)

    def test_named_drive_transport_and_same_folder_media_pairing(self):
        from songmaker.drive import DEFAULT_INPUT, default_settings
        settings = default_settings()
        with patch('songmaker.drive.run', return_value='[]') as execute:
            Drive(settings).listing(DEFAULT_INPUT)
            args = execute.call_args.args[0]
            self.assertIn('gdrive:Song Studio/Audio downloader/', args)
            self.assertNotIn('--drive-root-folder-id', args)
            Drive({'remote': 'gdrive'}).listing('imageFolder')
            args = execute.call_args.args[0]
            self.assertIn('--drive-root-folder-id', args)
            self.assertIn('imageFolder', args)

        class Shared:
            calls = 0
            def listing(_, folder):
                _.calls += 1
                self.assertEqual(folder, DEFAULT_INPUT)
                return [self.item('BaW_jenozKc.jpg', 'img-1'),
                        self.item('BaW_jenozKc.mp3', 'sound-1')]
        shared = Shared()
        summary = sync_owner(self.store, 'alice', settings, shared)
        self.assertEqual(shared.calls, 1)  # One shared-folder scan, not two
        self.assertEqual(summary['matched'], 1)
        jobs = self.store.jobs('alice')
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['destination']['output_folder'], 'Song Studio/song maker')

    def archive_fixture(self):
        from songmaker.drive import DEFAULT_INPUT, default_settings
        settings = default_settings()
        image, audio = self.media()
        jpg = self.root / 'Pair.jpg'
        jpg.write_bytes(image.read_bytes())
        files = {DEFAULT_INPUT: [
            {'Name': 'Pair.jpg', 'ID': 'source-image-1', 'Size': jpg.stat().st_size,
             'ModTime': '1', 'Hashes': {'MD5': digest(jpg, 'md5')}},
            {'Name': 'Pair.wav', 'ID': 'source-audio-1', 'Size': audio.stat().st_size,
             'ModTime': '1', 'Hashes': {'MD5': digest(audio, 'md5')}}
        ], DEFAULT_INPUT + '/used': []}
        source = {'remote': 'gdrive', 'image_folder': DEFAULT_INPUT,
                  'audio_folder': DEFAULT_INPUT,
                  'image': dict(files[DEFAULT_INPUT][0]), 'audio': dict(files[DEFAULT_INPUT][1])}
        job = self.store.enqueue('alice', source,
            {'origin': 'drive', 'source': source, 'destination': settings,
             'image_name': 'Pair.jpg', 'audio_name': 'Pair.wav',
             'local_name': 'Pair.mp4', 'project': 'Pair'})
        calls = []
        flags = {'fail_audio_once': False}
        owner_test = self
        class Fake(Drive):
            listing_log = []
            def listing(_, folder):
                _.listing_log.append(folder)
                return [dict(x) for x in files.get(folder, [])]
            def call(_, args, folder=None, timeout=300):
                if args[0] == 'mkdir':
                    files.setdefault(args[1].split(':', 1)[1], [])
                    return ''
                if args[0] != 'moveto':
                    raise AssertionError('Unexpected Drive mutation: ' + repr(args))
                owner_test.assertIn('--ignore-existing', args)
                assert owner_test.store.get(job['id'])['valid']
                assert owner_test.store.get(job['id'])['stage'] == 'Archiving Inputs'
                src, dst = (x.split(':', 1)[1] for x in args[1:3])
                src_folder, src_name = src.rsplit('/', 1)
                dst_folder, dst_name = dst.rsplit('/', 1)
                if flags['fail_audio_once'] and src_name.endswith('.wav'):
                    flags['fail_audio_once'] = False
                    raise RuntimeError('Drive temporary failure')
                original = next(x for x in files[src_folder] if x['Name'] == src_name)
                assert not any(x['Name'] == dst_name for x in files[dst_folder])
                files[src_folder].remove(original)
                files[dst_folder].append({**original, 'Name': dst_name})
                calls.append((src_name, dst_name))
                return ''
            def download(_, folder, item, target):
                assert folder == DEFAULT_INPUT
                target.write_bytes((jpg if item['Name'].endswith('.jpg') else audio).read_bytes())
            def upload(_, job, store):
                assert len(files[DEFAULT_INPUT]) == 0
                assert store.get(job['id'])['archive_status'] == 'Archived'
                return {'remote_id': 'output-verified', 'verified_at': time.time(),
                        'drive_status': 'Verified'}
        return job, settings, files, calls, flags, Fake

    def test_render_archives_inputs_immediately_before_upload(self):
        from songmaker.drive import DEFAULT_INPUT
        job, settings, files, calls, flags, Fake = self.archive_fixture()
        process(self.store, self.store.claim('render'), 'render', drive_factory=Fake)
        ready = self.store.get(job['id'])
        self.assertEqual(ready['stage'], 'Ready to Upload', ready)
        self.assertTrue(ready['valid'])
        self.assertEqual(ready['archive_status'], 'Archived')
        self.assertEqual([name for name, _ in calls], ['Pair.jpg', 'Pair.wav'])
        # Two initial reads plus two after each mutation, no duplicate pre/post scans.
        self.assertEqual(len(Fake.listing_log), 6)
        self.assertFalse(files[DEFAULT_INPUT])
        self.assertEqual({x['ID'] for x in files[DEFAULT_INPUT + '/used']},
                         {'source-image-1', 'source-audio-1'})
        # MP4 upload has not run, but archive is already finished.
        self.assertFalse(ready.get('remote_id'))
        process(self.store, self.store.claim('upload'), 'upload', drive_factory=Fake)
        self.assertEqual(self.store.get(job['id'])['stage'], 'Completed')

    def test_archive_partial_failure_collision_retry_without_rerender(self):
        from songmaker.drive import DEFAULT_INPUT
        job, settings, files, calls, flags, Fake = self.archive_fixture()
        files[DEFAULT_INPUT + '/used'].append({
            'Name': 'Pair.jpg', 'ID': 'older-file', 'Size': 42, 'Hashes': {}})
        flags['fail_audio_once'] = True
        process(self.store, self.store.claim('render'), 'render', drive_factory=Fake)
        paused = self.store.get(job['id'])
        self.assertEqual(paused['stage'], 'Needs Attention')
        self.assertTrue(paused['valid'])
        self.assertEqual(paused['archive_moves']['image']['name'], 'Pair (2).jpg')
        self.assertEqual(len(calls), 1)
        self.assertEqual({x['ID'] for x in files[DEFAULT_INPUT + '/used']},
                         {'older-file', 'source-image-1'})
        output = self.root/'outputs'/job['id']/job['local_name']
        before = digest(output)
        self.assertFalse(self.store.claim('upload'))
        response = self.client.post('/song-maker/api/jobs/'+job['id']+'/retry',
                                    headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.store.get(job['id'])['stage'], 'Queued')
        with patch('songmaker.worker.render', side_effect=AssertionError('must not rerender')):
            process(self.store, self.store.claim('render'), 'render', drive_factory=Fake)
        done = self.store.get(job['id'])
        self.assertEqual(done['stage'], 'Ready to Upload', done)
        self.assertEqual(done['archive_status'], 'Archived')
        self.assertEqual(digest(output), before)
        self.assertEqual(len(calls), 2)  # image was not moved twice
        self.assertEqual({x['ID'] for x in files[DEFAULT_INPUT + '/used']},
                         {'older-file', 'source-image-1', 'source-audio-1'})
        self.assertFalse(files[DEFAULT_INPUT])

    def test_failed_render_never_moves_drive_inputs(self):
        from songmaker.drive import DEFAULT_INPUT
        job, settings, files, calls, flags, Fake = self.archive_fixture()
        with patch('songmaker.worker.render', side_effect=RuntimeError('render failed')):
            process(self.store, self.store.claim('render'), 'render', drive_factory=Fake)
        self.assertEqual(self.store.get(job['id'])['stage'], 'Failed')
        self.assertFalse(calls)
        self.assertEqual(len(files[DEFAULT_INPUT]), 2)

    def item(self, name, identity='id', version=1):
        return {'Name': name, 'ID': identity, 'Size': version, 'ModTime': str(version), 'Hashes': {}}

    def media(self, duration=1.27):
        image = self.root/'picture.png'
        Image.new('RGB', (120, 180), '#167cba').save(image)
        audio = self.root/'آواز Song 01.wav'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                        f'sine=frequency=440:duration={duration}', str(audio)], check=True)
        return image, audio

    def submit(self, audio_name='آواز Song 01.wav'):
        image, audio = self.media()
        response = self.client.post('/song-maker/api/jobs', headers=self.headers,
            files={'image': ('picture.PNG', image.read_bytes(), 'image/png'),
                   'audio': (audio_name, audio.read_bytes(), 'audio/wav')},
            data={'resolution': '720p', 'fit': 'blur'})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_audio_naming_unicode_spaces_and_traversal(self):
        for name, expected in [('Song 01.mp3', 'Song 01.mp4'), ('My Beautiful Voice.wav', 'My Beautiful Voice.mp4'),
                               ('آواز.m4a', 'آواز.mp4'), ('Ronaldo CR7.M4A', 'Ronaldo CR7.mp4')]:
            self.assertEqual(output_name(name), expected)
        self.assertNotIn('/', output_name('../voice.wav'))
        self.assertNotIn('\\', output_name('..\\voice.wav'))
        self.assertLess(len(output_name('ا'*300+'.wav').encode()), 256)

    def test_batch_100_missing_conflicts_and_unicode(self):
        images = [self.item(f'Song {n}.JPG', f'i{n}') for n in range(100)]
        audio = [self.item(f'song {n}.MP3', f'a{n}') for n in range(100)]
        self.assertEqual(sum(p['stage']=='Matched' for p in pairs(images,audio)),100)
        result = pairs([self.item('Wait.jpg'),self.item('Conflict.png'),self.item('Conflict.webp'),
                        self.item('Cafe\u0301.JPG')],
                       [self.item('Other.wav'),self.item('Conflict.wav'),self.item('Café.mp3')])
        self.assertEqual(sorted(p['stage'] for p in result), sorted([
            'Waiting for Audio','Waiting for Image','Conflict / Needs Selection','Matched']))

    def test_incremental_sync_idempotence_and_versions(self):
        settings={'remote':'gdrive','image_folder':'imageFolder','audio_folder':'audioFolder','output_folder':'outputFolder'}
        class Fake:
            images=[self.item(f'{n}.jpg','i'+str(n)) for n in range(100)]
            audio=[self.item(f'{n}.mp3','a'+str(n)) for n in range(100)]
            def listing(_, folder): return _.images if folder=='imageFolder' else _.audio
        drive=Fake()
        sync_owner(self.store,'alice',settings,drive)
        sync_owner(self.store,'alice',settings,drive)
        self.assertEqual(len(self.store.jobs('alice')),100)
        drive.audio[0]=self.item('0.mp3','a0',2)
        sync_owner(self.store,'alice',settings,drive)
        self.assertEqual(len(self.store.jobs('alice')),101)
        self.assertFalse(list((self.root/'inputs').iterdir()))

    def test_manual_auth_isolation_and_duplicate_submission(self):
        self.assertEqual(self.client.get('/song-maker/').status_code,401)
        job=self.submit()
        again=self.submit()
        self.assertEqual(job['id'],again['id'])
        other={'X-Auth-User':'bob','X-Song-Maker':'1'}
        self.assertEqual(self.client.get('/song-maker/api/state',headers=other).json()['jobs'],[])
        self.assertEqual(self.client.post('/song-maker/api/jobs/'+job['id']+'/cancel',headers=other).status_code,404)
        self.assertEqual(self.client.post('/song-maker/api/settings',headers=other,json={}).status_code,403)
        forged={**self.headers,'Origin':'https://evil.example'}
        self.assertEqual(self.client.post('/song-maker/api/sync',headers=forged).status_code,403)
        self.assertEqual(self.client.post('/song-maker/api/sync',headers={'X-Auth-User':'alice'}).status_code,403)

    def test_real_render_full_audio_and_download_before_drive(self):
        job=self.submit()
        claim=self.store.claim('render')
        process(self.store,claim,'render')
        ready=self.store.get(job['id'])
        self.assertEqual(ready['stage'],'Ready to Upload',ready)
        self.assertTrue(ready['valid'])
        self.assertAlmostEqual(ready['duration'],1.27,places=2)
        output=self.root/'outputs'/job['id']/ready['local_name']
        data=validate(output,1.27)
        self.assertGreater(data['size'],1000)
        self.assertEqual(self.client.get('/song-maker/api/jobs/'+job['id']+'/video',headers=self.headers).status_code,200)
        # This test verifies the offline-failure state and local download. Never
        # touch the VPS's real rclone config during release preflight.
        class OfflineDrive:
            def __init__(self, *_args):
                pass
            def upload(self, *_args):
                raise RuntimeError('disposable offline Drive fixture')
        process(self.store,self.store.claim('upload'),'upload',drive_factory=OfflineDrive)
        self.assertEqual(self.store.get(job['id'])['stage'],'Waiting for Drive')
        self.assertTrue(output.exists())

    def test_fifo_transactional_claim_and_restart(self):
        for n in range(5):
            self.store.enqueue('alice',{'n':n},{'local_name':f'{n}.mp4'})
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            claims=list(pool.map(lambda _:self.store.claim('render'),range(5)))
        self.assertEqual(len({j['id'] for j in claims}),5)
        # Recovery is invoked by lane process only after obtaining its lifetime flock.
        restarted=Store(self.root)
        restarted.recover('render')
        claimed=restarted.claim('render')
        self.assertEqual(claimed['local_name'],'0.mp4')

    def test_cancel_retry_and_protected_delete(self):
        job=self.submit()
        path='/song-maker/api/jobs/'+job['id']
        self.assertEqual(self.client.post(path+'/delete',headers=self.headers).status_code,409)
        claim=self.store.claim('render')
        self.client.post(path+'/cancel',headers=self.headers)
        self.assertEqual(self.client.post(path+'/delete',headers=self.headers).status_code,409)
        process(self.store,claim,'render')
        self.assertEqual(self.store.get(job['id'])['stage'],'Cancelled')
        self.assertEqual(self.client.post(path+'/retry',headers=self.headers).status_code,200)
        self.assertEqual(self.store.get(job['id'])['stage'],'Queued')
        self.client.post(path+'/cancel',headers=self.headers)
        self.assertEqual(self.client.post(path+'/delete',headers=self.headers).status_code,200)
        self.assertEqual(self.store.jobs('alice'),[])
        # Tombstone prevents a deleted Drive version from being auto-submitted again.
        self.assertIsNotNone(self.store.get(job['id']))

    def test_exact_process_cancellation(self):
        with self.assertRaises(Cancelled):
            run([sys.executable,'-c','import time; time.sleep(10)'],lambda:True)
        self.assertEqual(run([sys.executable,'-c','print(42)']).strip(),'42')

    def test_ffmpeg_failure_is_not_valid_or_uploadable(self):
        job=self.submit()
        with patch('songmaker.worker.render',side_effect=RuntimeError('FFmpeg failure')):
            process(self.store,self.store.claim('render'),'render')
        self.assertEqual(self.store.get(job['id'])['stage'],'Failed')
        self.assertIsNone(self.store.claim('upload'))

    def test_upload_failure_retry_no_rerender_and_cleanup(self):
        job=self.submit()
        process(self.store,self.store.claim('render'),'render')
        settings={'remote':'gdrive','output_folder':'outputFolder'}
        self.store.record('settings','alice',settings)
        class Fail:
            def __init__(self,*args):pass
            def upload(self,*args):raise RuntimeError('offline')
        process(self.store,self.store.claim('upload'),'upload',drive_factory=Fail)
        failed=self.store.get(job['id'])
        self.assertEqual(failed['stage'],'Waiting for Drive')
        output=self.root/'outputs'/job['id']/failed['local_name']
        before=digest(output)
        self.assertFalse(cleanup(self.store,failed))
        self.client.post('/song-maker/api/jobs/'+job['id']+'/retry',headers=self.headers)
        class Good:
            def __init__(self,*args):pass
            def upload(self,*args):return {'remote_id':'verified-id','verified_at':time.time(),'drive_status':'Verified'}
        process(self.store,self.store.claim('upload'),'upload',drive_factory=Good)
        complete=self.store.get(job['id'])
        self.assertEqual(complete['stage'],'Completed')
        self.assertEqual(digest(output),before)
        self.assertTrue((self.root/'inputs'/job['id']).exists()) # manual original protected
        self.assertFalse((self.root/'temp'/job['id']).exists())
        self.assertTrue(output.exists())

    def test_drive_upload_collision_reservation_restart_and_checksum(self):
        job=self.submit()
        process(self.store,self.store.claim('render'),'render')
        self.store.update(job['id'],destination={'remote':'gdrive','output_folder':'outputFolder'})
        current=self.store.get(job['id'])
        class Fake(Drive):
            calls=[]
            def listing(_,folder):
                result=[{'Name':current['local_name'],'Size':5,'ID':'old','Hashes':{'MD5':'other'}}]
                if _.calls:
                    result.append({'Name':_.calls[-1],'Size':current['size'],'ID':'new','Hashes':{'MD5':current['md5']}})
                return result
            def stat(_, folder, name):
                self.assertEqual(folder, 'outputFolder')
                return {'Name':name, 'Size':current['size'], 'ID':'new',
                        'Hashes':{'MD5':current['md5']}}
            def call(_,args,*a,**k):
                self.assertIn('--ignore-existing',args)
                _.calls.append(args[2].split(':',1)[1])
                return ''
        drive=Fake(current['destination'])
        first=drive.upload(current,self.store)
        self.assertIn('(2).mp4',first['remote_name'])
        second=drive.upload(self.store.get(job['id']),self.store)
        self.assertEqual(first['remote_id'],second['remote_id'])
        self.assertEqual(drive.calls[0],drive.calls[1])

    def test_upload_verifier_reads_only_exact_output_metadata(self):
        from songmaker.drive import DEFAULT_OUTPUT, default_settings
        metadata = {'Name':'Song.mp4', 'ID':'drive-identity', 'Size':120,
                    'Hashes':{'MD5':'abc'}}
        with patch('songmaker.drive.run', return_value=json.dumps(metadata)) as execute:
            actual = Drive(default_settings()).stat(DEFAULT_OUTPUT, 'Song.mp4')
            self.assertEqual(actual, metadata)
            command = execute.call_args.args[0]
            self.assertIn('lsjson', command)
            self.assertIn('--stat', command)
            self.assertIn('--hash', command)
            self.assertIn('gdrive:Song Studio/song maker/Song.mp4', command)
            self.assertNotIn('--drive-root-folder-id', command)
        with self.assertRaises(ValueError):
            Drive(default_settings()).stat(DEFAULT_OUTPUT, '../escape.mp4')

    def test_upload_preflight_runs_before_multipart_spooling(self):
        with patch('songmaker.app.capacity', side_effect=RuntimeError('disk reserve unavailable')):
            response = self.client.post('/song-maker/api/jobs', headers=self.headers,
                files={'image': ('x.png', b'input'), 'audio': ('x.wav', b'input')})
        self.assertEqual(response.status_code, 507)
        self.assertEqual(self.store.jobs('alice'), [])

    def test_disk_preflight_and_invalid_media(self):
        with patch('songmaker.worker.shutil.disk_usage',return_value=type('Disk',(),{'free':4_999_999_999})()):
            from songmaker.worker import capacity
            with self.assertRaises(RuntimeError):capacity(self.root,1)
        result=self.client.post('/song-maker/api/jobs',headers=self.headers,
             files={'image':('x.jpg',b'bad'),'audio':('x.wav',b'bad')})
        self.assertEqual(result.status_code,422)
        self.assertEqual(self.store.jobs('alice'),[])


    def test_sso_user_can_request_only_approved_default_drive_sync(self):
        from songmaker.drive import default_settings
        user={'X-Auth-User':'bob','X-Song-Maker':'1'}
        initial=self.client.get('/song-maker/api/state',headers=user)
        self.assertEqual(initial.status_code,200)
        self.assertTrue(initial.json()['sync_access'])
        self.assertFalse(initial.json()['admin'])
        result=self.client.post('/song-maker/api/sync',headers=user)
        self.assertEqual(result.status_code,200,result.text)
        self.assertTrue(self.store.record('settings','bob')['sync_requested'])
        self.assertEqual(self.store.record('settings','bob')['image_folder'],default_settings()['image_folder'])
        self.assertEqual(self.store.record('settings','bob')['output_folder'],default_settings()['output_folder'])
        self.assertEqual(self.client.post('/song-maker/api/settings',headers=user,
            json={**default_settings(),'image_folder':'unexpectedRemoteFolder'}).status_code,403)
        self.assertEqual(self.client.get('/song-maker/api/state',headers=user).json()['jobs'],[])
        self.assertEqual(self.client.post('/song-maker/api/sync').status_code,401)

    def test_export_preset_is_private_and_applies_to_manual_and_drive_jobs(self):
        config={'resolution':'9:16','fit':'blur'}
        saved=self.client.post('/song-maker/api/export',headers=self.headers,json=config)
        self.assertEqual(saved.status_code,200,saved.text)
        state=self.client.get('/song-maker/api/state',headers=self.headers).json()
        self.assertEqual(state['export'],config)
        self.assertEqual(self.client.post('/song-maker/api/export',headers=self.headers,
            json={'resolution':'invalid','fit':'fit'}).status_code,422)
        image,audio=self.media()
        result=self.client.post('/song-maker/api/jobs',headers=self.headers,
            files={'image':('image.png',image.read_bytes(),'image/png'),
                   'audio':('Track.wav',audio.read_bytes(),'audio/wav')})
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['resolution'],'9:16')
        self.assertEqual(result.json()['fit'],'blur')
        from songmaker.drive import default_settings
        class DriveFixture:
            def listing(_,folder):
                return [self.item('New.jpg','image-1'),self.item('New.mp3','audio-1')]
        sync_owner(self.store,'alice',default_settings(),DriveFixture())
        new=next(j for j in self.store.jobs('alice') if j.get('origin')=='drive')
        self.assertEqual(new['resolution'],'9:16')
        self.assertEqual(new['fit'],'blur')
        guest={'X-Auth-User':'bob','X-Song-Maker':'1'}
        self.assertNotEqual(self.client.get('/song-maker/api/state',headers=guest).json()['export'],config)


if __name__=='__main__':unittest.main()

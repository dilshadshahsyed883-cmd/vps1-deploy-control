"""Song Maker two explicit handoff preferences: private input and no-Drive output."""
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
import wave
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'apps/song-maker/part01'))
_MODULE_STATE = tempfile.TemporaryDirectory()
os.environ['SONG_MAKER_DATA'] = _MODULE_STATE.name

try:
    from fastapi.testclient import TestClient
    from PIL import Image
    from songmaker.app import create_app
    from songmaker.store import Store
    from songmaker.worker import process
    from songmaker.media import digest
except ImportError as error:
    if os.environ.get('SONG_MAKER_REQUIRE_DEPS') == '1':
        raise
    raise unittest.SkipTest('Song Maker optional media test dependencies missing') from error


class SongMakerHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))
        self.client = TestClient(create_app(self.store))
        self.headers = {'X-Auth-User': 'alice', 'X-Is-Admin': '1', 'X-Song-Maker': '1'}
        self.defaults = {'resolution': '720p', 'fit': 'fit', 'quality': 'balanced'}

    def image(self):
        stream = io.BytesIO()
        Image.new('RGB', (300, 180), (50, 80, 120)).save(stream, 'PNG')
        return stream.getvalue()

    def audio(self):
        stream = io.BytesIO()
        with wave.open(stream, 'wb') as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(8000)
            output.writeframes(b'\\0\\0' * 8000)
        return stream.getvalue()

    def submit(self, handoff=False, owner='alice'):
        return self.client.post('/song-maker/api/jobs', headers={
            **self.headers, 'X-Auth-User': owner,
            **({'X-Audio-Downloader-Handoff': '1'} if handoff else {})
        }, files={'image': ('video_id.png', self.image(), 'image/png'),
                  'audio': ('video_id.wav', self.audio(), 'audio/wav')})

    def test_opt_in_input_gate_is_owner_scoped(self):
        self.assertEqual(self.submit(handoff=True).status_code, 409)
        flags = {**self.defaults, 'auto_input_from_audio_downloader': True}
        self.assertEqual(self.client.post('/song-maker/api/export', headers=self.headers, json=flags).status_code, 200)
        response = self.submit(handoff=True)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()['send_to_song_uploader'])
        self.assertEqual(self.submit(handoff=True, owner='bob').status_code, 409)
        self.assertEqual(self.client.post('/song-maker/api/export', headers=self.headers,
            json={**flags, 'send_to_song_uploader': 'yes'}).status_code, 422)

    def test_render_retains_local_mp4_thumbnail_and_does_not_touch_drive(self):
        settings = {**self.defaults, 'send_to_song_uploader': True}
        response = self.client.post('/song-maker/api/export', headers=self.headers, json=settings)
        self.assertEqual(response.status_code, 200, response.text)
        response = self.submit()
        self.assertEqual(response.status_code, 200, response.text)
        job = self.store.get(response.json()['id'])
        self.assertTrue(job['send_to_song_uploader'])
        output = self.store.root / 'outputs' / job['id'] / job['local_name']

        def fake_render(_image, _audio, output_path, *_a, **_k):
            output_path.write_bytes(b'validated MP4 stand-in')

        def fake_validate(path, _duration, _cancelled):
            return {'size': path.stat().st_size, 'md5': digest(path, 'md5')}

        def forbidden_drive(*_a, **_kw):
            raise AssertionError('No Drive call permitted on local output path')

        with patch('songmaker.worker.render', side_effect=fake_render), \\
             patch('songmaker.worker.validate', side_effect=fake_validate):
            process(self.store, job, 'render', drive_factory=forbidden_drive)

        finished = self.store.get(job['id'])
        self.assertEqual(finished['stage'], 'Completed', finished.get('error'))
        self.assertEqual(finished['uploader_status'], 'Available')
        self.assertEqual(finished['progress'], 100)
        self.assertIsNone(self.store.claim('upload'))
        self.assertTrue(output.exists())
        thumbnail = output.parent / (output.stem + '.jpg')
        self.assertTrue(thumbnail.exists())
        with Image.open(thumbnail) as check:
            self.assertEqual(check.size, (1280, 720))
        self.assertTrue((self.store.root / 'inputs' / job['id']).is_dir())
        rows = self.client.get('/song-maker/api/uploader/outputs', headers=self.headers).json()['outputs']
        self.assertEqual([row['id'] for row in rows], [job['id']])
        self.assertEqual(self.client.get(rows[0]['thumbnail_url'], headers=self.headers).status_code, 200)
        bob = {**self.headers, 'X-Auth-User': 'bob'}
        self.assertEqual(self.client.get('/song-maker/api/uploader/outputs', headers=bob).json()['outputs'], [])
        self.assertEqual(self.client.get(rows[0]['thumbnail_url'], headers=bob).status_code, 404)

    def test_drive_mode_is_default_and_job_setting_is_snapshotted(self):
        response = self.submit()
        self.assertEqual(response.status_code, 200, response.text)
        job_id = response.json()['id']
        self.assertFalse(self.store.get(job_id).get('send_to_song_uploader', False))
        export = self.client.get('/song-maker/api/state', headers=self.headers).json()['export']
        self.assertIs(export['send_to_song_uploader'], False)
        self.assertIs(export['auto_input_from_audio_downloader'], False)
        self.client.post('/song-maker/api/export', headers=self.headers,
                         json={**self.defaults, 'send_to_song_uploader': True})
        self.assertFalse(self.store.get(job_id).get('send_to_song_uploader', False))


if __name__ == '__main__':
    unittest.main()

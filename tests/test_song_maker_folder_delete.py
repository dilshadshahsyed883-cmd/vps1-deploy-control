"""Completed Song Maker deletion must not block on folder consumers or break uploads."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'apps/song-maker/part01'))
try:
    from fastapi.testclient import TestClient
    from songmaker.app import create_app
    from songmaker.folders import Folders
    from songmaker.store import Store
except ImportError as exc:
    if os.getenv('SONG_MAKER_REQUIRE_DEPS') == '1':
        raise
    raise unittest.SkipTest('Song Maker API dependencies unavailable') from exc


class DeleteFolderProjectTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.store = Store(Path(scratch.name) / 'maker')
        self.registry = Folders(self.store)
        self.client = TestClient(create_app(self.store), client=('127.0.0.1', 50000))
        self.addCleanup(self.client.close)
        self.headers = {'X-Auth-User': 'alice', 'X-Song-Maker': '1'}
        self.folder = self.registry.create('alice', 'Music')
        self.job = self.store.enqueue('alice', {'local': 'song1'}, {
            'project': 'song1', 'local_name': 'song1.mp4',
            'song_folder_id': self.folder['id'], 'send_to_song_uploader': True,
            'valid': True})
        self.store.update(self.job['id'], stage='Completed', valid=True)
        output = self.store.root / 'outputs' / self.job['id']
        output.mkdir(parents=True)
        self.video = output / 'song1.mp4'
        self.thumbnail = output / 'song1.jpg'
        self.video.write_bytes(b'validated-test-video')
        self.thumbnail.write_bytes(b'original-test-thumbnail')
        inputs = self.store.root / 'inputs' / self.job['id']
        inputs.mkdir(parents=True)
        (inputs / 'song1.mp3').write_bytes(b'original-audio')
        self.registry.publish(self.store.get(self.job['id']), self.video, self.thumbnail)
        self.media_path = self.registry.media_path('alice', self.folder['id'], self.job['id'])
        self.endpoint = '/song-maker/api/jobs/' + self.job['id']

    def row(self):
        with self.store.db() as db:
            return dict(db.execute('SELECT closed,consumed FROM song_folder_media WHERE job_id=?',
                                   (self.job['id'],)).fetchone())

    def delete(self, headers=None):
        return self.client.post(self.endpoint + '/delete', headers=headers or self.headers)

    def test_completed_unreserved_project_deletes_immediately(self):
        self.assertEqual(len(self.registry.outputs('alice', self.folder['id'])), 1)
        response = self.delete()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {'ok': True, 'media_retained': False})
        self.assertEqual(self.store.jobs('alice'), [])
        self.assertEqual(self.row(), {'closed': 1, 'consumed': 1})
        self.assertFalse(self.media_path.exists())
        self.assertFalse(self.video.exists())
        self.assertFalse((self.store.root / 'inputs' / self.job['id']).exists())
        self.assertEqual(self.client.get(self.endpoint + '/video', headers=self.headers).status_code, 404)
        self.assertEqual(self.delete().status_code, 404)
        self.assertEqual(self.store.enqueue('alice', {'local': 'song1'}, {})['id'], self.job['id'])

    def test_reserved_project_disappears_but_originals_remain_until_receipt(self):
        consumer = 'song-uploader:profile:1'
        self.registry.reserve('alice', self.folder['id'], self.job['id'], consumer)
        response = self.delete()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['media_retained'])
        self.assertEqual(self.store.jobs('alice'), [])
        self.assertEqual(self.row(), {'closed': 1, 'consumed': 0})
        self.assertEqual(self.registry.outputs('alice', self.folder['id']), [])
        self.assertTrue(self.media_path.is_dir())
        self.assertTrue(self.video.is_file())
        self.assertEqual(self.client.get(self.endpoint + '/video', headers=self.headers).content,
                         b'validated-test-video')
        self.assertEqual(self.client.get('/song-maker/api/uploader/' + self.job['id'] + '/thumbnail',
                                         headers=self.headers).content, b'original-test-thumbnail')
        self.assertEqual(self.client.get(self.endpoint + '/video',
                                         headers={'X-Auth-User': 'bob'}).status_code, 404)
        with self.assertRaisesRegex(ValueError, 'consumed or closing'):
            self.registry.reserve('alice', self.folder['id'], self.job['id'],
                                  'song-uploader:profile:2')
        receipt = self.registry.finish('alice', self.folder['id'], self.job['id'],
                                       consumer, 'w' * 11)
        self.assertTrue(receipt['cleaned'])
        self.assertEqual(self.row(), {'closed': 1, 'consumed': 1})
        self.assertFalse(self.media_path.exists())
        self.assertFalse(self.video.exists())
        self.assertEqual(self.client.get(self.endpoint + '/video',
                                         headers=self.headers).status_code, 404)

    def test_two_reserved_uploads_keep_originals_until_both_finish(self):
        first = 'song-uploader:profile:1'
        second = 'song-uploader:profile:2'
        self.registry.reserve('alice', self.folder['id'], self.job['id'], first)
        self.registry.reserve('alice', self.folder['id'], self.job['id'], second)
        self.assertEqual(self.delete().status_code, 200)
        intermediate = self.registry.finish('alice', self.folder['id'], self.job['id'],
                                            first, 'a' * 11)
        self.assertFalse(intermediate['cleaned'])
        self.assertTrue(intermediate['pending_dependents'])
        self.assertTrue(self.video.is_file())
        last = self.registry.finish('alice', self.folder['id'], self.job['id'],
                                    second, 'b' * 11)
        self.assertTrue(last['cleaned'])
        self.assertFalse(self.video.exists())

    def test_active_or_foreign_project_still_cannot_be_deleted(self):
        self.assertEqual(self.delete({'X-Auth-User': 'bob', 'X-Song-Maker': '1'}).status_code, 404)
        self.store.update(self.job['id'], stage='Rendering', lane='render')
        self.assertEqual(self.delete().status_code, 409)
        self.assertFalse(self.store.get(self.job['id'])['deleted'])
        self.assertEqual(self.row(), {'closed': 0, 'consumed': 0})
        self.assertTrue(self.video.is_file())


if __name__ == '__main__':
    unittest.main()

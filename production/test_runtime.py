import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from production import runtime


class RuntimeTests(unittest.TestCase):
    def test_edge_tts_uses_current_supported_websocket_handshake(self):
        voice = runtime.CATALOG['voice-studio']
        self.assertIn('edge-tts==7.3.1', voice['packages'])
        self.assertNotIn('edge-tts==7.2.8', voice['packages'])

    def test_sqlite_backup_includes_wal_and_preserves_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / 'state'
            state.mkdir()
            database = state / 'queue.sqlite3'
            with sqlite3.connect(database) as conn:
                conn.execute('PRAGMA journal_mode=WAL')
                conn.execute('CREATE TABLE jobs(id INTEGER PRIMARY KEY, status TEXT)')
                conn.execute("INSERT INTO jobs(status) VALUES ('waiting')")
                conn.commit()
                files = runtime.backup_databases(state, root / 'backup')
                with sqlite3.connect(root / 'backup' / files[0]) as backup:
                    self.assertEqual(backup.execute('SELECT status FROM jobs').fetchone(), ('waiting',))
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 1)

    def test_uploader_release_idle_preflight_validates_all_three_routes(self):
        class Response:
            status = 200
            def __init__(self, mode):
                self.mode = mode
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self, _count):
                return json.dumps({
                    'ok': True, 'engine_instance': self.mode, 'active_project_id': None
                }).encode()
        class Opener:
            calls = []
            def open(self, url, timeout):
                self.calls.append(url)
                mode = ('engine2' if '/engine2/' in url else
                        'shorts' if '/shorts/' in url else 'engine1')
                return Response(mode)
        opener = Opener()
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            runtime.urllib.request, 'build_opener', return_value=opener
        ):
            runtime.require_lum_release_idle(Path(tmp))
        self.assertEqual(opener.calls, [
            'http://127.0.0.1:8015/local-upload-manager/health',
            'http://127.0.0.1:8016/local-upload-manager/modes/engine2/health',
            'http://127.0.0.1:8017/local-upload-manager/modes/shorts/health',
        ])

    def test_uploader_release_blocks_busy_sqlite_without_queue_mutation(self):
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self, _count):
                return b'{"ok":true,"engine_instance":"engine1","active_project_id":null}'
        class Opener:
            def open(self, url, timeout): return Response()
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / 'lum1/core/data/uploader.db'
            database.parent.mkdir(parents=True)
            with sqlite3.connect(database) as conn:
                conn.execute('CREATE TABLE videos(id INTEGER PRIMARY KEY, status TEXT)')
                conn.execute("INSERT INTO videos(status) VALUES ('UPLOADING')")
            with patch.object(runtime.urllib.request, 'build_opener', return_value=Opener()):
                with self.assertRaisesRegex(RuntimeError, 'LUM_RELEASE_BLOCKED'):
                    runtime.require_lum_release_idle(Path(tmp))
            with sqlite3.connect(database) as conn:
                self.assertEqual(conn.execute('SELECT status FROM videos').fetchone(),
                                 ('UPLOADING',))

    def test_uploader_release_fails_closed_on_health_unavailable(self):
        class Opener:
            def open(self, url, timeout): raise OSError('host unavailable')
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            runtime.urllib.request, 'build_opener', return_value=Opener()
        ):
            with self.assertRaisesRegex(RuntimeError, 'LUM_RELEASE_BLOCKED'):
                runtime.require_lum_release_idle(Path(tmp))

    def test_song_uploader_active_work_blocks_shared_release(self):
        class Response:
            status = 200
            def __init__(self, mode): self.mode = mode
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self, _):
                return json.dumps({"ok": True, "engine_instance": self.mode,
                                   "active_project_id": "song-in-flight" if self.mode == "song" else None}).encode()
        class Opener:
            def open(self, url, timeout):
                mode = url.split("/modes/")[1].split("/")[0] if "/modes/" in url else "engine1"
                return Response(mode)
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "lum-song/core/data/uploader.db"
            database.parent.mkdir(parents=True)
            with sqlite3.connect(database) as conn:
                conn.execute("CREATE TABLE preserved(value TEXT)")
                conn.execute("INSERT INTO preserved VALUES ('keep')")
            with patch.object(runtime.urllib.request, "build_opener", return_value=Opener()):
                with self.assertRaisesRegex(RuntimeError, "uploader song"):
                    runtime.require_lum_release_idle(Path(tmp))
            with sqlite3.connect(database) as conn:
                self.assertEqual(conn.execute("SELECT value FROM preserved").fetchone(), ("keep",))

    def test_source_drift_rejected_without_overwriting_release(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(runtime, 'BASE', Path(tmp)):
            with patch('rebuild.fresh_vps.sources', return_value=[('part01/app.py', b'original')]):
                target = runtime.stage_source('short-maker', 'a' * 40)
                (target / 'part01/app.py').write_bytes(b'changed')
                with self.assertRaisesRegex(RuntimeError, 'source drift'):
                    runtime.stage_source('short-maker', 'a' * 40)
                self.assertEqual((target / 'part01/app.py').read_bytes(), b'changed')

    def test_pointer_swap_restores_previous_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            previous = root / 'previous'
            candidate = root / 'candidate'
            previous.mkdir()
            candidate.mkdir()
            current = root / 'current'
            runtime.atomic_link(current, previous)
            runtime.atomic_link(current, candidate)
            runtime.atomic_link(current, previous)
            self.assertEqual(current.resolve(), previous)
            self.assertTrue(candidate.is_dir())


if __name__ == '__main__':
    unittest.main()

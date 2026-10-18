import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from production import runtime


class RuntimeTests(unittest.TestCase):
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

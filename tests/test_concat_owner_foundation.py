"""Agent 5: additive owner migration and cross-owner negative fixtures.

This suite uses only temporary SQLite databases; no VPS or Drive is touched.
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/scene-builder/part01"))
from cloud_render_owner import (
    get_owner_config, migrate_files, migrate_production, owner_name,
    put_owner_config, queue_state, set_queue_state,
)


class ConcatOwnerMigration(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.execute("""CREATE TABLE files(
            id TEXT PRIMARY KEY, name TEXT NOT NULL, size INTEGER NOT NULL,
            received INTEGER NOT NULL DEFAULT 0, chunks INTEGER NOT NULL DEFAULT 0,
            sha256 TEXT NOT NULL, md5 TEXT NOT NULL DEFAULT '', state TEXT NOT NULL,
            drive_path TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        self.con.execute("INSERT INTO files(id,name,size,sha256,state) VALUES('old','old.mp4',100,?,'ready')",
                         ("a" * 64,))
        self.con.execute("""CREATE TABLE production_jobs(
            id INTEGER PRIMARY KEY AUTOINCREMENT, remote_id TEXT NOT NULL UNIQUE,
            drive_id TEXT NOT NULL UNIQUE, name TEXT NOT NULL, resource_keys TEXT NOT NULL,
            drive_size INTEGER NOT NULL, drive_md5 TEXT NOT NULL,
            folder_id TEXT NOT NULL, drive_path TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'Queued', error TEXT NOT NULL DEFAULT '',
            digest TEXT NOT NULL DEFAULT '', endpoint TEXT NOT NULL DEFAULT '',
            submitted_at INTEGER NOT NULL DEFAULT 0, cancel_requested INTEGER NOT NULL DEFAULT 0,
            rerender INTEGER NOT NULL DEFAULT 0, output_sha TEXT NOT NULL DEFAULT '',
            output_size INTEGER NOT NULL DEFAULT 0
        )""")
        self.con.execute("""INSERT INTO production_jobs(remote_id,drive_id,name,resource_keys,
                         drive_size,drive_md5,folder_id,drive_path)
                         VALUES('render-old','drive-a','old.zip','','99','','folder','gdrive:folder/old.zip')""")

    def tearDown(self):
        self.con.close()

    def test_ownerless_legacy_records_quarantined_and_ids_preserved(self):
        migrate_files(self.con)
        migrate_production(self.con)
        self.assertEqual(self.con.execute("SELECT owner_username FROM files WHERE id='old'").fetchone()[0], "")
        old = self.con.execute("SELECT id,remote_id,drive_path,owner_username FROM production_jobs").fetchone()
        self.assertEqual((old["id"], old["remote_id"], old["owner_username"]), (1, "render-old", ""))
        for user in ("alice", "bob", "admin"):
            self.assertEqual(self.con.execute("SELECT COUNT(*) FROM files WHERE owner_username=?", (user,)).fetchone()[0], 0)
            self.assertEqual(self.con.execute("SELECT COUNT(*) FROM production_jobs WHERE owner_username=?", (user,)).fetchone()[0], 0)

    def test_idempotent_migration_and_owner_drive_identity(self):
        migrate_files(self.con)
        migrate_production(self.con)
        migrate_files(self.con)
        migrate_production(self.con)
        self.con.execute("UPDATE production_jobs SET owner_username='alice' WHERE id=1")
        self.con.execute("""INSERT INTO production_jobs(remote_id,drive_id,name,resource_keys,
                         drive_size,drive_md5,folder_id,drive_path,owner_username)
                         VALUES('render-bob','drive-a','second.zip','','77','','folder',
                                'gdrive:folder/second.zip','bob')""")
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM production_jobs WHERE drive_id='drive-a'").fetchone()[0], 2)
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute("""INSERT INTO production_jobs(remote_id,drive_id,name,resource_keys,
                             drive_size,drive_md5,folder_id,drive_path,owner_username)
                             VALUES('render-alice-dup','drive-a','third.zip','','88','','folder',
                                    'gdrive:folder/third.zip','alice')""")

    def test_empty_schema_migration_preserves_existing_indexes(self):
        self.con.execute("DELETE FROM files")
        self.con.execute("DELETE FROM production_jobs")
        self.con.execute("CREATE INDEX production_custom_name ON production_jobs(name)")
        migrate_files(self.con)
        migrate_production(self.con)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM files").fetchone()[0], 0)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM production_jobs").fetchone()[0], 0)
        self.assertIsNotNone(self.con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='production_custom_name'").fetchone())
        migrate_files(self.con)
        migrate_production(self.con)

    def test_owners_have_independent_paused_queues_and_secret_config(self):
        migrate_production(self.con)
        self.assertEqual(queue_state(self.con, "alice"), "stopped")
        self.assertEqual(queue_state(self.con, "admin"), "stopped")
        set_queue_state(self.con, "alice", "running")
        self.assertEqual(queue_state(self.con, "alice"), "running")
        self.assertEqual(queue_state(self.con, "bob"), "stopped")
        self.assertEqual(queue_state(self.con, "admin"), "stopped")
        put_owner_config(self.con, "alice", "worker_key", "only-alice")
        put_owner_config(self.con, "bob", "worker_key", "only-bob")
        self.assertEqual(get_owner_config(self.con, "alice", "worker_key"), "only-alice")
        self.assertEqual(get_owner_config(self.con, "bob", "worker_key"), "only-bob")
        self.assertEqual(get_owner_config(self.con, "admin", "worker_key"), "")

    def test_invalid_spoofed_owners_denied(self):
        for owner in ("", "../alice", "bob/../../", "a b", "\\evil", "ad\nmin"):
            with self.subTest(owner=owner), self.assertRaises(ValueError):
                owner_name(owner)
        self.assertEqual(owner_name("ALICE"), "alice")


if __name__ == "__main__":
    unittest.main()

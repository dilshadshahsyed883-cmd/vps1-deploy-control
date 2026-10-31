"""Offline proof: the production tenant preflight never edits SQLite."""
from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from production.studio_tenant_readonly_preflight import (
    INSTANCE_RELATIVE, audit_all, audit_database,
)


def make_db(path, active=False, mismatch=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as con:
        con.executescript("""
            CREATE TABLE channels(id INTEGER PRIMARY KEY);
            CREATE TABLE channel_groups(id INTEGER PRIMARY KEY);
            CREATE TABLE channel_group_members(group_id INTEGER,channel_db_id INTEGER);
            CREATE TABLE sheet_sources(id INTEGER PRIMARY KEY);
            CREATE TABLE auto_engine_profiles(id INTEGER PRIMARY KEY);
            CREATE TABLE videos(id INTEGER PRIMARY KEY,status TEXT);
            CREATE TABLE studio_entity_owners(
                entity_kind TEXT,entity_id INTEGER,owner_username TEXT);
            INSERT INTO channels(id) VALUES (1),(2);
            INSERT INTO channel_groups(id) VALUES (1);
            INSERT INTO channel_group_members VALUES(1,1);
            INSERT INTO sheet_sources(id) VALUES (1);
            INSERT INTO auto_engine_profiles(id) VALUES (1);
            INSERT INTO studio_entity_owners VALUES
              ('channel',1,'alice'),('group',1,'alice'),('sheet',1,'alice'),
              ('profile',1,'alice');
            INSERT INTO videos(id,status) VALUES(1,'UPLOADED');
        """)
        if active:
            con.execute("INSERT INTO videos(id,status) VALUES (2,'UPLOADING')")
        if mismatch:
            con.execute("UPDATE studio_entity_owners SET owner_username='bob' "
                        "WHERE entity_kind='channel' AND entity_id=1")


class TenantPreflightTests(unittest.TestCase):
    def test_readonly_counts_no_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"upload.sqlite"
            make_db(path)
            before=hashlib.sha256(path.read_bytes()).hexdigest()
            data=audit_database(path)
            self.assertEqual(data["schema"],"PASS")
            self.assertEqual(data["busy"],0)
            self.assertEqual(data["unassigned"]["channel"],1)
            self.assertTrue(data["legacy_requires_explicit_admin_assignment"])
            self.assertTrue(data["ready"])
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),before)

    def test_active_or_cross_owner_group_blocks_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"upload.sqlite"
            make_db(path,active=True,mismatch=True)
            findings=audit_database(path)
            self.assertFalse(findings["ready"])
            self.assertEqual(findings["busy"],1)
            self.assertEqual(findings["owned_group_mismatch"],1)

    def test_all_three_instances_checked_and_missing_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for relative in INSTANCE_RELATIVE.values():
                make_db(root/relative)
            result=audit_all(root)
            self.assertEqual(result["preflight"],"PASS")
            self.assertEqual(len(result["instances"]),3)
            (root/INSTANCE_RELATIVE["short"]).unlink()
            self.assertEqual(audit_all(root)["preflight"],"BLOCKED")


if __name__=="__main__":
    unittest.main()

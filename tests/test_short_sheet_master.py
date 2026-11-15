"""Offline contracts for owner-scoped Google Sheet -> Short Uploader configuration.

No live Google requests, OAuth files, uploads, or queue writes.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
PART = ROOT / "apps/local-upload-manager/part01"
sys.path.insert(0, str(PART))
from desktop_core.modules.google_sheet_sync import rows_to_sheet_rows, parse_proxy_line


class Store:
    def __init__(self, db_path):
        self.path = db_path

    def connect(self):
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        return con

    def get_sheet_source(self, source_id):
        with self.connect() as con:
            row = con.execute("SELECT id FROM sheet_sources WHERE id=?", (int(source_id),)).fetchone()
            return {"id": int(row[0]), "sheet_url_or_id": "offline-fixture"} if row else None


def tenant(store):
    spec = importlib.util.spec_from_file_location("short_master_tenants", PART / "lum_engine/studio_tenants.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.StudioTenants(store)


class ShortSheetMasterContracts(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(str(Path(tmp.name) / "channels.sqlite3"))
        with self.store.connect() as c:
            c.executescript("""
              CREATE TABLE sheet_sources(
                id INTEGER PRIMARY KEY,sheet_name TEXT,enabled INTEGER,
                last_sync_at TEXT,last_sync_status TEXT,updated_at TEXT);
              CREATE TABLE channels(
                id INTEGER PRIMARY KEY AUTOINCREMENT,channel_id TEXT NOT NULL UNIQUE,
                channel_name TEXT NOT NULL,google_account_email TEXT,
                token_file_path TEXT NOT NULL DEFAULT '',channel_url TEXT,
                status TEXT NOT NULL DEFAULT 'ACTIVE',browser_profile_name TEXT,
                proxy_scheme TEXT,proxy_host TEXT,proxy_port TEXT,
                proxy_username TEXT,proxy_password TEXT,
                sheet_source_id INTEGER,sheet_row_key TEXT,sheet_present INTEGER,
                sheet_notes TEXT,sheet_last_seen_at TEXT,last_used_at TEXT,
                sheet_sr TEXT,sheet_ip_raw TEXT,sheet_status TEXT);
              CREATE TABLE channel_groups(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_name TEXT NOT NULL UNIQUE,notes TEXT);
              CREATE TABLE channel_group_members(
                group_id INTEGER,channel_db_id INTEGER,
                PRIMARY KEY(group_id,channel_db_id));
              CREATE TABLE videos(id INTEGER PRIMARY KEY,channel_db_id INTEGER,status TEXT);
              INSERT INTO sheet_sources VALUES (1,'Haseeb 2',1,NULL,NULL,NULL);
              INSERT INTO sheet_sources VALUES (2,'Other',1,NULL,NULL,NULL);
              INSERT INTO channels(channel_id,channel_name,google_account_email,
                  token_file_path,channel_url,status,sheet_source_id,sheet_row_key,sheet_present,
                  browser_profile_name,proxy_scheme,proxy_host,proxy_port,proxy_username,proxy_password)
                VALUES ('UCaaaaaaaaaaaaaaaaaaaaaa','Gojra Old','old@example.com',
                  '/keep/oauth.json','https://youtube.com/channel/UCaaaaaaaaaaaaaaaaaaaaaa',
                  'ACTIVE',1,'sr:19',1,'Old Browser','http','dc.decododo.com','10022','old-user','old-password');
              INSERT INTO channels(channel_id,channel_name,status,sheet_source_id,sheet_row_key,sheet_present)
                VALUES ('UCbbbbbbbbbbbbbbbbbbbbbb','Foreign','ACTIVE',2,'sr:1',1);
              INSERT INTO videos VALUES (7,1,'QUEUED');
            """)
        self.tenant = tenant(self.store)
        self.tenant.bind("sheet", 1, "alice")
        self.tenant.bind("sheet", 2, "bob")
        self.tenant.bind("channel", 1, "alice")
        self.tenant.bind("channel", 2, "bob")

    def sheet_row(self, *, ip="dc.decododo.com:10010:new-user:new-password",
                  channel="UCaaaaaaaaaaaaaaaaaaaaaa", group="Hania Asif",
                  sr="19", browser="Sheet Profile", status="ACTIVE",
                  include_proxy=True, include_group=True):
        item = {"sr": sr, "channel_name": "Gojra Gojra",
                "gmail": "new@example.com", "channel_link":
                "https://youtube.com/channel/" + channel if channel.startswith("UC") else channel,
                "browser_profile": browser, "status": status}
        if include_proxy:
            item["ip"] = ip
        if include_group:
            item["group"] = group
        return rows_to_sheet_rows([item], "/tmp/short-sheet-master-no-token-fixture")[0]

    def data(self):
        with self.store.connect() as c:
            return dict(c.execute("SELECT * FROM channels WHERE id=1").fetchone())

    def test_sheet_overwrites_proxy_name_gmail_browser_and_group_but_keeps_oauth_and_queue(self):
        result = self.tenant.sync_owned_sheet_rows("alice", 1, [self.sheet_row()])
        self.assertEqual(result["rows"], 1)
        self.assertEqual(result["groups_touched"], 1)
        row = self.data()
        self.assertEqual(row["channel_name"], "Gojra Gojra")
        self.assertEqual(row["google_account_email"], "new@example.com")
        self.assertEqual(row["proxy_host"], "dc.decododo.com")
        self.assertEqual(row["proxy_port"], "10010")
        self.assertEqual(row["proxy_username"], "new-user")
        self.assertEqual(row["proxy_password"], "new-password")
        self.assertEqual(row["browser_profile_name"], "Sheet Profile")
        self.assertEqual(row["token_file_path"], "/keep/oauth.json")
        self.assertEqual(row["status"], "ACTIVE")
        self.assertEqual(row["sheet_sr"], "19")
        with self.store.connect() as c:
            self.assertEqual(tuple(c.execute("SELECT id,channel_db_id,status FROM videos").fetchone()),
                             (7, 1, "QUEUED"))
            groups = c.execute("""SELECT g.group_name FROM channel_groups g
                      JOIN channel_group_members m ON m.group_id=g.id
                      WHERE m.channel_db_id=1""").fetchall()
            self.assertEqual([row[0] for row in groups], ["Hania Asif"])
            self.assertEqual(c.execute("SELECT channel_name FROM channels WHERE id=2").fetchone()[0], "Foreign")

    def test_sheet_is_repeatedly_idempotent_and_updates_group_only_for_own_sheet_row(self):
        self.tenant.sync_owned_sheet_rows("alice", 1, [self.sheet_row()])
        self.tenant.sync_owned_sheet_rows("alice", 1, [self.sheet_row(group="Ronaldo", ip="dc.decododo.com:10013")])
        row = self.data()
        self.assertEqual(row["proxy_port"], "10013")
        with self.store.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM channels").fetchone()[0], 2)
            groups = c.execute("""SELECT g.group_name FROM channel_groups g
                      JOIN channel_group_members m ON m.group_id=g.id
                      WHERE m.channel_db_id=1""").fetchall()
            self.assertEqual([x[0] for x in groups], ["Ronaldo"])

    def test_explicit_blank_proxy_clears_it_but_absent_column_preserves(self):
        row = self.sheet_row(ip="", group="", include_group=False)
        self.tenant.sync_owned_sheet_rows("alice", 1, [row])
        self.assertEqual(self.data()["proxy_port"], "")
        with self.store.connect() as c:
            c.execute("UPDATE channels SET proxy_port='12000',proxy_host='manual.host' WHERE id=1")
        no_col = self.sheet_row(include_proxy=False, include_group=False)
        self.tenant.sync_owned_sheet_rows("alice", 1, [no_col])
        self.assertEqual(self.data()["proxy_port"], "12000")

    def test_missing_sheet_uc_preserves_verified_identity(self):
        row = self.sheet_row(channel="https://youtube.com/@gojra", include_group=False)
        self.tenant.sync_owned_sheet_rows("alice", 1, [row])
        self.assertEqual(self.data()["channel_id"], "UCaaaaaaaaaaaaaaaaaaaaaa")
        self.assertEqual(self.data()["token_file_path"], "/keep/oauth.json")

    def test_sheet_sr_change_with_same_uc_reuses_original_db_row(self):
        self.tenant.sync_owned_sheet_rows("alice", 1, [self.sheet_row(sr="88", include_group=False)])
        self.assertEqual(self.data()["sheet_row_key"], "sr:88")
        with self.store.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM channels").fetchone()[0], 2)

    def test_foreign_uc_or_group_collision_rolls_back_every_field(self):
        own = self.sheet_row(ip="dc.decododo.com:10015", group="", include_group=False)
        foreign = self.sheet_row(channel="UCbbbbbbbbbbbbbbbbbbbbbb", sr="20", include_group=False)
        with self.assertRaises(PermissionError):
            self.tenant.sync_owned_sheet_rows("alice", 1, [own, foreign])
        self.assertEqual(self.data()["proxy_port"], "10022")
        self.assertEqual(self.data()["channel_name"], "Gojra Old")
        with self.store.connect() as c:
            self.assertIsNone(c.execute("SELECT last_sync_at FROM sheet_sources WHERE id=1").fetchone()[0])

    def test_bad_proxy_still_fails_without_partial_writes(self):
        with self.assertRaises(ValueError):
            self.tenant.sync_owned_sheet_rows(
                "alice", 1, [self.sheet_row(ip="dc.decododo.com:not-a-port")])
        self.assertEqual(self.data()["proxy_port"], "10022")

    def test_nonstandard_sheet_status_is_recorded_without_blocking_other_fields(self):
        row = self.sheet_row(sr="7", status="Channel Warmup",
                             ip="dc.decododo.com:10010", include_group=False)
        result = self.tenant.sync_owned_sheet_rows("alice", 1, [row])
        self.assertEqual(result["status_unmapped"], 1)
        channel = self.data()
        self.assertEqual(channel["proxy_port"], "10010")
        self.assertEqual(channel["status"], "ACTIVE")  # old operational state not guessed
        self.assertEqual(channel["sheet_status"], "Channel Warmup")
        with self.store.connect() as conn:
            receipt = conn.execute("SELECT last_sync_status FROM sheet_sources WHERE id=1").fetchone()[0]
            self.assertIn("master=1", receipt)
            self.assertIn("status_unmapped=1", receipt)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0], 1)

    def test_nonstandard_status_on_new_row_is_not_automatically_activated(self):
        row = self.sheet_row(channel="UCcccccccccccccccccccccc", sr="7",
                             status="Review Later", include_group=False)
        result = self.tenant.sync_owned_sheet_rows("alice", 1, [row])
        self.assertEqual(result["status_unmapped"], 1)
        with self.store.connect() as conn:
            new = conn.execute("SELECT status,sheet_status,proxy_port FROM channels "
                               "WHERE sheet_row_key='sr:7'").fetchone()
            self.assertIsNotNone(new)
            self.assertEqual(tuple(new), ("INACTIVE", "Review Later", "10010"))
            self.assertEqual(conn.execute("SELECT status FROM videos WHERE id=7").fetchone()[0], "QUEUED")

    def test_recognized_sheet_status_remains_authoritative(self):
        for incoming,expected in (("inactive", "INACTIVE"), ("enabled", "ACTIVE"),
                                  ("paused", "DISABLED")):
            self.tenant.sync_owned_sheet_rows(
                "alice", 1, [self.sheet_row(status=incoming, include_group=False)])
            self.assertEqual(self.data()["status"], expected)

    def test_unassigned_legacy_group_cannot_be_claimed_from_sheet(self):
        with self.store.connect() as c:
            c.execute("INSERT INTO channel_groups(group_name) VALUES ('Hania Asif')")
        with self.assertRaisesRegex(PermissionError, "group name"):
            self.tenant.sync_owned_sheet_rows("alice", 1, [self.sheet_row()])
        self.assertEqual(self.data()["proxy_port"], "10022")

    def test_freshness_is_per_user_and_requires_new_master_receipt(self):
        from lum_engine.sheets_bridge import owned_sheet_fresh
        self.assertFalse(owned_sheet_fresh(self.store, "alice"))
        self.assertFalse(owned_sheet_fresh(self.store, "bob"))
        with self.store.connect() as c:
            c.execute("""UPDATE sheet_sources SET
                last_sync_at=CURRENT_TIMESTAMP,last_sync_status='OWNED_SYNC: rows=1'
                WHERE id=1""")
        self.assertFalse(owned_sheet_fresh(self.store, "alice"))
        self.tenant.sync_owned_sheet_rows("alice", 1, [self.sheet_row(include_group=False)])
        self.assertTrue(owned_sheet_fresh(self.store, "alice"))
        self.assertFalse(owned_sheet_fresh(self.store, "bob"))
        with self.store.connect() as c:
            c.execute("""UPDATE sheet_sources SET
                last_sync_at='2020-01-01 00:00:00' WHERE id=1""")
        self.assertFalse(owned_sheet_fresh(self.store, "alice"))

    def test_failed_fresh_sync_immediately_blocks_new_uploads_preserving_last_success(self):
        from lum_engine.sheets_bridge import owned_sheet_fresh, sync_owned_sheet_source
        self.tenant.sync_owned_sheet_rows("alice", 1, [self.sheet_row(include_group=False)])
        self.assertTrue(owned_sheet_fresh(self.store, "alice"))
        original = self.data()
        with self.assertRaisesRegex(ValueError, "zero rows"):
            sync_owned_sheet_source(
                self.store, self.tenant, "alice", 1,
                fetch_rows=lambda url, gid: [],
            )
        self.assertFalse(owned_sheet_fresh(self.store, "alice"))
        self.assertEqual(self.data()["proxy_port"], original["proxy_port"])
        self.assertEqual(self.data()["token_file_path"], original["token_file_path"])
        with self.store.connect() as con:
            row = con.execute("SELECT last_sync_at,last_sync_status FROM sheet_sources WHERE id=1").fetchone()
            self.assertTrue(row["last_sync_at"])
            self.assertTrue(row["last_sync_status"].startswith("SHEET_MASTER_FAILED:"))

    def test_scoped_scheduler_and_worker_only_use_fresh_owner_sheets(self):
        scheduler = (PART / "lum_engine/auto_engine_v6_scheduler.py").read_text()
        worker = (PART / "lum_engine/auto_engine_v6_worker.py").read_text()
        app = (PART / "lum_engine/standalone_app.py").read_text()
        self.assertIn("next_sheet_poll = time.monotonic() + 15 * 60", scheduler)
        self.assertIn("owned_sheet_fresh(db, owner)", scheduler)
        self.assertIn('sheet_tenants.owner("profile",pid)', worker)
        self.assertIn("owned_sheet_fresh(db,owner)", worker)
        self.assertIn("_short_sheet_sync_before_start(user)", app)
        self.assertIn("web_sync_owned_sheet_source(", app)

    def test_source_owner_cannot_be_impersonated(self):
        with self.assertRaises(PermissionError):
            self.tenant.sync_owned_sheet_rows("bob", 1, [self.sheet_row()])
        self.assertEqual(self.data()["proxy_port"], "10022")


if __name__ == "__main__":
    unittest.main()

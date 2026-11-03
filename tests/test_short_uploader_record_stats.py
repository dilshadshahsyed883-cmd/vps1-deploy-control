"""Short Uploader owner receipt counters: SQL pagination, PKT, and isolation.

Tests run against a disposable SQLite database; no application imports, VPS
services, accounts, Google Sheets, or production records are touched.
"""
from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "apps/local-upload-manager/part01/lum_engine/auto_engine_v6.py"
APP = ROOT / "apps/local-upload-manager/part01/lum_engine/standalone_app.py"


def load_owner_methods():
    tree = ast.parse(ENGINE.read_text(encoding="utf-8"))
    cls = next(item for item in tree.body if isinstance(item, ast.ClassDef) and item.name == "V6Store")
    methods = [item for item in cls.body if isinstance(item, ast.FunctionDef)
               and item.name in {"owner_records", "owner_record_stats"}]
    if len(methods) != 2:
        raise AssertionError("Owner records and stats methods are both required")
    isolated = ast.ClassDef(name="ReadOnlyStore", bases=[], keywords=[], body=methods, decorator_list=[])
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    module = ast.Module(body=[future, isolated], type_ignores=[])
    ast.fix_missing_locations(module)
    env = {"datetime": datetime, "timezone": timezone,
           "timedelta": timedelta, "ZoneInfo": ZoneInfo}
    exec(compile(module, str(ENGINE), "exec"), env)
    return env["ReadOnlyStore"]


ReadOnlyStore = load_owner_methods()


class OwnerStore(ReadOnlyStore):
    def __init__(self, path):
        self.path = path

    def connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection


class ShortUploaderOwnerRecordsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = OwnerStore(str(Path(self.temp.name) / "receipts.sqlite"))
        with self.store.connect() as con:
            con.execute("""CREATE TABLE upload_records (
                id INTEGER PRIMARY KEY, profile_id INTEGER, channel_db_id INTEGER,
                uploaded_at TEXT NOT NULL, youtube_video_id TEXT NOT NULL UNIQUE)""")
            con.execute("""CREATE TABLE script_history (
                id INTEGER PRIMARY KEY, channel_id TEXT, uploaded_at TEXT)""")

    def add(self, profile_id, channel_db_id, uploaded_at, id):
        with self.store.connect() as con:
            con.execute(
                "INSERT INTO upload_records(profile_id,channel_db_id,uploaded_at,youtube_video_id) "
                "VALUES(?,?,?,?)", (profile_id, channel_db_id, uploaded_at, str(id)))

    def test_records_are_scoped_before_limit_and_counts_are_not_capped(self):
        now = datetime(2026, 11, 3, 19, 30, tzinfo=timezone.utc)
        self.add(10, 101, "2026-11-03 19:10:00", "alice-today")
        self.add(10, 101, "2026-11-03 18:50:00", "alice-yesterday")
        self.add(10, 101, "2026-10-27 19:15:00", "alice-30days")
        self.add(10, 101, "2020-03-04 00:00:00", "alice-older")
        self.add(10, 202, "2026-11-03 19:20:00", "alice-foreign-channel")
        self.add(20, 101, "2026-11-03 19:20:00", "bob-foreign-profile")
        with self.store.connect() as con:
            con.executemany(
                "INSERT INTO upload_records(profile_id,channel_db_id,uploaded_at,youtube_video_id) "
                "VALUES(20,202,'2026-11-03 19:20:00',?)",
                ((f"foreign-{i}",) for i in range(600)))
            con.execute("INSERT INTO script_history(channel_id,uploaded_at) "
                        "VALUES('ambiguous-legacy','2026-11-03 19:10:00')")
        actual = self.store.owner_records({10}, {101}, limit=500)
        self.assertEqual(len(actual), 4)
        self.assertEqual([r["youtube_video_id"] for r in actual],
                         ["alice-today", "alice-yesterday", "alice-30days", "alice-older"])
        s = self.store.owner_record_stats({10}, {101}, now=now)
        self.assertEqual(s, {"today": 1, "yesterday": 1, "days7": 2,
                             "days30": 3, "days90": 3, "days365": 3, "total": 4})

    def test_pkt_midnight_and_timestamp_offsets(self):
        now = datetime(2026, 11, 3, 19, 30, tzinfo=timezone.utc)  # 00:30 PKT Nov 4
        self.add(10, 101, "2026-11-04T00:15:00+05:00", "today-pkt")
        self.add(10, 101, "2026-11-03 18:59:59", "yesterday-pkt")
        s = self.store.owner_record_stats([10], [101], now=now)
        self.assertEqual((s["today"], s["yesterday"], s["total"]), (1, 1, 2))

    def test_fail_closed_no_access_or_mixed_owner(self):
        self.add(10, 101, "2026-11-03 19:10:00", "owned")
        for profiles, channels in (([], [101]), ([10], []), ([], []), ([10], [202]), ([20], [101])):
            with self.subTest(profiles=profiles, channels=channels):
                self.assertEqual(self.store.owner_records(profiles, channels), [])
                self.assertTrue(all(value == 0 for value in
                                    self.store.owner_record_stats(profiles, channels).values()))

    def test_all_time_not_capped_at_100000(self):
        with self.store.connect() as con:
            con.executemany(
                "INSERT INTO upload_records(profile_id,channel_db_id,uploaded_at,youtube_video_id) "
                "VALUES(10,101,'2020-01-01 00:00:00',?)",
                ((f"old-{i}",) for i in range(100001)))
        self.assertEqual(self.store.owner_record_stats({10}, {101})["total"], 100001)
        self.assertEqual(len(self.store.owner_records({10}, {101}, limit=500)), 500)

    def test_live_acl_rechecks_before_final_records_and_counts(self):
        content = APP.read_text(encoding="utf-8")
        self.assertIn('auto_v6["records"]=v6_store.owner_records(valid_ids,owned_channels,limit=500)', content)
        self.assertIn('auto_v6["record_stats"]=v6_store.owner_record_stats(valid_ids,owned_channels)', content)
        self.assertIn('_short_owned_automation_profiles(request)', content)
        engine = ENGINE.read_text(encoding="utf-8")
        self.assertIn('store.owner_records(allowed_profiles, allowed_channels, limit=500)', engine)
        self.assertIn('store.owner_record_stats(allowed_profiles, allowed_channels)', engine)


if __name__ == "__main__":
    unittest.main()

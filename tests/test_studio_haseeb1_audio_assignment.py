"""Regression tests for evidence-only Audio Downloader legacy ownership."""
from __future__ import annotations
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from production import studio_haseeb1_audio_assignment as audio
from production import runtime


class AudioOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.data = self.base / "data"
        self.scene = self.data / audio.SCENE_REL
        self.source = self.data / audio.DB_REL
        self.scene.parent.mkdir(parents=True)
        self.source.parent.mkdir(parents=True)
        with sqlite3.connect(self.scene) as con:
            con.execute("CREATE TABLE users(username TEXT,is_active INTEGER,studio_access TEXT,is_admin INTEGER)")
            con.execute("INSERT INTO users VALUES('haseeb1',1,'song,short',0)")
        with sqlite3.connect(self.source) as con:
            con.execute("""CREATE TABLE jobs(
                job_id TEXT PRIMARY KEY,video_id TEXT NOT NULL,status TEXT NOT NULL,
                owner_username TEXT NOT NULL DEFAULT '', song_maker_owner TEXT,
                auto_song_maker INTEGER NOT NULL DEFAULT 0,
                local_file TEXT,local_thumbnail TEXT,drive_path TEXT,
                thumbnail_drive_path TEXT,song_maker_job_id TEXT,song_folder_id TEXT,
                next_retry_at TEXT,upload_retry_count INTEGER DEFAULT 0)""")
            con.execute("""CREATE TABLE audio_preferences(
                owner TEXT PRIMARY KEY,format TEXT,quality INTEGER,
                auto_upload INTEGER,auto_song_maker INTEGER,song_folder_id TEXT)""")
            con.execute("INSERT INTO audio_preferences VALUES('haseeb1','wav',256,0,1,'pinned-folder')")
            con.execute("INSERT INTO audio_preferences VALUES('alice','mp3',128,1,0,'foreign-folder')")
        self.env = patch.multiple(runtime, BASE=self.base, DATA=self.data)
        self.disk = patch("shutil.disk_usage", return_value=SimpleNamespace(free=20_000_000_000))
        self.env.start()
        self.disk.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.disk.stop)

    def job(self, jid, status, owner="", handoff=None, auto=0):
        with sqlite3.connect(self.source) as db:
            db.execute("""INSERT INTO jobs(
                job_id,video_id,status,owner_username,song_maker_owner,auto_song_maker,
                local_file,local_thumbnail,drive_path,thumbnail_drive_path,
                song_maker_job_id,song_folder_id,next_retry_at,upload_retry_count)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (jid, "eH5Yd79Z_" + str(len(jid) % 10), status, owner, handoff, auto,
                 "/kept/audio.mp3", "/kept/thumb.jpg", "Song Studio/output/audio.mp3",
                 "Song Studio/output/thumb.jpg", "song-job", "saved-folder",
                 "2040-01-01T00:00:00+00:00", 17))

    def snapshot(self):
        with sqlite3.connect(self.source) as db:
            return db.execute("SELECT * FROM jobs ORDER BY job_id").fetchall()

    def preferences(self):
        with sqlite3.connect(self.source) as db:
            return db.execute("SELECT * FROM audio_preferences ORDER BY owner").fetchall()

    def test_readonly_inventory_reports_active_foreign_and_unknown_without_writes(self):
        self.job("a", "completed", handoff="haseeb1", auto=1)
        self.job("b", "upload_retry", handoff="haseeb1", auto=1)
        self.job("c", "completed", owner="alice", handoff="haseeb1", auto=1)
        self.job("d", "completed")
        self.job("e", "completed", owner="haseeb1")
        before = (self.snapshot(), self.preferences())
        result = audio.read_only(self.data, self.scene)
        self.assertEqual(result["inventory"]["total_jobs"], 5)
        self.assertEqual(result["inventory"]["nonterminal_jobs"], 1)
        self.assertEqual(result["inventory"]["owners"], {
            "already_owned": 1, "foreign_owner": 1, "handoff_conflict": 0,
            "unproven_legacy": 1, "nonterminal_deferred": 1, "eligible": 1,
        })
        self.assertEqual(before, (self.snapshot(), self.preferences()))
        self.assertFalse((self.base / "transactions").exists())

    def test_claim_only_proven_terminal_job_preserves_all_nonowner_columns(self):
        self.job("proved", "completed", handoff="haseeb1", auto=1)
        self.job("queued", "queued", handoff="haseeb1", auto=1)
        self.job("retry", "upload_retry", handoff="haseeb1", auto=1)
        self.job("unknown", "completed")
        self.job("foreign", "failed", owner="alice", handoff="haseeb1", auto=1)
        before = self.snapshot()
        prefs = self.preferences()
        result = audio.install("a" * 40)
        after = self.snapshot()
        self.assertEqual(result["claimed_now"], 1)
        self.assertEqual(result["state"], "PARTIAL")
        self.assertEqual([x[0] for x in before], [x[0] for x in after])
        for original, current in zip(before, after):
            self.assertEqual(original[:3] + original[4:], current[:3] + current[4:])
        self.assertEqual(dict((r[0], r[3]) for r in after),
                         {"foreign": "alice", "proved": "haseeb1", "queued": "",
                          "retry": "", "unknown": ""})
        self.assertEqual(self.preferences(), prefs)
        journal = self.base / "transactions/haseeb1-audio-downloader-owner" / ("a" * 40)
        with sqlite3.connect(journal / "backup/jobs.sqlite3") as backup:
            self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(backup.execute("SELECT owner_username FROM jobs WHERE job_id='proved'").fetchone()[0], "")
        self.assertEqual(json.loads((journal / "claimed.json").read_text())["job_ids"], ["proved"])
        self.assertNotIn("proved", (journal / "receipt.json").read_text())

    def test_idempotent_claim_and_online_compensation(self):
        self.job("verified", "completed", handoff="haseeb1", auto=1)
        first = audio.install("b" * 40)
        second = audio.install("c" * 40)
        self.assertEqual(first["claimed_now"], 1)
        self.assertEqual(second["claimed_now"], 0)
        self.assertEqual(audio.rollback("b" * 40)["claims_reverted"], 1)
        self.assertEqual(self.snapshot()[0][3], "")
        self.assertEqual(self.snapshot()[0][2], "completed")

    def test_foreign_preferences_are_never_merged_or_rekeyed(self):
        self.job("x", "completed", owner="alice", handoff="alice", auto=1)
        self.job("y", "completed", owner="haseeb1")
        pref = self.preferences()
        res = audio.install("d" * 40)
        self.assertEqual(res["claimed_now"], 0)
        self.assertEqual(self.preferences(), pref)
        self.assertEqual(self.snapshot()[0][3], "alice")

    def test_nonterminal_handoff_never_claimed_or_reset(self):
        for state in ("queued", "downloading", "downloaded", "uploading", "upload_retry"):
            self.job(state, state, handoff="haseeb1", auto=1)
        before = self.snapshot()
        res = audio.install("e" * 40)
        self.assertEqual(res["claimed_now"], 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(res["inventory"]["nonterminal_jobs"], 5)

    def test_explicit_foreign_handoff_blocks_implicit_claim(self):
        self.job("conflict", "completed", owner="", handoff="alice", auto=1)
        res = audio.install("f" * 40)
        self.assertEqual(res["inventory"]["owners"]["handoff_conflict"], 1)
        self.assertEqual(self.snapshot()[0][3], "")

    def test_account_not_active_aborts_without_creating_journal(self):
        with sqlite3.connect(self.scene) as db:
            db.execute("UPDATE users SET is_active=0")
        self.job("one", "completed", handoff="haseeb1", auto=1)
        with self.assertRaises(Exception):
            audio.install("1" * 40)
        self.assertEqual(self.snapshot()[0][3], "")
        self.assertFalse((self.base / "transactions").exists())

    def test_rollback_does_not_clobber_intervening_job_state(self):
        self.job("x", "completed", handoff="haseeb1", auto=1)
        audio.install("2" * 40)
        with sqlite3.connect(self.source) as db:
            db.execute("UPDATE jobs SET status='upload_retry' WHERE job_id='x'")
        with self.assertRaisesRegex(audio.AudioClaimBlocked, "ROLLBACK_OWNER_OR_STATE_CONFLICT"):
            audio.rollback("2" * 40)
        self.assertEqual(self.snapshot()[0][3], "haseeb1")
        self.assertEqual(self.snapshot()[0][2], "upload_retry")

    def test_legacy_missing_owner_column_reports_only_aggregate_evidence(self):
        self.job("h1", "completed", handoff="haseeb1", auto=1)
        self.job("h2", "failed", handoff="alice", auto=1)
        self.job("h3", "completed")
        with sqlite3.connect(self.source) as con:
            con.execute("ALTER TABLE jobs DROP COLUMN owner_username")
        before = self.preferences()
        report = audio.read_only(self.data, self.scene)
        self.assertEqual(report["schema_status"], "APPLY_BLOCKED_SCHEMA_DRIFT")
        self.assertEqual(report["schema_missing_jobs"], ["owner_username"])
        self.assertEqual(report["inventory"]["authenticated_handoff_matches"], 1)
        self.assertEqual(report["inventory"]["foreign_handoff_records"], 1)
        self.assertEqual(report["inventory"]["unproven_handoff_records"], 1)
        self.assertEqual(report["inventory"]["preferences"], {"target": 1, "other": 1})
        self.assertEqual(before, self.preferences())

    def test_foreign_handoff_active_account_proof_never_changes_owners(self):
        self.job("a", "completed", handoff="alice", auto=1)
        self.job("b", "completed", handoff="bob", auto=1)
        self.job("c", "completed", handoff="missing", auto=1)
        with sqlite3.connect(self.scene) as db:
            db.execute("INSERT INTO users VALUES('alice',1,'song,short',0)")
            db.execute("INSERT INTO users VALUES('bob',0,'song,short',0)")
        with sqlite3.connect(self.source) as db:
            db.execute("ALTER TABLE jobs DROP COLUMN owner_username")
        before = self.preferences()
        result = audio.read_only(self.data, self.scene)
        self.assertEqual(result["inventory"]["foreign_handoff_account_status"],
                         {"active_account": 1, "inactive_account": 1, "unknown_account": 1})
        self.assertEqual(result["inventory"]["foreign_handoff_records"], 3)
        self.assertEqual(before, self.preferences())

    def test_schema_drift_and_symlink_aborts_closed(self):
        with sqlite3.connect(self.source) as db:
            db.execute("DROP TABLE audio_preferences")
        report = audio.read_only(self.data, self.scene)
        self.assertEqual(report["schema_status"], "APPLY_BLOCKED_SCHEMA_DRIFT")
        self.assertIn("auto_upload", report["schema_missing_preferences"])
        self.assertEqual(report["inventory"]["total_jobs"], 0)
        with self.assertRaisesRegex(audio.AudioClaimBlocked, "AUDIO_PREF_SCHEMA_DRIFT"):
            audio.install("3" * 40)


    def legacy_jobs(self):
        """Observed live shape: 15 unknown, 5 handoffs to active foreign account."""
        with sqlite3.connect(self.scene) as db:
            db.execute("INSERT INTO users VALUES('alice',1,'song,short',0)")
        for idx in range(20):
            self.job(f"legacy{idx:02}", "completed",
                     handoff="alice" if idx >= 15 else None,
                     auto=1 if idx >= 15 else 0)
        with sqlite3.connect(self.source) as db:
            db.execute("ALTER TABLE jobs DROP COLUMN owner_username")

    def test_missing_column_claims_15_and_preserves_five_foreign_active_handoffs(self):
        self.legacy_jobs()
        before = self.snapshot()
        preferences = self.preferences()
        result = audio.install("4" * 40)
        self.assertEqual(result["state"], "OWNERSHIP_VERIFIED")
        self.assertEqual(result["claimed_now"], 15)
        self.assertEqual(result["preserved_foreign"], 5)
        self.assertEqual(result["schema_status"], "OWNER_COLUMN_ADDED")
        with sqlite3.connect(self.source) as db:
            after = db.execute("SELECT * FROM jobs ORDER BY job_id").fetchall()
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 20)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs WHERE owner_username='haseeb1'").fetchone()[0], 15)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs WHERE owner_username='alice'").fetchone()[0], 5)
        self.assertEqual([row[0] for row in before], [row[0] for row in after])
        self.assertEqual([tuple(row) for row in before], [tuple(row[:-1]) for row in after])
        self.assertEqual(self.preferences(), preferences)
        journal = self.base / "transactions/haseeb1-audio-downloader-owner" / ("4" * 40)
        with sqlite3.connect(journal / "backup/jobs.sqlite3") as backup:
            self.assertNotIn("owner_username",
                             [r[1] for r in backup.execute("PRAGMA table_info(jobs)")])
            self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_missing_column_rollback_owner_only(self):
        self.legacy_jobs()
        before = self.snapshot()
        audio.install("5" * 40)
        result = audio.rollback("5" * 40)
        self.assertEqual(result["claims_reverted"], 20)
        with sqlite3.connect(self.source) as db:
            after = db.execute("SELECT * FROM jobs ORDER BY job_id").fetchall()
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs WHERE owner_username=''").fetchone()[0], 20)
        self.assertEqual([tuple(row) for row in before], [tuple(row[:-1]) for row in after])

    def test_missing_column_active_job_never_alters_schema_or_jobs(self):
        self.legacy_jobs()
        with sqlite3.connect(self.source) as db:
            db.execute("UPDATE jobs SET status='upload_retry' WHERE job_id='legacy00'")
        before = self.snapshot()
        with self.assertRaisesRegex(audio.AudioClaimBlocked, "LEGACY_ACTIVE_JOBS"):
            audio.install("6" * 40)
        self.assertEqual(before, self.snapshot())
        self.assertFalse((self.base / "transactions").exists())

    def test_missing_column_foreign_inactive_account_never_claimed(self):
        self.legacy_jobs()
        with sqlite3.connect(self.scene) as db:
            db.execute("UPDATE users SET is_active=0 WHERE username='alice'")
        before = self.snapshot()
        with self.assertRaisesRegex(audio.AudioClaimBlocked, "LEGACY_FOREIGN_OWNER_NOT_ACTIVE"):
            audio.install("7" * 40)
        self.assertEqual(before, self.snapshot())

    def test_missing_column_new_job_blocks_legacy_assignment(self):
        self.legacy_jobs()
        with sqlite3.connect(self.source) as db:
            cols = self._columns()
            original = db.execute("SELECT * FROM jobs WHERE job_id='legacy00'").fetchone()
            copied = dict(zip(cols, original))
            copied["job_id"] = "extra"
            copied["video_id"] = "differentid"
            db.execute("INSERT INTO jobs (" + ",".join(cols) + ") VALUES (" +
                       ",".join("?" for _ in cols) + ")", [copied[col] for col in cols])
        with self.assertRaisesRegex(audio.AudioClaimBlocked, "LEGACY_TOTAL_CHANGED"):
            audio.install("8" * 40)
        self.assertNotIn("owner_username", self._columns())

    def _columns(self):
        with sqlite3.connect(self.source) as db:
            return [r[1] for r in db.execute("PRAGMA table_info(jobs)")]


if __name__ == "__main__":
    unittest.main()

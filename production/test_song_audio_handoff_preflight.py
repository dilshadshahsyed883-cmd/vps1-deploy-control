"""Safety regression tests: never bypass an active Song Maker handoff."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from production import song_audio_handoff_preflight as guard


class HandoffDrainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "audio.sqlite3"
        with sqlite3.connect(self.db) as con:
            con.execute("""CREATE TABLE jobs (
                status TEXT, song_maker_status TEXT, auto_song_maker INTEGER,
                auto_upload INTEGER, download_thumbnail INTEGER,
                song_maker_owner TEXT, local_file TEXT, local_thumbnail TEXT,
                song_maker_next_retry_at TEXT
            )""")

    def add(self, *, status="completed", phase="pending", auto=1, upload=0,
            thumbnail=1, owner="user_fixture", audio="file_fixture",
            image="image_fixture", retry=None):
        with sqlite3.connect(self.db) as con:
            con.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)",
                        (status, phase, auto, upload, thumbnail, owner, audio,
                         image, retry))

    def test_readonly_summary_distinguishes_genuine_due_and_stranded_rows(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        self.add()
        self.add(phase="sending", retry=future)
        self.add(status="failed")
        self.add(retry="invalid")
        expected = {"pending": 3, "sending": 1, "eligible": 3,
                    "ineligible": 1, "due": 1, "future_retry": 1,
                    "invalid_retry": 1}
        self.assertEqual(guard.handoff_summary(self.db), expected)
        with sqlite3.connect(self.db) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 4)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM jobs "
                                         "WHERE song_maker_status='pending'").fetchone()[0], 3)

    def test_real_idle_gate_passes_without_ever_sleeping(self):
        with patch.object(guard.runtime, "require_voice_release_idle") as idle, \
             patch.object(guard.time, "sleep") as sleep:
            outcome = guard.require_drain(self.root, self.db, timeout_seconds=0)
            self.assertEqual(outcome, {"handoff_drain": "PASS", "rechecks": 0})
            idle.assert_called_once_with(self.root, audio_db=self.db)
            sleep.assert_not_called()

    def test_eligible_pending_does_not_get_ignored_or_deleted(self):
        self.add()
        with patch.object(guard.runtime, "require_voice_release_idle",
                          side_effect=RuntimeError(guard.BUSY_HANDOFF)), \
             patch.object(guard.runtime, "event") as event:
            with self.assertRaisesRegex(RuntimeError, "did not drain"):
                guard.require_drain(self.root, self.db, timeout_seconds=0)
            self.assertEqual(guard.handoff_summary(self.db)["pending"], 1)
            event.assert_called_once()
            kwargs = event.call_args.kwargs
            self.assertEqual(kwargs["song_audio_handoff_readonly"]["eligible"], 1)
            self.assertNotIn("user_fixture", str(kwargs))
            self.assertNotIn("file_fixture", str(kwargs))

    def test_ineligible_old_pending_fails_immediately_without_mutation(self):
        self.add(status="failed", owner="")
        with patch.object(guard.runtime, "require_voice_release_idle",
                          side_effect=RuntimeError(guard.BUSY_HANDOFF)), \
             patch.object(guard.runtime, "event"), \
             patch.object(guard.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "cannot naturally drain"):
                guard.require_drain(self.root, self.db, timeout_seconds=60)
            sleep.assert_not_called()
        self.assertEqual(guard.handoff_summary(self.db)["ineligible"], 1)

    def test_non_handoff_protection_is_never_retried(self):
        self.add()
        with patch.object(guard.runtime, "require_voice_release_idle",
                          side_effect=RuntimeError("Audio Downloader has active/pending jobs")), \
             patch.object(guard.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "active/pending jobs"):
                guard.require_drain(self.root, self.db, timeout_seconds=120)
            sleep.assert_not_called()

    def test_only_actual_idle_gate_success_allows_completion(self):
        self.add()
        with patch.object(guard.runtime, "require_voice_release_idle",
                          side_effect=[RuntimeError(guard.BUSY_HANDOFF), None]) as idle, \
             patch.object(guard.runtime, "event"), \
             patch.object(guard.time, "sleep") as sleep:
            self.assertEqual(guard.require_drain(self.root, self.db,
                             timeout_seconds=60, poll_seconds=1),
                             {"handoff_drain": "PASS", "rechecks": 1})
            self.assertEqual(idle.call_count, 2)
            sleep.assert_called_once()

    def test_invalid_wait_controls_fail_closed(self):
        with self.assertRaises(ValueError):
            guard.require_drain(self.root, self.db, timeout_seconds=-1)
        with self.assertRaises(ValueError):
            guard.require_drain(self.root, self.db, poll_seconds=0)

    def test_linked_database_is_rejected(self):
        link = self.root / "linked.sqlite3"
        link.symlink_to(self.db)
        with self.assertRaisesRegex(RuntimeError, "missing"):
            guard.handoff_summary(link)

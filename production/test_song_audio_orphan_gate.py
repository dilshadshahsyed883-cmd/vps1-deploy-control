"""Proof-aware Song handoff fence, preserving all active cases and original rows."""
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from production import runtime


class OrphanHandoffIdleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.voice = self.root / "voice"
        for name in ("kokoro", "edge"):
            path = self.voice / name / "data" / (name + "_jobs.sqlite3")
            path.parent.mkdir(parents=True)
            with sqlite3.connect(path) as conn:
                conn.execute("CREATE TABLE jobs(status TEXT, delivery_status TEXT)")
        self.audio = self.root / "audio.sqlite3"
        with sqlite3.connect(self.audio) as conn:
            conn.execute("""CREATE TABLE jobs(
                job_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                song_maker_status TEXT NOT NULL,
                local_file TEXT,
                local_thumbnail TEXT
            )""")

    def add(self, ident="fixture", status="failed", phase="pending",
            audio=None, thumbnail=None):
        with sqlite3.connect(self.audio) as conn:
            conn.execute("INSERT INTO jobs VALUES (?,?,?,?,?)",
                         (ident, status, phase, audio, thumbnail))

    def check(self):
        runtime.require_voice_release_idle(self.voice, audio_db=self.audio)

    def test_stale_failed_job_with_no_media_pointers_is_preserved(self):
        self.add()
        with patch.object(runtime, "event") as report:
            self.check()
            report.assert_called_once_with(
                app="song-studio", dormant_handoff_orphans=1,
                ownership="READ_ONLY_PROOF", original_jobs="PRESERVED")
        with sqlite3.connect(self.audio) as db:
            self.assertEqual(db.execute("SELECT status,song_maker_status,local_file,"
                                        "local_thumbnail FROM jobs").fetchone(),
                             ("failed", "pending", None, None))

    def test_only_terminal_noncompleted_orphans_qualify(self):
        for status in ("cancelled", "delivered"):
            with self.subTest(status=status):
                with sqlite3.connect(self.audio) as db:
                    db.execute("DELETE FROM jobs")
                self.add(status=status)
                self.check()
        for status in ("completed", "queued", "processing", "mystery"):
            with self.subTest(status=status):
                with sqlite3.connect(self.audio) as db:
                    db.execute("DELETE FROM jobs")
                self.add(status=status)
                with self.assertRaises(RuntimeError):
                    self.check()

    def test_real_sending_never_qualifies_even_with_no_media(self):
        self.add(phase="sending")
        with self.assertRaisesRegex(RuntimeError, "active Song Maker handoffs"):
            self.check()

    def test_any_real_media_pointer_blocks_orphan_exemption(self):
        for audio, image in (("/original/audio.mp3", None),
                             (None, "/original/thumb.jpg"),
                             ("/original/audio.mp3", "/original/thumb.jpg")):
            with self.subTest(audio=bool(audio), image=bool(image)):
                with sqlite3.connect(self.audio) as db:
                    db.execute("DELETE FROM jobs")
                self.add(audio=audio, thumbnail=image)
                with self.assertRaisesRegex(RuntimeError, "active Song Maker handoffs"):
                    self.check()

    def test_ineligible_row_does_not_hide_other_active_handoff(self):
        self.add("legacy")
        self.add("live", status="completed", audio="/media/a.mp3",
                 thumbnail="/media/a.jpg")
        with self.assertRaisesRegex(RuntimeError, "active Song Maker handoffs"):
            self.check()

    def test_legacy_missing_columns_fail_closed_instead_of_guessing(self):
        with sqlite3.connect(self.audio) as conn:
            conn.execute("ALTER TABLE jobs RENAME TO original")
            conn.execute("CREATE TABLE jobs(job_id TEXT,status TEXT,song_maker_status TEXT)")
            conn.execute("INSERT INTO jobs VALUES('old','failed','pending')")
        with self.assertRaisesRegex(RuntimeError, "active Song Maker handoffs"):
            self.check()

    def test_active_voice_or_download_job_still_blocks(self):
        self.add()
        with sqlite3.connect(self.audio) as db:
            db.execute("INSERT INTO jobs VALUES('downloading','processing','off',NULL,NULL)")
        with self.assertRaisesRegex(RuntimeError, "active/pending jobs"):
            self.check()
        with sqlite3.connect(self.audio) as db:
            db.execute("DELETE FROM jobs WHERE job_id='downloading'")
        kokoro = self.voice / "kokoro/data/kokoro_jobs.sqlite3"
        with sqlite3.connect(kokoro) as db:
            db.execute("INSERT INTO jobs VALUES('processing','idle')")
        with self.assertRaisesRegex(RuntimeError, "Kokoro has pending"):
            self.check()

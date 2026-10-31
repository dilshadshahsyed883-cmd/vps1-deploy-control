"""Fail-closed global Audio Downloader idleness without any authenticated/user-scoped API."""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from production.voice_template_release import require_idle_audio_downloader


class TestAudioIdleGlobalPreflight(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "data").mkdir()
        self.db = self.root / "data" / "jobs.sqlite3"
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TABLE jobs (status TEXT NOT NULL)")
        setting = patch.dict(os.environ, {"AUDIO_DOWNLOADER_ROOT": str(self.root)})
        setting.start()
        self.addCleanup(setting.stop)

    def job(self, state):
        with sqlite3.connect(self.db) as conn:
            conn.execute("INSERT INTO jobs(status) VALUES (?)", (state,))

    def test_empty_and_only_terminal_jobs_allow_reload(self):
        require_idle_audio_downloader()
        for state in ("completed", "delivered", "failed", "cancelled"):
            self.job(state)
        require_idle_audio_downloader()

    def test_all_tenant_active_and_retry_jobs_block(self):
        for state in ("queued", "downloading", "downloaded", "uploading",
                      "upload_retry", "processing", "converting", "new_unknown_state"):
            with self.subTest(state=state):
                with sqlite3.connect(self.db) as conn:
                    conn.execute("DELETE FROM jobs")
                self.job(state)
                with self.assertRaisesRegex(RuntimeError, "active/pending jobs"):
                    require_idle_audio_downloader()

    def test_missing_database_blocks(self):
        self.db.unlink()
        with self.assertRaisesRegex(RuntimeError, "database unavailable"):
            require_idle_audio_downloader()
        self.assertFalse(self.db.exists(), "preflight must not create a new database")

    def test_missing_table_blocks(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("DROP TABLE jobs")
        with self.assertRaisesRegex(RuntimeError, "preflight unavailable"):
            require_idle_audio_downloader()

    def test_default_uses_same_data_root_as_production_runtime(self):
        from production import voice_template_release as release
        default_db = self.root / "voice-studio" / "audio-downloader" / "data" / "jobs.sqlite3"
        default_db.parent.mkdir(parents=True)
        with sqlite3.connect(default_db) as conn:
            conn.execute("CREATE TABLE jobs (status TEXT NOT NULL)")
            conn.execute("INSERT INTO jobs(status) VALUES ('completed')")
        # Remove the service-only env variable, as on the production deploy host.
        with patch.object(release, "DATA", self.root), patch.dict(os.environ, {}, clear=True):
            require_idle_audio_downloader()
            with sqlite3.connect(default_db) as conn:
                conn.execute("INSERT INTO jobs(status) VALUES ('upload_retry')")
            with self.assertRaisesRegex(RuntimeError, "active/pending jobs"):
                require_idle_audio_downloader()

    def test_symlinked_database_blocks(self):
        actual = self.root / "real.sqlite3"
        self.db.rename(actual)
        self.db.symlink_to(actual)
        with self.assertRaisesRegex(RuntimeError, "database unavailable"):
            require_idle_audio_downloader()


if __name__ == "__main__":
    unittest.main()

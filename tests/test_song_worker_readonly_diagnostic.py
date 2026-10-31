"""Offline tests for Song worker read-only, redacted diagnostic."""
from __future__ import annotations
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from production import song_uploader_worker_readonly_diagnostic as diag

class SongWorkerReadOnlyTests(unittest.TestCase):
    def test_categories_are_allowlisted_not_raw_errors(self):
        self.assertEqual(diag.classify("sqlite3.OperationalError: no such column: x"),"DB_SCHEMA")
        self.assertEqual(diag.classify("ConnectTimeout to proxy"),"NETWORK")
        self.assertEqual(diag.classify("Secret token REDACTED unauthorized"),"AUTH")
        self.assertEqual(diag.classify(""),"NONE")

    def test_output_contains_only_aggregates_even_for_sensitive_exception(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            v6=root/"auto_engine_v6"
            v6.mkdir()
            with sqlite3.connect(v6/"auto_engine_v6.sqlite3") as db:
                db.execute("CREATE TABLE engine_state (singleton INTEGER PRIMARY KEY, last_worker_error TEXT, last_scheduler_error TEXT, running INTEGER, scheduler_armed INTEGER)")
                db.execute("INSERT INTO engine_state VALUES (1, ?, '', 1, 1)",
                    ("sqlite3.OperationalError: no such column: example_secret_token_abc",))
            output=io.StringIO()
            with patch.object(diag,"unit_state",return_value="ACTIVE"), patch("sys.stdout",output):
                diag.run(root)
            text=output.getvalue()
            self.assertIn('"worker_error": "DB_SCHEMA"',text)
            self.assertIn('"scheduler_error": "NONE"',text)
            self.assertIn('"engine_running": true',text)
            self.assertNotIn("example_secret_token_abc",text)

    def test_missing_or_symlinked_db_fails_closed_without_following_link(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with patch.object(diag,"unit_state",return_value="UNKNOWN"):
                self.assertEqual(diag.collect(root)["db"],"UNAVAILABLE")
                v6=root/"auto_engine_v6"
                v6.mkdir()
                (v6/"auto_engine_v6.sqlite3").symlink_to("/etc/passwd")
                self.assertEqual(diag.collect(root)["db"],"UNAVAILABLE")

    def test_diagnostic_uses_bounded_serialized_lock_retry(self):
        workflow=(Path(__file__).resolve().parents[1]/".github/workflows/vps1-song-studio-release.yml").read_text()
        self.assertIn("'[vps1-song-worker-readonly-diagnose]'*",workflow)
        self.assertIn("for attempt in {1..30}; do",workflow)
        self.assertIn('BlockingIOError: [Errno 11] Resource temporarily unavailable',workflow)
        self.assertIn('apps=(local-upload-manager)',workflow)

    def test_dispatch_is_diagnostic_only(self):
        source=(Path(__file__).resolve().parents[1]/"production/runtime.py").read_text()
        self.assertIn("[vps1-song-worker-readonly-diagnose]",source)
        segment=source.split("if '[vps1-song-worker-readonly-diagnose]' in intent:",1)[1].split("if '[vps1-shortstudio-readonly-diagnose]'",1)[0]
        self.assertIn("report_song_worker(DATA / 'local-upload-manager/lum-song')",segment)
        self.assertIn("return  # No source install",segment)
        self.assertNotIn("install_app(",segment)

if __name__=="__main__":
    unittest.main()

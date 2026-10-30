"""Song Uploader: independent component health and accessible mobile Automation cards.

No VPS, YouTube, worker, profile, queue, or scheduler side effects.
"""
from __future__ import annotations

import ast
import sqlite3
import tempfile
import unittest
from pathlib import Path

from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[1]
PART = ROOT / "apps/local-upload-manager/part01"
ENGINE = (PART / "lum_engine/auto_engine_v6.py").read_text()
ROUTES = (PART / "lum_engine/standalone_app.py").read_text()
TEMPLATE = (PART / "templates/app.html").read_text()
CSS = (PART / "static/song_uploader_kokoro_ui.css").read_text()
BASE = (PART / "templates/base.html").read_text()
RELEASE = (ROOT / "production/song_uploader_group_summary_release.py").read_text()


class SongComponentStatusMobileTests(unittest.TestCase):
    def test_heartbeat_updates_only_own_error_and_retains_legacy_error(self):
        parsed = ast.parse(ENGINE)
        store = next(x for x in parsed.body if isinstance(x, ast.ClassDef) and x.name == "V6Store")
        method = next(x for x in store.body if isinstance(x, ast.FunctionDef) and x.name == "heartbeat")
        class_ast = ast.ClassDef(name="HeartbeatOnly", bases=[], keywords=[],
                                 body=[method], decorator_list=[])
        mod = ast.fix_missing_locations(ast.Module(body=[class_ast], type_ignores=[]))
        namespace = {"utcnow": lambda: "2026-10-30T17:00:00Z",
                     "clean": lambda s: str(s or "").strip()}
        exec(compile(mod, "<heartbeat-test>", "exec"), namespace)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "engine.db"
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE engine_state (singleton INTEGER PRIMARY KEY, last_worker_heartbeat TEXT, last_scheduler_heartbeat TEXT, last_error TEXT, last_worker_error TEXT NOT NULL DEFAULT '', last_scheduler_error TEXT NOT NULL DEFAULT '', updated_at TEXT)")
                db.execute("INSERT INTO engine_state(singleton,last_error) VALUES(1,'legacy text')")
            class FakeStore(namespace["HeartbeatOnly"]):
                def connect(self):
                    return sqlite3.connect(path)
            subject = FakeStore()
            subject.heartbeat("worker", "worker failed")
            subject.heartbeat("scheduler")
            with sqlite3.connect(path) as db:
                worker, scheduler, legacy = db.execute(
                    "SELECT last_worker_error,last_scheduler_error,last_error FROM engine_state WHERE singleton=1"
                ).fetchone()
            self.assertEqual((worker, scheduler, legacy), ("worker failed", "", "legacy text"))
            subject.heartbeat("scheduler", "scheduler failed")
            subject.heartbeat("worker")
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("SELECT last_worker_error,last_scheduler_error FROM engine_state").fetchone(),
                                 ("", "scheduler failed"))
            with self.assertRaisesRegex(ValueError, "Unknown"):
                subject.heartbeat("invalid", "test")

    def test_migration_is_additive_and_status_does_not_share_last_error(self):
        for name in ("last_worker_error", "last_scheduler_error"):
            self.assertIn("ALTER TABLE engine_state ADD COLUMN " + name, ENGINE)
            self.assertIn('if "' + name + '" not in cols:', ENGINE)
        self.assertIn('_worker_error = str(_engine.get("last_worker_error")', ROUTES)
        self.assertIn('_scheduler_error = str(_engine.get("last_scheduler_error")', ROUTES)
        self.assertNotIn('_last_error = str(_engine.get("last_error")', ROUTES)

    def test_song_mobile_card_is_labeled_and_other_uploaders_remain_tables(self):
        Environment().parse(TEMPLATE)
        block = TEMPLATE.split('data-song-group-summary', 1)[1].split("</table>", 1)[0]
        for label in ("ID", "ON/OFF", "Profile", "Group", "Channels",
                      "Videos", "Schedule (PKT)", "State", "Actions"):
            self.assertIn('data-mobile-label="' + label + '"', block)
        self.assertIn('.lum-shell[data-engine-instance="song"] .lum-ae-automation-profiles', CSS)
        self.assertIn('.lum-ae-empty-row', CSS)
        self.assertIn('overflow-x:hidden!important', CSS)
        self.assertIn('grid-template-columns:repeat(2,minmax(0,1fr))', CSS)
        self.assertIn("View Logs</a>", TEMPLATE)
        self.assertIn("engine_instance == 'song'", TEMPLATE)

    def test_guarded_song_only_release_includes_new_stylesheet(self):
        self.assertIn('"static/song_uploader_kokoro_ui.css"', RELEASE)
        self.assertIn("'20261030-status-cards-v2'", RELEASE)
        self.assertIn("&amp;song=20261030-status-cards-v2", BASE)
        self.assertIn("SONG_GROUP_SUMMARY_ROLLBACK=PASS", RELEASE)
        self.assertIn("    idle()", RELEASE)


if __name__ == "__main__":
    unittest.main()

"""Exercise the deployed claim-method repair and fail-closed profile fencing."""
import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from typing import Any, Dict, Iterable, List, Optional

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / "production/song_uploader_group_summary_release.py"
QUEUE = ROOT / "apps/local-upload-manager/part01/desktop_core/modules/db/queue_repository.py"


def load_overlay():
    tree = ast.parse(RELEASE.read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "queue_claim_overlay")
    def require(condition, detail):
        if not condition:
            raise RuntimeError(detail)
    scope = {"require": require, "QUEUE_REPOSITORY": str(QUEUE)}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(RELEASE), "exec"), scope)
    return scope["queue_claim_overlay"]


def legacy_source(source):
    tree = ast.parse(source)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef))
    fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "claim_available_jobs")
    idx = next(i for i, a in enumerate(fn.args.kwonlyargs) if a.arg == "allowed_auto_engine_profile_ids")
    del fn.args.kwonlyargs[idx]
    del fn.args.kw_defaults[idx]
    block = next(n for n in fn.body if isinstance(n, ast.Try))
    block.body = [n for n in block.body if not (
        isinstance(n, ast.If) and ast.unparse(n.test) == "allowed_auto_engine_profile_ids is not None")]
    return ast.unparse(tree).encode()


class ClaimRepairTests(unittest.TestCase):
    def setUp(self):
        self.source = QUEUE.read_bytes()
        self.overlay = load_overlay()

    def test_old_caller_contract_is_restored_without_replacing_other_methods(self):
        old = legacy_source(self.source.decode())
        patched = self.overlay(old, self.source)
        before, after = ast.parse(old), ast.parse(patched)
        bc = next(n for n in before.body if isinstance(n, ast.ClassDef))
        ac = next(n for n in after.body if isinstance(n, ast.ClassDef))
        for b, a in zip(bc.body, ac.body):
            if getattr(b, "name", "") != "claim_available_jobs":
                self.assertEqual(ast.dump(b), ast.dump(a))
        self.assertIn("allowed_auto_engine_profile_ids", patched.decode())

    def test_already_compatible_is_idempotent(self):
        self.assertEqual(self.overlay(self.source, self.source), self.source)

    def test_unreviewed_claim_drift_blocks(self):
        old = legacy_source(self.source.decode()).replace(b"wanted = 1", b"wanted = 5")
        with self.assertRaisesRegex(RuntimeError, "drift"):
            self.overlay(old, self.source)

    def claim_fixture(self, allowed):
        patched = self.overlay(legacy_source(self.source.decode()), self.source)
        tree = ast.parse(patched)
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef))
        fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "claim_available_jobs")
        scope = dict(globals())
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "<claim>", "exec"), scope)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "queue.sqlite"
            def connect():
                con = sqlite3.connect(path)
                con.row_factory = sqlite3.Row
                return con
            with connect() as con:
                con.execute("""CREATE TABLE videos (
                    id INTEGER PRIMARY KEY, channel_db_id INTEGER, auto_engine_profile_id INTEGER,
                    status TEXT, claim_worker_id TEXT, claimed_at TEXT, lease_expires_at TEXT,
                    app_upload_at TEXT, created_at TEXT, normalized_filename TEXT,
                    updated_at TEXT, recovery_state TEXT)""")
                for vid, profile in ((1, 10), (2, 20)):
                    con.execute("INSERT INTO videos(id,channel_db_id,auto_engine_profile_id,status,created_at,normalized_filename) VALUES (?,?,?,'QUEUED','2026-01-01',?)",
                                (vid, vid, profile, str(vid)))
                con.execute("INSERT INTO videos(id,channel_db_id,auto_engine_profile_id,status) VALUES (3,3,20,'PUBLISHED')")
            class FakeDB:
                def ensure_pending_upload_identities(self, **kwargs): pass
                def _set_ledger_state(self, *args): pass
                def get_video(self, vid):
                    with connect() as con:
                        return dict(con.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone())
            db = FakeDB()
            db.connect = connect
            jobs = scope["claim_available_jobs"](db, "test-worker", 1, auto_engine_only=True,
                                                  allowed_auto_engine_profile_ids=allowed)
            with connect() as con:
                statuses = dict(con.execute("SELECT id,status FROM videos"))
            return jobs, statuses

    def test_only_selected_profile_is_claimed(self):
        jobs, statuses = self.claim_fixture([20])
        self.assertEqual([j["id"] for j in jobs], [2])
        self.assertEqual(statuses, {1:"QUEUED", 2:"CLAIMED", 3:"PUBLISHED"})

    def test_empty_profile_scope_claims_nothing(self):
        jobs, statuses = self.claim_fixture([])
        self.assertEqual(jobs, [])
        self.assertEqual(statuses, {1:"QUEUED", 2:"QUEUED", 3:"PUBLISHED"})

    def test_unscoped_legacy_claim_remains_serial(self):
        jobs, statuses = self.claim_fixture(None)
        self.assertEqual([j["id"] for j in jobs], [1])
        self.assertEqual(statuses[2], "QUEUED")

    def test_release_checks_multiple_worker_turns_and_keeps_rollback(self):
        text = RELEASE.read_text()
        self.assertIn("        verify_worker_recovery()", text)
        self.assertIn('for _ in range(3):', text)
        self.assertIn('report.get("worker_error") == "NONE"', text)
        self.assertIn("SONG_GROUP_SUMMARY_ROLLBACK=PASS", text)
        self.assertIn("backup_databases(STATE", text)


if __name__ == "__main__":
    unittest.main()

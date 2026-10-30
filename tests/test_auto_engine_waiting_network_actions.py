"""Fail-closed retry/cancel contracts for WAITING_NETWORK Auto Engine queue rows."""
from __future__ import annotations

import ast
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[1]
PART = ROOT / "apps/local-upload-manager/part01"
REPO = PART / "desktop_core/modules/db/queue_repository.py"
ROUTES = PART / "lum_engine/standalone_app.py"
TEMPLATE = PART / "templates/app.html"
SCRIPT = PART / "static/auto_engine_v6.js"


def waiting_only():
    tree = ast.parse(REPO.read_text())
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "QueueRepository")
    fn = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "resolve_waiting_network_job")
    code = ast.Module(body=[ast.ClassDef(name="WaitingOnly", bases=[], keywords=[], body=[fn], decorator_list=[])], type_ignores=[])
    namespace = {}
    exec(compile(ast.fix_missing_locations(code), str(REPO), "exec"), namespace)
    return namespace["WaitingOnly"]


class WaitingNetworkQueueActions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        db_path = Path(self.tmp.name) / "queue.db"
        def connect():
            conn = sqlite3.connect(db_path, timeout=5)
            conn.row_factory = sqlite3.Row
            return conn
        self.connect = connect
        with connect() as conn:
            conn.executescript("""
                CREATE TABLE videos(
                    id INTEGER PRIMARY KEY,auto_engine_profile_id INTEGER,status TEXT,
                    claim_worker_id TEXT,recovery_state TEXT,youtube_video_id TEXT,youtube_url TEXT,
                    resumable_uri TEXT,media_stage_state TEXT,pipeline_stage TEXT,
                    upload_bytes_confirmed INTEGER,channel_db_id INTEGER,upload_fingerprint TEXT,
                    claimed_at TEXT,lease_expires_at TEXT,error_message TEXT,updated_at TEXT,progress INTEGER DEFAULT 0);
                CREATE TABLE upload_ledger(
                    channel_db_id INTEGER,upload_fingerprint TEXT,state TEXT,
                    video_id INTEGER,updated_at TEXT);
                CREATE TABLE upload_logs(video_id INTEGER,level TEXT,message TEXT,progress INTEGER);
                INSERT INTO videos(id,auto_engine_profile_id,status,recovery_state,
                    media_stage_state,pipeline_stage,upload_bytes_confirmed,channel_db_id,upload_fingerprint)
                    VALUES (1,9,'WAITING_NETWORK','NONE','PENDING','PENDING',0,4,'fp-1');
                INSERT INTO upload_ledger(channel_db_id,upload_fingerprint,state,video_id)
                    VALUES(4,'fp-1','CLAIMED',1);
            """)
        self.obj = waiting_only()()
        self.obj.connect = connect
        def ledger(conn, video_id, state, last_error=""):
            conn.execute("UPDATE upload_ledger SET state=? WHERE video_id=?", (state, video_id))
        self.obj._set_ledger_state = ledger

    def row(self):
        with self.connect() as conn:
            return conn.execute("SELECT * FROM videos WHERE id=1").fetchone()

    def test_retry_returns_only_proven_pre_media_job_to_queue(self):
        ok, message = self.obj.resolve_waiting_network_job(1, "retry")
        self.assertTrue(ok, message)
        self.assertEqual(self.row()["status"], "QUEUED")
        with self.connect() as conn:
            self.assertEqual(conn.execute("SELECT state FROM upload_ledger").fetchone()[0], "RESERVED")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM upload_logs").fetchone()[0], 1)

    def test_cancel_removes_only_safe_queue_row_and_retains_ledger(self):
        ok, message = self.obj.resolve_waiting_network_job(1, "cancel")
        self.assertTrue(ok, message)
        self.assertIsNone(self.row())
        with self.connect() as conn:
            self.assertEqual(conn.execute("SELECT state FROM upload_ledger").fetchone()[0], "CANCELLED")
            self.assertIsNone(conn.execute("SELECT video_id FROM upload_ledger").fetchone()[0])
            self.assertIn("cancelled", conn.execute("SELECT message FROM upload_logs").fetchone()[0])
        self.assertFalse(self.obj.resolve_waiting_network_job(1, "retry")[0])

    def test_active_or_uncertain_network_waits_cannot_be_overridden(self):
        variants = [
            ("claim_worker_id", "worker-123"),
            ("youtube_video_id", "REMOTE123"),
            ("resumable_uri", "https://resumable.example"),
            ("media_stage_state", "RUNNING"),
            ("pipeline_stage", "MEDIA"),
            ("upload_bytes_confirmed", 42),
            ("recovery_state", "REVIEW_REQUIRED"),
        ]
        for column, value in variants:
            with self.subTest(column=column):
                with self.connect() as conn:
                    conn.execute(f"UPDATE videos SET {column}=? WHERE id=1", (value,))
                for action in ("retry", "cancel"):
                    ok, _ = self.obj.resolve_waiting_network_job(1, action)
                    self.assertFalse(ok)
                    self.assertEqual(self.row()["status"], "WAITING_NETWORK")
                with self.connect() as conn:
                    conn.execute(f"UPDATE videos SET {column}=? WHERE id=1", ("PENDING" if column in {"media_stage_state","pipeline_stage"} else "NONE" if column=="recovery_state" else 0 if column=="upload_bytes_confirmed" else None,))
        with self.connect() as conn:
            conn.execute("UPDATE upload_ledger SET state='AMBIGUOUS'")
        self.assertFalse(self.obj.resolve_waiting_network_job(1, "retry")[0])
        self.assertFalse(self.obj.resolve_waiting_network_job(1, "cancel")[0])

    def test_guarded_deployment_allows_only_unclaimed_pre_media_network_wait(self):
        runtime = ROOT / "production/runtime.py"
        tree = ast.parse(runtime.read_text())
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "lum_parked_network_idle")
        scope = {"sqlite3": sqlite3}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])), str(runtime), "exec"), scope)
        guard = scope["lum_parked_network_idle"]
        with self.connect() as conn:
            self.assertEqual(guard(conn), 1)
            conn.execute("UPDATE videos SET claim_worker_id='live-worker' WHERE id=1")
        with self.connect() as conn:
            with self.assertRaisesRegex(RuntimeError, "worker"):
                guard(conn)
            conn.execute("UPDATE videos SET claim_worker_id=NULL,progress=15 WHERE id=1")
        with self.connect() as conn:
            with self.assertRaisesRegex(RuntimeError, "progress"):
                guard(conn)
            conn.execute("UPDATE videos SET progress=0 WHERE id=1")
            conn.execute("UPDATE upload_ledger SET state='MEDIA_IN_PROGRESS'")
        with self.connect() as conn:
            with self.assertRaisesRegex(RuntimeError, "ledger"):
                guard(conn)
        source = runtime.read_text()
        self.assertIn("parked = lum_parked_network_idle(conn)", source)
        self.assertIn('"PROCESSING", "IN_PROGRESS")', source)

    def test_wrong_status_cannot_retry_or_cancel(self):
        with self.connect() as conn:
            conn.execute("UPDATE videos SET status='UPLOADING' WHERE id=1")
        self.assertFalse(self.obj.resolve_waiting_network_job(1, "retry")[0])
        self.assertFalse(self.obj.resolve_waiting_network_job(1, "cancel")[0])

    def test_ui_includes_direct_buttons_even_for_legacy_job(self):
        src = TEMPLATE.read_text()
        self.assertIn("lum-ae-network-actions", src)
        self.assertIn('name="action" value="retry"', src)
        self.assertIn('name="action" value="cancel"', src)
        self.assertIn("if (q.status or '')|upper == 'WAITING_NETWORK'", src)
        self.assertLess(src.index("lum-ae-network-actions"), src.index("{% if q.channel_job %}"))
        Environment().parse(src)

    def test_stale_queue_ids_are_pruned_after_live_refresh(self):
        js = SCRIPT.read_text()
        self.assertIn("const visible=new Set(boxes.map(cb=>Number(cb.value)))", js)
        self.assertIn("if(!visible.has(id))qSelected.delete(id)", js)
        routes = ROUTES.read_text()
        self.assertIn('/actions/auto-v6/job/network-action', routes)
        self.assertIn("_v6_profile(int(row[\"auto_engine_profile_id\"]),request)", routes)
        self.assertIn('require_csrf(request,str(form.get("csrf_token") or ""))', routes)


if __name__ == "__main__":
    unittest.main()

"""Regression checks for owner-approved cancelled Scene project deletion."""
from __future__ import annotations
import ast
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER = (ROOT / "apps/scene-builder/part02/api_server.py").read_text()
WEB = (ROOT / "apps/scene-builder/part01/app.py").read_text()
QUEUE = (ROOT / "apps/scene-builder/part01/templates/queue.html").read_text()
POLICY = (ROOT / "apps/scene-builder/part01/precutover_policy.py").read_text()


class FakeHttpException(Exception):
    def __init__(self, status_code, detail):
        self.status_code, self.detail = status_code, detail
        super().__init__(detail)


class SceneCancelDeleteTests(unittest.TestCase):
    def test_worker_cancel_only_and_real_local_cleanup(self):
        tree = ast.parse(WORKER)
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "delete")
        fn.decorator_list = []  # Unit-test the handler without starting FastAPI.
        code = compile(ast.Module(body=[fn], type_ignores=[]), "<delete>", "exec")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            identifier = "a" * 32
            other = "b" * 32
            (root / identifier).mkdir()
            (root / other).mkdir()
            (root / identifier / "render.mp4").write_bytes(b"private rendered video")
            (root / other / "keep.mp4").write_bytes(b"another project")
            sql = root / "queue.db"
            with sqlite3.connect(sql) as conn:
                conn.executescript("""CREATE TABLE jobs (id TEXT PRIMARY KEY, status TEXT);
                    CREATE TABLE job_events (job_id TEXT);
                    INSERT INTO jobs VALUES ('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','cancelled');
                    INSERT INTO jobs VALUES ('bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb','completed');
                    INSERT INTO job_events VALUES ('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');
                    INSERT INTO job_events VALUES ('bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb');""")
            active = {"status": "completed", "cancel_requested": 1}
            state = {"remote_called": False}
            def fake_remote(job, directory):
                state["remote_called"] = True
                return False
            ns = {
                "auth": lambda token: None,
                "re": __import__("re"),
                "HTTPException": FakeHttpException,
                "JOBS": root,
                "_drive_naming_lock": threading.RLock(),
                "_audit_lock": lambda jid: threading.RLock(),
                "get_job": lambda jid: dict(active),
                "_delete_cancelled_drive_output": fake_remote,
                "shutil": __import__("shutil"),
                "_db_lock": threading.RLock(),
                "db": lambda: sqlite3.connect(sql),
                "_runtime_value_conn": lambda conn, key, default="": identifier,
                "_runtime_set_conn": lambda conn, key, value: None,
                "_wake": threading.Event(),
            }
            exec(code, ns)
            with self.assertRaises(FakeHttpException) as refusal:
                ns["delete"](identifier)
            self.assertEqual(refusal.exception.status_code, 409)
            self.assertFalse(state["remote_called"])
            self.assertTrue((root / identifier / "render.mp4").exists())
            active.update(status="cancelled", cancel_requested=0)
            with self.assertRaises(FakeHttpException):
                ns["delete"](identifier)
            self.assertFalse(state["remote_called"])
            active["cancel_requested"] = 1
            self.assertEqual(ns["delete"](identifier)["deleted"], identifier)
            self.assertTrue(state["remote_called"])
            self.assertFalse((root / identifier).exists())
            self.assertEqual((root / other / "keep.mp4").read_bytes(), b"another project")
            with sqlite3.connect(sql) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs WHERE id=?", (identifier,)).fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM job_events WHERE job_id=?", (identifier,)).fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs WHERE id=?", (other,)).fetchone()[0], 1)

    def test_remote_deletion_is_verified_and_file_scoped(self):
        worker = WORKER.split("def _delete_cancelled_drive_output(", 1)[1].split("\n\n@app.delete", 1)[0]
        for snippet in ('drive_destination', 'output_filename', 'len(matches) > 1',
                        'id<>? AND drive_destination=?', '_drive_entry_matches_local(entry, output)',
                        'output.resolve().is_relative_to(project_dir.resolve())',
                        '["rclone", "deletefile", destination]',
                        'remote_entry() is not None'):
            self.assertIn(snippet, worker)
        self.assertNotIn('"rclone", "purge"', worker)

    def test_web_authorization_metadata_and_ui(self):
        ast.parse(WEB)
        part = WEB.split('@app.post("/queue/{job_id}/delete")', 1)[1].split("\n\n@app.post(", 1)[0]
        for snippet in ("require_job_access(request, job_id)",
                        'worker_request("DELETE", f"/jobs/{job_id}"',
                        "DELETE FROM job_scene_library_settings WHERE job_id=?",
                        "DELETE FROM job_ownership WHERE job_id=?"):
            self.assertIn(snippet, part)
        block = QUEUE.split("function actionsFor(j){", 1)[1].split("\nfunction projectRow(j)", 1)[0]
        self.assertIn("if(s==='cancelled')", block)
        self.assertIn('Delete Permanently', block)
        self.assertIn('onsubmit="return confirm(', block)
        self.assertIn("Retry Drive Upload", block)
        policy = {}
        exec(compile(POLICY, "policy", "exec"), policy)
        uri = "/queue/" + "a"*32 + "/delete"
        manual_start = "/queue/" + "a"*32 + "/start"
        self.assertTrue(policy["mutation_allowed"]("POST", uri, True))
        self.assertFalse(policy["mutation_allowed"]("POST", uri, False))
        self.assertFalse(policy["mutation_allowed"]("GET", uri, True))
        self.assertFalse(policy["mutation_allowed"]("POST", "/queue/invalid/delete", True))
        self.assertTrue(policy["mutation_allowed"]("POST", manual_start, True))
        self.assertFalse(policy["mutation_allowed"]("POST", manual_start, False))
        self.assertFalse(policy["mutation_allowed"]("GET", manual_start, True))
        self.assertFalse(policy["mutation_allowed"]("POST", manual_start + "/extra", True))
        self.assertIn('@app.post("/queue/{job_id}/start")', WEB)
        self.assertIn('worker_request("POST", f"/jobs/{job_id}/start")', WEB)
        self.assertIn('require_job_access(request, job_id)\n    worker_request("POST", f"/jobs/{job_id}/start")', WEB)


if __name__ == "__main__":
    unittest.main()

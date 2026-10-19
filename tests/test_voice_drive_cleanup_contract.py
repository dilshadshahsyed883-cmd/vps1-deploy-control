"""Sandbox tests exercising the actual app-owned Voice cleanup functions.

Only one function's AST is compiled; no media engines, network or VPS accessed.
"""
import ast
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
EDGE = "apps/voice-studio/part03/edge_worker.py"
KOKORO = ("apps/voice-studio/part02/kokoro_worker.py",
          "apps/kokoro/kokoro_worker.py")


class IsolatedVoiceCleanupTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name) / "data"
        (self.data / "jobs").mkdir(parents=True)
        self.dbpath = self.data / "jobs.sqlite"
        with sqlite3.connect(self.dbpath) as c:
            c.execute("""CREATE TABLE jobs (
                job_id text,status text,delivery_status text,created_at text,
                input_file text,wav_file text,mp3_file text,srt_file text,
                drive_txt text,drive_wav text,drive_mp3 text,drive_srt text)""")

    def executor(self, file_path):
        source = (ROOT / file_path).read_text(encoding="utf-8")
        parsed = ast.parse(source, file_path)
        functions = [n for n in parsed.body if isinstance(n, ast.FunctionDef)
                     and n.name == "cleanup_delivered"]
        self.assertEqual(len(functions), 1)
        def db():
            c = sqlite3.connect(self.dbpath)
            c.row_factory = sqlite3.Row
            return c
        def update(jid, **fields):
            with db() as c:
                for key, value in fields.items():
                    c.execute("UPDATE jobs SET " + key + "=? WHERE job_id=?",
                              (value, jid))
        ns = {"Path": Path, "re": re, "shutil": shutil, "DATA": self.data,
              "db": db, "update": update}
        exec(compile(ast.Module(body=functions, type_ignores=[]), file_path, "exec"), ns)
        return ns["cleanup_delivered"]

    def record(self, edge=False, status="completed", delivery="delivered",
               remote=True, external=False, srt=False):
        jid = ("EDJ-" if edge else "KOJ-") + "A" * 16
        folder = self.data / "jobs" / jid
        folder.mkdir()
        root = Path(self.temp.name) if external else folder
        files = {key: root / (key + ".dat") for key in ("txt", "wav", "mp3")}
        for f in files.values():
            f.write_bytes(b"job-payload")
        subs = root / "srt.dat" if srt else None
        if subs: subs.write_bytes(b"captions")
        with sqlite3.connect(self.dbpath) as c:
            c.execute("""INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (
                jid, status, delivery, "2026-10-20T00:00:00",
                str(files["txt"]), str(files["wav"]), str(files["mp3"]) if edge else None,
                str(subs) if subs else None,
                "gdrive:txt" if remote else "",
                "gdrive:wav" if remote else "",
                "gdrive:mp3" if remote and edge else "",
                "gdrive:srt" if remote and srt else "",
            ))
        return jid, folder

    def delivery(self, jid):
        with sqlite3.connect(self.dbpath) as c:
            return c.execute("SELECT delivery_status FROM jobs WHERE job_id=?", (jid,)).fetchone()[0]

    def test_clean_only_verified_complete_kokoro(self):
        for path in KOKORO:
            with self.subTest(path=path):
                with sqlite3.connect(self.dbpath) as c:c.execute("DELETE FROM jobs")
                shutil.rmtree(self.data / "jobs")
                (self.data / "jobs").mkdir()
                jid, folder = self.record()
                self.assertEqual(self.executor(path)(), 1)
                self.assertFalse(folder.exists())
                self.assertEqual(self.delivery(jid), "cleaned")
                self.assertEqual(self.executor(path)(), 0)

    def test_kokoro_unverified_failed_and_external_sources_retained(self):
        for case in ({"remote": False}, {"status": "failed"}, {"delivery": "retry_pending"},
                     {"external": True}):
            with self.subTest(case=case):
                with sqlite3.connect(self.dbpath) as c:c.execute("DELETE FROM jobs")
                jid, folder = self.record(**case)
                self.assertEqual(self.executor(KOKORO[0])(), 0)
                self.assertTrue(folder.exists())
                self.assertNotEqual(self.delivery(jid), "cleaned")
                shutil.rmtree(folder)

    def test_edge_verified_clean_and_srt_receipt_required(self):
        jid, folder = self.record(edge=True, srt=True)
        self.assertEqual(self.executor(EDGE)(), 1)
        self.assertFalse(folder.exists())
        self.assertEqual(self.delivery(jid), "cleaned")

    def test_edge_missing_srt_remote_retains_payload(self):
        jid, folder = self.record(edge=True, srt=True)
        with sqlite3.connect(self.dbpath) as c:
            c.execute("UPDATE jobs SET drive_srt=NULL WHERE job_id=?", (jid,))
        self.assertEqual(self.executor(EDGE)(), 0)
        self.assertTrue(folder.exists())

    def test_edge_unverified_remote_retains_payload(self):
        _, folder = self.record(edge=True, remote=False)
        self.assertEqual(self.executor(EDGE)(), 0)
        self.assertTrue(folder.exists())

    def test_release_workflow_scoped_and_rollback_capable(self):
        source = (ROOT / ".github/workflows/vps1-app-cleanup-release.yml").read_text()
        self.assertIn("PHASE=READ_ONLY_APP_IDLE_PREFLIGHT", source)
        self.assertIn("for app in short-maker self-media-library video-merger", source)
        self.assertIn("rollback $SOURCE_SHA $app", source)
        self.assertNotIn("install $SOURCE_SHA all", source)
        self.assertIn("AUTO_SCRIPT=OFFLINE", source)


if __name__ == "__main__":
    unittest.main()

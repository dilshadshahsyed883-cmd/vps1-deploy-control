"""Isolated live acceptance contract tests: no production network or Drive use."""
from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path

from production.voice_tts_live_acceptance import (
    MARKER, insert_inert, validate_engine, check_existing_remote_receipt,
)

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "apps/voice-studio/part01/queue_actions.py"
SPEC = importlib.util.spec_from_file_location("voice_live_qa_actions", MODULE)
QA_ACTIONS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(QA_ACTIONS)


class VoiceLiveAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.files = {}
        for engine in ("kokoro", "edge"):
            path = Path(self.temp.name)/(engine+".sqlite")
            with sqlite3.connect(path) as db:
                db.execute("""CREATE TABLE jobs(
                  job_id TEXT PRIMARY KEY,batch_id TEXT NOT NULL,
                  source_filename TEXT NOT NULL,output_basename TEXT NOT NULL,
                  language TEXT,voice TEXT,speed REAL,locale TEXT,
                  rate INTEGER,pitch INTEGER,volume INTEGER,
                  status TEXT NOT NULL,delivery_status TEXT NOT NULL,
                  created_at TEXT NOT NULL,updated_at TEXT,input_file TEXT NOT NULL,
                  hidden_at TEXT,last_action TEXT,attempt INTEGER DEFAULT 0,
                  retry_at REAL DEFAULT 0,progress INTEGER DEFAULT 0,
                  drive_file_id TEXT,drive_verified_at TEXT,
                  drive_files_json TEXT DEFAULT '{}',drive_wav TEXT,drive_mp3 TEXT)""")
            self.files[engine] = path

    def http(self, db_path):
        def handler(method, path, cookie=""):
            if not cookie:
                return 401, {"detail":"unauthorized"}
            engine, job_id, action = path.split("/")[4], path.split("/")[6], path.split("/")[-1]
            with sqlite3.connect(db_path) as c:
                c.row_factory = sqlite3.Row
                try:
                    return 200, QA_ACTIONS.apply(c, engine, job_id, action)
                except QA_ACTIONS.QueueActionError as exc:
                    return exc.code, {"detail":exc.detail}
        return handler

    def test_both_engines_live_control_flow_is_disposable(self):
        for engine, path in self.files.items():
            with self.subTest(engine=engine):
                result = validate_engine(engine, path, "session=test",
                                         request=self.http(path))
                self.assertEqual(result["controls"], "PASS")
                self.assertEqual(result["anonymous_post"], "BLOCKED")
                with sqlite3.connect(path) as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)
                    self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_failed_qa_request_keeps_owner_record_and_deletes_only_test_record(self):
        path = self.files["kokoro"]
        with sqlite3.connect(path) as db:
            db.execute("""INSERT INTO jobs(job_id,batch_id,source_filename,
              output_basename,language,voice,speed,status,delivery_status,created_at,input_file)
              VALUES('KOJ-ABCDEF1234567890','owner','owner.txt','owner','en-us',
                     'af_heart',1,'completed','cleaned','2026-10-20','/owner/source')""")
        with self.assertRaisesRegex(RuntimeError,"invalid paused-to-skip"):
            validate_engine("kokoro",path,"session=test",request=lambda *args:(502,{}))
        with sqlite3.connect(path) as db:
            rows=db.execute("SELECT job_id,batch_id FROM jobs").fetchall()
        self.assertEqual(rows,[("KOJ-ABCDEF1234567890","owner")])

    def test_qa_insert_is_paused_and_has_no_media_file(self):
        for engine, path in self.files.items():
            with sqlite3.connect(path) as db:
                marker=MARKER+"fixture"
                insert_inert(db,engine,("KOJ-" if engine=="kokoro" else "EDJ-")
                             +"0000111122223333",marker)
                row=db.execute("SELECT status,delivery_status,input_file FROM jobs").fetchone()
                self.assertEqual(row[0],"paused")
                self.assertEqual(row[1],"waiting_generation")
                self.assertFalse(Path(row[2]).exists())

    def test_no_stored_receipt_is_reported_as_unverified(self):
        for engine,path in self.files.items():
            r=check_existing_remote_receipt(engine,path)
            self.assertEqual(r["remote_status"],"NO_EXISTING_VERIFIED_SAMPLE")
            self.assertEqual(r["stored_receipts"],0)

    def test_release_requires_live_proof_before_success(self):
        script=(ROOT/"production/voice_tts_controls_release.py").read_text()
        self.assertLess(script.index("verify_queues()"),script.index("qa = run_acceptance()"))
        self.assertLess(script.index("qa = run_acceptance()"),
                        script.index('atomic_json(journal / "result.json"'))
        self.assertIn('"owner_jobs_mutated":False',
                      (ROOT/"production/voice_tts_live_acceptance.py").read_text())
        self.assertIn("return {\"result\":\"PASS_VOICE_TTS_LIVE_CONTROL_QA\"",
                      (ROOT/"production/voice_tts_live_acceptance.py").read_text())


if __name__=="__main__":
    unittest.main()

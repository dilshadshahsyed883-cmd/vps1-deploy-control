"""Isolated live acceptance contract tests: no production network or Drive use."""
from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import unittest
import json
import hashlib
from types import SimpleNamespace
from unittest import mock
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

    def test_verified_drive_write_is_one_time_and_never_deletes_remote(self):
        from production import voice_tts_live_acceptance as acceptance
        journal=Path(self.temp.name)/"journal"
        journal.mkdir()
        remote={}
        sent=[]
        def fake_run(args,**kwargs):
            sent.append(args)
            if "copyto" in args:
                content=Path(args[-2]).read_bytes()
                remote[args[-1]]=content
                return SimpleNamespace(returncode=0,stdout="",stderr="")
            if "lsjson" in args and args[-1] in ("gdrive:Omni Voice/Kokoro",
                                                  "gdrive:Omni Voice/Edge TTS"):
                return SimpleNamespace(returncode=0,stdout=json.dumps({"IsDir":True}),stderr="")
            if "lsjson" in args:
                data=remote[args[-1]]
                name=args[-1].rsplit("/",1)[-1]
                body={"ID":"testproof12345678","Size":len(data),"Name":name,
                      "Hashes":{"MD5":hashlib.md5(data).hexdigest()}}
                return SimpleNamespace(returncode=0,stdout=json.dumps(body),stderr="")
            raise AssertionError("unexpected or destructive rclone operation")
        check=Path("/home/ubuntu/.config/masterctl/rclone.conf")
        original=Path.is_file
        def is_file(p):
            return p==check or original(p)
        with mock.patch.object(acceptance,"JOURNALS",journal), \
             mock.patch.object(acceptance.shutil,"which",return_value="/usr/bin/rclone"), \
             mock.patch.object(Path,"is_file",is_file), \
             mock.patch.object(acceptance.subprocess,"run",side_effect=fake_run):
            a=acceptance.drive_upload_smoke_once()
            self.assertEqual(a["result"],"PASS")
            self.assertEqual(set(a["engines"]),{"kokoro","edge"})
            self.assertEqual(len(remote),2)
            self.assertTrue(all("/_VPS1_VOICE_QA/" in key for key in remote))
            self.assertFalse(any("delete" in " ".join(cmd) for cmd in sent))
            count=len(sent)
            cached=acceptance.drive_upload_smoke_once()
            self.assertTrue(cached["cached"])
            self.assertEqual(len(sent),count)
            self.assertEqual(len(list(Path(self.temp.name).glob("vps1-voice-drive-qa-*"))),0)

    def test_failed_remote_copy_does_not_claim_drive_success(self):
        from production import voice_tts_live_acceptance as acceptance
        journal=Path(self.temp.name)/"journal-failed"
        journal.mkdir()
        check=Path("/home/ubuntu/.config/masterctl/rclone.conf")
        original=Path.is_file
        def is_file(p):
            return p==check or original(p)
        with mock.patch.object(acceptance,"JOURNALS",journal), \
             mock.patch.object(acceptance.shutil,"which",return_value="/usr/bin/rclone"), \
             mock.patch.object(Path,"is_file",is_file), \
             mock.patch.object(acceptance.subprocess,"run",
                               return_value=SimpleNamespace(returncode=1,stdout="",stderr="")):
            result=acceptance.drive_upload_smoke_once()
        self.assertEqual(result["result"],"INCOMPLETE")
        self.assertFalse((journal/"drive-qa-one-time-receipt.json").exists())
        self.assertEqual(result["google_reason"],"other")

    def test_rate_limited_receipt_stat_retries_without_recopy(self):
        from production import voice_tts_live_acceptance as acceptance
        folder=Path(self.temp.name)/"quota-check"
        folder.mkdir()
        remote={}
        attempts={"stat":0,"copy":0}
        def run(args,**kwargs):
            if "copyto" in args:
                attempts["copy"]+=1
                remote[args[-1]]=Path(args[-2]).read_bytes()
                return SimpleNamespace(returncode=0,stdout="",stderr="")
            if args[-1] in ("gdrive:Omni Voice/Kokoro","gdrive:Omni Voice/Edge TTS"):
                return SimpleNamespace(returncode=0,stdout=json.dumps({"IsDir":True}),stderr="")
            if "lsjson" in args:
                attempts["stat"]+=1
                if attempts["stat"]==1:
                    return SimpleNamespace(returncode=1,stdout="",stderr="Google API Error 403: rateLimitExceeded secret=DO_NOT_LOG")
                data=remote[args[-1]]
                return SimpleNamespace(returncode=0,stderr="",stdout=json.dumps({
                    "ID":"rate-limit-test-id", "Size":len(data),
                    "Name":args[-1].rsplit("/",1)[-1],
                    "Hashes":{"MD5":hashlib.md5(data).hexdigest()}}))
            raise AssertionError("Unexpected command")
        old=Path.is_file
        config=Path("/home/ubuntu/.config/masterctl/rclone.conf")
        with mock.patch.object(acceptance,"JOURNALS",folder), \
             mock.patch.object(acceptance.shutil,"which",return_value="/usr/bin/rclone"), \
             mock.patch.object(Path,"is_file",lambda p:p==config or old(p)), \
             mock.patch.object(acceptance.subprocess,"run",side_effect=run), \
             mock.patch.object(acceptance.time,"sleep",return_value=None) as sleep:
            result=acceptance.drive_upload_smoke_once()
        self.assertEqual(result["result"],"PASS")
        self.assertEqual(attempts["copy"],2)
        self.assertEqual(attempts["stat"],3)
        sleep.assert_called_once_with(10)

    def test_access_denial_does_not_trigger_quota_retry(self):
        from production import voice_tts_live_acceptance as acceptance
        denied=SimpleNamespace(returncode=1,stdout="",stderr="403 insufficientFilePermissions SECRET")
        with mock.patch.object(acceptance.subprocess,"run",return_value=denied) as run, \
             mock.patch.object(acceptance.time,"sleep") as sleep:
            result=acceptance.run_rclone_with_quota_backoff(["rclone","lsjson","remote"],timeout=10)
        self.assertEqual(result.returncode,1)
        run.assert_called_once()
        sleep.assert_not_called()

    def test_rclone_errors_are_redacted_into_safe_categories(self):
        from production.voice_tts_live_acceptance import classify_remote_error
        self.assertEqual(classify_remote_error("didn't find section in config file"),"remote_config")
        self.assertEqual(classify_remote_error("403 forbidden user secret:abc"),"permission")
        self.assertEqual(classify_remote_error("invalid_grant refresh token expired"),"authentication")
        self.assertEqual(classify_remote_error("429 user rate limit"),"quota")
        self.assertEqual(classify_remote_error("some unknown diagnostic"),"other_remote_error")

    def test_safe_rclone_config_does_not_report_oauth_tokens(self):
        from production.voice_tts_live_acceptance import safe_rclone_auth_facts
        config=Path(self.temp.name)/"rclone.conf"
        config.write_text("[gdrive]\ntype=drive\nscope=drive.readonly\n"
                          'token={"access_token":"never-print-this"}\n')
        data=safe_rclone_auth_facts(config)
        self.assertEqual(data,{"remote_type":"drive","auth_mode":"oauth",
                               "scope":"read_only"})
        self.assertNotIn("never-print-this",repr(data))

    def test_allowlisted_google_reasons_are_not_raw_error_text(self):
        from production.voice_tts_live_acceptance import safe_google_error_reason
        samples=[
            ("googleapi: Error 403: x access_token=VERY_SECRET (reason: storageQuotaExceeded)",
             "storageQuotaExceeded"),
            ('{"error":{"errors":[{"reason":"insufficientFilePermissions"}]}}',
             "insufficientFilePermissions"),
            ("Request had insufficient authentication scopes",
             "oauthInsufficientScope"),
            ("Google API 403 token=VERY_SECRET", "unclassified403"),
        ]
        for original,expected in samples:
            with self.subTest(expected=expected):
                result=safe_google_error_reason(original)
                self.assertEqual(result,expected)
                self.assertNotIn("VERY_SECRET",result)

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

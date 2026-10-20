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

    def test_rate_limited_receipt_retries_with_bounded_waits(self):
        from production import voice_tts_live_acceptance as acceptance
        folder=Path(self.temp.name)/"retry-journal"
        folder.mkdir()
        remote={}
        calls=[]
        def fake_run(cmd,**kwargs):
            calls.append(cmd)
            if "copyto" in cmd:
                remote[cmd[-1]]=Path(cmd[-2]).read_bytes()
                return SimpleNamespace(returncode=0,stdout="",stderr="")
            if "lsjson" in cmd:
                data=remote[cmd[-1]]
                if len([c for c in calls if "lsjson" in c])==1:
                    return SimpleNamespace(returncode=1,stdout="",
                        stderr="googleapi Error 403 rateLimitExceeded token=PRIVATE")
                return SimpleNamespace(returncode=0,
                    stdout=json.dumps({"ID":"qa-remote-123456789","Size":len(data),
                        "Name":cmd[-1].rsplit("/",1)[-1],
                        "Hashes":{"MD5":hashlib.md5(data).hexdigest()}}),
                    stderr="")
            raise AssertionError("unexpected rclone action")
        config=Path("/home/ubuntu/.config/masterctl/rclone.conf")
        real_is_file=Path.is_file
        def is_file(p):
            return p==config or real_is_file(p)
        with mock.patch.object(acceptance,"JOURNALS",folder), \
             mock.patch.object(acceptance.shutil,"which",return_value="/usr/bin/rclone"), \
             mock.patch.object(Path,"is_file",is_file), \
             mock.patch.object(acceptance.subprocess,"run",side_effect=fake_run), \
             mock.patch.object(acceptance.time,"sleep") as sleeper:
            result=acceptance.drive_upload_smoke_once()
        self.assertEqual(result["result"],"PASS")
        self.assertEqual(sleeper.call_args_list,[mock.call(6)])
        self.assertEqual(len([c for c in calls if "copyto" in c]),2)
        self.assertEqual(len(remote),2)

    def test_receipt_failure_reuses_nonce_without_duplicate_copy(self):
        from production import voice_tts_live_acceptance as acceptance
        folder=Path(self.temp.name)/"resume-journal"
        folder.mkdir()
        remote={}
        copied=[]
        fail_reads=True
        def fake_run(cmd,**kwargs):
            if "copyto" in cmd:
                copied.append(cmd[-1])
                remote[cmd[-1]]=Path(cmd[-2]).read_bytes()
                return SimpleNamespace(returncode=0,stdout="",stderr="")
            if "lsjson" in cmd:
                if fail_reads:
                    return SimpleNamespace(returncode=1,stdout="",
                        stderr="googleapi Error 403 rateLimitExceeded")
                data=remote[cmd[-1]]
                return SimpleNamespace(returncode=0,
                    stdout=json.dumps({"ID":"qa-remote-123456789","Size":len(data),
                        "Name":cmd[-1].rsplit("/",1)[-1],
                        "Hashes":{"MD5":hashlib.md5(data).hexdigest()}}),
                    stderr="")
            raise AssertionError("unexpected Drive request")
        config=Path("/home/ubuntu/.config/masterctl/rclone.conf")
        real_is_file=Path.is_file
        def is_file(p):
            return p==config or real_is_file(p)
        with mock.patch.object(acceptance,"JOURNALS",folder), \
             mock.patch.object(acceptance.shutil,"which",return_value="/usr/bin/rclone"), \
             mock.patch.object(Path,"is_file",is_file), \
             mock.patch.object(acceptance.subprocess,"run",side_effect=fake_run), \
             mock.patch.object(acceptance.time,"sleep"):
            first=acceptance.drive_upload_smoke_once()
            self.assertEqual(first["result"],"INCOMPLETE")
            self.assertEqual(first["google_reason"],"rateLimitExceeded")
            self.assertEqual(len(copied),1)
            partial=json.loads((folder/"drive-qa-progress.json").read_text())
            self.assertTrue(partial["items"]["kokoro"]["copied"])
            self.assertFalse(partial["items"]["kokoro"]["verified"])
            fail_reads=False
            second=acceptance.drive_upload_smoke_once()
            self.assertEqual(second["result"],"PASS")
            self.assertEqual(len(copied),2)
            self.assertEqual(copied[0],next(k for k in remote if "/Kokoro/" in k))
            third=acceptance.drive_upload_smoke_once()
            self.assertTrue(third["cached"])
            self.assertEqual(len(copied),2)

    def test_google_403_permission_does_not_retry(self):
        from production import voice_tts_live_acceptance as acceptance
        bad=SimpleNamespace(returncode=1,stdout="",
             stderr="Google API 403: insufficientFilePermissions")
        with mock.patch.object(acceptance.subprocess,"run",return_value=bad) as runner, \
             mock.patch.object(acceptance.time,"sleep") as sleeper:
            result=acceptance.qa_rclone_command(["rclone","lsjson","some-file"])
        self.assertEqual(result.returncode,1)
        runner.assert_called_once()
        sleeper.assert_not_called()

    def test_authenticated_synthetic_audio_verifies_filenames_and_drive_links_once(self):
        from production import voice_tts_live_acceptance as acceptance
        journal=Path(self.temp.name)/"real-audio-journal"
        journal.mkdir()
        jobs={}
        operations=[]
        def request(method,path,cookie="",payload=None):
            operations.append((method,path))
            self.assertEqual(cookie,"session=qa-test")
            engine="kokoro" if "/kokoro/" in path else "edge"
            if path.endswith("/voices"):
                field="language" if engine=="kokoro" else "locale"
                return 200,{"voices":[{"id":"af_heart" if engine=="kokoro" else "en-US-Test",
                                        field:"en-us" if engine=="kokoro" else "en-US"}]}
            if path.endswith("/generate"):
                self.assertEqual(method,"POST")
                self.assertTrue(payload["output_name"].startswith("VPS1_QA_"+engine+"_"))
                jid=("KOJ-" if engine=="kokoro" else "EDJ-")+("A"*16 if engine=="kokoro" else "B"*16)
                jobs[jid]={"output":payload["output_name"],"engine":engine}
                return 200,{"job_id":jid}
            if path.endswith("/jobs?limit=250"):
                rows=[]
                for jid,j in jobs.items():
                    if j["engine"]!=engine:
                        continue
                    name=j["output"]
                    extensions=("wav","txt") if engine=="kokoro" else ("mp3","wav","txt")
                    remote={ext:{"id":"testdrive-id-"+ext,"name":name+"."+ext,
                                  "url":"https://drive.google.com/file/d/testdrive-id-"+ext+"/view"}
                            for ext in extensions}
                    primary="wav" if engine=="kokoro" else "mp3"
                    rows.append({"job_id":jid,"status":"completed","delivery_status":"cleaned",
                                 "naming_method":"custom",
                                 "output_filename":name+"."+primary,
                                 "drive_verified_at":"2026-10-20T18:00:00Z",
                                 "drive_file_id":remote[primary]["id"],
                                 "drive_url":remote[primary]["url"],
                                 "drive_files":remote})
                return 200,{"jobs":rows}
            if path.endswith("/delete") and method=="POST":
                return 200,{"status":"hidden"}
            raise AssertionError("Unexpected audio QA route: "+path)
        with mock.patch.object(acceptance,"JOURNALS",journal):
            first=acceptance.audio_output_smoke_once("session=qa-test",request=request)
            self.assertEqual(first["result"],"PASS")
            self.assertEqual(first["engines"],{"kokoro":"PASS","edge":"PASS"})
            self.assertEqual(len([o for o in operations if o[1].endswith("/generate")]),2)
            old_count=len(operations)
            second=acceptance.audio_output_smoke_once("session=qa-test",request=request)
            self.assertEqual(second["result"],"PASS")
            self.assertEqual(second["engines"],{"kokoro":"PASS_CACHED","edge":"PASS_CACHED"})
            self.assertEqual(len(operations),old_count)
            self.assertTrue((journal/"one-time-generated-audio-qa.json").exists())

    def test_failed_voice_catalog_does_not_create_or_modify_a_job(self):
        from production import voice_tts_live_acceptance as acceptance
        journal=Path(self.temp.name)/"unavailable-voice-catalog"
        journal.mkdir()
        calls=[]
        def unavailable(method,path,cookie="",payload=None):
            calls.append((method,path))
            return 503,{}
        with mock.patch.object(acceptance,"JOURNALS",journal):
            result=acceptance.audio_output_smoke_once("session=qa-test",request=unavailable)
        self.assertEqual(result["result"],"INCOMPLETE")
        self.assertTrue(all(x[0]=="GET" for x in calls))
        self.assertTrue(result["owner_jobs_mutated"] is False)

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

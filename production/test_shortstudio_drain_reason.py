"""Reason-coded, secret-free read-only Short Studio drain evidence."""
import json
import tempfile
import unittest
from pathlib import Path

from production.shortstudio_cooperation_readonly import (
    SERVICES, inspect_active_drain,
)


class ReadonlyBlockedReasonTests(unittest.TestCase):
    def test_missing_host_files_is_unknown_not_idle(self):
        with tempfile.TemporaryDirectory() as path:
            result=inspect_active_drain(Path(path))
            self.assertEqual(result["mode"],"READ_ONLY")
            self.assertEqual(result["status"],"BLOCKED")
            self.assertEqual(result["blocked_at"],"INSPECTION_UNREADABLE")
            self.assertFalse(result["active_upload_drain_verified"])

    def test_nonterminal_maker_job_is_opaque_blocker(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            unit_dir=root/"units"
            unit_dir.mkdir()
            maker=root/"private-maker"
            jobs=maker/"jobs"/"PRIVATE_JOB_NAME_DO_NOT_PRINT"
            jobs.mkdir(parents=True)
            state=jobs/"state.json"
            state.write_text(json.dumps({
                "status":"queued", "owner":"SENSITIVE_OWNER_DO_NOT_PRINT"}))
            env=root/"secret.env"
            env.write_text(
                "SHORT_MAKER_DATA="+str(maker)+"\n"
                "SHORT_UPLOADER_APP_DB="+str(root/"missing-app.sqlite3")+"\n"
                "SHORT_UPLOADER_ENGINE_DB="+str(root/"missing-v6.sqlite3")+"\n")
            (unit_dir/SERVICES["maker"]).write_text(
                "[Service]\nEnvironmentFile="+str(env)+"\n")
            first=inspect_active_drain(unit_dir)
            self.assertEqual(first["blocked_at"],"MAKER_JOB_NOT_TERMINAL")
            self.assertFalse(first["maker_jobs_idle"])
            self.assertNotIn("SENSITIVE_OWNER",str(first))
            self.assertNotIn("PRIVATE_JOB_NAME",str(first))
            state.write_text('{"status":"completed"}')
            second=inspect_active_drain(unit_dir)
            self.assertTrue(second["maker_jobs_idle"])
            self.assertEqual(second["blocked_at"],"UPLOADER_APP_DB_UNAVAILABLE")
            self.assertFalse(second["active_upload_drain_snapshot_verified"])

    def test_workflow_reason_is_static_allowlisted_and_no_raw_payload(self):
        source=(Path(__file__).resolve().parents[1]/
                ".github/workflows/vps1-short-studio-release.yml").read_text()
        self.assertIn("blocked_at=snapshot.get('blocked_at','UNKNOWN')",source)
        self.assertIn("'drain_blocked_at':blocked_at",source)
        self.assertIn("if blocked_at not in allowed:",source)
        self.assertIn("READONLY_DRAIN_REASON_INVALID",source)
        self.assertNotIn("print(snapshot)",source)
        self.assertNotIn("print(config)",source)
        self.assertNotIn("print(data)",source)
        self.assertIn("'active_upload_drain_verified':False",source)
        self.assertIn("'retirement_enabled':False",source)
        self.assertIn("'deployment_performed':False",source)


if __name__=="__main__":
    unittest.main()

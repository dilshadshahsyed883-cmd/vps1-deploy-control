"""Reason-coded, secret-free read-only Short Studio drain evidence."""
import json
import sqlite3
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

    def test_known_maker_control_lock_does_not_masquerade_as_job(self):
        # Short Maker upload_create_lock uses precisely this regular file.
        # Unknown files and symlinks must still prevent an idle claim.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            unit_dir = root / "units"
            unit_dir.mkdir()
            maker = root / "private-maker"
            job = maker / "jobs" / "PRIVATE_JOB_DO_NOT_PRINT"
            job.mkdir(parents=True)
            (job / "state.json").write_text('{"status":"completed"}')
            lock = maker / "jobs" / ".resumable-create.lock"
            lock.touch()
            env = root / "secret.env"
            env.write_text(
                "SHORT_MAKER_DATA=" + str(maker) + "\n"
                "SHORT_UPLOADER_APP_DB=" + str(root / "missing-app.sqlite3") + "\n"
                "SHORT_UPLOADER_ENGINE_DB=" + str(root / "missing-v6.sqlite3") + "\n"
            )
            (unit_dir / SERVICES["maker"]).write_text(
                "[Service]\nEnvironmentFile=" + str(env) + "\n"
            )
            clean = inspect_active_drain(unit_dir)
            self.assertTrue(clean["maker_jobs_idle"])
            self.assertEqual(clean["blocked_at"], "UPLOADER_APP_DB_UNAVAILABLE")
            self.assertFalse(clean["active_upload_drain_verified"])
            self.assertNotIn("PRIVATE_JOB", str(clean))
            unknown = maker / "jobs" / ".unknown-file"
            unknown.touch()
            unsafe = inspect_active_drain(unit_dir)
            self.assertEqual(unsafe["blocked_at"], "MAKER_JOB_ENTRY_UNSAFE")
            self.assertFalse(unsafe["maker_jobs_idle"])
            unknown.unlink()
            lock.unlink()
            lock.symlink_to(job / "state.json")
            linked = inspect_active_drain(unit_dir)
            self.assertEqual(linked["blocked_at"], "MAKER_JOB_ENTRY_UNSAFE")
            self.assertFalse(linked["maker_jobs_idle"])

    def test_published_is_terminal_and_pending_state_output_is_sanitized(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            units = root / "units"
            units.mkdir()
            maker = root / "maker"
            (maker / "jobs").mkdir(parents=True)
            app = root / "app.sqlite"
            engine = root / "engine.sqlite"
            with sqlite3.connect(app) as db:
                db.execute("CREATE TABLE videos (status TEXT NOT NULL)")
                db.execute("INSERT INTO videos VALUES ('PUBLISHED')")
            with sqlite3.connect(engine) as db:
                db.execute("CREATE TABLE job_sources (state TEXT NOT NULL)")
            env = root / "private.env"
            env.write_text(
                "SHORT_MAKER_DATA=" + str(maker) + "\n"
                "SHORT_UPLOADER_APP_DB=" + str(app) + "\n"
                "SHORT_UPLOADER_ENGINE_DB=" + str(engine) + "\n"
            )
            (units / SERVICES["maker"]).write_text(
                "[Service]\nEnvironmentFile=" + str(env) + "\n"
            )
            finished = inspect_active_drain(units)
            self.assertTrue(finished["uploader_jobs_idle"])
            self.assertEqual(finished["blocked_at"], "NONE")
            self.assertEqual(finished["uploader_job_nonterminal_categories"], [])
            self.assertFalse(finished["active_upload_drain_verified"])
            with sqlite3.connect(app) as db:
                db.execute("INSERT INTO videos VALUES ('QUEUED')")
                db.execute("INSERT INTO videos VALUES ('OWNER_PRIVATE_UNKNOWN_STATE')")
                db.execute("INSERT INTO videos VALUES ('WAITING_NETWORK')")
            blocked = inspect_active_drain(units)
            self.assertEqual(blocked["blocked_at"], "UPLOADER_JOB_NOT_TERMINAL")
            self.assertEqual(blocked["uploader_job_nonterminal_categories"],
                             ["QUEUED", "UNKNOWN", "WAITING_NETWORK"])
            self.assertFalse(blocked["uploader_jobs_idle"])
            self.assertFalse(blocked["active_upload_drain_snapshot_verified"])
            self.assertNotIn("OWNER_PRIVATE_UNKNOWN_STATE", str(blocked))

    def test_workflow_reason_is_static_allowlisted_and_no_raw_payload(self):
        source=(Path(__file__).resolve().parents[1]/
                ".github/workflows/vps1-short-studio-release.yml").read_text()
        self.assertIn("blocked_at=snapshot.get('blocked_at','UNKNOWN')",source)
        self.assertIn("'drain_blocked_at':blocked_at",source)
        self.assertIn("if blocked_at not in allowed:",source)
        self.assertIn("READONLY_DRAIN_REASON_INVALID",source)
        self.assertIn("READONLY_JOB_CATEGORIES_INVALID",source)
        self.assertNotIn("print(snapshot)",source)
        self.assertNotIn("print(config)",source)
        self.assertNotIn("print(data)",source)
        self.assertIn("'active_upload_drain_verified':False",source)
        self.assertIn("'retirement_enabled':False",source)
        self.assertIn("'deployment_performed':False",source)


if __name__=="__main__":
    unittest.main()

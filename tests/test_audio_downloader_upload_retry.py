"""Regression: 60-second Drive spacing and automatic 180-second upload-only retry."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "apps/voice-studio/part01/audio_downloader_api.py"
VIDEO = "BaW_jenozKc"
MP3 = b"ID3" + b"x" * 2048
JPG = b"\\xff\\xd8\\xff" + b"x" * 2048


class DriveUploadSchedulingTests(unittest.TestCase):
    def setUp(self):
        try:
            import fastapi  # noqa: F401 - optional in the broad offline pilot
        except ImportError:
            self.skipTest("FastAPI dependency is provided by dedicated Audio Downloader CI")
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"AUDIO_DOWNLOADER_ROOT": self.tmp.name})
        self.env.start()
        spec = importlib.util.spec_from_file_location("audio_upload_pacing_test", MODULE)
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)
        self.m.init_storage()
        self.jid = "ADJ-" + "C" * 16
        self.local_dir = self.m.JOBS / self.jid
        self.local_dir.mkdir(parents=True)
        self.local = self.local_dir / (VIDEO + ".mp3")
        self.local.write_bytes(MP3)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def create_job(self, status="uploading", thumbnail=False):
        with self.m.db() as con:
            con.execute("""INSERT INTO jobs
                (job_id,batch_id,video_id,url,project,folder,format,quality,
                 auto_upload,keep_metadata,download_thumbnail,status,local_file,
                 local_thumbnail,created_at,updated_at)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (self.jid, "ADB-TEST", VIDEO, "https://youtu.be/" + VIDEO,
                 "", "", "mp3", 192, 1, 0, int(thumbnail),
                 status, str(self.local),
                 str(self.local_dir / (VIDEO + ".jpg")) if thumbnail else None,
                 self.m.utc(), self.m.utc()))

    def job(self):
        with self.m.db() as con:
            return dict(con.execute("SELECT * FROM jobs WHERE job_id=?", (self.jid,)).fetchone())

    def test_additive_migration_and_reported_policy(self):
        with self.m.db() as con:
            columns = {r[1] for r in con.execute("PRAGMA table_info(jobs)")}
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"next_retry_at", "upload_retry_count"}.issubset(columns))
        self.assertIn("audio_upload_pacing", tables)
        with patch.object(self.m, "deps", return_value={"ready": True, "upload_ready": True}):
            response = self.m.status()
        self.assertEqual(response["upload_gap_seconds"], 60)
        self.assertEqual(response["upload_retry_seconds"], 180)

    def test_failed_upload_is_retried_after_three_minutes_without_redownloading(self):
        self.create_job()
        before = datetime.now(timezone.utc)
        self.m._schedule_upload_retry(self.jid, RuntimeError("temporary Drive failure"))
        data = self.job()
        self.assertEqual(data["status"], "upload_retry")
        self.assertEqual(data["upload_retry_count"], 1)
        eta = datetime.fromisoformat(data["next_retry_at"])
        self.assertGreaterEqual((eta - before).total_seconds(), 179)
        self.assertLessEqual((eta - before).total_seconds(), 182)
        self.assertIsNone(self.m._claim())
        with self.m.db() as con:
            con.execute("UPDATE jobs SET next_retry_at=? WHERE job_id=?",
                        (self.m.utc_after(-1), self.jid))
        claimed = self.m._claim()
        self.assertTrue(claimed["_resume_upload"])
        self.assertEqual(self.job()["status"], "uploading")
        with patch.object(self.m, "deps", return_value={"upload_ready": False}):
            with self.assertRaisesRegex(RuntimeError, "connection unavailable"):
                self.m.resume_upload(claimed)
        self.m._schedule_upload_retry(self.jid, RuntimeError("Drive offline"))
        self.assertEqual(self.job()["upload_retry_count"], 2)
        self.assertTrue(self.local.is_file())

    def test_recover_interrupted_upload_but_not_interrupted_youtube_download(self):
        self.create_job()
        self.m.recover_interrupted_jobs()
        self.assertEqual(self.job()["status"], "upload_retry")
        self.assertTrue(self.job()["next_retry_at"])
        self.assertTrue(self.local.exists())
        with self.m.db() as con:
            con.execute("UPDATE jobs SET status='downloading' WHERE job_id=?", (self.jid,))
        self.m.recover_interrupted_jobs()
        self.assertEqual(self.job()["status"], "failed")

    def test_preexisting_failed_drive_upload_is_automatically_recovered(self):
        self.create_job(status="failed")
        self.m.recover_interrupted_jobs()
        self.assertEqual(self.job()["status"], "upload_retry")
        self.assertIsNotNone(self.job()["next_retry_at"])
        self.assertTrue(self.local.exists())

    def test_successful_audio_sets_60_second_pacing_and_waits_before_next(self):
        self.m._record_audio_upload()
        with self.m.db() as con:
            next_time = con.execute("SELECT next_upload_at FROM audio_upload_pacing WHERE id=1").fetchone()[0]
        remaining = (datetime.fromisoformat(next_time) - datetime.now(timezone.utc)).total_seconds()
        self.assertGreaterEqual(remaining, 58)
        self.assertLessEqual(remaining, 61)
        slept = []
        def advance(seconds):
            slept.append(seconds)
            with self.m.db() as con:
                con.execute("UPDATE audio_upload_pacing SET next_upload_at=? WHERE id=1", (self.m.utc_after(-1),))
        with patch.object(self.m.time, "sleep", side_effect=advance):
            self.m._wait_for_audio_upload_slot()
        self.assertTrue(slept)
        self.assertGreater(slept[0], 0)

    def test_thumbnail_failure_resumes_without_reuploading_audio_or_redownloading(self):
        self.create_job(thumbnail=True)
        thumbnail = self.local_dir / (VIDEO + ".jpg")
        thumbnail.write_bytes(JPG)
        attempts = []
        fail_thumb_once = [True]
        def subprocess_run(args, **kwargs):
            if "yt-dlp" in repr(args):
                self.fail("The retry attempted to download from YouTube")
            if "copyto" in args:
                dest = args[args.index("copyto") + 2]
                attempts.append(dest)
                if dest.endswith(".jpg") and fail_thumb_once[0]:
                    fail_thumb_once[0] = False
                    return subprocess.CompletedProcess(args, 1, "", "transient")
                return subprocess.CompletedProcess(args, 0, "", "")
            if "lsjson" in args:
                dest = args[-1]
                return subprocess.CompletedProcess(args, 0,
                    json.dumps({"Size": len(JPG) if dest.endswith(".jpg") else len(MP3)}), "")
            self.fail("Unexpected command: " + repr(args))
        state = {"upload_ready": True, "rclone": "/fake/rclone"}
        with patch.object(self.m, "deps", return_value=state), \
             patch.object(self.m.subprocess, "run", side_effect=subprocess_run), \
             patch.object(self.m, "verified_drive_public_link",
                          return_value="https://drive.google.com/file/d/123456789012/view?usp=sharing"):
            with self.assertRaisesRegex(RuntimeError, "automatic retry"):
                self.m.resume_upload(self.job())
            self.m._schedule_upload_retry(self.jid, RuntimeError("temporary"))
            row = self.job()
            self.assertEqual(row["status"], "upload_retry")
            self.assertTrue(row["drive_path"].endswith(".mp3"))
            self.assertTrue(row["drive_url"])
            self.assertTrue(self.local.exists())
            with self.m.db() as con:
                con.execute("UPDATE jobs SET next_retry_at=? WHERE job_id=?",
                            (self.m.utc_after(-1), self.jid))
            claimed = self.m._claim()
            self.m.resume_upload(claimed)
        self.assertEqual(self.job()["status"], "delivered")
        self.assertEqual(self.job()["upload_retry_count"], 1)
        self.assertIsNone(self.job()["next_retry_at"])
        self.assertFalse(self.local.exists())
        self.assertFalse(thumbnail.exists())
        self.assertEqual(len([x for x in attempts if x.endswith(".mp3")]), 1)
        self.assertEqual(len([x for x in attempts if x.endswith(".jpg")]), 2)


    def test_streamed_download_and_drive_progress_persists_without_saving_logs(self):
        self.create_job(status="downloading")
        class Process:
            returncode = 0
            def __init__(self, lines):
                self.stdout = iter(lines)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def wait(self, timeout=None):
                return 0
        with patch.object(self.m.subprocess, "Popen",
                          return_value=Process(["AD_PROGRESS: 10.0%\\n", "AD_PROGRESS: 73.4%\\n",
                                                "AD_PROGRESS: 100.0%\\n"])):
            result = self.m._run_tracked(["yt-dlp"], jid=self.jid, phase="download")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.job()["download_percent"], 99)
        self.assertNotIn("AD_PROGRESS", str(self.job()))
        with patch.object(self.m.subprocess, "Popen",
                          return_value=Process(["Transferred: 1 MiB / 10 MiB, 10%, 2 MiB/s\\n",
                                                "Transferred: 10 MiB / 10 MiB, 100%, 2 MiB/s\\n"])):
            self.m._run_tracked(["rclone"], jid=self.jid, phase="upload")
        self.assertEqual(self.job()["upload_percent"], 99)

    def test_manual_failed_upload_reuses_preserved_local_file(self):
        self.create_job(status="failed")
        response = self.m.job_action(self.jid, "retry")
        self.assertTrue(response["ok"])
        self.assertEqual(self.job()["status"], "upload_retry")
        self.assertTrue(self.local.exists())
        claimed = self.m._claim()
        self.assertTrue(claimed["_resume_upload"])

    def test_progress_contract_is_additive_and_survives_restart(self):
        self.create_job(status="downloading")
        self.m._update(self.jid, "uploading", download_percent=100, upload_percent=72,
                       progress_stage="uploading audio")
        row = self.m.sanitized_row(self.m.row_for(self.jid))
        self.assertEqual((row["download_percent"], row["upload_percent"]), (100, 72))
        self.assertEqual(row["progress_stage"], "uploading audio")
        self.m.init_storage()
        self.assertEqual(self.job()["upload_percent"], 72)


    def test_provider_errors_are_categorized_without_exposing_raw_logs(self):
        cases = {
            'Error: userRateLimitExceeded 403 secret=abc': 'RATE_LIMITED',
            'Error: storageQuotaExceeded': 'STORAGE_FULL',
            'Error: invalid_grant oauth_secret': 'AUTH_REQUIRED',
            'Error: immutable file already exists': 'DESTINATION_EXISTS',
            'Error: insufficientFilePermissions': 'PERMISSION_DENIED',
            'Error: connection reset by peer': 'NETWORK_FAILURE',
            'some unrecognized private error': 'TRANSFER_FAILED',
        }
        for raw, expected in cases.items():
            self.assertEqual(self.m._drive_copy_failure_code(raw), expected)
            self.assertNotIn('secret', self.m._drive_copy_failure_code(raw).lower())

    def test_existing_immutable_remote_is_reconciled_only_after_full_verification(self):
        self.create_job(status='downloaded')
        state = {'upload_ready': True, 'rclone': '/fake/rclone', 'progress_streaming': False}
        def copy_exists(args, **kwargs):
            self.assertIn('copyto', args)
            self.assertIn('--immutable', args)
            return subprocess.CompletedProcess(args, 1, '', 'failed: immutable file already exists')
        with patch.object(self.m, 'subprocess') as subprocess_mock, \
             patch.object(self.m, 'verified_drive_public_link',
                          return_value='https://drive.google.com/file/d/123456789012/view?usp=sharing') as verify:
            subprocess_mock.run.side_effect = copy_exists
            self.m.upload_downloaded(self.job(), self.local, None, state)
        verify.assert_called_once()
        self.assertEqual(self.job()['status'], 'delivered')
        self.assertTrue(self.job()['drive_url'])
        self.assertFalse(self.local.exists())

    def test_immutable_conflict_mismatch_never_marks_delivered_or_deletes_audio(self):
        self.create_job(status='downloaded')
        state = {'upload_ready': True, 'rclone': '/fake/rclone', 'progress_streaming': False}
        def copy_exists(args, **kwargs):
            return subprocess.CompletedProcess(args, 1, '', 'immutable file already exists')
        with patch.object(self.m, 'subprocess') as subprocess_mock, \
             patch.object(self.m, 'verified_drive_public_link',
                          side_effect=RuntimeError('Google Drive content hash mismatch')):
            subprocess_mock.run.side_effect = copy_exists
            with self.assertRaisesRegex(RuntimeError, 'DESTINATION_CONFLICT_UNVERIFIED'):
                self.m.upload_downloaded(self.job(), self.local, None, state)
        self.assertTrue(self.local.exists())
        self.assertNotEqual(self.job()['status'], 'delivered')
        self.m._schedule_upload_retry(self.jid, RuntimeError(
            'Google Drive upload failed (DESTINATION_CONFLICT_UNVERIFIED); automatic retry scheduled'))
        self.assertEqual(self.job()['status'], 'upload_retry')
        self.assertEqual(self.job()['upload_retry_count'], 1)
        self.assertTrue(self.local.exists())


if __name__ == "__main__":
    unittest.main()

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
        self.assertTrue({"next_retry_at", "upload_retry_count", "thumbnail_upload_after"}.issubset(columns))
        self.assertIn("audio_upload_pacing", tables)
        with patch.object(self.m, "deps", return_value={"ready": True, "upload_ready": True}):
            response = self.m.status()
        self.assertEqual(response["upload_gap_seconds"], 60)
        self.assertEqual(response["thumbnail_gap_seconds"], 30)
        self.assertEqual(response["upload_retry_seconds"], 180)

    def test_failed_upload_is_retried_after_three_minutes_without_redownloading(self):
        self.create_job()
        before = datetime.now(timezone.utc)
        self.m._schedule_upload_retry(self.jid, self.m.DriveUploadFailure("RATE_LIMITED"))
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
        self.m._schedule_upload_retry(self.jid, self.m.DriveUploadFailure("RATE_LIMITED"))
        self.assertEqual(self.job()["upload_retry_count"], 2)
        self.assertTrue(self.local.is_file())

    def test_recover_interrupted_upload_but_not_interrupted_youtube_download(self):
        self.create_job()
        self.m.recover_interrupted_jobs()
        self.assertEqual(self.job()["status"], "failed")
        self.assertIsNone(self.job()["next_retry_at"])
        self.assertTrue(self.local.exists())
        with self.m.db() as con:
            con.execute("UPDATE jobs SET status='downloading' WHERE job_id=?", (self.jid,))
        self.m.recover_interrupted_jobs()
        self.assertEqual(self.job()["status"], "failed")

    def test_preexisting_failed_drive_upload_remains_manual_after_restart(self):
        self.create_job(status="failed")
        with self.m.db() as con:
            con.execute("UPDATE jobs SET error=? WHERE job_id=?",
                        ("Drive anonymous access returned unexpected content type", self.jid))
        self.m.recover_interrupted_jobs()
        self.assertEqual(self.job()["status"], "failed")
        self.assertIsNone(self.job()["next_retry_at"])
        self.assertIn("unexpected content type", self.job()["error"])
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
                    return subprocess.CompletedProcess(args, 1, "", "userRateLimitExceeded (403)")
                return subprocess.CompletedProcess(args, 0, "", "")
            self.fail("Unexpected command: " + repr(args))
        state = {"upload_ready": True, "rclone": "/fake/rclone"}
        with patch.object(self.m, "deps", return_value=state), \
             patch.object(self.m.subprocess, "run", side_effect=subprocess_run), \
             patch.object(self.m, "_wait_for_thumbnail_upload") as delay:
            with self.assertRaisesRegex(RuntimeError, "automatic retry"):
                self.m.resume_upload(self.job())
            self.m._schedule_upload_retry(self.jid, self.m.DriveUploadFailure("RATE_LIMITED"))
            row = self.job()
            self.assertEqual(row["status"], "upload_retry")
            self.assertTrue(row["drive_path"].endswith(".mp3"))
            self.assertIsNone(row["drive_url"])
            self.assertTrue(self.local.exists())
            with self.m.db() as con:
                con.execute("UPDATE jobs SET next_retry_at=? WHERE job_id=?",
                            (self.m.utc_after(-1), self.jid))
            claimed = self.m._claim()
            self.m.resume_upload(claimed)
        self.assertEqual(delay.call_count, 2)
        self.assertEqual(self.job()["status"], "delivered")
        self.assertEqual(self.job()["upload_retry_count"], 1)
        self.assertIsNone(self.job()["next_retry_at"])
        self.assertTrue(self.local.exists())
        self.assertTrue(thumbnail.exists())
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

    def test_thumbnail_waits_30_seconds_after_audio_and_schedule_survives_restart(self):
        self.create_job(status='downloaded', thumbnail=True)
        thumbnail = self.local_dir / (VIDEO + '.jpg')
        thumbnail.write_bytes(JPG)
        state = {'upload_ready': True, 'rclone': '/fake/rclone', 'progress_streaming': False}
        uploads = []
        waits = []
        def transfer(args, **kwargs):
            self.assertIn('copyto', args)
            uploads.append(args[args.index('copyto') + 2])
            return subprocess.CompletedProcess(args, 0, '', '')
        def advance(seconds):
            waits.append(seconds)
            self.assertEqual(len(uploads), 1, 'Photo started before audio-only transfer finished')
            with self.m.db() as con:
                due = con.execute('SELECT thumbnail_upload_after FROM jobs WHERE job_id=?',
                                  (self.jid,)).fetchone()[0]
                self.assertIsNotNone(due)
                con.execute('UPDATE jobs SET thumbnail_upload_after=? WHERE job_id=?',
                            (self.m.utc_after(-1), self.jid))
        with patch.object(self.m.subprocess, 'run', side_effect=transfer), \
             patch.object(self.m.time, 'sleep', side_effect=advance), \
             patch.object(self.m, 'verified_drive_public_link',
                          side_effect=AssertionError('No verification requests')):
            self.m.upload_downloaded(self.job(), self.local, thumbnail, state)
        self.assertEqual(len(uploads), 2)
        self.assertEqual([x.split('.')[-1] for x in uploads], ['mp3', 'jpg'])
        self.assertTrue(waits, 'Thumbnail transfer must wait for the durable 30s gap')
        self.assertGreater(waits[0], 0)
        self.assertIsNone(self.job()['thumbnail_upload_after'])

    def test_persisted_thumbnail_schedule_blocks_resume_after_restart(self):
        self.create_job(status='upload_retry', thumbnail=True)
        thumbnail = self.local_dir / (VIDEO + '.jpg')
        thumbnail.write_bytes(JPG)
        remote = 'gdrive:Song Studio/Audio downloader/' + VIDEO + '.mp3'
        with self.m.db() as con:
            con.execute('UPDATE jobs SET drive_path=?,thumbnail_upload_after=? WHERE job_id=?',
                        (remote, self.m.utc_after(30), self.jid))
        self.m.init_storage()  # Simulate a process restarting and reopening SQLite.
        uploads = []
        def transfer(args, **kwargs):
            uploads.append(args[args.index('copyto') + 2])
            return subprocess.CompletedProcess(args, 0, '', '')
        def advance(seconds):
            self.assertFalse(uploads, 'Thumbnail uploaded before its wait elapsed')
            with self.m.db() as con:
                con.execute('UPDATE jobs SET thumbnail_upload_after=? WHERE job_id=?',
                            (self.m.utc_after(-1), self.jid))
        state = {'upload_ready': True, 'rclone': '/fake/rclone', 'progress_streaming': False}
        with patch.object(self.m.subprocess, 'run', side_effect=transfer), \
             patch.object(self.m.time, 'sleep', side_effect=advance):
            self.m.upload_downloaded(self.job(), self.local, thumbnail, state)
        self.assertEqual(len(uploads), 1)
        self.assertTrue(uploads[0].endswith('.jpg'))
        self.assertTrue(self.local.exists())


    def test_two_upload_operations_only_and_no_remote_probes(self):
        self.create_job(status='downloaded', thumbnail=True)
        thumbnail = self.local_dir / (VIDEO + ".jpg")
        thumbnail.write_bytes(JPG)
        commands = []
        def upload(args, **kwargs):
            self.assertIn('copyto', args, 'No lsjson/md5sum/link/permission verification')
            commands.append(args)
            return subprocess.CompletedProcess(args, 0, '', '')
        state = {'upload_ready': True, 'rclone': '/fake/rclone', 'progress_streaming': False}
        with patch.object(self.m.subprocess, 'run', side_effect=upload), \
             patch.object(self.m, '_wait_for_thumbnail_upload') as wait, \
             patch.object(self.m, 'verified_drive_public_link',
                          side_effect=AssertionError('Remote verification must not run')):
            self.m.upload_downloaded(self.job(), self.local, thumbnail, state)
        wait.assert_called_once_with(self.jid)
        self.assertEqual(len(commands), 2)
        self.assertEqual([c[c.index('copyto') + 2].split('.')[-1] for c in commands], ['mp3', 'jpg'])
        row = self.job()
        self.assertEqual(row['status'], 'delivered')
        self.assertIsNone(row['drive_url'])
        self.assertIsNone(row['thumbnail_drive_url'])
        self.assertTrue(row['drive_path'].endswith('.mp3'))
        self.assertTrue(row['thumbnail_drive_path'].endswith('.jpg'))
        self.assertIn('not independently verified', row['progress_stage'])
        self.assertTrue(self.local.exists())
        self.assertTrue(thumbnail.exists())

    def test_audio_only_uses_one_copy_operation(self):
        self.create_job(status='downloaded')
        state = {'upload_ready': True, 'rclone': '/fake/rclone', 'progress_streaming': False}
        with patch.object(self.m.subprocess, 'run',
                          return_value=subprocess.CompletedProcess(['rclone'], 0, '', '')) as run, \
             patch.object(self.m, 'verified_drive_public_link',
                          side_effect=AssertionError('No extra Drive probes')):
            self.m.upload_downloaded(self.job(), self.local, None, state)
        self.assertEqual(run.call_count, 1)
        self.assertIn('copyto', run.call_args.args[0])
        self.assertTrue(self.local.exists())
        self.assertEqual(self.job()['status'], 'delivered')

    def test_immutable_conflict_never_runs_probe_or_claims_delivery(self):
        self.create_job(status='downloaded')
        state = {'upload_ready': True, 'rclone': '/fake/rclone', 'progress_streaming': False}
        with patch.object(self.m.subprocess, 'run',
                          return_value=subprocess.CompletedProcess(['rclone'], 1, '',
                                                                  'immutable file already exists')) as run, \
             patch.object(self.m, 'verified_drive_public_link',
                          side_effect=AssertionError('No extra Drive probes')):
            with self.assertRaisesRegex(self.m.DriveUploadFailure, 'DESTINATION_CONFLICT_UNVERIFIED'):
                self.m.upload_downloaded(self.job(), self.local, None, state)
        self.assertEqual(run.call_count, 1)
        self.assertTrue(self.local.exists())
        self.assertNotEqual(self.job()['status'], 'delivered')

    def test_immutable_conflict_requires_manual_retry_without_deleting_audio(self):
        self.create_job(status='downloaded')
        state = {'upload_ready': True, 'rclone': '/fake/rclone', 'progress_streaming': False}
        with patch.object(self.m.subprocess, 'run',
                          return_value=subprocess.CompletedProcess(['rclone'], 1, '',
                                                                  'immutable file already exists')):
            with self.assertRaises(self.m.DriveUploadFailure) as raised:
                self.m.upload_downloaded(self.job(), self.local, None, state)
        self.m._schedule_upload_retry(self.jid, raised.exception)
        self.assertEqual(self.job()['status'], 'failed')
        self.assertIsNone(self.job()['next_retry_at'])
        self.assertEqual(self.job()['upload_retry_count'], 0)
        self.assertTrue(self.local.exists())


    def test_only_confirmed_api_limits_schedule_retry_other_errors_are_terminal(self):
        self.create_job()
        problems = [
            RuntimeError('Drive anonymous access returned unexpected content type'),
            RuntimeError('Google Drive content hash mismatch'),
            RuntimeError('Google Drive anonymous audio download not verified'),
            self.m.DriveUploadFailure('AUTH_REQUIRED'),
            self.m.DriveUploadFailure('PERMISSION_DENIED'),
            self.m.DriveUploadFailure('STORAGE_FULL'),
            self.m.DriveUploadFailure('NETWORK_TIMEOUT'),
            self.m.DriveUploadFailure('NETWORK_FAILURE'),
            self.m.DriveUploadFailure('TRANSFER_FAILED'),
            self.m.DriveUploadFailure('DESTINATION_CONFLICT_UNVERIFIED'),
        ]
        for problem in problems:
            with self.subTest(problem=str(problem)):
                with self.m.db() as con:
                    con.execute("UPDATE jobs SET status='uploading',next_retry_at=NULL WHERE job_id=?", (self.jid,))
                self.m._schedule_upload_retry(self.jid, problem)
                data = self.job()
                self.assertEqual(data['status'], 'failed')
                self.assertIsNone(data['next_retry_at'])
                self.assertEqual(data['upload_retry_count'], 0)
                self.assertTrue(self.local.exists())
                self.assertEqual(data['progress_stage'], 'manual retry required')

    def test_legacy_non_limit_retry_jobs_stop_after_restart(self):
        self.create_job(status='upload_retry')
        with self.m.db() as con:
            con.execute("""UPDATE jobs SET next_retry_at=?,upload_retry_count=70,error=?
                           WHERE job_id=?""", (self.m.utc_after(-1),
                               'Drive anonymous access returned unexpected content type', self.jid))
        self.m.recover_interrupted_jobs()
        self.assertEqual(self.job()['status'], 'failed')
        self.assertEqual(self.job()['upload_retry_count'], 70)
        self.assertIsNone(self.job()['next_retry_at'])
        self.assertIsNone(self.m._claim())
        self.assertTrue(self.local.exists())

    def test_existing_rate_limit_retry_survives_restart(self):
        self.create_job(status='upload_retry')
        with self.m.db() as con:
            con.execute("""UPDATE jobs SET next_retry_at=?,error=?
                           WHERE job_id=?""", (self.m.utc_after(-1),
                               str(self.m.DriveUploadFailure('RATE_LIMITED')), self.jid))
        self.m.recover_interrupted_jobs()
        self.assertEqual(self.job()['status'], 'upload_retry')
        self.assertTrue(self.m._claim()['_resume_upload'])

    def test_manual_retry_survives_restart_without_automatic_retry(self):
        self.create_job(status='failed')
        self.m.job_action(self.jid, 'retry')
        self.assertEqual(self.job()['progress_stage'], 'manual retry requested')
        self.m.recover_interrupted_jobs()
        self.assertEqual(self.job()['status'], 'upload_retry')
        self.assertTrue(self.m._claim()['_resume_upload'])

    def test_rclone_non_limit_errors_are_manual_and_retry_internal_attempts_disabled(self):
        self.create_job(status='downloaded')
        state = {'upload_ready': True, 'rclone': '/fake/rclone', 'progress_streaming': False}
        calls = []
        def transfer(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 1, '', 'insufficientFilePermissions')
        with patch.object(self.m.subprocess, 'run', side_effect=transfer):
            with self.assertRaisesRegex(self.m.DriveUploadFailure, 'PERMISSION_DENIED') as raised:
                self.m.upload_downloaded(self.job(), self.local, None, state)
        self.assertEqual(calls[0][calls[0].index('--retries')+1], '1')
        self.assertEqual(calls[0][calls[0].index('--low-level-retries')+1], '1')
        self.m._schedule_upload_retry(self.jid, raised.exception)
        self.assertEqual(self.job()['status'], 'failed')
        self.assertTrue(self.local.exists())

    def test_content_type_verification_error_does_not_automatically_retry(self):
        import hashlib
        import urllib.request
        self.create_job(status='uploading')
        remote = 'gdrive:Song Studio/Audio downloader/' + VIDEO + '.mp3'
        def drive(args, **kwargs):
            if 'lsjson' in args:
                return subprocess.CompletedProcess(args, 0, json.dumps({
                    'Size': len(MP3), 'Hashes': {'MD5': hashlib.md5(MP3).hexdigest()}}), '')
            if 'link' in args:
                return subprocess.CompletedProcess(args, 0,
                    'https://drive.google.com/file/d/123456789012/view', '')
            self.fail('unexpected Drive command')
        class BadResponse:
            headers = {'Content-Type': 'text/html'}
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, limit): return b'<html>not-audio</html>'
        with patch.object(self.m, 'deps', return_value={'rclone':'/fake/rclone'}), \
             patch.object(self.m.subprocess, 'run', side_effect=drive), \
             patch.object(urllib.request, 'urlopen', return_value=BadResponse()):
            with self.assertRaisesRegex(RuntimeError, 'unexpected content type') as raised:
                self.m.verified_drive_public_link(remote, self.local, 'mp3')
        self.m._schedule_upload_retry(self.jid, raised.exception)
        self.assertEqual(self.job()['status'], 'failed')
        self.assertIsNone(self.job()['next_retry_at'])
        self.assertTrue(self.local.exists())

    def test_verified_drive_api_rate_limit_429_can_automatically_retry(self):
        import urllib.request
        import urllib.error
        import hashlib
        self.create_job(status='uploading')
        remote = 'gdrive:Song Studio/Audio downloader/' + VIDEO + '.mp3'
        def drive(args, **kwargs):
            if 'lsjson' in args:
                return subprocess.CompletedProcess(args, 0, json.dumps({
                    'Size': len(MP3), 'Hashes': {'MD5': hashlib.md5(MP3).hexdigest()}}), '')
            if 'link' in args:
                return subprocess.CompletedProcess(args, 0,
                    'https://drive.google.com/file/d/123456789012/view', '')
            self.fail('unexpected Drive command')
        with patch.object(self.m, 'deps', return_value={'rclone':'/fake/rclone'}), \
             patch.object(self.m.subprocess, 'run', side_effect=drive), \
             patch.object(urllib.request, 'urlopen',
                          side_effect=urllib.error.HTTPError('https://drive.google.com', 429,
                                                               'Rate Limited', {}, None)):
            with self.assertRaisesRegex(self.m.DriveUploadFailure, 'RATE_LIMITED') as raised:
                self.m.verified_drive_public_link(remote, self.local, 'mp3')
        self.m._schedule_upload_retry(self.jid, raised.exception)
        self.assertEqual(self.job()['status'], 'upload_retry')
        self.assertEqual(self.job()['upload_retry_count'], 1)
        self.assertTrue(self.local.exists())


if __name__ == "__main__":
    unittest.main()

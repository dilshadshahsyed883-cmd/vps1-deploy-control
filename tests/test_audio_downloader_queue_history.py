"""VS-ISS-20261020-05/06: isolated Audio Downloader queue/history persistence and browser route contracts."""
from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "apps/voice-studio/part01/audio_downloader_api.py"
TEMPLATE = ROOT / "apps/voice-studio/part01/templates/voice_studio.html"


class AudioDownloaderQueueHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="audio-downloader-test-")
        self.env = patch.dict(os.environ, {"AUDIO_DOWNLOADER_ROOT": self.tmp.name})
        self.env.start()
        spec = importlib.util.spec_from_file_location("isolated_audio_downloader", MODULE)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        app = FastAPI()
        app.include_router(self.module.router)
        self.client = TestClient(app)
        self.module.init_storage()

    def tearDown(self):
        self.client.close()
        self.env.stop()
        self.tmp.cleanup()

    def record(self, job_id: str, status: str, error: str = ""):
        with self.module.db() as db:
            db.execute(
                """INSERT INTO jobs
                (job_id,batch_id,video_id,url,project,folder,format,quality,
                auto_upload,keep_metadata,status,title,error,created_at,updated_at,completed_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (job_id, "ADB-TEST", "ABCDEFGHIJK",
                 "https://www.youtube.com/watch?v=ABCDEFGHIJK",
                 "Test", "Test Folder", "mp3", 192, 0, 0,
                 status, "Authorized test clip", error,
                 "2026-10-20T10:00:00+00:00", "2026-10-20T10:00:00+00:00",
                 "2026-10-20T10:00:00+00:00" if status in ("completed", "delivered") else None),
            )

    def test_empty_queue_returns_authenticated_backend_response_shape(self):
        r = self.client.get("/tts/api/audio-downloader/jobs")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"jobs": []})
        # Nginx session enforcement cannot be reproduced in this isolated TestClient.

    def test_active_failed_and_cancelled_jobs_are_real_records(self):
        for suffix, status, error in (
            ("1", "queued", ""),
            ("2", "downloading", ""),
            ("3", "failed", "Authorized test failure"),
            ("4", "cancelled", ""),
        ):
            self.record("ADJ-" + suffix * 16, status, error)
        response = self.client.get("/tts/api/audio-downloader/jobs?limit=200")
        self.assertEqual(response.status_code, 200)
        by_state = {r["status"]: r for r in response.json()["jobs"]}
        self.assertEqual(set(by_state), {"queued", "downloading", "failed", "cancelled"})
        self.assertEqual(by_state["failed"]["error"], "Authorized test failure")
        self.assertNotIn("local_file", by_state["failed"])

    def test_completed_history_survives_db_reopening(self):
        jid = "ADJ-" + "A" * 16
        self.record(jid, "completed")
        first = self.client.get("/tts/api/audio-downloader/jobs").json()["jobs"]
        self.assertEqual(first[0]["job_id"], jid)
        # Close/reopen connection, as happens across requests and process restarts.
        self.module.init_storage()
        second = self.client.get("/tts/api/audio-downloader/jobs").json()["jobs"]
        self.assertEqual(second[0]["status"], "completed")
        self.assertEqual(second[0]["title"], "Authorized test clip")

    def test_retry_and_cancel_preserve_other_records(self):
        pending = "ADJ-" + "B" * 16
        failed = "ADJ-" + "C" * 16
        self.record(pending, "queued")
        self.record(failed, "failed", "Test error")
        self.assertEqual(self.client.post("/tts/api/audio-downloader/jobs/" + pending + "/cancel").status_code, 200)
        self.assertEqual(self.client.post("/tts/api/audio-downloader/jobs/" + failed + "/retry").status_code, 200)
        jobs = {row["job_id"]: row for row in self.client.get("/tts/api/audio-downloader/jobs").json()["jobs"]}
        self.assertEqual(jobs[pending]["status"], "cancelled")
        self.assertEqual(jobs[failed]["status"], "queued")
        self.assertIsNone(jobs[failed]["error"])

    def test_proxy_and_status_routes_still_registered(self):
        routes = {r.path for r in self.client.app.routes}
        self.assertTrue({"/tts/api/audio-downloader/status", "/tts/api/audio-downloader/proxy",
                         "/tts/api/audio-downloader/jobs",
                         "/tts/api/audio-downloader/jobs/{job_id}/audio"}.issubset(routes))


class AudioDownloaderBrowserRouteContractTests(unittest.TestCase):
    def test_voice_shell_prefix_for_all_audio_downloader_calls(self):
        html = TEMPLATE.read_text(encoding="utf-8")
        base = "(location.pathname.startsWith('/voice-studio/')?'/voice-studio':'')"
        self.assertIn("const root = " + base + "+'/tts/api/audio-downloader';", html)
        self.assertIn("prefix=" + base + "+'/tts/api/audio-downloader/proxy';", html)
        self.assertIn("fetch(root+endpoint.slice('/tts/api/audio-downloader'.length)", html)
        self.assertIn("if(!Array.isArray(r.jobs))", html)
        self.assertIn("const historyJobs=jobs.filter(j=>terminal.has(j.status))", html)
        self.assertIn("if(location.hash==='#vs-audio')choose(true)", html)
        self.assertIn("cache:'no-store'", html)

    def test_backend_has_durable_sqlite_and_main_routes(self):
        source = MODULE.read_text(encoding="utf-8")
        self.assertIn("DB = DATA / 'jobs.sqlite3'", source)
        self.assertIn("@router.get('/jobs')", source)
        self.assertIn("SELECT * FROM jobs ORDER BY created_at DESC", source)
        self.assertIn("recover_interrupted_jobs()", source)


class AudioDownloaderWorkerSafeReleaseTests(unittest.TestCase):
    def test_existing_voice_release_overlays_only_reviewed_audio_expressions(self):
        from production import voice_template_release as release
        source = TEMPLATE.read_bytes()
        previous = source
        for old, corrected in release.AUDIO_BROWSER_ROUTE_PAIRS:
            self.assertEqual(source.count(corrected), 1)
            self.assertEqual(source.count(old), 0)
            previous = previous.replace(corrected, old, 1)
        # An unrelated installed header must survive byte-for-byte.
        previous = b"<!-- retained independent Voice styling -->\n" + previous
        patched = release.audio_browser_route_overlay(previous, source)
        self.assertEqual(patched, b"<!-- retained independent Voice styling -->\n" + source)

    def test_release_rejects_unknown_audio_runtime_drift(self):
        from production import voice_template_release as release
        source = TEMPLATE.read_bytes()
        original = source.replace(b"const root = ", b"const root_drift = ", 1)
        with self.assertRaisesRegex(RuntimeError, "installed Audio Downloader JavaScript"):
            release.audio_browser_route_overlay(original, source)


if __name__ == "__main__":
    unittest.main()

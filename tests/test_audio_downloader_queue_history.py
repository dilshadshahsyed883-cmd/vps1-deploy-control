"""VS-ISS-20261020-05/06: isolated Audio Downloader queue/history persistence and browser route contracts."""
from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "apps/voice-studio/part01/audio_downloader_api.py"
TEMPLATE = ROOT / "apps/voice-studio/part01/templates/voice_studio.html"


class AudioDownloaderQueueHistoryTests(unittest.TestCase):
    def setUp(self):
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest('FastAPI/httpx required for isolated runtime integration')
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

    def test_enqueue_ignores_legacy_batch_and_subfolder_preferences(self):
        data = {'links': ['https://youtu.be/BaW_jenozKc'], 'permission_confirmed': True,
                'project_name': 'Old Batch', 'drive_folder': 'Old Subfolder'}
        # This test loads the API through importlib rather than sys.modules;
        # resolve postponed type annotations for the isolated Pydantic model.
        self.module.BatchRequest.model_rebuild(_types_namespace=vars(self.module))
        with patch.object(self.module, 'deps', return_value={'ready': True, 'upload_ready': True}):
            result = self.module.enqueue(self.module.BatchRequest(**data))
        self.assertEqual(result['created'], 1)
        with self.module.db() as con:
            row = con.execute('SELECT video_id,project,folder FROM jobs').fetchone()
        self.assertEqual(tuple(row), ('BaW_jenozKc', '', ''))

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
        routes = {r.path for r in self.module.router.routes}
        self.assertTrue({"/tts/api/audio-downloader/status", "/tts/api/audio-downloader/proxy",
                         "/tts/api/audio-downloader/jobs",
                         "/tts/api/audio-downloader/jobs/{job_id}/audio"}.issubset(routes))


class AudioDownloaderBrowserRouteContractTests(unittest.TestCase):
    def test_audio_downloader_has_only_flat_drive_destination_and_no_batch_input(self):
        html = TEMPLATE.read_text(encoding='utf-8')
        panel = html.split('<!-- AUDIO_DOWNLOADER_TAB_V1 -->', 1)[1].split('<!-- /AUDIO_DOWNLOADER_TAB_V1 -->', 1)[0]
        self.assertIn('id="vsaDrivePath">Google Drive / Audio Downloader</div>', panel)
        for removed in ('id="vsaProject"', 'id="vsaFolder"', 'Project / batch name',
                        'Google Drive subfolder', 'YouTube Audio / Audio Batch'):
            self.assertNotIn(removed, panel)
        self.assertNotIn("$('vsaProject')", html)
        self.assertNotIn("$('vsaFolder')", html)
        self.assertIn("job.video_id||'audio'", html)

    def test_voice_shell_prefix_for_all_audio_downloader_calls(self):
        html = TEMPLATE.read_text(encoding="utf-8")
        base = "(location.pathname.startsWith('/voice-studio/')?'/voice-studio':'')"
        self.assertIn("const root = " + base + "+'/'+'tts/api/audio-downloader';", html)
        self.assertIn("prefix=" + base + "+'/'+'tts/api/audio-downloader/proxy';", html)
        self.assertIn("fetch(root+endpoint.slice(('/'+'tts/api/audio-downloader').length)", html)
        self.assertIn("if(!Array.isArray(r.jobs))", html)
        self.assertIn("const historyJobs=jobs.filter(j=>terminal.has(j.status))", html)
        self.assertIn("if(location.hash==='#vs-audio')choose(true)", html)
        self.assertIn("cache:'no-store'", html)
        self.assertNotIn("+'/tts/api/audio-downloader'", html)
        self.assertNotIn("endpoint.slice('/tts/api/audio-downloader'", html)

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

    def test_nginx_sub_filter_cannot_duplicate_voice_prefix_in_any_audio_action(self):
        from production import voice_template_release as release
        template = TEMPLATE.read_bytes()
        originals = template
        for old, corrected in release.AUDIO_BROWSER_V3_PATH_PAIRS:
            self.assertEqual(template.count(corrected), 1, corrected)
            self.assertEqual(template.count(old), 0, old)
            originals = originals.replace(corrected, old, 1)
        self.assertEqual(release.audio_browser_route_overlay(originals, template), template)
        self.assertEqual(release.audio_browser_route_overlay(template, template), template)
        start = template.index(b'<script id="audio-downloader-v1-js">')
        end = template.index(b'</script>', start)
        second_start = template.index(b'<script id="audio-downloader-proxy-v1">')
        second_end = template.index(b'</script>', second_start)
        for js in (template[start:end], template[second_start:second_end]):
            public = js
            for needle, replacement in (
                (b"'/jobs", b"'/voice-studio/jobs"),
                (b"'/status", b"'/voice-studio/status"),
                (b"'/profiles", b"'/voice-studio/profiles"),
                (b"'/enable", b"'/voice-studio/enable"),
            ):
                public = public.replace(needle, replacement)
            self.assertEqual(public, js, "Nginx changed browser request path")
        vulnerable = b"api(root+'/jobs?limit=200')"
        self.assertNotEqual(vulnerable.replace(b"'/jobs", b"'/voice-studio/jobs"),
                            vulnerable, "regression test must detect the previous bug")

    def test_release_verifies_real_post_nginx_browser_javascript(self):
        release = (ROOT / "production/voice_template_release.py").read_text()
        self.assertIn("public Audio Downloader browser fetch paths rewritten or stale", release)
        self.assertIn("public Audio Downloader browser fetch paths duplicated Voice prefix", release)
        self.assertIn("verify_audio_downloader_https(cookie)", release)

    def test_release_rejects_active_audio_download(self):
        from production import voice_template_release as release
        from unittest.mock import Mock
        fake = Mock()
        fake.status = 200
        fake.read.return_value = b'{"queue":{"queued":0,"downloading":1,"uploading":0}}'
        with patch.object(release.urllib.request, "urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value = fake
            with self.assertRaisesRegex(RuntimeError, "active/pending jobs"):
                release.require_idle_audio_downloader()

    def test_release_allows_idle_download_queue(self):
        from production import voice_template_release as release
        from unittest.mock import Mock
        fake = Mock()
        fake.status = 200
        fake.read.return_value = b'{"queue":{"queued":0,"downloading":0,"uploading":0}}'
        with patch.object(release.urllib.request, "urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value = fake
            release.require_idle_audio_downloader()

    def test_release_probes_authenticated_queue_history_datasource_and_proxy(self):
        from production import voice_template_release as release
        import json
        def fake_https(path, cookie=""):
            if not cookie:
                return 401, "text/html", b""
            if path.endswith("/jobs?limit=1"):
                return 200, "application/json", b'{"jobs":[]}'
            if path.endswith("/status"):
                return 200, "application/json", b'{"queue":{}}'
            if path.endswith("/proxy"):
                return 200, "application/json", b'{"profiles":[]}'
            return 404, "application/json", b'{"detail":"Not Found"}'
        with patch("production.route_uploader_scene_https.https_get", side_effect=fake_https):
            release.verify_audio_downloader_https("session=authorized-fixture")

    def test_production_readback_requires_public_revision_and_real_endpoints(self):
        release_source = (ROOT / "production/voice_template_release.py").read_text()
        self.assertIn('b"AUDIO_DOWNLOADER_BROWSER_ROUTE_CONTRACT_V2" in full_html', release_source)
        self.assertIn('verify_audio_downloader_https(cookie)', release_source)
        self.assertIn('require_idle_audio_downloader()', release_source)
        self.assertIn('anonymous in (401, 403, 303)', release_source)
        self.assertNotIn('all(new in full_html and old not in full_html for old, new in AUDIO_BROWSER_ROUTE_PAIRS)', release_source)

    def test_release_does_not_accept_queue_404_as_success(self):
        from production import voice_template_release as release
        with patch("production.route_uploader_scene_https.https_get",
                   return_value=(404, "application/json", b'{"detail":"Not Found"}')):
            with self.assertRaisesRegex(RuntimeError, "authenticated HTTPS jobs endpoint failed"):
                release.verify_audio_downloader_https("session=authorized-fixture")

    def test_release_rejects_unknown_audio_runtime_drift(self):
        from production import voice_template_release as release
        source = TEMPLATE.read_bytes()
        original = source.replace(b"const root = ", b"const root_drift = ", 1)
        with self.assertRaisesRegex(RuntimeError, "installed Audio Downloader JavaScript"):
            release.audio_browser_route_overlay(original, source)


if __name__ == "__main__":
    unittest.main()

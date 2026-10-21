"""Offline regression for two isolated Voice Studio queue routes and safe release."""
from __future__ import annotations

from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from production import voice_queue_release as release

ROOT = Path(__file__).resolve().parents[1]


class VoiceQueueRepairTests(TestCase):
    def test_both_canonical_backends_expose_queue_and_keep_generation(self):
        for module in release.MODULES:
            with self.subTest(module=str(module)):
                source = (ROOT / "apps/voice-studio" / module).read_bytes()
                release.check_source(module, source)

    def test_fail_closed_on_missing_jobs_route(self):
        module = release.MODULES[0]
        source = (ROOT / "apps/voice-studio" / module).read_bytes()
        broken = source.replace(b"@router.get('/jobs')", b"@router.get('/old-jobs')")
        self.assertNotEqual(source, broken)
        with self.assertRaisesRegex(RuntimeError, "lacks canonical GET /jobs"):
            release.check_source(module, broken)

    def test_queued_endpoints_are_isolated_from_omnivoice_and_other_apps(self):
        code = (ROOT / "production/voice_queue_release.py").read_text()
        runtime = (ROOT / "production/runtime.py").read_text()
        workflow = (ROOT / ".github/workflows/vps1-voice-queue-api-release.yml").read_text()
        self.assertEqual([str(path) for path in release.MODULES],
                         ["part01/kokoro_api.py", "part01/edge_api.py"])
        self.assertIn("atomic_link(CURRENT, target)", code)
        self.assertIn("atomic_link(CURRENT, previous)", code)
        self.assertIn('run(["systemctl", "restart", UNIT]', code)
        self.assertIn("verify_queues()", code)
        self.assertNotIn("workers.stop(", code)
        self.assertNotIn("workers.install(", code)
        self.assertNotIn("prepare_kokoro(", code)
        self.assertNotIn("unlink(", code)
        self.assertNotIn("DELETE FROM", code)
        self.assertIn("'[vps1-voice-queue-api-only]' in commit_message", runtime)
        self.assertIn("install_voice_queue_api(revision)", runtime)
        self.assertIn("'[vps1-voice-queue-release]'", workflow)
        self.assertIn("'[vps1-voice-queue-api-only]'", workflow)
        self.assertIn("PASS_VOICE_QUEUE_API", workflow)
        self.assertIn("install $SOURCE_SHA voice-studio", workflow)
        self.assertIn("BlockingIOError: [Errno 11]", workflow)
        self.assertNotIn("install $SOURCE_SHA all", workflow)

    def test_endpoint_checks_refuse_status_only_false_positive(self):
        with patch.object(release, "local_endpoint", side_effect=[
            (200, {"ready": True}), (404, {"detail": "Not Found"})
        ]):
            with self.assertRaisesRegex(RuntimeError, "upstream queue API"):
                release.verify_queues()

    def test_api_works_even_with_empty_queue(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        def local(engine, action):
            if action == "status":
                return 200, {"ready": True, "reason": ""}
            return 200, {"jobs": []}
        with patch.object(release, "local_endpoint", side_effect=local), \
             patch("production.route_uploader_scene_https.scene_cookie", return_value="token"), \
             patch("production.route_uploader_scene_https.https_get",
                   side_effect=lambda path, cookie=None: (
                       (200, "application/json", b'{"jobs":[]}')
                       if cookie else (401, "text/html", b""))):
            release.verify_queues()

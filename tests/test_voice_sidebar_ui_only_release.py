"""Offline guardrails for the isolated Voice sidebar HTML-only release."""
from __future__ import annotations

import ast
import hashlib
from pathlib import Path
import unittest

from production.voice_template_release import (
    ASSET_NOTE, NEW_ASSETS, OLD_ASSETS, PREVIOUS_ASSETS, PAGE, TITLE,
    patch_template,
)

ROOT = Path(__file__).resolve().parents[1]


class VoiceSidebarUIOnlyTests(unittest.TestCase):
    def setUp(self):
        self.previous = (
            b"<!doctype html><html><head>" + TITLE
            + b"\n<style>.vps3-main-sidebar{display:flex}</style>\n"
            + OLD_ASSETS
            + b"</head><body>"
            + b'<button id="vsEngineOmni">OmniVoice</button>'
            + b'<button id="vsEngineKokoro">Kokoro</button>'
            + b"</body></html>"
        )

    def test_html_only_patch_relocates_shell_assets_without_touching_engines(self):
        new = patch_template(self.previous)
        self.assertEqual(new.count(NEW_ASSETS), 1)
        self.assertNotIn(OLD_ASSETS, new)
        self.assertIn(ASSET_NOTE, new)
        self.assertLess(new.index(NEW_ASSETS), new.index(b"<style>"))
        self.assertIn(b'id="vsEngineOmni"', new)
        self.assertIn(b'id="vsEngineKokoro"', new)
        self.assertEqual(new.replace(TITLE + b"\n" + NEW_ASSETS, TITLE)
                         .replace(ASSET_NOTE, OLD_ASSETS), self.previous)
        self.assertNotEqual(hashlib.sha256(new).digest(), hashlib.sha256(self.previous).digest())

    def test_previous_production_voice_sidebar_version_upgrades_in_place(self):
        # Previous UI-only deploy already contains the early asset block.
        older = self.previous.replace(OLD_ASSETS, ASSET_NOTE)
        older = older.replace(TITLE, TITLE + b"\n" + PREVIOUS_ASSETS)
        next_page = patch_template(older)
        self.assertIn(NEW_ASSETS, next_page)
        self.assertNotIn(PREVIOUS_ASSETS, next_page)
        self.assertEqual(next_page.replace(NEW_ASSETS, PREVIOUS_ASSETS), older)
        self.assertIn(b'id="vsEngineOmni"', next_page)

    def test_canonical_assets_without_optional_note_are_safe_and_idempotent(self):
        # Other approved independent Voice releases may keep the canonical
        # CSS/JS pair while omitting the original legacy relocation comment.
        current = self.previous.replace(OLD_ASSETS, NEW_ASSETS)
        self.assertNotIn(ASSET_NOTE, current)
        self.assertEqual(patch_template(current), current)
        with self.assertRaises(RuntimeError):
            patch_template(current.replace(b'id="vsEngineOmni"', b'id="missing"'))
        with self.assertRaises(RuntimeError):
            patch_template(current.replace(b'id="vsEngineKokoro"', b'id="missing"'))
        with self.assertRaises(RuntimeError):
            patch_template(current.replace(NEW_ASSETS, NEW_ASSETS + NEW_ASSETS))

    def test_fail_closed_for_unrecognized_or_duplicate_production_html(self):
        for changed in (
            self.previous.replace(OLD_ASSETS, b""),
            self.previous.replace(OLD_ASSETS, OLD_ASSETS + OLD_ASSETS),
            self.previous.replace(TITLE, b"Other Page"),
            self.previous.replace(b'id="vsEngineOmni"', b'id="missing"'),
            self.previous.replace(OLD_ASSETS, NEW_ASSETS).replace(
                b'id="vsEngineOmni"', b'id="missing"'),
        ):
            with self.subTest(changed=len(changed)):
                with self.assertRaises(RuntimeError):
                    patch_template(changed)

    def test_release_only_restarts_web_and_keeps_worker_paths_unmodified(self):
        source = (ROOT / "production/voice_template_release.py").read_text()
        tree = ast.parse(source)
        self.assertTrue(tree.body)
        self.assertEqual(str(PAGE), "part01/templates/voice_studio.html")
        self.assertIn('run(["systemctl", "restart", UNIT]', source)
        self.assertIn("atomic_link(CURRENT, target)", source)
        self.assertIn("atomic_link(CURRENT, previous)", source)
        self.assertIn("verify_runtime()", source)
        self.assertIn("anonymous in {401, 403, 303}", source)
        self.assertNotIn("workers.install(", source)
        self.assertNotIn("workers.stop(", source)
        self.assertNotIn("prepare_kokoro(", source)
        self.assertNotIn("DATA /", source)
        self.assertNotIn("production-voice-dispatcher", source)

    def test_dedicated_release_is_scoped_and_only_retries_host_lock(self):
        workflow = (ROOT / ".github/workflows/vps1-voice-ui-only-release.yml").read_text()
        self.assertIn("group: vps1-voice-ui-only", workflow)
        self.assertIn("'[vps1-voice-ui-only-release]'", workflow)
        self.assertIn("'[vps1-voice-template-only]'", workflow)
        self.assertIn("vps1deploy@100.87.55.92", workflow)
        self.assertIn("install $SOURCE_SHA voice-studio", workflow)
        self.assertIn("BlockingIOError: [Errno 11]", workflow)
        self.assertIn("PASS_VOICE_TEMPLATE_ONLY", workflow)
        self.assertNotIn("install $SOURCE_SHA all", workflow)
        self.assertNotIn("nginx-route-only", workflow)
        self.assertNotIn("voice-dispatcher", workflow)

    def test_protected_main_message_required_to_enter_ui_only_path(self):
        source = (ROOT / "production/runtime.py").read_text()
        self.assertIn("if app == 'voice-studio':", source)
        self.assertIn("'[vps1-voice-template-only]' in commit_message", source)
        self.assertIn("install_voice_template(revision)", source)
        self.assertIn("return", source)
        voice = (ROOT / "apps/voice-studio/part01/templates/voice_studio.html").read_bytes()
        song_assets = NEW_ASSETS.replace(
            b"vps1_native_site_shell.css?v=20261020-upload-studio-v1",
            b"vps1_native_site_shell.css?v=20261029-song-studio-v1",
        ).replace(
            b"vps1_native_site_shell.js?v=20261020-upload-studio-v1",
            b"vps1_native_site_shell.js?v=20261029-song-studio-v1",
        )
        # Preserve the byte-exact original CSS plus script tag contract while
        # permitting an explicit cache-busting query for Copyright navigation.
        expected_assets = song_assets.replace(
            b'vps1_native_site_shell.js?v=20261029-song-studio-v1"',
            b'vps1_native_site_shell.js?v=20261029-song-studio-v1&copyright-nav=20261030-v3"',
        )
        self.assertIn(expected_assets, voice)



class VoiceQueueURLOverlayTests(unittest.TestCase):
    """VPS Scene SSO app-prefixed routes, with root-local compatibility."""

    def test_three_active_engine_routes_and_edge_audio_route_are_rewrite_safe(self):
        from production.voice_template_release import QUEUE_ROUTE_PAIRS
        source = (ROOT / "apps/voice-studio/part01/templates/voice_studio.html").read_bytes()
        self.assertEqual(len(QUEUE_ROUTE_PAIRS), 3)
        for old, new in QUEUE_ROUTE_PAIRS:
            with self.subTest(old=old[:90]):
                self.assertEqual(source.count(new), 1)
                self.assertNotIn(old, source)
                self.assertNotIn(b"'/tts/api/", new)
        self.assertIn(b"location.pathname.startsWith('/voice-studio/')", source)
        self.assertIn(b"'/voice-studio':'')+'/'+'tts/api/kokoro'", source)
        self.assertIn(b"'/voice-studio':'')+'/'+'tts/api/edge'", source)
        self.assertIn(b"apiRoot+'/audio/'+encodeURIComponent(j.job_id)", source)
        self.assertIn(b"root+'/files/'+encodeURIComponent(j.job_id)+'/'+localAudio", source)

    def test_existing_runtime_overlay_is_byte_scoped_and_idempotent(self):
        from production.voice_template_release import queue_route_overlay, QUEUE_ROUTE_PAIRS
        source = (ROOT / "apps/voice-studio/part01/templates/voice_studio.html").read_bytes()
        original = source
        for old, new in QUEUE_ROUTE_PAIRS:
            original = original.replace(new, old, 1)
        self.assertNotEqual(original, source)
        self.assertEqual(queue_route_overlay(original, source), source)
        self.assertEqual(queue_route_overlay(source, source), source)

    def test_unknown_or_duplicate_runtime_blocks_release_without_worker_change(self):
        from production.voice_template_release import queue_route_overlay, QUEUE_ROUTE_PAIRS
        source = (ROOT / "apps/voice-studio/part01/templates/voice_studio.html").read_bytes()
        old, new = QUEUE_ROUTE_PAIRS[0]
        for damaged in (source.replace(new, b"UNKNOWN_QUEUE_ROOT", 1),
                        source.replace(new, new + new, 1)):
            with self.assertRaisesRegex(RuntimeError, "queue JS differs"):
                queue_route_overlay(damaged, source)

    def test_release_installs_only_voice_html_not_queue_worker(self):
        from production import voice_template_release as module
        code = (ROOT / "production/voice_template_release.py").read_text()
        self.assertIn("patched_html = queue_route_overlay(patched_html, source_html)", code)
        self.assertIn("public Voice queue JavaScript is not updated", code)
        self.assertIn("local Voice queue JavaScript is not updated", code)
        self.assertNotIn("workers.install(", code)
        self.assertNotIn("workers.stop(", code)
        self.assertTrue(hasattr(module, "queue_route_overlay"))



class VoiceFullHTTPSReadRegression(unittest.TestCase):
    def test_queue_script_tail_is_verified_beyond_legacy_180k_limit(self):
        from unittest.mock import MagicMock, patch
        from production.voice_template_release import read_full_authenticated_voice_html
        late_scripts = (b'X' * 190_000
            + b'<script id="voiceStudioDualEngineV1Script"></script>'
            + b'<script id="voiceStudioEdgeEngineV1Script"></script>')
        tls_context = MagicMock()
        tls_context.__enter__.return_value = MagicMock()
        tcp_context = MagicMock()
        tcp_context.__enter__.return_value = MagicMock()
        response = MagicMock(status=200)
        response.getheader.side_effect = lambda key, default='': {'Content-Type':'text/html; charset=utf-8', 'Cache-Control':'private, no-store', 'X-Voice-Queue-UI':'VSQ-20261020-01'}.get(key,default)
        response.read.return_value = late_scripts
        https_conn = MagicMock()
        https_conn.getresponse.return_value = response
        with patch('production.voice_template_release.socket.create_connection',
                   return_value=tcp_context), \
             patch('production.voice_template_release.ssl.create_default_context') as ssl_ctx, \
             patch('production.voice_template_release.http.client.HTTPSConnection',
                   return_value=https_conn):
            ssl_ctx.return_value.wrap_socket.return_value = tls_context
            self.assertEqual(read_full_authenticated_voice_html('session=only-test'),
                             late_scripts)
            response.read.assert_called_once_with(750_000)
            self.assertEqual(https_conn.request.call_args.args[:2],
                             ('GET', '/voice-studio/app'))
            self.assertEqual(https_conn.request.call_args.kwargs['headers']['Cookie'],
                             'session=only-test')

    def test_full_probe_rejects_missing_late_inline_engine_script(self):
        from unittest.mock import MagicMock, patch
        from production.voice_template_release import read_full_authenticated_voice_html
        conn = MagicMock()
        conn.getresponse.return_value.status = 200
        conn.getresponse.return_value.getheader.side_effect = lambda key, default='': {'Content-Type':'text/html', 'Cache-Control':'private, no-store', 'X-Voice-Queue-UI':'VSQ-20261020-01'}.get(key,default)
        conn.getresponse.return_value.read.return_value = b'X' * 180_000
        tls = MagicMock()
        raw = MagicMock()
        with patch('production.voice_template_release.socket.create_connection',
                   return_value=raw), \
             patch('production.voice_template_release.ssl.create_default_context') as ctx, \
             patch('production.voice_template_release.http.client.HTTPSConnection',
                   return_value=conn):
            ctx.return_value.wrap_socket.return_value = tls
            with self.assertRaisesRegex(RuntimeError, 'late queue scripts'):
                read_full_authenticated_voice_html('session=only-test')



class VoiceQueueBrowserCacheAndTraceTests(unittest.TestCase):
    def test_reviewed_queue_fetch_functions_are_installed_and_idempotent(self):
        from production.voice_template_release import (
            QUEUE_FETCH_PAIRS, queue_route_overlay)
        source = (ROOT / "apps/voice-studio/part01/templates/voice_studio.html").read_bytes()
        self.assertEqual(len(QUEUE_FETCH_PAIRS), 5)
        original = source
        for old, new in QUEUE_FETCH_PAIRS:
            self.assertNotIn(old, source)
            self.assertEqual(source.count(new), 1)
            original = original.replace(new, old, 1)
        self.assertEqual(queue_route_overlay(original, source), source)
        self.assertEqual(queue_route_overlay(source, source), source)
        self.assertIn(b"cache:'no-store'", source)
        self.assertIn(b"new URL(url,location.href).pathname", source)
        self.assertIn(b"+'/'+'jobs?limit=150&v='+Date.now()", source)


    def test_nginx_sub_filter_cannot_rewrite_kokoro_or_edge_jobs_paths(self):
        raw = (ROOT / "apps/voice-studio/part01/templates/voice_studio.html").read_bytes()
        for engine in (b'voiceStudioDualEngineV1Script', b'voiceStudioEdgeEngineV1Script'):
            start = raw.index(b'<script id="' + engine + b'">')
            end = raw.index(b'</script>', start)
            js = raw[start:end]
            self.assertNotIn(b"'/jobs", js, engine)
            self.assertNotIn(b'"/jobs', js, engine)
            self.assertIn(b"+'/'+'jobs", js, engine)
            public_js = js.replace(b"'/jobs", b"'/voice-studio/jobs")
            self.assertEqual(public_js, js, engine)

    def test_voice_html_document_is_uncached_with_stable_revision_stamp(self):
        from production.voice_template_release import (
            voice_web_cache_overlay, WEB_CACHE_NEW, WEB_CACHE_OLD, VOICE_WEB)
        source = (ROOT / "apps/voice-studio" / VOICE_WEB).read_bytes()
        self.assertIn(WEB_CACHE_NEW, source)
        self.assertNotIn(WEB_CACHE_OLD, source)
        previous = source.replace(WEB_CACHE_NEW, WEB_CACHE_OLD)
        self.assertEqual(voice_web_cache_overlay(previous, source), source)
        self.assertEqual(voice_web_cache_overlay(source, source), source)
        for corrupted in (source.replace(WEB_CACHE_NEW, b"unreviewed", 1),
                          source.replace(WEB_CACHE_NEW, WEB_CACHE_NEW+WEB_CACHE_NEW, 1)):
            with self.assertRaisesRegex(RuntimeError, "web entrypoint"):
                voice_web_cache_overlay(corrupted, source)
        self.assertIn(b'Cache-Control', WEB_CACHE_NEW)
        self.assertIn(b'no-store', WEB_CACHE_NEW)
        self.assertIn(b'X-Voice-Queue-UI', WEB_CACHE_NEW)

    def test_release_does_not_touch_queue_or_worker_state(self):
        source = (ROOT / "production/voice_template_release.py").read_text()
        self.assertIn("(target / VOICE_WEB).write_bytes(patched_entry)", source)
        self.assertIn("voice_web_cache_overlay", source)
        self.assertIn("QUEUE_ROUTE_PAIRS + QUEUE_FETCH_PAIRS", source)
        self.assertIn("local Voice document cache/revision headers absent", source)
        self.assertIn("public Voice document can be cached", source)
        self.assertNotIn("workers.stop(", source)
        self.assertNotIn("workers.install(", source)
        self.assertNotIn("DELETE FROM", source)

if __name__ == "__main__":
    unittest.main()

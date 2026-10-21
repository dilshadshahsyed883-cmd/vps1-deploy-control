"""OmniVoice header + Kokoro-like sub-tabs; no runtime or queue changes."""
import inspect
import re
import unittest
from pathlib import Path

from production import voice_template_release as release

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "apps/voice-studio/part01/templates/voice_studio.html").read_bytes()


def prior_live_template():
    """Reconstruct only the exact last approved pre-header source state."""
    css_start = SOURCE.index(release.OMNI_HEADER_STYLE_ID)
    css_stop = SOURCE.index(b"</style>", css_start) + len(b"</style>")
    css = SOURCE[css_start:css_stop]
    assert SOURCE.count(css + b"\n") == 1
    legacy = SOURCE.replace(css + b"\n", b"", 1)
    header = release._omni_header(SOURCE)
    start = header.index(b'<nav class="enginebar"')
    end = header.index(b"</nav>", start) + len(b"</nav>")
    nav = header[start:end]
    assert legacy.count(header) == 1
    legacy = legacy.replace(header, nav, 1)
    assert legacy.count(release.OMNI_HEADING_PLACEHOLDER) == 1
    legacy = legacy.replace(release.OMNI_HEADING_PLACEHOLDER,
                            release.OMNI_LEGACY_HEADING, 1)
    return legacy


class OmniKokoroHeaderTests(unittest.TestCase):
    def test_prior_production_layout_migrates_to_exact_source(self):
        previous = prior_live_template()
        updated = release.omni_kokoro_header_overlay(previous, SOURCE)
        self.assertEqual(updated, SOURCE)
        self.assertNotEqual(previous, SOURCE)
        release.verify_omni_kokoro_header(updated)

    def test_is_idempotent_with_current_layout(self):
        self.assertEqual(release.omni_kokoro_header_overlay(SOURCE, SOURCE), SOURCE)
        self.assertEqual(release.omni_kokoro_header_overlay(
            release.omni_kokoro_header_overlay(prior_live_template(), SOURCE), SOURCE), SOURCE)

    def test_header_is_before_omni_content_and_five_existing_actions_retained(self):
        header = release._omni_header(SOURCE)
        self.assertLess(SOURCE.index(release.OMNI_HEADER_START),
                        SOURCE.index(release.OMNI_PAGE_SECTION))
        self.assertIn(b'<h1>OmniVoice</h1>', header)
        self.assertEqual(header.count(b'data-vps1-tab='), 5)
        self.assertEqual(SOURCE.count(b'id="omniWorkerBadge"'), 1)
        self.assertEqual(SOURCE.count(b'<nav class="enginebar"'), 1)
        for section in (b'production', b'design', b'clone', b'gpu', b'queue'):
            self.assertIn(b'data-vps1-tab="' + section + b'"', header)
        self.assertIn(b'body.vs-kokoro-active #vsOmniHeader', SOURCE)
        self.assertIn(b'body.vs-edge-active #vsOmniHeader', SOURCE)
        self.assertIn(b'body.vs-audio-active #vsOmniHeader', SOURCE)
        self.assertIn(b'#vsOmniHeader .enginebar .engine-tab.active', SOURCE)

    def test_reject_unknown_html_or_duplicate_style(self):
        old = prior_live_template()
        bad = (
            old.replace(b'<nav class="enginebar"', b'<nav class="other"', 1),
            old.replace(b'<h1>OmniVoice GPU</h1>', b'<h1>Unknown GPU</h1>', 1),
            old + release.OMNI_HEADER_STYLE_ID,
        )
        for html in bad:
            with self.subTest(html=html[-70:]):
                with self.assertRaisesRegex(RuntimeError, "VOICE_UI_RELEASE_BLOCKED"):
                    release.omni_kokoro_header_overlay(html, SOURCE)
        with self.assertRaisesRegex(RuntimeError, "VOICE_UI_RELEASE_BLOCKED"):
            release.omni_kokoro_header_overlay(old, SOURCE.replace(
                b'<h1>OmniVoice</h1>', b'<h1>Other</h1>', 1))

    def test_api_drive_queue_js_is_byte_identical(self):
        before = prior_live_template()
        after = release.omni_kokoro_header_overlay(before, SOURCE)
        extract = lambda d: re.findall(rb'<script(?:\s[^>]*)?>[\s\S]*?</script>', d)
        self.assertEqual(extract(before), extract(after))
        for marker in (b'tts/api/clone/omnivoice', b'vps1VoiceStudioTab',
                       b'omniQueueStatus', b'omniProfileSelect',
                       b'vskQueueRows', b'vseQueueRows', b'vsaQueueRows'):
            self.assertIn(marker, after)

    def test_installer_deploy_and_https_guards(self):
        installer = inspect.getsource(release.install)
        verifier = inspect.getsource(release.verify_runtime)
        self.assertIn('omni_kokoro_header_overlay(patched_html, source_html)', installer)
        self.assertIn('verify_omni_kokoro_header(body)', verifier)
        self.assertIn('verify_omni_kokoro_header(full_html)', verifier)


if __name__ == "__main__":
    unittest.main()

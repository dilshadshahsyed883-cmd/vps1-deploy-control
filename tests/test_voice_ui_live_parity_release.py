"""Regression: approved Voice UI must reach the old-template overlay installer.

The live UI-only deployment intentionally copies an immutable release; testing
the GitHub template alone missed three UI changes on 2026-10-21.
"""
import inspect
import unittest
from pathlib import Path

from production import voice_template_release as release

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "apps/voice-studio/part01/templates/voice_studio.html").read_bytes()


def legacy_source_fixture():
    """Simulate exactly the prior deployed UI while retaining all other scripts."""
    css_start = SOURCE.index(release.VOICE_UI_PARITY_ID)
    css_stop = SOURCE.index(b"</style>", css_start) + len(b"</style>")
    css = SOURCE[css_start:css_stop]
    assert SOURCE.count(css + b"\n") == 1
    old = SOURCE.replace(css + b"\n", b"", 1)
    for legacy, approved in (release.VOICE_UI_MARKUP_PAIRS +
                             release.VOICE_UI_JS_PAIRS):
        assert SOURCE.count(approved) == 1
        assert old.count(approved) == 1
        old = old.replace(approved, legacy, 1)
    return old


class VoiceUILiveParityTests(unittest.TestCase):
    def test_exact_legacy_html_becomes_complete_current_source(self):
        previous = legacy_source_fixture()
        deployed = release.voice_ui_parity_overlay(previous, SOURCE)
        self.assertEqual(deployed, SOURCE)
        self.assertIn(release.VOICE_UI_PARITY_ID, deployed)
        self.assertEqual(deployed.count(b'<th scope="col">Controls</th>'), 2)
        self.assertIn(b"d.append(button);", deployed)
        self.assertIn(b"ctrl.className='vsq-controls'", deployed)
        self.assertNotIn(b"c.append(' ',button);", deployed)
        self.assertNotEqual(previous, deployed)

    def test_idempotent_live_overlay(self):
        self.assertEqual(release.voice_ui_parity_overlay(SOURCE, SOURCE), SOURCE)
        self.assertEqual(release.voice_ui_parity_overlay(
            release.voice_ui_parity_overlay(legacy_source_fixture(), SOURCE),
            SOURCE), SOURCE)

    def test_reject_unknown_runtime_control_actions(self):
        fixture = legacy_source_fixture()
        for changed in (
            fixture.replace(b"c.append(' ',button);", b"c.append(button);", 1),
            fixture.replace(b"el.className='vse-btn alt';", b"el.className='unknown';", 1),
            fixture.replace(b'<th>Audio</th>', b'<th>Missing</th>', 1),
            fixture.replace(b"<div id=\"vseQueue\" class=\"vse-card\"",
                            b"<div id=\"vseQueue\" class=\"broken\"", 1),
        ):
            with self.subTest(fragment=changed[:60]):
                with self.assertRaisesRegex(RuntimeError, "VOICE_UI_RELEASE_BLOCKED"):
                    release.voice_ui_parity_overlay(changed, SOURCE)

    def test_reject_unreviewed_github_source_or_duplicate_styles(self):
        with self.assertRaisesRegex(RuntimeError, "VOICE_UI_RELEASE_BLOCKED"):
            release.voice_ui_parity_overlay(
                legacy_source_fixture(),
                SOURCE.replace(release.VOICE_UI_PARITY_ID, b'id="removed"', 1))
        with self.assertRaisesRegex(RuntimeError, "VOICE_UI_RELEASE_BLOCKED"):
            release.voice_ui_parity_overlay(
                legacy_source_fixture() + release.VOICE_UI_PARITY_ID, SOURCE)

    def test_original_api_and_drive_js_preserved(self):
        before = legacy_source_fixture()
        after = release.voice_ui_parity_overlay(before, SOURCE)
        # Round-trip only the strictly approved presentation bits, assert the
        # rest of the complete HTML (including all API and Drive JS) is identical.
        for old, new in release.VOICE_UI_MARKUP_PAIRS + release.VOICE_UI_JS_PAIRS:
            after = after.replace(new, old, 1)
        start = after.index(release.VOICE_UI_PARITY_ID)
        end = after.index(b"</style>", start) + len(b"</style>")
        after = after[:start] + after[end+1:]
        self.assertEqual(after, before)
        for script in (b"/delivery/retry", b"drive_verified_at",
                       b"drive_file_id", b"tts/api/edge",
                       b"tts/api/kokoro", b"Hide this finished job"):
            self.assertIn(script, after)

    def test_production_installer_mandates_public_html_readback(self):
        installer = inspect.getsource(release.install)
        checker = inspect.getsource(release.verify_runtime)
        self.assertIn("voice_ui_parity_overlay(patched_html, source_html)", installer)
        self.assertIn("verify_runtime()", installer)
        self.assertIn("LIVE_VOICE_UI_PARITY_MISSING", checker)
        self.assertIn("authenticated public page stayed old", checker)
        self.assertIn("VOICE_UI_PARITY_ID in full_html", checker)
        self.assertIn("body.count(b'<th scope=\"col\">Controls</th>') == 2", checker)
        self.assertIn("VOICE_UI_PARITY_ID in body", checker)


if __name__ == "__main__":
    unittest.main()

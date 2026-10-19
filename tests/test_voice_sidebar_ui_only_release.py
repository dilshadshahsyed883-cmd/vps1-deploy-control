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

    def test_fail_closed_for_unrecognized_or_duplicate_production_html(self):
        for changed in (
            self.previous.replace(OLD_ASSETS, b""),
            self.previous.replace(OLD_ASSETS, OLD_ASSETS + OLD_ASSETS),
            self.previous.replace(TITLE, b"Other Page"),
            self.previous.replace(b'id="vsEngineOmni"', b'id="missing"'),
            self.previous.replace(OLD_ASSETS, NEW_ASSETS),
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
        self.assertIn(NEW_ASSETS, voice)


if __name__ == "__main__":
    unittest.main()

"""Offline tests for minimal immutable Voice web repair; no Song cutover."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from production import voice_tenant_recovery as recovery


class VoiceTenantRecoveryTests(unittest.TestCase):
    def make_legacy(self, root):
        releases = root / "releases/voice-studio"
        previous = releases / "legacy"
        (previous / "part01").mkdir(parents=True)
        entry = b"from voice_tenant import scene_identity, set_identity, reset_identity, owner\n"
        (previous / "part01/vps1_omnivoice_app.py").write_bytes(entry)
        (previous / "part01/untouched.txt").write_text("unchanged\n")
        manifest = {"app": "voice-studio", "revision": "old",
                    "files": {"part01/vps1_omnivoice_app.py":
                              hashlib.sha256(entry).hexdigest()}}
        (previous / "_production_manifest.json").write_text(json.dumps(manifest))
        return previous, releases

    def test_exact_tenant_file_added_to_immutable_overlay_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            previous, releases = self.make_legacy(root)
            data = (b"def scene_identity(cookie): return {'username':'fixture'}\n"
                    b"def set_identity(user, admin): return None\n"
                    b"def reset_identity(tokens): return None\n")
            with patch.object(recovery, "VOICE_RELEASES", releases):
                candidate = recovery._release(previous, "a" * 40, data)
                self.assertEqual((candidate / recovery.TENANT).read_bytes(), data)
                self.assertFalse((previous / recovery.TENANT).exists())
                self.assertEqual((candidate / "part01/untouched.txt").read_text(),
                                 "unchanged\n")
                expected = hashlib.sha256(data).hexdigest()
                manifest = json.loads((candidate / "_production_manifest.json").read_text())
                self.assertEqual(manifest["files"][str(recovery.TENANT)], expected)
                self.assertEqual(recovery._release(previous, "a" * 40, data), candidate)
                with self.assertRaisesRegex(RuntimeError, "manifest differs|modified"):
                    recovery._release(previous, "a" * 40, b"different")

    def test_preexisting_tenant_file_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous, releases = self.make_legacy(Path(tmp))
            (previous / recovery.TENANT).write_bytes(b"owned")
            with patch.object(recovery, "VOICE_RELEASES", releases):
                with self.assertRaisesRegex(RuntimeError, "already contains"):
                    recovery._release(previous, "b" * 40, b"new")

    def test_source_requires_reviewed_scene_session_entrypoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            file = repo / "apps/voice-studio" / recovery.TENANT
            file.parent.mkdir(parents=True)
            file.write_text("def scene_identity(cookie): return {}\n"
                            "def set_identity(user, admin): return None\n"
                            "def reset_identity(tokens): return None\n")
            with patch.object(recovery, "REPO", repo):
                self.assertIn(b"def scene_identity(", recovery._source())
                file.write_text("def scene_identity(: syntax error\n")
                with self.assertRaises(SyntaxError):
                    recovery._source()

    def test_only_voice_web_can_be_started_in_this_recovery(self):
        source = Path(recovery.__file__).read_text()
        self.assertIn('runtime.run(["systemctl", "reset-failed", VOICE_UNIT]', source)
        self.assertIn('runtime.run(["systemctl", "start", VOICE_UNIT]', source)
        self.assertNotIn('runtime.run(["systemctl", "restart", SONG_UNIT]', source)
        self.assertNotIn("UPDATE jobs SET", source)
        self.assertNotIn("DELETE FROM jobs", source)
        self.assertIn('runtime.require_voice_release_idle', source)

    def test_runtime_dispatch_requires_protected_exact_voice_selector(self):
        repo = Path(__file__).resolve().parents[1]
        runtime = (repo / "production/runtime.py").read_text()
        workflow = (repo / ".github/workflows/vps1-song-studio-release.yml").read_text()
        self.assertIn("[vps1-voice-tenant-recovery-only]", runtime)
        self.assertIn("VOICE_TENANT_RECOVERY_ONLY", workflow)
        self.assertIn("apps=(voice-studio)", workflow)
        self.assertIn("cancel-in-progress: false", workflow)

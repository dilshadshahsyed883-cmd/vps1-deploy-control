"""Offline safety gates for the protected, source-only Voice recovery lane."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from production import song_audio_voice_recovery as recovery


def original_state():
    return {
        "voice_pointer": "LEGACY_OWNED",
        "scene_pointer": "LEGACY_OWNED",
        "song_pointer": "NOT_A_SYMLINK",
        "voice_split": "UNSET",
        "scene_song_route": "UNSET",
        "voice_health": "UNAVAILABLE",
        "voice_missing_python_module": "voice_tenant",
        "voice_recent_error_classes": {"ModuleNotFoundError": True},
        "voice_unit": {"active": "failed"},
        "scene_unit": {"active": "active"},
        "song_unit": {"active": "inactive", "pid_present": False},
    }


class VoiceRecoveryTests(unittest.TestCase):
    def test_proven_missing_module_allows_readonly_recovery_preflight(self):
        with patch.object(recovery.diagnostic, "report", return_value=original_state()), \
             patch.object(recovery.runtime, "free_port") as unused:
            result = recovery._require_safe_live_state()
            self.assertEqual(result["voice_missing_python_module"], "voice_tenant")
            unused.assert_called_once_with(8152)

    def test_cannot_recover_wrong_missing_module_split_worker_or_scene_route(self):
        overrides = [
            ("voice_missing_python_module", "queue_actions"),
            ("voice_missing_python_module", "UNKNOWN"),
            ("voice_split", "SEPARATED"),
            ("scene_song_route", "DEDICATED"),
            ("voice_pointer", "SONG_AUDIO_OVERLAY"),
            ("scene_pointer", "OUTSIDE_OWNER"),
            ("voice_health", "HTTP_500"),
        ]
        for field, bad in overrides:
            with self.subTest(field=field, value=bad):
                state = original_state()
                state[field] = bad
                with patch.object(recovery.diagnostic, "report", return_value=state), \
                     patch.object(recovery.runtime, "free_port"), \
                     self.assertRaises(RuntimeError):
                    recovery._require_safe_live_state()
        state = original_state()
        state["song_unit"] = {"active": "active", "pid_present": True}
        with patch.object(recovery.diagnostic, "report", return_value=state), \
             patch.object(recovery.runtime, "free_port"), \
             self.assertRaisesRegex(RuntimeError, "independent Song"):
            recovery._require_safe_live_state()
        state = original_state()
        state["voice_unit"]["active"] = "active"
        with patch.object(recovery.diagnostic, "report", return_value=state), \
             patch.object(recovery.runtime, "free_port"), \
             self.assertRaisesRegex(RuntimeError, "proven"):
            recovery._require_safe_live_state()

    def test_existing_healthy_voice_is_readonly_noop_state(self):
        state = original_state()
        state["voice_unit"]["active"] = "active"
        state["voice_health"] = "HTTP_200_OK"
        with patch.object(recovery.diagnostic, "report", return_value=state), \
             patch.object(recovery.runtime, "free_port"):
            self.assertEqual(recovery._require_safe_live_state(), state)

    def test_only_missing_frozen_companions_stage_without_modifying_previous(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "legacy"
            (old / "part01").mkdir(parents=True)
            code = root / "repo/apps/voice-studio/part01"
            code.mkdir(parents=True)
            (code / "voice_tenant.py").write_bytes(b"# reviewed identity\n")
            (code / "queue_actions.py").write_bytes(b"# reviewed queue\n")
            (old / "part01/queue_actions.py").write_bytes(b"# old queue: do not overwrite\n")
            with patch.object(recovery.runtime, "REPO", root / "repo"):
                data = recovery._reviewed_missing_files(old)
                self.assertEqual(data, {"part01/voice_tenant.py": b"# reviewed identity\n"})
                self.assertEqual((old / "part01/queue_actions.py").read_bytes(),
                                 b"# old queue: do not overwrite\n")
                (old / "part01/voice_tenant.py").write_bytes(b"# existing identity\n")
                with self.assertRaisesRegex(RuntimeError, "not confirmed"):
                    recovery._reviewed_missing_files(old)
                (old / "part01/voice_tenant.py").unlink()
                (old / "part01/queue_actions.py").unlink()
                data = recovery._reviewed_missing_files(old)
                self.assertEqual(set(data), set(recovery.COMPANIONS))
                (old / "part01/voice_tenant.py").symlink_to(code / "voice_tenant.py")
                with self.assertRaisesRegex(RuntimeError, "unsafe"):
                    recovery._reviewed_missing_files(old)

    def test_disposable_candidate_import_uses_existing_voice_venv_without_job_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            candidate = Path(tmp)
            python = candidate / "approved-voice-python"
            with patch.object(recovery.cutover, "_verify_voice_candidate_modules") as verified, \
                 patch.object(recovery.runtime, "run") as runner:
                recovery._test_candidate_import(candidate, python)
                verified.assert_called_once_with(candidate)
                args = runner.call_args.args[0]
                self.assertEqual(args[:5], ["runuser", "-u", "ubuntu", "--", "/usr/bin/env"])
                self.assertIn("FRESH_WEBSITE_SMOKE_TEST=1", args)
                self.assertIn("SONG_AUDIO_BACKEND_SPLIT=0", args)
                self.assertIn("PYTHONDONTWRITEBYTECODE=1", args)
                self.assertIn("import vps1_omnivoice_app; assert vps1_omnivoice_app.app is not None", args)
                self.assertNotIn("systemctl", args)
                self.assertEqual(runner.call_args.kwargs["cwd"], candidate / "part01")

    def test_recovery_does_not_touch_service_without_exact_intent_or_live_proof(self):
        with patch.object(recovery.runtime, "check_host"), \
             patch.object(recovery.runtime, "run", return_value="other protected commit"), \
             patch.object(recovery.runtime, "atomic_link") as link:
            with self.assertRaisesRegex(RuntimeError, "exclusive"):
                recovery.recover("a" * 40)
            link.assert_not_called()
        with patch.object(recovery.runtime, "check_host"), \
             patch.object(recovery.runtime, "run",
                          return_value="[vps1-song-audio-voice-recovery]\n[vps1-song-audio-separate]"), \
             patch.object(recovery.runtime, "atomic_link") as link:
            with self.assertRaisesRegex(RuntimeError, "exclusive"):
                recovery.recover("a" * 40)
            link.assert_not_called()
        with patch.object(recovery.runtime, "check_host"), \
             patch.object(recovery.runtime, "run",
                          return_value="[vps1-song-audio-voice-recovery]"), \
             patch.object(recovery, "_require_safe_live_state",
                          side_effect=RuntimeError("worker is busy")), \
             patch.object(recovery.runtime, "atomic_link") as link:
            with self.assertRaisesRegex(RuntimeError, "busy"):
                recovery.recover("a" * 40)
            link.assert_not_called()

    def test_unit_ownership_rejects_incorrect_workdir_before_any_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            unit = Path(tmp) / "voice.service"
            unit.write_text("WorkingDirectory=/unsafe\n"
                            "ExecStart=/suspicious/python -m uvicorn app:app\n")
            with patch.object(recovery.cutover, "_verify_pointer",
                              return_value=Path(tmp) / "old"):
                with self.assertRaisesRegex(RuntimeError, "not pointer-owned"):
                    recovery._safe_source_and_unit(unit)
            unit.write_text("WorkingDirectory=/srv/fresh-website/current/voice-studio/part01\n"
                            "ExecStart=/suspicious/python -m uvicorn app:app\n")
            with patch.object(recovery.cutover, "_verify_pointer",
                              return_value=Path(tmp) / "old"):
                with self.assertRaisesRegex(RuntimeError, "unapproved"):
                    recovery._safe_source_and_unit(unit)

    def test_recovery_selector_keeps_serialized_tailscale_and_is_not_cutover(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/vps1-song-studio-release.yml").read_text()
        runtime = (root / "production/runtime.py").read_text()
        for marker in ("SONG_AUDIO_RECOVER_ONLY", "[vps1-song-audio-voice-recovery]",
                       "cancel-in-progress: false", "StrictHostKeyChecking=yes",
                       "github.actor == 'dilshadshahsyed883-cmd'",
                       "apps=(voice-studio)"):
            self.assertIn(marker, workflow)
        self.assertIn("from production.song_audio_voice_recovery import recover", runtime)
        self.assertIn("Voice identity recovery requires exact protected Voice lane", runtime)
        self.assertNotIn("[vps1-song-audio-separate]", recovery.RECOVERY_MARKER)

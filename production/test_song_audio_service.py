"""Offline safety contract for independent Song Audio systemd preparation."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sqlite3

from production import song_audio_service as service


SHA = "a" * 40


class SongAudioServiceTests(unittest.TestCase):
    def test_hardened_unit_uses_exact_song_entry_and_root(self):
        root = Path("/var/lib/fresh-website/voice-studio/audio-downloader")
        body = service.render_unit(SHA, root)
        self.assertIn("song_audio_app:app --host 127.0.0.1 --port 8152 --workers 1", body)
        self.assertIn("WorkingDirectory=/srv/fresh-website/current/song-studio/part01", body)
        self.assertIn("ReadWritePaths=" + str(root) + " ", body)
        self.assertIn("ProtectSystem=strict", body)
        self.assertIn("User=ubuntu", body)
        self.assertNotIn("vps1_omnivoice_app", body)
        env = service.render_env(root)
        self.assertEqual(env.count("AUDIO_DOWNLOADER_ROOT="), 1)
        self.assertIn("SONG_AUDIO_CUTOVER_ACTIVE=1", env)
        self.assertNotIn("PASSWORD=", env)

    def test_unsafe_revision_or_data_root_rejected(self):
        for sha in ("", "bad", "a" * 39, "g" * 40):
            with self.assertRaises(ValueError):
                service.render_unit(sha, Path("/var/lib/fresh-website/voice-studio/audio-downloader"))
        for bad in (Path("/"), Path("relative"), Path("/tmp/folder name")):
            with self.assertRaises(ValueError):
                service.render_unit(SHA, bad)
            with self.assertRaises(ValueError):
                service.render_env(bad)

    def test_readonly_preflight_requires_real_busy_queues_to_be_idle(self):
        canonical = Path("/var/lib/fresh-website/voice-studio/audio-downloader")
        with (
            patch.object(service, "canonical_root", return_value=canonical),
            patch.object(service, "VOICE_ENV") as voice_env,
            patch.object(service, "SCENE_ENV") as scene_env,
            patch.object(service.subprocess, "run") as run,
            patch("production.song_audio_handoff_preflight.require_drain") as drain,
            patch.object(service.runtime, "free_port") as free,
        ):
            voice_env.is_file.return_value = True
            scene_env.is_file.return_value = True
            voice_env.is_symlink.return_value = False
            scene_env.is_symlink.return_value = False
            run.return_value.returncode = 0
            result = service.preflight()
            drain.assert_called_once_with(service.runtime.DATA / "voice-studio",
                                         canonical / "data/jobs.sqlite3")
            free.assert_called_once_with(8152)
            self.assertFalse(result["route_cutover_performed"])
            drain.side_effect = RuntimeError("Audio Downloader has active jobs")
            with self.assertRaisesRegex(RuntimeError, "active jobs"):
                service.preflight()

    def test_generic_installer_cannot_start_song_without_coordinated_cutover(self):
        from production.runtime import CATALOG, SONG_AUDIO_SPEC, install_app
        self.assertNotIn("song-studio", CATALOG)
        self.assertEqual(SONG_AUDIO_SPEC["port"], 8152)
        with self.assertRaisesRegex(RuntimeError, "atomic Voice/Scene cutover"):
            install_app("song-studio", SHA)

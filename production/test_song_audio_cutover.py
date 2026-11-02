"""Pure offline tests for the guarded Voice -> Song Audio production transaction."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, MagicMock, patch

from production import song_audio_cutover as cutover
from production import runtime

REPO = Path(__file__).resolve().parents[1]


class SongAudioCutoverTests(unittest.TestCase):
    def test_scene_transform_uses_exact_reviewed_song_upstream_only(self):
        source = (REPO / "apps/scene-builder/part01/app.py").read_text()
        anchor = '_VS_UPSTREAM="http://127.0.0.1:8143"\n'
        route = '\n@app.get("/voice-studio", response_class=HTMLResponse)'
        start = source.index(anchor) + len(anchor)
        end = source.index(route, start)
        added = source[start:end]
        self.assertIn("_song_audio_upstream", added)
        legacy = source.replace(anchor + added, anchor, 1)
        legacy = legacy.replace(
            '_vs_requests.get, _song_audio_upstream() + "/audio-downloader/app"',
            '_vs_requests.get, "http://127.0.0.1:8143/audio-downloader/app"', 1)
        legacy = legacy.replace(
            'url=(_song_audio_upstream() if studio == "song" else _VS_UPSTREAM)+"/"+upstream_path.lstrip("/")',
            'url=_VS_UPSTREAM+"/"+upstream_path.lstrip("/")', 1)
        self.assertNotIn("_song_audio_upstream", legacy)
        changed = cutover._patch_scene_source(legacy.encode(), source.encode())
        self.assertEqual(changed, source.encode())
        self.assertEqual(cutover._patch_scene_source(changed, source.encode()), changed)
        with self.assertRaisesRegex(RuntimeError, "drift"):
            cutover._patch_scene_source(
                legacy.replace('_VS_UPSTREAM="http://127.0.0.1:8143"',
                               '_VS_UPSTREAM="http://public.example"').encode(),
                source.encode())

    def test_environment_update_preserves_secrets_and_denies_duplicate_controls(self):
        original = b"WEB_USERNAME=team\nWEB_ACCESS_KEY=private-fixture\n"
        after = cutover._patch_env(original, {"SONG_AUDIO_SPLIT_UPSTREAM": "http://127.0.0.1:8152"})
        self.assertIn(b"WEB_ACCESS_KEY=private-fixture\n", after)
        self.assertEqual(after.count(b"SONG_AUDIO_SPLIT_UPSTREAM="), 1)
        self.assertEqual(cutover._patch_env(after, {
            "SONG_AUDIO_SPLIT_UPSTREAM": "http://127.0.0.1:8152"}), after)
        with self.assertRaisesRegex(RuntimeError, "Ambiguous"):
            cutover._patch_env(
                b"SONG_AUDIO_BACKEND_SPLIT=0\nSONG_AUDIO_BACKEND_SPLIT=1\n",
                {"SONG_AUDIO_BACKEND_SPLIT": "1"})
        with self.assertRaises(ValueError):
            cutover._patch_env(original, {"SONG_AUDIO_SPLIT_UPSTREAM": "evil\nINJECT=yes"})

    def test_song_env_migrates_only_legacy_audio_settings_not_voice_secrets(self):
        root = Path("/var/lib/fresh-website/voice-studio/audio-downloader")
        old = (b"AUDIO_DOWNLOADER_ROOT=/var/lib/fresh-website/voice-studio/audio-downloader\n"
               b"AUDIO_DOWNLOADER_PROXY_POOL=host-private-fixture\n"
               b"AUDIO_DOWNLOADER_YTDLP_BIN=/obsolete/yt-dlp\n"
               b"WEB_ACCESS_KEY=never-export-this\n"
               b"VOICE_CLONE_ROOT=/private/voice\n")
        new = cutover._song_environment(root, "a" * 40, old)
        self.assertIn(b"AUDIO_DOWNLOADER_PROXY_POOL=host-private-fixture\n", new)
        self.assertEqual(new.count(b"AUDIO_DOWNLOADER_ROOT="), 1)
        self.assertEqual(new.count(b"AUDIO_DOWNLOADER_YTDLP_BIN="), 1)
        self.assertNotIn(b"WEB_ACCESS_KEY", new)
        self.assertNotIn(b"VOICE_CLONE_ROOT", new)
        with self.assertRaisesRegex(RuntimeError, "Duplicated"):
            cutover._song_environment(root, "a" * 40,
                                      b"AUDIO_DOWNLOADER_PROXY_POOL=one\n"
                                      b"AUDIO_DOWNLOADER_PROXY_POOL=two\n")

    def test_only_verified_live_song_worker_exempts_voice_release_from_song_queue(self):
        with tempfile.TemporaryDirectory() as td:
            conf = Path(td)
            voice = conf / "production-voice-studio.env"
            song = conf / "production-song-studio.env"
            scene = conf / "scene-builder.env"
            with patch.object(runtime, "CONFIG", conf):
                self.assertFalse(runtime.song_audio_independent_live())
                voice.write_text("SONG_AUDIO_BACKEND_SPLIT=0\n")
                self.assertFalse(runtime.song_audio_independent_live())
                voice.write_text(
                    "SONG_AUDIO_BACKEND_SPLIT=1\n"
                    "AUDIO_DOWNLOADER_ROOT=/var/lib/fresh-website/voice-studio/audio-downloader\n")
                with self.assertRaises(RuntimeError):
                    runtime.song_audio_independent_live()
                song.write_text(
                    "SONG_AUDIO_CUTOVER_ACTIVE=1\n"
                    "AUDIO_DOWNLOADER_ROOT=/var/lib/fresh-website/voice-studio/audio-downloader\n")
                scene.write_text("SONG_AUDIO_SPLIT_UPSTREAM=http://127.0.0.1:8152\n")
                opener = Mock()
                response = MagicMock(status=200)
                response.__enter__.return_value = response
                response.read.return_value = b'{"ok":true,"worker_active":true,"smoke":false}'
                opener.open.return_value = response
                with patch.object(runtime.subprocess, "run", return_value=Mock(returncode=0)), \
                     patch.object(runtime.urllib.request, "build_opener", return_value=opener):
                    self.assertTrue(runtime.song_audio_independent_live())
                    response.read.return_value = b'{"ok":true,"worker_active":false,"smoke":false}'
                    with self.assertRaisesRegex(RuntimeError, "not proven active"):
                        runtime.song_audio_independent_live()
                scene.write_text("SONG_AUDIO_SPLIT_UPSTREAM=http://127.0.0.1:8143\n")
                with self.assertRaisesRegex(RuntimeError, "dedicated service"):
                    runtime.song_audio_independent_live()

    def test_voice_preflight_requires_synthesis_idle_but_not_song_idle_after_split(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name in ("kokoro", "edge"):
                db = root / name / "data" / (name + "_jobs.sqlite3")
                db.parent.mkdir(parents=True)
                with sqlite3.connect(db) as conn:
                    conn.execute("CREATE TABLE jobs(status TEXT, delivery_status TEXT)")
            with patch.object(runtime, "song_audio_independent_live", return_value=True) as verified:
                runtime.require_voice_release_idle(root)
                verified.assert_called_once_with()
                with self.assertRaisesRegex(RuntimeError, "preflight DB unavailable"):
                    runtime.require_voice_release_idle(root, audio_db=root / "missing.sqlite3")

    def test_manifest_scoped_overlay_keeps_unrelated_files_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            previous = root / "releases" / "old"
            (previous / "part01").mkdir(parents=True)
            (previous / "part01/app.py").write_bytes(b"original")
            (previous / "part01/other.py").write_bytes(b"preserve")
            manifest = {"app": "scene-builder", "revision": "old",
                        "files": {"part01/app.py": hashlib.sha256(b"original").hexdigest(),
                                  "part01/other.py": hashlib.sha256(b"preserve").hexdigest()}}
            (previous / "_scene_candidate_manifest.json").write_text(json.dumps(manifest))
            target = root / "releases" / "new"
            cutover._clone_overlay(previous, target, {"part01/app.py": b"updated"},
                                   "_scene_candidate_manifest.json", "scene-builder", "a" * 40)
            self.assertEqual((previous / "part01/app.py").read_bytes(), b"original")
            self.assertEqual((target / "part01/app.py").read_bytes(), b"updated")
            self.assertEqual((target / "part01/other.py").read_bytes(), b"preserve")
            with self.assertRaisesRegex(RuntimeError, "already exists"):
                cutover._clone_overlay(previous, target, {"part01/app.py": b"updated"},
                                       "_scene_candidate_manifest.json", "scene-builder", "a" * 40)

    def test_release_is_explicit_protected_voice_only_intent(self):
        runtime = (REPO / "production/runtime.py").read_text()
        workflow = (REPO / ".github/workflows/vps1-song-studio-release.yml").read_text()
        self.assertIn("if '[vps1-song-audio-separate]' in message:", runtime)
        self.assertIn("install_song_audio(revision)", runtime)
        self.assertIn("SONG_AUDIO_SEPARATE", workflow)
        self.assertIn("apps=(voice-studio)", workflow)
        self.assertIn("StrictHostKeyChecking=yes", workflow)
        self.assertIn("vps1-production-deploy install $SOURCE_SHA $1", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertNotIn("pull_request_target", workflow)
        self.assertIn("runtime.require_voice_release_idle", (REPO / "production/song_audio_cutover.py").read_text())

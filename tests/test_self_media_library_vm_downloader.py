"""Video Merger downloader parity tests for SML's independent video-only intake."""
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / "apps/self-media-library/part01"
sys.path.insert(0, str(ROOT))
import video_merger_downloader as d


class DownloadStrategy(unittest.TestCase):
    def test_format_and_video_merger_clients_are_kept(self):
        code = (ROOT / "video_merger_downloader.py").read_text()
        original = (ROOT.parents[1] / "video-merger/part01/app.py").read_text()
        for token in ("bv*+ba/best/bv*", "tv,web_safari", "--merge-output-format",
                      "--no-playlist", "YOUTUBE_AUTH_REQUIRED", "YOUTUBE_RATE_LIMIT"):
            self.assertIn(token, code)
            self.assertIn(token, original)
        media = (ROOT / "media_extensions.py").read_text()
        self.assertIn("download_youtube_video(", media)
        self.assertIn("persist_image(", media)
        self.assertIn("INSERT INTO sources(", media)
        self.assertIn("Imported; video sources require Process", media)

    def test_no_implicit_proxy_route_and_opt_in_proxy_only(self):
        with patch.dict(os.environ, {"SML_YTDLP_PROXY_URL": "http://user:secret@vm.example:8080"}):
            self.assertEqual(d._proxy_routes(False), [None])
        with patch.dict(os.environ, {"SML_YTDLP_PROXY_URL": ""}):
            with patch.object(d, "_read_active_profile", return_value=None):
                self.assertFalse(d.proxy_available())
                with self.assertRaisesRegex(RuntimeError, "Proxy profile is not configured"):
                    d._proxy_routes(True)

    def test_video_merger_readonly_fallback_for_authorized_proxy(self):
        vm = {"id": "vm", "host": "vm.example", "port": 8080,
              "username": "vmowner", "password": "vm-secret"}
        audio = {"id": "audio", "host": "audio.example", "port": 8080,
                 "username": "audowner", "password": "audio-secret"}
        with patch.dict(os.environ, {"SML_YTDLP_PROXY_URL": ""}):
            with patch.object(d, "_read_active_profile",
                              side_effect=lambda p: vm if p == d.VIDEO_PROXY_FILE else audio):
                routes = d._proxy_routes(True)
                self.assertEqual(len(routes), 2)
                self.assertIn("vm.example", routes[0])
                self.assertIn("audio.example", routes[1])
                self.assertNotIn("vm-secret", d._redact("failure " + routes[0], routes))
                self.assertEqual(d._proxy_routes(False), [None])

    def test_auth_challenge_uses_one_alternate_playback_client(self):
        attempts = []
        def fake(*args, **kwargs):
            attempts.append(args[3])
            if len(attempts) == 1:
                raise RuntimeError("Sign in to confirm you are not a bot")
            path = Path(args[1]) / "video.mp4"
            return path
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(d, "_proxy_routes", return_value=[None]), \
                 patch.object(d, "_cookie_file", return_value=None), \
                 patch.object(d, "_run_attempt", side_effect=fake):
                result = d.download_youtube_video("https://youtu.be/jNQXAC9IVRw",
                                                   Path(tmp), lambda *_: None, use_proxy=False)
        self.assertEqual(result.name, "video.mp4")
        self.assertEqual(attempts, ["default", "tv,web_safari"])

    def test_auth_challenge_proxy_fallback_does_not_modify_profiles(self):
        attempts = []
        def fake(*args, **kwargs):
            attempts.append((args[2], args[3]))
            if len(attempts) < 3:
                raise RuntimeError("Sign in to confirm you are not a bot")
            return Path(args[1]) / "video.mp4"
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(d, "_proxy_routes", return_value=["http://vm", "http://audio"]), \
                 patch.object(d, "_cookie_file", return_value=None), \
                 patch.object(d, "_run_attempt", side_effect=fake):
                d.download_youtube_video("https://youtu.be/jNQXAC9IVRw",
                                         Path(tmp), lambda *_: None, use_proxy=True)
        self.assertEqual(attempts, [
            ("http://vm", "default"), ("http://vm", "tv,web_safari"),
            ("http://audio", "default")])

    def test_429_does_not_issue_repeated_requests(self):
        attempts = []
        def fake(*args, **kwargs):
            attempts.append(1)
            raise RuntimeError("HTTP Error 429 Too Many Requests")
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(d, "_proxy_routes", return_value=[None]), \
                 patch.object(d, "_cookie_file", return_value=None), \
                 patch.object(d, "_run_attempt", side_effect=fake):
                with self.assertRaisesRegex(RuntimeError, "YOUTUBE_RATE_LIMIT"):
                    d.download_youtube_video("https://youtu.be/jNQXAC9IVRw",
                                             Path(tmp), lambda *_: None, use_proxy=False)
        self.assertEqual(len(attempts), 1)

    def test_audio_only_is_rejected_by_ffprobe_gate(self):
        class FakeProcess:
            def __init__(self, args, **kwargs):
                self.args = args
                self.stdout = iter(["download:  40.0%", "download:100%"])
                output = Path(args[args.index("-o") + 1].replace("%(ext)s", "mp4"))
                output.write_bytes(b"fixture")
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def wait(self): return 0
            def kill(self): pass
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "attempt-1"
            with patch.object(d.subprocess, "Popen", FakeProcess), \
                 patch.object(d, "probe_video", return_value={"width":0,"height":0,"codec":""}), \
                 patch.object(d, "_js_runtime", return_value="node:/usr/bin/node"):
                with self.assertRaisesRegex(RuntimeError, "audio-only"):
                    d._run_attempt("https://youtu.be/abc", target, None, "default",
                                   None, lambda *_: None, 10)

    def test_read_only_proxy_requires_private_owner_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "proxy.json"
            cfg.write_text(json.dumps({"enabled":True,"active_id":"a",
                "profiles":[{"id":"a","host":"vm.example","port":8080,
                             "username":"user","password":"secret"}]}))
            cfg.chmod(0o644)
            self.assertIsNone(d._read_active_profile(cfg))
            cfg.chmod(0o600)
            self.assertEqual(d._read_active_profile(cfg)["id"],"a")
            symlink = Path(tmp) / "link.json"
            symlink.symlink_to(cfg)
            self.assertIsNone(d._read_active_profile(symlink))


if __name__ == "__main__":
    unittest.main()

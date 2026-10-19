"""App and engine status only: no user-data, OAuth, queue, or history access."""
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / "apps" / "local-upload-manager" / "part01"
sys.path.insert(0, str(ROOT))
from lum_engine.mode_runtime import probe


class FakeResponse:
    def __init__(self, body, code=200):
        self.status = code
        self.body = io.BytesIO(json.dumps(body).encode("utf-8"))
    def __enter__(self): return self
    def __exit__(self, *_args): return False
    def read(self, count): return self.body.read(count)


class EngineRuntimeTests(unittest.TestCase):
    def test_engine_one_is_current_authenticated_app(self):
        with patch("urllib.request.urlopen", side_effect=AssertionError("no network")):
            self.assertEqual(probe("engine1")["web"], "connected")

    def test_engine_two_service_ready_but_no_publish_claim(self):
        with patch("urllib.request.urlopen", return_value=FakeResponse({
            "ok": True, "app": "Local Upload Manager", "worker_alive": False
        })) as request:
            result = probe("engine2")
            self.assertEqual(result["web"], "connected")
            self.assertEqual(result["worker"], "stopped")
            self.assertIn("127.0.0.1:8016/local-upload-manager/health", request.call_args.args[0].full_url)
            self.assertNotIn("publishing", result.get("worker", ""))

    def test_shorts_worker_ready_is_separate_from_website(self):
        with patch("urllib.request.urlopen", return_value=FakeResponse({
            "ok": True, "app": "Local Upload Manager", "worker_alive": True
        })):
            self.assertEqual(probe("shorts")["worker"], "running")

    def test_short_stream_is_distinct_service(self):
        with patch("urllib.request.urlopen", return_value=FakeResponse({
            "ok": True, "app": "short-stream"
        })) as request:
            result = probe("short_stream")
            self.assertEqual(result["web"], "connected")
            self.assertEqual(result["worker"], "separate_stream_service")
            self.assertIn("127.0.0.1:8145/health", request.call_args.args[0].full_url)

    def test_port_validation_denies_outbound_requests(self):
        with patch.dict(os.environ, {"LUM_MODE_ENGINE2_PORT": "not-a-port"}), \
             patch("urllib.request.urlopen", side_effect=AssertionError("no network")):
            self.assertEqual(probe("engine2")["web"], "not_configured")

    def test_wrong_app_cannot_be_misreported_connected(self):
        with patch("urllib.request.urlopen", return_value=FakeResponse({
            "ok": True, "app": "unrelated-service", "worker_alive": True
        })):
            self.assertEqual(probe("engine2")["web"], "not_connected")

    def test_unavailable_service_gracefully_indicates_not_connected(self):
        with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
            self.assertEqual(probe("shorts")["web"], "not_connected")

    def test_no_unknown_mode_is_allowed(self):
        with self.assertRaises(ValueError):
            probe("../secrets")


if __name__ == "__main__":
    unittest.main()

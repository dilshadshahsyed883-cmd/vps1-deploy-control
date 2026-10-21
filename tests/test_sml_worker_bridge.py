"""Scene Builder SML-first worker regressions without external services."""
import hashlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
FILE = ROOT / "apps/scene-builder/part02/sml_worker_bridge.py"
SPEC = importlib.util.spec_from_file_location("sml_worker_bridge", FILE)
sml = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sml)


def clip(ident="CLP-A123", keywords=None, description="winter snow mountain"):
    return {
        "id": ident, "type": "video", "description": description,
        "content_url": f"/api/v1/media/video/{ident}/content",
        "keywords": keywords or ["snow", "mountain"],
        "quality": 82, "duration": 3.1, "sha256": "",
    }


class SMLWorkerBridgeTests(unittest.TestCase):
    def setUp(self):
        self.client = sml.SMLWorkerClient(
            origin="http://127.0.0.1:8147", token="fake-test-token")

    def test_auth_is_required(self):
        client = sml.SMLWorkerClient(origin="http://127.0.0.1:8147", token="")
        self.assertFalse(client.enabled)
        with self.assertRaises(sml.SMLMediaUnavailable):
            client.ranked("snow mountain")

    def test_invalid_origin_rejected(self):
        for origin in ("file:///etc/passwd", "http://u:p@127.0.0.1:8147",
                       "https://127.0.0.1:8147/path"):
            with self.subTest(origin=origin):
                with self.assertRaises(sml.SMLMediaUnavailable):
                    sml.SMLWorkerClient(origin=origin, token="test")

    def test_positive_match_before_stock(self):
        unrelated = clip(ident="CLP-B456", keywords=["office"], description="business desk")
        with patch.object(self.client, "_json", return_value={"items": [unrelated, clip()]}):
            matches = self.client.ranked("snow mountain")
        self.assertEqual([c["id"] for c in matches], ["CLP-A123"])

    def test_no_match_means_pexels_fallback(self):
        with patch.object(self.client, "_json", return_value={"items": [clip()]}):
            self.assertEqual(self.client.ranked("underwater coral"), [])

    def test_no_duplicates(self):
        with patch.object(self.client, "_json", return_value={"items": [clip()]}):
            self.assertEqual(self.client.ranked("snow", used_ids={"CLP-A123"}), [])

    def test_invalid_remote_reference_not_returned(self):
        row = clip()
        row["content_url"] = "https://another-host.example/file"
        with patch.object(self.client, "_json", return_value={"items": [row]}):
            self.assertEqual(self.client.ranked("snow"), [])

    def test_reservation_uses_exact_approved_id(self):
        item = {"id": "CLP-A123", "reservation_id": "RES-" + "c" * 32}
        with patch.object(self.client, "_json", return_value={"items": [item]}) as api:
            reservation = self.client.reserve("JOB-1", "SCENE_001", "CLP-A123")
        self.assertEqual(reservation, item["reservation_id"])
        self.assertEqual(api.call_args.kwargs["payload"]["preferred_ids"], ["CLP-A123"])

    def test_mismatch_reservation_never_used(self):
        item = {"id": "CLP-OTHER", "reservation_id": "RES-" + "c" * 32}
        with patch.object(self.client, "_json", return_value={"items": [item]}):
            with self.assertRaises(sml.SMLMediaUnavailable):
                self.client.reserve("JOB-1", "SCENE_001", "CLP-A123")

    def test_hash_checked_download(self):
        data = b"media test bytes"
        candidate = clip()
        candidate["sha256"] = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(sml.urllib.request, "urlopen", return_value=io.BytesIO(data)):
                target = Path(folder) / "job.mp4"
                self.assertEqual(self.client.download(candidate, target),
                                 hashlib.sha256(data).hexdigest())
                self.assertEqual(target.read_bytes(), data)

    def test_failed_download_cleans_partial_and_never_commits(self):
        candidate = clip()
        candidate["sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(sml.urllib.request, "urlopen",
                              return_value=io.BytesIO(b"wrong-hash")):
                target = Path(folder) / "job.mp4"
                with self.assertRaises(sml.SMLMediaUnavailable):
                    self.client.download(candidate, target)
                self.assertFalse(target.exists())
                self.assertFalse(Path(str(target) + ".partial").exists())

    def test_no_api_tokens_committed(self):
        worker = (ROOT / "apps/scene-builder/part02/api_server.py").read_text()
        web = (ROOT / "apps/scene-builder/part01/app.py").read_text()
        self.assertIn("_try_sml_first_scene(", worker)
        self.assertIn("SML Media", worker)
        self.assertIn("sml_auto_scene_matching", worker)
        self.assertIn("sml-select", web)
        self.assertNotIn("real-api-key", worker)

    def test_sml_v2_respects_exact_approved_reservation(self):
        extensions = (ROOT / "apps/self-media-library/part01/media_extensions.py").read_text()
        self.assertIn("preferred_ids", extensions)
        self.assertIn("check_editor_token(x_sml_editor_token)", extensions)


if __name__ == "__main__":
    unittest.main()

"""Self Media Library phase-1 import dedup and frame-quality regressions."""
from __future__ import annotations
import ast
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

APP = Path(__file__).resolve().parents[1] / "apps/self-media-library/part01"
sys.path.insert(0, str(APP))
from media_integrity import FRAME_BYTES, quality_pixels, sha256_file, score_clip


class QualityUnit(unittest.TestCase):
    def test_exact_hash_streaming(self):
        with tempfile.TemporaryDirectory() as root:
            a = Path(root) / "a"
            b = Path(root) / "b"
            a.write_bytes(b"a" * (1024 * 1024 + 3))
            b.write_bytes(a.read_bytes())
            self.assertEqual(sha256_file(a), sha256_file(b))
            b.write_bytes(b"b")
            self.assertNotEqual(sha256_file(a), sha256_file(b))

    def test_blank_black_white_and_valid_frames(self):
        self.assertEqual(quality_pixels(bytes(FRAME_BYTES)), (5, True))
        self.assertEqual(quality_pixels(bytes([255]) * FRAME_BYTES), (5, True))
        score, blank = quality_pixels(bytes([v % 256 for v in range(FRAME_BYTES)]))
        self.assertFalse(blank)
        self.assertGreaterEqual(score, 40)
        with self.assertRaises(ValueError):
            quality_pixels(b"short")

    def test_source_contract_and_compatibility(self):
        source = (APP / "app.py").read_text()
        ast.parse(source)
        self.assertIn('ensure_column("sources", "sha256"', source)
        self.assertIn('ensure_column("clips", "sha256"', source)
        self.assertIn('setting_true(settings, "duplicate_detection"', source)
        self.assertIn('setting_true(settings, "quality_filter"', source)
        ext = (APP / "media_extensions.py").read_text()
        self.assertIn("sha256_file(output)", ext)


@unittest.skipUnless(shutil.which("ffmpeg") and importlib.util.find_spec("fastapi") is not None,
                     "FFmpeg/FastAPI integration dependencies required")
class SourceDedupIntegration(unittest.TestCase):
    def test_local_video_upload_dedup_and_blank_quality(self):
        from fastapi.testclient import TestClient
        import io
        with tempfile.TemporaryDirectory(prefix="sml-integrity-") as root:
            previous = os.environ.get("SML_DATA_ROOT")
            os.environ["SML_DATA_ROOT"] = root
            try:
                sys.modules.pop("app", None)
                import app
                video = Path(root) / "blank.mp4"
                subprocess.run(
                    ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                     "-i", "color=c=black:s=128x72:r=24:d=3",
                     "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)],
                    check=True, timeout=45,
                )
                value, blank = score_clip(video, 1)
                self.assertTrue(blank)
                self.assertEqual(value, 5)
                data = video.read_bytes()
                with TestClient(app.app) as client:
                    a = client.post("/api/sources/upload", files={"file": ("x.mp4", io.BytesIO(data), "video/mp4")})
                    b = client.post("/api/sources/upload", files={"file": ("x.mp4", io.BytesIO(data), "video/mp4")})
                    self.assertEqual(a.status_code, 200, a.text)
                    self.assertEqual(b.status_code, 200, b.text)
                    self.assertEqual(a.json()["id"], b.json()["id"])
                    self.assertTrue(b.json()["duplicate"])
                    self.assertEqual(len(client.get("/api/sources").json()["items"]), 1)
                    # Process synchronously to assert the quality toggle applies to created clips.
                    app.process_source(a.json()["id"])
                    clips = client.get("/api/clips").json()["items"]
                    self.assertGreaterEqual(len(clips), 1)
                    self.assertTrue(all(c["status"] == "rejected" for c in clips))
                    self.assertTrue(all(c["quality"] == 5 for c in clips))
            finally:
                sys.modules.pop("app", None)
                if previous is None:
                    os.environ.pop("SML_DATA_ROOT", None)
                else:
                    os.environ["SML_DATA_ROOT"] = previous

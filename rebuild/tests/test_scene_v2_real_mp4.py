"""Real, offline, no-queue V2 FFmpeg render integration test.

No live VPS, production credentials, queue DB mutation, browser, or external media.
Exercises the restored owner-sourced V2 renderer and validates the MP4 by FFprobe.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
PART2 = ROOT / "apps/scene-builder/part02"
sys.path.insert(0, str(PART2))


def ffmpeg(*args):
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", *map(str, args)],
        capture_output=True, text=True, timeout=25,
    )
    if result.returncode:
        raise RuntimeError("Synthetic fixture FFmpeg error: " + result.stderr[-500:])


class V2RendererRealIntegration(unittest.TestCase):
    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools absent")
    def test_real_render_final_mp4_h264_aac_probe(self):
        from smart_scroll.v2_renderer import render_mixed_media, _validate_final_mp4
        with tempfile.TemporaryDirectory(prefix="scene-v2-real-ci-") as temp:
            outdir = Path(temp)
            image = outdir / "blue.png"
            audio = outdir / "synthetic_narration.wav"
            output = outdir / "sample.mp4"
            ffmpeg("-f", "lavfi", "-i", "color=c=blue:s=128x72:d=1",
                   "-frames:v", "1", image)
            ffmpeg("-f", "lavfi", "-i",
                   "sine=frequency=330:sample_rate=44100:duration=2",
                   "-c:a", "pcm_s16le", audio)
            scene = {
                "scene_code": "CI_SYNTHETIC_001",
                "start": 0.0, "end": 2.0, "duration": 2.0,
                "media_path": str(image),
                "transition_out": "none", "effect": "none",
                "filter": "none", "motion": "none",
            }
            config = SimpleNamespace(
                width=128, height=72, fps=12, quality_crf=25,
                output_path=str(output), audio_path=str(audio),
                render_audio_path=str(audio),
                lut_enabled=False, captions_enabled=False,
                metadata={"scene_sync_plan": [scene], "caption_segments": []},
            )
            result = render_mixed_media(config)
            self.assertEqual(Path(result), output)
            info = _validate_final_mp4(output, "ffmpeg",
                                       expected_duration=2.0, expect_audio=True)
            self.assertGreater(output.stat().st_size, 1024)
            self.assertEqual(info["video_codec"], "h264")
            self.assertEqual(info["audio_codec"], "aac")
            self.assertEqual(info["pixel_format"], "yuv420p")
            self.assertEqual((info["width"], info["height"]), (128, 72))
            self.assertLess(abs(info["duration"] - 2.0), 0.25)
            print("SCENE_V2_REAL_MP4=" + json.dumps(
                {"codec": info["video_codec"], "audio": info["audio_codec"],
                 "pix_fmt": info["pixel_format"], "duration": info["duration"],
                 "bytes": output.stat().st_size}, sort_keys=True), flush=True)
            # This uses only a throwaway CI output, no real queue or user state.
            self.assertFalse((outdir / "queue.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()

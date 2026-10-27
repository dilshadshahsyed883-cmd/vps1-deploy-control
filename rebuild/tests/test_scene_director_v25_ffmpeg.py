"""Opt-in Director V25 real synthetic MP4 regression (no user media or VPS)."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps/scene-builder/part01"))
sys.path.insert(0, str(ROOT / "apps/scene-builder/part02"))
sys.path.insert(0, str(ROOT / "tests"))

from director_v25_execution import compile_v25
from test_scene_director_v25_execution import Catalog, plan, CAPS


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class DirectorV25RealFFmpegTests(unittest.TestCase):
    def test_compiled_director_animation_renders_actual_mp4(self):
        from smart_scroll.v2_renderer import render_mixed_media, _validate_final_mp4
        with tempfile.TemporaryDirectory(prefix="director-v25-mp4-") as temp:
            root = Path(temp)
            media = root / "source.mp4"
            generated = subprocess.run([
                "ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc2=s=128x72:r=8:d=2.0",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast", str(media),
            ], capture_output=True, timeout=40)
            self.assertEqual(generated.returncode, 0, generated.stderr.decode(errors="replace")[-900:])
            catalog = Catalog(root, ["Alexa 6.mp4"])
            compiled = compile_v25(
                plan(), catalog, source_mode="library_only", library_enabled=True,
                selected_preset="cinematic_documentary", capabilities=CAPS,
            )
            shot = dict(compiled["scenes"][0],
                        media_path=str(media), start=0.0, end=1.3,
                        duration=1.3, timing_validated=True)
            self.assertEqual(shot["capcut_animation_effect"], "pan_left")
            self.assertEqual(shot["capcut_animation_target"], "videos")
            self.assertEqual(shot["director_v25_media_filename"], "Alexa 6.mp4")
            destination = root / "final.mp4"
            settings = SimpleNamespace(
                width=128, height=72, fps=8, output_path=str(destination),
                audio_path="", render_audio_path="", quality_crf=24,
                intermediate_render_preset="ultrafast", render_chunk_scenes=4,
                video_quality_preset="original", lut_enabled=False,
                captions_enabled=False, canvas_fill_mode="blur", canvas_blur_strength=12,
                canvas_fill_color="#101018", capcut_mirror_enabled=False,
                capcut_mirror_percent=0, capcut_zoom_level=100,
                capcut_noise_enabled=False, capcut_noise_effect="film",
                capcut_noise_intensity=35, capcut_noise_percent=0,
                capcut_noise_target="both", capcut_animation_category="combo",
                capcut_animation_effect="none", capcut_animation_duration=1.0,
                capcut_animation_intensity=60, capcut_animation_target="both",
                metadata={"director_v25": True,
                          "scene_sync_plan": [shot], "caption_segments": []},
            )
            render_mixed_media(settings)
            self.assertTrue(destination.is_file() and destination.stat().st_size > 1024)
            info = _validate_final_mp4(destination, "ffmpeg", expected_duration=1.3, expect_audio=False)
            self.assertEqual((info["width"], info["height"]), (128, 72))


if __name__ == "__main__":
    unittest.main()

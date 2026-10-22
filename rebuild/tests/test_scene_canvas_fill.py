"""Offline FFmpeg Canvas Fill regression for Scene Builder.

Tests generated images/video frames only; no network, production queues or user data.
"""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps/scene-builder/part02"))


def render_canvas(mode: str, width: int, height: int, source_size: str = "72x128",
                  *, blur: int = 16, color: str = "#ff0000", video: bool = False) -> bytes:
    from smart_scroll.v2_renderer import _canvas_fill_chains, _motion

    chains, source = _canvas_fill_chains(0, 0, width, height, mode, blur, color)
    graph = ";".join(chains + [
        f"{source}{_motion({'motion':'none'}, width, height, 8, .5, not video)},"
        "format=rgb24[out]",
    ])
    video_filter = f"testsrc2=s={source_size}:r=8:d=0.6" if video else f"color=c=white:s={source_size}:r=8:d=0.6"
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
         "-filter_complex_threads", "1", "-f", "lavfi", "-i", video_filter,
         "-filter_complex", graph, "-map", "[out]", "-frames:v", "4" if video else "1",
         "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=50,
    )
    if proc.returncode:
        raise AssertionError(f"ffmpeg {mode} ({width}x{height}): {proc.stderr.decode(errors='replace')[-1500:]}")
    return proc.stdout


def pixel(frame: bytes, width: int, x: int, y: int) -> tuple[int, int, int]:
    at = (y * width + x) * 3
    return tuple(frame[at:at+3])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
class CanvasFillMatrix(unittest.TestCase):
    def test_canvas_fill_modes_keep_original_center_and_fill_sides(self):
        # Portrait source in landscape; every mode uses the actual FFmpeg graph.
        width, height = 192, 108
        for mode in ("blur", "black", "solid", "stretch", "crop"):
            with self.subTest(mode=mode):
                frame = render_canvas(mode, width, height)
                self.assertEqual(len(frame), width * height * 3)
                center = pixel(frame, width, width//2, height//2)
                side = pixel(frame, width, 2, height//2)
                if mode in ("blur", "black", "solid"):
                    self.assertTrue(all(v >= 240 for v in center), (mode, center))
                if mode == "black":
                    self.assertTrue(all(v <= 3 for v in side), side)
                elif mode == "solid":
                    self.assertGreater(side[0], 240)
                    self.assertLess(side[1], 10)
                    self.assertLess(side[2], 10)
                elif mode == "blur":
                    self.assertTrue(all(v >= 230 for v in side), (mode, side))

    def test_square_and_vertical_output_with_image_and_moving_video(self):
        for width, height, source in [(108, 108, "72x128"), (108, 192, "128x72"),
                                       (192, 108, "72x128")]:
            with self.subTest(size=(width, height)):
                still = render_canvas("blur", width, height, source)
                self.assertEqual(len(still), width*height*3)
                frames = render_canvas("blur", width, height, source, video=True)
                frame_size = width * height * 3
                self.assertEqual(len(frames), frame_size * 4)
                self.assertNotEqual(frames[:frame_size], frames[-frame_size:])

    def test_legacy_saved_project_is_crop_and_invalid_values_are_safe(self):
        from smart_scroll.project_model import ProjectSettings
        from smart_scroll.v2_renderer import _canvas_fill_chains
        old = ProjectSettings.from_dict({"width": 1920, "height": 1080})
        self.assertEqual(old.canvas_fill_mode, "crop")
        chains, source = _canvas_fill_chains(0, 0, 192, 108, "crop", 16, "#000000")
        self.assertEqual(chains, [])
        self.assertEqual(source, "[0:v]")
        chains, _ = _canvas_fill_chains(0, 0, 192, 108, "solid", 99, ";invalid")
        self.assertIn("color=0x101018", chains[0])
        with self.assertRaises(ValueError):
            _canvas_fill_chains(0, 0, 192, 108, "unexpected", 16, "#000000")


if __name__ == "__main__":
    unittest.main()

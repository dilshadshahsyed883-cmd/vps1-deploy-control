"""Scene Builder Director V25: odd-size media accepts TXT canvas fill.

This uses generated local PNG, ffprobe, and real FFmpeg filters only.
Classic's strict landscape gate must remain unchanged.
"""
from __future__ import annotations

import ast
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps/scene-builder/part02"))
sys.path.insert(0, str(ROOT / "apps/scene-builder/part01"))
sys.path.insert(0, str(ROOT / "tests"))
from smart_scroll.reliability_engine import validate_media
from rebuild.tests.test_scene_canvas_fill import render_canvas
from director_v25_execution import compile_v25
from test_scene_director_v25_execution import Catalog, plan, CAPS


def png_rgb(path: Path, width: int, height: int) -> None:
    """Generate a valid exact-dimension RGB PNG without downloads or Pillow."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))
    header = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">2I5B", width, height, 8, 2, 0, 0, 0)
    scan = b"\x00" + bytes((230, 30, 50)) * width
    compressed = zlib.compress(scan * height)
    path.write_bytes(header + chunk(b"IHDR", ihdr) +
                     chunk(b"IDAT", compressed) + chunk(b"IEND", b""))


def load_media_policy_func():
    """Execute the single production decision helper, not the live worker."""
    path = ROOT / "apps/scene-builder/part02/api_server.py"
    source = path.read_text("utf-8")
    node = next((n for n in ast.parse(source).body
                 if isinstance(n, ast.FunctionDef) and n.name == "_requires_landscape_media"), None)
    assert node, "Director V25 media policy helper not registered"
    ns = {"Any": object}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), ns)
    return ns["_requires_landscape_media"]


class DirectorCanvasMediaTests(unittest.TestCase):
    def test_director_accepts_exact_612x435_with_any_canvas_mode(self):
        require_landscape = load_media_policy_func()
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "Alexa 6.png"
            png_rgb(source, 612, 435)
            self.assertGreater(source.stat().st_size, 1024)
            self.assertTrue(require_landscape({"pexels_prefer_landscape": True}))
            with self.assertRaisesRegex(ValueError, "Portrait or square media rejected: 612x435"):
                validate_media(source, "image", require_landscape=True)
            for mode in ("blur", "crop", "black", "solid", "stretch"):
                with self.subTest(mode=mode):
                    # Mode comes from the Director's [OUTPUT], not the UI preset.
                    director = {
                        "director_v25_shot": "SCENE_001.SHOT_01",
                        "pexels_prefer_landscape": True,
                        "canvas_fill_mode": mode,
                    }
                    self.assertFalse(require_landscape(director))
                    info = validate_media(source, "image", require_landscape=require_landscape(director))
                    self.assertEqual((info["width"], info["height"]), (612, 435))
                    self.assertTrue(info["valid"])
                    self.assertEqual(info["warning"], "low_resolution")
            self.assertTrue(require_landscape({"scene_code": "SCENE_001", "pexels_prefer_landscape": True}))
            self.assertFalse(require_landscape({"pexels_prefer_landscape": False}))

    def test_intro_and_main_director_uses_same_aspect_acceptance(self):
        policy = load_media_policy_func()
        for shot in ("INTRO.SHOT_01", "SCENE_001.SHOT_01"):
            with self.subTest(shot=shot):
                self.assertFalse(policy({"director_v25_shot": shot, "pexels_prefer_landscape": True}))
        worker = (ROOT / "apps/scene-builder/part02/api_server.py").read_text("utf-8")
        self.assertIn('if scene.get("director_v25_shot"):', worker)
        self.assertIn('require_landscape=_requires_landscape_media(scene)', worker)
        self.assertIn('require_landscape=_requires_landscape_media(old)', worker)
        self.assertIn('"pexels_prefer_landscape": (False if metadata.get("director_v25")', worker)

    def test_scene_plan_explicit_canvas_is_authority_with_safe_default(self):
        with tempfile.TemporaryDirectory() as temp:
            catalog = Catalog(Path(temp), ["Alexa 6.mp4"])
            for declared, expected in (
                ("", "blur"),
                ("canvas_fill_mode=black", "black"),
                ("canvas_fill_mode=solid\ncanvas_fill_color=#ffffff", "solid"),
                ("canvas_fill_mode=crop", "crop"),
                ("canvas_fill_mode=stretch", "stretch"),
            ):
                with self.subTest(expected=expected):
                    txt = plan().replace("filename=Atlas.mp4",
                                         "filename=Atlas.mp4\n" + declared if declared else "filename=Atlas.mp4")
                    result = compile_v25(
                        txt, catalog, source_mode="library_only", library_enabled=True,
                        selected_preset="storytelling", capabilities=CAPS,
                    )
                    self.assertEqual(result["settings"]["canvas_fill_mode"], expected)
                    self.assertEqual(result["settings"]["canvas_blur_strength"], 16)
                    if expected == "solid":
                        self.assertEqual(result["settings"]["canvas_fill_color"], "#ffffff")

    def test_ffmpeg_canvas_fill_adapts_612x435_landscape_and_portrait(self):
        # The real renderer's canvas filter must produce fixed output geometry.
        import shutil
        if not shutil.which("ffmpeg"):
            self.skipTest("FFmpeg unavailable")
        for mode in ("blur", "black", "solid", "crop"):
            with self.subTest(mode=mode):
                for width, height in ((192, 108), (108, 192)):
                    frame = render_canvas(mode, width, height, "612x435")
                    self.assertEqual(len(frame), width * height * 3)


if __name__ == "__main__":
    unittest.main()

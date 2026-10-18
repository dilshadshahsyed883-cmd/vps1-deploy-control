"""Restored user-supplied V2 renderer: syntax + actual FFmpeg validation.

This is a source-recovery check, NOT a full Scene Builder render-worker test.
Required sibling modules remain separately gated before any worker activation.
"""
from __future__ import annotations
import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]
PATH=ROOT/"apps/scene-builder/part02/smart_scroll/v2_renderer.py"
EXPECTED_SHA256="3ef882b67eaa5d5103ded144b6f1dbaf1908b9f59a451d80d701a04ff28c910f"

def extracted():
    """Run four independent functions without faking unavailable packages."""
    source=ast.parse(PATH.read_text(encoding="utf-8"),filename=str(PATH))
    keep={"_atempo_filters_for_speed","_ffprobe_path","_validate_final_mp4","_render_disk_guard"}
    nodes=[x for x in source.body if isinstance(x,(ast.FunctionDef,ast.AsyncFunctionDef)) and x.name in keep]
    assert {x.name for x in nodes}==keep
    scope={"__builtins__":__builtins__,"os":os,"subprocess":subprocess,"shutil":shutil,
           "json":json,"Path":Path,"Any":object}
    # Annotations are postponed in the restored file. AST extraction preserves
    # the isolated functions' behavior without importing unresolved sibling code.
    nodes.insert(0,ast.ImportFrom(module="__future__",
        names=[ast.alias(name="annotations")],level=0))
    ast.fix_missing_locations(ast.Module(body=nodes,type_ignores=[]))
    exec(compile(ast.Module(body=nodes,type_ignores=[]),str(PATH),"exec"),scope)
    return scope

class OwnerSourceRecovery(unittest.TestCase):
    def test_exact_uploaded_source_integrity_and_python_syntax(self):
        content=PATH.read_bytes()
        self.assertEqual(hashlib.sha256(content).hexdigest(),EXPECTED_SHA256)
        ast.parse(content.decode("utf-8"),filename=str(PATH))
        self.assertIn("def render_mixed_media(",content.decode())
        self.assertIn("libx264",content.decode())
        self.assertIn("_validate_final_mp4(",content.decode())

    def test_speed_filter_and_real_mp4_validation(self):
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            self.skipTest("FFmpeg suite not present on runner")
        fn=extracted()
        self.assertEqual(fn["_atempo_filters_for_speed"](1),[])
        self.assertEqual(fn["_atempo_filters_for_speed"](4),["atempo=2.00000","atempo=2.00000"])
        with tempfile.TemporaryDirectory(prefix="vps1-v2-restoration-") as dirname:
            target=Path(dirname)/"one_second.mp4"
            cmd=[
                "ffmpeg","-v","error","-f","lavfi",
                "-i","color=c=blue:s=128x72:r=15:d=1",
                "-f","lavfi",
                "-i","sine=frequency=440:sample_rate=44100:duration=1",
                "-shortest","-c:v","libx264","-pix_fmt","yuv420p",
                "-c:a","aac","-movflags","faststart","-y",str(target)
            ]
            subprocess.run(cmd,check=True,capture_output=True,timeout=25)
            result=fn["_validate_final_mp4"](target,shutil.which("ffmpeg"),
                         expected_duration=1.0,expect_audio=True)
            self.assertEqual(result["video_codec"],"h264")
            self.assertEqual(result["pixel_format"],"yuv420p")
            self.assertEqual(result["audio_codec"],"aac")
            self.assertEqual((result["width"],result["height"]),(128,72))
            target.write_bytes(b"invalid mp4")
            with self.assertRaises(RuntimeError):
                fn["_validate_final_mp4"](target,shutil.which("ffmpeg"))
if __name__=="__main__":unittest.main()

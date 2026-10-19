"""Offline shared-module parity tests; no app imports, network or writes to prod."""
import ast
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from shared_modules.media_files import resolve_relative_video
from shared_modules.time_utils import scheduled_utc, utc_now
from shared_modules.youtube_ids import is_youtube_channel_id

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "apps" / "short-stream" / "part01" / "auto_stream_engine.py"

def source_function(name, class_name=None):
    """Compile a single original pure function, not any production engine."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    scope = tree.body
    if class_name:
        scope = next(x for x in scope if isinstance(x, ast.ClassDef)
                     and x.name == class_name).body
    f = next(x for x in scope if isinstance(x, ast.FunctionDef) and x.name == name)
    ns = {"datetime": datetime, "timezone": timezone, "Path": Path}
    exec(compile(ast.Module(body=[f], type_ignores=[]), str(SOURCE), "exec"), ns)
    return ns[name]

def result_or_error(fn):
    try:
        return ("result", fn())
    except Exception as ex:
        return (type(ex).__name__, str(ex))

class ReusableModuleTests(unittest.TestCase):
    def test_time_equivalence(self):
        source = source_function("scheduled_utc")
        for value in (None, "", "2026-10-20T13:00:00+05:00",
                      "2026-10-20T08:00:00Z", "2026-10-20T10:00:00",
                      "invalid", "2026-10-20T09:00:00-04:00"):
            with self.subTest(value=value):
                self.assertEqual(result_or_error(lambda: scheduled_utc(value)),
                                 result_or_error(lambda: source(value)))
        self.assertTrue(utc_now().endswith("+00:00"))

    def test_media_equivalence(self):
        source = source_function("_source", "StreamEngine")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            media = base / "media"
            (media / "sub").mkdir(parents=True)
            (media / "sub" / "clip.mp4").write_bytes(b"x")
            (media / "sub" / "notes.txt").write_text("x")
            (base / "outside.mp4").write_bytes(b"x")
            (media / "sub" / "link.mp4").symlink_to(media / "sub" / "clip.mp4")
            fake = type("Legacy", (), {"media": media})()
            for name in ("sub/clip.mp4", "sub/notes.txt", "missing.mp4",
                         "../outside.mp4", "/etc/passwd", "sub/link.mp4", ""):
                with self.subTest(name=name):
                    self.assertEqual(
                        result_or_error(lambda: resolve_relative_video(media, name)),
                        result_or_error(lambda: source(fake, name)))

    def test_channel_syntax(self):
        self.assertTrue(is_youtube_channel_id("UC" + "x" * 22))
        for bad in (None, "", "UC" + "x" * 21, "UC" + "!" * 22, "test"):
            self.assertFalse(is_youtube_channel_id(bad))

if __name__ == "__main__":
    unittest.main()

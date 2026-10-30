"""Offline Engine 2 / Short Uploader parity contract; no network, credentials or jobs."""
from __future__ import annotations

import ast
import os
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / "apps/local-upload-manager/part01"
ENGINE = ROOT / "lum_engine/auto_engine_v6.py"
WORKER = ROOT / "lum_engine/auto_engine_v6_worker.py"


def load_pure_function(name: str, namespace: dict, *, last: bool = False):
    tree = ast.parse(ENGINE.read_text(encoding="utf-8"))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name]
    assert functions, name
    code = compile(ast.Module(body=[functions[-1 if last else 0]], type_ignores=[]), str(ENGINE), "exec")
    exec(code, namespace)
    return namespace[name]


def png_bytes(width=320, height=180):
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)
    pixels = (b"\x00" + b"\x00\x00\x00" * width) * height
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b"")


class FakeStore:
    def __init__(self, root, name="image.png"):
        self.root = Path(root)
        self.name = name

    def channel_job_meta(self, _video_id):
        return {"thumbnail_source_folder_id": "DRIVE_FOLDER", "thumbnail_filename": self.name}

    def job_source(self, _video_id):
        return {"staging_path": str(self.root / "video.mp4")}


class ThumbnailParityTests(unittest.TestCase):
    def test_engine2_uses_short_uploader_shared_workspace_with_thumbnail_policy(self):
        ui = (ROOT / "templates/app.html").read_text(encoding="utf-8")
        server = (ROOT / "lum_engine/standalone_app.py").read_text(encoding="utf-8")
        source = ENGINE.read_text(encoding="utf-8")
        self.assertIn('engine_instance in {"engine2", "shorts", "song", "shortstudio"}', server)
        self.assertIn('selected_auto_mode = "engine1"', server)
        self.assertIn("{% if auto_mode == 'engine1' %}", ui)
        self.assertIn("('engine2', 'Engine 2')", ui)
        self.assertNotIn("('shorts', 'Short Uploader')", ui)
        self.assertIn("Short Uploader has moved to Short Studio", ui)
        self.assertIn("if thumbnail_required_for_mode():\n        store.scan_drive_thumbnails(pid)", source)
        self.assertIn("int(thumbnail_required_for_mode())", source)

        policy = load_pure_function("thumbnail_required_for_mode", {"os": os})
        with patch.dict(os.environ, {"LUM_ENGINE_INSTANCE": "engine2"}):
            self.assertTrue(policy())
        with patch.dict(os.environ, {"LUM_ENGINE_INSTANCE": "shorts"}):
            self.assertFalse(policy())

    def test_valid_drive_thumbnail_is_downloaded_and_verified(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            namespace = {
                "Path": Path, "V6Store": object, "THUMB_EXTS": {".jpg", ".jpeg", ".png"},
                "clean": lambda value: str(value or "").strip(),
                "struct": struct, "zlib": zlib,
            }
            load_pure_function("_image_dimensions", namespace)
            calls = []
            def download(folder, name, target):
                calls.append((folder, name))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(png_bytes())
            namespace["drive_download"] = download
            fn = load_pure_function("download_channel_thumbnail_for_job", namespace)
            store = FakeStore(root)
            actual = fn(store, 12)
            self.assertEqual(actual, str(root / "thumbnail/image.png"))
            self.assertEqual(calls, [("DRIVE_FOLDER", "image.png")])
            self.assertTrue(Path(actual).exists())
            # A verified cache is reused and does not trigger another download.
            self.assertEqual(fn(store, 12), actual)
            self.assertEqual(len(calls), 1)

    def test_bad_drive_thumbnail_is_rejected_before_youtube_upload(self):
        namespace = {
            "Path": Path, "V6Store": object, "THUMB_EXTS": {".jpg", ".jpeg", ".png"},
            "clean": lambda value: str(value or "").strip(),
            "struct": struct, "zlib": zlib,
        }
        load_pure_function("_image_dimensions", namespace)
        downloaded = [b"corrupt", b"x" * (2 * 1024 * 1024 + 1), png_bytes(width=2, height=2)]
        def download(_folder, _name, target):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(downloaded.pop(0))
        namespace["drive_download"] = download
        fn = load_pure_function("download_channel_thumbnail_for_job", namespace)
        with tempfile.TemporaryDirectory() as d:
            store = FakeStore(d)
            for error in ("validation failed", "exceeds 2 MB", "too small"):
                with self.subTest(error=error):
                    with self.assertRaisesRegex(RuntimeError, error):
                        fn(store, 77)
                    self.assertFalse((Path(d) / "thumbnail/image.png").exists())
            with self.assertRaisesRegex(RuntimeError, "JPG/JPEG/PNG"):
                fn(FakeStore(d, name="bad.webp"), 77)

    def test_queue_preflight_only_suppresses_local_missing_for_valid_indexed_drive_thumbnail(self):
        namespace = {
            "Any": Any, "Dict": Dict, "Path": Path, "V6Store": object,
            "THUMB_EXTS": {".jpg", ".jpeg", ".png"},
            "clean": lambda value: str(value or "").strip(),
            "thumbnail_required_for_mode": lambda: True,
            "_CHANNEL_LEGACY_QUEUE_PREFLIGHT": lambda _db, _store: {
                "checked": 1, "issues": [{"video_id": 8, "code": "THUMBNAIL_MISSING", "severity": "ERROR"}],
                "errors": 1, "warnings": 0, "ok": False,
            },
        }
        fn = load_pure_function("queue_preflight", namespace, last=True)
        class Cursor:
            def __init__(self, row): self.row = row
            def fetchone(self): return self.row
        class Conn:
            def __init__(self, store): self.store = store
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def execute(self, _sql, _params): return Cursor(self.store.indexed)
        class Store:
            def __init__(self):
                self.indexed = {"present": 1, "filename": "id123.png", "source_folder_id": "FOLDER", "size_bytes": 40000}
            def channel_job_meta(self, _id):
                return {"profile_id": 9, "thumbnail_drive_file_id": "DRIVE_FILE",
                        "thumbnail_filename": "id123.png", "thumbnail_source_folder_id": "FOLDER"}
            def connect(self): return Conn(self)
        store = Store()
        self.assertEqual(fn(None, store)["errors"], 0)
        store.indexed["size_bytes"] = 2 * 1024 * 1024 + 1
        self.assertEqual(fn(None, store)["errors"], 1)
        store.indexed["size_bytes"] = 40000
        store.indexed["filename"] = "wrong.png"
        self.assertEqual(fn(None, store)["errors"], 1)
        namespace["thumbnail_required_for_mode"] = lambda: False
        self.assertEqual(fn(None, store)["errors"], 0)

    def test_skipped_required_thumbnail_never_completes_engine2_drive_cleanup(self):
        worker = WORKER.read_text(encoding="utf-8")
        self.assertIn('thumb_state=="SKIPPED" and int(meta.get("strict_thumbnail") or 0)', worker)
        self.assertIn('set_channel_job_post_state(store,video_id,"MANUAL_RETRY_REQUIRED"', worker)
        self.assertIn('if thumb_state in {"SUCCEEDED","SKIPPED"}:', worker)
        for path in (ENGINE, WORKER, ROOT / "lum_engine/standalone_app.py"):
            with self.subTest(path=path):
                ast.parse(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

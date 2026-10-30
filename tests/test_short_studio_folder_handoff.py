"""Short Studio VPS folder handoff safety regressions (no YouTube network access)."""
import ast
import hashlib
import os
import re
import shutil
import tempfile
import unittest
from unittest.mock import patch
import uuid
from pathlib import Path

from production import workers

ROOT = Path(__file__).resolve().parents[1]
MAKER = ROOT / "apps/short-maker/part01/app.py"
ENGINE = ROOT / "apps/local-upload-manager/part01/lum_engine/auto_engine_v6.py"
WORKER = ROOT / "apps/local-upload-manager/part01/lum_engine/auto_engine_v6_worker.py"
WEB = ROOT / "apps/local-upload-manager/part01/lum_engine/standalone_app.py"
TEMPLATE = ROOT / "apps/local-upload-manager/part01/templates/app.html"


def helpers(path, names, globals_):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert len(nodes) == len(names)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), globals_)
    return globals_


class ShortStudioFolderHandoffTests(unittest.TestCase):
    def test_python_modules_compile(self):
        for path in (MAKER, ENGINE, WORKER, WEB):
            with self.subTest(path=path.name):
                compile(path.read_text(encoding="utf-8"), str(path), "exec")

    def test_legacy_maker_submit_does_not_shadow_folder_validator(self):
        # The posted Form name and the folder resolver must not share a local name.
        src = MAKER.read_text(encoding="utf-8")
        self.assertIn('short_vps_folder_name: str = Form("", alias="short_vps_folder")', src)
        self.assertIn('short_target = short_vps_folder(short_vps_folder_name)', src)

    def test_maker_folder_isolation_and_atomic_copy(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "shorts"
            g = helpers(MAKER, {"short_vps_folder", "short_vps_folder_names", "publish_short_to_vps"}, {
                "SHORT_VPS_ROOT": root, "SHORT_VPS_DEFAULT": "Default",
                "Path": Path, "re": re, "shutil": shutil, "uuid": uuid, "os": os,
            })
            self.assertEqual(g["short_vps_folder_names"](), ["Default"])
            folder = g["short_vps_folder"]("Channel A")
            folder.mkdir()
            local = Path(td) / "abc123.mp4"
            local.write_bytes(b"fake mp4 for file copy test")
            first = g["publish_short_to_vps"](local, "Channel A", "abcd1234")
            second = g["publish_short_to_vps"](local, "Channel A", "efgh5678")
            self.assertNotEqual(first, second)
            self.assertEqual((folder / first).read_bytes(), local.read_bytes())
            self.assertEqual((folder / second).read_bytes(), local.read_bytes())
            with self.assertRaises(OSError):
                folder.rmdir()
            for bad in ("../escape", "/tmp", ".", "", "bad/name", "a" * 70):
                with self.subTest(name=bad):
                    with self.assertRaises(ValueError):
                        g["short_vps_folder"](bad)
            (root / "evil").symlink_to(Path(td), target_is_directory=True)
            with self.assertRaises(ValueError):
                g["short_vps_folder"]("evil")
            self.assertNotIn("evil", g["short_vps_folder_names"]())

    def test_uploader_source_fingerprint_prevents_replacement(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            folder = root / "Channel A"
            folder.mkdir()
            source = folder / "video123.mp4"
            source.write_bytes(b"first file")
            pattern = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,63}\Z")
            g = helpers(ENGINE, {"shortstudio_folder_path", "shortstudio_source_path", "shortstudio_file_id"}, {
                "SHORT_STUDIO_ROOT": root, "SHORT_FOLDER_PREFIX": "shortstudio-local:",
                "SHORT_FOLDER_PATTERN": pattern, "Path": Path, "hashlib": hashlib,
            })
            source_id = "shortstudio-local:Channel A"
            self.assertEqual(g["shortstudio_source_path"](source_id, source.name), source)
            original = g["shortstudio_file_id"](source_id, source)
            source.write_bytes(b"second file, a different size")
            self.assertNotEqual(original, g["shortstudio_file_id"](source_id, source))
            with self.assertRaises(ValueError):
                g["shortstudio_source_path"]("shortstudio-local:../escape", source.name)
            source.unlink()
            source.symlink_to(Path(td) / "external.mp4")
            with self.assertRaises(FileNotFoundError):
                g["shortstudio_source_path"](source_id, source.name)

    def test_deployment_shares_exact_path_without_mutating_existing_files(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            root = base / "short-maker" / "vps_short_folders"
            with patch.object(workers, "DATA", base), patch.object(workers.os, "chown"):
                self.assertEqual(workers.prepare_short_studio_shared_root(), root)
                self.assertTrue(root.is_dir())
                existing = root / "keep.mp4"
                existing.write_bytes(b"do not touch user MP4")
                self.assertEqual(workers.prepare_short_studio_shared_root(), root)
                self.assertEqual(existing.read_bytes(), b"do not touch user MP4")
                existing.unlink()
                root.rmdir()
                root.symlink_to(base, target_is_directory=True)
                with self.assertRaisesRegex(RuntimeError, "symlink denied"):
                    workers.prepare_short_studio_shared_root()

    def test_scoped_release_and_sandbox_write_path(self):
        with patch.object(workers, "DATA", Path("/var/lib/fresh-website")):
            uploader = workers.mode_exec("lum-shortstudio-worker",
                                          Path("/venv/bin/python"), "-m lum_engine.auto_engine_v6_worker")
            self.assertIn(
                "SHORT_STUDIO_VPS_FOLDER_ROOT=/var/lib/fresh-website/short-maker/vps_short_folders",
                uploader)
            worker_unit = workers.unit("local-upload-manager", "a" * 40,
                                       "lum-shortstudio-worker", "part01",
                                       "-m lum_engine.auto_engine_v6_worker")
            web_unit = workers.unit("local-upload-manager", "a" * 40,
                                    "lum-shortstudio-web", "part01",
                                    "-m uvicorn lum_engine.standalone_app:app")
            self.assertIn("ReadWritePaths=/var/lib/fresh-website/local-upload-manager "
                          "-/home/ubuntu/.config/masterctl "
                          "/var/lib/fresh-website/short-maker/vps_short_folders", worker_unit)
            self.assertNotIn("short-maker/vps_short_folders", web_unit.split("ReadWritePaths=")[1])
        maker = MAKER.read_text(encoding="utf-8")
        engine = ENGINE.read_text(encoding="utf-8")
        self.assertIn('DATA_ROOT / "vps_short_folders"', maker)
        self.assertIn('"/var/lib/fresh-website/short-maker/vps_short_folders"', engine)
        workflow = (ROOT / ".github/workflows/vps1-short-studio-release.yml").read_text(encoding="utf-8")
        self.assertIn("for app in local-upload-manager short-maker; do", workflow)
        self.assertNotIn("for app in local-upload-manager short-maker video-merger scene-builder", workflow)
        self.assertIn("AUTO_SCRIPT=OFFLINE", workflow)

    def test_preserves_drive_fallback_and_publication_guards(self):
        engine = ENGINE.read_text(encoding="utf-8")
        worker = WORKER.read_text(encoding="utf-8")
        template = TEMPLATE.read_text(encoding="utf-8")
        self.assertIn('if os.environ.get("LUM_ENGINE_INSTANCE") == "shortstudio"', engine)
        self.assertIn('return self.scan_shortstudio_local(profile_id', engine)
        self.assertIn('if not folder_id:', engine)
        self.assertIn('raise ValueError("Google Drive Video Folder is required.")', engine)
        self.assertIn('short_folder_name TEXT NOT NULL DEFAULT', engine)
        self.assertIn('ALTER TABLE profile_sources ADD COLUMN short_folder_name', engine)
        self.assertIn('name="short_vps_folder"', template)
        self.assertIn('Use Google Drive source instead', template)
        self.assertIn('remote_verify(uploader,row,yid)', worker)
        self.assertLess(worker.index('store.record_upload(db,video_id'), worker.index('original.unlink()'))
        self.assertIn('Other queued/failed channel jobs still need this VPS video', worker)
        self.assertIn('Source MP4 changed after queueing; refusing to delete replacement.', worker)
        self.assertIn('folder_id.startswith(SHORT_FOLDER_PREFIX)', worker)
        self.assertIn('if folder_id=="songmaker-local":', worker)


if __name__ == "__main__":
    unittest.main()

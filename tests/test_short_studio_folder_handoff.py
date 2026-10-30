"""Short Studio VPS folder handoff safety regressions (no YouTube network access)."""
import ast
import hashlib
import os
import re
import sqlite3
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

    def test_short_maker_third_settings_tab_keeps_render_on_maker(self):
        src = MAKER.read_text(encoding="utf-8")
        tree = ast.parse(src)
        html = next(
            n.value.value for n in tree.body
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "HTML" for t in n.targets)
        )
        maker_button = html.index('id="makerTabBtn"')
        queue_button = html.index('id="queueTabBtn"')
        settings_button = html.index('id="settingsTabBtn"')
        self.assertLess(maker_button, queue_button)
        self.assertLess(queue_button, settings_button)
        self.assertIn('role="tablist"', html)
        form_at = html.index('<form id="form">')
        maker_at = html.index('<section id="makerPanel">')
        settings_at = html.index('<section id="settingsPanel"')
        end_form_at = html.index('</form>', settings_at)
        queue_at = html.index('<section id="queuePanel"')
        self.assertLess(form_at, maker_at)
        self.assertLess(maker_at, settings_at)
        self.assertLess(settings_at, end_form_at)
        self.assertLess(end_form_at, queue_at)
        maker_section = html[maker_at:settings_at]
        settings_section = html[settings_at:end_form_at]
        self.assertIn('id="render"', maker_section)
        self.assertIn('id="bar"', maker_section)
        self.assertIn('id="status"', maker_section)
        for control in ('id="shortVpsFolder"', 'id="createShortFolder"',
                        'id="deleteShortFolder"', 'id="shortFolderMessage"'):
            with self.subTest(control=control):
                self.assertIn(control, maker_section)
                self.assertNotIn(control, settings_section)
        self.assertLess(maker_section.index('id="shortVpsFolder"'),
                        maker_section.index('<h2>Images</h2>'))
        for control in ('name="base_name"', 'id="availableToUploader"',
                        'id="uploadToDrive"', 'id="driveOutputPath"',
                        'id="verifiedClean"'):
            with self.subTest(control=control):
                self.assertIn(control, settings_section)
                self.assertNotIn(control, maker_section)
        self.assertIn('value="9:16 · 1080 × 1920 (vertical)"', settings_section)
        self.assertIn('id="availableToUploader" name="available_to_short_uploader" type="checkbox" checked', settings_section)
        self.assertIn('id="uploadToDrive" name="upload_to_drive" type="checkbox">', settings_section)
        self.assertIn("selectOutputDestination('uploader')", html)
        self.assertIn("selectOutputDestination('drive')", html)
        self.assertIn("fd.append('available_to_short_uploader',uploaderDestination.checked", html)
        self.assertIn('Select exactly one output destination.', src)
        self.assertIn('if cfg.get("available_to_short_uploader",', src)
        self.assertIn('strict=bool(cfg.get("output_names"))', src)
        self.assertIn('"output_names": reserved_names', src)
        self.assertIn('available_to_short_uploader: bool | None = Form(None)', src)
        self.assertNotIn('id="render"', settings_section)
        self.assertIn("settingsTabBtn.onclick=()=>showShortTab('settings')", html)
        self.assertIn("panels[key].hidden=!active", html)
        self.assertIn("['video_duration','image_duration','output_count'", html)
        self.assertIn("fd.append('short_vps_folder',shortFolderSelect.value)", html)

    def test_folder_numbering_with_shared_batch_tag_is_durable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "shorts"
            folder = root / "Aina Asif"
            folder.mkdir(parents=True)
            g = helpers(MAKER, {"short_vps_folder", "reserve_short_output_names"}, {
                "SHORT_VPS_ROOT": root, "DATA_ROOT": Path(td) / "data",
                "Path": Path, "re": re, "sqlite3": sqlite3, "uuid": uuid,
            })
            first = g["reserve_short_output_names"]("Aina Asif", 3)
            self.assertRegex(first[0], r"^Aina Asif 001_[0-9a-f]{6}\\.mp4$")
            tag1 = first[0].rsplit("_", 1)[1]
            self.assertEqual(first, [
                "Aina Asif 001_" + tag1, "Aina Asif 002_" + tag1,
                "Aina Asif 003_" + tag1,
            ])
            # No output yet exists on disk: a queued concurrent batch still
            # must get new numbers and a different six-character tag.
            second = g["reserve_short_output_names"]("Aina Asif", 2)
            self.assertRegex(second[0], r"^Aina Asif 004_[0-9a-f]{6}\\.mp4$")
            self.assertEqual(second[1][:14], "Aina Asif 005_")
            self.assertNotEqual(tag1, second[0].rsplit("_", 1)[1])
            folder.joinpath("Aina Asif 020_abc123.mp4").write_bytes(b"existing")
            third = g["reserve_short_output_names"]("Aina Asif", 1)
            self.assertTrue(third[0].startswith("Aina Asif 021_"))
            # Delete-after-upload must never reset numbering.
            folder.joinpath("Aina Asif 020_abc123.mp4").unlink()
            fourth = g["reserve_short_output_names"]("Aina Asif", 1)
            self.assertTrue(fourth[0].startswith("Aina Asif 022_"))
            other = root / "Channel B"
            other.mkdir()
            self.assertTrue(g["reserve_short_output_names"]("Channel B", 1)[0].startswith("Channel B 001_"))

    def test_batch_filenames_never_overwrite_published_vps_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "folders"
            folder = root / "Channel A"
            folder.mkdir(parents=True)
            local = Path(td) / "Channel A 001_abcdef.mp4"
            local.write_bytes(b"new bytes")
            existing = folder / local.name
            existing.write_bytes(b"original bytes")
            g = helpers(MAKER, {"short_vps_folder", "publish_short_to_vps"}, {
                "SHORT_VPS_ROOT": root, "Path": Path, "re": re,
                "shutil": shutil, "uuid": uuid, "os": os,
            })
            with self.assertRaises(FileExistsError):
                g["publish_short_to_vps"](local, "Channel A", "any", strict=True)
            self.assertEqual(existing.read_bytes(), b"original bytes")

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

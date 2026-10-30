"""Song Uploader manual metadata: offline, owner-safe regression coverage."""
from __future__ import annotations

import ast
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "apps/local-upload-manager/part01/lum_engine/auto_engine_v6.py"
TEMPLATE = ROOT / "apps/local-upload-manager/part01/templates/app.html"
JAVASCRIPT = ROOT / "apps/local-upload-manager/part01/static/auto_engine_v6.js"


def selected_function(name, *, method=False):
    tree = ast.parse(BACKEND.read_text(encoding="utf-8"))
    matches = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name and not method:
            matches.append(node)
        if isinstance(node, ast.ClassDef) and method:
            matches += [child for child in node.body
                        if isinstance(child, ast.FunctionDef) and child.name == name]
    assert matches, name
    fn = matches[-1]
    fn.decorator_list = []
    module = ast.Module(body=[ast.ImportFrom(
        module="__future__", names=[ast.alias(name="annotations")], level=0), fn],
        type_ignores=[])
    code = compile(ast.fix_missing_locations(module), str(BACKEND), "exec")
    return code


class SongUploaderManualMetadataTests(unittest.TestCase):
    def test_persistent_source_choice_never_resets_on_unspecified_edit(self):
        with tempfile.TemporaryDirectory() as folder:
            dbpath = Path(folder) / "source.sqlite"
            with sqlite3.connect(dbpath) as con:
                con.execute("""CREATE TABLE profile_sources(
                    profile_id INTEGER PRIMARY KEY,drive_video_folder_id TEXT,
                    drive_video_folder_url TEXT,drive_thumbnail_folder_id TEXT,
                    drive_thumbnail_folder_url TEXT,thumbnail_source_mode TEXT,
                    short_folder_name TEXT,local_songmaker_enabled INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT)""")
            namespace = {
                "clean": lambda value: str(value or "").strip(),
                "extract_drive_folder_id": lambda value: value,
                "Optional": object,
            }
            exec(selected_function("save_source", method=True), namespace)
            class Store:
                def connect(self):
                    return sqlite3.connect(dbpath)
            store = Store()
            save = namespace["save_source"]
            save(store, 42, songmaker_enabled=True)
            save(store, 42)  # Legacy caller may not clear selected Song Maker mode.
            with sqlite3.connect(dbpath) as con:
                self.assertEqual(con.execute(
                    "SELECT local_songmaker_enabled FROM profile_sources").fetchone()[0], 1)
            save(store, 42, songmaker_enabled=False)
            with sqlite3.connect(dbpath) as con:
                self.assertEqual(con.execute(
                    "SELECT local_songmaker_enabled FROM profile_sources").fetchone()[0], 0)

    def test_manual_metadata_creates_one_video_id_row_per_local_video_no_sheet(self):
        namespace = {
            "clean": lambda value: str(value or "").strip(),
            "Path": Path,
        }
        exec(selected_function("_channel_fetch_rows"), namespace)
        def forbidden(*args, **kwargs):
            raise AssertionError("Google Sheet must never be fetched for manual metadata")
        namespace["fetch_sheet_csv_gid"] = forbidden
        namespace["fetch_sheet_csv_tab"] = forbidden
        rows = namespace["_channel_fetch_rows"](
            {"metadata_sheet_url_or_id": ""},
            [{"filename": "sQ74S7cZ3Kg.mp4"}, {"filename": "anotherSong.mp4"}],
            {"metadata_source": "manual", "manual_title": "Song {video_stem}",
             "manual_description": "Saved on website", "manual_category_id": "10",
             "manual_upload_date": "2026-11-01", "manual_upload_time": "09:30"})
        self.assertEqual([r["video_id"] for r in rows], ["sQ74S7cZ3Kg", "anotherSong"])
        self.assertEqual([r["category_id"] for r in rows], ["10", "10"])
        self.assertTrue(all(r["upload_enabled"] == "YES" and r["done"] == "YES" for r in rows))
        self.assertTrue(all(r["upload_date"] == "2026-11-01" for r in rows))

    def test_form_save_writes_manual_metadata_and_songmaker_source(self):
        class PC:
            payload = None
            def get_auto_engine_profile(self, pid): return {"id":pid}
            def save_auto_engine_profile(self, data, pid):
                self.payload = data
                return 11
            def delete_auto_engine_profile(self, pid):
                raise AssertionError("No rollback expected")
        class Store:
            source = None
            binding = None
            def channel_binding_for_channel(self, cid): return {}
            def channel_binding(self, pid): return {"channel_db_id":8}
            def save_source(self, pid, **kw): self.source = kw
            def save_channel_binding(self, pid, cid, **kw): self.binding = kw
        pc, store = PC(), Store()
        namespace = {
            "clean": lambda x: str(x or "").strip(),
            "_truthy": lambda x: str(x).lower() in ("1", "yes", "on"),
            "_channel_get_channel": lambda db, cid: {"id": cid, "channel_name": "Music"},
            "_channel_schedule_at": lambda d, t, tz: d + t,
            "extract_gid": lambda *args, **kw: "",
            "os": os,
        }
        exec(selected_function("save_profile_from_form"), namespace)
        with patch.dict(os.environ, {"LUM_ENGINE_INSTANCE": "song"}):
            result = namespace["save_profile_from_form"](pc, store, {
                "channel_db_id": "8", "category_name": "Test Channel",
                "video_source_mode": "songmaker", "metadata_source": "manual",
                "manual_title": "My Song {video_stem}", "manual_description": "Saved description",
                "manual_category_id": "10", "manual_upload_date": "2026-11-01",
                "manual_upload_time": "09:30", "enabled": "1"}, 11)
        self.assertEqual(result, 11)
        self.assertTrue(store.source["songmaker_enabled"])
        self.assertEqual(store.binding["metadata_source"], "manual")
        self.assertEqual(store.binding["manual_category_id"], "10")
        self.assertEqual(store.binding["manual_description"], "Saved description")
        self.assertFalse(pc.payload["metadata_sheet_url_or_id"])

    def test_rendered_profile_has_both_modes_without_changing_other_uploaders(self):
        source = TEMPLATE.read_text(encoding="utf-8")
        pos = source.index("<h2>Channel Resources</h2>")
        start = source.rfind('<section class="lum-card">', 0, pos)
        end = source.index("</section>", pos) + len("</section>")
        template = Environment().from_string(source[start:end])
        data = dict(src={}, csrf="example", p=None, ae=SimpleNamespace(short_folders=[]))
        manual = template.render(engine_instance="song", **data)
        self.assertIn('value="songmaker" selected', manual)
        self.assertIn('value="manual" selected', manual)
        self.assertIn('name="manual_title"', manual)
        self.assertNotIn('name="drive_video_folder" required', manual)
        legacy = template.render(engine_instance="shortstudio", **data)
        self.assertNotIn('name="metadata_source"', legacy)
        self.assertIn('name="metadata_sheet_url_or_id" required', legacy)
        old_song = template.render(engine_instance="song",
                                   **{**data, "src": {"local_songmaker_enabled": 0},
                                      "p": {"metadata_source": "sheet", "manual_category_id": "10"}})
        self.assertIn('value="drive" selected', old_song)
        self.assertIn('value="sheet" selected', old_song)
        self.assertIn('name="drive_video_folder" required', old_song)

    def test_backend_and_browser_guards(self):
        source = BACKEND.read_text(encoding="utf-8")
        self.assertIn("DEFAULT 'sheet'", source)
        self.assertIn('metadata_source=="manual"', source)
        self.assertIn('"category_id":clean(row.get("category_id")) or "22"', source)
        # Switching an existing Sheet-backed profile to Manual must not write
        # new manual uploads back to its old, preserved Sheet URL/GID.
        self.assertIn('use_manual_metadata=clean(store.channel_binding(pid).get("metadata_source"))=="manual"', source)
        self.assertIn('metadata_sheet_url="" if use_manual_metadata', source)
        self.assertIn('metadata_sheet_gid="" if use_manual_metadata', source)
        self.assertIn('if state=="READY" and not schedule_at:', source)
        self.assertIn('self.scan_songmaker_local(profile_id, thumbnail=True)', source)
        self.assertIn("strict_thumbnail", source)
        js = JAVASCRIPT.read_text(encoding="utf-8")
        self.assertIn("SONG_PROFILE_MANUAL_METADATA_V1", js)
        self.assertIn("input.required=show", js)
        self.assertIn("e.target?.matches?.('#song-profile-video-source,#song-profile-metadata-source')", js)


if __name__ == "__main__":
    unittest.main()

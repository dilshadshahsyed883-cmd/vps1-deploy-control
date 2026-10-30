"""Song Uploader: restore the original group profile without altering channel mode."""
from __future__ import annotations

import ast
import os
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "apps/local-upload-manager/part01/lum_engine/auto_engine_v6.py"
TEMPLATE = ROOT / "apps/local-upload-manager/part01/templates/app.html"
SCRIPT = ROOT / "apps/local-upload-manager/part01/static/auto_engine_v6.js"


class Database:
    def __init__(self, current=None):
        self.current = current

    def get_auto_engine_profile(self, profile_id):
        return self.current

    def list_channel_groups(self):
        return [{"id": 10, "group_name": "Bengali"}]

    def list_channel_group_members(self, group_id):
        return [{"id": 1}, {"id": 2}] if group_id == 10 else []


class Store:
    def __init__(self, bound=False):
        self.bound = bound

    def channel_binding(self, profile_id):
        return {"channel_db_id": 1} if self.bound else {}


class GroupProfileRegression(TestCase):
    def save_wrapper(self):
        tree = ast.parse(BACKEND.read_text(encoding="utf-8"))
        funcs = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name == "save_profile_from_form"]
        func = funcs[-1]  # Current channel binding wrapper
        ns = {
            "Any": object, "V6Store": object, "Dict": dict, "os": os,
            "clean": lambda value: str(value or "").strip(),
            "_CHANNEL_LEGACY_SAVE_PROFILE": lambda db, st, form, pid: (77, form, pid),
        }
        exec(compile(ast.Module(body=[func], type_ignores=[]), str(BACKEND), "exec"), ns)
        return ns["save_profile_from_form"]

    def test_group_selection_routes_to_existing_multi_channel_engine(self):
        save = self.save_wrapper()
        data = {
            "profile_kind": "group", "channel_db_id": "",
            "channel_group_id": "10", "category_name": "Bengali Daily",
            "metadata_sheet_tab": "Bengali", "daily_upload_count": "4",
            "upload_scope": "per_channel", "work_mode": "manual_review",
        }
        with patch.dict(os.environ, {"LUM_ENGINE_INSTANCE": "song"}):
            saved, fields, profile_id = save(Database(), Store(), data, None)
        self.assertEqual((saved, profile_id), (77, None))
        self.assertEqual(fields["channel_group_id"], "10")
        self.assertEqual(fields["daily_upload_count"], "4")

    def test_unmatched_or_empty_group_is_rejected(self):
        save = self.save_wrapper()
        for group in ("", "999", "no-group"):
            with self.subTest(group=group), patch.dict(os.environ, {"LUM_ENGINE_INSTANCE": "song"}):
                with self.assertRaises(ValueError):
                    save(Database(), Store(), {"profile_kind": "group",
                         "channel_group_id": group, "category_name": "Bengali",
                         "metadata_sheet_tab": "Bengali"}, None)

    def test_existing_channel_binding_cannot_be_converted_to_group(self):
        save = self.save_wrapper()
        with patch.dict(os.environ, {"LUM_ENGINE_INSTANCE": "song"}):
            with self.assertRaisesRegex(ValueError, "Channel profile"):
                save(Database(current={"id": 9}), Store(bound=True),
                     {"profile_kind": "group", "channel_group_id": "10",
                      "category_name": "Bengali",
                      "metadata_sheet_tab": "Bengali"}, 9)
            with self.assertRaisesRegex(ValueError, "Group profile"):
                save(Database(current={"id": 9}), Store(),
                     {"profile_kind": "channel", "channel_db_id": "1"}, 9)

    def test_template_and_js_present_group_and_single_modes(self):
        html = TEMPLATE.read_text(encoding="utf-8")
        Environment().parse(html)
        self.assertIn('id="lum-ae-profile-kind"', html)
        self.assertIn('name="channel_group_id"', html)
        self.assertIn("ae.groups or []", html)
        self.assertIn('data-ae-group-fields', html)
        self.assertIn('name="daily_upload_count"', html)
        self.assertIn('name="upload_scope"', html)
        self.assertIn('data-ae-single-choice', html)
        self.assertIn('name="channel_db_id"', html)
        script = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("function updateSongProfileKind()", script)
        self.assertIn("channelSelect.disabled=group", script)
        self.assertIn("groupSelect.disabled=!group", script)
        base = (ROOT / "apps/local-upload-manager/part01/templates/base.html").read_text()
        self.assertIn("auto_engine_v6.js?v=20261030-song-group-profiles-v1", base)

    def test_legacy_group_eligibility_and_duplicate_guards_preserved(self):
        backend = BACKEND.read_text(encoding="utf-8")
        self.assertIn("pc_db.list_channel_group_members(int(gid),active_only=True)", backend)
        self.assertIn("classify_google_json(clean(r.get(\"token_file_path\"))) == VALID_TOKEN", backend)
        self.assertIn("if pc_db.is_channel_filename_duplicate(", backend)
        self.assertIn("_CHANNEL_LEGACY_SAVE_PROFILE(pc_db, store, form, profile_id)", backend)

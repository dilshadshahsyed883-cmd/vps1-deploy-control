"""Song-only channel source and effective-mode regressions (offline, no VPS changes)."""
import ast
import os
from pathlib import Path
from typing import Any
from unittest import TestCase
from unittest.mock import patch

from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "apps/local-upload-manager/part01/lum_engine/standalone_app.py"
TEMPLATE = ROOT / "apps/local-upload-manager/part01/templates/app.html"


def extract_function(name, namespace):
    tree = ast.parse(BACKEND.read_text(encoding="utf-8"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
    function.decorator_list = []
    scope = dict(namespace)
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(BACKEND), "exec"), scope)
    return scope[name]


class SongSheetsOnlyTests(TestCase):
    def test_manual_onboarding_section_is_absent_for_both_song_roles(self):
        src = TEMPLATE.read_text(encoding="utf-8")
        start = src.index('<div data-lum-channel-panel="manager">')
        stop = src.index('<section class="lum-channel-summary">', start)
        fragment = Environment(autoescape=True).from_string(src[start:stop])
        for admin in (False, True):
            html = fragment.render(engine_instance="song", user={"is_admin": admin},
                                   base_path="/song-uploader", channel_rows=[], csrf="csrf")
            self.assertNotIn("Your YouTube channels", html)
            self.assertNotIn("/actions/studio-tenant/channel-create", html)
            self.assertNotIn("/actions/studio-tenant/group-create", html)
        other = fragment.render(engine_instance="shortstudio", user={"is_admin": False},
                                base_path="/short-uploader", channel_rows=[], csrf="csrf")
        self.assertNotIn("Your YouTube channels", other)
        copyright_html = fragment.render(engine_instance="copyright",
                                         user={"is_admin": False},
                                         base_path="/copyright-uploader", channel_rows=[], csrf="csrf")
        self.assertIn("Your YouTube channels", copyright_html)

    def test_legacy_off_modes_render_on_without_enabling_engine(self):
        class Store:
            def settings(self):
                return {"sheets_sync_enabled": "0", "auto_engine_enabled": "0", "private": "secret"}
            def counts(self, owner=None):
                return {"TOTAL": 0}
            def setting(self, name, default=""):
                return {"engine_enabled": "0", "pause_after_current": "1"}.get(name, default)
        class Worker:
            active_project_id = None
        stats = extract_function("stats_payload", {
            "Any": Any, "os": os, "store": Store(),
            "worker": Worker(), "all_channels": lambda user: [],
        })
        with patch.dict(os.environ, {"LUM_ENGINE_INSTANCE": "song"}):
            for admin in (False, True):
                result = stats({"username": "tenant", "is_admin": admin})
                self.assertEqual(result["settings"]["sheets_sync_enabled"], "1")
                self.assertEqual(result["settings"]["auto_engine_enabled"], "1")
                self.assertFalse(result["engine_enabled"])
                self.assertTrue(result["pause_after_current"])
                if not admin:
                    self.assertNotIn("private", result["settings"])
        with patch.dict(os.environ, {"LUM_ENGINE_INSTANCE": "engine1"}):
            result = stats({"username": "admin", "is_admin": True})
            self.assertEqual(result["settings"]["sheets_sync_enabled"], "0")
            self.assertEqual(result["settings"]["auto_engine_enabled"], "0")

    def test_saved_off_does_not_block_song_sheets_sync(self):
        from types import SimpleNamespace
        class Store:
            def setting(self, key, default=""):
                return "0"
        calls = []
        user = {"username": "alice", "is_admin": 1}
        db = SimpleNamespace(
            get_sheet_source=lambda sid: {"id": sid, "sheet_url_or_id": "own"} if sid == 7 else None,
            list_sheet_sources=lambda: [{"id": 7, "sheet_name": "Mine",
                                         "sheet_url_or_id": "own", "enabled": 1},
                                        {"id": 8, "sheet_name": "Foreign",
                                         "sheet_url_or_id": "foreign", "enabled": 1}],
        )
        environment = {
            "Request": object, "Form": lambda value: value, "os": os,
            "current_user": lambda request: user,
            "require_csrf": lambda request, token: None,
            "require_heavy": lambda user: None,
            "store": Store(), "db": db,
            "_studio_require": lambda kind, sid, viewer: (
                calls.append(("require", sid)) if sid == 7 else
                (_ for _ in ()).throw(PermissionError("foreign sheet"))),
            "_studio_allowed_ids": lambda kind, viewer: {7},
            "tenant_fetch_sheet_csv": lambda link, gid: (
                calls.append(("fetch", link)) or [{"row": "own"}]),
            "tenant_rows_to_sheet_rows": lambda rows, path: rows,
            "tenant_token_dir": "/tmp",
            "studio_tenants": SimpleNamespace(
                sync_owned_sheet_rows=lambda username, sid, rows: (
                    calls.append(("sync", sid)) or {"ok": True, "rows": 1})),
            "web_sync_sheet_source_by_id": lambda *args: (
                _ for _ in ()).throw(AssertionError("Global source sync")),
            "web_sync_all_sheet_sources": lambda *args: (
                _ for _ in ()).throw(AssertionError("Global all sync")),
            "redirect": lambda path, **kwargs: (path, kwargs),
        }
        single = extract_function("sheets_sync", environment)
        all_sources = extract_function("sheets_sync_all", environment)
        with patch.dict(os.environ, {"LUM_ENGINE_INSTANCE": "song"}):
            for admin in (False, True):
                user["is_admin"] = admin
                calls.clear()
                self.assertIn("Sheet synced", single(object(), "token", 7)[1]["message"])
                self.assertIn("Updated 1", all_sources(object(), "token")[1]["message"])
                self.assertEqual([x for x in calls if x[0] == "fetch"],
                                 [("fetch", "own"), ("fetch", "own")])
        with patch.dict(os.environ, {"LUM_ENGINE_INSTANCE": "engine1"}):
            self.assertIn("Enable Sheets Sync",
                          single(object(), "token", 7)[1]["error"])
            self.assertIn("Enable Sheets Sync",
                          all_sources(object(), "token")[1]["error"])

    def test_new_song_database_defaults_to_modes_on_not_upload_running(self):
        source = (ROOT / "apps/local-upload-manager/part01/lum_engine/config.py").read_text()
        tree = ast.parse(source)
        assignment = next(node for node in tree.body
                          if isinstance(node, ast.Assign) and any(
                              isinstance(target, ast.Name) and target.id == "DEFAULT_SETTINGS"
                              for target in node.targets))
        compiled = compile(ast.Module(body=[assignment], type_ignores=[]),
                           "config.py", "exec")
        for instance in ("song", "shortstudio", "engine1"):
            with patch.dict(os.environ, {"LUM_ENGINE_INSTANCE": instance}):
                values = {"os": os}
                exec(compiled, values)
                settings = values["DEFAULT_SETTINGS"]
                expected = "1" if instance == "song" else "0"
                self.assertEqual(settings["sheets_sync_enabled"], expected)
                self.assertEqual(settings["auto_engine_enabled"], expected)
                if instance == "song":
                    self.assertEqual(settings["engine_enabled"], "0")

    def test_song_disallows_direct_channel_and_group_creation_for_all_users(self):
        class NotFound(Exception):
            def __init__(self, status_code, detail):
                super().__init__(detail)
                self.status_code = status_code
        class Tenants:
            def create_channel(self, *args):
                raise AssertionError("Song manual channel creation called")
            def create_group(self, *args):
                raise AssertionError("Song manual group creation called")
        user = {"username": "admin", "is_admin": True}
        environment = {
            "Request": object, "Form": lambda value: value,
            "os": os, "HTTPException": NotFound,
            "current_user": lambda request: user,
            "require_csrf": lambda request, token: None,
            "require_heavy": lambda user: None,
            "studio_tenants": Tenants(),
        }
        channel = extract_function("studio_tenant_channel_create", environment)
        group = extract_function("studio_tenant_group_create", environment)
        with patch.dict(os.environ, {"LUM_ENGINE_INSTANCE": "song"}):
            for admin in (False, True):
                user["is_admin"] = admin
                for action in (lambda: channel(object(), "token", "UC123", "Name"),
                               lambda: group(object(), "token", "Group", [])):
                    with self.assertRaises(NotFound) as result:
                        action()
                    self.assertEqual(result.exception.status_code, 404)

"""Regression: Short Uploader profile Save uses the real safe folder helper.

Disposable folder trees; no engine, production DB, uploads, or schedule actions.
"""
from __future__ import annotations

import ast
import asyncio
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "apps/local-upload-manager/part01/lum_engine/standalone_app.py"
ENGINE = ROOT / "apps/local-upload-manager/part01/lum_engine/auto_engine_v6.py"


def function_from_ast(path, function, environment):
    module = ast.parse(path.read_text(encoding="utf-8"))
    func = next(n for n in module.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == function)
    func.decorator_list = []
    generated = ast.Module(body=[ast.ImportFrom(module="__future__",
                 names=[ast.alias(name="annotations")], level=0), func], type_ignores=[])
    exec(compile(ast.fix_missing_locations(generated), str(path), "exec"), environment)
    return environment[function]


class ShortProfileFolderRegression(unittest.TestCase):
    def test_helper_is_imported_from_existing_hardened_engine(self):
        source = WEB.read_text(encoding="utf-8")
        engine = ENGINE.read_text(encoding="utf-8")
        self.assertIn("shortstudio_folder_path as v6_shortstudio_folder_path,", source)
        self.assertIn("def shortstudio_folder_path(name: str)", engine)
        self.assertIn("folder=v6_shortstudio_folder_path(folder_name)", source)
        self.assertIn('marker=folder/".studio_owner"', source)
        self.assertIn("marker.is_symlink()", source)
        self.assertIn('studio_tenants.bind("profile"', source)
        release = (ROOT/"production/short_uploader_group_release.py").read_text()
        runtime = (ROOT/"production/runtime.py").read_text()
        workflow = (ROOT/".github/workflows/vps1-short-studio-release.yml").read_text()
        self.assertIn('("lum_engine/standalone_app.py",) if profile_folder_only', release)
        self.assertIn("verify_live(profile_folder_only=True)", release)
        self.assertIn("SHORT_PROFILE_FOLDER_RELEASE", release)
        self.assertIn("[vps1-shortstudio-profile-folder-fix]", runtime)
        self.assertIn("profile_folder_only=True", runtime)
        self.assertIn("[vps1-shortstudio-profile-folder-fix]", workflow)
        self.assertIn("AUTO_SCRIPT=OFFLINE", workflow)

    def test_both_roles_can_save_owned_folder_and_foreign_data_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root/"Hania Asif"
            folder.mkdir()
            marker = folder/".studio_owner"
            marker.write_text("alice\n", encoding="utf-8")
            hardened = {"Path":Path, "SHORT_FOLDER_PATTERN":re.compile(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,63}\Z"),
                        "SHORT_STUDIO_ROOT":root}
            path = function_from_ast(ENGINE, "shortstudio_folder_path", hardened)
            self.assertEqual(path("Hania Asif"), folder)
            with self.assertRaises(ValueError):
                path("../other")
            for is_admin in (False, True):
                saved, owned, calls = [], [], []
                user = {"username":"Alice","is_admin":is_admin}
                environment = {
                    "os":os,
                    "current_user":lambda req: user,
                    "require_heavy":lambda u: None,
                    "require_csrf":lambda req, supplied: None if supplied=="csrf" else
                        (_ for _ in ()).throw(PermissionError("CSRF")),
                    "_v6_profile":lambda pid, req: owned.append(pid),
                    "_studio_require":lambda kind, rid, u: calls.append((kind,rid))
                        if (kind,rid)==("group",7) else
                        (_ for _ in ()).throw(PermissionError("foreign group")),
                    "v6_shortstudio_folder_path":path,
                    "v6_save_profile_from_form":lambda db,store,fields,pid:
                        saved.append((fields,pid)) or 42,
                    "db":object(),
                    "v6_store":object(),
                    "studio_tenants":SimpleNamespace(bind=lambda kind,pid,owner: owned.append((kind,pid,owner))),
                    "_song_account_scope":lambda u: False,
                    "redirect":lambda location,**kwargs:(location,kwargs),
                }
                handle = function_from_ast(WEB, "auto_v6_profile_save", environment)
                class Request:
                    def __init__(self, data):self.data=data
                    async def form(self):return self.data

                def attempt(foldername, group="7", profile="42"):
                    return asyncio.run(handle(Request({
                        "csrf_token":"csrf","profile_id":profile,"channel_group_id":group,
                        "short_vps_folder":foldername,"work_mode":"manual",
                    })))
                with patch.dict(os.environ,{"LUM_ENGINE_INSTANCE":"shortstudio"}):
                    result = attempt("Hania Asif")
                    self.assertEqual(result[1].get("message"),"Auto Engine profile #42 saved.")
                    self.assertEqual(saved[-1][0]["short_vps_folder"],"Hania Asif")
                    self.assertEqual(owned[-1],("profile",42,"Alice"))
                    self.assertEqual(calls[-1],("group",7))

                    before = len(saved)
                    marker.write_text("bob", encoding="utf-8")
                    foreign = attempt("Hania Asif")
                    self.assertIn("belongs to another user",foreign[1]["error"])
                    self.assertEqual(len(saved),before)

                    marker.write_text("alice", encoding="utf-8")
                    traversal = attempt("../other")
                    self.assertIn("valid Short Maker VPS folder",traversal[1]["error"])
                    self.assertEqual(len(saved),before)

                    invalid_group = attempt("Hania Asif",group="8")
                    self.assertIn("foreign group",invalid_group[1]["error"])
                    self.assertEqual(len(saved),before)

                    marker.unlink()
                    empty_marker = attempt("Hania Asif")
                    self.assertIn("belongs to another user",empty_marker[1]["error"])
                    self.assertEqual(len(saved),before)

                    (root/"Linked").symlink_to(folder,target_is_directory=True)
                    linked = attempt("Linked")
                    self.assertIn("symlinks are prohibited",linked[1]["error"])
                    self.assertEqual(len(saved),before)


if __name__ == "__main__":
    unittest.main()

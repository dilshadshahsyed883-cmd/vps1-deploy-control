"""Short Uploader: admin and member have equal *owned* operational permissions.

Offline / disposable fixtures. No production media, tokens, scheduler or jobs.
"""
from __future__ import annotations

import ast
import importlib.util
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from jinja2 import Environment

ROOT=Path(__file__).resolve().parents[1]
APP=ROOT/"apps/local-upload-manager/part01/lum_engine/standalone_app.py"
TENANTS=ROOT/"apps/local-upload-manager/part01/lum_engine/studio_tenants.py"
TEMPLATE=ROOT/"apps/local-upload-manager/part01/templates/app.html"


def extract(name, bindings):
    tree=ast.parse(APP.read_text(encoding="utf-8"))
    node=next(x for x in tree.body if isinstance(x,(ast.FunctionDef,ast.AsyncFunctionDef)) and x.name==name)
    node.decorator_list=[]
    module=ast.Module(body=[ast.ImportFrom(module="__future__",
                      names=[ast.alias(name="annotations")],level=0),node],type_ignores=[])
    ast.fix_missing_locations(module)
    env={"Form":lambda default:default,**bindings}
    exec(compile(module,str(APP),"exec"),env)
    return env[name]


class ShortOwnerParityTests(unittest.TestCase):
    def test_admin_and_member_own_only_tenant_records(self):
        spec=importlib.util.spec_from_file_location("short_owner_parity",TENANTS)
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp:
            class DB:
                def connect(self):return sqlite3.connect(temp+"/owners.sqlite")
            acl=module.StudioTenants(DB())
            acl.bind("channel",11,"alice")
            acl.bind("channel",22,"bob")
            acl.bind("profile",31,"alice")
            acl.bind("profile",32,"bob")
            acl.bind("sheet",41,"alice")
            acl.bind("sheet",42,"bob")
            acl.bind("group",51,"alice")
            acl.bind("group",52,"bob")
            with patch.dict(os.environ,{"LUM_ENGINE_INSTANCE":"shortstudio"}):
                for user in ({"username":"Alice","is_admin":True},{"username":"Alice","is_admin":False}):
                    for kind,own,foreign in (("channel",11,22),("profile",31,32),
                                             ("sheet",41,42),("group",51,52)):
                        self.assertEqual(acl.allowed_ids(kind,user),{own})
                        acl.require(kind,own,user)
                        with self.assertRaises(PermissionError):
                            acl.require(kind,foreign,user)
                    with self.assertRaises(PermissionError):
                        acl.require("channel",99,user)
                with self.assertRaises(PermissionError):
                    acl.assign_by_admin("channel",99,"alice",
                                        {"username":"alice","is_admin":True})
            with patch.dict(os.environ,{"LUM_ENGINE_INSTANCE":"song"}):
                self.assertIsNone(acl.allowed_ids("channel",{"username":"alice","is_admin":True}))

    def test_short_admin_and_member_never_use_global_project_or_log_queries(self):
        calls=[]
        store=SimpleNamespace(
            list=lambda **kw:(calls.append(("list",kw)) or []),
            events=lambda **kw:(calls.append(("events",kw)) or []))
        fn1=extract("get_projects",{"store":store,"os":os,"project_view":lambda p:p})
        fn2=extract("get_logs",{"store":store,"os":os})
        with patch.dict(os.environ,{"LUM_ENGINE_INSTANCE":"shortstudio"}):
            for admin in (True,False):
                who={"username":"Alice","is_admin":admin}
                fn1(who);fn2(who)
        self.assertEqual(calls,[("list",{"owner":"Alice","limit":500}),
                                ("events",{"owner":"Alice","limit":300})]*2)

    def test_queue_rejects_foreign_selection_for_both_roles(self):
        jobs=[{"id":101,"auto_engine_profile_id":1,"channel_db_id":11,"status":"FAILED"},
              {"id":202,"auto_engine_profile_id":2,"channel_db_id":22,"status":"FAILED"},
              {"id":303,"auto_engine_profile_id":1,"channel_db_id":22,"status":"PAUSED"}]
        owners={11:"alice",22:"bob"}
        current={"username":"alice","is_admin":True}
        fn=extract("_short_owned_queue_jobs",{
            "os":os,"current_user":lambda request:current,
            "_short_owned_automation_profiles":lambda request:[{"id":1}],
            "db":SimpleNamespace(list_auto_engine_jobs=lambda limit=5000:jobs),
            "studio_tenants":SimpleNamespace(owner=lambda kind,entity_id:owners.get(entity_id,""))
        })
        with patch.dict(os.environ,{"LUM_ENGINE_INSTANCE":"shortstudio"}):
            for admin in (True,False):
                current["is_admin"]=admin
                self.assertEqual([j["id"] for j in fn(object())],[101])
                self.assertEqual([j["id"] for j in fn(object(),[101])],[101])
                for selected in ([202],[303],[999],[101,202],[101,101],[-1]):
                    with self.subTest(admin=admin,selected=selected):
                        with self.assertRaises(PermissionError):fn(object(),selected)

    def test_short_token_rescan_with_no_owned_channels_never_scans_all(self):
        scanned=[]
        class ACL:
            def allowed_ids(self,kind,user):return set()
            def require(self,*args):raise AssertionError("No channel should be selected")
        fn=extract("channels_rescan_tokens",{
            "os":os,"current_user":lambda request:{"username":"alice","is_admin":True},
            "require_csrf":lambda *args:None,"require_heavy":lambda user:None,
            "studio_tenants":ACL(),
            "rescan_channel_tokens":lambda db,ids:(scanned.append(ids) or {"checked":0,"updated":0,"missing":0,"invalid":0}),
            "db":object(),"redirect":lambda path,**kw:(path,kw)
        })
        with patch.dict(os.environ,{"LUM_ENGINE_INSTANCE":"shortstudio"}):
            result=fn(object(),"csrf","all_missing",[])
        self.assertEqual(scanned,[])
        self.assertIn("No owned channels",result[1]["message"])

    def test_parity_release_deploys_full_short_acl_and_rolls_back(self):
        runtime=(ROOT/"production/runtime.py").read_text(encoding="utf-8")
        install=(ROOT/"production/short_uploader_group_release.py").read_text(encoding="utf-8")
        workflow=(ROOT/".github/workflows/vps1-short-studio-release.yml").read_text(encoding="utf-8")
        self.assertIn("[vps1-shortstudio-owner-parity]",runtime)
        self.assertIn("owner_parity_only=True",runtime)
        self.assertIn("owner_parity_only=False",install)
        self.assertIn('"lum_engine/studio_tenants.py"',install)
        self.assertIn("SHORT_OWNER_PARITY_LIVE",install)
        self.assertIn("SHORT_GROUP_ROLLBACK=PASS",install)
        self.assertIn("[vps1-shortstudio-owner-parity]",workflow)
        self.assertIn("AUTO_SCRIPT=OFFLINE",workflow)

    def test_ui_uses_identical_owned_controls_for_admin_and_member(self):
        source=TEMPLATE.read_text(encoding="utf-8")
        Environment().parse(source)
        self.assertIn("oauth_client_ready if engine_instance == 'shortstudio' else clients",source)
        self.assertIn("{% if engine_instance == 'shortstudio' %}",source)
        self.assertIn("engine_instance in ['song','shortstudio'] or user.is_admin",source)
        self.assertIn("engine_instance != 'shortstudio'",source)
        self.assertIn("{{ auth_panel(False) }}",source)
        self.assertIn('action="{{ base_path }}/actions/auto-v6/retry-failed"',source)
        self.assertIn('action="{{ base_path }}/actions/auto-v6/remove-selected"',source)


if __name__=="__main__":
    unittest.main()

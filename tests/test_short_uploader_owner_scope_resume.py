"""Short Uploader owner-scope repair and Resume Paused regression tests.

Disposable SQLite fixtures only: no VPS services, OAuth, Google Sheets,
production media, or real upload activity.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
ENGINE=ROOT/"apps/local-upload-manager/part01/lum_engine/auto_engine_v6.py"
WEB=ROOT/"apps/local-upload-manager/part01/lum_engine/standalone_app.py"
WORKER=ROOT/"apps/local-upload-manager/part01/lum_engine/auto_engine_v6_worker.py"


def extracted(path, names, bindings):
    tree=ast.parse(path.read_text(encoding="utf-8"))
    nodes=[node for node in tree.body
           if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name in names]
    if len(nodes)!=len(names):
        raise AssertionError("Missing backend function: "+str(names))
    for node in nodes:
        node.decorator_list=[]
    module=ast.Module(body=[ast.ImportFrom(module="__future__",
                      names=[ast.alias(name="annotations")],level=0)]+nodes,type_ignores=[])
    ast.fix_missing_locations(module)
    namespace={"__package__":"lum_engine","os":os,"sqlite3":sqlite3,"Form":lambda default:default}
    namespace.update(bindings)
    exec(compile(module,str(path),"exec"),namespace)
    return namespace


class Ownership:
    def __init__(self, owners):
        self.owners=owners

    def owner(self, kind, entity_id):
        return self.owners.get((kind,int(entity_id)))


class Fixture:
    def __init__(self, directory):
        self.path=str(Path(directory)/"channels.sqlite3")
        self.state_path=str(Path(directory)/"engine.sqlite3")
        self.profiles=[
            {"id":10,"channel_group_id":1,"enabled":1,"category_name":"Alice"},
            {"id":20,"channel_group_id":2,"enabled":1,"category_name":"Bob"},
            {"id":30,"channel_group_id":3,"enabled":1,"category_name":"Mixed"},
            {"id":40,"channel_group_id":4,"enabled":1,"category_name":"Unassigned"},
            {"id":50,"channel_group_id":1,"enabled":0,"category_name":"Disabled"},
            {"id":60,"channel_db_id":22,"enabled":1,"category_name":"Bad direct"},
        ]
        self.acl=Ownership({
            ("profile",10):"alice",("group",1):"alice",("channel",11):"alice",
            ("profile",20):"bob",("group",2):"bob",("channel",22):"bob",
            ("profile",30):"alice",("group",3):"alice",
            ("profile",50):"alice",("profile",60):"alice",
        })
        with self.connect() as con:
            con.execute("CREATE TABLE channel_group_members(group_id INTEGER,channel_db_id INTEGER)")
            con.executemany("INSERT INTO channel_group_members VALUES(?,?)",[(1,11),(2,22),(3,22),(4,11)])
        with self.store_connect() as con:
            con.execute("CREATE TABLE engine_state(singleton INTEGER PRIMARY KEY,running INTEGER,pause_after_current INTEGER,manual_profile_ids TEXT,updated_at TEXT DEFAULT CURRENT_TIMESTAMP)")
            con.execute("INSERT INTO engine_state(singleton,running,pause_after_current,manual_profile_ids) VALUES(1,1,0,NULL)")
        self.store=SimpleNamespace(connect=self.store_connect, engine=self.engine,
            set_engine=self.set_engine,group_settings=lambda pid:{},
            channel_binding=lambda pid:{})

    def connect(self):
        return sqlite3.connect(self.path)

    def list_auto_engine_profiles(self):
        return [dict(profile) for profile in self.profiles]

    def store_connect(self):
        con=sqlite3.connect(self.state_path)
        con.row_factory=sqlite3.Row
        return con

    def engine(self):
        with self.store_connect() as con:
            return dict(con.execute("SELECT * FROM engine_state WHERE singleton=1").fetchone())

    def set_engine(self, **values):
        with self.store_connect() as con:
            for key,value in values.items():
                if key in {"running","pause_after_current"}:
                    con.execute("UPDATE engine_state SET "+key+"=? WHERE singleton=1",(int(value),))

    def state(self, *, running=1, manual=None, paused=0):
        with self.store_connect() as con:
            con.execute("UPDATE engine_state SET running=?,pause_after_current=?,manual_profile_ids=? WHERE singleton=1",(running,paused,manual))


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db=Fixture(self.temp.name)
        package=ModuleType("lum_engine")
        package.__path__=[]
        tenant=ModuleType("lum_engine.studio_tenants")
        tenant.StudioTenants=lambda pc_db:pc_db.acl
        patcher=patch.dict(sys.modules,{"lum_engine":package,"lum_engine.studio_tenants":tenant})
        patcher.start()
        self.addCleanup(patcher.stop)
        envpatch=patch.dict(os.environ,{"LUM_ENGINE_INSTANCE":"shortstudio"})
        envpatch.start()
        self.addCleanup(envpatch.stop)
        self.ns=extracted(ENGINE,["shortstudio_verified_profile_ids",
                                  "ensure_shortstudio_owner_scope","auto_start_new_jobs"],{})
        self.store=self.db.store
        self.verify=self.ns["shortstudio_verified_profile_ids"]
        self.reconcile=self.ns["ensure_shortstudio_owner_scope"]

    def test_running_null_reconstructs_all_valid_owners_not_only_clicking_user(self):
        self.assertEqual(self.verify(self.db,self.store),{10:True,20:True,50:False})
        self.assertEqual(self.reconcile(self.db,self.store,{10}),[10,20])
        self.assertEqual(self.db.engine()["manual_profile_ids"],"10,20")

    def test_stopped_engine_starts_only_explicit_requested_account(self):
        self.db.state(running=0,manual=None)
        self.assertEqual(self.reconcile(self.db,self.store,[10]),[10])
        self.assertEqual(self.db.engine()["manual_profile_ids"],"10")

    def test_active_foreign_scope_is_preserved_when_resuming_own_jobs(self):
        self.db.state(running=1,manual="20")
        self.assertEqual(self.reconcile(self.db,self.store,[10]),[10,20])
        self.assertEqual(self.db.engine()["manual_profile_ids"],"10,20")

    def test_mixed_unassigned_and_foreign_direct_targets_are_rejected(self):
        for pid in (30,40,60):
            with self.subTest(profile=pid):
                with self.assertRaises(PermissionError):
                    self.reconcile(self.db,self.store,[pid])
                self.assertIsNone(self.db.engine()["manual_profile_ids"])

    def test_existing_global_pause_remains_in_effect(self):
        self.db.state(running=1,manual=None,paused=1)
        with self.assertRaises(PermissionError):
            self.reconcile(self.db,self.store,[10])
        self.assertIsNone(self.db.engine()["manual_profile_ids"])

    def test_auto_start_never_leaves_new_short_engine_running_unscoped(self):
        self.db.state(running=0,manual=None)
        self.assertTrue(self.ns["auto_start_new_jobs"](self.store,self.db.profiles[0],1,pc_db=self.db))
        self.assertEqual(self.db.engine()["manual_profile_ids"],"10")
        self.assertEqual(self.db.engine()["running"],1)

    def test_reconcile_filters_stale_unowned_scope(self):
        self.db.state(running=1,manual="20,30,40,50,60")
        self.assertEqual(self.reconcile(self.db,self.store,[10]),[10,20,50])
        self.assertEqual(self.db.engine()["manual_profile_ids"],"10,20,50")

    def test_resume_route_starts_owned_scope_before_requeue(self):
        calls=[]
        jobs=[{"id":123,"auto_engine_profile_id":10,"channel_db_id":11,"status":"PAUSED"}]
        fn=extracted(WEB,["auto_v6_resume_paused"],{
            "current_user":lambda req:{"username":"alice","is_admin":False},
            "require_csrf":lambda *args:None,"require_heavy":lambda *args:None,
            "_short_owned_queue_jobs":lambda req,statuses=None:jobs,
            "_short_owner_start_scoped":lambda req,pids:calls.append(("scope",set(pids))),
            "db":SimpleNamespace(resume_paused=lambda **kw:(calls.append(("resume",kw)) or 1)),
            "redirect":lambda path,**kw:(path,kw),
        })["auto_v6_resume_paused"]
        result=fn(object(),"token")
        self.assertEqual(calls[0],("scope",{10}))
        self.assertEqual(calls[1],("resume",{"auto_engine_only":True,"video_ids":[123]}))
        self.assertIn("Resumed 1 owned",result[1]["message"])

    def test_worker_validates_scope_before_claiming(self):
        text=WORKER.read_text(encoding="utf-8")
        section=text[text.index('state=store.engine()',text.index('def main()')):]
        self.assertLess(section.index("ensure_shortstudio_owner_scope(db,store)"),
                        section.index("db.claim_available_jobs("))
        self.assertLess(section.index("shortstudio_verified_profile_ids(db,store)"),
                        section.index("db.claim_available_jobs("))


if __name__=="__main__":
    unittest.main()

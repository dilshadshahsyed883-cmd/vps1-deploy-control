"""Short Uploader member Automation UI, tenant scoping and isolated release contracts."""
from __future__ import annotations

import ast
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from jinja2 import Environment

ROOT=Path(__file__).resolve().parents[1]
TEMPLATE=ROOT/"apps/local-upload-manager/part01/templates/app.html"
BACKEND=ROOT/"apps/local-upload-manager/part01/lum_engine/standalone_app.py"
SCRIPT=ROOT/"apps/local-upload-manager/part01/static/auto_engine_v6.js"


def extract(name, bindings):
    tree=ast.parse(BACKEND.read_text(encoding="utf-8"))
    node=next(x for x in tree.body if isinstance(x,(ast.FunctionDef,ast.AsyncFunctionDef)) and x.name==name)
    node.decorator_list=[]
    module=ast.Module(body=[ast.ImportFrom(module="__future__",
                    names=[ast.alias(name="annotations")],level=0),node],type_ignores=[])
    ast.fix_missing_locations(module)
    scope={"Form": lambda value: value, **bindings}
    exec(compile(module,str(BACKEND),"exec"),scope)
    return scope[name]


class ShortMemberControls(TestCase):
    def test_ui_restores_five_controls_for_short_admin_and_member(self):
        src=TEMPLATE.read_text(encoding="utf-8")
        start=src.rfind("{% if engine_instance in ['song', 'shortstudio'] %}",0,
                        src.index('data-ae-open-schedules>Profile Schedules'))
        end=src.index('<div class="lum-table-wrap lum-ae-automation-profiles-wrap">',start)
        controls=Environment().from_string(src[start:end])
        a=src.index('<section class="lum-card lum-ae-automation-planner"')
        z=src.index('<section class="lum-card lum-ae-pc-controls"',a)
        preflight=Environment().from_string(src[a:z])
        a=src.index('<dialog id="lum-ae-schedules-dialog"')
        z=src.index("</dialog>",a)+len("</dialog>")
        dialog=Environment().from_string(src[a:z])
        for mode,admin,expected in (("shortstudio",False,True),("shortstudio",True,True),
                                    ("song",False,True),("copyright",False,False)):
            with self.subTest(mode=mode,admin=admin):
                ctx={"engine_instance":mode,"user":{"is_admin":admin},"csrf":"test",
                     "base_path":"/short-uploader","ae":{"profiles":[]},"p":None}
                html=controls.render(**ctx)
                for label in ("Profile Schedules","Preview Next Jobs",
                              "Start All Profiles","Sync All Channels"):
                    self.assertEqual(label in html,expected,label)
                p=preflight.render(**ctx)
                self.assertEqual('data-ae-queue-preflight' in p,expected)
                self.assertEqual('/actions/auto-v6/start-all' in p,expected)
                self.assertEqual('/actions/auto-v6/profile-schedules/save'
                                 in dialog.render(**ctx),expected)

    def test_owned_profile_selection_omits_foreign_and_mixed_channel_groups(self):
        user={"username":"alice","is_admin":False}
        class Tenants:
            def allowed_ids(self,kind,viewer):
                self_assert=kind=="profile" and viewer["username"]=="alice"
                assert self_assert
                return {10,20}
            def owner(self,kind,entity_id):
                return "alice" if kind=="profile" and entity_id in {10,20} else "bob"
        def verified(pid,req):
            if pid==20: raise PermissionError("group contains foreign channel")
            return {"id":pid,"enabled":1}
        fn=extract("_short_owned_automation_profiles",{
            "os":os,"current_user":lambda req:user,"studio_tenants":Tenants(),
            "db":SimpleNamespace(list_auto_engine_profiles=lambda:[{"id":10},{"id":20},{"id":30}]),
            "v6_store":SimpleNamespace(group_settings=lambda pid:{}),
            "_v6_profile":verified})
        with patch.dict(os.environ,{"LUM_ENGINE_INSTANCE":"shortstudio"}):
            self.assertEqual([x["id"] for x in fn(object())],[10])

    def test_admin_bulk_selection_never_expands_to_global_records(self):
        profiles={10:{"id":10,"enabled":1},20:{"id":20,"enabled":1},
                  30:{"id":30,"enabled":1,"channel_group_id":5},
                  40:{"id":40,"enabled":1,"channel_db_id":7}}
        owners={("profile",10):"alice",("profile",20):"bob",
                ("profile",30):"alice",("profile",40):"alice",
                ("group",5):"alice",("channel",9):"bob",
                ("channel",7):"bob"}
        class Tenants:
            def allowed_ids(self,kind,user):
                return None if user["is_admin"] else {10,30,40}
            def owner(self,kind,entity_id):
                return owners.get((kind,entity_id),"")
        class DB:
            def list_auto_engine_profiles(self):return list(profiles.values())
            def connect(self):return self
            def __enter__(self):return self
            def __exit__(self,*args):return False
            def execute(self,*args):return self
            def fetchall(self):return [(9,)]
        user={"username":"alice","is_admin":True}
        fn=extract("_short_owned_automation_profiles",{
            "os":os,"current_user":lambda req:user,"studio_tenants":Tenants(),
            "db":DB(),"v6_store":SimpleNamespace(group_settings=lambda pid:{}),
            "_v6_profile":lambda pid,req:profiles[pid]})
        with patch.dict(os.environ,{"LUM_ENGINE_INSTANCE":"shortstudio"}):
            self.assertEqual([x["id"] for x in fn(object())],[10])
            user["is_admin"]=False
            self.assertEqual([x["id"] for x in fn(object())],[10])

    def test_short_admin_start_all_has_no_global_fallback(self):
        selected={"id":10,"enabled":1,"category_name":"Mine"}
        actions=[]
        class Store:
            def engine(self):return {"manual_profile_ids":"99","running":1,"pause_after_current":0}
            def group_settings(self,pid):return {}
            def set_manual_profile_ids(self,ids):actions.append(("scope",ids))
            def set_engine(self,**kw):actions.append(("engine",kw))
        fn=extract("auto_v6_start_all",{
            "os":os,"current_user":lambda req:{"username":"alice","is_admin":True},
            "require_csrf":lambda *a:None,"require_heavy":lambda *a:None,
            "_short_owned_automation_profiles":lambda req:[selected],
            "db":SimpleNamespace(list_auto_engine_profiles=lambda:(_ for _ in ()).throw(
                 AssertionError("Short admin must not use global profiles"))),
            "v6_store":Store(),"v6_generate_queue":lambda db,st,p:{"created":1},
            "v6_mark_schedule_skipped_today":lambda *a:None,
            "redirect":lambda path,**kw:(path,kw)})
        with patch.dict(os.environ,{"LUM_ENGINE_INSTANCE":"shortstudio"}):
            result=fn(object(),"csrf","START ALL")
        self.assertEqual(actions[0],("scope",[10,99]))
        self.assertEqual(actions[1][0],"engine")
        self.assertIn("owned profile",result[1]["message"])

    def test_member_sync_validates_own_group_without_start_or_queue_generation(self):
        calls=[]
        profile={"id":10,"enabled":1,"category_name":"Mine"}
        class Store:
            def group_settings(self,pid): return {}
            def is_channel_profile(self,pid): return False
            def set_engine(self,**kwargs): raise AssertionError("Sync may not start uploads")
        fn=extract("auto_v6_sync_all",{
            "os":os,"current_user":lambda req:{"username":"alice","is_admin":False},
            "require_csrf":lambda *a:None,"require_heavy":lambda *a:None,
            "_short_owned_automation_profiles":lambda req:[profile],
            "db":SimpleNamespace(list_auto_engine_profiles=lambda:(_ for _ in ()).throw(AssertionError("global query"))),
            "v6_store":Store(),"v6_preflight_profile":
                lambda db,store,p:(calls.append(p["id"]) or {"ok":True,"created":0}),
            "redirect":lambda path,**kw:(path,kw)})
        with patch.dict(os.environ,{"LUM_ENGINE_INSTANCE":"shortstudio"}):
            result=fn(object(),"csrf")
        self.assertEqual(calls,[10])
        self.assertIn("no uploads started",result[1]["message"])

    def test_start_all_explicitly_scopes_owned_ids_and_preserves_existing_scope(self):
        own={"id":10,"enabled":1,"category_name":"Mine"}
        class Store:
            def __init__(self): self.manual="99";self.calls=[];self.running=False
            def engine(self):return {"manual_profile_ids":self.manual,"running":self.running,"pause_after_current":0}
            def group_settings(self,pid):return {}
            def set_manual_profile_ids(self,ids):self.calls.append(("scope",ids));self.manual=",".join(map(str,ids))
            def set_engine(self,**kwargs):self.calls.append(("engine",kwargs))
        store=Store()
        fn=extract("auto_v6_start_all",{
            "os":os,"current_user":lambda req:{"username":"alice","is_admin":False},
            "require_csrf":lambda *a:None,"require_heavy":lambda *a:None,
            "_short_owned_automation_profiles":lambda req:[own],
            "db":SimpleNamespace(list_auto_engine_profiles=lambda:(_ for _ in ()).throw(AssertionError("global profile query"))),
            "v6_store":store,"v6_generate_queue":lambda db,s,pr:{"created":1,"duplicates_blocked":0},
            "v6_mark_schedule_skipped_today":lambda db,p:None,
            "redirect":lambda path,**kw:(path,kw)})
        with patch.dict(os.environ,{"LUM_ENGINE_INSTANCE":"shortstudio"}):
            fn(object(),"csrf","START ALL")
            self.assertEqual(store.calls[0],("scope",[10,99]))
            self.assertEqual(store.calls[1][0],"engine")
            store.calls.clear();store.manual=None;store.running=True
            result=fn(object(),"csrf","START ALL")
            self.assertIn("without an owner scope",result[1]["error"])
            self.assertEqual(store.calls,[])

    def test_preview_and_queue_preflight_exclude_foreign_jobs(self):
        own={"id":10,"enabled":1,"category_name":"Mine"}
        base={
            "os":os,"current_user":lambda req:{"username":"alice","is_admin":False},
            "require_heavy":lambda *a:None,"_short_owned_automation_profiles":lambda req:[own],
            "v6_store":SimpleNamespace(group_settings=lambda pid:{}),
            "V6JSONResponse":lambda obj,status_code=200:obj,
        }
        previews=extract("auto_v6_preview_all",{**base,
            "db":SimpleNamespace(list_auto_engine_profiles=lambda:(_ for _ in ()).throw(AssertionError("global profile query"))),
            "_v6_profile":lambda pid,req:own,
            "v6_planner":lambda *a,**kw:{"ready":True,"jobs":[{"filename":"OWN.mp4"}]}})
        with patch.dict(os.environ,{"LUM_ENGINE_INSTANCE":"shortstudio"}):
            data=previews(object())
        self.assertEqual([x["name"] for x in data["profiles"]],["Mine"])
        self.assertEqual(data["eligible_jobs"],1)
        jobs=[{"id":1,"auto_engine_profile_id":10},{"id":2,"auto_engine_profile_id":20}]
        queue=SimpleNamespace(list_auto_engine_jobs=lambda limit=5000:jobs)
        scan=extract("auto_v6_queue_preflight",{**base,"db":queue,
            "require_csrf":lambda *a:None,
            "v6_queue_preflight":lambda db,store:{"ok":True,"jobs":db.list_auto_engine_jobs()}})
        with patch.dict(os.environ,{"LUM_ENGINE_INSTANCE":"shortstudio"}):
            result=scan(object(),"csrf")
        self.assertEqual([row["id"] for row in result["jobs"]],[1])

    def test_save_schedules_changes_only_verified_owned_profiles(self):
        changes=[]
        own={"id":10,"category_name":"Mine","schedule_enabled":False,
             "schedule_times":"09:00","daily_upload_count":1}
        async def form():return {"csrf_token":"test","schedule_enabled_10":"1",
                                 "schedule_times_10":"12:00","schedule_enabled_20":"1",
                                 "schedule_times_20":"08:00"}
        fn=extract("auto_v6_profile_schedules_save",{
            "os":os,"current_user":lambda req:{"username":"alice","is_admin":False},
            "require_heavy":lambda *a:None,"require_csrf":lambda *a:None,
            "_short_owned_automation_profiles":lambda req:[own],
            "db":SimpleNamespace(list_auto_engine_profiles=lambda:(_ for _ in ()).throw(AssertionError("global query")),
                save_auto_engine_profile=lambda data,pid:changes.append((pid,data))),
            "v6_store":SimpleNamespace(group_settings=lambda pid:{}),
            "v6_parse_schedule_times":lambda s:s.split(","),
            "redirect":lambda path,**kw:(path,kw)})
        with patch.dict(os.environ,{"LUM_ENGINE_INSTANCE":"shortstudio"}):
            asyncio.run(fn(SimpleNamespace(form=form)))
        self.assertEqual([i for i,_ in changes],[10])
        self.assertEqual(changes[0][1]["schedule_times"],"12:00")

    def test_short_only_release_and_user_scope_contract(self):
        runtime=(ROOT/"production/runtime.py").read_text(encoding="utf-8")
        installer=(ROOT/"production/short_uploader_group_release.py").read_text(encoding="utf-8")
        workflow=(ROOT/".github/workflows/vps1-short-studio-release.yml").read_text(encoding="utf-8")
        js=SCRIPT.read_text(encoding="utf-8")
        self.assertIn("[vps1-shortstudio-member-controls]",runtime)
        self.assertIn("member_controls_only=True",runtime)
        self.assertIn("member_controls_only=False",installer)
        self.assertIn("'static/auto_engine_v6.js') if member_controls_only",installer)
        self.assertIn("SHORT_MEMBER_CONTROLS_RELEASE",installer)
        self.assertIn("apps=(local-upload-manager)",workflow)
        self.assertIn("[vps1-shortstudio-member-controls]",workflow)
        self.assertIn("['song','shortstudio'].includes",js)

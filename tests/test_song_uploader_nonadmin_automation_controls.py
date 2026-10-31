"""Song non-admin Automation controls: UI parity and strict tenant action scope."""
from __future__ import annotations

import ast
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "apps/local-upload-manager/part01/templates/app.html"
BACKEND = ROOT / "apps/local-upload-manager/part01/lum_engine/standalone_app.py"
SCRIPT = ROOT / "apps/local-upload-manager/part01/static/auto_engine_v6.js"


def extracted(name: str, env: dict):
    tree = ast.parse(BACKEND.read_text(encoding="utf-8"))
    method = next(node for node in tree.body
                  if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)
    method.decorator_list = []
    ast.fix_missing_locations(method)
    module = ast.Module(
        body=[ast.ImportFrom(module="__future__",
                             names=[ast.alias(name="annotations")], level=0), method],
        type_ignores=[])
    scope = dict(env)
    scope.setdefault("Form", lambda value: value)
    ast.fix_missing_locations(module)
    exec(compile(module, str(BACKEND), "exec"), scope)
    return scope[name]


class SongNonAdminAutomation(TestCase):
    def test_five_controls_and_schedule_dialog_appear_for_both_song_roles(self):
        source = TEMPLATE.read_text(encoding="utf-8")
        start = source.index('<div class="lum-ae-pc-row lum-ae-planner-actions lum-ae-bulk-profile-controls"')
        end = source.index('<div class="lum-table-wrap lum-ae-automation-profiles-wrap">', start)
        start = source.rfind("{% if engine_instance in ['song', 'shortstudio'] %}", 0, start)
        top = Environment().from_string(source[start:end])
        a = source.index('<section class="lum-card lum-ae-automation-planner"')
        z = source.index('<section class="lum-card lum-ae-pc-controls"', a)
        preflight = Environment().from_string(source[a:z])
        d = source.index('<dialog id="lum-ae-schedules-dialog"')
        d2 = source.index('</dialog>', d)+len('</dialog>')
        dialog = Environment().from_string(source[d:d2])
        for instance, admin, expected in (("song", False, True), ("song", True, True),
                                           ("shortstudio", False, False),
                                           ("shortstudio", True, True)):
            with self.subTest(instance=instance, admin=admin):
                ctx = {"engine_instance": instance, "user": {"is_admin": admin},
                       "csrf": "valid", "base_path": "/song-uploader",
                       "ae": {"profiles": []}, "p": None}
                top_html = top.render(**ctx)
                preflight_html = preflight.render(**ctx)
                dialog_html = dialog.render(**ctx)
                for label in ("Profile Schedules", "Preview Next Jobs",
                              "Start All Profiles", "Sync All Channels"):
                    self.assertEqual(label in top_html, expected, label)
                self.assertEqual('data-ae-queue-preflight' in preflight_html, expected)
                self.assertEqual("/actions/auto-v6/start-all" in preflight_html, expected)
                self.assertEqual("/actions/auto-v6/profile-schedules/save" in dialog_html, expected)

    def test_nonadmin_global_controls_remain_blocked_outside_song(self):
        source = BACKEND.read_text(encoding="utf-8")
        self.assertIn('if os.getenv("LUM_ENGINE_INSTANCE") == "song":\n                allowed_exact.update({', source)
        for endpoint in ("sync-all", "start-all", "queue-preflight", "profile-schedules/save"):
            self.assertIn('route("/actions/auto-v6/'+endpoint+'")', source)
        self.assertIn('os.getenv("LUM_ENGINE_INSTANCE") != "song") or', source)
        self.assertIn("TENANT_SAFE_WRITES", source)
        script = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("dataset.engineInstance==='song'?'automation':'queue'", script)

    def test_owner_selection_drops_foreign_or_mixed_group_profiles(self):
        user = {"username": "alice", "is_admin": 0}
        all_profiles = [{"id": 10}, {"id": 20}, {"id": 30}, {"id": 40}]
        class Tenants:
            def allowed_ids(self, kind, viewer):
                self_user = viewer["username"]
                assert kind == "profile" and self_user == "alice"
                return {10, 20, 30}
        class Store:
            def group_settings(self, pid):
                return {"song_folder_owner": "bob" if pid == 20 else ""}
        def require_profile(pid, request):
            if pid == 30:
                raise PermissionError("Profile contains another user's channel")
            return {"id": pid}
        fn = extracted("_song_owned_automation_profiles", {
            "os": os, "current_user": lambda request: user,
            "studio_tenants": Tenants(),
            "db": SimpleNamespace(list_auto_engine_profiles=lambda: all_profiles),
            "v6_store": Store(), "_v6_profile": require_profile,
        })
        with patch.dict(os.environ, {"LUM_ENGINE_INSTANCE": "song"}):
            self.assertEqual([p["id"] for p in fn(object())], [10])

    def test_nonadmin_sync_all_is_owned_validation_only(self):
        owned = {"id": 10, "enabled": 1, "category_name": "ALICE"}
        calls = []
        class Store:
            def group_settings(self, pid):
                return {}
            def is_channel_profile(self, pid):
                return True
            def set_engine(self, **kwargs):
                raise AssertionError("Sync All cannot start engine")
        fn = extracted("auto_v6_sync_all", {
            "os": os, "current_user": lambda request: {"username": "alice", "is_admin": 0},
            "require_csrf": lambda *args: None, "require_heavy": lambda *args: None,
            "_song_owned_automation_profiles": lambda request: [owned],
            "db": SimpleNamespace(list_auto_engine_profiles=lambda: (_ for _ in ()).throw(AssertionError("Global list used"))),
            "v6_store": Store(),
            "v6_sync_channel_profile": lambda db, store, pr, queue_valid: (calls.append((pr["id"], queue_valid)) or {"created": 0}),
            "redirect": lambda path, **kwargs: (path, kwargs),
        })
        with patch.dict(os.environ, {"LUM_ENGINE_INSTANCE": "song"}):
            response = fn(object(), "valid")
        self.assertEqual(calls, [(10, False)])
        self.assertIn("no uploads started", response[1]["message"])

    def test_start_all_adds_only_owned_to_manual_scope(self):
        own = {"id": 10, "enabled": 1, "category_name": "ALICE"}
        class Store:
            def __init__(self):
                self.scope = []
                self.ran = []
                self.running = 0
                self.manual = "90"
            def group_settings(self, pid):
                return {}
            def engine(self):
                return {"running": self.running, "manual_profile_ids": self.manual,
                        "pause_after_current": 0}
            def set_manual_profile_ids(self, ids):
                self.scope.append(ids)
                self.manual = ",".join(map(str, ids))
            def set_engine(self, **kwargs):
                self.ran.append(kwargs)
        store = Store()
        generated = []
        def generate(db, v6, profile):
            generated.append((profile["id"], list(v6.scope)))
            return {"created": 1, "duplicates_blocked": 0}
        fn = extracted("auto_v6_start_all", {
            "os": os, "current_user": lambda request: {"username": "alice", "is_admin": 0},
            "require_csrf": lambda *args: None, "require_heavy": lambda *args: None,
            "_song_owned_automation_profiles": lambda request: [own],
            "db": SimpleNamespace(list_auto_engine_profiles=lambda: (_ for _ in ()).throw(AssertionError("Global list used"))),
            "v6_store": store, "v6_generate_queue": generate,
            "v6_mark_schedule_skipped_today": lambda *args: None,
            "redirect": lambda path, **kwargs: (path, kwargs),
        })
        with patch.dict(os.environ, {"LUM_ENGINE_INSTANCE": "song"}):
            response = fn(object(), "valid", "START ALL")
            self.assertEqual(store.scope, [[10, 90]])
            self.assertEqual(generated, [(10, [[10, 90]])])
            self.assertTrue(store.ran and store.ran[0]["running"])
            self.assertIn("owned profile", response[1]["message"])
            store.running = 1
            store.manual = None
            store.scope.clear()
            store.ran.clear()
            blocked = fn(object(), "valid", "START ALL")
            self.assertIn("without an owner scope", blocked[1]["error"])
            self.assertFalse(store.ran)
            self.assertFalse(store.scope)

    def test_preflight_uses_filtered_queue_not_global_aggregate(self):
        owned = [{"id": 10}]
        all_jobs = [{"id": 1, "auto_engine_profile_id": 10},
                    {"id": 2, "auto_engine_profile_id": 20}]
        class QueueDB:
            def list_auto_engine_jobs(self, limit=5000):
                return all_jobs
        def preflight(queue_db, store):
            jobs = queue_db.list_auto_engine_jobs()
            return {"ok": True, "checked": len(jobs), "issues":
                    [{"video_id": job["id"], "message": "issue"} for job in jobs]}
        fn = extracted("auto_v6_queue_preflight", {
            "os": os, "current_user": lambda req: {"username": "alice", "is_admin": 0},
            "require_csrf": lambda *args: None, "require_heavy": lambda *args: None,
            "_song_owned_automation_profiles": lambda req: owned,
            "db": QueueDB(), "v6_store": object(), "v6_queue_preflight": preflight,
            "V6JSONResponse": lambda value, status_code=200: value,
        })
        with patch.dict(os.environ, {"LUM_ENGINE_INSTANCE": "song"}):
            result = fn(object(), "csrf")
        self.assertEqual(result["checked"], 1)
        self.assertEqual([x["video_id"] for x in result["issues"]], [1])

    def test_preview_all_excludes_unowned_profile_metadata(self):
        own={"id": 10, "enabled": 1, "category_name": "ALICE"}
        fn = extracted("auto_v6_preview_all", {
            "os": os, "current_user": lambda req: {"username": "alice", "is_admin": 0},
            "require_heavy": lambda *args: None,
            "_song_owned_automation_profiles": lambda req: [own],
            "db": SimpleNamespace(list_auto_engine_profiles=lambda: (_ for _ in ()).throw(AssertionError("Global profiles read"))),
            "v6_store": SimpleNamespace(group_settings=lambda pid: {}),
            "_v6_profile": lambda pid, req: own,
            "v6_planner": lambda *args, **kwargs: {"jobs": [{"filename":"ALICE.mp4"}], "ready": True},
            "V6JSONResponse": lambda value, status_code=200: value,
        })
        with patch.dict(os.environ, {"LUM_ENGINE_INSTANCE": "song"}):
            response=fn(object())
        self.assertEqual(response["profiles"][0]["name"], "ALICE")
        self.assertEqual(len(response["profiles"]), 1)
        self.assertEqual(response["eligible_jobs"], 1)

    def test_bulk_schedule_save_touches_only_own_profile(self):
        saved=[]
        own={"id": 10, "category_name": "ALICE", "schedule_times": "09:00",
             "schedule_enabled": False, "daily_upload_count": 1}
        class Store:
            def group_settings(self, pid):
                return {}
        async def request_form():
            return {"csrf_token": "valid", "schedule_times_10": "12:00",
                    "schedule_enabled_10": "1", "schedule_times_20": "01:00",
                    "schedule_enabled_20": "1"}
        fn=extracted("auto_v6_profile_schedules_save",{
            "os": os, "current_user": lambda req: {"username":"alice","is_admin":0},
            "require_heavy": lambda *args: None,
            "require_csrf": lambda *args: None,
            "_song_owned_automation_profiles": lambda req: [own],
            "db": SimpleNamespace(
                list_auto_engine_profiles=lambda: (_ for _ in ()).throw(AssertionError("Global list used")),
                save_auto_engine_profile=lambda data,pid: saved.append((pid,data))),
            "v6_store": Store(), "v6_parse_schedule_times": lambda s: s.split(","),
            "redirect": lambda path, **kwargs: (path, kwargs),
        })
        with patch.dict(os.environ, {"LUM_ENGINE_INSTANCE": "song"}):
            response=asyncio.run(fn(SimpleNamespace(form=request_form)))
        self.assertEqual([pid for pid,_ in saved], [10])
        self.assertEqual(saved[0][1]["schedule_times"], "12:00")
        self.assertIn("1 profile", response[1]["message"])

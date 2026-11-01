"""Regression: Song Uploader member handover must not change a foreign tenant."""
from __future__ import annotations
import ast
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "apps/local-upload-manager/part01/lum_engine/standalone_app.py"
TEMPLATE = ROOT / "apps/local-upload-manager/part01/templates/app.html"


def extracted(name, env):
    source = ast.parse(BACKEND.read_text(encoding="utf-8"))
    fn = next(n for n in source.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    fn.decorator_list = []
    scope = {"Form": lambda x: x, **env}
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), fn], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(BACKEND), "exec"), scope)
    return scope[name]


class SongMemberHandover(TestCase):
    def test_member_ui_exposes_only_scoped_queue_and_sheets_tools(self):
        ui = TEMPLATE.read_text(encoding="utf-8")
        sheet = ui[ui.index('<div class="lum-short-sheet-header-actions">'):ui.index("</div>", ui.index('<div class="lum-short-sheet-header-actions">'))]
        self.assertIn("engine_instance in ['song','shortstudio'] or user.is_admin", sheet)
        queue = ui[ui.index('<section class="lum-card lum-ae-pc-controls"'):ui.index('<div class="lum-ae-queue-summary">')]
        for endpoint in ("retry-failed", "resume-paused", "clear-done", "remove-selected"):
            action = 'action="{{ base_path }}/actions/auto-v6/' + endpoint + '"'
            at = queue.index(action)
            self.assertIn("engine_instance in ['song','shortstudio'] or user.is_admin", queue[at-220:at])
        for endpoint in ("/actions/auto-v6/engine", "/actions/auto-v6/concurrency"):
            self.assertIn(endpoint, queue)
        backend = BACKEND.read_text(encoding="utf-8")
        start = backend.index('            if os.getenv("LUM_ENGINE_INSTANCE") == "song":\n                allowed_exact.update({')
        end = backend.index('            if os.getenv("LUM_ENGINE_INSTANCE") == "shortstudio":', start)
        song_allowlist = backend[start:end]
        for endpoint in ("retry-failed", "resume-paused", "clear-done", "remove-selected", "recovery-clear", "job/upload-now"):
            self.assertIn('route("/actions/auto-v6/' + endpoint + '")', song_allowlist)
        self.assertIn('route("/actions/sheets/sync-all")', song_allowlist)
        upload_action = ui.index('action="{{ base_path }}/actions/auto-v6/job/upload-now"')
        self.assertIn("{% if engine_instance in ['song','shortstudio'] or user.is_admin", ui[upload_action-240:upload_action])
        self.assertIn("{% if engine_instance in ['song','shortstudio'] or user.is_admin", ui[ui.index("lum-ae-recovery"):ui.index("lum-ae-recovery")+190])

    def test_sync_all_only_fetches_owned_enabled_sources(self):
        calls = []
        owned = {"username": "alice", "is_admin": 0}
        sources = [
            {"id": 1, "sheet_name": "Mine", "sheet_url_or_id": "own", "enabled": 1},
            {"id": 2, "sheet_name": "Foreign", "sheet_url_or_id": "foreign", "enabled": 1},
            {"id": 3, "sheet_name": "Disabled", "sheet_url_or_id": "disabled", "enabled": 0},
        ]
        class Tenant:
            def allowed_ids(self, kind, user):
                return {1, 3}
            def require(self, kind, entity_id, user):
                calls.append(("check", entity_id))
                assert entity_id == 1
            def sync_owned_sheet_rows(self, username, sid, rows):
                calls.append(("sync", sid, username))
                return {"ok": True, "rows": len(rows)}
        fn = extracted("sheets_sync_all", {
            "os": os, "current_user": lambda req: owned,
            "studio_tenants": Tenant(), "db": SimpleNamespace(list_sheet_sources=lambda: sources),
            "require_csrf": lambda *args: None, "require_heavy": lambda *args: None,
            "tenant_fetch_sheet_csv": lambda link, gid: (calls.append(("fetch", link)) or [{"row": 1}]),
            "tenant_rows_to_sheet_rows": lambda rows, path: rows, "tenant_token_dir": "/tmp",
            "web_sync_all_sheet_sources": lambda *args: (_ for _ in ()).throw(AssertionError("Global sheet sync")),
            "redirect": lambda url, **kwargs: (url, kwargs),
        })
        with patch.dict(os.environ, {"LUM_ENGINE_INSTANCE": "song"}):
            result = fn(object(), "csrf")
        self.assertEqual(calls, [("check", 1), ("fetch", "own"), ("sync", 1, "alice")])
        self.assertIn("No uploads started", result[1]["message"])

    def test_queue_filters_foreign_and_mixed_channel_jobs_and_denies_forgery(self):
        member = {"username": "alice", "is_admin": 0}
        jobs = [
            {"id": 1, "auto_engine_profile_id": 10, "channel_db_id": 100, "status": "FAILED"},
            {"id": 2, "auto_engine_profile_id": 20, "channel_db_id": 200, "status": "FAILED"},
            {"id": 3, "auto_engine_profile_id": 10, "channel_db_id": 200, "status": "FAILED"},
        ]
        class Tenant:
            def require(self, kind, cid, user):
                if cid != 100: raise PermissionError("foreign channel")
        fn = extracted("_song_member_queue_jobs", {
            "os": os, "current_user": lambda req: member,
            "_song_owned_automation_profiles": lambda req: [{"id": 10}],
            "_v6_profile": lambda pid, req: {"id": pid},
            "db": SimpleNamespace(list_auto_engine_jobs=lambda limit=5000: jobs),
            "studio_tenants": Tenant(),
        })
        with patch.dict(os.environ, {"LUM_ENGINE_INSTANCE": "song"}):
            self.assertEqual([row["id"] for row in fn(object())], [1])
            for forged in ([2], [3], [1, 2], [999], [0], [-1], ["bad"], [1, 1]):
                with self.subTest(forged=forged), self.assertRaises(PermissionError):
                    fn(object(), forged)

    def test_engine_scope_preserves_foreign_selection_and_refuses_unsafe_state(self):
        state = {"running": 0, "pause_after_current": 0, "manual_profile_ids": "90"}
        scope = []
        starts = []
        store = SimpleNamespace(
            engine=lambda: dict(state),
            set_manual_profile_ids=lambda ids: scope.append(ids),
            set_engine=lambda **kwargs: starts.append(kwargs),
        )
        fn = extracted("_song_member_start_scoped", {
            "v6_store": store,
            "_song_owned_automation_profiles": lambda req: [{"id": 10}],
            "_song_member_engine_guard": lambda: None,
        })
        fn(object(), {10})
        self.assertEqual(scope, [[10, 90]])
        self.assertEqual(len(starts), 1)
        with self.assertRaises(PermissionError):
            fn(object(), {20})
        guard = extracted("_song_member_engine_guard", {"v6_store": store})
        state.update(running=1, manual_profile_ids=None)
        with self.assertRaises(PermissionError):
            guard()
        state.update(running=0, pause_after_current=1)
        with self.assertRaises(PermissionError):
            guard()

    def test_clear_done_only_removes_member_completed_ids(self):
        deleted = []
        fn = extracted("auto_v6_clear_done", {
            "os": os,
            "current_user": lambda req: {"username": "alice", "is_admin": 0},
            "require_csrf": lambda *args: None, "require_heavy": lambda *args: None,
            "_song_member_queue_jobs": lambda req, statuses: [
                {"id": 7, "status": "UPLOADED"}],
            "db": SimpleNamespace(
                delete_auto_engine_jobs=lambda ids: (deleted.extend(ids) or len(ids)),
                clear_completed_auto_engine_jobs=lambda: (_ for _ in ()).throw(AssertionError("Global deletion"))
            ),
            "redirect": lambda url, **kwargs: (url, kwargs),
        })
        with patch.dict(os.environ, {"LUM_ENGINE_INSTANCE": "song"}):
            response = fn(object(), "csrf")
        self.assertEqual(deleted, [7])
        self.assertIn("1 owned", response[1]["message"])

    def test_invalid_retry_selection_never_retries_all_owned(self):
        import asyncio
        member = {"username": "alice", "is_admin": 0}
        async def invoke(values):
            class FormData(dict):
                def getlist(self, key):
                    return values if key == "video_ids" else []
            class Request:
                async def form(self):
                    return FormData(csrf_token="token")
            fn = extracted("auto_v6_retry_failed", {
                "os": os, "current_user": lambda req: member,
                "require_heavy": lambda user: None,
                "require_csrf": lambda *args: None,
                "_v6_selected_ids": lambda form: sorted({
                    int(v) for v in form.getlist("video_ids")
                    if str(v).strip().isdigit() and int(v) > 0
                }),
                "_song_member_queue_jobs": lambda *args: (_ for _ in ()).throw(
                    AssertionError("Malformed selection must not reach bulk retry")),
                "redirect": lambda path, **kw: (path, kw),
            })
            return await fn(Request())
        with patch.dict(os.environ, {"LUM_ENGINE_INSTANCE": "song"}):
            for values in (["0"], ["bad"], ["1", "1"], ["1", "invalid"]):
                with self.subTest(values=values):
                    result = asyncio.run(invoke(values))
                    self.assertIn("Invalid or duplicate", result[1]["error"])

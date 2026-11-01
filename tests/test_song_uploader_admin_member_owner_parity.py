"""Song operational parity: same role capabilities, strictly separate owned records."""
from __future__ import annotations

import ast
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "apps/local-upload-manager/part01/lum_engine/standalone_app.py"
UI = ROOT / "apps/local-upload-manager/part01/templates/app.html"


def extract(name, scope):
    source = ast.parse(BACKEND.read_text(encoding="utf-8"))
    method = next(n for n in source.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name == name)
    method.decorator_list = []
    module = ast.Module(body=[method], type_ignores=[])
    ast.fix_missing_locations(module)
    env = dict(scope)
    env.setdefault("Form", lambda value: value)
    exec(compile(module, str(BACKEND), "exec"), env)
    return env[name]


class SongAccountParity(unittest.TestCase):
    def test_strict_user_for_song_admin_but_other_apps_unchanged(self):
        scope = extract("_studio_strict_user", {"os": os})
        with patch.dict(os.environ, {"LUM_ENGINE_INSTANCE": "song"}):
            for role in (0, 1):
                self.assertEqual(scope({"username": "alice", "is_admin": role})["is_admin"], 0)
        with patch.dict(os.environ, {"LUM_ENGINE_INSTANCE": "shortstudio"}):
            self.assertEqual(scope({"username": "alice", "is_admin": 1})["is_admin"], 1)

    def test_owned_acl_never_bypassed_by_admin_flag(self):
        actors = []
        class Tenants:
            def require(self, kind, rid, viewer):
                actors.append((kind, rid, viewer["username"], viewer["is_admin"]))
                if rid != 10:
                    raise PermissionError("Foreign or legacy object")
            def allowed_ids(self, kind, viewer):
                actors.append((kind, None, viewer["username"], viewer["is_admin"]))
                return {10}
        ctx = {"studio_tenants": Tenants(),
               "_studio_strict_user": extract("_studio_strict_user", {"os": os})}
        required = extract("_studio_require", ctx)
        allowed = extract("_studio_allowed_ids", ctx)
        with patch.dict(os.environ, {"LUM_ENGINE_INSTANCE": "song"}):
            for role in (0, 1):
                user = {"username": "alice", "is_admin": role}
                self.assertEqual(allowed("channel", user), {10})
                required("channel", 10, user)
                with self.assertRaises(PermissionError):
                    required("channel", 20, user)
        self.assertTrue(actors)
        self.assertTrue(all(item[3] == 0 for item in actors))

    def test_admin_queue_cannot_take_foreign_profile_or_channel(self):
        jobs = [{"id": 1, "auto_engine_profile_id": 10, "channel_db_id": 100, "status": "FAILED"},
                {"id": 2, "auto_engine_profile_id": 20, "channel_db_id": 200, "status": "FAILED"},
                {"id": 3, "auto_engine_profile_id": 10, "channel_db_id": 200, "status": "FAILED"}]
        actor = {"username": "alice", "is_admin": 1}
        fn = extract("_song_member_queue_jobs", {
            "os": os, "current_user": lambda req: actor,
            "_song_owned_automation_profiles": lambda req: [{"id": 10}],
            "_v6_profile": lambda pid, req: {"id": pid},
            "_studio_require": lambda kind, rid, user: (
                None if rid == 100 else (_ for _ in ()).throw(PermissionError("Foreign"))),
            "db": SimpleNamespace(list_auto_engine_jobs=lambda limit=5000: jobs),
        })
        with patch.dict(os.environ, {"LUM_ENGINE_INSTANCE": "song"}):
            for role in (0, 1):
                actor["is_admin"] = role
                self.assertEqual([x["id"] for x in fn(object())], [1])
                for selected in ([2], [3], [1, 2]):
                    with self.assertRaises(PermissionError):
                        fn(object(), selected)

    def test_song_oauth_panel_identical_for_admin_and_member(self):
        source = UI.read_text(encoding="utf-8")
        start = source.index("{% macro auth_panel(")
        end = source.index("{% endmacro %}", start) + len("{% endmacro %}")
        rendered = Environment(autoescape=True).from_string(
            source[start:end] + "\n{{ auth_panel(False) }}"
        )
        ctx = dict(engine_instance="song", base_path="/song-uploader",
                   channel_summary={}, channel_rows=[], channel_groups=[],
                   channel_sheets=[], clients=[], oauth_url="", oauth_session_id="",
                   csrf="csrf")
        html = []
        for role in (False, True):
            output = rendered.render(user={"is_admin": role}, **ctx)
            self.assertNotIn("Shared OAuth client is managed by an administrator", output)
            self.assertNotIn("Set / Replace Shared Client", output)
            self.assertIn("Verify &amp; Save Selected Channel Token", output)
            html.append(output)
        self.assertEqual(html[0], html[1])

    def test_song_shared_engine_global_forms_absent_for_both_roles(self):
        text = UI.read_text(encoding="utf-8")
        self.assertIn("engine_instance != 'song' and (user.is_admin", text)
        src = BACKEND.read_text(encoding="utf-8")
        self.assertIn('Shared engine controls are not account scoped', src)
        self.assertIn('Global records export is not account scoped', src)


if __name__ == "__main__":
    unittest.main()

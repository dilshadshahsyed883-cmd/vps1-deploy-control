"""Offline behavior regression for Scene Builder's two-role user model.

Runs without FastAPI or a production database. Only selected real route
functions are compiled out of app.py with controlled dependencies.
"""
from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
from urllib.parse import quote
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "apps/scene-builder/part01/app.py").read_text()
TEMPLATE = (ROOT / "apps/scene-builder/part01/templates/user_management.html").read_text()
BASE = (ROOT / "apps/scene-builder/part01/templates/base.html").read_text()
TREE = ast.parse(SOURCE)


class HTTPException(Exception):
    def __init__(self, status_code, detail=""):
        super().__init__(detail)
        self.status_code = status_code


class Response:
    def __init__(self, status_code=200, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


class RedirectResponse(Response):
    def __init__(self, url, status_code=303):
        super().__init__(status_code)
        self.url = url


def get_function(name, bindings):
    found = [node for node in TREE.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name]
    if len(found) != 1:
        raise AssertionError("expected exactly one " + name)
    node = found[0]
    node.decorator_list = []
    source = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node], type_ignores=[])
    ast.fix_missing_locations(source)
    scope = dict(bindings)
    exec(compile(source, "scene-app-isolated.py", "exec"), scope)
    return scope[name]


class RoleBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.datafile = str(Path(self.tmp.name) / "users.db")
        with self.db() as conn:
            conn.executescript("""
                CREATE TABLE users (
                    username TEXT PRIMARY KEY COLLATE NOCASE, secret_hash TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1, is_admin INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    daily_render_limit INTEGER NOT NULL DEFAULT 25,
                    subscription_plan_days INTEGER NOT NULL DEFAULT 30,
                    subscription_start_at TEXT NOT NULL DEFAULT '',
                    subscription_end_at TEXT NOT NULL DEFAULT '',
                    last_login_at TEXT NOT NULL DEFAULT '',
                    app_access TEXT NOT NULL DEFAULT 'scene_builder');
                CREATE TABLE job_ownership(job_id TEXT PRIMARY KEY, owner_username TEXT);
                INSERT INTO users(username,secret_hash,is_active,is_admin,created_at,subscription_end_at,app_access)
                    VALUES('owner','hash:owner',1,1,'today','2020-01-01','scene_builder');
                INSERT INTO users(username,secret_hash,is_active,is_admin,created_at,subscription_end_at,app_access)
                    VALUES('editor','hash:editor',1,0,'today','2020-01-01','scene_builder');
            """)
        self.events = []
        self.bindings = {
            "db": self.db,
            "Form": lambda default: default,
            "HTTPException": HTTPException,
            "RedirectResponse": RedirectResponse,
            "sqlite3": sqlite3,
            "re": __import__("re"),
            "hash_secret": lambda value: "hash:" + value,
            "now_iso": lambda: "2026-10-19",
            "requests": SimpleNamespace(utils=SimpleNamespace(quote=quote)),
            "audit_event": lambda *args: self.events.append(args),
            "hmac": __import__("hmac"),
            "secrets": __import__("secrets"),
        }
        self.account = get_function("user_account", {**self.bindings,
            "parse_iso": lambda value: datetime.fromisoformat(value).replace(tzinfo=timezone.utc) if value else None,
            "datetime": datetime, "timezone": timezone})
        self.require_admin = get_function("require_admin", self.bindings)
        self.edit = get_function("user_management_action", {**self.bindings, "require_admin": self.require_admin})
        self.allow_app = get_function("app_access_allows", self.bindings)
        self.sso = get_function("workspace_session_auth", {
            **self.bindings,
            "SCENE_SSO_APP_IDS": frozenset(("short-maker", "short-stream", "self-media-library",
                "video-merger", "voice-studio", "auto-caption-studio", "local-upload-manager")),
            "logged_in": lambda req: bool(req.session.get("authenticated")),
            "user_account": self.account,
            "Response": Response,
        })

    def db(self):
        c = sqlite3.connect(self.datafile)
        c.row_factory = sqlite3.Row
        return c

    def request(self, username="owner", admin=True, token="csrf-ok"):
        return SimpleNamespace(session={
            "username": username, "is_admin": admin,
            "authenticated": True, "user_management_csrf": token
        })

    def post(self, action, username, **kwargs):
        return self.edit(self.request(), action=action, username=username, csrf_token="csrf-ok", **kwargs)

    def test_legacy_expired_nonadmin_can_access_apps(self):
        account = self.account("editor")
        self.assertFalse(account["subscription_expired"])
        self.assertEqual(account["subscription_status"], "Active")
        for name in ("scene_builder", "auto_caption", "lum"):
            self.assertTrue(self.allow_app(account, name), name)
        account["is_active"] = 0
        self.assertFalse(self.allow_app(account, "lum"))
        self.assertFalse(self.allow_app(account, "unknown"))

    def test_all_independent_apps_allow_nonadmin_without_role_escalation(self):
        req = self.request("editor", admin=False)
        for name in ("short-maker", "short-stream", "self-media-library", "video-merger",
                     "voice-studio", "auto-caption-studio", "local-upload-manager"):
            res = self.sso(req, app=name)
            self.assertEqual(res.status_code, 204, name)
            self.assertEqual(res.headers["X-Is-Admin"], "0", name)
            self.assertNotIn(res.headers["X-App-Access"], {"all", "both"}, name)
        self.assertEqual(self.sso(req, app="unknown").status_code, 403)
        self.assertEqual(self.sso(SimpleNamespace(session={}), app="voice-studio").status_code, 401)
        req.session["username"] = "not-found"
        self.assertEqual(self.sso(req, app="voice-studio").status_code, 401)

    def test_admin_has_access_and_only_admin_may_manage_users(self):
        res = self.sso(self.request(), app="video-merger")
        self.assertEqual(res.status_code, 204)
        self.assertEqual(res.headers["X-Is-Admin"], "1")
        self.assertEqual(res.headers["X-App-Access"], "both")
        with self.assertRaises(HTTPException) as error:
            self.edit(self.request("editor", admin=False), action="create", username="newuser",
                      secret_key="abcdefghi", csrf_token="csrf-ok")
        self.assertEqual(error.exception.status_code, 403)

    def test_admin_create_role_change_and_password_reset(self):
        created = self.post("create", "editor02", secret_key="password123", role="non_admin")
        self.assertEqual(created.status_code, 303)
        user = self.account("editor02")
        self.assertEqual(user["is_admin"], 0)
        self.assertEqual(user["daily_render_limit"], 0)
        self.assertEqual(user["app_access"], "all")
        self.assertEqual(user["subscription_end_at"], "")
        self.post("set_role", "editor02", role="admin")
        self.assertEqual(self.account("editor02")["is_admin"], 1)
        self.post("set_role", "editor02", role="non_admin")
        self.assertEqual(self.account("editor02")["is_admin"], 0)
        self.post("reset_password", "editor02", secret_key="new-password-456")
        with self.db() as conn:
            self.assertEqual(conn.execute("SELECT secret_hash FROM users WHERE username='editor02'").fetchone()[0],
                             "hash:new-password-456")
        self.assertTrue(self.events)

    def test_last_active_admin_protected_but_other_admin_can_be_demoted(self):
        with self.assertRaises(HTTPException) as error:
            self.post("set_role", "owner", role="non_admin")
        self.assertEqual(error.exception.status_code, 409)
        self.post("create", "second-admin", secret_key="password123", role="admin")
        self.post("set_role", "second-admin", role="non_admin")
        self.assertEqual(self.account("owner")["is_admin"], 1)
        self.post("set_role", "second-admin", role="admin")
        self.post("set_role", "owner", role="non_admin")
        self.assertEqual(self.account("owner")["is_admin"], 0)

    def test_csrf_disable_enable_and_existing_data_preserved(self):
        with self.assertRaises(HTTPException) as error:
            self.edit(self.request(), action="disable", username="editor", csrf_token="incorrect")
        self.assertEqual(error.exception.status_code, 403)
        with self.assertRaises(HTTPException):
            self.post("disable", "owner")
        self.post("disable", "editor")
        self.assertEqual(self.account("editor")["subscription_status"], "Disabled")
        self.assertEqual(self.sso(self.request("editor", False), app="short-maker").status_code, 401)
        self.post("enable", "editor")
        self.assertEqual(self.account("editor")["subscription_status"], "Active")
        self.assertEqual(self.account("editor")["subscription_end_at"], "2020-01-01")

    def test_delete_requires_no_jobs_and_nonadmin_role(self):
        with self.assertRaises(HTTPException):
            self.post("delete", "owner")
        with self.db() as conn:
            conn.execute("INSERT INTO job_ownership(job_id, owner_username) VALUES('job1','editor')")
        with self.assertRaises(HTTPException) as error:
            self.post("delete", "editor")
        self.assertEqual(error.exception.status_code, 409)
        with self.db() as conn:
            conn.execute("DELETE FROM job_ownership WHERE job_id='job1'")
        self.post("delete", "editor")
        self.assertIsNone(self.account("editor"))

    def test_candidate_policy_grants_only_exact_admin_post(self):
        policy = ROOT / "apps/scene-builder/part01/precutover_policy.py"
        source = policy.read_text()
        self.assertIn('_ADMIN_USER_MANAGEMENT = ("POST", "/user-management")', source)
        self.assertIn('if (method, path) == _ADMIN_USER_MANAGEMENT:', source)
        app_source = SOURCE
        self.assertIn("candidate_mutation_allowed(", app_source)
        self.assertIn("auto_script_enabled=os.getenv(\"FRESH_AUTO_SCRIPT_READY\", \"0\") == \"1\"", app_source)
        self.assertIn('require_admin(request)\n    expected', app_source)
        self.assertIn("Invalid user-management action token", app_source)

    def test_live_scene_identity_uses_db_role_not_stale_session(self):
        class JsonResponse:
            def __init__(self, data, headers=None):
                self.data = data
                self.headers = headers or {}
        func = get_function("scene_session_identity", {
            **self.bindings,
            "require_login": lambda req: None if req.session.get("authenticated") else
                (_ for _ in ()).throw(HTTPException(401, "Login required")),
            "user_account": self.account,
            "JSONResponse": JsonResponse,
        })
        req = self.request("editor", admin=True)
        role = func(req)
        self.assertTrue(role.data["authenticated"])
        self.assertFalse(role.data["is_admin"])  # A stale session cannot escalate.
        self.assertEqual(role.data["username"], "editor")
        self.assertEqual(role.headers["Cache-Control"], "private, no-store")
        self.assertEqual(role.headers["Vary"], "Cookie")
        req.session["username"] = "owner"
        self.assertTrue(func(req).data["is_admin"])
        req.session["username"] = "missing-user"
        with self.assertRaises(HTTPException) as missing:
            func(req)
        self.assertEqual(missing.exception.status_code, 401)
        req.session["authenticated"] = False
        with self.assertRaises(HTTPException) as anonymous:
            func(req)
        self.assertEqual(anonymous.exception.status_code, 401)

    def test_ui_only_two_roles_and_admin_sidebar_guard(self):
        self.assertIn('name="role"', TEMPLATE)
        self.assertIn('value="non_admin"', TEMPLATE)
        self.assertIn('value="admin"', TEMPLATE)
        self.assertIn('name="csrf_token"', TEMPLATE)
        self.assertNotIn('name="plan_days"', TEMPLATE)
        self.assertNotIn('name="app_access"', TEMPLATE)
        self.assertNotIn('name="daily_render_limit"', TEMPLATE)
        self.assertIn('{% if request.session.get("is_admin") %}', BASE)
        self.assertIn('href="/admin">Admin</a>', BASE)
        self.assertNotIn('{% if request.session.get("is_admin") %}<a href="/video-studio/', BASE)


if __name__ == "__main__":
    unittest.main()

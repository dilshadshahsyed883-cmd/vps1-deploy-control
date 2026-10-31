"""Non-destructive, dependency-free regressions for per-user Studio work isolation."""
from __future__ import annotations

import ast
import json
from pathlib import Path
import sqlite3
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
SHORT = ROOT / "apps/short-maker/part01/app.py"
MERGER = ROOT / "apps/video-merger/part01/app.py"


def isolated_functions(path: Path, names: set[str], context: dict) -> dict:
    original = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    functions = [node for node in original.body if isinstance(node, ast.FunctionDef)
                 and node.name in names]
    if {node.name for node in functions} != names:
        raise AssertionError("Reviewed owner functions are missing")
    # Do not import the live application or initialize workers/databases.
    body = []
    for node in functions:
        node.decorator_list = []
        body.append(node)
    program = ast.fix_missing_locations(ast.Module(body=body, type_ignores=[]))
    namespace = dict(context)
    exec(compile(program, str(path), "exec"), namespace)
    return namespace


class Request:
    def __init__(self, owner: str, admin: bool = False):
        self.headers = {"x-auth-user": owner, "x-is-admin": "1" if admin else "0"}


class HttpFailure(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        super().__init__(detail)


class UserIsolationContracts(unittest.TestCase):
    def test_short_job_list_and_direct_resource_access_are_owner_scoped(self):
        records = {
            "a": {"job_id": "a", "owner": "Alice", "status": "completed"},
            "b": {"job_id": "b", "owner": "Bob", "status": "completed"},
            "legacy": {"job_id": "legacy", "status": "completed"},
        }
        ns = isolated_functions(SHORT, {
            "short_identity", "short_state_owner", "short_can_access", "short_owned_state",
        }, {"Request": Request, "HTTPException": HttpFailure,
            "get_state": lambda jid: dict(records[jid])})
        alice, bob, admin = Request("Alice"), Request("Bob"), Request("root", True)
        self.assertTrue(ns["short_can_access"](records["a"], alice))
        self.assertFalse(ns["short_can_access"](records["b"], alice))
        self.assertFalse(ns["short_can_access"](records["legacy"], alice))
        self.assertEqual(ns["short_owned_state"]("a", alice)["owner"], "Alice")
        with self.assertRaises(HttpFailure) as denied:
            ns["short_owned_state"]("b", alice)
        self.assertEqual(denied.exception.status_code, 404)
        with self.assertRaises(HttpFailure):
            ns["short_owned_state"]("legacy", bob)
        self.assertTrue(all(ns["short_can_access"](x, admin) for x in records.values()))
        with self.assertRaises(HttpFailure) as unauth:
            ns["short_can_access"](records["a"], Request(""))
        self.assertEqual(unauth.exception.status_code, 401)

    def test_merger_jobs_and_library_sql_hide_legacy_and_other_users(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript("""
            CREATE TABLE jobs(id TEXT, owner_username TEXT, videos_json TEXT,
                              order_idx INTEGER, created_at TEXT);
            CREATE TABLE library_items(id TEXT, owner_username TEXT, name TEXT,
                                       created_at TEXT, trim_json TEXT);
        """)
        for name, owner in (("a", "Alice"), ("b", "Bob"), ("legacy", "")):
            connection.execute("INSERT INTO jobs VALUES(?,?,?,1,'2026-10-31')",
                               (name, owner, "[]"))
            connection.execute("INSERT INTO library_items VALUES(?,?,?,'2026-10-31','{}')",
                               (name, owner, name))
        ns = isolated_functions(MERGER, {"list_jobs", "list_library_items"},
                                {"DB_LOCK": threading.RLock(), "db": lambda: connection,
                                 "json": json, "library_in_use": lambda item: False})
        self.assertEqual([v["id"] for v in ns["list_jobs"]("Alice")], ["a"])
        self.assertEqual([v["id"] for v in ns["list_jobs"]("Bob")], ["b"])
        self.assertEqual(len(ns["list_jobs"]("root", admin=True)), 3)
        self.assertEqual([v["id"] for v in ns["list_library_items"]("Alice")], ["a"])
        self.assertEqual([v["id"] for v in ns["list_library_items"]("Bob")], ["b"])
        self.assertEqual(len(ns["list_library_items"](None)), 3)
        self.assertEqual(ns["list_jobs"](""), [])
        connection.close()

    def test_all_mutation_and_download_paths_have_owner_guards(self):
        short = SHORT.read_text()
        merger = MERGER.read_text()
        for guard in (
            "short_owned_state(job_id, request)",
            "short_can_access(state, request)",
            '"owner": owner',
            'owner=owner, message="Resuming PC file upload"',
            'if not admin:',
        ):
            self.assertIn(guard, short)
        for guard in ("self.require_library_owner(", "normalize_video_spec(x,i,owner=owner)",
                      "CREATE TABLE IF NOT EXISTS library_items(",
                      "ADD COLUMN owner_username", "owner_username<>",
                      "Shared global controls are administrator-only"):
            self.assertIn(guard, merger)
        self.assertEqual(merger.count("self.require_library_owner("), 4)
        self.assertIn("admin=self.is_admin()", merger)

    def test_source_compiles_without_importing_live_apps(self):
        for path in (SHORT, MERGER):
            compile(path.read_text(), str(path), "exec")


if __name__ == "__main__":
    unittest.main()

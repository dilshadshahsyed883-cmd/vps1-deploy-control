"""Regression tests for the Scene Builder FFprobe EMFILE incident.

The isolated AST harness exercises the production db() function without
importing the worker or touching its persistent queue, services, or media.
"""
from __future__ import annotations

import ast
from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Iterator
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "apps/scene-builder/part02/api_server.py"


def isolated_db(database_path: Path):
    """Load exactly the deployed db() implementation against a temp path."""
    module = ast.parse(SOURCE.read_text(encoding="utf-8"))
    node = next(node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == "db")
    namespace = {
        "sqlite3": sqlite3,
        "DB_PATH": database_path,
        "contextmanager": contextmanager,
        "Iterator": Iterator,
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace["db"]


class SceneWorkerFdLifecycle(unittest.TestCase):
    def test_connection_always_closed_on_success(self):
        with tempfile.TemporaryDirectory() as folder:
            db = isolated_db(Path(folder) / "queue.sqlite3")
            with db() as connection:
                connection.execute("CREATE TABLE telemetry (value INTEGER)")
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")

    def test_commits_and_rolls_back_like_existing_callers(self):
        with tempfile.TemporaryDirectory() as folder:
            db = isolated_db(Path(folder) / "queue.sqlite3")
            with db() as connection:
                connection.execute("CREATE TABLE telemetry (value INTEGER)")
            with db() as connection:
                connection.execute("INSERT INTO telemetry VALUES (1)")
            with self.assertRaisesRegex(RuntimeError, "rollback"):
                with db() as connection:
                    connection.execute("INSERT INTO telemetry VALUES (2)")
                    raise RuntimeError("rollback")
            with db() as connection:
                values = [r[0] for r in connection.execute("SELECT value FROM telemetry")]
            self.assertEqual(values, [1])

    @unittest.skipUnless(Path("/proc/self/fd").exists(), "requires Linux procfs")
    def test_repeated_render_progress_does_not_exhaust_file_descriptors(self):
        with tempfile.TemporaryDirectory() as folder:
            db = isolated_db(Path(folder) / "queue.sqlite3")
            with db() as connection:
                connection.execute("CREATE TABLE telemetry (value INTEGER)")
            start = len(os.listdir("/proc/self/fd"))
            for counter in range(350):
                with db() as connection:
                    connection.execute("INSERT INTO telemetry VALUES (?)", (counter,))
            end = len(os.listdir("/proc/self/fd"))
            self.assertLessEqual(end, start + 4, f"FD leak: before={start} after={end}")
            with db() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0], 350)


if __name__ == "__main__":
    unittest.main()

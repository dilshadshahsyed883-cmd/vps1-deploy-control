"""Safe SQLite regressions: exact-owner manual retry and accurate per-user counters.

AST extracts original repository methods without importing the live worker, token
store, network clients or production directories.
"""
from __future__ import annotations

import ast
import sqlite3
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any, Iterable, Optional

ROOT=Path(__file__).resolve().parents[1]
SOURCES=[
    ROOT/"apps/local-upload-manager/part01",
    ROOT/"apps/copyright-uploader/part01",
]


def method_at(path: Path, cls: str, name: str, namespace: dict) -> object:
    module=ast.parse(path.read_text(encoding="utf-8"))
    parent=next(c for c in module.body if isinstance(c,ast.ClassDef) and c.name==cls)
    fn=next(x for x in parent.body if isinstance(x,ast.FunctionDef) and x.name==name)
    fn.decorator_list=[]
    out=ast.fix_missing_locations(ast.Module(body=[fn],type_ignores=[]))
    env={"Optional":Optional,"Iterable":Iterable,"Any":Any,**namespace}
    exec(compile(out,str(path),"exec"),env)
    return env[name]


class RepositoryOwnerRegressions(unittest.TestCase):
    def test_paused_job_retry_never_wakes_other_users(self):
        for source in SOURCES:
            with self.subTest(source=source.name):
                with tempfile.TemporaryDirectory() as tmp:
                    path=Path(tmp)/"queue.sqlite"
                    with sqlite3.connect(path) as con:
                        con.executescript("""
                            CREATE TABLE videos(
                               id INTEGER PRIMARY KEY,status TEXT,
                               recovery_state TEXT,resumable_uri TEXT,
                               claim_worker_id TEXT,claimed_at TEXT,
                               lease_expires_at TEXT,updated_at TEXT,
                               auto_engine_profile_id INTEGER
                            );
                            INSERT INTO videos(id,status,recovery_state,resumable_uri)
                              VALUES(1,'PAUSED','NONE','');
                            INSERT INTO videos(id,status,recovery_state,resumable_uri)
                              VALUES(2,'PAUSED','NONE','remote');
                            INSERT INTO videos(id,status,recovery_state,resumable_uri)
                              VALUES(3,'PAUSED','REVIEW_REQUIRED','');
                        """)
                    class Fake:
                        def __init__(self):
                            self.ledger=[]
                        def connect(self):
                            con=sqlite3.connect(path)
                            con.row_factory=sqlite3.Row
                            return con
                        def _set_ledger_state(self, con, vid, state):
                            self.ledger.append((vid,state))
                    repo=Fake()
                    method=method_at(
                        source/"desktop_core/modules/db/queue_repository.py",
                        "QueueRepository","resume_paused",{})
                    self.assertEqual(method(repo,exclude_auto_engine=True,video_ids=[1]),1)
                    with repo.connect() as con:
                        rows=dict(con.execute("SELECT id,status FROM videos"))
                    self.assertEqual(rows,{1:"QUEUED",2:"PAUSED",3:"PAUSED"})
                    self.assertEqual(repo.ledger,[(1,"RESERVED")])
                    self.assertEqual(method(repo,exclude_auto_engine=True,video_ids=[]),0)
                    self.assertEqual(method(repo,exclude_auto_engine=True,video_ids=[3]),0)
                    self.assertEqual(method(repo,exclude_auto_engine=True,video_ids=[2]),1)
                    self.assertEqual(repo.ledger[-1],(2,"RESUMABLE"))

    def test_project_status_counts_are_per_user_and_admin_is_global(self):
        for source in SOURCES:
            with self.subTest(source=source.name):
                with tempfile.TemporaryDirectory() as tmp:
                    path=Path(tmp)/"projects.sqlite"
                    with sqlite3.connect(path) as con:
                        con.executescript("""
                            CREATE TABLE projects(id INTEGER PRIMARY KEY,
                              owner_username TEXT NOT NULL,status TEXT NOT NULL);
                            INSERT INTO projects VALUES(1,'Alice','QUEUED');
                            INSERT INTO projects VALUES(2,'Alice','COMPLETED');
                            INSERT INTO projects VALUES(3,'Bob','FAILED');
                            INSERT INTO projects VALUES(4,'Bob','QUEUED');
                        """)
                    class Fake:
                        def connect(self):
                            con=sqlite3.connect(path)
                            con.row_factory=sqlite3.Row
                            return con
                    method=method_at(
                        source/"lum_engine/project_store.py","ProjectStore","counts",
                        {"ACTIVE_STATES":{"UPLOADING"},"WAITING_STATES":{"QUEUED"}})
                    a=method(Fake(),owner="ALICE")
                    b=method(Fake(),owner="bob")
                    admin=method(Fake())
                    self.assertEqual((a["TOTAL"],a["WAITING"]),(2,1))
                    self.assertEqual((b["TOTAL"],b["WAITING"]),(2,1))
                    self.assertEqual(admin["TOTAL"],4)
                    self.assertNotIn("FAILED",a)
                    self.assertNotIn("COMPLETED",b)


if __name__=="__main__":
    unittest.main()

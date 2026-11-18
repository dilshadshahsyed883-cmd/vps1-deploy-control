"""Agent 4: read-only, temporary-fixture security tests for the pre-isolation baseline.

These are NOT evidence that Concat Studio multiuser access is safe. They protect
the existing admin-only gate and the reusable Copyright tenant boundary while
the production owner-scoping implementation is pending. No VPS, real tokens,
external Drive, Salad, or YouTube services are contacted.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

ROOT = Path(__file__).resolve().parents[1]
SCENE = ROOT / "apps" / "scene-builder" / "part01"
COPYRIGHT = ROOT / "apps" / "copyright-uploader" / "part01" / "lum_engine"
sys.path.insert(0, str(SCENE))
from cloud_render import register_cloud_render

MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 81


class CloudRenderOwnerAPITests(unittest.TestCase):
    """Alice, Bob and Admin receive only their own Cloud Render resources."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        app = FastAPI()

        def require_login(request: Request):
            if not request.session.get("authenticated"):
                raise HTTPException(401, "Login required")

        def require_admin(request: Request):
            if not request.session.get("is_admin"):
                raise HTTPException(403, "Administrator access required")

        self.manager = register_cloud_render(
            app, Jinja2Templates(directory=str(SCENE / "templates")),
            self.tmp.name, require_admin, require_login,
        )
        self.addCleanup(self.manager.pool.shutdown, wait=True)

        @app.get("/_audit/session/{user}")
        def set_session(request: Request, user: str):
            request.session.clear()
            if user in ("alice", "bob", "admin"):
                request.session.update({
                    "authenticated": True, "username": user,
                    "is_admin": user == "admin",
                })
            return {"user": user}

        app.add_middleware(SessionMiddleware, secret_key="temporary-audit-fixture")
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        for user, job in (("alice", "alice-media"), ("bob", "bob-media")):
            with self.manager.connection() as con:
                con.execute("""INSERT INTO production_jobs
                    (remote_id,drive_id,name,resource_keys,drive_size,drive_md5,
                     folder_id,drive_path,owner_username) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (job, "drive-" + job, job + ".zip", "", 0, "", "", "", user))
            self.manager.receive(job, 0, MP4, name=job + ".mp4",
                                 size=len(MP4), sha=hashlib.sha256(MP4).hexdigest())
        # Legacy record has no verified account association and is invisible.
        with self.manager.connection() as con:
            con.execute("""INSERT INTO files(id,name,size,sha256,state)
                           VALUES(?,?,?,?,?)""",
                        ("legacy-media", "legacy.mp4", len(MP4),
                         hashlib.sha256(MP4).hexdigest(), "ready"))

    def test_session_list_and_download_deny_other_owners_even_admin(self):
        expected = {"alice": {"alice-media"}, "bob": {"bob-media"}, "admin": set()}
        for user, ids in expected.items():
            self.client.get("/_audit/session/" + user)
            with self.subTest(user=user):
                media = self.client.get("/api/cloud-render/media",
                                        headers={"X-Auth-User": "alice",
                                                 "X-Is-Admin": "1"})
                self.assertEqual(media.status_code, 200)
                self.assertEqual({row["id"] for row in media.json()["items"]}, ids)
                self.assertEqual(self.client.get("/api/cloud-render/production").status_code, 200)
                for job in ("alice-media", "bob-media", "legacy-media"):
                    res = self.client.get("/api/cloud-render/media/" + job + "/file")
                    self.assertEqual(res.status_code, 200 if job in ids else 404)

    def test_owner_credentials_are_private_and_admin_not_exempt(self):
        self.manager.save({"worker_key": "alice-secret", "endpoint": "https://fixture.salad.cloud"}, "alice")
        self.manager.save({"worker_key": "bob-secret"}, "bob")
        for user, expected_endpoint in (("alice", "https://fixture.salad.cloud"),
                                        ("bob", ""), ("admin", "")):
            self.client.get("/_audit/session/" + user)
            cfg = self.client.get("/api/cloud-render/config")
            self.assertEqual(cfg.status_code, 200)
            self.assertEqual(cfg.json()["endpoint"], expected_endpoint)
            self.assertEqual(cfg.json()["worker_key_saved"], user != "admin")
            self.assertNotIn("alice-secret", cfg.text)
            self.assertNotIn("bob-secret", cfg.text)

    def test_anonymous_and_spoofed_headers_cannot_access_cloud_render(self):
        self.client.get("/_audit/session/guest")
        for path in ("/cloud-render/", "/api/cloud-render/media",
                     "/api/cloud-render/production", "/api/cloud-render/config"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path, headers={
                    "X-Auth-User": "admin", "X-Is-Admin": "1"
                }).status_code, 401)

    def test_direct_ingest_requires_token_and_owner_bound_job(self):
        self.client.get("/_audit/session/guest")
        data = b"ignored"
        params={"index": 0, "name": "orphan.mp4", "size": len(MP4),
                "sha256": hashlib.sha256(MP4).hexdigest()}
        path="/api/cloud-render/ingest/orphan/chunk"
        self.assertEqual(self.client.post(path, params=params, content=data).status_code, 401)
        token=self.manager.rotate_token()["receiver_token"]
        response=self.client.post(path, params=params, content=data,
                                  headers={"X-Cloud-Render-Token": token})
        self.assertEqual(response.status_code, 404)

    def test_browser_writes_require_csrf_for_every_role(self):
        for user in ("alice", "bob", "admin"):
            self.client.get("/_audit/session/" + user)
            for path in ("/api/cloud-render/production/jobs/999/cancel",
                         "/api/cloud-render/production/queue/start",
                         "/api/cloud-render/media/bob-media/rename"):
                with self.subTest(user=user, path=path):
                    response = self.client.post(path, json={"name": "ignored.mp4"})
                    self.assertEqual(response.status_code, 403)

    def test_cross_owner_media_mutation_denied(self):
        for user, foreign_job in (("alice", "bob-media"),
                                  ("bob", "alice-media"),
                                  ("admin", "alice-media")):
            self.client.get("/_audit/session/" + user)
            with self.manager.connection() as con:
                self.assertIsNone(con.execute(
                    "SELECT id FROM files WHERE id=? AND owner_username=?",
                    (foreign_job, user)).fetchone())
            with self.assertRaises(HTTPException) as denied:
                self.manager.rename(foreign_job, "newname.mp4", user)
            self.assertEqual(denied.exception.status_code, 404)

class CopyrightOwnerIsolationReferenceTests(unittest.TestCase):
    """Protect the strict Copyright behavior that Concat must reuse explicitly."""

    def test_two_users_admin_legacy_and_immutable_channel_sheet_bindings(self):
        spec = importlib.util.spec_from_file_location(
            "concat_audit_tenants", COPYRIGHT / "studio_tenants.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with tempfile.TemporaryDirectory() as tmp:
            class Repo:
                def connect(self):
                    return sqlite3.connect(str(Path(tmp) / "tenant.sqlite3"))

            tenants = mod.StudioTenants(Repo())
            with patch.dict(os.environ, {"LUM_ENGINE_INSTANCE": "copyright"}):
                for kind in ("channel", "sheet", "group", "profile"):
                    tenants.bind(kind, 11, "Alice")
                    tenants.bind(kind, 22, "Bob")
                alice = {"username": "alice", "is_admin": 0}
                bob = {"username": "bob", "is_admin": 0}
                admin = {"username": "admin", "is_admin": 1}

                for kind in ("channel", "sheet", "group", "profile"):
                    self.assertEqual(tenants.allowed_ids(kind, alice), {11})
                    self.assertEqual(tenants.allowed_ids(kind, bob), {22})
                    self.assertEqual(tenants.allowed_ids(kind, admin), set())
                    tenants.require(kind, 11, alice)
                    tenants.require(kind, 22, bob)
                    for actor, forbidden_id in ((alice, 22), (bob, 11),
                                                (admin, 11), (admin, 22),
                                                (admin, 99)):
                        with self.subTest(kind=kind, actor=actor["username"],
                                          forbidden=forbidden_id):
                            with self.assertRaises(PermissionError):
                                tenants.require(kind, forbidden_id, actor)
                    self.assertEqual(tenants.owner(kind, 99), "")
                    with self.assertRaises(PermissionError):
                        tenants.bind(kind, 11, "bob")

                for actor in (alice, bob, admin):
                    with self.assertRaises(PermissionError):
                        tenants.require("channel", 99, actor)
                with self.assertRaises(PermissionError):
                    tenants.allowed_ids("sheet", {"username": ""})


if __name__ == "__main__":
    unittest.main()

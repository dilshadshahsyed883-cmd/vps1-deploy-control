"""Real HTML template and admin-role route acceptance for Cloud Render."""
from __future__ import annotations
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

ROOT = Path(__file__).resolve().parents[1]
SCENE = ROOT / "apps/scene-builder/part01"
sys.path.insert(0, str(SCENE))
from cloud_render import register_cloud_render

class CloudRenderHtmlTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        app = FastAPI()
        def login(req: Request):
            if not req.session.get("authenticated"):
                raise HTTPException(401, "Login required")
        def admin(req: Request):
            if not req.session.get("is_admin"):
                raise HTTPException(403, "Administrator access required")
        self.manager = register_cloud_render(app, Jinja2Templates(directory=str(SCENE/"templates")),
                                             self.tmp.name, admin, login)
        @app.get("/_fixture/session/{role}")
        def session(req: Request, role: str):
            req.session.clear()
            if role in ("admin", "member"):
                req.session.update({"authenticated": True, "is_admin": role=="admin",
                                    "username": "owner" if role=="admin" else "member"})
            return {"role": role}
        app.add_middleware(SessionMiddleware, secret_key="test-cloud-render-session")
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.addCleanup(self.manager.pool.shutdown, wait=True)

    def test_admin_real_html_and_gpu_config_routes(self):
        self.client.get("/_fixture/session/admin")
        res = self.client.get("/cloud-render/")
        self.assertEqual(res.status_code,200)
        self.assertIn("text/html",res.headers.get("content-type",""))
        for marker in ("Cloud Render", "GPU Config", "Media", 'data-tab="gpu"', 'data-tab="media"'):
            self.assertIn(marker,res.text)
        self.assertIn('href="/cloud-render/"',res.text)
        self.assertEqual(res.text.count('data-tab="gpu"'),1)
        self.assertEqual(res.text.count('data-tab="media"'),1)
        cfg = self.client.get("/api/cloud-render/config")
        self.assertEqual(cfg.status_code,200)
        self.assertFalse(cfg.json()["image_ready"])
        self.assertEqual(self.client.get("/api/cloud-render/media").json()["items"],[])

    def test_member_and_guest_cannot_read_media(self):
        for role in ("member","guest"):
            self.client.get("/_fixture/session/"+role)
            res = self.client.get("/cloud-render/")
            self.assertIn(res.status_code,(401,403))
            self.assertIn(self.client.get("/api/cloud-render/config").status_code,(401,403))
            self.assertIn(self.client.get("/api/cloud-render/media").status_code,(401,403))

    def test_mutation_requires_admin_csrf(self):
        self.client.get("/_fixture/session/admin")
        self.assertEqual(self.client.post("/api/cloud-render/config",json={"auto_upload":True}).status_code,403)
        self.assertFalse(self.client.get("/api/cloud-render/config").json()["auto_upload"])

if __name__ == "__main__":
    unittest.main()

"""Copyright-only non-admin ownership and manual-queue handover regression.

Uses disposable SQLite, no production credentials, no YouTube upload.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
COPY = ROOT / "apps/copyright-uploader/part01"


class CopyrightMemberHandover(unittest.TestCase):
    def test_persistent_owner_pause_and_metadata_sql_guards(self):
        script = r"""
from lum_engine.project_store import ProjectStore
store = ProjectStore()

def add(owner, title):
    return store.create_project(dict(
        owner_username=owner, title=title, channel_db_id=7,
        drive_url="https://drive.google.com/file/d/example/view",
        source_mode="drive_link", source_origin="drive_link",
    ))

alice=add("alice","Alice video")
bob=add("bob","Bob video")
assert not store.owner_queue_paused("alice")
store.set_owner_queue_paused("ALICE", True)
assert store.owner_queue_paused("alice")
store=ProjectStore()
assert store.owner_queue_paused("alice"), "pause must persist across restarts"
assert not store.edit_waiting_metadata(alice,"bob",
    title="Attacker",description="",tags="",category_id="22",privacy_status="private")
assert store.edit_waiting_metadata(alice,"alice",
    title="Alice updated",description="Owner metadata",tags="a,b",
    category_id="24",privacy_status="public")
assert store.get(alice)["title"] == "Alice updated"
assert store.get(alice)["privacy_status"] == "public"
assert store.get(bob)["title"] == "Bob video"
assert not store.cancel_waiting(alice,"bob")
claimed=store.claim_next_eligible()
assert claimed and claimed["id"] == bob, "paused owner must be skipped, not block others"
assert not store.claim_next_eligible(), "paused owner's waiting job is protected"
store.set_owner_queue_paused("alice",False)
assert not store.owner_queue_paused("alice")
claimed=store.claim_next_eligible()
assert claimed and claimed["id"]==alice, "resume enables only this owner"
assert not store.edit_waiting_metadata(alice,"alice",
    title="Late edit",description="",tags="",category_id="22",privacy_status="private"), "claimed jobs immutable"
assert not store.cancel_waiting(alice,"alice")
assert store.get(alice)["title"]=="Alice updated"
assert store.get(bob)["title"]=="Bob video"
print("OWNER_PAUSE_SQLITE=PASS")
"""
        with tempfile.TemporaryDirectory() as temp:
            env = {**os.environ, "LUM_DATA_DIR": temp,
                   "YOUTUBE_UPLOAD_MANAGER_DATA_DIR": str(Path(temp) / "core"),
                   "LUM_ENGINE_INSTANCE": "copyright",
                   "PYTHONDONTWRITEBYTECODE": "1"}
            run = subprocess.run([sys.executable, "-c", script],
                                 cwd=str(COPY), env=env, text=True,
                                 capture_output=True)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn("OWNER_PAUSE_SQLITE=PASS", run.stdout)

    def test_two_identities_http_isolation_without_uploading(self):
        script = r"""
import re
from fastapi.testclient import TestClient
from lum_engine.standalone_app import app, store

def add(owner, title):
    return store.create_project(dict(owner_username=owner, title=title,
        channel_db_id=7, drive_url="https://drive.google.com/file/d/example/view",
        source_mode="drive_link", source_origin="drive_link"))

alice=add("alice", "Private Alice Video")
bob=add("bob", "Private Bob Video")
client=TestClient(app, follow_redirects=False)
def headers(owner):
    return {"X-Auth-User": owner, "X-App-Access": "lum", "X-Is-Admin": "0"}
def csrf(owner):
    response=client.get("/copyright-uploader/app?tab=queue", headers=headers(owner))
    assert response.status_code == 200, response.text[:500]
    token=re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert token, "rendered CSRF token missing"
    return token.group(1), response.text
token, alice_page=csrf("alice")
_, bob_page=csrf("bob")
assert "Private Alice Video" in alice_page and "Private Bob Video" not in alice_page
assert "Private Bob Video" in bob_page and "Private Alice Video" not in bob_page
bad=client.post("/copyright-uploader/actions/copyright/queue/bulk",
    headers=headers("bob"), data={"csrf_token":token,"action":"cancel","project_ids":str(alice)})
assert bad.status_code==403,bad.text
assert store.get(alice)["status"]=="QUEUED"
bad=client.post(f"/copyright-uploader/actions/copyright/metadata/{alice}",
    headers=headers("bob"), data={"csrf_token":token,"title":"stolen"})
assert bad.status_code==403,bad.text
pause=client.post("/copyright-uploader/actions/copyright/queue/pause",
    headers=headers("alice"),data={"csrf_token":token})
assert pause.status_code==303,pause.text
assert store.owner_queue_paused("alice") and not store.owner_queue_paused("bob")
claimed=store.claim_next_eligible()
assert claimed and claimed["id"]==bob
blocked=client.get("/copyright-uploader/api/auto-v6/engine",headers=headers("alice"))
assert blocked.status_code==404,blocked.text
print("TWO_IDENTITY_HTTP=PASS")
"""
        with tempfile.TemporaryDirectory() as temp:
            env = {**os.environ, "LUM_DATA_DIR": temp,
                   "YOUTUBE_UPLOAD_MANAGER_DATA_DIR": str(Path(temp) / "core"),
                   "LUM_ENGINE_INSTANCE": "copyright",
                   "LUM_BASE_PATH": "/copyright-uploader",
                   "LUM_SUITE_AUTH": "1",
                   "LUM_COOKIE_SECURE": "0",
                   "LUM_SESSION_SECRET": "copyright-ci-disposable-only",
                   "PYTHONDONTWRITEBYTECODE": "1"}
            run = subprocess.run([sys.executable, "-c", script],
                                 cwd=str(COPY), env=env, text=True, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn("TWO_IDENTITY_HTTP=PASS", run.stdout)

    def test_both_roles_render_owned_manual_controls(self):
        from production.copyright_uploader_browser import fixture
        for role in (False, True):
            queue = fixture("queue", is_admin=role)
            sheets = fixture("sheets", is_admin=role)
            self.assertIn("/actions/copyright/queue/pause", queue)
            self.assertIn("/actions/copyright/queue/bulk", queue)
            self.assertIn("/actions/copyright/metadata/1", queue)
            self.assertNotIn("/actions/copyright/metadata/4", queue)
            self.assertIn('name="project_ids"', queue)
            self.assertIn("/actions/sheets/sync-all", sheets)
            self.assertNotIn("Auto Engine</a>", queue)
            if not role:
                self.assertNotIn("/actions/stop-current", queue)
                self.assertNotIn("/actions/pause-after-current", queue)
                self.assertNotIn("/actions/resume", queue)

    def test_server_routes_scoped_and_scheduler_blocked(self):
        app = (COPY / "lum_engine/standalone_app.py").read_text()
        for route in ("queue/pause", "queue/start", "queue/bulk"):
            self.assertIn('route("/actions/copyright/' + route + '")', app)
        self.assertIn('route("/actions/copyright/metadata/")', app)
        self.assertIn('Bulk selection contains an unowned project', app)
        self.assertIn('studio_tenants.allowed_ids("sheet", user)', app)
        self.assertIn('studio_tenants.sync_owned_sheet_rows(', app)
        self.assertIn('Automatic uploader engine is disabled for Copyright Studio', app)
        self.assertIn('store.owner_queue_paused(', app)


if __name__ == "__main__":
    unittest.main()

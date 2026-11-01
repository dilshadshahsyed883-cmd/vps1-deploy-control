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
assert store.archive_terminal(alice, "bob") is False
assert store.archive_terminal(alice, "alice") is False  # claimed/active is immutable
terminal=add("alice","Completed Alice")
store.update(terminal, status="COMPLETED")
assert store.archive_terminal(terminal,"bob") is False
assert store.archive_terminal(terminal,"alice") is True
assert store.get(terminal)["status"] == "COMPLETED"
assert not any(x["id"]==terminal for x in store.list(owner="alice"))
p=store.add_copyright_profile("alice","Owner Profile","Group A","manual")
assert not store.update_copyright_profile("bob",p,"Hijacked","Group B","sheets")
assert store.update_copyright_profile("alice",p,"New Name","Group A","sheets")
assert not store.delete_copyright_profile("bob",p)
assert store.delete_copyright_profile("alice",p)
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
admin_job=add("administrator", "Private Admin Video")
client=TestClient(app, follow_redirects=False)
def headers(owner,admin=False):
    return {"X-Auth-User": owner, "X-App-Access": "lum", "X-Is-Admin": "1" if admin else "0"}
def csrf(owner,admin=False):
    response=client.get("/copyright-uploader/app?tab=queue", headers=headers(owner,admin))
    assert response.status_code == 200, response.text[:500]
    token=re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert token, "rendered CSRF token missing"
    return token.group(1), response.text
token, alice_page=csrf("alice")
_, bob_page=csrf("bob")
_, admin_page=csrf("administrator",True)
assert "Private Admin Video" in admin_page and "Private Alice Video" not in admin_page and "Private Bob Video" not in admin_page
assert "Private Admin Video" not in alice_page and "Private Admin Video" not in bob_page
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
blocked_admin=client.post("/copyright-uploader/actions/pause-after-current",headers=headers("administrator",True),data={"csrf_token":token})
assert blocked_admin.status_code==404,blocked_admin.text
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
                   "LUM_AUTO_ENGINE_V6_ROOT": str(Path(temp) / "auto_engine_v6"),
                   "LUM_GOOGLE_OAUTH_ROOT": str(Path(temp) / "secure"),
                   "LUM_BASE_PATH": "/copyright-uploader",
                   "LUM_SUITE_AUTH": "1",
                   "LUM_COOKIE_SECURE": "0",
                   "LUM_SESSION_SECRET": "copyright-ci-disposable-only",
                   "PYTHONDONTWRITEBYTECODE": "1"}
            run = subprocess.run([sys.executable, "-c", script],
                                 cwd=str(COPY), env=env, text=True, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn("TWO_IDENTITY_HTTP=PASS", run.stdout)

    def test_private_oauth_client_per_account_and_no_shared_overwrite(self):
        script = r"""
import json
from lum_engine.oauth_web import (
    _owner_client_path, save_owner_client_json, list_owner_clients, owner_client_for_oauth
)
data = {"installed": {
    "client_id": "test-disposable.apps.googleusercontent.com",
    "project_id": "copyright-ci-disposable",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
    "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
    "client_secret": "disposable-only-secret",
    "redirect_uris": ["http://localhost:8765/"]
}}
valid = json.dumps(data).encode("utf-8")
alice = _owner_client_path("alice")
bob = _owner_client_path("bob")
assert alice != bob
save_owner_client_json("alice", valid)
assert alice.is_file() and not bob.exists()
assert list_owner_clients("alice")[0]["label"] == "my_oauth_client"
assert owner_client_for_oauth("alice") == alice
try:
    save_owner_client_json("bob", b"not JSON")
except ValueError:
    pass
else:
    raise AssertionError("Invalid credentials accepted")
assert alice.is_file() and not bob.exists()
save_owner_client_json("bob", valid)
assert bob.is_file() and alice.read_bytes() == bob.read_bytes()
print("PRIVATE_OAUTH_ISOLATION=PASS")
"""
        with tempfile.TemporaryDirectory() as temp:
            env = {**os.environ, "LUM_DATA_DIR": temp,
                   "YOUTUBE_UPLOAD_MANAGER_DATA_DIR": str(Path(temp) / "core"),
                   "LUM_ENGINE_INSTANCE": "copyright", "PYTHONDONTWRITEBYTECODE": "1"}
            run = subprocess.run([sys.executable, "-c", script],
                                 cwd=str(COPY), env=env, text=True, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn("PRIVATE_OAUTH_ISOLATION=PASS", run.stdout)

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
            self.assertNotIn("/actions/stop-current", queue)
            self.assertNotIn("/actions/pause-after-current", queue)
            self.assertNotIn("/actions/resume", queue)
            self.assertIn("/actions/copyright/queue/remove",queue)
            self.assertIn("Remove Done / Cancelled",queue)

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
        self.assertIn('route("/actions/copyright/profile/update")', app)
        self.assertIn('route("/actions/copyright/profile/delete")', app)
        self.assertIn('store.archive_terminal(',app)
        self.assertIn('os.getenv("LUM_ENGINE_INSTANCE") == "copyright"', app)


if __name__ == "__main__":
    unittest.main()

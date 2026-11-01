"""Real ASGI routes and migrated SQLite schemas, with no workers or providers.

Each uploader runs in a fresh process and temporary data root. TestClient is
deliberately not entered as a context manager: no startup/upload worker runs.
Only provider calls are mocked; authentication, CSRF, routing, templates,
ownership checks and SQLite migrations are the application implementations.
"""
from pathlib import Path
import importlib.util
from types import SimpleNamespace
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def exercise(mode):
    with tempfile.TemporaryDirectory(prefix="studio-tenant-http-") as temporary:
        os.environ.update(
            LUM_DATA_DIR=temporary, LUM_SESSION_SECRET="offline-http-test-secret",
            LUM_SUITE_AUTH="1", LUM_BASE_PATH="/test", LUM_ENGINE_INSTANCE=mode,
            YOUTUBE_UPLOAD_MANAGER_DATA_DIR=temporary + "/core",
            LUM_AUTO_ENGINE_V6_ROOT=temporary + "/auto-engine-v6",
            LUM_GOOGLE_OAUTH_ROOT=temporary + "/secure-oauth",
            SHORT_STUDIO_VPS_FOLDER_ROOT=temporary + "/short-maker-vps",
        )
        package = "copyright-uploader" if mode == "copyright" else "local-upload-manager"
        sys.path.insert(0, str(ROOT / "apps" / package / "part01"))
        from fastapi.testclient import TestClient
        from lum_engine import standalone_app as app

        def client(name, admin=False):
            return TestClient(app.app, follow_redirects=False, headers={
                "X-Auth-User": name, "X-App-Access": "lum",
                "X-Is-Admin": "1" if admin else "0",
            })

        alice, bob, admin = client("Alice"), client("Bob"), client("Admin", True)

        def csrf(viewer):
            response = viewer.get("/test/app?tab=channels")
            assert response.status_code == 200, response.text[:300]
            return re.search(r'name="csrf_token" value="([^"]+)"', response.text)[1]

        a_token, b_token, admin_token = map(csrf, (alice, bob, admin))

        def post(viewer, token, action, **data):
            return viewer.post("/test/actions/" + action,
                               data={"csrf_token": token, **data})

        for viewer, token, letter, title in (
            (alice, a_token, "a", "ALICE_PRIVATE_CHANNEL"),
            (bob, b_token, "b", "BOB_PRIVATE_CHANNEL"),
        ):
            response = post(viewer, token, "studio-tenant/channel-create",
                            channel_id="UC" + letter * 22, channel_name=title)
            if mode == "song":
                # Retired Song onboarding must reject direct POSTs. Seed legacy
                # ownership directly to keep testing tenant isolation and OAuth.
                assert response.status_code == 404
                with app.db.connect() as connection:
                    channel_db_id = connection.execute(
                        "INSERT INTO channels(channel_id,channel_name,token_file_path,status) "
                        "VALUES (?,?,'','ACTIVE')",
                        ("UC" + letter * 22, title)).lastrowid
                app.studio_tenants.bind("channel", channel_db_id,
                                        "Alice" if letter == "a" else "Bob")
            else:
                assert response.status_code == 303 and "error=" not in response.headers["location"]
        a_id = app.studio_tenants.allowed_ids("channel", {"username": "Alice"}).pop()
        b_id = app.studio_tenants.allowed_ids("channel", {"username": "Bob"}).pop()
        for viewer, own, foreign in (
            (alice, "ALICE_PRIVATE_CHANNEL", "BOB_PRIVATE_CHANNEL"),
            (bob, "BOB_PRIVATE_CHANNEL", "ALICE_PRIVATE_CHANNEL"),
        ):
            html = viewer.get("/test/app?tab=channels").text
            assert own in html and foreign not in html
        html = admin.get("/test/app?tab=channels").text
        if mode == "shortstudio":
            # Short admin uses the SAME private workspace rules as a member.
            assert "ALICE_PRIVATE_CHANNEL" not in html and "BOB_PRIVATE_CHANNEL" not in html
        else:
            assert "ALICE_PRIVATE_CHANNEL" in html and "BOB_PRIVATE_CHANNEL" in html

        duplicate = post(bob, b_token, "studio-tenant/channel-create",
                         channel_id="UC" + "a" * 22, channel_name="Hijack")
        group = post(alice, a_token, "studio-tenant/group-create",
                     group_name="Alice private group", channel_ids=str(a_id))
        mixed = post(alice, a_token, "studio-tenant/group-create",
                     group_name="Invalid mixed group", channel_ids=[str(a_id), str(b_id)])
        if mode == "song":
            assert duplicate.status_code == group.status_code == mixed.status_code == 404
        else:
            assert "error=" in duplicate.headers["location"]
            assert "error=" not in group.headers["location"]
            assert "error=" in mixed.headers["location"]

        # Exercise both the permitted and forbidden OAuth branches: a NameError
        # hidden in a redirect must not count as successful access control.
        for action, method in (("test", "test_connection"), ("activate", "select_active")):
            with patch.object(app._dual_auth, method, return_value={"method": "web"}) as provider:
                denied = post(alice, a_token, "auth-connections/" + action,
                              channel_db_id=b_id, method="web")
                assert "error=" in denied.headers["location"]
                provider.assert_not_called()
                allowed = post(alice, a_token, "auth-connections/" + action,
                               channel_db_id=a_id, method="web")
                assert "error=" not in allowed.headers["location"], allowed.headers["location"]
                provider.assert_called_once()
        with patch.object(app._dual_auth, "begin", return_value="https://example.invalid/oauth") as provider:
            denied = post(alice, a_token, "google-oauth/connect", channel_db_id=b_id)
            assert "error=" in denied.headers["location"]
            provider.assert_not_called()

        # Legacy grant uses the actual migrated core schema, not a hand-built
        # subset. Existing channel ownership cannot be transferred by an admin.
        with app.db.connect() as connection:
            legacy_id = connection.execute(
                "INSERT INTO channels(channel_id,channel_name,token_file_path,status) VALUES (?,?,'','ACTIVE')",
                ("UC" + "c" * 22, "LEGACY_PRIVATE_CHANNEL"),
            ).lastrowid
        assert "LEGACY_PRIVATE_CHANNEL" not in alice.get("/test/app?tab=channels").text
        grant = post(admin, admin_token, "studio-tenant/grant", entity_kind="channel",
                     entity_id=legacy_id, owner_username="Alice")
        if mode == "shortstudio":
            assert grant.status_code == 403 or "error=" in grant.headers.get("location", "")
            assert app.studio_tenants.owner("channel", legacy_id) == ""
            app.studio_tenants.bind("channel",legacy_id,"Alice")  # test fixture only
        else:
            assert "error=" not in grant.headers["location"], grant.headers["location"]
            assert app.studio_tenants.owner("channel", legacy_id) == "alice"
        transfer = post(admin, admin_token, "studio-tenant/grant", entity_kind="channel",
                        entity_id=b_id, owner_username="Alice")
        assert "error=" in transfer.headers["location"]
        assert app.studio_tenants.owner("channel", b_id) == "bob"
        denied = post(alice, a_token, "studio-tenant/grant", entity_kind="channel",
                      entity_id=b_id, owner_username="Alice")
        assert denied.status_code == 403
        denied = post(alice, "incorrect", "studio-tenant/channel-create",
                      channel_id="UC" + "d" * 22, channel_name="Invalid CSRF")
        assert denied.status_code == 403

        # Verify user-owned first-tab Sheet Sync through the real ASGI path.
        # A foreign channel in the parsed Sheet must abort all SQLite writes.
        app.store.set_setting("sheets_sync_enabled","1")
        source_id=app.db.save_sheet_source(sheet_name="Alice Workspace",
            sheet_url_or_id="https://docs.google.com/spreadsheets/d/offline-fixture",
            gid="FIRST_TAB",enabled=True)
        app.studio_tenants.bind("sheet",source_id,"alice")
        own_sheet_row=SimpleNamespace(key="alice-sheet-row",
            channel_id="UC"+"x"*22,channel_name="ALICE_SHEET_CHANNEL",
            gmail="alice@example.com",notes="")
        bob_sheet_row=SimpleNamespace(key="bob-sheet-row",
            channel_id="UC"+"b"*22,channel_name="BOB_PRIVATE_CHANNEL",
            gmail="bob@example.com",notes="")
        with (patch.object(app,"tenant_fetch_sheet_csv",return_value=[{"fixture":"row"}]),
              patch.object(app,"tenant_rows_to_sheet_rows",
                           return_value=[own_sheet_row,bob_sheet_row]) as parser):
            foreign_post=post(bob,b_token,"sheets/sync",source_id=source_id)
            assert "error=" in foreign_post.headers["location"]
            mixed_post=post(alice,a_token,"sheets/sync",source_id=source_id)
            assert "error=" in mixed_post.headers["location"]
            parser.assert_called_once()
        assert "ALICE_SHEET_CHANNEL" not in alice.get("/test/app?tab=channels").text
        with (patch.object(app,"tenant_fetch_sheet_csv",return_value=[{"fixture":"row"}]),
              patch.object(app,"tenant_rows_to_sheet_rows",return_value=[own_sheet_row])):
            owned_post=post(alice,a_token,"sheets/sync",source_id=source_id)
            assert "error=" not in owned_post.headers["location"],owned_post.headers["location"]
        assert "ALICE_SHEET_CHANNEL" in alice.get("/test/app?tab=channels").text
        assert "ALICE_SHEET_CHANNEL" not in bob.get("/test/app?tab=channels").text
        removed=post(alice,a_token,"sheets/delete",source_id=source_id)
        assert "error=" not in removed.headers["location"],removed.headers["location"]

        # Queue history and logs must not reveal other owners or global events
        # that can contain private paths/channel identifiers.
        for owner in ("Alice", "Bob"):
            project = app.store.create_project({
                "owner_username": owner, "title": owner + "_PRIVATE_JOB",
                "drive_url": "https://drive.google.com/file/d/offline-fixture/view",
                "channel_db_id": a_id if owner == "Alice" else b_id,
            })
            app.store.event(project, owner + "_PRIVATE_EVENT")
        app.store.event(None, "GLOBAL_PRIVATE_EVENT")
        for viewer, own, foreign in ((alice, "Alice", "Bob"), (bob, "Bob", "Alice")):
            for tab in ("dashboard", "logs"):
                html = viewer.get("/test/app?tab=" + tab).text
                assert foreign + "_PRIVATE_JOB" not in html
                assert foreign + "_PRIVATE_EVENT" not in html
                assert "GLOBAL_PRIVATE_EVENT" not in html
            html = viewer.get("/test/app?tab=logs").text
            assert own + "_PRIVATE_EVENT" in html
        assert TestClient(app.app, follow_redirects=False).get("/test/app").status_code == 303
        assert not app.worker.thread, "HTTP verification must never start an upload worker"


class TenantHTTPTests(unittest.TestCase):
    def test_three_independent_uploaders(self):
        # The generic offline-pilot intentionally installs no web dependencies.
        # Dedicated three-Studio owner CI installs pinned uploader requirements
        # and MUST execute the real Alice/Bob/admin HTTP tests.
        if importlib.util.find_spec("fastapi") is None:
            self.skipTest("Real HTTP integration requires pinned uploader dependencies (enforced by three-Studio CI)")
        for mode in ("song", "shortstudio", "copyright"):
            with self.subTest(mode=mode):
                result = subprocess.run([sys.executable, __file__, "--instance", mode],
                                        cwd=ROOT, text=True, capture_output=True, timeout=60,
                                        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--instance":
        exercise(sys.argv[2])
    else:
        unittest.main()

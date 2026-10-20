"""Offline V2 integration tests: no real network, token storage or YouTube publishing."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/short-stream/part01"))
from cryptography.fernet import Fernet
import auto_stream_integrations as mod
from auto_stream_engine import StreamEngine

CID = "UC" + "a" * 22


class AutoStreamIntegrationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.media = self.root / "media"
        self.media.mkdir()
        (self.media / "video.mp4").write_bytes(b"video")
        self.core = StreamEngine(self.root / "core", self.media)
        self.app = mod.AutoStreamAccounts(self.root / "core")
        self.env = patch.dict(os.environ, {
            "AUTO_STREAM_GOOGLE_CLIENT_ID": "fake-client.apps.googleusercontent.com",
            "AUTO_STREAM_GOOGLE_CLIENT_SECRET": "fake-client-secret",
            "AUTO_STREAM_GOOGLE_REDIRECT_URI": "https://app.scenebuilder.online/short-stream/engine/oauth/callback",
            "AUTO_STREAM_FERNET_KEY": Fernet.generate_key().decode(),
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_channel_metadata_import_never_grants_authorization(self):
        r = self.app.import_metadata(CID, "My channel")
        self.assertFalse(r["authorized"])
        self.assertFalse(self.app.channel_catalog()[0]["authorized"])
        with self.assertRaisesRegex(ValueError, "Authorize"):
            self.app.add_sheet(CID, "a"*24, "Sheet1!A1:F100")

    def test_oauth_pkce_state_and_single_use(self):
        begin = self.app.begin_oauth()
        self.assertIn("code_challenge_method=S256", begin["authorization_url"])
        self.assertIn("access_type=offline", begin["authorization_url"])
        from urllib.parse import parse_qs, urlparse
        params = parse_qs(urlparse(begin["authorization_url"]).query)
        self.assertNotIn(mod.SHEETS_SCOPE, params["scope"][0].split())
        with_sheets = self.app.begin_oauth(include_sheets=True)
        sheets_params = parse_qs(urlparse(with_sheets["authorization_url"]).query)
        self.assertIn(mod.SHEETS_SCOPE, sheets_params["scope"][0].split())
        state = params["state"][0]
        self.assertNotIn("fake-client-secret", begin["authorization_url"])
        sent = []
        def fake_http(url, **kwargs):
            sent.append(url)
            if url == mod.TOKEN_URL:
                return {"access_token": "fake-access", "refresh_token": "fake-refresh",
                        "scope": mod.YOUTUBE_SCOPE + " " + mod.SHEETS_SCOPE}
            return {"items": [{"id": CID, "snippet": {"title": "Channel V2"}}]}
        with patch.object(mod, "_http_json", side_effect=fake_http):
            result = self.app.finish_oauth(state, "fake-auth-code")
        self.assertEqual(result["id"], CID)
        self.assertEqual(result["title"], "Channel V2")
        self.assertEqual(len(sent), 2)
        self.assertNotIn("fake-refresh", (self.root / "core" / "integrations.sqlite3").read_bytes().decode("latin1"))
        self.assertTrue(self.app.channel_catalog()[0]["authorized"])
        with self.assertRaisesRegex(ValueError, "expired or already consumed"):
            self.app.finish_oauth(state, "fake-auth-code")

    def test_disconnecting_revokes_local_token(self):
        self.app.import_metadata(CID, "Channel")
        self.assertFalse(self.app.disconnect(CID)["authorized"])
        with self.assertRaisesRegex(ValueError, "OAuth"):
            self.app._access(CID, mod.YOUTUBE_SCOPE)

    def test_sheets_dry_run_and_draft_only(self):
        self.app.import_metadata(CID, "Channel")
        with self.app.connect() as con:
            con.execute("UPDATE channels SET authorized=1,token=?,scopes=? WHERE id=?",
                        (mod._cipher().encrypt(b"token"), mod.SHEETS_SCOPE, CID))
        link = self.app.add_sheet(CID, "abcdefghijklmnopqrstuvwxyz123", "Sheet1!A1:Z50")
        sheet = {"values": [
            ["channel_id", "media_path", "title", "scheduled_at", "enabled"],
            [CID, "video.mp4", "Scheduled Stream", "2026-11-20T18:00:00+05:00", "TRUE"],
            ["UC" + "b"*22, "video.mp4", "Wrong Channel", "2026-11-20T18:00:00+05:00", "true"],
        ]}
        def fake(url, **kwargs):
            if url == mod.TOKEN_URL:
                return {"access_token": "temporary"}
            return sheet
        with patch.object(mod, "_http_json", side_effect=fake):
            planned = self.app.sync_sheet(link["id"], dry_run=True, engine=self.core)
            self.assertEqual(planned["total"], 1)
            self.assertEqual(planned["created"], 0)
            self.assertEqual(len(planned["errors"]), 1)
            self.assertEqual(len(self.core.list_jobs()), 0)
            applied = self.app.sync_sheet(link["id"], dry_run=False, engine=self.core)
        self.assertEqual(applied["created"], 1)
        self.assertEqual(self.core.list_jobs()[0]["status"], "DRAFT")

    def test_verified_live_status_only_from_youtube(self):
        self.app.import_metadata(CID, "Channel")
        with self.app.connect() as con:
            con.execute("UPDATE channels SET authorized=1,token=?,scopes=? WHERE id=?",
                        (mod._cipher().encrypt(b"token"), mod.YOUTUBE_SCOPE, CID))
        def fake(url, **kwargs):
            if url == mod.TOKEN_URL:
                return {"access_token": "fake"}
            return {"items": [
                {"id": "b1", "status": {"lifeCycleStatus": "live", "privacyStatus": "private"}},
                {"id": "b2", "status": {"lifeCycleStatus": "ready"}},
            ]}
        with patch.object(mod, "_http_json", side_effect=fake):
            statuses = self.app.channel_live_status(CID)["broadcasts"]
        self.assertTrue(statuses[0]["youtube_verified_live"])
        self.assertFalse(statuses[1]["youtube_verified_live"])

    def test_google_api_hosts_allowlisted(self):
        with self.assertRaisesRegex(ValueError, "allowlist"):
            mod._http_json("https://example.net/exfil?token=fake")

    def test_credentials_missing_fail_closed(self):
        with patch.dict(os.environ, {"AUTO_STREAM_GOOGLE_CLIENT_ID": ""}):
            with self.assertRaisesRegex(ValueError, "not configured"):
                self.app.begin_oauth()


if __name__ == "__main__":
    unittest.main()

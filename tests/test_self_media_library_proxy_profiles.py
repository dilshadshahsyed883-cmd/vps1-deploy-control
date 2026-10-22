"""SML independent proxy profile CRUD and YouTube selection regression."""
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / "apps/self-media-library/part01"
sys.path.insert(0, str(ROOT))
import sml_proxy_profiles as profiles
import video_merger_downloader as downloader

PROFILE = {"name":"SML Proxy One","host":"proxy.example.com",
           "port":8020,"username":"my-user","password":"my-private-secret"}


class ProfileManager(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous = os.environ.get("SML_DATA_ROOT")
        os.environ["SML_DATA_ROOT"] = self.tmp.name

    def tearDown(self):
        if self.previous is None: os.environ.pop("SML_DATA_ROOT", None)
        else: os.environ["SML_DATA_ROOT"] = self.previous
        self.tmp.cleanup()

    def test_crud_persistence_active_and_0600(self):
        first = profiles.save(PROFILE)
        second = profiles.save({**PROFILE, "name":"Second proxy", "host":"other.example.com"})
        stored = Path(self.tmp.name) / "proxy_profiles.json"
        self.assertEqual(stat.S_IMODE(stored.stat().st_mode), 0o600)
        self.assertEqual(profiles.state()["active_id"], first)
        self.assertFalse(profiles.state()["enabled"])
        self.assertNotIn("my-private-secret", str(profiles.state()))
        with self.assertRaisesRegex(ValueError, "already exists"):
            profiles.save(PROFILE)
        profiles.toggle(True)
        self.assertTrue(profiles.state()["enabled"])
        self.assertIn("proxy.example.com", profiles.active_url())
        profiles.set_active(second)
        self.assertIn("other.example.com", profiles.active_url())
        profiles.delete(second)
        self.assertFalse(profiles.state()["enabled"])
        self.assertEqual(profiles.state()["active_id"], first)
        self.assertEqual(len(profiles.state()["profiles"]), 1)

    def test_edit_keeps_password_and_never_returns_it(self):
        pid = profiles.save(PROFILE)
        profiles.save({**PROFILE, "id":pid, "password":"", "name":"Renamed"})
        self.assertEqual(profiles.state()["profiles"][0]["name"],"Renamed")
        profiles.toggle(True)
        self.assertIn("my-private-secret", profiles.active_url())
        self.assertNotIn("password", str(profiles.state()).lower().replace("has_password",""))

    def test_local_selection_overrides_video_merger_without_mutation(self):
        pid = profiles.save(PROFILE)
        profiles.toggle(True)
        with patch.object(downloader, "_read_active_profile", return_value=None), \
             patch.dict(os.environ, {"SML_YTDLP_PROXY_URL":"http://system.example:8080"}):
            self.assertEqual(downloader._proxy_routes(False), [None])
            selected = downloader._proxy_routes(True)
            self.assertEqual(len(selected), 1)
            self.assertIn("proxy.example.com", selected[0])
            self.assertNotIn("system.example", selected[0])
        self.assertEqual(pid, profiles.state()["active_id"])

    def test_connectivity_test_no_secrets_and_does_not_claim_download(self):
        pid = profiles.save(PROFILE)
        class Response:
            status = 204
            def __enter__(self):return self
            def __exit__(self, *_):pass
        with patch.object(profiles, "build_opener") as opener:
            opener.return_value.open.return_value = Response()
            tested = profiles.test(pid)
        self.assertEqual(tested["connectivity"], "PASS")
        self.assertEqual(tested["youtube_video_access"], "NOT_VERIFIED")
        with patch.object(profiles, "build_opener") as opener:
            opener.return_value.open.side_effect=RuntimeError(
                "Proxy authorization http://my-user:my-private-secret@proxy.example.com"
            )
            with self.assertRaises(ValueError) as error:
                profiles.test(pid)
        self.assertNotIn("my-private-secret",str(error.exception))

    def test_refuse_world_readable_or_symlink_config(self):
        pid=profiles.save(PROFILE)
        path=profiles.config_path()
        path.chmod(0o644)
        with self.assertRaisesRegex(ValueError,"private"):
            profiles.state()
        path.chmod(0o600)
        symlink = Path(self.tmp.name) / "alias.json"
        symlink.symlink_to(path)
        with patch.object(profiles,"config_path",return_value=symlink):
            with self.assertRaisesRegex(ValueError,"symlink"):
                profiles.state()

    def test_http_api_saves_and_exposes_no_password(self):
        from fastapi.testclient import TestClient
        sys.modules.pop("app", None)
        try:
            import app as module
            with TestClient(module.app) as client:
                base = "/api/v1/media/proxy"
                empty = client.get(base)
                self.assertEqual(empty.status_code, 200)
                self.assertEqual(empty.json()["profiles"], [])
                saved = client.post(base+"/save", json=PROFILE)
                self.assertEqual(saved.status_code, 200, saved.text)
                profile_id = saved.json()["id"]
                listed = client.get(base).json()
                self.assertEqual(len(listed["profiles"]), 1)
                self.assertNotIn("password", listed["profiles"][0])
                self.assertNotIn("my-private-secret", str(listed))
                self.assertEqual(client.post(base+"/toggle", json={"enabled":True}).status_code, 200)
                self.assertTrue(client.get(base).json()["enabled"])
                self.assertTrue(client.get("/api/v1/media/status").json()["proxy_configured"])
                self.assertEqual(client.post(base+"/delete",json={"id":profile_id}).status_code,200)
                self.assertFalse(client.get(base).json()["enabled"])
        finally:
            sys.modules.pop("app", None)

    def test_proxy_ui_has_all_video_merger_equivalent_controls(self):
        html=(ROOT/"static/index.html").read_text()
        js=(ROOT/"static/app.js").read_text()
        for name in ("smlProxyName","smlProxyHost","smlProxyPort","smlProxyUser",
                     "smlProxyPass","smlProxyEnabled","smlProxyList","smlProxyForm"):
            self.assertIn('id="'+name+'"', html)
        for action in ("save","toggle","active","test","delete"):
            self.assertIn("smlProxyRequest('"+action+"'", js)
        self.assertIn("No saved proxy profiles",js)
        self.assertIn("Leave blank to keep saved password",js)


if __name__ == "__main__":
    unittest.main()

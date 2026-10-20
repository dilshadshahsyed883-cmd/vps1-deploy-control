"""Offline tests for Experiment Studio persistent browser profiles and privacy boundaries."""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "apps/scene-builder/part01"
sys.path.insert(0, str(SOURCE))
from experiment_browser import (BrowserProfileEngine, BrowserError, validate_profile,
                                validate_proxy, validate_url, MAX_ACTIVE)


class ValidationTests(unittest.TestCase):
    def test_profile_normalization_and_proxy_credentials(self):
        info = validate_profile({
            "name": " Research ", "start_url": "https://example.org/",
            "proxy_server": "http://proxy.example.com:8080",
            "proxy_username": "foo", "proxy_password": "bar",
            "locale": "en-US", "timezone": "Asia/Karachi",
            "width": 1280, "height": 800,
        })
        self.assertEqual(info["name"], "Research")
        self.assertEqual(info["proxy_username"], "foo")
        self.assertEqual(info["proxy_password"], "bar")
        self.assertEqual(info["timezone"], "Asia/Karachi")

    def test_private_navigation_and_invalid_proxy_denied(self):
        for value in ("http://localhost:80", "http://127.0.0.1/",
                      "http://[::1]/", "http://169.254.169.254/latest",
                      "file:///etc/passwd", "javascript:alert(1)",
                      "http://metadata.google.internal/"):
            with self.subTest(value=value), self.assertRaises(BrowserError):
                validate_url(value)
        for value in ("http://127.0.0.1:123", "socks5://user:pass@public.example:1080",
                      "http://proxy.example:abc", "http://proxy.example"):
            with self.subTest(value=value), self.assertRaises((BrowserError, ValueError)):
                validate_proxy(value)
        self.assertEqual(validate_url("about:blank", allow_blank=True), "about:blank")
        with self.assertRaises(BrowserError):
            validate_url("about:blank")

    def test_privacy_controls_validation(self):
        item = validate_profile({
            "name": "Private research", "privacy_mode": "strict",
            "do_not_track": "on", "geolocation_mode": "fixed",
            "geo_lat": "31.5", "geo_lon": "74.3",
            "color_scheme": "dark", "reduced_motion": "reduce",
            "device_scale": "1.5",
        })
        self.assertEqual(item["privacy_mode"], "strict")
        self.assertEqual(item["do_not_track"], 1)
        self.assertEqual(item["device_scale"], 1.5)
        self.assertEqual(item["geo_lat"], 31.5)
        for field, value in (("privacy_mode", "undetectable"),
                             ("do_not_track", "maybe"),
                             ("geolocation_mode", "auto"),
                             ("geo_lat", "100"), ("geo_lon", "181"),
                             ("device_scale", "4"),
                             ("color_scheme", "ultraviolet"),
                             ("reduced_motion", "unknown")):
            with self.subTest(field=field), self.assertRaises(BrowserError):
                validate_profile({"name": "Test", field: value})

    def test_invalid_fingerprint_parameters_denied(self):
        for field, value in (("timezone", "Nowhere/Unreal"), ("locale", "abcd"),
                             ("width", "99"), ("user_agent", "bad\nheader"),
                             ("proxy_password", "only-secret")):
            with self.subTest(field=field), self.assertRaises(BrowserError):
                validate_profile({"name": "One", field: value})


class ProfileTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.engine = BrowserProfileEngine(Path(self.temp.name))

    async def asyncTearDown(self):
        await self.engine.close_all()

    async def test_account_separation_and_redacted_secrets(self):
        a = await self.engine.create("alice", {"name": "First",
            "proxy_server": "http://proxy.example:8080",
            "proxy_username": "operator", "proxy_password": "top-secret"})
        b = await self.engine.create("bob", {"name": "Second"})
        self.assertEqual(a["state"], "stopped")
        self.assertNotIn("top-secret", str(a))
        self.assertEqual(len(await self.engine.list("alice")), 1)
        self.assertEqual(len(await self.engine.list("bob")), 1)
        self.assertNotEqual(a["id"], b["id"])
        with self.assertRaises(BrowserError) as denial:
            await self.engine.stop("bob", a["id"])
        self.assertEqual(denial.exception.status, 404)
        await self.engine.delete("alice", a["id"])
        self.assertEqual(await self.engine.list("alice"), [])
        self.assertEqual(len(await self.engine.list("bob")), 1)

    async def test_clone_isolated_without_cookies_or_proxy_credentials(self):
        original = await self.engine.create("alice", {
            "name": "Research", "proxy_server": "https://proxy.example.net:443",
            "proxy_username": "secret-user", "proxy_password": "private-password",
            "privacy_mode": "strict", "device_scale": "2"})
        copied = await self.engine.clone("alice", original["id"])
        self.assertNotEqual(original["id"], copied["id"])
        self.assertEqual(copied["device_scale"], 2)
        self.assertEqual(copied["privacy_mode"], "strict")
        self.assertEqual(list((self.engine.root / "profiles" / copied["id"]).iterdir()), [])
        source = self.engine._get("alice", original["id"])
        cloned = self.engine._get("alice", copied["id"])
        self.assertEqual(source["proxy_password"], "private-password")
        self.assertEqual(cloned["proxy_password"], "")
        self.assertEqual(cloned["proxy_username"], "")
        self.assertNotIn("private-password", str(copied))
        with self.assertRaises(BrowserError):
            await self.engine.clone("bob", original["id"])

    async def test_legacy_database_migrates_without_removing_profiles(self):
        with self.engine._db() as db:
            db.execute("INSERT INTO profiles (id,owner,name,start_url,proxy_server,"
                       "proxy_username,proxy_password,locale,timezone,user_agent,"
                       "width,height,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       ("1" * 24, "alice", "Legacy", "about:blank", "", "", "",
                        "en-US", "UTC", "", 1280, 800, "1"))
            db.commit()
        found = await self.engine.list("alice")
        self.assertEqual(found[0]["privacy_mode"], "standard")
        self.assertEqual(found[0]["state"], "stopped")

    async def test_rejects_start_when_chrome_missing_and_preserves_profile(self):
        item = await self.engine.create("alice", {"name": "Test"})
        with patch("experiment_browser.browser_executable", return_value=None):
            with self.assertRaises(BrowserError) as error:
                await self.engine.start("alice", item["id"])
        self.assertEqual(error.exception.status, 503)
        self.assertEqual((await self.engine.list("alice"))[0]["state"], "stopped")

    async def test_stop_and_delete_are_owner_scoped_and_idempotent(self):
        item = await self.engine.create("alice", {"name": "Test"})
        result = await self.engine.stop("alice", item["id"])
        self.assertEqual(result["state"], "stopped")
        await self.engine.delete("alice", item["id"])
        with self.assertRaises(BrowserError):
            await self.engine.delete("alice", item["id"])


if __name__ == "__main__":
    unittest.main()

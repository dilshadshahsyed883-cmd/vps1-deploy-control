"""Focused non-network unit tests for Experiment browser capacity and account boundaries."""
import sys
import tempfile
import unittest
from pathlib import Path

PART = Path(__file__).resolve().parents[1] / "apps/scene-builder/part01"
sys.path.insert(0, str(PART))
from experiment_browser import MAX_ACTIVE, BrowserError
from experiment_browser_management import ManagedBrowserProfileEngine


class ScopedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.engine = ManagedBrowserProfileEngine(Path(tmp.name))

    async def asyncTearDown(self):
        await self.engine.close_all()

    async def test_capacity_contract(self):
        self.assertEqual(MAX_ACTIVE, 10)

    async def test_proxy_owner_and_secret_redaction(self):
        saved = await self.engine.save_proxy("alice", {
            "name": "East", "pool": "Research",
            "proxy_server": "http://example.org:8080",
            "proxy_username": "agent", "proxy_password": "secret-value"})
        self.assertTrue(saved["credentials_configured"])
        self.assertNotIn("secret-value", str(saved))
        self.assertEqual(await self.engine.proxies("bob"), [])
        profile = await self.engine.create("alice", {
            "name": "Browser", "managed_proxy_id": saved["id"]})
        self.assertEqual(profile["managed_proxy_id"], saved["id"])
        with self.assertRaises(BrowserError):
            await self.engine.create("bob", {
                "name": "Unknown", "managed_proxy_id": saved["id"]})
        with self.assertRaises(BrowserError):
            await self.engine.delete_proxy("alice", saved["id"])
        await self.engine.update("alice", profile["id"], {"managed_proxy_id": ""})
        await self.engine.delete_proxy("alice", saved["id"])
        self.assertEqual(await self.engine.proxies("alice"), [])


if __name__ == "__main__":
    unittest.main()

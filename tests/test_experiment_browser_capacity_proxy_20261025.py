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

    async def test_ten_running_sessions_refuse_eleventh(self):
        import time

        class Page:
            url = "about:blank"

            def is_closed(self):
                return False

        class Context:
            async def close(self):
                pass

        profiles = [await self.engine.create("alice", {"name": f"Profile {n}"})
                    for n in range(11)]
        for entry in profiles[:10]:
            self.engine.running[entry["id"]] = {
                "owner": "alice", "used": time.monotonic(),
                "page": Page(), "context": Context()}
        with self.assertRaises(BrowserError) as result:
            await self.engine.start("alice", profiles[10]["id"])
        self.assertEqual(result.exception.status, 409)
        self.assertEqual(len(self.engine.running), 10)
        self.assertTrue((self.engine.root / "profiles" / profiles[10]["id"]).is_dir())

    async def test_memory_admission_preserves_profile(self):
        from unittest.mock import patch
        profile = await self.engine.create("alice", {"name": "Memory Guard"})
        with patch.object(self.engine, "available_memory_mb", return_value=12):
            with self.assertRaises(BrowserError) as result:
                await self.engine.start("alice", profile["id"])
        self.assertEqual(result.exception.status, 503)
        self.assertTrue((self.engine.root / "profiles" / profile["id"]).is_dir())

    async def test_profile_crash_status(self):
        import time
        from unittest.mock import AsyncMock

        class DeadPage:
            def is_closed(self):
                return True

        class DeadContext:
            async def close(self):
                pass

        profile = await self.engine.create("alice", {"name": "Recoverable"})
        self.engine.running[profile["id"]] = {
            "owner": "alice", "used": time.monotonic(),
            "page": DeadPage(), "context": DeadContext()}
        self.engine._recover = AsyncMock()
        found = await self.engine.list("alice")
        self.assertEqual(found[0]["state"], "crashed")
        await __import__("asyncio").sleep(0)
        self.engine._recover.assert_awaited_once()
        self.assertTrue((self.engine.root / "profiles" / profile["id"]).exists())

    async def test_proxy_pool_no_direct_fallback(self):
        from unittest.mock import patch
        from experiment_browser import BrowserProfileEngine

        a = await self.engine.save_proxy("alice", {
            "name": "First", "pool": "Primary",
            "proxy_server": "http://first.example.org:8080"})
        await self.engine.save_proxy("alice", {
            "name": "Backup", "pool": "Primary",
            "proxy_server": "http://backup.example.org:8080"})
        profile = await self.engine.create("alice", {
            "name": "Managed", "managed_proxy_id": a["id"]})
        used = []

        async def launch(instance, owner, identity):
            server = instance._launch_proxy[identity]["proxy_server"]
            used.append(server)
            if "first" in server:
                raise BrowserError("Proxy not reachable", 502)
            return {"ok": True}

        async def probe(instance, owner, identity):
            return {"ip": "8.8.8.8"}

        with patch.object(BrowserProfileEngine, "start", launch), \
             patch.object(BrowserProfileEngine, "connection_check", probe):
            await self.engine.start("alice", profile["id"])
        self.assertEqual(used, ["http://first.example.org:8080",
                                "http://backup.example.org:8080"])


if __name__ == "__main__":
    unittest.main()

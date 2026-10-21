"""EXP-UX-20261021-02: offline migration, tenancy, isolation and management."""
import asyncio
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

PART = Path(__file__).resolve().parents[1] / "apps/scene-builder/part01"
sys.path.insert(0, str(PART))
from experiment_browser import BrowserError
from experiment_browser_management import ManagedBrowserProfileEngine


class ManagementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.engine = ManagedBrowserProfileEngine(Path(tmp.name))

    async def asyncTearDown(self):
        await self.engine.close_all()

    async def test_legacy_migration_retains_data_credentials_and_storage(self):
        with self.engine._db() as db:
            db.execute("INSERT INTO profiles (id,owner,name,start_url,proxy_server,"
                       "proxy_username,proxy_password,locale,timezone,user_agent,"
                       "width,height,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       ("a" * 24, "alice", "Old", "about:blank",
                        "http://proxy.example:8080", "login", "secret", "en-US", "UTC",
                        "", 1280, 800, "1"))
            db.commit()
        folder = self.engine.root / "profiles" / ("a" * 24)
        (folder / "Cookies").write_text("persist")
        another = ManagedBrowserProfileEngine(self.engine.root)
        info = await another.list("alice")
        self.assertEqual(len(info), 1)
        self.assertEqual(info[0]["name"], "Old")
        self.assertEqual(info[0]["group_id"], "")
        self.assertEqual(info[0]["tags"], "")
        self.assertEqual((folder / "Cookies").read_text(), "persist")
        self.assertEqual(another._get("alice", "a" * 24)["proxy_password"], "secret")
        self.assertNotIn("secret", json.dumps(info))

    async def test_groups_defaults_edits_and_cross_owner_access(self):
        group = await self.engine.save_group("alice", {
            "name": "Research", "proxy_server": "http://proxy.example:8000"})
        profile = await self.engine.create("alice", {
            "name": "Alpha", "group_id": group["id"], "tags": "demo, test"})
        self.assertEqual(profile["proxy_server"], "http://proxy.example:8000")
        self.assertEqual(profile["group_id"], group["id"])
        self.assertEqual(profile["tags"], "demo,test")
        self.assertEqual((await self.engine.groups("alice"))[0]["profile_count"], 1)
        with self.assertRaises(BrowserError):
            await self.engine.update("bob", profile["id"], {"name": "Steal"})
        with self.assertRaises(BrowserError):
            await self.engine.create("bob", {"name": "Wrong group", "group_id":group["id"]})
        edited = await self.engine.update("alice", profile["id"], {"name": "New", "tags": "one, two"})
        self.assertEqual(edited["name"], "New")
        self.assertEqual(edited["tags"], "one,two")
        await self.engine.delete_group("alice", group["id"])
        self.assertEqual((await self.engine.list("alice"))[0]["group_id"], "")
        self.assertTrue((self.engine.root / "profiles" / profile["id"]).exists())

    async def test_preserve_proxy_credentials_on_profile_update(self):
        profile = await self.engine.create("alice", {
            "name":"Secret","proxy_server":"http://proxy.example.net:3128",
            "proxy_username":"admin","proxy_password":"private"})
        await self.engine.update("alice",profile["id"],{"name":"Edited"})
        saved = self.engine._get("alice",profile["id"])
        self.assertEqual(saved["proxy_password"],"private")
        self.assertNotIn("private",str(await self.engine.list("alice")))
        with self.assertRaises(BrowserError) as duplicate:
            await self.engine.create("alice",{"name":"edited"})
        self.assertEqual(duplicate.exception.status,409)

    async def test_templates_do_not_save_secrets(self):
        tpl = await self.engine.save_template("alice",{"name":"Default","settings":{
            "name":"Ignored","locale":"en-US","timezone":"UTC",
            "proxy_server":"http://proxy.example.com:8080",
            "proxy_username":"user","proxy_password":"password"}})
        stored = (await self.engine.templates("alice"))[0]
        self.assertEqual(stored["id"],tpl["id"])
        self.assertNotIn("password",json.dumps(stored))
        self.assertNotIn("proxy_username",stored["settings"])
        with self.assertRaises(BrowserError):
            await self.engine.delete_template("bob",tpl["id"])
        await self.engine.delete_template("alice",tpl["id"])
        self.assertEqual(await self.engine.templates("alice"),[])

    async def test_batch_one_hundred_isolated_directories(self):
        result = await self.engine.batch_create("alice",{"count":100,"prefix":"Batch"})
        self.assertEqual(result["created_count"],100)
        self.assertEqual(result["failed_count"],0)
        identities={p["id"] for p in result["created"]}
        self.assertEqual(len(identities),100)
        for identity in identities:
            self.assertEqual(list((self.engine.root / "profiles" / identity).iterdir()), [])
        self.assertEqual(len(await self.engine.list("alice")),100)

    async def test_batch_partial_validation_and_duplicate_names(self):
        result = await self.engine.batch_create("alice",{"count":4,"csv_rows":[
            {"name":"alpha"},{"name":"alpha"},{"name":"beta","proxy_server":"http://127.0.0.1:8080"},
            {"name":"gamma"}]})
        self.assertEqual(result["created_count"],2)
        self.assertEqual(result["failed_count"],2)
        self.assertEqual({p["name"] for p in result["created"]},{"alpha","gamma"})
        self.assertEqual(len(await self.engine.list("alice")),2)
        with self.assertRaises(BrowserError):
            await self.engine.batch_create("alice",{"count":101})
        with self.assertRaises(BrowserError):
            await self.engine.batch_create("alice",{"count":3,"csv_rows":[{"name":"a"}]})

    async def test_bulk_move_and_delete_require_valid_owner_stopped(self):
        one = await self.engine.create("alice",{"name":"One"})
        two = await self.engine.create("alice",{"name":"Two"})
        group = await self.engine.save_group("alice",{"name":"Folder"})
        ids = [one["id"],two["id"]]
        await self.engine.bulk("alice",{"action":"move","profile_ids":ids,"group_id":group["id"]})
        self.assertTrue(all(x["group_id"] == group["id"] for x in await self.engine.list("alice")))
        with self.assertRaises(BrowserError):
            await self.engine.bulk("bob",{"action":"delete","profile_ids":ids})
        self.engine.running[one["id"]] = {"owner":"alice","used":0}
        with self.assertRaises(BrowserError):
            await self.engine.bulk("alice",{"action":"delete","profile_ids":ids})
        self.engine.running.pop(one["id"])
        result = await self.engine.bulk("alice",{"action":"delete","profile_ids":ids})
        self.assertEqual(result["updated"],2)
        self.assertEqual(await self.engine.list("alice"),[])
        for identity in ids:
            self.assertFalse((self.engine.root / "profiles" / identity).exists())


class RouteAndUiContracts(unittest.TestCase):
    def test_routes_and_assets_app_scoped(self):
        app = (PART / "app.py").read_text()
        html = (PART / "templates/experiment_studio.html").read_text()
        js = (PART / "static/experiment_browser_management.js").read_text()
        css = (PART / "static/experiment_browser_management.css").read_text()
        for fragment in ('"/api/experiment-browser/groups"',
                         '"/api/experiment-browser/templates"',
                         '"/api/experiment-browser/batches"',
                         '"/api/experiment-browser/profiles/bulk"',
                         '"PATCH"', '"DELETE"'):
            if fragment in ('"PATCH"', '"DELETE"'):
                continue
            self.assertIn(fragment,app)
        for tab in ("profiles","batch","groups","sessions"):
            self.assertIn('data-exp-tab="'+tab+'"',html)
        for field in ("exp-batch-count","exp-csv-file","exp-edit-form",
                      "exp-bulk-move","exp-bulk-delete","exp-group-form","exp-template-form",
                      "browser-preview","exp-dns-check","exp-webrtc-check"):
            self.assertIn('id="'+field+'"',html)
        self.assertIn('window.antidetectManagementBridge', (PART / "static/experiment_browser.js").read_text())
        self.assertIn("https://browserleaks.com/dns",js)
        self.assertIn("https://browserleaks.com/webrtc",js)
        self.assertNotIn("vps1_native_site_shell",css)
        self.assertNotIn("#app-sidebar",css)


if __name__ == "__main__":
    unittest.main()

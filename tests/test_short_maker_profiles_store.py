"""Short Profiles staged persistence safety, ownership, and PKT idempotency tests."""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

MODULE = Path(__file__).resolve().parents[1] / "apps/short-maker/part01/short_profiles.py"


class ShortProfileStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        spec = importlib.util.spec_from_file_location("short_profiles_test_module", MODULE)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.app = FastAPI()
        self.folders = {"alice": {"Own Output"}, "bob": {"Bob Output"}}

        def short_identity(request: Request):
            user = request.headers.get("x-auth-user")
            if not user:
                raise HTTPException(401, detail="Login required")
            return user, request.headers.get("x-is-admin") == "1"

        def owned_folder(name, request):
            owner, _ = short_identity(request)
            if name not in self.folders.get(owner.casefold(), set()):
                raise HTTPException(403, detail="Foreign or nonexistent folder")
            return Path(self.temp.name) / name

        def verified_import(owner, receipt):
            if receipt == self.alice_import and owner == "alice":
                return {"audio_count": 4, "image_count": 10}
            if receipt == self.bob_import and owner == "bob":
                return {"audio_count": 2, "image_count": 6}
            return None

        self.alice_import = "a" * 32
        self.bob_import = "b" * 32
        self.store = self.module.register_short_profile_store(
            self.app, Path(self.temp.name) / "data", short_identity,
            owned_folder, verified_import,
        )
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.alice = {"x-auth-user": "Alice"}
        self.bob = {"x-auth-user": "bob", "x-is-admin": "1"}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def profile(self, name="پاکستانی شارٹس", **kwargs):
        return {
            "name": name, "source_import_id": self.alice_import,
            "output_folder": "Own Output", "videos_per_day": 5000,
            "schedule_hhmm": "00:00", **kwargs,
        }

    def create(self, **kwargs):
        return self.client.post("/api/short-profiles", json=self.profile(**kwargs),
                                headers=self.alice)

    def test_verified_source_required_and_no_fake_scheduler(self):
        unauth = self.client.get("/api/short-profiles")
        self.assertEqual(unauth.status_code, 401)
        no_media = self.client.post("/api/short-profiles", headers=self.alice,
                                    json=self.profile(source_import_id="f" * 32))
        self.assertEqual(no_media.status_code, 409)
        created = self.create()
        self.assertEqual(created.status_code, 200, created.text)
        row = created.json()
        self.assertEqual(row["name"], "پاکستانی شارٹس")
        self.assertEqual(row["videos_per_day"], 5000)  # no arbitrary cap
        self.assertEqual(row["enabled"], 0)
        unavailable = self.client.post(
            f"/api/short-profiles/{row['id']}/start", headers=self.alice)
        self.assertEqual(unavailable.status_code, 503)
        self.assertEqual(self.client.get("/api/short-profiles",
                         headers=self.alice).json()["scheduler_active"], False)

    def test_strict_ownership_even_for_admin_and_duplicate_names(self):
        created = self.create()
        self.assertEqual(created.status_code, 200, created.text)
        pid = created.json()["id"]
        self.assertEqual(self.create(name=" پاکستانی شارٹس ").status_code, 409)
        self.assertEqual(self.client.patch(f"/api/short-profiles/{pid}",
                         headers=self.bob, json={"name": "stolen"}).status_code, 404)
        self.assertEqual(self.client.get(f"/api/short-profiles/{pid}/history",
                         headers=self.bob).status_code, 404)
        self.assertEqual(self.client.post(f"/api/short-profiles/{pid}/stop",
                         headers=self.bob).status_code, 404)
        self.assertEqual(self.client.delete(f"/api/short-profiles/{pid}",
                         headers=self.bob,
                         json={"confirm_name":"پاکستانی شارٹس"}).status_code, 404)
        self.assertEqual(self.client.get("/api/short-profiles",
                         headers=self.bob).json()["profiles"], [])
        self.assertEqual(self.create(name="not alice",
                         output_folder="Bob Output").status_code, 403)

    def test_edit_keeps_identity_history_and_soft_delete_preserves_media(self):
        pid = self.create().json()["id"]
        edit = self.client.patch(f"/api/short-profiles/{pid}", headers=self.alice,
                                 json={"name":"Custom Name", "videos_per_day":7})
        self.assertEqual(edit.status_code, 200, edit.text)
        self.assertEqual(edit.json()["source_import_id"], self.alice_import)
        self.assertEqual(edit.json()["videos_per_day"], 7)
        self.assertEqual(self.client.delete(f"/api/short-profiles/{pid}",
                          headers=self.alice,
                          json={"confirm_name":"Wrong"}).status_code, 409)
        deleted = self.client.delete(f"/api/short-profiles/{pid}",
                          headers=self.alice,
                          json={"confirm_name":"Custom Name"})
        self.assertTrue(deleted.json()["media_preserved"])
        self.assertEqual(self.client.get("/api/short-profiles",
                         headers=self.alice).json()["profiles"], [])
        self.assertEqual(self.client.get(f"/api/short-profiles/{pid}/history",
                         headers=self.alice).json()["history"], [])
        self.assertEqual(self.client.patch(f"/api/short-profiles/{pid}",
                         headers=self.alice,json={"name":"oops"}).status_code, 404)
        self.assertEqual(self.create(name="Custom Name").status_code, 200)

    def test_daily_record_uses_pkt_day_and_never_duplicate(self):
        pid = self.create(name="Daily").json()["id"]
        with self.store._connect() as db:
            db.execute("UPDATE short_profiles SET enabled=1 WHERE id=?", (pid,))
        before = datetime(2026, 11, 1, 18, 59, tzinfo=timezone.utc)
        after = datetime(2026, 11, 1, 19, 1, tzinfo=timezone.utc)
        first = self.store.reserve_daily_record("alice", pid, before)
        second = self.store.reserve_daily_record("alice", pid, before)
        third = self.store.reserve_daily_record("alice", pid, after)
        self.assertTrue(first["new"])
        self.assertFalse(second["new"])
        self.assertEqual(first["run"]["id"], second["run"]["id"])
        self.assertEqual(first["run"]["pkt_date"], "2026-11-01")
        self.assertEqual(third["run"]["pkt_date"], "2026-11-02")
        self.assertEqual(len(self.store.history("alice", pid)), 2)
        self.assertEqual(first["run"]["target"], 5000)
        with self.assertRaises(HTTPException) as error:
            self.store.reserve_daily_record("bob", pid, after)
        self.assertEqual(error.exception.status_code, 404)

    def test_invalid_inputs_fail_closed(self):
        self.assertEqual(self.create(videos_per_day=True).status_code, 400)
        self.assertEqual(self.create(videos_per_day=0).status_code, 400)
        self.assertEqual(self.create(schedule_hhmm="25:00").status_code, 400)
        self.assertEqual(self.create(name="a\nb").status_code, 400)
        self.assertEqual(self.create(source_import_id="pc-folder-path").status_code, 400)
        self.assertEqual(self.create(name="a" * 121).status_code, 400)


if __name__ == "__main__":
    unittest.main()

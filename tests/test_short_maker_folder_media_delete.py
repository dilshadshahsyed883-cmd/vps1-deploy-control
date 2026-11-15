"""Short Maker owned VPS folder media management: explicit warning, no retirement gates."""
from __future__ import annotations

import ast
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "apps/short-maker/part01/app.py"


class ShortMakerFolderMediaDeleteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        with patch.dict(os.environ, {
            "SHORT_MAKER_DATA": str(root / "data"),
            "SHORT_STUDIO_VPS_FOLDER_ROOT": str(root / "folders"),
            "SHORT_MAKER_UNUSED_FOLDER_DELETE_ENABLED": "0",
            "SHORT_MAKER_COOPERATIVE_LOCK_ENABLED": "0",
            "SHORT_MAKER_DAILY_PROFILES_ENABLED": "0",
        }):
            spec = importlib.util.spec_from_file_location("folder_media_" + str(id(self)), SOURCE)
            self.maker = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.maker)
        self.client = TestClient(self.maker.app)
        self.client.__enter__()
        self.owner = {"x-auth-user": "alice"}
        self.other = {"x-auth-user": "bob"}
        self.folder = self.maker.short_personal_default("alice")
        self.path = self.maker.short_vps_folder(self.folder)
        self.url = "/api/vps-folders/" + self.folder + "/media"

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.maker.executor.shutdown(wait=True)
        self.temp.cleanup()

    def test_lists_all_mp4s_and_deletes_selected_only_after_confirmation(self):
        (self.path / "legacy.mp4").write_bytes(b"a" * 4096)
        (self.path / "another.MP4").write_bytes(b"b" * 1024)
        (self.path / "keep.mp4").write_bytes(b"c" * 512)
        (self.path / "in-progress.mp4.part").write_bytes(b"partial")
        (self.path / "notes.txt").write_text("keep", encoding="utf-8")

        found = self.client.get(self.url, headers=self.owner)
        self.assertEqual(found.status_code, 200, found.text)
        self.assertEqual([f["name"] for f in found.json()["files"]],
                         ["another.MP4", "keep.mp4", "legacy.mp4"])
        payload = {"filenames": ["legacy.mp4", "another.MP4", "legacy.mp4"]}
        denied = self.client.post(self.url, headers=self.owner, json=payload)
        self.assertEqual(denied.status_code, 409)
        self.assertTrue((self.path / "legacy.mp4").exists())

        done = self.client.post(self.url, headers=self.owner,
                                json={**payload, "confirm_delete": True})
        self.assertEqual(done.status_code, 200, done.text)
        self.assertEqual(done.json()["deleted_count"], 2)
        self.assertFalse((self.path / "legacy.mp4").exists())
        self.assertFalse((self.path / "another.MP4").exists())
        self.assertTrue((self.path / "keep.mp4").is_file())
        self.assertTrue((self.path / "in-progress.mp4.part").is_file())
        self.assertTrue((self.path / "notes.txt").is_file())
        self.assertTrue((self.path / ".studio_owner").is_file())
        self.assertEqual([f["name"] for f in self.client.get(self.url, headers=self.owner).json()["files"]],
                         ["keep.mp4"])

    def test_owner_isolation_and_rejects_paths_or_non_mp4s(self):
        (self.path / "safe.mp4").write_bytes(b"safe")
        self.assertEqual(self.client.get(self.url, headers=self.other).status_code, 403)
        self.assertEqual(self.client.post(self.url, headers=self.other,
                                          json={"filenames": ["safe.mp4"], "confirm_delete": True}).status_code, 403)
        for filename in ("../safe.mp4", "dir\\safe.mp4", "safe.mp4/../safe.mp4", ".studio_owner"):
            with self.subTest(filename=filename):
                response = self.client.post(self.url, headers=self.owner,
                    json={"filenames": [filename], "confirm_delete": True})
                self.assertEqual(response.status_code, 400, response.text)
        self.assertTrue((self.path / "safe.mp4").is_file())

    def test_missing_or_symlink_is_not_deleted(self):
        (self.path / "real.mp4").write_bytes(b"real")
        (self.path / "alias.mp4").symlink_to(self.path / "real.mp4")
        found = self.client.get(self.url, headers=self.owner)
        self.assertEqual([f["name"] for f in found.json()["files"]], ["real.mp4"])
        refused = self.client.post(self.url, headers=self.owner,
            json={"filenames": ["real.mp4", "alias.mp4"], "confirm_delete": True})
        self.assertEqual(refused.status_code, 404)
        self.assertTrue((self.path / "real.mp4").is_file())
        missing = self.client.post(self.url, headers=self.owner,
            json={"filenames": ["missing.mp4"], "confirm_delete": True})
        self.assertEqual(missing.status_code, 404)

    def test_direct_folder_delete_single_warning_owner_only_without_dependency_gate(self):
        name = "Aina Asif"
        created = self.client.post("/api/vps-folders", headers=self.owner, json={"name": name})
        self.assertEqual(created.status_code, 200, created.text)
        folder = self.maker.short_vps_folder(name)
        (folder / "rendered.mp4").write_bytes(b"video")
        nested = folder / "nested"
        nested.mkdir()
        (nested / "notes.txt").write_text("media", encoding="utf-8")
        # An owned profile/job may still reference this folder: the explicit
        # user deletion is not blocked, and the job record is not touched.
        job_id = "b" * 16
        self.maker.set_state(job_id, owner="alice", status="running",
                             config={"short_vps_folder": name},
                             published_outputs=["rendered.mp4"])
        url = "/api/vps-folders/" + name
        unconfirmed = self.client.request("DELETE", url, headers=self.owner,
                                          json={"confirm_delete": False})
        self.assertEqual(unconfirmed.status_code, 409, unconfirmed.text)
        self.assertTrue(folder.exists())
        foreign = self.client.request("DELETE", url, headers=self.other,
            json={"confirm_delete": True})
        self.assertEqual(foreign.status_code, 403, foreign.text)
        self.assertTrue(folder.exists())
        # Even when the old unused-folder feature flag is active, the
        # explicitly confirmed direct deletion is independent of that gate.
        self.maker._sm_unused_retirement_enabled = True
        deleted = self.client.request("DELETE", url, headers=self.owner,
                                     json={"confirm_delete": True})
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertFalse(folder.exists())
        self.assertEqual(self.maker.get_state(job_id)["status"], "running")

    def test_direct_folder_delete_keeps_default_and_external_symlink_target(self):
        own_default = "/api/vps-folders/" + self.folder
        denied = self.client.request("DELETE", own_default, headers=self.owner,
                                     json={"confirm_delete": True})
        self.assertEqual(denied.status_code, 403, denied.text)
        name = "My removable clips"
        self.assertEqual(self.client.post("/api/vps-folders", headers=self.owner,
                         json={"name": name}).status_code, 200)
        folder = self.maker.short_vps_folder(name)
        (folder / "link").symlink_to(self.path, target_is_directory=True)
        (self.path / "keep.mp4").write_bytes(b"external")
        deleted = self.client.request("DELETE", "/api/vps-folders/" + name,
                                     headers=self.owner, json={"confirm_delete": True})
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertFalse(folder.exists())
        self.assertTrue((self.path / "keep.mp4").is_file())

    def test_legacy_delete_url_remains_compatible(self):
        (self.path / "old-client.mp4").write_bytes(b"old")
        response = self.client.post(self.url + "/delete", headers=self.owner,
                                    json={"filenames": ["old-client.mp4"],
                                          "confirm_delete": True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse((self.path / "old-client.mp4").exists())

    def test_active_maker_job_does_not_block_explicit_media_deletion(self):
        (self.path / "rendered.mp4").write_bytes(b"output")
        self.maker.set_state("a" * 16, owner="alice", status="running",
                             config={"short_vps_folder": self.folder},
                             published_outputs=["rendered.mp4"])
        deleted = self.client.post(self.url, headers=self.owner,
            json={"filenames": ["rendered.mp4"], "confirm_delete": True})
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertFalse((self.path / "rendered.mp4").exists())


class ShortMakerFolderMediaUiTests(unittest.TestCase):
    def test_manage_media_select_all_single_warning_and_refresh(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        html = next(node.value.value for node in tree.body if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == "HTML"
                            for target in node.targets))
        for fragment in ("Manage media", "Select all (", "Delete selected (",
                         "WARNING: Permanently delete", "confirm_delete:true",
                         "/media',{cache:'no-store'}", "method:'POST'",
                         "inventory.append(row,mediaRow)", "await loadOwnerFolderNames();"):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, html)
        self.assertIn("deleteFolder.textContent='Delete Folder'", html)
        self.assertNotIn("Review dependencies", html)
        self.assertNotIn("Linked Profiles</th>", html)


if __name__ == "__main__":
    unittest.main()

"""Safety regression tests for VPS1 12-hour deployment artifact cleanup."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from production import storage_gc as gc


class StorageGCTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name) / "fresh-website"
        self.unit_dir = Path(self.workspace.name) / "units"
        self.unit_dir.mkdir()
        self.now = time.time()
        self.app = "short-maker"
        for name in ("releases", "venvs", "current", "control/state"):
            (self.root / name).mkdir(parents=True)
        (self.root / "releases" / self.app).mkdir()
        (self.root / "venvs" / self.app).mkdir()
        self.revisions = [("%040x" % i) for i in (1, 2, 3, 4)]
        for rev in self.revisions:
            rel = self.root / "releases" / self.app / rev
            env = self.root / "venvs" / self.app / rev
            rel.mkdir()
            env.mkdir()
            (rel / "source.py").write_bytes(b"release" * 100)
            (env / "interpreter").write_bytes(b"venv" * 100)
            self.set_old(rel)
            self.set_old(env)
        self.current = self.root / "current" / self.app
        self.current.symlink_to(self.root / "releases" / self.app / self.revisions[0])
        self.set_old(self.current, symlink=True)
        self.manifest = self.root / "control/state" / (self.app + ".json")
        self.manifest.write_text(json.dumps({
            "status": "ACTIVE", "revision": self.revisions[0],
            "previous": {
                "current": str(self.root / "releases" / self.app / self.revisions[1]),
                "active": True,
            },
        }))
        # Ensure no other app has an unknown unit/reference.
        (self.unit_dir / "vps1-production-short-maker.service").write_text(
            "[Service]\nExecStart=/usr/bin/true\n"
        )

    def set_old(self, path: Path, symlink=False):
        stamp = self.now - 26 * 3600
        os.utime(path, (stamp, stamp), follow_symlinks=not symlink)

    def inventory(self):
        return gc.inventory(self.root, now=self.now, unit_dir=self.unit_dir)

    def execute(self):
        return gc.execute(root=self.root, apply=True, now=self.now,
                          unit_dir=self.unit_dir, test_mode=True)

    def test_dry_run_is_non_destructive_and_keeps_rollback(self):
        plan = self.inventory()
        self.assertFalse(plan.get("blocked"))
        self.assertEqual([(x["app"], x["revision"]) for x in plan["candidates"]],
                         [(self.app, self.revisions[3])])
        self.assertGreater(plan["estimated_bytes"], 0)
        for rev in self.revisions:
            self.assertTrue((self.root / "releases" / self.app / rev).exists())

    def test_guarded_apply_deletes_only_superseded_release_and_venv(self):
        outcome = self.execute()
        self.assertEqual(outcome["outcome"]["deleted"], [self.app + "/" + self.revisions[3]])
        self.assertFalse((self.root / "releases" / self.app / self.revisions[3]).exists())
        self.assertFalse((self.root / "venvs" / self.app / self.revisions[3]).exists())
        for rev in self.revisions[:3]:
            self.assertTrue((self.root / "releases" / self.app / rev).exists())
            self.assertTrue((self.root / "venvs" / self.app / rev).exists())

    def test_recent_promotion_blocks_all_deletions(self):
        os.utime(self.current, (self.now, self.now), follow_symlinks=False)
        self.assertEqual(self.inventory()["candidates"], [])

    def test_unverified_pointer_blocks_all_deletions(self):
        self.manifest.write_text(json.dumps({"status": "ACTIVE", "revision": "wrong"}))
        self.assertEqual(self.inventory()["candidates"], [])

    def test_missing_active_manifest_blocks(self):
        self.manifest.unlink()
        self.assertEqual(self.inventory()["candidates"], [])

    def test_symlink_candidate_refused(self):
        bad = self.root / "releases" / self.app / self.revisions[3]
        import shutil
        shutil.rmtree(bad)
        bad.symlink_to(self.root / "control")
        self.assertEqual(self.inventory()["candidates"], [])

    def test_systemd_reference_preserved(self):
        unit = self.unit_dir / "vps1-production-short-maker.service"
        unit.write_text("Environment=PATH=" + str(
            self.root / "venvs" / self.app / self.revisions[3]) + "/bin\n")
        self.assertEqual(self.inventory()["candidates"], [])

    def test_young_venv_cannot_be_removed(self):
        path = self.root / "venvs" / self.app / self.revisions[3]
        os.utime(path, (self.now, self.now))
        self.assertEqual(self.inventory()["candidates"], [])

    def test_unknown_unit_symlink_fails_closed(self):
        (self.unit_dir / "vps1-unexpected.service").symlink_to("/does-not-exist")
        self.assertEqual(self.inventory()["candidates"], [])

    def test_never_descends_into_user_data(self):
        media = self.root / "user-media"
        media.mkdir()
        (media / "original.mp4").write_bytes(b"never delete")
        self.execute()
        self.assertEqual((media / "original.mp4").read_bytes(), b"never delete")

    def test_explicit_apply_refuses_nonproduction_path(self):
        with self.assertRaises(RuntimeError):
            gc.execute(root=self.root, apply=True, unit_dir=self.unit_dir)


if __name__ == "__main__":
    unittest.main()

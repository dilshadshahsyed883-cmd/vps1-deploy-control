"""Offline transactional tests for the Scene Auto Uploader deploy carrier."""
from __future__ import annotations
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from production import scene_uploader_ui_release as ui

OLD_HTML = "<iframe src='/LUM/app'></iframe>"
NEW_HTML = "<iframe src='/local-upload-manager/app?auto_mode=engine1'></iframe>"
OLD_JS = "const old='/LUM/app';"
NEW_JS = "const next='/local-upload-manager/app?auto_mode=engine2';"


class SceneUploaderReleaseTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="scene-uploader-unit-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.releases = self.base / "releases/scene-builder"
        self.old = self.releases / "existing"
        self.source = self.base / "source"
        self.current = self.base / "current/scene-builder"
        self.transactions = self.base / "transactions"
        self.sha = "a" * 40
        for directory in (self.old / "part01", self.source):
            (directory / "templates").mkdir(parents=True)
            (directory / "static").mkdir()
        for base, html, js in ((self.old / "part01", OLD_HTML, OLD_JS),
                               (self.source, NEW_HTML, NEW_JS)):
            (base / ui.FILES[0]).write_text(html)
            (base / ui.FILES[1]).write_text(js)
        self.current.parent.mkdir(parents=True)
        self.current.symlink_to(self.old, target_is_directory=True)
        self.patches = [
            patch.object(ui, "CURRENT", self.current),
            patch.object(ui, "RELEASES", self.releases),
            patch.object(ui, "SOURCE", self.source),
            patch.object(ui, "TRANSACTIONS", self.transactions),
            patch.object(ui, "active", return_value=True),
            patch.object(ui, "healthy", return_value=True),
            patch.object(ui, "restart", return_value=None),
            patch.object(ui.os, "geteuid", return_value=0),
            patch.object(ui.socket, "gethostname", return_value="vps-01a23801"),
            patch.object(ui.shutil, "disk_usage", return_value=type("Disk", (), {"free": 10_000_000_000})()),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_patch_exact_two_source_files_and_preserve_old_release(self):
        with patch.object(ui, "publish_route") as publish:
            ui.activate(self.sha)
        publish.assert_called_once_with(self.sha)
        new = self.current.resolve()
        self.assertNotEqual(new, self.old)
        self.assertEqual((new / "part01" / ui.FILES[0]).read_text(), NEW_HTML)
        self.assertEqual((new / "part01" / ui.FILES[1]).read_text(), NEW_JS)
        self.assertEqual((self.old / "part01" / ui.FILES[0]).read_text(), OLD_HTML)
        saved = json.loads((self.transactions / self.sha / "release.json").read_text())
        self.assertEqual(saved["status"], "ACTIVE")
        self.assertEqual(saved["modified_files"], list(ui.FILES))

    def test_failed_website_publication_restores_old_pointer(self):
        with patch.object(ui, "publish_route", side_effect=RuntimeError("HTTPS verify failed")):
            with self.assertRaisesRegex(RuntimeError, "HTTPS verify failed"):
                ui.activate(self.sha)
        self.assertEqual(self.current.resolve(), self.old)
        self.assertEqual((self.old / "part01" / ui.FILES[0]).read_text(), OLD_HTML)

    def test_idempotent_when_ui_already_current(self):
        for rel in ui.FILES:
            (self.old / "part01" / rel).write_bytes((self.source / rel).read_bytes())
        with patch.object(ui, "publish_route") as publish:
            ui.activate(self.sha)
        publish.assert_called_once_with(self.sha)
        self.assertFalse((self.transactions / self.sha).exists())
        self.assertEqual(self.current.resolve(), self.old)

    def test_invalid_or_untrusted_code_never_changes_release(self):
        (self.source / ui.FILES[0]).write_text(OLD_HTML)
        with patch.object(ui, "publish_route") as publish:
            with self.assertRaisesRegex(RuntimeError, "legacy LUM"):
                ui.activate(self.sha)
        publish.assert_not_called()
        self.assertEqual(self.current.resolve(), self.old)

    def test_github_trigger_is_single_app_not_all(self):
        workflow = (Path(__file__).parent.parent /
                    ".github/workflows/vps1-production.yml").read_text()
        self.assertIn("[vps1-production-release:local-upload-manager]", workflow)
        self.assertIn("&& 'local-upload-manager' || 'all'", workflow)
        gateway = (Path(__file__).parent / "gateway.py").read_text()
        self.assertIn("state.get('revision') == args.revision", gateway)
        self.assertIn("from production.scene_uploader_ui_release import activate", gateway)


if __name__ == "__main__":
    unittest.main()

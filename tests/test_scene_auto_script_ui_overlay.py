"""UI-only Scene candidate overlay: protected two-file patch + rollback regression."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from production import scene_auto_script_ui_overlay as ui

ROOT = Path(__file__).resolve().parents[1]
WEB_BASE = ROOT / "apps/scene-builder/part01/templates/base.html"
UI_CSS = ROOT / "apps/scene-builder/part01/static/auto_script_kokoro_20261026.css"


class AutoScriptUIOnlyTests(unittest.TestCase):
    def test_base_patch_is_scoped_and_idempotent(self):
        existing = WEB_BASE.read_text(encoding="utf-8")
        self.assertEqual(existing.count(ui.LINK), 1)
        before = existing.replace(ui.LINK, "")
        output = ui.patch_base(before)
        self.assertEqual(output, existing)
        self.assertEqual(ui.patch_base(output), output)
        self.assertEqual(output.count(ui.LINK), 1)
        self.assertLess(output.index(ui.ANCHOR), output.index(ui.LINK))
        with self.assertRaisesRegex(RuntimeError, "template content"):
            ui.patch_base(before.replace(ui.ANCHOR, ""))
        with self.assertRaisesRegex(RuntimeError, "unrecognized"):
            ui.patch_base(before.replace("vps1-experiment-tabs", "unknown"))

    def fixture(self, directory):
        base = Path(directory)
        releases = base / "releases/scene-builder"
        prior = releases / ("b" * 40)
        (prior / "part01/templates").mkdir(parents=True)
        (prior / "part01/static").mkdir(parents=True)
        (prior / "part02/renderer").mkdir(parents=True)
        clean = WEB_BASE.read_text(encoding="utf-8").replace(ui.LINK, "")
        (prior / ui.BASE_PATH).write_text(clean, encoding="utf-8")
        (prior / "part02/renderer/engine.py").write_bytes(b"working renderer bytes")
        (prior / "part01/static/keep.js").write_bytes(b"existing JS unaffected")
        old_manifest = {"app":"scene-builder", "revision": "b"*40,
                        "files": {ui.BASE_PATH: hashlib.sha256(clean.encode()).hexdigest()}}
        (prior / "_scene_candidate_manifest.json").write_text(json.dumps(old_manifest))
        current = base / "current/scene-builder"
        current.parent.mkdir(parents=True)
        current.symlink_to(prior)
        css = base / "trusted_css"
        css.write_bytes(UI_CSS.read_bytes())
        return releases, prior, current, css

    def protected_installer(self, releases, current, css, tmp):
        return (
            patch.object(ui, "CURRENT", current),
            patch.object(ui, "RELEASES", releases),
            patch.object(ui, "TRANSACTIONS", Path(tmp) / "transactions"),
            patch.object(ui, "CSS_SOURCE", css),
            patch.object(ui.os, "geteuid", return_value=0),
            patch.object(ui.shutil, "disk_usage", return_value=types.SimpleNamespace(free=10_000_000_000)),
            patch.object(ui.subprocess, "run", return_value=types.SimpleNamespace(returncode=0)),
            patch.object(ui, "healthy", return_value=True),
            patch.object(ui, "verify_live", return_value=None),
        )

    def test_install_clones_web_only_leaves_worker_source_and_queue_untouched(self):
        from contextlib import ExitStack
        with tempfile.TemporaryDirectory() as tmp:
            releases, prior, current, css = self.fixture(tmp)
            with ExitStack() as stack:
                for ctx in self.protected_installer(releases, current, css, tmp):
                    stack.enter_context(ctx)
                restart = stack.enter_context(patch.object(ui, "restart_web"))
                ui.install("a" * 40)
                latest = current.resolve(strict=True)
                self.assertEqual(latest.name, "a"*40+"-auto-script-ui-only")
                self.assertEqual((latest/"part02/renderer/engine.py").read_bytes(),
                                 (prior/"part02/renderer/engine.py").read_bytes())
                self.assertEqual((latest/"part01/static/keep.js").read_bytes(),
                                 (prior/"part01/static/keep.js").read_bytes())
                self.assertEqual((prior/ui.BASE_PATH).read_text().count(ui.LINK), 0)
                self.assertEqual((latest/ui.BASE_PATH).read_text().count(ui.LINK), 1)
                self.assertEqual((latest/ui.CSS_PATH).read_bytes(), UI_CSS.read_bytes())
                manifest = json.loads((latest/"_scene_candidate_manifest.json").read_text())
                self.assertEqual(manifest["files"][ui.CSS_PATH],hashlib.sha256(UI_CSS.read_bytes()).hexdigest())
                self.assertEqual(restart.call_count,1)
                ui.install("a" * 40)
                self.assertEqual(restart.call_count,1)

    def test_web_restart_failure_rolls_back_pointer_and_preserves_renderer(self):
        from contextlib import ExitStack
        with tempfile.TemporaryDirectory() as tmp:
            releases, prior, current, css = self.fixture(tmp)
            with ExitStack() as stack:
                for ctx in self.protected_installer(releases, current, css, tmp):
                    stack.enter_context(ctx)
                stack.enter_context(patch.object(ui, "restart_web", side_effect=[
                    RuntimeError("web failed"), None]))
                with self.assertRaisesRegex(RuntimeError,"web failed"):
                    ui.install("c" * 40)
                self.assertEqual(current.resolve(), prior)
                self.assertEqual((prior/"part02/renderer/engine.py").read_bytes(),
                                 b"working renderer bytes")

    def test_runtime_dispatch_precedes_full_worker_refresh(self):
        runtime = (ROOT/"production/runtime.py").read_text()
        self.assertIn("if '[vps1-auto-script-ui-overlay]' in intent:",runtime)
        self.assertLess(runtime.index("if '[vps1-auto-script-ui-overlay]' in intent:"),
                        runtime.index("from production.scene_candidate_release import install as install_scene_candidate"))
        code=(ROOT/"production/scene_auto_script_ui_overlay.py").read_text()
        for disallowed in ("SceneWorkerSourceRefresh(", "worker_source_refresh.refresh(",
                           "production_worker.ensure(", "require_idle(", "subprocess.run(['rclone'"):
            self.assertNotIn(disallowed,code)
        self.assertIn("shutil.copytree(previous, target",code)


if __name__ == "__main__":
    unittest.main()

"""Short Studio copy/move contracts; entirely offline with no user state mutations."""
from __future__ import annotations

import ast
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from production import workers, short_studio_release

ROOT = Path(__file__).resolve().parents[1]
LUM = ROOT / "apps/local-upload-manager/part01"
SCENE = ROOT / "apps/scene-builder/part01"


class ShortStudioV1(unittest.TestCase):
    def test_existing_short_maker_moved_not_duplicated(self):
        maker = (ROOT / "apps/short-maker/part01/app.py").read_text()
        scene = (SCENE / "templates/base.html").read_text()
        merger = (ROOT / "apps/video-merger/part01/templates/index.html").read_text()
        shell = (SCENE / "static/vps1_native_site_shell.js").read_text()
        self.assertIn('href="/short-maker"', maker)
        self.assertIn('aria-label="Short Studio"', maker)
        self.assertIn('href="/short-uploader"', maker)
        self.assertIn('href="/short-maker"', scene)
        self.assertNotIn('href="/short-maker/"', scene)
        self.assertNotIn('href="/short-maker/"', merger)
        self.assertIn('["Short Studio", "/short-maker", "short"]', shell)
        self.assertIn('addLink(tabs, "Short Uploader", "/short-uploader", true)', shell)
        self.assertIn('APP_TITLE = "Short Maker"', maker)

    def test_independent_short_uploader_worker_storage_and_auth(self):
        specs = dict((k, cmd) for k, _part, cmd in workers.SPECS["local-upload-manager"])
        for name in ("lum-shortstudio-web", "lum-shortstudio-worker", "lum-shortstudio-scheduler"):
            self.assertIn(name, specs)
        self.assertEqual(specs["lum-shortstudio-worker"], specs["lum-shorts-worker"])
        self.assertEqual(specs["lum-shortstudio-scheduler"], specs["lum-shorts-scheduler"])
        self.assertEqual(workers.MODE_ROOTS["shortstudio"], "lum-shortstudio")
        with patch.object(workers, "DATA", Path("/tmp/test-short-studio")):
            cmd = workers.mode_exec("lum-shortstudio-web", Path("/venv/bin/python"), specs["lum-shortstudio-web"])
        for required in (
            "LUM_ENGINE_INSTANCE=shortstudio",
            "LUM_BASE_PATH=/short-uploader",
            "LUM_SUITE_AUTH=1",
            "/tmp/test-short-studio/local-upload-manager/lum-shortstudio",
            "YOUTUBE_UPLOAD_MANAGER_DATA_DIR=/tmp/test-short-studio/local-upload-manager/lum-shortstudio/core",
            "--port 8019",
        ):
            self.assertIn(required, cmd)
        self.assertNotIn("/lum3/", cmd)
        self.assertNotIn("/lum-song/", cmd)
        self.assertNotIn("/lum1/", cmd)

    def test_new_engine_is_off_until_configured_and_thumbnail_free(self):
        config = (LUM / "lum_engine/config.py").read_text()
        self.assertIn('"shortstudio"} else "1"', config)
        source = (LUM / "lum_engine/auto_engine_v6.py").read_text()
        tree = ast.parse(source)
        func = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "thumbnail_required_for_mode")
        scope = {"os": os}
        exec(compile(ast.Module(body=[func], type_ignores=[]), "<thumbnail-policy>", "exec"), scope)
        for mode, required in (("shortstudio", False), ("shorts", False),
                               ("song", True), ("engine2", True)):
            with patch.dict(os.environ, {"LUM_ENGINE_INSTANCE": mode}):
                self.assertEqual(scope["thumbnail_required_for_mode"](), required)
        ui = (LUM / "templates/app.html").read_text()
        server = (LUM / "lum_engine/standalone_app.py").read_text()
        self.assertIn('engine_instance not in [\'shorts\', \'shortstudio\']', ui)
        self.assertIn('upload_instance in {"shorts", "shortstudio"} and has_thumbnail', server)
        self.assertIn('engine_instance not in [\'song\', \'shortstudio\']', ui)
        self.assertIn('Short Uploader · Auto Engine', ui)
        self.assertNotIn("('shorts', 'Short Uploader')", ui)
        from jinja2 import Environment
        Environment().parse(ui)
        Environment().parse((LUM / "templates/base.html").read_text())

    def test_https_route_is_authenticated_idempotent_and_guarded(self):
        from production.route_uploader_scene_https import ROUTE as LUM_ROUTE, ANCHOR
        original = "server {\n" + LUM_ROUTE + ANCHOR + "\n}\n"
        proposed = short_studio_release.apply_route(original)
        self.assertIn("location ^~ /short-uploader/ {", proposed)
        self.assertIn("proxy_pass http://127.0.0.1:8019;", proposed)
        self.assertIn("auth_request /_scene_uploader_auth;", proposed)
        self.assertIn("proxy_set_header X-Auth-User $scene_lum_user;", proposed)
        self.assertEqual(short_studio_release.apply_route(proposed), proposed)
        with self.assertRaisesRegex(RuntimeError, "already claimed"):
            short_studio_release.apply_route(original.replace(
                ANCHOR, "location = /short-uploader { return 200; }\n" + ANCHOR))
        with self.assertRaisesRegex(RuntimeError, "drift"):
            short_studio_release.apply_route("server {\n" + ANCHOR + "\n}")

    def test_release_stays_in_existing_protected_path(self):
        runtime = (ROOT / "production/runtime.py").read_text()
        self.assertIn("install_route(revision)", runtime)
        self.assertIn("restore_route(short_studio_route_receipt)", runtime)
        self.assertIn('("shortstudio", 8019, "/short-uploader/health", "lum-shortstudio")', runtime)
        self.assertIn("require_lum_release_idle(state)", runtime)
        source = (ROOT / "production/short_studio_release.py").read_text()
        self.assertIn('guard(https_get(path)[0] == 401', source)
        self.assertIn('authenticated Short Uploader HTML readback failed', source)
        self.assertIn('if before == after:', source)


if __name__ == "__main__":
    unittest.main()

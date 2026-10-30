"""Short Studio copy/move contracts; entirely offline with no user state mutations."""
from __future__ import annotations

import ast
import os
import sqlite3
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from production import workers, short_studio_release, runtime

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
        for filename, key in (("auto_engine_v6.js", "lum.auto.v6.panel.shortstudio"),
                              ("channels_subtabs_v5_1.js", "lum.shortstudio.channel.panel"),
                              ("navigation_state_v1.js", "lum.shortstudio.main.tab"),
                              ("lum_ux_state_v2.js", "lum.shortstudio.route")):
            self.assertIn(key, (LUM / "static" / filename).read_text())

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

    def test_https_reload_converges_without_dropping_auth(self):
        calls = []
        statuses = iter((404, 404, 401, 404, 401, 401))
        authorized = iter(((404, "text/plain", b""), (200, "text/html", b"Short Uploader | Auto Engine")))

        def get(path, cookie=""):
            calls.append((path, bool(cookie)))
            if path in ("/short-uploader/app", "/short-uploader/health"):
                return next(statuses), "application/json", b""
            if path == "/short-uploader/app?tab=auto_engine":
                self.assertEqual(cookie, "session=dummy")
                return next(authorized)
            if path == "/short-uploader":
                return 308, "", b""
            if path == "/_scene_uploader_auth":
                return 404, "", b""
            raise AssertionError(path)

        with patch.object(short_studio_release.time, "sleep") as sleep:
            short_studio_release.verify_https_route(
                get, lambda: "session=dummy", attempts=4, delay=0.01)
        self.assertEqual(sleep.call_count, 3)
        self.assertIn(("/short-uploader", False), calls)
        self.assertIn(("/_scene_uploader_auth", True), calls)

    def test_https_reload_rejects_public_or_invalid_auth(self):
        for unsafe in (200, 302, 303, 403):
            with self.subTest(status=unsafe):
                def get(path, cookie=""):
                    if path == "/short-uploader/app":
                        return unsafe, "text/html", b""
                    return 401, "text/plain", b""

                with self.assertRaisesRegex(RuntimeError, "anonymous Short Uploader access"):
                    short_studio_release.verify_https_route(get, lambda: "session=dummy", attempts=2, delay=0)

    def test_https_reload_fails_closed_if_stale_404_never_converges(self):
        def get(path, cookie=""):
            return 404, "application/json", b'{"detail":"Not Found"}'
        with patch.object(short_studio_release.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "auth never converged"):
                short_studio_release.verify_https_route(get, lambda: "session=dummy", attempts=3, delay=0)

    def test_https_reload_fails_if_authenticated_html_missing(self):
        def get(path, cookie=""):
            if not cookie:
                return 401, "", b""
            return 200, "text/html", b"Not the Short Uploader"
        with self.assertRaisesRegex(RuntimeError, "authenticated Short Uploader HTML readback failed"):
            short_studio_release.verify_https_route(get, lambda: "session=dummy", attempts=2, delay=0)

    def test_recovered_first_install_requires_empty_owner_state(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            state, units = base / "state", base / "units"
            root = state / "lum-shortstudio"
            root.mkdir(parents=True)
            units.mkdir()
            (root / ".vps1-independent-engine").write_text("shortstudio\\n")
            core = root / "core/data/uploader.db"
            v6 = root / "auto_engine_v6/auto_engine_v6.sqlite3"
            core.parent.mkdir(parents=True)
            v6.parent.mkdir(parents=True)
            with sqlite3.connect(core) as db:
                db.execute("CREATE TABLE videos (id INTEGER PRIMARY KEY)")
                db.execute("CREATE TABLE channels (id INTEGER PRIMARY KEY)")
            with sqlite3.connect(v6) as db:
                db.execute("CREATE TABLE job_sources (video_id INTEGER PRIMARY KEY)")
                db.execute("CREATE TABLE engine_state (running INTEGER, scheduler_armed INTEGER)")
                db.execute("INSERT INTO engine_state VALUES(0,0)")
            with patch.object(runtime.socket, "socket") as socket_ctor:
                socket_ctor.return_value.__enter__.return_value.connect_ex.return_value = 111
                self.assertTrue(runtime.shortstudio_rolled_back_empty(state, unit_dir=units))
                with sqlite3.connect(core) as db:
                    db.execute("INSERT INTO videos VALUES(1)")
                self.assertFalse(runtime.shortstudio_rolled_back_empty(state, unit_dir=units))
                with sqlite3.connect(core) as db:
                    db.execute("DELETE FROM videos")
                    db.execute("INSERT INTO channels VALUES(1)")
                self.assertFalse(runtime.shortstudio_rolled_back_empty(state, unit_dir=units))
                with sqlite3.connect(core) as db:
                    db.execute("DELETE FROM channels")
                with sqlite3.connect(v6) as db:
                    db.execute("INSERT INTO job_sources VALUES(1)")
                self.assertFalse(runtime.shortstudio_rolled_back_empty(state, unit_dir=units))
                with sqlite3.connect(v6) as db:
                    db.execute("DELETE FROM job_sources")
                (units / "vps1-production-lum-shortstudio-web.service").write_text("unit")
                self.assertFalse(runtime.shortstudio_rolled_back_empty(state, unit_dir=units))
                (units / "vps1-production-lum-shortstudio-web.service").unlink()
                (root / "secure").mkdir()
                (root / "secure/oauth-token.json").write_text("{}")
                self.assertFalse(runtime.shortstudio_rolled_back_empty(state, unit_dir=units))

    def test_first_install_rejects_live_listener_and_armed_scheduler(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "state/lum-shortstudio"
            root.mkdir(parents=True)
            units = Path(td) / "units"
            units.mkdir()
            (root / ".vps1-independent-engine").write_text("shortstudio\\n")
            dbfile = root / "auto_engine_v6/auto_engine_v6.sqlite3"
            dbfile.parent.mkdir(parents=True)
            with sqlite3.connect(dbfile) as db:
                db.execute("CREATE TABLE engine_state (running INTEGER, scheduler_armed INTEGER)")
                db.execute("INSERT INTO engine_state VALUES(0,1)")
            with patch.object(runtime.socket, "socket") as sock:
                sock.return_value.__enter__.return_value.connect_ex.return_value = 111
                self.assertFalse(runtime.shortstudio_rolled_back_empty(root.parent, unit_dir=units))
                with sqlite3.connect(dbfile) as db:
                    db.execute("UPDATE engine_state SET scheduler_armed=0")
                sock.return_value.__enter__.return_value.connect_ex.return_value = 0
                self.assertFalse(runtime.shortstudio_rolled_back_empty(root.parent, unit_dir=units))

    def test_release_stays_in_existing_protected_path(self):
        runtime = (ROOT / "production/runtime.py").read_text()
        self.assertIn("install_route(revision)", runtime)
        self.assertIn("restore_route(short_studio_route_receipt)", runtime)
        self.assertIn('("shortstudio", 8019, "/short-uploader/health", "lum-shortstudio")', runtime)
        self.assertIn("require_lum_release_idle(state)", runtime)
        source = (ROOT / "production/short_studio_release.py").read_text()
        self.assertIn('verify_https_route(https_get, scene_cookie)', source)
        self.assertIn('authenticated Short Uploader HTML readback failed', source)
        self.assertIn('if before == after:', source)


if __name__ == "__main__":
    unittest.main()

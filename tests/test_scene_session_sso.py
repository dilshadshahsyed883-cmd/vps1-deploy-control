"""VPS1 single-login routing admission, role and rollback regression tests.

Offline only; the real cutover additionally requires Scene session endpoint
readback, trusted TLS, and owner-authenticated production HTTPS probes.
"""
import ast
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from production import nginx_route_only as legacy
from production import scene_session_routes as session
from production.scene_public_https import nginx_https, VOICE_ANCHOR, VOICE_EXTRA
from production.route_auto_caption_scene_https import add_route as add_caption
from production.route_uploader_scene_https import add_route as add_uploader


class SessionRouteContract(unittest.TestCase):
    def test_all_expected_apps_use_scene_session_not_basic(self):
        expected = {
            "short-maker": 8146,
            "short-stream": 8145,
            "self-media-library": 8147,
            "video-merger": 8148,
            "voice-studio": 8143,
            "auto-caption-studio": 8144,
        }
        self.assertEqual({a: legacy.CATALOG[a]["port"] for a in session.APP_IDS}, expected)
        for app, port in expected.items():
            with self.subTest(app=app):
                text = session.config(app)
                self.assertIn("internal;", text)
                self.assertIn("internal/workspace-auth?app=" + app, text)
                self.assertIn("auth_request " + session.AUTH_PREFIX + app, text)
                self.assertIn("auth_request_set $scene_sso_user", text)
                self.assertIn("proxy_set_header X-Auth-User $scene_sso_user;", text)
                self.assertIn("proxy_set_header X-App-Access $scene_sso_access;", text)
                self.assertIn("proxy_set_header X-Is-Admin $scene_sso_admin;", text)
                self.assertIn('proxy_set_header Authorization "";', text)
                self.assertIn("proxy_pass http://127.0.0.1:" + str(port), text)
                self.assertNotIn("127.0.0.1:8888", text)
                self.assertNotIn('auth_basic "VPS1 Workspace"', text)
                self.assertIn("proxy_set_header Upgrade $http_upgrade;", text)
                self.assertIn("location ^~ /" + app + "/", text)

    @unittest.skipUnless(__import__("shutil").which("nginx") and
                         __import__("shutil").which("openssl"),
                         "real Nginx parser only on Ubuntu CI")
    def test_nginx_parser_accepts_session_proxy_with_existing_routes(self):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cert, key = root / "tls.crt", root / "tls.key"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048",
                "-nodes", "-keyout", str(key), "-out", str(cert),
                "-days", "1", "-subj", "/CN=app.scenebuilder.online"],
                check=True, capture_output=True, timeout=30)
            scene = nginx_https().replace(VOICE_ANCHOR, VOICE_EXTRA + VOICE_ANCHOR, 1)
            scene = add_caption(scene)
            scene, _ = add_uploader(scene)
            snippet_name = str(root / "short-maker.conf")
            with mock.patch.object(legacy, "SNIPPET_DIR", root):
                scene, changed = legacy.add_route(scene, "short-maker")
                self.assertTrue(changed)
                include = f" include {legacy.snippet_path('short-maker')};\n"
                scene = scene.replace(include, session.config("short-maker"))
            scene = scene.replace(legacy.INCLUDE_MARKER,
                legacy.SCENE_SESSION_HIDE + legacy.INCLUDE_MARKER)
            scene = scene.replace("/etc/letsencrypt/live/app.scenebuilder.online/fullchain.pem", str(cert))
            scene = scene.replace("/etc/letsencrypt/live/app.scenebuilder.online/privkey.pem", str(key))
            scene = scene.replace("listen 80;", "listen 18880;")
            scene = scene.replace("listen 443 ssl;", "listen 18443 ssl;")
            config_file = root / "nginx.conf"
            config_file.write_text(
                f"pid {root}/nginx.pid;\nerror_log {root}/error.log;\n" +
                "events {} http { access_log off;\n" + scene + "\n}\n")
            tested = subprocess.run(["nginx", "-t", "-p", str(root),
                                      "-c", str(config_file)],
                                     capture_output=True, text=True, timeout=20)
            self.assertEqual(tested.returncode, 0, tested.stderr)

    def test_existing_lum_scene_bridge_stays_independent(self):
        self.assertNotIn("local-upload-manager", session.APP_IDS)
        with self.assertRaisesRegex(RuntimeError, "unsupported"):
            session.config("local-upload-manager")
        from production.route_uploader_scene_https import ROUTE
        self.assertIn("auth_request /_scene_uploader_auth;", ROUTE)
        self.assertIn("proxy_pass http://127.0.0.1:8015;", ROUTE)

    def test_scene_endpoint_present_role_scoped_and_candidate_safe(self):
        src = (Path(__file__).resolve().parents[1] /
               "apps/scene-builder/part01/app.py").read_text()
        ast.parse(src)
        self.assertIn('@app.get("/internal/workspace-auth")', src)
        self.assertIn('request.session.get("username")', src)
        self.assertIn("account.get(\"subscription_expired\")", src)
        self.assertIn("if not permitted:", src)
        self.assertIn('return Response(status_code=403)', src)
        self.assertIn('else "lum")', src)
        self.assertIn('"auto_caption" if app == "auto-caption-studio"', src)
        self.assertEqual(src.count('"/internal/workspace-auth"'), 3)

    def test_only_exact_reviewed_nginx_site_with_optional_hide(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(legacy, "SNIPPET_DIR", Path(d)):
                site = nginx_https()
                snippet = legacy.snippet_path("short-maker")
                snippet.write_text(legacy.route_config("short-maker"))
                site, _ = legacy.add_route(site, "short-maker")
                site = site.replace(legacy.INCLUDE_MARKER,
                      legacy.SCENE_SESSION_HIDE + legacy.INCLUDE_MARKER)
                self.assertEqual(legacy.owned_routes(site)[1], ["short-maker"])
                snippet.write_text(session.config("short-maker"))
                self.assertEqual(legacy.owned_routes(site)[1], ["short-maker"])
                snippet.write_text("location / { return 200; }")
                with self.assertRaisesRegex(RuntimeError, "snippet drift"):
                    legacy.owned_routes(site)


class AppScopedSessionMigration(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.site = self.root / "scene.conf"
        self.site.write_text(nginx_https())
        self.snippets = self.root / "snippets"
        self.snippets.mkdir()
        self.tx = self.root / "tx"
        for name, value in (("SITE", self.site),
                            ("SNIPPET_DIR", self.snippets),
                            ("TRANSACTIONS", self.tx)):
            p = mock.patch.object(legacy, name, value)
            p.start()
            self.addCleanup(p.stop)
        for obj, name, value in (
            (session, "ready_cookie", lambda app: "session=fake"),
            (session, "verify", lambda app, cookie: None),
            (legacy, "check_scene", lambda: None),
            (legacy, "command", lambda argv: None),
        ):
            p = mock.patch.object(obj, name, value)
            p.start()
            self.addCleanup(p.stop)

    def _legacy_short_maker(self):
        site, changed = legacy.add_route(self.site.read_text(), "short-maker")
        self.assertTrue(changed)
        self.site.write_text(site)
        legacy.snippet_path("short-maker").write_text(legacy.route_config("short-maker"))
        return site

    def test_pilot_replaces_exactly_one_app_without_removing_route(self):
        old = self._legacy_short_maker()
        session.migrate("a" * 40, "short-maker")
        self.assertIn(legacy.SCENE_SESSION_HIDE, self.site.read_text())
        self.assertIn("vps1-route-short-maker.conf", self.site.read_text())
        self.assertEqual(legacy.snippet_path("short-maker").read_text(),
                         session.config("short-maker"))
        self.assertEqual(json.loads((self.tx / "sso-short-maker.json").read_text())["state"],
                         "ACTIVE")
        self.assertTrue(session.active("short-maker", self.site.read_text()))
        session.rollback("b" * 40, "short-maker")
        self.assertEqual(legacy.snippet_path("short-maker").read_text(),
                         legacy.route_config("short-maker"))
        self.assertEqual(json.loads((self.tx / "sso-short-maker.json").read_text())["state"],
                         "ROLLED_BACK")
        self.assertEqual(legacy.owned_routes(self.site.read_text())[1],
                         ["short-maker"])

    def test_auth_or_postreload_failure_preserves_original(self):
        old = self._legacy_short_maker()
        original = legacy.snippet_path("short-maker").read_bytes()
        with mock.patch.object(session, "verify", side_effect=RuntimeError("health failed")):
            with self.assertRaisesRegex(RuntimeError, "health failed"):
                session.migrate("c" * 40, "short-maker")
        self.assertEqual(self.site.read_text(), old)
        self.assertEqual(legacy.snippet_path("short-maker").read_bytes(), original)
        self.assertFalse((self.tx / "sso-short-maker.json").exists())

    def test_scene_auth_not_ready_blocks_before_site_mutation(self):
        old = self._legacy_short_maker()
        with mock.patch.object(session, "ready_cookie", side_effect=RuntimeError("Scene endpoint absent")):
            with self.assertRaisesRegex(RuntimeError, "Scene endpoint absent"):
                session.migrate("d" * 40, "short-maker")
        self.assertEqual(self.site.read_text(), old)
        self.assertEqual(legacy.snippet_path("short-maker").read_text(),
                         legacy.route_config("short-maker"))

    def test_voice_and_caption_standalone_are_reconciled_individually(self):
        site = self.site.read_text().replace(VOICE_ANCHOR,
                                           VOICE_EXTRA + VOICE_ANCHOR, 1)
        site = add_caption(site)
        site, _ = add_uploader(site)
        self.site.write_text(site)
        session.migrate("e" * 40, "voice-studio")
        result = self.site.read_text()
        self.assertIn("vps1-route-voice-studio.conf", result)
        self.assertIn("location ^~ /auto-caption-studio/", result)
        self.assertIn("location ^~ /local-upload-manager/", result)
        self.assertIn("proxy_pass http://127.0.0.1:18080;", result)
        self.assertTrue(session.active("voice-studio", result))


if __name__ == "__main__":
    unittest.main()

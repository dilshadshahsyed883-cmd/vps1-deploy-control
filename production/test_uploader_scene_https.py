"""Offline contracts for Scene HTTPS Auto Uploader session routing (no uploads)."""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from production.route_uploader_scene_https import (
    MARKER, ROUTE, LEGACY_ROUTE, PREVIOUS_ROUTE, APP_EXACT_ROUTE, add_route, retire_legacy_managed_route, verify_outer_shell,
)
from production.scene_public_https import nginx_https

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "apps/scene-builder/part01/templates/local_upload_manager.html"
STATE = ROOT / "apps/scene-builder/part01/static/lum_host_state_v1.js"


class UploaderSceneRouteTests(unittest.TestCase):
    def test_scene_outer_shell_is_not_overridden(self):
        config, changed = add_route(nginx_https())
        self.assertTrue(changed)
        self.assertIn("location ^~ /local-upload-manager/ {", config)
        self.assertIn("location = /local-upload-manager {", ROUTE)
        self.assertIn("location = /local-upload-manager/ { return 308 /local-upload-manager; }", ROUTE)
        exact = ROUTE.split(" location = /_scene_uploader_auth", 1)[0]
        self.assertIn("proxy_pass http://127.0.0.1:18080;", exact)
        self.assertNotIn("8015", exact)
        self.assertIn("location / {\n    proxy_pass http://127.0.0.1:18080;", config)

    def test_exact_legacy_auto_engine_url_keeps_original_lum_engine(self):
        # An exact-match route has priority over prefix and regex locations.
        # This is the user's bookmarked /local-upload-manager/app?tab=auto_engine&embedded=1.
        self.assertIn("location = /local-upload-manager/app {", ROUTE)
        self.assertIn("proxy_pass http://127.0.0.1:8015;", APP_EXACT_ROUTE)
        self.assertIn("auth_request /_scene_uploader_auth;", APP_EXACT_ROUTE)
        self.assertIn("proxy_set_header X-Auth-User $scene_lum_user;", APP_EXACT_ROUTE)
        self.assertNotIn("8018", APP_EXACT_ROUTE)
        self.assertNotIn("/song-uploader", APP_EXACT_ROUTE)
        app_source = (ROOT / "apps/local-upload-manager/part01/lum_engine/standalone_app.py").read_text()
        self.assertIn('@app.get(route("/app"), response_class=HTMLResponse)', app_source)
        self.assertIn('auto_mode: str = "engine1"', app_source)
        self.assertIn('selected = tab if tab in allowed else default_tab', app_source)

    def test_current_prior_exact_uploader_route_upgrades_without_redirect(self):
        initial = nginx_https()
        previous = initial.replace(
            " location / {\\n    proxy_pass http://127.0.0.1:18080;",
            PREVIOUS_ROUTE + " location / {\\n    proxy_pass http://127.0.0.1:18080;", 1)
        self.assertNotEqual(initial, previous)
        updated, changed = add_route(previous)
        self.assertTrue(changed)
        self.assertIn(APP_EXACT_ROUTE, updated)
        self.assertEqual(add_route(updated), (updated, False))

    def test_identity_from_scene_auth_not_visitor_headers(self):
        for snippet in (
            "location = /_scene_uploader_auth {",
            "    internal;",
            "proxy_pass http://127.0.0.1:18080/internal/local-upload-manager-auth;",
            "auth_request /_scene_uploader_auth;",
            "auth_request_set $scene_lum_user $upstream_http_x_auth_user;",
            "auth_request_set $scene_lum_access $upstream_http_x_app_access;",
            "auth_request_set $scene_lum_admin $upstream_http_x_is_admin;",
            "proxy_set_header X-Auth-User $scene_lum_user;",
            "proxy_set_header X-App-Access $scene_lum_access;",
            "proxy_set_header X-Is-Admin $scene_lum_admin;",
        ):
            self.assertIn(snippet, ROUTE)

    def test_add_route_is_idempotent_and_refuses_unknown_changes(self):
        initial = nginx_https()
        patched, changed = add_route(initial)
        self.assertTrue(changed)
        self.assertEqual(patched.count(MARKER), 1)
        again, changed = add_route(patched)
        self.assertFalse(changed)
        self.assertEqual(patched, again)
        with self.assertRaisesRegex(RuntimeError, "another config"):
            add_route(initial.replace(" location / {", " location ^~ /local-upload-manager/ { return 200; }\n location / {", 1))
        with self.assertRaisesRegex(RuntimeError, "differs"):
            add_route(patched.replace("127.0.0.1:8015", "127.0.0.1:1"))

    def test_exact_previous_uploader_route_upgrades_without_other_changes(self):
        previous = nginx_https().replace(
            " location / {\\n    proxy_pass http://127.0.0.1:18080;",
            LEGACY_ROUTE + " location / {\\n    proxy_pass http://127.0.0.1:18080;", 1)
        upgraded, changed = add_route(previous)
        self.assertTrue(changed)
        self.assertIn(ROUTE, upgraded)
        self.assertNotIn(LEGACY_ROUTE + " location / {", upgraded)
        self.assertEqual(add_route(upgraded), (upgraded, False))

    def test_retires_only_reviewed_legacy_uploader_include(self):
        from unittest.mock import patch
        include = " include /etc/nginx/snippets/vps1-route-local-upload-manager.conf;\n"
        site = "before\n" + include + "after\n"
        with patch("production.nginx_route_only.owned_routes",
                   return_value=("reviewed", ["local-upload-manager"])):
            updated, changed = retire_legacy_managed_route(site)
        self.assertTrue(changed)
        self.assertEqual(updated, "before\nafter\n")

    def test_leaves_site_without_legacy_uploader_include_unchanged(self):
        site = "reviewed scene site\n"
        self.assertEqual(retire_legacy_managed_route(site), (site, False))

    def test_rejects_unreviewed_legacy_uploader_include(self):
        from unittest.mock import patch
        include = " include /etc/nginx/snippets/vps1-route-local-upload-manager.conf;\n"
        with patch("production.nginx_route_only.owned_routes",
                   return_value=("reviewed", ["short-maker"])):
            with self.assertRaisesRegex(RuntimeError, "reviewed managed route"):
                retire_legacy_managed_route(include)

    def test_outer_shell_retries_then_requires_native_document(self):
        from unittest.mock import patch
        responses = [
            (308, "text/html", b""),
            (303, "text/html", b""),
            (200, "text/html; charset=utf-8", b'<div class="lum-shell"></div>'),
        ]
        with patch("production.route_uploader_scene_https.https_get",
                   side_effect=responses) as request, \
             patch("production.route_uploader_scene_https.time.sleep"):
            verify_outer_shell("session=valid", published=True)
        self.assertEqual(request.call_count, 3)

    def test_outer_shell_persistent_old_route_fails_closed(self):
        from unittest.mock import patch
        with patch("production.route_uploader_scene_https.https_get",
                   return_value=(308, "text/html", b"")), \
             patch("production.route_uploader_scene_https.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "outer shell regressed"):
                verify_outer_shell("session=valid", published=True)

    def test_outer_shell_does_not_retry_unexpected_server_error(self):
        from unittest.mock import patch
        with patch("production.route_uploader_scene_https.https_get",
                   return_value=(500, "text/html", b"")) as request:
            with self.assertRaisesRegex(RuntimeError, "outer shell regressed"):
                verify_outer_shell("session=valid", published=True)
        self.assertEqual(request.call_count, 1)

    def test_parent_ui_and_embedded_state_use_one_registered_mode_endpoint(self):
        html = HTML.read_text()
        state = STATE.read_text()
        self.assertNotIn("/LUM/app", html + state)
        self.assertNotIn("/local-upload-manager-engine2", html)
        self.assertNotIn("/local-upload-manager-engine3", html)
        self.assertIn("data-default-src=\"/local-upload-manager/app?", html)
        self.assertIn("u.pathname!=='/local-upload-manager/app'", html)
        self.assertIn("'short_stream'", html)
        self.assertIn("'engine2'", html)
        self.assertIn("out.searchParams.set('auto_mode',autoMode)", state)
        self.assertIn("u.pathname!=='/local-upload-manager/app'", state)

    @unittest.skipUnless(shutil.which("nginx"), "Nginx is installed in Ubuntu CI")
    def test_nginx_parses_authenticated_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = add_route(nginx_https())[0]
            # Syntax-check TLS directives against a locally generated test cert.
            crt = root / "test.crt"
            key = root / "test.key"
            subprocess.run([
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-keyout", str(key), "-out", str(crt), "-days", "1",
                "-subj", "/CN=app.scenebuilder.online"
            ], check=True, capture_output=True, timeout=20)
            config = config.replace(
                "/etc/letsencrypt/live/app.scenebuilder.online/fullchain.pem", str(crt)
            ).replace(
                "/etc/letsencrypt/live/app.scenebuilder.online/privkey.pem", str(key)
            )
            conf = root / "nginx.conf"
            conf.write_text("events {}\nhttp {\n" + config + "\n}\n")
            command = ["nginx", "-t", "-p", str(root), "-c", str(conf)]
            if hasattr(__import__("os"), "geteuid") and __import__("os").geteuid() != 0:
                command.insert(0, "sudo")
            tested = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(tested.returncode, 0, tested.stderr)


if __name__ == "__main__":
    unittest.main()

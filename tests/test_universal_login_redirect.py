"""Offline guardrails for shared browser login and native app routes."""
import importlib.util
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

from production.scene_public_https import nginx_https
from production.route_uploader_scene_https import add_route as add_uploader
from production.universal_login_redirect import apply, RULE, MARKER, ANCHOR


ROOT = Path(__file__).resolve().parents[1]
LOGIN_HELPER = ROOT / "apps/scene-builder/part01/login_next.py"
SPEC = importlib.util.spec_from_file_location("scene_login_next", LOGIN_HELPER)
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)


class LoginReturnTargetTests(unittest.TestCase):
    def test_valid_deep_links_are_kept(self):
        values = ("/", "/app", "/short-maker/", "/short-uploader/app?tab=queue",
                  "/song-uploader/app?tab=auto_engine&profile_id=42",
                  "/copyright-uploader/app?tab=add_video", "/voice-studio/app",
                  "/video-merger/", "/self-media-library/")
        for value in values:
            with self.subTest(value=value):
                self.assertEqual(helper.safe_login_target(value), value)

    def test_external_and_unsafe_redirects_rejected(self):
        values = ("https://evil.example/path", "//evil.example/",
                  "/\\evil.example", "/%5cevil.example", "/%2fevil.example/",
                  "/%252fevil.example", "/short-maker/../admin",
                  "/short-maker/%2e%2e/admin", "/short-maker/%252e%252e/admin",
                  "/login", "/logout", "/internal/workspace-auth",
                  "/short-maker/\nX-Injected: yes", "", " /app", "javascript:alert(1)")
        for value in values:
            with self.subTest(value=value):
                self.assertEqual(helper.safe_login_target(value), "/")

    def test_login_url_escapes_query_parameters(self):
        self.assertEqual(helper.login_redirect_url("/short-uploader/app?tab=queue&group=2"),
                         "/login?next=%2Fshort-uploader%2Fapp%3Ftab%3Dqueue%26group%3D2")
        self.assertIn("next=%2Fshort-maker%2F",
                      helper.login_redirect_url("/short-maker/", error="Invalid sign in"))

    def test_scene_web_template_and_login_handler_preserve_destination(self):
        app = (ROOT / "apps/scene-builder/part01/app.py").read_text()
        template = (ROOT / "apps/scene-builder/part01/templates/login.html").read_text()
        self.assertIn('name="next" value="{{ login_next }}"', template)
        self.assertIn("login_redirect_url(next, error=", app)
        self.assertIn("return RedirectResponse(safe_login_target(next), status_code=303)", app)
        self.assertIn('"text/html" in request.headers.get("accept", "")', app)


class NginxGlobalLoginTests(unittest.TestCase):
    def site(self):
        value, changed = add_uploader(nginx_https())
        self.assertTrue(changed)
        return value.replace(ANCHOR,
             " include /etc/nginx/snippets/vps1-route-short-maker.conf;\n" + ANCHOR, 1)

    def test_inserts_single_exact_hostwide_rule_and_preserves_other_routes(self):
        original = self.site()
        source = original.replace(ANCHOR, (
            " # VPS1_SHORT_STUDIO_UPLOADER_ROUTE_V1\n"
            " location ^~ /short-uploader/ { auth_request /_scene_uploader_auth; }\n"
            " # VPS1_COPYRIGHT_STUDIO_UPLOADER_ROUTE_V1\n"
            " location ^~ /copyright-uploader/ { auth_request /_scene_uploader_auth; }\n" +
            ANCHOR), 1)
        upgraded = apply(source)
        self.assertEqual(upgraded.count(MARKER), 1)
        self.assertEqual(upgraded, source.replace(ANCHOR, RULE + ANCHOR, 1))
        self.assertEqual(apply(upgraded), upgraded)
        self.assertIn("location ^~ /short-uploader/", upgraded)
        self.assertIn("location ^~ /copyright-uploader/", upgraded)

    def test_fails_closed_on_unreviewed_policy_or_unowned_tls_site(self):
        with self.assertRaisesRegex(RuntimeError, "TLS server"):
            apply("server { listen 80; }")
        with self.assertRaisesRegex(RuntimeError, "policy differs"):
            apply(apply(self.site()).replace("return 303", "return 200"))

    def test_api_and_websocket_guards_stay_ahead_of_redirect(self):
        self.assertIn('if ($uri ~* "(^|/)(api|health|healthz)(/|$)") { return 401; }', RULE)
        self.assertIn('if ($http_upgrade != "") { return 401; }', RULE)
        self.assertIn('if ($http_accept !~* "text/html") { return 401; }', RULE)
        self.assertLess(RULE.index("return 401;"), RULE.index("return 303"))
        self.assertIn("return 303 /login?next=$request_uri;", RULE)
        self.assertNotIn("error_page 403", RULE)

    @unittest.skipUnless(shutil.which("nginx"), "Nginx parser unavailable")
    def test_real_nginx_parser_accepts_global_redirect_in_scene_site(self):
        if not shutil.which("openssl"):
            self.skipTest("OpenSSL not installed")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key, cert = root / "key.pem", root / "cert.pem"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048",
                            "-nodes", "-keyout", str(key), "-out", str(cert),
                            "-days", "1", "-subj", "/CN=app.scenebuilder.online"],
                           check=True, capture_output=True, timeout=30)
            value = apply(self.site()).replace(
                " include /etc/nginx/snippets/vps1-route-short-maker.conf;",
                " location ^~ /short-maker/ { return 401; }")
            value = value.replace(
                "/etc/letsencrypt/live/app.scenebuilder.online/fullchain.pem", str(cert))
            value = value.replace(
                "/etc/letsencrypt/live/app.scenebuilder.online/privkey.pem", str(key))
            value = value.replace("listen 80;", "listen 18880;")
            value = value.replace("listen 443 ssl;", "listen 18443 ssl;")
            conf = root / "nginx.conf"
            conf.write_text(f"pid {root}/nginx.pid; error_log {root}/error.log; events {{}} http {{ access_log off;\n{value}\n}}")
            run = subprocess.run(["nginx", "-t", "-p", tmp, "-c", str(conf)],
                                 capture_output=True, text=True, timeout=20)
            self.assertEqual(run.returncode, 0, run.stderr)


if __name__ == "__main__":
    unittest.main()

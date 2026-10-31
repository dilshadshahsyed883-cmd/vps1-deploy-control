"""Offline guardrails for shared browser login and native app routes."""
import importlib.util
import http.client
import socket
import time
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

    def test_full_unescaped_nginx_deep_link_is_preserved(self):
        raw = "next=/short-uploader/app?tab=queue&profile=2&source=maker"
        self.assertEqual(
            helper.browser_login_target(raw, "/short-uploader/app?tab=queue"),
            "/short-uploader/app?tab=queue&profile=2&source=maker",
        )
        self.assertEqual(
            helper.browser_login_target("next=%2Fshort-maker%2F", "/short-maker/"),
            "/short-maker/",
        )
        self.assertEqual(helper.browser_login_target("next=//evil.example", "//evil.example"), "/")

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
        self.assertIn('if ($uri ~ "^/(_|internal/)") { return 401; }', RULE)
        self.assertIn('if ($http_upgrade != "") { return 401; }', RULE)
        self.assertIn('if ($http_accept !~* "text/html") { return 401; }', RULE)
        self.assertLess(RULE.index("return 401;"), RULE.index("return 303"))
        self.assertIn("return 303 /login?next=$request_uri;", RULE)
        self.assertNotIn("error_page 403", RULE)

    def test_guarded_release_retries_only_known_busy_conditions(self):
        workflow = (ROOT / ".github/workflows/vps1-universal-login-release.yml").read_text()
        self.assertIn("for attempt in $(seq 1 18); do", workflow)
        self.assertIn("Scene production worker: queued or in-flight Scene jobs exist; source refresh deferred", workflow)
        self.assertIn("BlockingIOError: [Errno 11] Resource temporarily unavailable", workflow)
        self.assertIn('test "$installed" = 1', workflow)
        self.assertIn("nginx-route-only $SOURCE_SHA short-maker", workflow)
        self.assertNotIn("kill -", workflow)
        self.assertNotIn("systemctl stop", workflow)

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


    @unittest.skipUnless(shutil.which("nginx"), "Nginx executable unavailable")
    def test_live_nginx_internal_auth_401_never_becomes_500(self):
        """Exercise a real auth_request: HTML browser gets 303, subrequest 401."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "demo").mkdir()
            (root / "demo/index.html").write_text("<html>Protected demo</html>")
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            conf = root / "nginx.conf"
            conf.write_text(
                f"pid {root}/nginx.pid; error_log {root}/error.log; events {{}} "
                f"http {{ access_log off; server {{ listen 127.0.0.1:{port};\n"
                + RULE +
                f" location = /_mock_auth {{ internal; return 401; }}\n"
                f" location ^~ /demo/ {{ auth_request /_mock_auth; root {root}; }}\n"
                f" location = /login {{ return 200; }}\n"
                f"}} }}\n"
            )
            parsed = subprocess.run(["nginx", "-t", "-p", tmp, "-c", str(conf)],
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(parsed.returncode, 0, parsed.stderr)
            process = subprocess.Popen(
                ["nginx", "-p", tmp, "-c", str(conf), "-g", "daemon off;"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            try:
                def get(path, accept):
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                    conn.request("GET", path, headers={"Accept": accept})
                    response = conn.getresponse()
                    status = response.status
                    location = response.getheader("Location", "")
                    response.read()
                    conn.close()
                    return status, location

                for _ in range(30):
                    try:
                        status, _ = get("/login", "text/html")
                        if status == 200:
                            break
                    except OSError:
                        time.sleep(0.05)
                else:
                    self.fail("Nginx failed to bind test listener")
                status, location = get("/demo/index.html", "text/html")
                self.assertEqual(status, 303)
                self.assertIn("/login?next=/demo/index.html", location)
                self.assertEqual(get("/demo/index.html", "*/*")[0], 401)
                self.assertEqual(get("/demo/api/health", "text/html")[0], 401)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()

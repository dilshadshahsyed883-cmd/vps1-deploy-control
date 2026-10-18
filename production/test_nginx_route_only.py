"""Tests for GitHub-driven Nginx routes. No VPS changes or network needed."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from production import nginx_route_only as routes
from production.scene_public_https import nginx_https, VOICE_ANCHOR, VOICE_EXTRA
from production.route_auto_caption_scene_https import add_route as add_acs
from production.route_uploader_scene_https import add_route as add_lum

ROOT = Path(__file__).resolve().parents[1]


class RouteRenderTests(unittest.TestCase):
    def test_supported_combinations_keep_owner_sso_and_scene_login(self):
        base = nginx_https()
        modified = base.replace(VOICE_ANCHOR, VOICE_EXTRA + VOICE_ANCHOR, 1)
        modified = add_acs(modified)
        modified, _ = add_lum(modified)
        self.assertIn(modified, routes.known_sites())
        self.assertTrue(routes.standalone(modified, "voice-studio"))
        self.assertTrue(routes.standalone(modified, "auto-caption-studio"))
        self.assertTrue(routes.standalone(modified, "local-upload-manager"))
        new, changed = routes.add_route(modified, "short-maker")
        self.assertTrue(changed)
        self.assertEqual(new.replace(
            " include " + str(routes.snippet_path("short-maker")) + ";\n", ""), modified)
        for existing in (VOICE_EXTRA, routes.ACS_ROUTE, routes.UPLOADER_ROUTE):
            self.assertEqual(new.count(existing), 1)
        self.assertIn("proxy_pass http://127.0.0.1:18080;", new)
        self.assertIn("location = /internal/runtime-ready { return 404; }", new)
        self.assertIn("ssl_certificate /etc/letsencrypt/live/", new)
        for app in ("voice-studio", "auto-caption-studio", "local-upload-manager"):
            self.assertEqual(routes.add_route(modified, app), (modified, False))

    def test_app_routes_only_proxy_to_authenticated_existing_gateway(self):
        for app in routes.APPS:
            conf = routes.route_config(app)
            self.assertIn(f"location ^~ /{app}/", conf)
            self.assertIn("proxy_pass http://127.0.0.1:8888;", conf)
            self.assertIn("proxy_set_header Authorization $http_authorization;", conf)
            self.assertIn("proxy_set_header Upgrade $http_upgrade;", conf)
            self.assertNotIn("auth_basic off;", conf)
            self.assertNotIn("proxy_pass http://127.0.0.1:18080;", conf)
        for app in ("all", "scene-builder", "auto-script", "../abc"):
            with self.assertRaisesRegex(RuntimeError, "unapproved"):
                routes.route_config(app)

    def test_site_drift_and_unknown_preexisting_route_are_blocked(self):
        old = nginx_https()
        for text in (old + "location ^~ /short-maker/ { return 200; }\n",
                     old.replace("listen 443 ssl;", "listen 443;"),
                     old.replace("server_name app.scenebuilder.online;",
                                 "server_name outside.example;", 1)):
            with self.assertRaisesRegex(RuntimeError, "drift"):
                routes.add_route(text, "short-maker")
        with self.assertRaisesRegex(RuntimeError, "prior workspace"):
            # The old PR60 workspace include is owned separately, not ours.
            # Until reconciled, route-only fails rather than overwriting it.
            routes.add_route(old.replace(routes.INCLUDE_MARKER,
                        routes.WORKSPACE_INCLUDE + routes.INCLUDE_MARKER),
                             "short-maker")

    def test_existing_owned_snippet_is_idempotent_and_exact(self):
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch.object(routes, "SNIPPET_DIR", Path(folder)):
                app = "short-maker"
                base = nginx_https()
                site, _ = routes.add_route(base, app)
                p = routes.snippet_path(app)
                p.write_text(routes.route_config(app))
                self.assertEqual(routes.owned_routes(site)[1], [app])
                self.assertEqual(routes.add_route(site, app), (site, False))
                p.write_text("location / { return 200; }")
                with self.assertRaisesRegex(RuntimeError, "snippet drift"):
                    routes.add_route(site, app)

    def test_health_ui_paths_include_prefix_exactly_once(self):
        self.assertEqual(routes.public_path("short-maker", "health"), "/short-maker/health")
        self.assertEqual(routes.public_path("self-media-library", "health"),
                         "/self-media-library/api/health")
        self.assertEqual(routes.public_path("voice-studio", "health"), "/voice-studio/healthz")
        self.assertEqual(routes.public_path("video-merger", "health"), "/video-merger/health")
        self.assertEqual(routes.public_path("local-upload-manager", "ui"),
                         "/local-upload-manager/app")

    @unittest.skipUnless(shutil.which("nginx") and shutil.which("openssl"),
                         "real Nginx parser in Ubuntu CI")
    def test_nginx_parser_accepts_combined_existing_routes_plus_new(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cert, key = root / "test.crt", root / "test.key"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048",
                            "-nodes", "-keyout", str(key), "-out", str(cert),
                            "-days", "1", "-subj", "/CN=app.scenebuilder.online"],
                           check=True, capture_output=True, timeout=20)
            old = nginx_https()
            old = old.replace(VOICE_ANCHOR, VOICE_EXTRA + VOICE_ANCHOR, 1)
            old = add_acs(old)
            old, _ = add_lum(old)
            site, _ = routes.add_route(old, "short-maker")
            inc = f" include {routes.snippet_path('short-maker')};\n"
            site = site.replace(inc, routes.route_config("short-maker"))
            site = site.replace("/etc/letsencrypt/live/app.scenebuilder.online/fullchain.pem",
                                str(cert)).replace(
                                    "/etc/letsencrypt/live/app.scenebuilder.online/privkey.pem",
                                    str(key))
            # Same TLS/server/location syntax without binding privileged test ports.
            site = site.replace("listen 80;", "listen 18880;")
            site = site.replace("listen 443 ssl;", "listen 18443 ssl;")
            config = root / "nginx.conf"
            config.write_text(f"pid {root}/nginx.pid;\n" +
                              f"error_log {root}/nginx-error.log;\n" +
                              "events {} http { access_log off;\n" + site + "\n}\n")
            result = subprocess.run(["nginx", "-t", "-p", str(root), "-c", str(config)],
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)


class ReleaseTransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.site = self.root / "scene.conf"
        self.snippets = self.root / "snippets"
        self.snippets.mkdir()
        self.tx = self.root / "transactions"
        self.site.write_text(nginx_https())
        for name, value in (("SITE", self.site), ("SNIPPET_DIR", self.snippets),
                            ("TRANSACTIONS", self.tx)):
            patcher = mock.patch.object(routes, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, fake in (("preflight", lambda sha, app, **kw: "Basic simulated"),
                           ("check_app", lambda *a, **kw: None),
                           ("check_scene", lambda: None),
                           ("command", lambda *a, **kw: None)):
            patcher = mock.patch.object(routes, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_publish_is_scoped_and_can_rollback(self):
        original = self.site.read_bytes()
        routes.publish("a" * 40, "short-maker")
        generated = self.site.read_text()
        self.assertIn("vps1-route-short-maker.conf", generated)
        self.assertNotIn("vps1-route-video-merger.conf", generated)
        self.assertTrue((self.snippets / "vps1-route-short-maker.conf").exists())
        record = json.loads((self.tx / "short-maker.json").read_text())
        self.assertEqual(record["state"], "ACTIVE")
        self.assertEqual(record["owner_uat"], "PENDING")
        routes.publish("a" * 40, "short-maker")
        self.assertEqual(self.site.read_text(), generated)
        routes.rollback("b" * 40, "short-maker")
        self.assertEqual(self.site.read_bytes(), original)
        self.assertFalse((self.snippets / "vps1-route-short-maker.conf").exists())
        self.assertEqual(json.loads((self.tx / "short-maker.json").read_text())["state"],
                         "ROLLED_BACK")
        with self.assertRaisesRegex(RuntimeError, "no active"):
            routes.rollback("c" * 40, "short-maker")

    def test_post_reload_verify_failure_restores_previous_site(self):
        original = self.site.read_bytes()
        with mock.patch.object(routes, "check_app",
                               side_effect=RuntimeError("mock verification failure")):
            with self.assertRaisesRegex(RuntimeError, "mock verification failure"):
                routes.publish("b" * 40, "short-maker")
        self.assertEqual(self.site.read_bytes(), original)
        self.assertFalse(routes.snippet_path("short-maker").exists())
        self.assertFalse((self.tx / "short-maker.json").exists())

    def test_rollback_blocks_newer_nginx_drift(self):
        routes.publish("a" * 40, "short-maker")
        self.site.write_text(self.site.read_text() + "# unrelated live route\n")
        with self.assertRaisesRegex(RuntimeError, "modified after release"):
            routes.rollback("b" * 40, "short-maker")
        self.assertTrue(routes.snippet_path("short-maker").exists())


class DeploymentDispatchTests(unittest.TestCase):
    def test_remote_helper_sends_only_route_operation_to_route_module(self):
        from production import remote_deploy
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            sha = "c" * 40
            checkout = root / sha
            (checkout / ".git").mkdir(parents=True)
            args = ["helper", "nginx-route-only", sha, "short-maker"]
            calls = []
            def git_response(cmd, **kw):
                return sha if (cmd[:2] == ["git", "ls-remote"] or
                               cmd[:2] == ["git", "rev-parse"]) else ""
            def fake_run(argv, **kw):
                calls.append(argv)
                return mock.Mock(returncode=0)
            with mock.patch.object(remote_deploy, "BASE", root), \
                 mock.patch.object(remote_deploy.os, "geteuid", return_value=0), \
                 mock.patch.object(remote_deploy.socket, "gethostname",
                                   return_value="vps-01a23801"), \
                 mock.patch.object(remote_deploy, "command", side_effect=git_response), \
                 mock.patch.object(remote_deploy.subprocess, "run",
                                   side_effect=fake_run), \
                 mock.patch.object(sys, "argv", args):
                remote_deploy.main()
                self.assertEqual(calls[0][2:4],
                                 ["production.nginx_route_only", "nginx-route-only"])
                self.assertNotIn("production.runtime", str(calls))
                self.assertNotIn("production.gateway", str(calls))
                args[3] = "all"
                with self.assertRaisesRegex(ValueError, "one approved independent app"):
                    remote_deploy.main()

    def test_actions_workflow_keeps_production_tailscale_ssh_guards(self):
        source = (ROOT / ".github/workflows/vps1-production.yml").read_text()
        helper = (ROOT / "production/remote_deploy.py").read_text()
        for literal in ("nginx-route-only", "nginx-route-rollback", "environment: production",
                        "tailscale/github-action@v4", "id-token: write",
                        "StrictHostKeyChecking=yes", "vps1deploy@100.87.55.92",
                        "APP_SELECTION"):
            self.assertIn(literal, source)
        for literal in ("nginx-route-only", "nginx-route-rollback",
                        "production.nginx_route_only",
                        "one approved independent app"):
            self.assertIn(literal, helper)


if __name__ == "__main__":
    unittest.main()

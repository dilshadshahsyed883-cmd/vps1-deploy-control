"""GitHub source and safe read-only VPS1 inventory regression checks."""
import unittest
from unittest import mock
from production import web_preflight as w


class WebPreflightTests(unittest.TestCase):
    def test_source_catalog_unique_loopback_ports(self):
        apps = w.catalog()
        ports = [a["port"] for a in apps] + [w.GATEWAY_PORT]
        self.assertEqual(len(ports), len(set(ports)))
        self.assertTrue(all(a["bind"] == "127.0.0.1" for a in apps))

    def test_known_api_and_health_contracts(self):
        apps = {a["app"]: a for a in w.catalog()}
        self.assertEqual(apps["voice-studio"]["port"], 8143)
        self.assertEqual(apps["voice-studio"]["public_health"], "/voice-studio/healthz")
        self.assertEqual(apps["auto-caption-studio"]["port"], 8144)
        self.assertEqual(apps["local-upload-manager"]["public_health"], "/local-upload-manager/health")
        self.assertEqual(apps["scene-public-candidate"]["port"], 18080)
        self.assertEqual(apps["vps-storage-manager"]["port"], 8149)

    def test_source_mode_does_not_probe_host(self):
        with mock.patch.object(w, "host_inventory", side_effect=AssertionError("host read forbidden")):
            with mock.patch("sys.argv", ["web_preflight.py"]), mock.patch("builtins.print") as printer:
                w.main()
                self.assertIn('"mode": "source_only"', printer.call_args.args[0])

    def test_public_listener_marked_exposed(self):
        sample = [{"app": "voice-studio", "port": 8143, "health": "/healthz",
                   "service": "vps1-production-voice-studio.service",
                   "https_path": "/voice-studio/"}]
        with mock.patch.object(w, "listeners", return_value={8143: ["0.0.0.0"]}), \
             mock.patch.object(w, "http_probe", return_value="PASS"), \
             mock.patch.object(w, "tls_probe", return_value=404), \
             mock.patch.object(w, "service_state", return_value="ACTIVE"), \
             mock.patch.object(w, "gateway_anon_status", return_value=401), \
             mock.patch.object(w, "helper_state", return_value="SOURCE_MATCH"), \
             mock.patch.object(w, "process", return_value=None):
            got = w.host_inventory(sample)
            self.assertEqual(got["services"][0]["listen"], "EXPOSED")
            self.assertEqual(got["independent_https"]["voice-studio"]["state"], "MISSING")

    def test_cert_symlink_permission_denied_is_not_marked_absent(self):
        with mock.patch.object(w.Path, "is_file", side_effect=PermissionError(13, "Permission denied")):
            self.assertEqual(w.safe_presence(w.CERT), "UNVERIFIABLE_NON_ROOT")

    def test_nginx_permission_denied_is_not_a_syntax_failure(self):
        class Completed:
            returncode = 1
            stderr = "nginx: open() \"/etc/letsencrypt/live/key\" failed (13: Permission denied)"
        with mock.patch.object(w.shutil, "which", return_value="/usr/sbin/nginx"), \
             mock.patch.object(w, "process", return_value=Completed()):
            self.assertEqual(w.nginx_syntax_state(), "UNVERIFIABLE_NON_ROOT")

    def test_helper_permission_denied_is_reported(self):
        with mock.patch.object(w.Path, "is_file", side_effect=PermissionError(13, "Permission denied")):
            self.assertEqual(w.helper_state(), "UNVERIFIABLE_NON_ROOT")

    def test_route_ownership_summarizes_only_route_presence(self):
        from unittest.mock import mock_open
        site = ("server {\n"
                " location = /internal/runtime-ready { return 404; }\n"
                " location ^~ /voice-studio/ { proxy_pass http://127.0.0.1:8888; }\n"
                " location / { proxy_pass http://127.0.0.1:18080; }\n"
                "}\n")
        with mock.patch.object(w.Path, "read_text", mock_open(read_data=site)):
            found = w.site_route_ownership()
            self.assertTrue(found["reviewed_marker_present"])
            self.assertTrue(found["routes_or_workspace_include"]["voice-studio"])
            self.assertFalse(found["routes_or_workspace_include"]["short-maker"])
            self.assertTrue(found["scene_public_upstream"])

    def test_helper_operation_capability_distinguishes_old_helper(self):
        with mock.patch.object(w.Path, "is_file", return_value=True), \
             mock.patch.object(w.Path, "is_symlink", return_value=False), \
             mock.patch.object(w.Path, "read_bytes", return_value=b"legacy install rollback"):
            self.assertEqual(w.helper_route_capability(), "NO")
        with mock.patch.object(w.Path, "is_file", return_value=True), \
             mock.patch.object(w.Path, "is_symlink", return_value=False), \
             mock.patch.object(w.Path, "read_bytes", return_value=b"allow nginx-route-only"):
            self.assertEqual(w.helper_route_capability(), "YES")

    def test_loopback_listener_detection(self):
        class Completed:
            returncode = 0
            stdout = 'LISTEN 0 128 127.0.0.1:8143 0.0.0.0:*\nLISTEN 0 128 [::1]:8888 [::]:*\n'
        with mock.patch.object(w, "process", return_value=Completed()):
            self.assertEqual(w.listeners()[8143], ["127.0.0.1"])
            self.assertEqual(w.listeners()[8888], ["::1"])


if __name__ == "__main__":
    unittest.main()

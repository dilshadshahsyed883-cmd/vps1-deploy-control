import copy
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from render_nginx_routes import load_registry, render, validate_registry

FIXTURE = ROOT / "tests" / "fixtures" / "nginx-apps.json"
EMPTY = ROOT / "infrastructure" / "registry" / "apps.json"


class NginxRegistryTests(unittest.TestCase):
    def setUp(self):
        self.valid = load_registry(FIXTURE)

    def test_live_registry_intentionally_empty(self):
        live = load_registry(EMPTY)
        self.assertEqual(live["apps"], [])
        self.assertIn("inactive until explicitly wired", render(live))
        with self.assertRaisesRegex(ValueError, "empty managed"):
            render(live, require_nonempty=True)

    def test_deterministic_loopback_only(self):
        conf = render(self.valid, require_nonempty=True)
        self.assertEqual(conf, render(self.valid))
        self.assertIn("location ^~ /ci-canary/", conf)
        self.assertIn("proxy_pass http://127.0.0.1:18081;", conf)
        self.assertIn('proxy_set_header Connection "upgrade";', conf)
        self.assertNotIn("server_name", conf)

    def test_prefix_strip_behavior(self):
        data = copy.deepcopy(self.valid)
        data["apps"][0]["prefix_mode"] = "strip"
        self.assertIn("proxy_pass http://127.0.0.1:18081/;", render(data))

    def test_rejects_route_and_header_injection(self):
        for bad in ("/ci-canary/; return 200 hacked;", "/ci-canary/\nlocation / {", "/another/"):
            data = copy.deepcopy(self.valid)
            data["apps"][0]["path"] = bad
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                render(data)

    def test_rejects_duplicate_entries(self):
        data = copy.deepcopy(self.valid)
        data["apps"].append(copy.deepcopy(data["apps"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            render(data)

    def test_rejects_bad_port_types_and_values(self):
        for port in (True, 80, 65536, "18081", -1):
            data = copy.deepcopy(self.valid)
            data["apps"][0]["upstream_port"] = port
            with self.subTest(port=port), self.assertRaises(ValueError):
                render(data)

    def test_rejects_unknown_fields_and_domain(self):
        data = copy.deepcopy(self.valid)
        data["apps"][0]["proxy_pass"] = "http://evil.example"
        with self.assertRaises(ValueError):
            render(data)
        data = copy.deepcopy(self.valid)
        data["domain"] = "another.example"
        with self.assertRaises(ValueError):
            render(data)

    def test_rejects_nonboolean_websocket_and_bad_unit(self):
        data = copy.deepcopy(self.valid)
        data["apps"][0]["websocket"] = "yes"
        with self.assertRaises(ValueError):
            render(data)
        data = copy.deepcopy(self.valid)
        data["apps"][0]["service_unit"] = "../../root.service"
        with self.assertRaises(ValueError):
            render(data)

    def test_duplicate_json_keys_rejected(self):
        from render_nginx_routes import unique_pairs
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            unique_pairs([("apps", []), ("apps", [])])


if __name__ == "__main__":
    unittest.main()

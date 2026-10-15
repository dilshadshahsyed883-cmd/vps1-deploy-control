import copy
import json
from pathlib import Path
import unittest

from tools.render_nginx_routes import load_registry
from tools.review_nginx_contracts import review, validate_inventory

ROOT = Path(__file__).resolve().parents[1]


class ContractReviewTests(unittest.TestCase):
    def setUp(self):
        self.registry = load_registry(ROOT / "tests/fixtures/nginx-apps.json")
        self.inventory = json.loads((ROOT / "tests/fixtures/nginx-inventory.json").read_text())

    def test_live_empty_never_turns_on_route(self):
        live = load_registry(ROOT / "infrastructure/registry/apps.json")
        result = review(live, None)
        self.assertEqual(result["status"], "NO_LIVE_ROUTES")
        self.assertFalse(result["production_changed"])

    def test_synthetic_match_has_review_diff_not_authority(self):
        out = review(self.registry, self.inventory, fixture=True)
        self.assertEqual(out["routes"], 1)
        self.assertIn("+location ^~ /ci-canary/", out["diff"])
        self.assertTrue(out["needs_vps_activation"])
        self.assertFalse(out["production_changed"])
        with self.assertRaisesRegex(ValueError, "origin mismatch"):
            review(self.registry, self.inventory)

    def test_nonempty_missing_evidence_rejected(self):
        with self.assertRaisesRegex(ValueError, "verified live inventory"):
            review(self.registry, None)

    def test_mismatched_port_blocked(self):
        v = copy.deepcopy(self.inventory)
        v["apps"][0]["upstream_port"] = 65000
        with self.assertRaisesRegex(ValueError, "diverges"):
            review(self.registry, v, fixture=True)

    def test_unowned_static_and_websocket_blocked(self):
        for key, value in [("static_paths", ["/static/"]),
                           ("websocket_path", "/ws")]:
            v = copy.deepcopy(self.inventory)
            v["apps"][0][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "outside owned route"):
                validate_inventory(v, fixture=True)

    def test_strip_requires_prefix_browser_verification(self):
        v = copy.deepcopy(self.inventory)
        v["apps"][0]["prefix_mode"] = "strip"
        with self.assertRaisesRegex(ValueError, "browser verification"):
            validate_inventory(v, fixture=True)

    def test_auth_and_ws_contracts_required(self):
        for key, value in [("auth_policy", "none"), ("websocket_path", None)]:
            v = copy.deepcopy(self.inventory)
            v["apps"][0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_inventory(v, fixture=True)

    def test_evidence_origin_guard(self):
        v = copy.deepcopy(self.inventory)
        v["origin"] = "verified-vps1-agent-report"
        v["evidence_report"] = "/tmp/untrusted.json"
        with self.assertRaisesRegex(ValueError, "canonical agent report"):
            validate_inventory(v)

    def test_production_empty_registry_stays_empty_even_with_inventory(self):
        v = copy.deepcopy(self.inventory)
        v["origin"] = "verified-vps1-agent-report"
        v["evidence_report"] = "Automation_Master_System/VPS_1/02_REPORTS/approved.json"
        with self.assertRaises(ValueError):
            review(load_registry(ROOT / "infrastructure/registry/apps.json"), v)

    def test_reproducible_no_diff_when_same_config(self):
        from tools.render_nginx_routes import render
        before = render(self.registry)
        out = review(self.registry, self.inventory, previous=before, fixture=True)
        self.assertEqual(out["diff"], "")


if __name__ == "__main__":
    unittest.main()

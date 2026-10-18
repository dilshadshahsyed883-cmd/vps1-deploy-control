"""Validate independently evidenced VPS1 HTTPS route records without VPS access."""
import json
import re
import unittest
from pathlib import Path

RECORD = Path(__file__).resolve().parents[1] / "production/verified_https_routes.json"
EXPECTED_NEW = {
    "short-maker": 8146,
    "short-stream": 8145,
    "self-media-library": 8147,
    "video-merger": 8148,
    "local-upload-manager": 8015,
}
EXPECTED_PRESERVED = {"voice-studio": 8143, "auto-caption-studio": 8144}


class VerifiedRouteEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.record = json.loads(RECORD.read_text())

    def test_successful_release_bound_to_exact_proof(self):
        d = self.record
        self.assertEqual(d["github_conclusion"], "success")
        self.assertEqual(d["github_run_id"], 39167302988)
        self.assertEqual(d["git_main_release_sha"], "38c1a07a07a6a9dade2d8cfafe6ca6d6c85e0ed8")
        self.assertEqual(d["github_run_url"],
                         "https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/actions/runs/39167302988")
        self.assertRegex(d["git_main_release_sha"], r"^[0-9a-f]{40}$")

    def test_exact_new_and_existing_routes(self):
        routes = self.record["routes"]
        self.assertEqual(len(routes), 7)
        self.assertEqual({r["app"]: r["upstream_port"] for r in routes if r["route_phase"] == "new"}, EXPECTED_NEW)
        self.assertEqual({r["app"]: r["upstream_port"] for r in routes if r["route_phase"] == "preexisting"}, EXPECTED_PRESERVED)
        self.assertEqual(self.record["new_routes_verified"], 5)
        self.assertEqual(self.record["previous_routes_preserved_and_verified"], 2)
        for r in routes:
            self.assertTrue(r["https_url"].startswith("https://app.scenebuilder.online/"))
            self.assertTrue(r["api_health_url"].startswith("https://app.scenebuilder.online/"))
            self.assertEqual(r["anonymous_http_401"], "VERIFIED")
            self.assertEqual(r["authenticated_http_200_health_and_ui"], "VERIFIED")
            self.assertEqual(r["post_cutover_repeat_check"], "VERIFIED")

    def test_uat_and_offline_app_not_misrepresented(self):
        self.assertEqual(self.record["owner_functional_uat"], "PENDING")
        self.assertEqual(self.record["auto_script"], "OFFLINE")
        self.assertIn("Not end-to-end", self.record["status_scope"])
        self.assertEqual(self.record["app_restarts_during_route_release"], 0)
        for r in self.record["routes"]:
            self.assertEqual(r["owner_functional_uat"], "PENDING")

    def test_no_credential_fields_or_values_in_public_record(self):
        payload = RECORD.read_text().lower()
        for forbidden in ("private_key", "password", "secret_key", "set-cookie",
                          "bearer ", "authorization: basic", "client_secret"):
            self.assertNotIn(forbidden, payload)


if __name__ == "__main__":
    unittest.main()

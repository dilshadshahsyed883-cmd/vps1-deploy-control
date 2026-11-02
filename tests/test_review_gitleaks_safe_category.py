"""Regression: public Gitleaks CI cannot leak unresolved source identifiers."""
import importlib.util
from pathlib import Path
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "tools/review_gitleaks_findings.py"
spec = importlib.util.spec_from_file_location("review_gitleaks_findings", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PublicGitleaksClassificationTests(unittest.TestCase):
    def test_tests_are_only_reported_as_coarse_safe_bucket(self):
        finding = {
            "File": "/repo/rebuild/tests/private-test.py",
            "RuleID": "generic-api-key",
            "Secret": "FAKE_PRIVATE_VALUE_FOR_UNIT_TEST",
            "Commit": "deadbeef" * 5,
            "StartLine": 44,
        }
        self.assertEqual(module.safe_category(finding), ("test", "generic-api-key"))
        detail = ":".join(module.safe_category(finding))
        for secret_input in ("FAKE_PRIVATE_VALUE_FOR_UNIT_TEST", "private-test.py", "deadbeef"):
            self.assertNotIn(secret_input, detail)

    def test_arbitrary_scanner_values_are_never_logged(self):
        finding = {
            "File": "/repo/production/auth_service.py",
            "RuleID": "password\nSECRET_SHOULD_NOT_LEAK",
            "Secret": "OTHER_FAKE_TOKEN",
        }
        self.assertEqual(module.safe_category(finding), ("application", "other_rule"))

    def test_docs_and_foreign_paths_are_coarsened(self):
        self.assertEqual(module.safe_category({"File": "/repo/.github/workflows/foo.yml"}),
                         ("config_or_docs", "other_rule"))
        self.assertEqual(module.safe_category({"File": "/outside/unknown/private.env"}),
                         ("other", "other_rule"))

    def test_non_object_report_item_is_safe(self):
        self.assertEqual(module.safe_category(None), ("invalid", "other"))

    def test_historical_test_evidence_never_discloses_source(self):
        fake_source = (
            "import unittest\n"
            "class Demo(unittest.TestCase):\n"
            "    def test_invalid_source(self):  # fake fixture\n"
            "        token = 'SYNTHETIC_TEST_VALUE'\n"
            "        with self.assertRaises(ValueError):\n"
            "            parse(token)\n"
        ).encode()
        finding = {"File": "/repo/tests/test_legacy.py",
                   "Commit": "a" * 40, "StartLine": 4}
        with mock.patch.object(module.subprocess, "check_output", return_value=fake_source):
            bucket = module.safe_test_evidence(finding, Path("/tmp/repo"))
        self.assertEqual(bucket, "test_synthetic_failure_context")
        self.assertNotIn("SYNTHETIC_TEST_VALUE", bucket)
        self.assertNotIn("test_legacy.py", bucket)
        self.assertNotIn("a" * 40, bucket)

    def test_unknown_commit_evidence_is_never_trusted(self):
        finding = {"File": "/repo/tests/test_legacy.py",
                   "Commit": "malformed", "StartLine": 4}
        self.assertEqual(module.safe_test_evidence(finding, Path("/tmp/repo")),
                         "uninspectable")


if __name__ == "__main__":
    unittest.main()

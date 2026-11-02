"""Regression: public Gitleaks CI cannot leak unresolved source identifiers."""
import importlib.util
from pathlib import Path
import unittest

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


if __name__ == "__main__":
    unittest.main()

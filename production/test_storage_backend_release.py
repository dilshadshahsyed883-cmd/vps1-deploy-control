"""Guardrails for the dedicated Storage Manager backend release lane."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class StorageBackendRelease(unittest.TestCase):
    def test_helper_uses_dedicated_adapter_without_gateway_or_app_fanout(self):
        source = (ROOT / "production/remote_deploy.py").read_text()
        self.assertIn("STORAGE_BACKEND_APP = 'vps-storage-manager'", source)
        self.assertIn("production.storage_backend_release", source)
        self.assertIn("Dedicated lane: preserve the existing candidate unit", source)
        branch = source.split("if selected == STORAGE_BACKEND_APP:", 1)[1]
        self.assertNotIn("production.gateway", branch.split("if operation.startswith", 1)[0])

    def test_adapter_is_pointer_only_and_rolls_back(self):
        source = (ROOT / "production/storage_backend_release.py").read_text()
        for token in (
            'UNIT = "vps1-storage-candidate.service"',
            'ENV = Path("/etc/fresh-website/vps-storage-manager.env")',
            'STATE = Path("/var/lib/fresh-website/vps-storage-manager")',
            "atomic_link(CURRENT, target)",
            "_rollback(previous)",
            "backup_databases(STATE",
            '"/v1/metrics"',
        ):
            self.assertIn(token, source)
        self.assertIn("POINTER_AND_SERVICE_PRESERVED", source)
        self.assertNotIn("systemctl stop", source)
        self.assertNotIn("production.gateway", source)

    def test_backend_contract_still_excludes_legacy_metric_fields(self):
        source = (ROOT / "apps/vps-storage-manager/part01/storage_manager.py").read_text()
        for token in ('"services"', '"service_details"', '"startable_services"', '"processes"'):
            # These are allowlist checks in the production adapter only; the backend
            # must not return any of them from metrics.
            self.assertNotIn(token, source[source.index("def metrics()"):source.index("def backup_database")])
        self.assertIn('if path == "/v1/metrics":', source)
        self.assertIn('if path == "/v1/full-production-clean/status":', source)


if __name__ == "__main__":
    unittest.main()

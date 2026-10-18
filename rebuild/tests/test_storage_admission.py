import unittest
from rebuild import storage_admission

MODEL = {"bytes": 100, "users": ["short-maker", "voice-studio"], "sha256": "a" * 64}

class StoragePlanTests(unittest.TestCase):
    def setUp(self):
        self.ledger = {"apps": {"short-maker": 300, "voice-studio": 450},
                       "shared_uv_bytes": 200,
                       "shared_models": {"model-v1": dict(MODEL)}}

    def test_add_only_one_app(self):
        out = storage_admission.plan("install", "short-stream", self.ledger, 7_000_000_000, 1000)
        self.assertTrue(out["admitted"])
        self.assertEqual(out["projected_bytes"], 2050)
        self.assertFalse(out["destructive_actions"])

    def test_only_fails_on_disk_reserve(self):
        self.assertFalse(storage_admission.plan("install", "short-stream", self.ledger, 5_000_000_000, 1000)["admitted"])
        self.assertTrue(storage_admission.plan("install", "short-stream", self.ledger, 60_000_000_000, 40_000_000_000)["admitted"])
        self.assertFalse(storage_admission.plan("install", "short-stream", self.ledger, 42_000_000_000, 40_000_000_000)["admitted"])

    def test_remove_does_not_erase_shared_model(self):
        out = storage_admission.plan("remove", "short-maker", self.ledger, 6_000_000_000)
        self.assertEqual(out["estimated_reclaimable_app_bytes"], 300)
        self.assertEqual(out["retained_models"], ["model-v1"])
        self.assertEqual(out["shared_resources_deleted"], [])
        self.assertTrue(out["requires_backup_and_agent"])

    def test_no_offline_or_untracked(self):
        with self.assertRaises(ValueError):
            storage_admission.plan("install", "auto-script", self.ledger, 8_000_000_000)
        with self.assertRaises(ValueError):
            storage_admission.plan("remove", "short-stream", self.ledger, 8_000_000_000)

    def test_no_invalid_model_ownership(self):
        self.ledger["shared_models"]["model-v1"]["users"] = ["auto-script"]
        with self.assertRaises(ValueError):
            storage_admission.plan("remove", "short-maker", self.ledger, 8_000_000_000)

if __name__ == "__main__":
    unittest.main()

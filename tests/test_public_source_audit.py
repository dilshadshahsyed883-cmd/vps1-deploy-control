"""Unit tests for public data gate (no VPS/external data)."""
from pathlib import Path
import tempfile
import unittest
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from audit_public_source import audit, classify_path


class PublicInventoryAuditTest(unittest.TestCase):
    def test_fails_on_mutable_and_credential_artifacts(self):
        for name in (".env", "dir/.env.production", "user.sqlite", "id_ed25519", "clip.mp4", "backup.zip", "myuser/.ssh/config"):
            with self.subTest(name=name):
                self.assertIsNotNone(classify_path(name))

    def test_allows_source_files_and_ordinary_metadata(self):
        for name in ("apps/myapp/server.py", "rebuild/snapshots/live-source-manifest.json", "README.md"):
            self.assertIsNone(classify_path(name))

    def test_flags_private_data_in_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            p = root / "apps/app/.acs_backups/snapshot/README.txt"
            p.parent.mkdir(parents=True)
            p.write_text("Contact: person@company.test\\n", encoding="utf-8")
            _, categories = audit(root, [str(p.relative_to(root))])
            self.assertIn("personally_identifying_contact_in_manifest_or_backup", categories)

    def test_service_home_path_is_not_personal_email(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            p = root / "rebuild/snapshots/sample.json"
            p.parent.mkdir(parents=True)
            p.write_text('{"working_directory": "/home/ubuntu/apps/example"}', encoding="utf-8")
            count, categories = audit(root, [str(p.relative_to(root))])
            self.assertEqual(count, 1)
            self.assertFalse(categories)

    def test_blocks_other_home_user(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            p = root / "rebuild/snapshots/sample.json"
            p.parent.mkdir(parents=True)
            p.write_text('{"working_directory": "/home/personaluser/private"}', encoding="utf-8")
            _, categories = audit(root, [str(p.relative_to(root))])
            self.assertIn("nonservice_user_home_path_in_manifest_or_backup", categories)


if __name__ == "__main__":
    unittest.main()

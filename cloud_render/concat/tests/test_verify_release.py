"""Offline tests for the Concat official Windows asset verifier."""
import hashlib
import tempfile
import unittest
from pathlib import Path

from cloud_render.concat.verify_release import expected_digest, verify


class ConcatVerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.installer = self.root / "Concat-0.2.8-windows-x86_64-setup.exe"
        self.sums = self.root / "SHA256SUMS"
        self.installer.write_bytes(b"MZ" + b"x" * 1_000_001)
        self.digest = hashlib.sha256(self.installer.read_bytes()).hexdigest()
        self.sums.write_text(f"{self.digest}  Concat-0.2.8-windows-x86_64-setup.exe\n", encoding="utf-8")

    def test_valid(self):
        self.assertEqual(verify(self.installer, self.sums), self.digest)

    def test_checksum_mismatch(self):
        self.sums.write_text("0" * 64 + "  Concat-0.2.8-windows-x86_64-setup.exe\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Checksum mismatch"):
            verify(self.installer, self.sums)

    def test_reject_missing_entry(self):
        self.sums.write_text("0" * 64 + "  other.exe\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "exactly one"):
            verify(self.installer, self.sums)

    def test_reject_duplicate_entry(self):
        self.sums.write_text(self.sums.read_text() * 2, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "exactly one"):
            verify(self.installer, self.sums)

    def test_reject_non_exe_file(self):
        self.installer.write_bytes(b"ZZ" + b"x" * 1_000_001)
        with self.assertRaisesRegex(ValueError, "MZ"):
            verify(self.installer, self.sums)

    def test_reject_small_file(self):
        self.installer.write_bytes(b"MZ")
        with self.assertRaisesRegex(ValueError, "small"):
            verify(self.installer, self.sums)

    def test_accept_starred_path_in_sums(self):
        self.assertEqual(
            expected_digest(self.digest + " *./Concat-0.2.8-windows-x86_64-setup.exe\n"), self.digest
        )


if __name__ == "__main__":
    unittest.main()

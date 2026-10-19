"""Static safety contracts for per-app Drive-verified cleanup (no production IO)."""
import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]

class PerAppCleanupContracts(unittest.TestCase):
    def source(self, path):
        content = (ROOT / path).read_text(encoding="utf-8")
        ast.parse(content, filename=path)
        return content

    def test_short_maker_only_after_verified_delivery(self):
        s = self.source("apps/short-maker/part01/app.py")
        self.assertIn("verified=verify_drive_file(local_path,remote_file)", s)
        self.assertIn('verified_clean=True  # Master rule:', s)
        self.assertIn('"temp", "tmp"', s)
        self.assertIn("target.is_symlink()", s)

    def test_acs_requires_all_sibling_jobs_verified(self):
        s = self.source("apps/auto-caption-studio/part01/web_backend/pipeline_v3.py")
        self.assertIn("SELECT status,drive_status,drive_verified,upload_verified,", s)
        self.assertIn("all(sibling_safe(item) for item in sibling_jobs)", s)
        self.assertIn("root.parent != projects_root().resolve()", s)
        self.assertIn('not bool(meta.get("web_preview_reference", False))', s)

    def test_library_never_bypasses_remote_check(self):
        s = self.source("apps/self-media-library/part01/app.py")
        self.assertIn("Drive verification cannot be bypassed", s)
        self.assertIn('verify = True  # Mandatory per-app remote readback', s)

    def test_merger_checks_exact_remote_bytes(self):
        s = self.source("apps/video-merger/part01/app.py")
        self.assertIn("size!=local_size", s)
        self.assertIn("shutil.rmtree(ws,ignore_errors=True)", s)

    def test_rules_preserve_non_output_engines(self):
        doc = (ROOT / "docs/PER_APP_OUTPUT_LIFECYCLE_V1.md").read_text(encoding="utf-8")
        for keyword in ("Auto Script OFFLINE", "active/retry/consumer", "Storage Manager", "all sibling"):
            self.assertIn(keyword, doc)

if __name__ == "__main__":
    unittest.main()

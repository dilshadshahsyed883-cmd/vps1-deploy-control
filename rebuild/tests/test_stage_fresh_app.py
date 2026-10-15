"""Non-production deployment artifact integrity and rollback unit tests."""
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from scripts import stage_fresh_app

SHA_A = "a" * 40
SHA_B = "b" * 40

class StageAppSafetyTest(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(t.cleanup)
        self.root=Path(t.name)

    def archive(self, sha, body=b"test payload"):
        archive=self.root / ("short-maker-"+sha[:12]+".tar.gz")
        with tarfile.open(archive,"w:gz") as tar:
            entry=tarfile.TarInfo("short-maker/part01/app.py")
            entry.size=len(body)
            tar.addfile(entry,io.BytesIO(body))
        manifest=self.root / ("short-maker-"+sha[:12]+".tar.gz.manifest.json")
        manifest.write_text(json.dumps({
            "app":"short-maker","commit_sha":sha,"archive":archive.name,
            "sha256":hashlib.sha256(archive.read_bytes()).hexdigest(),
            "files":[{"path":"part01/app.py","sha256":hashlib.sha256(body).hexdigest()}]
        }))
        return archive,manifest

    def test_stage_two_revisions_and_rollback(self):
        a,am=self.archive(SHA_A)
        self.assertEqual(stage_fresh_app.stage("short-maker",SHA_A,a,am,root=self.root/"stage")["status"],"STAGED_ONLY")
        b,bm=self.archive(SHA_B, b"second payload")
        self.assertEqual(stage_fresh_app.stage("short-maker",SHA_B,b,bm,root=self.root/"stage")["status"],"STAGED_ONLY")
        self.assertEqual(stage_fresh_app.rollback("short-maker",root=self.root/"stage")["revision"],SHA_A)
        self.assertEqual((self.root/"stage/current/short-maker/part01/app.py").read_bytes(),b"test payload")

    def test_offline_app_blocked(self):
        a,am=self.archive(SHA_A)
        with self.assertRaises(ValueError):
            stage_fresh_app.stage("auto-script", SHA_A, a, am, root=self.root/"stage")

    def test_corrupt_archive_rejected(self):
        a,am=self.archive(SHA_A)
        a.write_bytes(a.read_bytes()+b"tamper")
        with self.assertRaises(ValueError):
            stage_fresh_app.stage("short-maker", SHA_A, a, am,root=self.root/"stage")

    def test_no_restart_or_production_management_in_source(self):
        body=Path(stage_fresh_app.__file__).read_text()
        self.assertNotIn("systemctl", body)
        self.assertNotIn("nginx -s", body)

if __name__=="__main__":
    unittest.main()

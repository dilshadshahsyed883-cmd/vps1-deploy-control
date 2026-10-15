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


    def test_audit_matches_commit_and_files(self):
        a, am = self.archive(SHA_A)
        stage_fresh_app.stage("short-maker", SHA_A, a, am, root=self.root/"stage")
        result = stage_fresh_app.audit("short-maker", SHA_A, a, am, root=self.root/"stage")
        self.assertEqual(result["status"], "STAGING_AUDIT_PASS")
        self.assertEqual(result["revision"], SHA_A)

    def test_audit_detects_tampering(self):
        a, am = self.archive(SHA_A)
        stage_fresh_app.stage("short-maker", SHA_A, a, am, root=self.root/"stage")
        p = self.root/"stage/current/short-maker/part01/app.py"
        p.write_text("tampered")
        with self.assertRaises(ValueError):
            stage_fresh_app.audit("short-maker", SHA_A, a, am, root=self.root/"stage")

    def test_audit_detects_wrong_active_revision(self):
        a, am = self.archive(SHA_A)
        stage_fresh_app.stage("short-maker", SHA_A, a, am, root=self.root/"stage")
        b, bm = self.archive(SHA_B)
        stage_fresh_app.stage("short-maker", SHA_B, b, bm, root=self.root/"stage")
        with self.assertRaises(ValueError):
            stage_fresh_app.audit("short-maker", SHA_A, a, am, root=self.root/"stage")

    def test_audit_detects_extra_file(self):
        a, am = self.archive(SHA_A)
        stage_fresh_app.stage("short-maker", SHA_A, a, am, root=self.root/"stage")
        (self.root/"stage/current/short-maker/rogue.py").write_text("rogue")
        with self.assertRaises(ValueError):
            stage_fresh_app.audit("short-maker", SHA_A, a, am, root=self.root/"stage")

    def test_local_drift_clean_revision(self):
        a, am = self.archive(SHA_A)
        stage_fresh_app.stage("short-maker", SHA_A, a, am, root=self.root/"stage")
        x = stage_fresh_app.inspect_active("short-maker", revision=SHA_A, root=self.root/"stage")
        self.assertEqual(x["status"], "STAGING_LOCAL_DRIFT_CHECK_PASS")
        self.assertFalse(x["github_manifest_compared"])

    def test_local_drift_wrong_expected_sha(self):
        a, am = self.archive(SHA_A)
        stage_fresh_app.stage("short-maker", SHA_A, a, am, root=self.root/"stage")
        with self.assertRaises(ValueError):
            stage_fresh_app.inspect_active("short-maker", revision=SHA_B, root=self.root/"stage")

    def test_local_drift_tampered_file(self):
        a, am = self.archive(SHA_A)
        stage_fresh_app.stage("short-maker", SHA_A, a, am, root=self.root/"stage")
        (self.root/"stage/current/short-maker/part01/app.py").write_text("tampered")
        with self.assertRaises(ValueError):
            stage_fresh_app.inspect_active("short-maker", root=self.root/"stage")

    def test_local_drift_added_file(self):
        a, am = self.archive(SHA_A)
        stage_fresh_app.stage("short-maker", SHA_A, a, am, root=self.root/"stage")
        (self.root/"stage/current/short-maker/new.py").write_text("rogue")
        with self.assertRaises(ValueError):
            stage_fresh_app.inspect_active("short-maker", root=self.root/"stage")

    def test_local_drift_modified_manifest(self):
        a, am = self.archive(SHA_A)
        stage_fresh_app.stage("short-maker", SHA_A, a, am, root=self.root/"stage")
        mp = self.root/"stage/current/short-maker/_staging_manifest.json"
        data = json.loads(mp.read_text())
        data["files"]["part01/app.py"] = "f"*64
        mp.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            stage_fresh_app.inspect_active("short-maker", root=self.root/"stage")

    def test_local_drift_offline_app_blocked(self):
        with self.assertRaises(ValueError):
            stage_fresh_app.inspect_active("auto-script", root=self.root/"stage")

    def test_stage_refuses_symlinked_release_directory(self):
        a, am = self.archive(SHA_A)
        stage = self.root/"stage"
        stage.mkdir()
        (stage/"releases").symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            stage_fresh_app.stage("short-maker", SHA_A, a, am, root=stage)
        self.assertFalse((self.root/"short-maker").exists())

    def test_stage_refuses_symlinked_app_directory(self):
        a, am = self.archive(SHA_A)
        stage = self.root/"stage"
        (stage/"releases").mkdir(parents=True)
        (stage/"releases/short-maker").symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            stage_fresh_app.stage("short-maker", SHA_A, a, am, root=stage)

    def test_stage_refuses_symlinked_current_and_previous_dir(self):
        a, am = self.archive(SHA_A)
        for name in ("current", "previous"):
            with self.subTest(name=name):
                stage = self.root/("stage-"+name)
                stage.mkdir()
                (stage/name).symlink_to(self.root, target_is_directory=True)
                with self.assertRaisesRegex(ValueError, "symlink"):
                    stage_fresh_app.stage("short-maker", SHA_A, a, am, root=stage)

    def test_rollback_refuses_symlinked_previous_container(self):
        a, am = self.archive(SHA_A)
        stage = self.root/"stage"
        stage_fresh_app.stage("short-maker", SHA_A, a, am, root=stage)
        other = self.root/"other"
        other.mkdir()
        (stage/"previous").rmdir()
        (stage/"previous").symlink_to(other, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            stage_fresh_app.rollback("short-maker", root=stage)

    def test_no_restart_or_production_management_in_source(self):
        body=Path(stage_fresh_app.__file__).read_text()
        self.assertNotIn("systemctl", body)
        self.assertNotIn("nginx -s", body)

if __name__=="__main__":
    unittest.main()

"""Sanitized cross-service Short Maker/Uploader production config preflight."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/"production/shortstudio_cooperation_readonly.py"

def load():
    spec=importlib.util.spec_from_file_location("shortstudio_coop_audit",SCRIPT)
    mod=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class CooperationReadOnlyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.unit_dir=self.root/"systemd"
        self.unit_dir.mkdir()
        self.folder=self.root/"shared"
        self.folder.mkdir()
        self.module=load()
        self.envfiles={}
        for index,(kind,name) in enumerate(self.module.SERVICES.items()):
            envfile=self.root/(kind+".env")
            enabled=self.module.REQUIRED_KEYS[kind]
            envfile.write_text("GOOGLE_CLIENT_SECRET=do-not-print\n"
                "PRIVATE_TOKEN=protected-token\n"
                "SHORT_STUDIO_VPS_FOLDER_ROOT="+str(self.folder)+"\n"
                +enabled+"=1\n",encoding="utf-8")
            (self.unit_dir/name).write_text(
                "[Service]\nEnvironmentFile="+str(envfile)+"\n",encoding="utf-8")
            self.envfiles[kind]=envfile

    def tearDown(self):
        self.tmp.cleanup()

    def result(self):
        return self.module.inspect(self.unit_dir)

    def test_all_four_services_need_identical_host_root_and_prove_no_release(self):
        result=self.result()
        self.assertTrue(result["same_host_root_proven"])
        self.assertTrue(result["lock_flags_consistent"])
        self.assertTrue(result["all_guards_enabled"])
        self.assertEqual(result["status"],"HOST_CONFIGURATION_READY_FOR_LIVE_GATES")
        self.assertFalse(result["retirement_enabled"])
        self.assertFalse(result["production_activation_ready"])
        self.assertFalse(result["live_mount_namespace_verified"])
        self.assertFalse(result["active_upload_drain_verified"])
        self.assertFalse(result["two_user_isolation_verified"])
        encoded=json.dumps(result)
        self.assertNotIn("do-not-print",encoded)
        self.assertNotIn("protected-token",encoded)
        self.assertNotIn(str(self.folder),encoded)
        self.assertNotIn(str(self.unit_dir),encoded)

    def test_root_drift_blocks_and_cannot_be_overridden_by_enabled_flags(self):
        other=self.root/"foreign"
        other.mkdir()
        self.envfiles["worker"].write_text(
            "SHORT_STUDIO_VPS_FOLDER_ROOT="+str(other)+"\n"
            "LUM_SHORT_STUDIO_COOPERATIVE_LOCK_ENABLED=1\n")
        result=self.result()
        self.assertFalse(result["same_host_root_proven"])
        self.assertFalse(result["production_activation_ready"])
        self.assertEqual(result["status"],"BLOCKED")

    def test_disabled_or_missing_worker_guard_blocks_lock_consistency(self):
        text=self.envfiles["scheduler"].read_text()
        self.envfiles["scheduler"].write_text(text.replace(
            "LUM_SHORT_STUDIO_COOPERATIVE_LOCK_ENABLED=1",
            "LUM_SHORT_STUDIO_COOPERATIVE_LOCK_ENABLED=0"))
        report=self.result()
        self.assertFalse(report["lock_flags_consistent"])
        self.assertFalse(report["all_guards_enabled"])
        self.assertEqual(report["status"],"BLOCKED")

    def test_missing_unit_or_envfile_refuses_to_guess(self):
        (self.unit_dir/self.module.SERVICES["worker"]).unlink()
        report=self.result()
        self.assertFalse(report["same_host_root_proven"])
        self.assertFalse(report["units"]["worker"]["configuration_present"])

    def test_symlinked_root_or_configuration_refused(self):
        alias=self.root/"linked"
        alias.symlink_to(self.folder,target_is_directory=True)
        self.envfiles["maker"].write_text(
            "SHORT_STUDIO_VPS_FOLDER_ROOT="+str(alias)+"\n"
            "SHORT_MAKER_COOPERATIVE_LOCK_ENABLED=1\n")
        self.assertFalse(self.result()["same_host_root_proven"])
        self.envfiles["maker"].unlink()
        self.envfiles["maker"].symlink_to(self.envfiles["worker"])
        report=self.result()
        self.assertFalse(report["units"]["maker"]["configuration_present"])

    def test_duplicate_config_keys_and_optional_envfiles_fail_closed(self):
        with self.envfiles["worker"].open("a") as fd:
            fd.write("SHORT_STUDIO_VPS_FOLDER_ROOT="+str(self.folder)+"\n")
        self.assertEqual(self.result()["status"],"BLOCKED")
        self.envfiles["worker"].write_text(
            "SHORT_STUDIO_VPS_FOLDER_ROOT="+str(self.folder)+"\n"
            "LUM_SHORT_STUDIO_COOPERATIVE_LOCK_ENABLED=1\n")
        unit=self.unit_dir/self.module.SERVICES["worker"]
        unit.write_text(unit.read_text().replace("EnvironmentFile=",
                                               "EnvironmentFile=-"))
        self.assertFalse(self.result()["same_host_root_proven"])

    def test_service_unit_names_match_canonical_runtime_and_shortstudio_release(self):
        runtime=(ROOT/"production/runtime.py").read_text(encoding="utf-8")
        short_release=(ROOT/"production/short_uploader_group_release.py").read_text(
            encoding="utf-8")
        self.assertIn("vps1-production-lum-shortstudio-",short_release)
        self.assertIn("vps1-production-{app}",runtime)
        self.assertEqual(set(self.module.SERVICES),{"maker","web","worker","scheduler"})


if __name__=="__main__":
    unittest.main()

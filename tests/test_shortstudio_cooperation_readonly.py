"""Sanitized cross-service Short Maker/Uploader production config preflight."""
import importlib.util
import json
import sqlite3
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

    def test_generated_unit_path_environment_is_not_a_false_blocker(self):
        for name in self.module.SERVICES.values():
            unit=self.unit_dir/name
            unit.write_text(unit.read_text()
                +"Environment=PATH=/usr/local/bin:/usr/bin:/bin\\n")
        result=self.result()
        self.assertTrue(result["same_host_root_proven"])
        self.assertEqual(result["status"],"HOST_CONFIGURATION_READY_FOR_LIVE_GATES")

    def test_all_disabled_consistent_guards_are_still_blocked(self):
        for kind,env in self.envfiles.items():
            text=env.read_text()
            env.write_text(text.replace(
                self.module.REQUIRED_KEYS[kind]+"=1",
                self.module.REQUIRED_KEYS[kind]+"=0"))
        result=self.result()
        self.assertTrue(result["same_host_root_proven"])
        self.assertTrue(result["lock_flags_consistent"])
        self.assertFalse(result["all_guards_enabled"])
        self.assertEqual(result["status"],"BLOCKED")

    def test_direct_systemd_environment_or_drop_in_cannot_override_ro_proof(self):
        unit=self.unit_dir/self.module.SERVICES["worker"]
        unit.write_text(unit.read_text()+"Environment=SHORT_STUDIO_VPS_FOLDER_ROOT=/tmp/other\\n")
        self.assertEqual(self.result()["status"],"BLOCKED")
        unit.write_text(unit.read_text().split("Environment=SHORT_STUDIO")[0])
        dropin=Path(str(unit)+".d")
        dropin.mkdir()
        (dropin/"override.conf").write_text(
            "[Service]\\nEnvironment=SHORT_STUDIO_VPS_FOLDER_ROOT=/tmp/other\\n")
        self.assertEqual(self.result()["status"],"BLOCKED")

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

    def live_fake_proc(self):
        proc=self.root/"proc"
        proc.mkdir()
        names=list(self.module.SERVICES)
        pids=dict(zip(names,range(4100,4100+len(names))))
        for kind in names:
            pid=pids[kind]
            p=proc/str(pid)
            p.mkdir()
            # /proc/PID/root points into a process mount namespace. A root
            # symlink to "/" models same-mount identity without real processes.
            (p/"root").symlink_to("/",target_is_directory=True)
            chunks=[
                b"DO_NOT_LOG_TOKEN=secret-binary-content",
                b"SHORT_STUDIO_VPS_FOLDER_ROOT="+str(self.folder).encode("utf-8"),
                self.module.REQUIRED_KEYS[kind].encode("ascii")+b"=1"]
            if kind!="maker":
                chunks.append(b"LUM_ENGINE_INSTANCE=shortstudio")
            (p/"environ").write_bytes(b"\x00".join(chunks)+b"\x00")
        finder=lambda unit: pids[next(k for k,v in self.module.SERVICES.items() if v==unit)]
        return proc,pids,finder

    def test_actual_process_mount_and_env_match_is_sanitized_but_not_activation(self):
        proc,pids,finder=self.live_fake_proc()
        result=self.module.inspect_live_processes(self.unit_dir,proc,pid_lookup=finder)
        self.assertEqual(result["status"],"LIVE_MOUNT_AND_FLAGS_PROVEN")
        self.assertTrue(result["four_processes_same_mount_verified"])
        self.assertTrue(result["four_processes_lock_flags_verified"])
        self.assertFalse(result["active_upload_drain_verified"])
        self.assertFalse(result["production_activation_ready"])
        serialized=json.dumps(result)
        for sensitive in ("secret-binary-content",str(self.folder),str(self.unit_dir),
                          str(pids["maker"])):
            self.assertNotIn(sensitive,serialized)

    def test_running_worker_with_disabled_guard_fails_closed(self):
        proc,pids,finder=self.live_fake_proc()
        worker=proc/str(pids["worker"])/"environ"
        worker.write_bytes(worker.read_bytes().replace(
            b"LUM_SHORT_STUDIO_COOPERATIVE_LOCK_ENABLED=1",
            b"LUM_SHORT_STUDIO_COOPERATIVE_LOCK_ENABLED=0"))
        result=self.module.inspect_live_processes(self.unit_dir,proc,pid_lookup=finder)
        self.assertEqual(result["status"],"BLOCKED")
        self.assertFalse(result["four_processes_same_mount_verified"])
        self.assertFalse(result["four_processes_lock_flags_verified"])

    def test_missing_process_or_mismatched_root_is_blocked(self):
        proc,pids,finder=self.live_fake_proc()
        (proc/str(pids["scheduler"])/"environ").unlink()
        result=self.module.inspect_live_processes(self.unit_dir,proc,pid_lookup=finder)
        self.assertEqual(result["status"],"BLOCKED")
        self.assertFalse(result["units"]["scheduler"]["process_root_verified"])
        # Recreate the scheduler env with a path that differs from host root.
        scheduler=proc/str(pids["scheduler"])/"environ"
        scheduler.write_bytes(b"SHORT_STUDIO_VPS_FOLDER_ROOT=/different-root\x00"
            b"LUM_SHORT_STUDIO_COOPERATIVE_LOCK_ENABLED=1\x00"
            b"LUM_ENGINE_INSTANCE=shortstudio\x00")
        bad=self.module.inspect_live_processes(self.unit_dir,proc,pid_lookup=finder)
        self.assertEqual(bad["status"],"BLOCKED")

    def _drain_fixture(self):
        maker = self.root / "maker_data"
        jobs = maker / "jobs"
        jobs.mkdir(parents=True)
        job = jobs / "job-001"
        job.mkdir()
        state = job / "state.json"
        state.write_text(json.dumps({"status": "completed",
                                     "owner": "PRIVATE_OWNER"}))
        ready = maker / "ready_video_uploads"
        ready.mkdir()
        session = ready / "session-001"
        session.mkdir()
        manifest = session / "manifest.json"
        manifest.write_text(json.dumps({"status": "completed",
                                        "owner": "PRIVATE_OWNER"}))
        appdb = self.root / "uploader_app.db"
        enginedb = self.root / "uploader_engine.db"
        with sqlite3.connect(appdb) as db:
            db.execute("CREATE TABLE videos(id INTEGER, status TEXT)")
            db.execute("INSERT INTO videos VALUES(1, 'COMPLETED')")
        with sqlite3.connect(enginedb) as db:
            db.execute("CREATE TABLE job_sources(id INTEGER, state TEXT)")
            db.execute("INSERT INTO job_sources VALUES(1, 'COMPLETE')")
        with self.envfiles["maker"].open("a") as out:
            out.write("SHORT_MAKER_DATA=" + str(maker) + "\n")
            out.write("SHORT_UPLOADER_APP_DB=" + str(appdb) + "\n")
            out.write("SHORT_UPLOADER_ENGINE_DB=" + str(enginedb) + "\n")
        return maker, state, manifest, appdb, enginedb

    def test_drain_snapshot_sanitized_and_never_authorizes_production(self):
        self._drain_fixture()
        result = self.module.inspect_active_drain(self.unit_dir)
        self.assertEqual(result["status"], "SNAPSHOT_NO_ACTIVE_UPLOADS")
        self.assertTrue(result["active_upload_drain_snapshot_verified"])
        self.assertTrue(result["maker_jobs_idle"])
        self.assertTrue(result["ready_imports_idle"])
        self.assertTrue(result["uploader_jobs_idle"])
        self.assertTrue(result["uploader_source_claims_idle"])
        self.assertFalse(result["active_upload_drain_verified"])
        self.assertFalse(result["production_activation_ready"])
        encoded = json.dumps(result)
        self.assertNotIn("PRIVATE_OWNER", encoded)
        self.assertNotIn(str(self.root), encoded)
        self.assertNotIn("secret", encoded)

    def test_drain_snapshot_blocks_running_maker_paused_import_and_uploader_work(self):
        _, state, manifest, appdb, enginedb = self._drain_fixture()
        def report():
            return self.module.inspect_active_drain(self.unit_dir)
        state.write_text('{"status":"running"}')
        self.assertEqual(report()["status"], "BLOCKED")
        state.write_text('{"status":"completed"}')
        manifest.write_text('{"status":"paused"}')
        self.assertEqual(report()["status"], "BLOCKED")
        manifest.write_text('{"status":"completed"}')
        with sqlite3.connect(appdb) as db:
            db.execute("UPDATE videos SET status='UPLOADING'")
        self.assertFalse(report()["uploader_jobs_idle"])
        with sqlite3.connect(appdb) as db:
            db.execute("UPDATE videos SET status='COMPLETED'")
        with sqlite3.connect(enginedb) as db:
            db.execute("UPDATE job_sources SET state='CLAIMED'")
        self.assertFalse(report()["uploader_source_claims_idle"])
        with sqlite3.connect(enginedb) as db:
            db.execute("UPDATE job_sources SET state='COMPLETE'")
        self.assertTrue(report()["active_upload_drain_snapshot_verified"])

    def test_drain_snapshot_rejects_unknown_db_missing_state_and_path_override(self):
        maker, state, _, appdb, _ = self._drain_fixture()
        state.unlink()
        self.assertFalse(self.module.inspect_active_drain(
            self.unit_dir)["active_upload_drain_snapshot_verified"])
        state.write_text('{"status":"completed"}')
        appdb.unlink()
        self.assertEqual(self.module.inspect_active_drain(self.unit_dir)["status"],
                         "BLOCKED")
        # A sensitive path override in a systemd unit invalidates the proof.
        unit = self.unit_dir / self.module.SERVICES["maker"]
        unit.write_text(unit.read_text() +
                        "Environment=SHORT_UPLOADER_APP_DB=/tmp/untrusted\n")
        self.assertEqual(self.module.inspect_active_drain(self.unit_dir)["status"],
                         "BLOCKED")
        self.assertFalse(self.module.inspect_active_drain(
            self.unit_dir)["production_activation_ready"])

    def test_service_unit_names_match_canonical_runtime_and_shortstudio_release(self):
        runtime=(ROOT/"production/runtime.py").read_text(encoding="utf-8")
        short_release=(ROOT/"production/short_uploader_group_release.py").read_text(
            encoding="utf-8")
        self.assertIn("vps1-production-lum-shortstudio-",short_release)
        self.assertIn("vps1-production-{app}",runtime)
        self.assertEqual(set(self.module.SERVICES),{"maker","web","worker","scheduler"})


if __name__=="__main__":
    unittest.main()

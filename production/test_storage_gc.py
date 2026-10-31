"""Safety regression tests for VPS1 12-hour deployment artifact cleanup."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from production import storage_gc as gc


class StorageGCTests(unittest.TestCase):
    def test_esrch_kernel_thread_requires_strong_resource_free_proof(self):
        """Live Kthread with no user-space resources must not block every release."""
        import errno
        from unittest.mock import patch

        proc = Path(self.workspace.name) / "proc-kthreads"
        pid = proc / "2"
        (pid / "fd").mkdir(parents=True)
        (pid / "cmdline").write_bytes(b"")
        (pid / "maps").write_bytes(b"")
        (pid / "status").write_text("Name:\tkthreadd\nKthread:\t1\n")
        target = self.root / "releases" / self.app / self.revisions[3]

        def stat_line(flags=0x00200000, start=12345):
            fields = (["S", "0"] + ["0"] * 4 + [str(flags)]
                      + ["0"] * 12 + [str(start)] + ["0"] * 4)
            return f"{pid.name} (kthreadd) " + " ".join(fields)

        (pid / "stat").write_text(stat_line())
        original = Path.read_bytes

        def process_read_bytes(path):
            if path == pid / "environ":
                raise ProcessLookupError(errno.ESRCH, "kernel process without user mm")
            return original(path)

        with patch.object(Path, "read_bytes", autospec=True, side_effect=process_read_bytes):
            self.assertEqual(gc._runtime_reference_probe([target], proc_root=proc),
                             "clear")

            # Missing or contradictory kernel identity always blocks deletion.
            (pid / "status").write_text("Name:\tkthreadd\nKthread:\t0\n")
            self.assertEqual(gc._runtime_reference_probe([target], proc_root=proc),
                             "unverifiable-process-race")
            (pid / "status").write_text("Kthread:\t1\n")
            (pid / "stat").write_text(stat_line(flags=0))
            self.assertTrue(gc._runtime_in_use([target], proc_root=proc))
            (pid / "stat").write_text(stat_line())

            # Any FD, mapping, or cwd reference defeats the exemption.
            (pid / "fd" / "4").symlink_to(target)
            self.assertTrue(gc._runtime_in_use([target], proc_root=proc))
            (pid / "fd" / "4").unlink()
            (pid / "maps").write_bytes(os.fsencode(target))
            self.assertTrue(gc._runtime_in_use([target], proc_root=proc))
            (pid / "maps").write_bytes(b"")
            (pid / "cwd").symlink_to(target)
            self.assertTrue(gc._runtime_in_use([target], proc_root=proc))
            (pid / "cwd").unlink()

            # Real userspace references still win over kernel-thread detection.
            (pid / "cmdline").write_bytes(os.fsencode(target) + b"/app.py")
            self.assertEqual(gc._runtime_reference_probe([target], proc_root=proc),
                             "referenced")

    def test_kernel_thread_pid_reuse_and_unreadable_status_fail_closed(self):
        import errno
        from unittest.mock import patch

        proc = Path(self.workspace.name) / "proc-kthread-race"
        pid = proc / "2"
        (pid / "fd").mkdir(parents=True)
        (pid / "maps").write_bytes(b"")
        (pid / "status").write_text("Kthread:\t1\n")
        target = self.root / "releases" / self.app / self.revisions[3]

        def stat_line(start):
            fields = (["I", "0"] + ["0"] * 4 + [str(0x00200000)]
                      + ["0"] * 12 + [str(start)] + ["0"] * 4)
            return f"{pid.name} (kworker) " + " ".join(fields)

        (pid / "stat").write_text(stat_line(12345))
        original_read_text = Path.read_text
        count = [0]

        def changed_stat(path, *args, **kwargs):
            if path == pid / "stat":
                count[0] += 1
                return stat_line(12345 if count[0] == 1 else 12346)
            return original_read_text(path, *args, **kwargs)

        self.assertEqual(gc._confirmed_resource_free_kthread(pid), True)
        with patch.object(Path, "read_text", autospec=True, side_effect=changed_stat):
            self.assertEqual(gc._confirmed_resource_free_kthread(pid),
                             "identity-changed")

        (pid / "status").unlink()
        self.assertFalse(gc._confirmed_resource_free_kthread(pid))

    def test_transient_proc_race_requires_clean_independent_rescan(self):
        from unittest.mock import patch

        target = self.root / "releases" / self.app / self.revisions[3]
        with patch.object(gc, "_runtime_reference_pass",
                          side_effect=["unverifiable-process-race", "clear"]) as scan, \
             patch.object(gc.time, "sleep") as pause:
            self.assertEqual(gc._runtime_reference_probe([target]), "clear")
            self.assertEqual(scan.call_count, 2)
            pause.assert_called_once_with(0.05)

        # A second scan that positively references a release always blocks it.
        with patch.object(gc, "_runtime_reference_pass",
                          side_effect=["unverifiable-process-race", "referenced"]) as scan, \
             patch.object(gc.time, "sleep"):
            self.assertTrue(gc._runtime_in_use([target]))
            self.assertEqual(scan.call_count, 2)

        # Permission failures are not transient races and cannot be retried away.
        with patch.object(gc, "_runtime_reference_pass",
                          return_value="unverifiable-permission-read-environ") as scan, \
             patch.object(gc.time, "sleep") as pause:
            self.assertTrue(gc._runtime_in_use([target]))
            scan.assert_called_once()
            pause.assert_not_called()

        # A persistent or unresolved process race must remain fail-closed.
        with patch.object(gc, "_runtime_reference_pass",
                          return_value="unverifiable-process-race") as scan, \
             patch.object(gc.time, "sleep") as pause:
            self.assertTrue(gc._runtime_in_use([target]))
            self.assertEqual(scan.call_count, 3)
            self.assertEqual(pause.call_count, 2)

    def test_readonly_runtime_stage_is_redacted_and_apply_semantics_unchanged(self):
        from unittest.mock import patch
        proc = Path(self.workspace.name) / "diagnostic-proc"
        (proc / "982374").mkdir(parents=True)
        target = self.root / "releases" / self.app / self.revisions[3]
        with patch.object(gc.time, "sleep") as pause:
            detailed = gc._runtime_reference_probe(
                [target], proc_root=proc, explain_race=True)
            standard = gc._runtime_reference_probe([target], proc_root=proc)
        self.assertEqual(detailed, "unverifiable-process-race-at-read-cmdline")
        self.assertEqual(standard, "unverifiable-process-race")
        self.assertTrue(gc._runtime_in_use([target], proc_root=proc))
        self.assertNotIn("982374", detailed)
        self.assertNotIn(self.revisions[3], detailed)
        self.assertEqual(pause.call_count, 4)

    def test_esrch_during_proc_read_only_ignores_confirmed_vanished_pid(self):
        import errno
        import shutil
        from unittest.mock import patch
        proc = Path(self.workspace.name) / "proc-esrch"
        process = proc / "982374"
        process.mkdir(parents=True)
        target = self.root / "releases" / self.app / self.revisions[3]
        gone = ProcessLookupError(errno.ESRCH, "process vanished")
        # PID still present: fail closed; a re-used or zombie PID is unsafe
        # until independently proven harmless.
        with patch.object(Path, "read_bytes", side_effect=gone):
            self.assertEqual(gc._runtime_reference_probe([target], proc_root=proc),
                             "unverifiable-process-race")
            self.assertTrue(gc._runtime_in_use([target], proc_root=proc))

        # PID truly vanished after enumeration: no process may reference it.
        def disappear(_path):
            shutil.rmtree(process)
            raise ProcessLookupError(errno.ESRCH, "process vanished")

        with patch.object(Path, "read_bytes", autospec=True, side_effect=disappear):
            self.assertEqual(gc._runtime_reference_probe([target], proc_root=proc), "clear")
        self.assertFalse(process.exists())

    def test_runtime_esrch_skips_only_stable_resource_free_zombie(self):
        from unittest.mock import patch
        import errno

        proc = Path(self.workspace.name) / "proc-zombie"
        pid = proc / "982375"
        (pid / "fd").mkdir(parents=True)
        target = self.root / "releases" / self.app / self.revisions[3]
        gone = ProcessLookupError(errno.ESRCH, "process is not runnable")

        def stat_line(state="Z", start=123456):
            # /proc/PID/stat fields: state is #3, starttime is #22.
            fields = [state] + ["0"] * 18 + [str(start)] + ["0"] * 4
            return f"{pid.name} (worker ) name) " + " ".join(fields)

        (pid / "stat").write_text(stat_line())
        with patch.object(Path, "read_bytes", side_effect=gone):
            self.assertEqual(gc._runtime_reference_probe([target], proc_root=proc),
                             "clear")
            # Existing runnable PID must never be ignored.
            (pid / "stat").write_text(stat_line("R"))
            self.assertEqual(gc._runtime_reference_probe([target], proc_root=proc),
                             "unverifiable-process-race")
            (pid / "stat").write_text(stat_line())
            # Open descriptor: even a claimed zombie is not proven harmless.
            (pid / "fd" / "5").symlink_to(target)
            self.assertEqual(gc._runtime_reference_probe([target], proc_root=proc),
                             "unverifiable-process-race")
            (pid / "fd" / "5").unlink()

            # PID reuse / changed start time between checks must fail closed.
            real_read_text = Path.read_text
            reads = [0]

            def changing_stat(path, *args, **kwargs):
                if path == pid / "stat":
                    reads[0] += 1
                    return stat_line(start=123456 + (reads[0] > 1))
                return real_read_text(path, *args, **kwargs)

            with patch.object(Path, "read_text", autospec=True,
                              side_effect=changing_stat), \
                 patch.object(gc.time, "sleep") as pause:
                self.assertEqual(gc._runtime_reference_probe([target], proc_root=proc),
                                 "unverifiable-process-identity-change")
                pause.assert_not_called()

    def test_zombie_stat_missing_or_invalid_remains_protected(self):
        from unittest.mock import patch
        import errno

        proc = Path(self.workspace.name) / "proc-unverified"
        pid = proc / "986443"
        (pid / "fd").mkdir(parents=True)
        target = self.root / "releases" / self.app / self.revisions[3]
        with patch.object(Path, "read_bytes",
                          side_effect=ProcessLookupError(errno.ESRCH, "gone")):
            self.assertEqual(gc._runtime_reference_probe([target], proc_root=proc),
                             "unverifiable-process-race")
            (pid / "stat").write_text("untrusted")
            self.assertEqual(gc._runtime_reference_probe([target], proc_root=proc),
                             "unverifiable-process-race")

    def test_redacted_runtime_error_has_errno_and_stage_without_pid_leak(self):
        import errno
        from unittest.mock import patch
        proc = Path(self.workspace.name) / "proc-sample"
        (proc / "123456").mkdir(parents=True)
        target = self.root / "releases" / self.app / self.revisions[3]
        with patch.object(Path, "read_bytes", side_effect=OSError(errno.EIO, "io unavailable")):
            status = gc._runtime_reference_probe([target], proc_root=proc)
        self.assertEqual(status, "unverifiable-process-error-read-cmdline-errno-5")
        self.assertNotIn("123456", status)
        self.assertNotIn(self.revisions[3], status)

    def test_unreadable_and_referenced_processes_stay_protected_but_distinct(self):
        from unittest.mock import patch
        proc = Path(self.workspace.name) / "procs"
        proc.mkdir()
        pid = proc / "1234"
        pid.mkdir()
        target = self.root / "releases" / self.app / self.revisions[3]
        # Missing files in a still-live PID remain unsafe.
        self.assertEqual(gc._runtime_reference_probe([target], proc_root=proc),
                         "unverifiable-process-race")
        self.assertTrue(gc._runtime_in_use([target], proc_root=proc))
        for name in ("cmdline", "environ", "maps"):
            (pid / name).write_bytes(b"")
        (pid / "fd").mkdir()
        self.assertEqual(gc._runtime_reference_probe([target], proc_root=proc), "clear")
        (pid / "cmdline").write_bytes(os.fsencode(target) + b"/server.py")
        self.assertEqual(gc._runtime_reference_probe([target], proc_root=proc), "referenced")
        self.assertTrue(gc._runtime_in_use([target], proc_root=proc))
        self.assertEqual(gc._runtime_reference_probe([target], proc_root=proc),
                         "referenced")


    def test_runtime_probe_cannot_block_guarded_install_while_deployment_busy(self):
        script = (Path(__file__).resolve().parents[1] /
                  "scripts/install_storage_gc.sh").read_text()
        self.assertIn("--probe-runtime", script)
        self.assertIn("STORAGE_GC_RUNTIME_PROBE=LOCKED_OR_UNAVAILABLE_NO_DELETION", script)
        self.assertNotIn("exit 26", script)

    def test_release_status_skips_only_expected_active_deploy_lock(self):
        workflow = (Path(__file__).resolve().parents[1] /
                    ".github/workflows/vps1-storage-gc-release.yml").read_text()
        self.assertIn("STORAGE_GC_STATUS=BUSY_LOCK_READONLY_STATUS_SKIPPED",
                      workflow)
        self.assertIn("STORAGE_GC_STATUS=FAILED_UNEXPECTED_ERROR",
                      workflow)
        self.assertIn("Resource temporarily unavailable", workflow)
        self.assertIn("remote storage-gc-status", workflow)
        self.assertNotIn("systemctl stop vps1-storage-deployment-gc", workflow)

    def test_scene_web_spool_identity_parser_requires_nonroot_running_user(self):
        from production.storage_gc_spool import parse_identity
        from types import SimpleNamespace
        props = "User=sceneweb\nMainPID=28471\nActiveState=active\n"
        status = "Name:\tuvicorn\nUid:\t1007\t1007\t1007\t1007\n"
        lookup = lambda _: SimpleNamespace(pw_uid=1007, pw_gid=1007)
        # Mock group lookup since production group is verified separately.
        from unittest.mock import patch
        with patch("production.storage_gc_spool.grp.getgrgid",
                   return_value=SimpleNamespace(gr_name="sceneweb")):
            self.assertEqual(parse_identity(props, status, get_user=lookup)[:3],
                             ("sceneweb", "sceneweb", 1007))
            for unsafe in (
                "User=root\nMainPID=28471\nActiveState=active\n",
                "User=sceneweb\nMainPID=0\nActiveState=active\n",
                "User=sceneweb\nMainPID=28471\nActiveState=inactive\n",
                "User=sceneweb\nMainPID=28471\nActiveState=active\nUser=sceneweb\n",
            ):
                with self.assertRaises(RuntimeError):
                    parse_identity(unsafe, status, get_user=lookup)
            with self.assertRaises(RuntimeError):
                parse_identity(props, status.replace("1007", "1009"),
                               get_user=lookup)
            # Web servers may legitimately run as a low-numbered, dedicated
            # system account such as www-data; the fixed unit/PID still must match.
            system_lookup = lambda _: SimpleNamespace(pw_uid=33, pw_gid=33)
            system_status = status.replace("1007", "33")
            self.assertEqual(parse_identity(props, system_status,
                                            get_user=system_lookup)[2], 33)
            with self.assertRaises(RuntimeError):
                parse_identity(props, status.replace("1007", "0"),
                               get_user=lambda _: SimpleNamespace(pw_uid=0, pw_gid=0))


    def test_manual_spool_alignment_never_executes_cleanup_or_weakens_permissions(self):
        root = Path(__file__).resolve().parents[1]
        aligner = (root / "production/storage_gc_spool.py").read_text()
        installer = (root / "scripts/install_storage_gc.sh").read_text()
        self.assertIn("systemctl", aligner)
        self.assertIn('if os.geteuid() != 0:', aligner)
        self.assertIn('if (DIR / "request").exists()', aligner)
        self.assertIn('mode.st_mode & 0o777) != 0o700', aligner)
        self.assertIn('fcntl.LOCK_EX | fcntl.LOCK_NB', aligner)
        self.assertIn('subprocess.run(["systemd-tmpfiles", "--create"', aligner)
        self.assertIn('-m production.storage_gc_spool', installer)
        self.assertNotIn("storage-gc.py --apply", aligner)
        self.assertNotIn("chmod 0777", installer)

    def test_manual_systemd_path_reuses_existing_protected_gc_engine(self):
        root = Path(__file__).resolve().parents[1]
        units = root / "infrastructure/systemd"
        manual = (units / "vps1-storage-deployment-gc-manual.path").read_text()
        service = (units / "vps1-storage-deployment-gc.service").read_text()
        status = (units / "vps1-storage-deployment-gc-status.service").read_text()
        timer = (units / "vps1-storage-deployment-gc-status.timer").read_text()
        installer = (root / "scripts/install_storage_gc.sh").read_text()
        self.assertIn("Unit=vps1-storage-deployment-gc.service", manual)
        self.assertIn("ExecStartPre=-/usr/bin/rm -f /run/vps1-storage-gc-manual/request", service)
        self.assertIn("storage-gc.py --apply", service)
        self.assertIn("storage-gc.py --publish-status", status)
        self.assertNotIn("--apply", status)
        self.assertIn("OnUnitActiveSec=30min", timer)
        self.assertIn("systemd-tmpfiles", (root / "production/storage_gc_spool.py").read_text())
        self.assertIn("vps1-storage-deployment-gc-manual.path", installer)


    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name) / "fresh-website"
        self.unit_dir = Path(self.workspace.name) / "units"
        self.unit_dir.mkdir()
        self.now = time.time()
        self.app = "short-maker"
        for name in ("releases", "venvs", "current", "control/state"):
            (self.root / name).mkdir(parents=True)
        (self.root / "releases" / self.app).mkdir()
        (self.root / "venvs" / self.app).mkdir()
        self.revisions = [("%040x" % i) for i in (1, 2, 3, 4)]
        for rev in self.revisions:
            rel = self.root / "releases" / self.app / rev
            env = self.root / "venvs" / self.app / rev
            rel.mkdir()
            env.mkdir()
            (rel / "source.py").write_bytes(b"release" * 100)
            (env / "interpreter").write_bytes(b"venv" * 100)
            self.set_old(rel)
            self.set_old(env)
        self.current = self.root / "current" / self.app
        self.current.symlink_to(self.root / "releases" / self.app / self.revisions[0])
        self.set_old(self.current, symlink=True)
        self.manifest = self.root / "control/state" / (self.app + ".json")
        self.manifest.write_text(json.dumps({
            "status": "ACTIVE", "revision": self.revisions[0],
            "previous": {
                "current": str(self.root / "releases" / self.app / self.revisions[1]),
                "active": True,
            },
        }))
        # Ensure no other app has an unknown unit/reference.
        (self.unit_dir / "vps1-production-short-maker.service").write_text(
            "[Service]\nExecStart=/usr/bin/true\n"
        )

    def set_old(self, path: Path, symlink=False):
        stamp = self.now - 26 * 3600
        os.utime(path, (stamp, stamp), follow_symlinks=not symlink)

    def inventory(self):
        return gc.inventory(self.root, now=self.now, unit_dir=self.unit_dir)

    def execute(self):
        return gc.execute(root=self.root, apply=True, now=self.now,
                          unit_dir=self.unit_dir, test_mode=True)

    def test_dry_run_is_non_destructive_and_keeps_rollback(self):
        plan = self.inventory()
        self.assertFalse(plan.get("blocked"))
        self.assertEqual([(x["app"], x["revision"]) for x in plan["candidates"]],
                         [(self.app, self.revisions[3])])
        self.assertGreater(plan["estimated_bytes"], 0)
        for rev in self.revisions:
            self.assertTrue((self.root / "releases" / self.app / rev).exists())

    def test_scoped_inventory_matches_full_candidate_and_preserves_fallback(self):
        full = self.inventory()
        scoped = gc.inventory(
            self.root, now=self.now, unit_dir=self.unit_dir,
            only_app=self.app, only_revision=self.revisions[3])
        self.assertEqual(scoped["candidates"], full["candidates"])
        self.assertEqual(scoped["protected"], full["protected"])
        self.assertEqual(scoped["estimated_bytes"], full["estimated_bytes"])
        # A newly protected revision must still be rejected by a scoped recheck.
        (self.unit_dir / "vps1-protect-old.service").write_text(
            "WorkingDirectory=" + str(self.root / "releases" / self.app /
                                      self.revisions[3]) + "\\n")
        self.assertEqual(gc.inventory(
            self.root, now=self.now, unit_dir=self.unit_dir,
            only_app=self.app, only_revision=self.revisions[3])["candidates"], [])

    def test_apply_scopes_revalidation_to_one_revision(self):
        from unittest.mock import patch

        real_inventory = gc.inventory
        plan = self.inventory()
        with patch.object(gc, "inventory", wraps=real_inventory) as inspected:
            result = gc.apply_plan(self.root, plan, now=self.now,
                                   unit_dir=self.unit_dir, test_mode=True)
        self.assertEqual(result["deleted"],
                         [self.app + "/" + self.revisions[3]])
        self.assertEqual(inspected.call_count, 1)
        self.assertEqual(inspected.call_args.kwargs["only_app"], self.app)
        self.assertEqual(inspected.call_args.kwargs["only_revision"],
                         self.revisions[3])
        for rev in self.revisions[:3]:
            self.assertTrue((self.root / "releases" / self.app / rev).exists())

    def test_guarded_apply_deletes_only_superseded_release_and_venv(self):
        outcome = self.execute()
        self.assertEqual(outcome["outcome"]["deleted"], [self.app + "/" + self.revisions[3]])
        self.assertFalse((self.root / "releases" / self.app / self.revisions[3]).exists())
        self.assertFalse((self.root / "venvs" / self.app / self.revisions[3]).exists())
        for rev in self.revisions[:3]:
            self.assertTrue((self.root / "releases" / self.app / rev).exists())
            self.assertTrue((self.root / "venvs" / self.app / rev).exists())

    def test_recent_promotion_blocks_all_deletions(self):
        os.utime(self.current, (self.now, self.now), follow_symlinks=False)
        self.assertEqual(self.inventory()["candidates"], [])

    def test_unverified_pointer_blocks_all_deletions(self):
        self.manifest.write_text(json.dumps({"status": "ACTIVE", "revision": "wrong"}))
        self.assertEqual(self.inventory()["candidates"], [])

    def test_missing_active_manifest_blocks(self):
        self.manifest.unlink()
        self.assertEqual(self.inventory()["candidates"], [])

    def test_symlink_candidate_refused(self):
        bad = self.root / "releases" / self.app / self.revisions[3]
        import shutil
        shutil.rmtree(bad)
        bad.symlink_to(self.root / "control")
        self.assertEqual(self.inventory()["candidates"], [])

    def test_systemd_reference_preserved(self):
        unit = self.unit_dir / "vps1-production-short-maker.service"
        unit.write_text("Environment=PATH=" + str(
            self.root / "venvs" / self.app / self.revisions[3]) + "/bin\n")
        self.assertEqual(self.inventory()["candidates"], [])

    def test_young_venv_cannot_be_removed(self):
        path = self.root / "venvs" / self.app / self.revisions[3]
        os.utime(path, (self.now, self.now))
        self.assertEqual(self.inventory()["candidates"], [])

    def test_unknown_unit_symlink_fails_closed(self):
        (self.unit_dir / "vps1-unexpected.service").symlink_to("/does-not-exist")
        self.assertEqual(self.inventory()["candidates"], [])

    def test_never_descends_into_user_data(self):
        media = self.root / "user-media"
        media.mkdir()
        (media / "original.mp4").write_bytes(b"never delete")
        self.execute()
        self.assertEqual((media / "original.mp4").read_bytes(), b"never delete")

    def test_running_process_prevents_deletion(self):
        from unittest.mock import patch
        with (patch.object(gc, "_runtime_in_use", return_value=True),
              patch.object(gc.subprocess, "run") as run):
            run.return_value.returncode = 0
            plan = self.inventory()
            result = gc.apply_plan(self.root, plan, now=self.now,
                                   unit_dir=self.unit_dir, test_mode=False)
        self.assertEqual(result["deleted"], [])
        self.assertTrue(any("process-in-use" in value for value in result["skipped"]))
        self.assertTrue((self.root / "releases" / self.app / self.revisions[3]).exists())

    def test_verified_dual_revision_mismatch_reclaims_only_old_venv(self):
        # Web-only/current pointer and the recorded active Python environment
        # are independently protected. Never modify either manifest or pointer.
        self.manifest.write_text(json.dumps({
            "status": "ACTIVE", "revision": self.revisions[1],
            "previous": {"current": str(self.root / "releases" / self.app / self.revisions[1])},
        }))
        plan = self.inventory()
        self.assertEqual([(c["revision"], c["scope"]) for c in plan["candidates"]],
                         [(self.revisions[3], "venv-only-pointer-mismatch")])
        self.assertEqual(plan["candidates"][0]["paths"],
                         [str(self.root / "venvs" / self.app / self.revisions[3])])
        result = self.execute()
        self.assertEqual(result["outcome"]["deleted"],
                         [self.app + "/" + self.revisions[3]])
        for rev in self.revisions:
            self.assertTrue((self.root / "releases" / self.app / rev).is_dir())
        for rev in self.revisions[:3]:
            self.assertTrue((self.root / "venvs" / self.app / rev).is_dir())
        self.assertFalse((self.root / "venvs" / self.app / self.revisions[3]).exists())
        self.assertEqual(json.loads(self.manifest.read_text())["revision"], self.revisions[1])
        self.assertEqual(self.current.resolve().name, self.revisions[0])

    def test_mismatch_refuses_unverifiable_recorded_release(self):
        self.manifest.write_text(json.dumps({
            "status": "ACTIVE", "revision": "a" * 40,
        }))
        self.assertEqual(self.inventory()["candidates"], [])

    def test_mismatch_preserves_unit_references_even_when_old(self):
        self.manifest.write_text(json.dumps({
            "status": "ACTIVE", "revision": self.revisions[1],
        }))
        (self.unit_dir / "vps1-production-short-maker.service").write_text(
            "ExecStart=" + str(self.root / "venvs" / self.app / self.revisions[3]) +
            "/bin/python\\n"
        )
        self.assertEqual(self.inventory()["candidates"], [])

    def test_mismatch_rechecks_manifest_before_apply(self):
        self.manifest.write_text(json.dumps({
            "status": "ACTIVE", "revision": self.revisions[1],
        }))
        plan = self.inventory()
        self.assertEqual(len(plan["candidates"]), 1)
        self.manifest.write_text(json.dumps({
            "status": "ACTIVE", "revision": self.revisions[3],
        }))
        from unittest.mock import patch
        with patch.object(gc.subprocess, "run") as run, \
             patch.object(gc, "_runtime_in_use", return_value=False):
            run.return_value.returncode = 0
            outcome = gc.apply_plan(self.root, plan, now=self.now,
                                    unit_dir=self.unit_dir, test_mode=False)
        self.assertFalse(outcome["deleted"])
        self.assertTrue((self.root / "venvs" / self.app / self.revisions[3]).exists())

    def test_process_environment_reference_blocks_removal(self):
        proc = Path(self.workspace.name) / "proc"
        pid = proc / "1234"
        pid.mkdir(parents=True)
        (pid / "cmdline").write_bytes(b"python\\0server.py\\0")
        (pid / "environ").write_bytes(
            b"PYTHONPATH=" + os.fsencode(self.root / "venvs" / self.app / self.revisions[3]))
        (pid / "maps").write_bytes(b"")
        (pid / "fd").mkdir()
        target = self.root / "venvs" / self.app / self.revisions[3]
        self.assertTrue(gc._runtime_in_use([target], proc_root=proc))

    def test_process_file_descriptor_reference_blocks_removal(self):
        proc = Path(self.workspace.name) / "proc"
        pid = proc / "1234"
        pid.mkdir(parents=True)
        for name in ("cmdline", "environ", "maps"):
            (pid / name).write_bytes(b"")
        (pid / "fd").mkdir()
        target = self.root / "venvs" / self.app / self.revisions[3]
        (pid / "fd" / "0").symlink_to(target / "interpreter")
        self.assertTrue(gc._runtime_in_use([target], proc_root=proc))

    def test_unreadable_process_state_fails_closed(self):
        proc = Path(self.workspace.name) / "proc"
        pid = proc / "1234"
        pid.mkdir(parents=True)
        target = self.root / "venvs" / self.app / self.revisions[3]
        self.assertTrue(gc._runtime_in_use([target], proc_root=proc))

    def populate_control_checkouts(self):
        root = self.root / "control/releases"
        root.mkdir(parents=True)
        revisions = ["%040x" % i for i in range(100, 116)]
        for index, rev in enumerate(revisions):
            item = root / rev
            (item / ".git").mkdir(parents=True)
            (item / ".git/HEAD").write_text(rev + "\n")
            (item / ".git/config").write_text(
                '[remote "origin"]\n'
                '    url = https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control.git\n'
            )
            (item / "source.py").write_text("application source")
            stamp = self.now - (96 - index) * 3600
            os.utime(item, (stamp, stamp))
        return root, revisions

    def test_control_gc_preserves_latest_12_and_reclaims_only_detached_old_checkouts(self):
        root, revisions = self.populate_control_checkouts()
        expected = revisions[:4]
        plan = gc.control_release_inventory(self.root, now=self.now, unit_dir=self.unit_dir)
        self.assertFalse(plan.get("blocked"))
        self.assertEqual(sorted(x["revision"] for x in plan["candidates"]), expected)
        self.assertEqual(len(plan["protected"]), 12)
        result = self.execute()
        self.assertEqual(sorted(x.split("/")[-1] for x in result["outcome"]["deleted"]
                                if x.startswith("control/")), expected)
        for rev in revisions[4:]:
            self.assertTrue((root / rev / ".git/HEAD").is_file())
        self.assertTrue(self.current.resolve().is_dir())

    def test_control_gc_preserves_manifest_and_unit_refs(self):
        root, revisions = self.populate_control_checkouts()
        old = revisions[0]
        content = json.loads(self.manifest.read_text())
        content["operator_previous_sha"] = old
        self.manifest.write_text(json.dumps(content))
        (self.unit_dir / "vps1-control-reference.service").write_text(
            "WorkingDirectory=" + str(root / revisions[1]) + "\n"
        )
        plan = gc.control_release_inventory(self.root, now=self.now, unit_dir=self.unit_dir)
        self.assertEqual(sorted(x["revision"] for x in plan["candidates"]),
                         revisions[2:4])
        self.assertTrue((root / old).exists())

    def test_control_gc_refuses_wrong_git_head_and_symlink(self):
        root, revisions = self.populate_control_checkouts()
        (root / revisions[0] / ".git/HEAD").write_text("f" * 40 + "\n")
        import shutil
        shutil.rmtree(root / revisions[1])
        (root / revisions[1]).symlink_to(self.root / "user-media")
        plan = gc.control_release_inventory(self.root, now=self.now, unit_dir=self.unit_dir)
        self.assertEqual(sorted(x["revision"] for x in plan["candidates"]),
                         revisions[2:4])
        self.execute()
        self.assertTrue((root / revisions[0]).exists())
        self.assertTrue((root / revisions[1]).is_symlink())

    def test_control_gc_refuses_wrong_origin_and_git_alternates(self):
        root, revisions = self.populate_control_checkouts()
        (root / revisions[0] / ".git/config").write_text(
            '[remote "origin"]\n    url = https://example.com/untrusted.git\n')
        info = root / revisions[1] / ".git/objects/info"
        info.mkdir(parents=True)
        (info / "alternates").write_text("/other/repository/objects\n")
        plan = gc.control_release_inventory(self.root, now=self.now, unit_dir=self.unit_dir)
        self.assertEqual(sorted(x["revision"] for x in plan["candidates"]),
                         revisions[2:4])
        self.execute()
        self.assertTrue((root / revisions[0]).exists())
        self.assertTrue((root / revisions[1]).exists())

    def test_control_gc_age_grace_is_stricter_than_app_gc(self):
        root, revisions = self.populate_control_checkouts()
        recent = root / revisions[0]
        os.utime(recent, (self.now - 26 * 3600, self.now - 26 * 3600))
        plan = gc.control_release_inventory(self.root, now=self.now, unit_dir=self.unit_dir)
        self.assertTrue(all(x["revision"] != revisions[0] for x in plan["candidates"]))

    def test_control_gc_unreadable_state_blocks_all_control_cleanup(self):
        root, revisions = self.populate_control_checkouts()
        (self.root / "control/state/bad.json").write_text("not-json")
        plan = gc.control_release_inventory(self.root, now=self.now, unit_dir=self.unit_dir)
        self.assertIn("blocked", plan)
        self.assertEqual(plan["candidates"], [])
        self.execute()
        for rev in revisions:
            self.assertTrue((root / rev).exists())

    def test_control_gc_skips_when_root_deployer_owns_lock(self):
        root, revisions = self.populate_control_checkouts()
        from unittest.mock import patch
        import contextlib

        @contextlib.contextmanager
        def active_deployment(*, test_mode=False):
            yield False

        with patch.object(gc, "control_checkout_lock", active_deployment):
            result = self.execute()
        self.assertIn("control:deployment-active", result["outcome"]["skipped"])
        for rev in revisions:
            self.assertTrue((root / rev).exists())

    def test_control_gc_process_reference_blocks_delete(self):
        root, revisions = self.populate_control_checkouts()
        plan = gc.control_release_inventory(self.root, now=self.now, unit_dir=self.unit_dir)
        from unittest.mock import patch
        with patch.object(gc, "_runtime_in_use", return_value=True):
            result = gc.apply_control_release_plan(
                self.root, plan, now=self.now, unit_dir=self.unit_dir, test_mode=False)
        self.assertEqual(result["deleted"], [])
        self.assertTrue(all((root / rev).exists() for rev in revisions))

    def test_control_gc_rechecks_eligibility_before_apply(self):
        root, revisions = self.populate_control_checkouts()
        plan = gc.control_release_inventory(self.root, now=self.now, unit_dir=self.unit_dir)
        item = root / revisions[0]
        os.utime(item, (self.now, self.now))
        outcome = gc.apply_control_release_plan(
            self.root, plan, now=self.now, unit_dir=self.unit_dir, test_mode=True)
        self.assertIn("control/" + revisions[0] + ":no-longer-eligible",
                      outcome["skipped"])
        self.assertTrue(item.exists())

    def test_skip_reason_summary_is_bounded_and_redacts_identifiers(self):
        skipped = [
            "control:deployment-active",
            "control/" + "a" * 40 + ":process-in-use",
            "short-maker/" + "b" * 40 + ":service-inactive",
            "control/" + "c" * 40 + ":no-longer-eligible",
            "short-maker/" + "d" * 40,
        ]
        summary = gc.summarize_skip_reasons(skipped)
        self.assertEqual(sum(summary.values()), len(skipped))
        self.assertEqual(summary["deployment-active"], 1)
        self.assertEqual(summary["process-in-use-or-unverifiable"], 1)
        self.assertEqual(summary["service-inactive"], 1)
        self.assertEqual(summary["revalidation-changed"], 1)
        self.assertEqual(summary["revalidation-or-unknown"], 1)
        self.assertNotIn("a" * 40, json.dumps(summary))
        self.assertNotIn("short-maker", json.dumps(summary))
        self.assertEqual(gc.summarize_skip_reasons(None), {})

    def test_legacy_audit_status_includes_redacted_skip_reasons(self):
        from unittest.mock import patch
        state = Path(self.workspace.name) / "private"
        state.mkdir()
        status = Path(self.workspace.name) / "public" / "status.json"
        old = {
            "mode": "apply", "timestamp": 1792456676,
            "deleted": [], "skipped": [
                "control/" + "b" * 40 + ":process-in-use",
                "control/" + "a" * 40 + ":no-longer-eligible"
            ], "free_before_bytes": 1_000_000,
            "free_after_bytes": 1_000_000
        }
        (state / "deployment-gc-history.jsonl").write_text(json.dumps(old) + "\n")
        with patch.object(gc, "execute", return_value={"inventory": {
                "candidates": [], "control": {"candidates": []},
                "estimated_bytes": 12345}}):
            payload = gc.publish_readonly_status(
                state=state, destination=status, test_mode=True)
        self.assertEqual(payload["last_cleanup"]["skipped_count"], 2)
        self.assertEqual(payload["last_cleanup"]["skip_reason_counts"], {
            "process-in-use-or-unverifiable": 1, "revalidation-changed": 1})
        self.assertEqual(payload["eligible_candidate_count"], 0)
        exported = status.read_text()
        self.assertNotIn("control/", exported)
        self.assertNotIn("b" * 40, exported)

    def test_explicit_apply_refuses_nonproduction_path(self):
        with self.assertRaises(RuntimeError):
            gc.execute(root=self.root, apply=True, unit_dir=self.unit_dir)


if __name__ == "__main__":
    unittest.main()

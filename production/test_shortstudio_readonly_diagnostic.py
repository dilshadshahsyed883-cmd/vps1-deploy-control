"""Offline test: the Short Studio diagnostic emits only aggregate evidence."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from production import shortstudio_readonly_diagnostic as diag

class DiagnosticTests(unittest.TestCase):
    def test_sensitive_rows_are_never_printed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            db=root/"core.db"
            with sqlite3.connect(db) as c:
                c.execute("CREATE TABLE channels (id INTEGER, secret TEXT)")
                c.execute("INSERT INTO channels VALUES(1, 'TOP_SECRET_DO_NOT_EMIT')")
            summary=diag.sqlite_summary(db,("channels",))
            self.assertEqual(summary["tables"]["channels"],{"count":1})
            self.assertNotIn("TOP_SECRET_DO_NOT_EMIT",str(summary))
            self.assertFalse(diag.folder_summary(root)["nonempty"] is False)
    def test_bad_files_fail_closed_without_reading_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            target=root/"target"
            target.write_text("TOP_SECRET_DO_NOT_EMIT")
            link=root/"link"
            link.symlink_to(target)
            self.assertEqual(diag.sqlite_summary(link,("channels",))["reason"],"symlink")
            self.assertEqual(diag.folder_summary(link)["reason"],"symlink")
    def test_secure_structure_shows_only_counts_and_expected_directory_names(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            clients=root/"oauth_clients"
            sessions=root/"oauth_sessions"
            clients.mkdir()
            sessions.mkdir()
            report=diag.secure_structure(root)
            self.assertEqual(report["unexpected_count"],0)
            self.assertFalse(report["expected"]["oauth_clients"]["nonempty"])
            (clients/"actual-private-token.json").write_text("TOP_SECRET_DO_NOT_EMIT")
            (root/"private-file-name.txt").write_text("TOP_SECRET_DO_NOT_EMIT")
            report=diag.secure_structure(root)
            self.assertTrue(report["expected"]["oauth_clients"]["nonempty"])
            self.assertEqual(report["unexpected_count"],1)
            self.assertEqual(report["unexpected_files"],1)
            self.assertNotIn("private-file-name",str(report))
            self.assertNotIn("actual-private-token",str(report))
            self.assertNotIn("TOP_SECRET_DO_NOT_EMIT",str(report))
    def test_secure_tree_scan_only_emits_type_counts(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            (root/"oauth_clients").mkdir()
            (root/"private_filename.txt").write_text("PRIVATE_VALUE")
            out=diag.secure_tree_types(root)
            self.assertEqual(out["symlinks"],0)
            self.assertEqual(out["special"],0)
            self.assertEqual(out["entries"],2)
            self.assertNotIn("private_filename",str(out))
            self.assertNotIn("PRIVATE_VALUE",str(out))
            (root/"oauth_clients"/"link").symlink_to(root/"private_filename.txt")
            self.assertEqual(diag.secure_tree_types(root)["symlinks"],1)

    def test_output_has_fixed_shape_and_no_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp)/"state"
            units=Path(tmp)/"units"
            units.mkdir()
            (state/"lum-shortstudio").mkdir(parents=True)
            with patch.object(diag,"port_8019",return_value="unreachable"),patch.object(
                diag,"unit_metadata",return_value={"exists":False,"state":"absent","symlink":False}):
                result=diag.collect(state,unit_dir=units)
            self.assertEqual(result["mode"],"READ_ONLY")
            self.assertEqual(result["scope"],"shortstudio")
            self.assertEqual(result["cooperation"]["mode"],"READ_ONLY")
            self.assertFalse(result["cooperation"]["production_activation_ready"])
            self.assertFalse(result["cooperation"]["retirement_enabled"])
            self.assertFalse(result["cooperation"]["same_host_root_proven"])
            self.assertEqual(result["active_drain_snapshot"]["mode"], "READ_ONLY")
            self.assertFalse(result["active_drain_snapshot"]["active_upload_drain_verified"])
            self.assertFalse(result["active_drain_snapshot"]["production_activation_ready"])
            self.assertEqual(result["active_drain_snapshot"]["status"], "BLOCKED")
            self.assertFalse(result["empty_first_install_proven"])
            self.assertEqual(list((state/"lum-shortstudio").iterdir()),[])
    def test_protected_main_readonly_audit_reports_only_sanitized_drain_flags(self):
        workflow=(Path(__file__).resolve().parents[1]/".github/workflows/vps1-short-studio-release.yml").read_text(encoding="utf-8")
        for marker in ("data.get('active_drain_snapshot')",
                       "'maker_jobs_idle_snapshot'",
                       "'ready_imports_idle_snapshot'",
                       "'uploader_jobs_idle_snapshot'",
                       "'uploader_source_claims_idle_snapshot'",
                       "'active_upload_drain_snapshot_verified'",
                       "'active_upload_drain_verified':False",
                       "'retirement_enabled':False",
                       "'deployment_performed':False"):
            self.assertIn(marker,workflow)


    def test_protected_readonly_audit_unit_flags_are_boolean_only(self):
        workflow=(Path(__file__).resolve().parents[1]/".github/workflows/vps1-short-studio-release.yml").read_text(encoding="utf-8")
        self.assertIn("roles=('maker','web','worker','scheduler')",workflow)
        self.assertIn("def role_booleans(source, keys):",workflow)
        self.assertIn("bool((units.get(role) or {}).get(key,False))",workflow)
        for marker in ("'unit_config_flags'","'unit_process_flags'",
                       "'configuration_present'","'folder_root_safe'",
                       "'lock_enabled'","'running_process_verified'",
                       "'process_lock_flag_verified'","'process_root_verified'"):
            self.assertIn(marker,workflow)
        for forbidden in ("print(data)", "print(config)", "print(live)",
                          "print(units)", "print(paths)"):
            self.assertNotIn(forbidden,workflow)
        self.assertIn("'active_upload_drain_verified':False",workflow)
        self.assertIn("'retirement_enabled':False",workflow)
        self.assertIn("'deployment_performed':False",workflow)

if __name__ == "__main__": unittest.main()

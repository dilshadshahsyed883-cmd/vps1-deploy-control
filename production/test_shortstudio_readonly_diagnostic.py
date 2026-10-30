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
            self.assertFalse(result["empty_first_install_proven"])
            self.assertEqual(list((state/"lum-shortstudio").iterdir()),[])
if __name__ == "__main__": unittest.main()

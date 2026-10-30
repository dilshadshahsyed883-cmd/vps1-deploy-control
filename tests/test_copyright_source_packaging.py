"""Copyright Uploader immutable source packaging: exact reviewed code, no secrets."""
import tempfile
from pathlib import Path
from unittest import TestCase, mock
from rebuild import fresh_vps


class CopyrightSourcePackaging(TestCase):
    def test_real_copyright_uploader_source_is_packaged_without_secret_material(self):
        files = fresh_vps.sources("copyright-uploader")
        names = {name for name, _ in files}
        self.assertTrue(all(f"part0{n}/desktop_core/modules/secret_store.py" in names
                            for n in (1, 2, 3)))
        self.assertTrue(any(name.endswith("/lum_engine/standalone_app.py") for name in names))
        self.assertFalse(any(name.endswith((".db", ".sqlite", ".pem", ".key", ".env"))
                             for name in names))

    def test_reject_lookalike_secret_files_even_inside_allowed_module_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "apps/copyright-uploader/part01/desktop_core/modules"
            folder.mkdir(parents=True)
            (folder / "secret_store.py").write_text("def empty(): return None\n")
            with mock.patch.object(fresh_vps, "ROOT", root):
                names = dict(fresh_vps.sources("copyright-uploader"))
                self.assertIn("part01/desktop_core/modules/secret_store.py", names)
                for unsafe in ("secret_backdoor.py", "client_secret.py", "credentials.py"):
                    path = folder / unsafe
                    path.write_text("def noop(): pass\n")
                    with self.assertRaisesRegex(ValueError, "secret filename"):
                        fresh_vps.sources("copyright-uploader")
                    path.unlink()

    def test_embedded_secret_and_symlink_still_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "apps/copyright-uploader/part01/desktop_core/modules"
            folder.mkdir(parents=True)
            helper = folder / "secret_store.py"
            fixture_name = "client_" + "secret"
            fixture_value = "".join(["abcd", "efgh"] * 4)
            helper.write_text(f'{fixture_name} = "{fixture_value}"\\n')
            with mock.patch.object(fresh_vps, "ROOT", root):
                with self.assertRaisesRegex(ValueError, "secret or binary source"):
                    fresh_vps.sources("copyright-uploader")
                helper.write_text("def empty(): return None\n")
                other = folder / "safe.py"
                other.symlink_to(helper)
                with self.assertRaisesRegex(ValueError, "source symlink"):
                    fresh_vps.sources("copyright-uploader")


if __name__ == "__main__":
    import unittest
    unittest.main()

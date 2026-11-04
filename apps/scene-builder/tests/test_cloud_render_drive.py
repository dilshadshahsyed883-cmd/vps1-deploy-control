"""Cloud Render Drive Bridge tests: safe diagnostics, no cross-file deletion."""
import hashlib
import importlib.util
import pathlib
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

FILE = pathlib.Path(__file__).resolve().parents[1] / "part01" / "cloud_render.py"
spec = importlib.util.spec_from_file_location("cloud_render_under_test", FILE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class DriveBridgeErrorTests(unittest.TestCase):
    def test_every_rclone_call_uses_canonical_config_even_with_decoy_env(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {"RCLONE_CONFIG": "/tmp/decoy-rclone.conf", "HOME": "/tmp/decoy-home"}):
            with patch.object(module.subprocess, "run", return_value=SimpleNamespace(
                    returncode=0, stdout="gdrive:\\n", stderr="")) as execute:
                result = module.CloudRender.execute(["rclone", "listremotes"], 15)
        self.assertEqual(result, "gdrive:\\n")
        self.assertEqual(execute.call_args.args[0], [
            "rclone", "--config", "/home/ubuntu/.config/masterctl/rclone.conf", "listremotes"])
        self.assertTrue(execute.call_args.kwargs["capture_output"])
        self.assertEqual(execute.call_args.kwargs["timeout"], 15)

    def test_canonical_bridge_path_matches_deployment_configuration(self):
        runtime = (FILE.parents[3] / "production/runtime.py").read_text()
        self.assertIn("RCLONE = '/home/ubuntu/.config/masterctl/rclone.conf'", runtime)
        self.assertEqual(module.CANONICAL_RCLONE_CONFIG, "/home/ubuntu/.config/masterctl/rclone.conf")
    def test_rclone_error_categorization_never_leaks_stderr(self):
        cases = [
            ("Failed to refresh token: invalid_grant secret_access_token_xyz", "authentication"),
            ("Failed to create file system: didn't find section in config file", "configuration"),
            ("googleapi: Error 403: insufficientPermissions", "permission denied"),
            ("Google Drive: quota exceeded", "quota"),
            ("Failed to copy: --immutable destination already exists", "immutable"),
            ("dial tcp: i/o timeout", "network"),
        ]
        for stderr, category in cases:
            with self.subTest(category=category):
                with patch.object(module.subprocess, "run", return_value=SimpleNamespace(
                        returncode=1, stdout="", stderr=stderr)):
                    with self.assertRaises(module.RcloneFailure) as caught:
                        module.CloudRender.execute(["rclone", "copyto", "local", "gdrive:file"])
                self.assertIn(category, str(caught.exception).lower())
                self.assertNotIn("secret_access_token_xyz", str(caught.exception))
                self.assertNotIn("local", str(caught.exception))
                self.assertNotIn("gdrive:file", str(caught.exception))

    def test_remote_stat_only_swallows_remote_file_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            app = module.CloudRender(directory)
            try:
                with patch.object(app, "execute", side_effect=module.RcloneFailure(
                        "lsjson", 4, "remote_file_absent")):
                    self.assertIsNone(app.remote_stat("gdrive:Cloud Render/file.mp4"))
                with patch.object(app, "execute", side_effect=module.RcloneFailure(
                        "lsjson", 1, "Drive authentication expired or unauthorized")):
                    with self.assertRaises(module.RcloneFailure):
                        app.remote_stat("gdrive:Cloud Render/file.mp4")
            finally:
                app.pool.shutdown(wait=True)

    def test_unknown_remote_does_not_delete_original(self):
        with tempfile.TemporaryDirectory() as directory:
            app = module.CloudRender(directory)
            try:
                job, media = self._seed(app)
                with patch.object(module.shutil, "which", return_value="/usr/bin/rclone"):
                    with patch.object(app, "execute", return_value="otherdrive:\n"):
                        app._upload(job)
                self.assertTrue(media.is_file())
                record = self._record(app, job)
                self.assertEqual(record["state"], "failed")
                self.assertIn('remote "gdrive" is not configured', record["error"])
            finally:
                app.pool.shutdown(wait=True)

    def test_copy_error_preserves_mp4_and_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            app = module.CloudRender(directory)
            try:
                job, media = self._seed(app)
                def fake_execute(cmd, timeout=1200):
                    if cmd[1] == "listremotes":
                        return "gdrive:\n"
                    if cmd[1] == "copyto":
                        raise module.RcloneFailure("copyto", 1, "Google Drive destination permission denied")
                    raise AssertionError(cmd)
                with patch.object(module.shutil, "which", return_value="/usr/bin/rclone"):
                    with patch.object(app, "execute", side_effect=fake_execute):
                        with patch.object(app, "remote_stat", return_value=None):
                            app._upload(job)
                record = self._record(app, job)
                self.assertEqual(record["state"], "failed")
                self.assertIn("permission denied", record["error"])
                self.assertTrue(media.is_file())
            finally:
                app.pool.shutdown(wait=True)

    def test_verified_existing_remote_deletes_only_matching_local_mp4(self):
        with tempfile.TemporaryDirectory() as directory:
            app = module.CloudRender(directory)
            try:
                job, media = self._seed(app)
                payload = media.read_bytes()
                info = {"Size": len(payload), "Hashes": {
                    "MD5": hashlib.md5(payload, usedforsecurity=False).hexdigest()}}
                with patch.object(module.shutil, "which", return_value="/usr/bin/rclone"):
                    with patch.object(app, "execute", return_value="gdrive:\n") as execute:
                        with patch.object(app, "remote_stat", return_value=info):
                            app._upload(job)
                self.assertEqual([call.args[0][1] for call in execute.call_args_list], ["listremotes"])
                self.assertEqual(self._record(app, job)["state"], "delivered")
                self.assertFalse(media.exists())
            finally:
                app.pool.shutdown(wait=True)

    @staticmethod
    def _seed(app):
        job = "testjob"
        media = app.local(job)
        payload = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 1024
        media.write_bytes(payload)
        with app.connection() as con:
            con.execute(
                "INSERT INTO files(id,name,size,received,sha256,md5,state) VALUES(?,?,?,?,?,?,?)",
                (job, "test.mp4", len(payload), len(payload),
                 hashlib.sha256(payload).hexdigest(),
                 hashlib.md5(payload, usedforsecurity=False).hexdigest(), "queued"),
            )
        return job, media

    @staticmethod
    def _record(app, job):
        with app.connection() as con:
            return dict(con.execute("SELECT * FROM files WHERE id=?", (job,)).fetchone())


if __name__ == "__main__":
    unittest.main()

"""Offline regression contracts for Short Maker persistent resumable upload."""
import ast
import json
import re
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "apps/short-maker/part01/app.py"
CLIENT = ROOT / "apps/short-maker/part01/resumable_client.js"


class UploadError(Exception):
    def __init__(self, status_code, detail):
        self.status_code, self.detail = status_code, detail
        super().__init__(detail)


def isolated(names, symbols):
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    functions = [node for node in tree.body
                 if isinstance(node, ast.FunctionDef) and node.name in names]
    assert len(functions) == len(names)
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(SERVER), "exec"), symbols)
    return symbols


class ShortMakerResumeTests(unittest.TestCase):
    def test_upload_snapshot_resumes_exact_part_offsets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            namespace = isolated({"upload_target", "upload_snapshot"}, {
                "Path": Path, "JOBS_ROOT": root, "HTTPException": UploadError,
                "safe_name": lambda name: Path(name).name
            })
            job = "0123456789abcdef"
            manifest = {"files": {
                "images": [{"name": "../photo.png", "size": 10},
                           {"name": "second.jpg", "size": 12}],
                "audio": [{"name": "track.mp3", "size": 8}]
            }}
            first = namespace["upload_target"](job, "images", 0, manifest["files"]["images"][0])
            second = namespace["upload_target"](job, "images", 1, manifest["files"]["images"][1])
            audio = namespace["upload_target"](job, "audio", 0, manifest["files"]["audio"][0])
            for p in (first, second, audio):
                p.parent.mkdir(parents=True, exist_ok=True)
            first.with_name(first.name + ".part").write_bytes(b"abcd")
            second.write_bytes(b"x" * 12)
            snapshot = namespace["upload_snapshot"](job, manifest)
            self.assertEqual([x["uploaded"] for x in snapshot["files"]["images"]], [4, 12])
            self.assertEqual(snapshot["files"]["audio"][0]["uploaded"], 0)
            self.assertEqual(snapshot["uploaded_bytes"], 16)
            self.assertEqual(snapshot["total_bytes"], 30)
            self.assertEqual(snapshot["upload_progress"], 53.3)
            audio.write_bytes(b"z" * 8)
            first.write_bytes(b"x" * 10)
            self.assertEqual(namespace["upload_snapshot"](job, manifest)["upload_progress"], 100)

    def test_upload_configuration_keeps_destinations_exclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Default").mkdir()
            names = isolated({"upload_config"}, {
                "Path": Path, "HTTPException": UploadError,
                "SHORT_VPS_DEFAULT": "Default", "DEFAULT_DRIVE_OUTPUT": "gdrive:Short_Maker/Outputs",
                "short_vps_folder_names": lambda: ["Default"],
                "short_vps_folder": lambda n: root / n,
                "normalize_drive_path": lambda p: p,
                "parse_drive_folder_inputs": lambda a, b: [a] if a else [],
                "safe_name": lambda n: Path(n).name
            })
            config = {"available_to_short_uploader": True, "upload_to_drive": False}
            result = names["upload_config"](config)
            self.assertEqual(result["short_vps_folder"], "Default")
            self.assertTrue(result["available_to_short_uploader"])
            self.assertFalse(result["verified_clean"])
            with self.assertRaises(UploadError):
                names["upload_config"]({**config, "upload_to_drive": True})
            with self.assertRaises(UploadError):
                names["upload_config"]({**config, "output_count": 1000})

    def test_browser_resume_and_idempotent_queue_contracts(self):
        app = SERVER.read_text(encoding="utf-8")
        js = CLIENT.read_text(encoding="utf-8")
        self.assertIn('id="progressDetail"', app)
        for marker in ('id="renderProgressTrack"', 'id="renderProgressBar"',
                       'id="uploadPctLabel"', 'id="renderPctLabel"',
                       'client_request_id', 'with upload_create_lock():'):
            self.assertIn(marker, app)
        self.assertIn('previous.get("client_request_id") != request_id', app)
        self.assertIn('id="mUploading"', app)
        self.assertIn('src="/short-maker/resumable_client.js"', app)
        self.assertIn('status="uploading"', app)
        self.assertIn('if state.get("status") != "uploading":', app)
        self.assertIn('with upload_session_lock(job_id):', app)
        self.assertIn('reserve_short_output_names(cfg["short_vps_folder"]', app)
        for fragment in ("indexedDB.open(", "navigator.locks", "SHORT_CHUNK_SIZE",
                         "file.slice(offset", "meta.job_id", "shortDBSave(meta,blobs)",
                         "shortDBRead('meta','active')", "uploaded_bytes", "'/finalize'",
                         "pollShortJob", "crypto.randomUUID()", "withShortUploadLock(",
                         "renderProgressBar.style.width", "client_request_id:meta.client_request_id"):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, js)


if __name__ == "__main__":
    unittest.main()

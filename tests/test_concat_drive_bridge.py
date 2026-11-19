"""Restricted bridge protocol tests; no production service or Google access."""
import hashlib
import http.client
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import socket
import tempfile
import threading
import unittest
from unittest.mock import Mock

from production.concat_drive_bridge import Server, validate_row

OWNER = "a" * 64
ROW = dict(drive_id="file-1", name="bundle.zip", resource_keys="", drive_size=3,
           drive_md5="", folder_id="parent", drive_path="bundle.zip")


class UnixHTTP(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(2)
        self.sock.connect(self.host)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "intake.sock")
        self.intake = Mock()
        self.intake.remote.return_value = "gdrive:"
        self.intake.discover.return_value = [["file-1", "bundle.zip", "", 3, "", "parent", "bundle.zip"]]
        def download(row, target):
            target.write_bytes(b"zip")
            return "bundle.zip"
        self.intake.download.side_effect = download
        self.execute = Mock(return_value="gdrive:\n")
        self.server = Server(self.path, self.intake, self.execute, os.getuid(), self.temp.name)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def post(self, op, obj):
        conn = UnixHTTP(self.path, timeout=2)
        try:
            conn.request("POST", "/v1/" + op, json.dumps(obj))
            result = conn.getresponse()
            return result.status, dict(result.getheaders()), result.read()
        finally:
            conn.close()

    def wait_for_idle(self):
        # Reading Content-Length bytes can finish before the server's finally
        # block. Wait for both handler permits before checking spool cleanup.
        acquired = 0
        try:
            for _ in range(2):
                self.assertTrue(self.server.slots.acquire(timeout=2))
                acquired += 1
        finally:
            for _ in range(acquired):
                self.server.slots.release()

    def test_health_checks_canonical_remote(self):
        status, _, body = self.post("health", {"owner": OWNER})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.execute.assert_called_once_with(["rclone", "listremotes"], 30)
        self.execute.return_value = "other:\n"
        self.assertEqual(self.post("health", {"owner": OWNER})[0], 502)

    def test_scan_keeps_resource_key_and_does_not_allow_commands(self):
        link = "https://drive.google.com/drive/folders/parent?resourcekey=key"
        self.assertEqual(self.post("scan", {"owner": OWNER, "link": link})[0], 200)
        self.intake.discover.assert_called_once_with(link)
        for obj in ({"owner": OWNER, "link": link, "cmd": "rm"},
                    {"owner": "alice", "link": link},
                    {"owner": OWNER, "link": link, "remote": "other:"}):
            self.assertEqual(self.post("scan", obj)[0], 400)
        self.assertEqual(self.post("upload-raw", {"owner": OWNER})[0], 404)

    def test_download_stream_integrity_and_private_cleanup(self):
        status, headers, data = self.post("download", {"owner": OWNER, "row": ROW})
        self.assertEqual(status, 200)
        self.assertEqual(data, b"zip")
        self.assertEqual(headers["X-Content-SHA256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(headers["Content-Length"], "3")
        self.wait_for_idle()
        self.assertEqual(list(Path(self.temp.name).glob("intake-*")), [])

    def test_untrusted_uid_cannot_invoke_intake(self):
        self.server.uid = os.getuid() + 1
        with self.assertRaises((OSError, http.client.HTTPException)):
            self.post("scan", {"owner": OWNER, "link": "secret-link"})
        self.intake.discover.assert_not_called()

    def test_error_sanitization_and_failed_download_cleanup(self):
        error = RuntimeError("private-path token-secret private-drive-link")
        error.category = "Google Drive destination permission denied"
        self.intake.download.side_effect = error
        status, _, data = self.post("download", {"owner": OWNER, "row": ROW})
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(data), {"error": "source_denied"})
        self.assertEqual(list(Path(self.temp.name).glob("intake-*")), [])
        self.assertNotIn(b"token-secret", data)


    def post_mp4(self, data, *, owner=OWNER, job="test-1", name="s2.mp4",
                 sha=None, md5=None, extras=None):
        conn = UnixHTTP(self.path, timeout=10)
        headers = {
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(data)),
            "X-Owner": owner,
            "X-Job-ID": job,
            "X-File-Name": name,
            "X-Content-SHA256": sha or hashlib.sha256(data).hexdigest(),
            "X-Content-MD5": md5 or hashlib.md5(data, usedforsecurity=False).hexdigest(),
        }
        headers.update(extras or {})
        try:
            conn.putrequest("POST", "/v1/upload")
            for key, value in headers.items():
                conn.putheader(key, value)
            conn.endheaders()
            conn.send(data)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_fixed_concat_outputs_upload_is_verified_and_idempotent(self):
        data = b"\x00\x00\x00\x18ftypisom" + b"test MP4 body" * 8
        actual_md5 = hashlib.md5(data, usedforsecurity=False).hexdigest()
        uploaded = []
        stored = {"exists": False}
        def fake_execute(command, timeout):
            if command[1] == "lsjson":
                if not stored["exists"]:
                    missing = RuntimeError("Private Drive item not found")
                    missing.category = "remote_file_absent"
                    raise missing
                return json.dumps({"Size": len(data), "Hashes": {"MD5": actual_md5}})
            if command[1] == "copyto":
                self.assertEqual(command[0:2], ["rclone", "copyto"])
                self.assertEqual(command[-1], "--immutable")
                self.assertEqual(Path(command[2]).read_bytes(), data)
                uploaded.append(command)
                stored["exists"] = True
                return ""
            raise AssertionError("unexpected canonical command")
        self.execute.side_effect = fake_execute
        with patch("production.concat_drive_bridge.shutil.disk_usage",
                   return_value=SimpleNamespace(free=50 * 1024**3)):
            status, reply = self.post_mp4(
                data, extras={"X-Destination": "gdrive:arbitrary/private"})
            self.assertEqual(status, 200)
            expected = "gdrive:Concat Outputs/" + OWNER + "/test-1_s2.mp4"
            self.assertEqual(reply["remote"], expected)
            self.assertEqual(reply["md5"], actual_md5)
            self.assertEqual(uploaded[0][3], expected)
            status, second = self.post_mp4(data)
            self.assertEqual(status, 200)
            self.assertEqual(second["remote"], expected)
            self.assertEqual(len(uploaded), 1)
        self.wait_for_idle()
        self.assertEqual(list(Path(self.temp.name).glob("output-*")), [])

    def test_denied_malformed_or_tampered_mp4_never_uploaded(self):
        data = b"\x00\x00\x00\x18ftypisom" + b"test"
        self.execute.reset_mock()
        with patch("production.concat_drive_bridge.shutil.disk_usage",
                   return_value=SimpleNamespace(free=50 * 1024**3)):
            for changes in (
                {"owner": "b" * 63},
                {"job": "../bad"},
                {"name": "../bad.mp4"},
                {"name": "forbidden.zip"},
                {"sha": "0" * 64},
                {"md5": "0" * 32},
            ):
                with self.subTest(changes=changes):
                    status, _ = self.post_mp4(data, **changes)
                    self.assertIn(status, (400, 422))
            status, response = self.post_mp4(b"not an mp4 container")
            self.assertEqual(status, 422)
            self.assertEqual(response["error"], "invalid_mp4")
        self.execute.assert_not_called()
        self.wait_for_idle()
        self.assertEqual(list(Path(self.temp.name).glob("output-*")), [])

    def test_existing_drive_conflict_is_never_overwritten(self):
        data = b"\x00\x00\x00\x18ftypisom" + b"test"
        self.execute.side_effect = lambda command, timeout: json.dumps(
            {"Size": len(data), "Hashes": {"MD5": "0" * 32}}
        ) if command[1] == "lsjson" else None
        with patch("production.concat_drive_bridge.shutil.disk_usage",
                   return_value=SimpleNamespace(free=50 * 1024**3)):
            status, response = self.post_mp4(data)
            self.assertEqual(status, 409)
            self.assertEqual(response["error"], "remote_conflict")
        self.assertEqual([x.args[0][1] for x in self.execute.call_args_list],
                         ["lsjson"])

    def test_wrong_peer_uid_cannot_upload_output(self):
        data = b"\x00\x00\x00\x18ftypisom" + b"test"
        self.server.uid = os.getuid() + 1
        with self.assertRaises((OSError, http.client.HTTPException)):
            self.post_mp4(data)
        self.execute.assert_not_called()

    def test_no_caller_paths_or_malformed_rows(self):
        for updates in ({"destination": "/etc/passwd"}, {"drive_id": "../x"},
                        {"name": "../x.zip"}, {"resource_keys": "a/b\r\nX:y"},
                        {"drive_size": 5 * 1024**3}, {"drive_path": "../x.zip"},
                        {"drive_md5": "not-a-hash"}):
            with self.subTest(updates=updates):
                with self.assertRaises(ValueError):
                    validate_row({**ROW, **updates})


if __name__ == "__main__":
    unittest.main()

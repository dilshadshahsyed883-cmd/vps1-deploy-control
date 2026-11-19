"""Restricted bridge protocol tests; no production service or Google access."""
import hashlib
import http.client
import json
import os
from pathlib import Path
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
        self.assertEqual(self.post("upload", {"owner": OWNER})[0], 404)

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

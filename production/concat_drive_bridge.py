"""Read-only Concat intake over a peer-authenticated Unix socket.

Run separately as the canonical Scene identity. No shell, caller paths, remote
names, OAuth material or arbitrary rclone arguments cross this boundary.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import socket
import socketserver
import struct
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler

MAX_BODY = 16 * 1024
MAX_JSON = 16 * 1024 * 1024
MAX_ZIP = 4 * 1024 ** 3
ID = re.compile(r"[A-Za-z0-9_-]{1,200}")


def validate_row(row):
    fields = {"drive_id", "name", "resource_keys", "drive_size", "drive_md5", "folder_id", "drive_path"}
    if not isinstance(row, dict) or set(row) != fields:
        raise ValueError()
    if not isinstance(row["drive_id"], str) or not ID.fullmatch(row["drive_id"]):
        raise ValueError()
    for key in ("name", "resource_keys", "drive_md5", "folder_id", "drive_path"):
        if not isinstance(row[key], str) or len(row[key]) > 4096:
            raise ValueError()
    if (type(row["drive_size"]) is not int or not 0 <= row["drive_size"] <= MAX_ZIP
            or (row["folder_id"] and not ID.fullmatch(row["folder_id"]))
            or not row["name"].lower().endswith(".zip")
            or any(c in row["name"] for c in "/\\\r\n")
            or (row["drive_md5"] and not re.fullmatch(r"[a-f0-9]{32}", row["drive_md5"]))):
        raise ValueError()
    if row["resource_keys"] and not all(re.fullmatch(r"[\w-]{1,200}/[\w-]{1,200}", p, re.ASCII)
                                         for p in row["resource_keys"].split(",")):
        raise ValueError()
    path = row["drive_path"]
    if path and ("\\" in path or any(ord(c) < 32 for c in path)
                 or any(p in ("", ".", "..") for p in path.split("/"))):
        raise ValueError()
    return row


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    # Bound workers before spawning threads, including idle/malformed clients.
    def __init__(self, path, intake, execute, uid, spool):
        self.intake, self.execute, self.uid, self.spool = intake, execute, uid, Path(spool)
        self.slots = threading.BoundedSemaphore(2)
        super().__init__(str(path), Handler)

    def verify_request(self, request, address):
        request.settimeout(15)
        _, uid, _ = struct.unpack("3i", request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        return uid == self.uid

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def handle_error(self, request, address):
        pass  # Never log raw rclone exceptions, links, resource keys or headers.


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def json(self, code, obj):
        body = json.dumps(obj).encode()
        if len(body) > MAX_JSON:
            code, body = 502, b'{"error":"Drive Bridge listing too large"}'
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            raw_length = self.headers.get_all("Content-Length", [])
            if len(raw_length) != 1 or self.headers.get("Transfer-Encoding"):
                raise ValueError()
            length = int(raw_length[0])
            if not 0 < length <= MAX_BODY:
                raise ValueError()
            obj = json.loads(self.rfile.read(length))
            if not isinstance(obj, dict) or not re.fullmatch(r"[a-f0-9]{64}", str(obj.get("owner", ""))):
                raise ValueError()
            op = self.path
            expected = {"owner"} | ({"link"} if op == "/v1/scan" else
                                      {"row"} if op == "/v1/download" else set())
            if set(obj) != expected:
                raise ValueError()
            if op == "/v1/health":
                # Check the actual canonical identity, not just socket existence.
                remotes = self.server.execute(["rclone", "listremotes"], 30).splitlines()
                if self.server.intake.remote() not in remotes:
                    raise RuntimeError()
                self.json(200, {"ok": True, "protocol": 1})
            elif op == "/v1/scan":
                # Existing parser restricts to Drive HTTPS URLs and validated IDs.
                result = self.server.intake.discover(obj["link"])
                self.json(200, {"files": result})
            elif op == "/v1/download":
                row = validate_row(obj["row"])
                # Fresh, private per-request spool; caller never selects a path.
                with tempfile.TemporaryDirectory(prefix="intake-", dir=self.server.spool) as temp:
                    target = Path(temp) / "bundle.zip"
                    name = self.server.intake.download(row, target)
                    size = target.stat().st_size
                    if not 0 < size <= MAX_ZIP:
                        raise RuntimeError()
                    digest = hashlib.sha256()
                    with target.open("rb") as stream:
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            digest.update(chunk)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/zip")
                    self.send_header("Content-Length", str(size))
                    self.send_header("X-Content-SHA256", digest.hexdigest())
                    # JSON encoding safely carries Unicode filenames without header injection.
                    self.send_header("X-Zip-Name", json.dumps(name[:240], ensure_ascii=True))
                    self.end_headers()
                    with target.open("rb") as stream:
                        shutil.copyfileobj(stream, self.wfile, 1024 * 1024)
            else:
                self.json(404, {"error": "Unsupported Drive Bridge operation"})
        except (ValueError, TypeError, KeyError):
            self.json(400, {"error": "Invalid Drive Bridge request"})
        except Exception as exc:
            # Do not expose local paths, config errors, Drive URLs or token data.
            category = str(getattr(exc, "category", ""))
            code = ("source_denied" if "permission" in category else
                    "authentication" if "authentication" in category else
                    "source_missing" if category == "remote_file_absent" else
                    "configuration" if "configuration" in category else
                    "unavailable")
            self.json(502, {"error": code})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scene-source", required=True, type=Path)
    parser.add_argument("--socket", default="/run/concat-drive-bridge/intake.sock", type=Path)
    parser.add_argument("--spool", default="/var/lib/concat-drive-bridge", type=Path)
    parser.add_argument("--client-user", default="concatweb")
    args = parser.parse_args()
    os.umask(0o077)
    # Root-managed source/environment only; never accepted from socket callers.
    sys.path.insert(0, str(args.scene_source.resolve(strict=True)))
    from cloud_render import CloudRender
    from cloud_render_production import DriveIntake
    uid = pwd.getpwnam(args.client_user).pw_uid
    if args.socket.exists() or args.socket.is_symlink():
        raise RuntimeError("Refusing to replace an existing bridge socket")
    with Server(args.socket, DriveIntake(CloudRender.execute), CloudRender.execute, uid, args.spool) as server:
        os.chmod(args.socket, 0o660)
        try:
            server.serve_forever()
        finally:
            args.socket.unlink(missing_ok=True)


if __name__ == "__main__":
    main()

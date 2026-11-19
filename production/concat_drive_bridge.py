"""Restricted Concat Drive bridge over a peer-authenticated Unix socket.

Run separately as the canonical Scene identity. No shell, caller paths, remote
names, OAuth material or arbitrary rclone arguments cross this boundary.
The sole output write is a verified MP4 to the fixed Concat Outputs folder.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
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
MAX_MP4 = 20 * 1024 ** 3
OUTPUT_FOLDER = "Concat Outputs"
MP4_NAME = re.compile(r"[A-Za-z0-9_.() -]{5,150}")
OUTPUT_JOB = re.compile(r"[A-Za-z0-9_-]{1,80}")
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

    def upload_output(self):
        """Accept only an owner-tagged, checksummed MP4 into Concat Outputs.

        The authenticated Concat service streams bytes, not caller-selected
        local paths. Remote name and root are constructed here, and existing
        Drive objects are never overwritten. No generic write/delete API.
        """
        try:
            allowed = (
                "Content-Length", "Content-Type", "X-Owner", "X-Job-ID",
                "X-File-Name", "X-Content-SHA256", "X-Content-MD5",
            )
            fields = {}
            for key in allowed:
                values = self.headers.get_all(key, [])
                if len(values) != 1:
                    raise ValueError()
                fields[key] = values[0]
            if self.headers.get("Transfer-Encoding"):
                raise ValueError()
            if fields["Content-Type"] != "application/octet-stream":
                raise ValueError()
            owner, job, name = (fields["X-Owner"], fields["X-Job-ID"],
                                fields["X-File-Name"])
            sha, md5 = fields["X-Content-SHA256"], fields["X-Content-MD5"]
            size = int(fields["Content-Length"])
            if (not re.fullmatch(r"[a-f0-9]{64}", owner)
                    or not OUTPUT_JOB.fullmatch(job)
                    or not MP4_NAME.fullmatch(name)
                    or not name.lower().endswith(".mp4")
                    or not re.fullmatch(r"[a-f0-9]{64}", sha)
                    or not re.fullmatch(r"[a-f0-9]{32}", md5)
                    or not 0 < size <= MAX_MP4):
                raise ValueError()

            if shutil.disk_usage(self.server.spool).free - 5_000_000_000 < size:
                self.json(507, {"error": "insufficient_spool"})
                return

            remote = f"{self.server.intake.remote()}{OUTPUT_FOLDER}/{owner}/{job}_{name}"
            with tempfile.TemporaryDirectory(prefix="output-", dir=self.server.spool) as temp:
                target = Path(temp) / "output.mp4"
                sha_seen = hashlib.sha256()
                md5_seen = hashlib.md5(usedforsecurity=False)
                remaining = size
                with target.open("xb") as handle:
                    while remaining:
                        data = self.rfile.read(min(1024 * 1024, remaining))
                        if not data:
                            raise ValueError()
                        handle.write(data)
                        sha_seen.update(data)
                        md5_seen.update(data)
                        remaining -= len(data)
                if (not hmac.compare_digest(sha_seen.hexdigest(), sha)
                        or not hashlib.compare_digest(md5_seen.hexdigest(), md5)):
                    self.json(422, {"error": "content_mismatch"})
                    return

                def stat():
                    try:
                        return json.loads(self.server.execute(
                            ["rclone", "lsjson", remote, "--stat", "--hash"], 60))
                    except Exception as exc:
                        if getattr(exc, "category", "") == "remote_file_absent":
                            return None
                        raise

                previous = stat()
                if previous is None:
                    self.server.execute(
                        ["rclone", "copyto", str(target), remote, "--immutable"], 3600)
                    previous = stat()
                hashes = (previous or {}).get("Hashes") or {} if isinstance(previous, dict) else {}
                actual_md5 = hashes.get("MD5") or hashes.get("md5") or ""
                if (not isinstance(previous, dict)
                        or previous.get("Size") != size
                        or not isinstance(actual_md5, str)
                        or not hashlib.compare_digest(actual_md5.lower(), md5)):
                    self.json(409, {"error": "remote_conflict"})
                    return
                self.json(200, {"ok": True, "remote": remote, "size": size, "md5": md5})
        except (ValueError, TypeError, KeyError):
            self.json(400, {"error": "invalid_upload"})
        except Exception as exc:
            category = str(getattr(exc, "category", "")).lower()
            code = ("authentication" if "authentication" in category else
                    "output_denied" if "permission" in category else
                    "configuration" if "configuration" in category else
                    "unavailable")
            self.json(502, {"error": code})

    def do_POST(self):
        if self.path == "/v1/upload":
            return self.upload_output()
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

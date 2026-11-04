"""Isolated HTTP bridge for Concat v0.2.8's native project/export API.

The HTTP process persists; one native Concat export runs at a time. No shell
commands, user-chosen output paths, secrets in git, or public GPU promotion.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cloud_render.concat.project_bundle import restore
from cloud_render.concat.gpu.resumable import ResumableUpload, UploadError
from cloud_render.concat.gpu.vulkan_probe import probe as probe_vulkan
from cloud_render.concat.gpu.cuda_backend import probe_cuda, render as render_cuda

IDS = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
DIGESTS = re.compile(r"^[a-f0-9]{64}$")
CHUNK = 8 * 1024 * 1024
MAX_BUNDLE = 4 * 1024 * 1024 * 1024
MAX_QUEUE = 20
MP4_SIGNATURE = b"ftyp"


class WorkerError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def digest_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for piece in iter(lambda: stream.read(CHUNK), b""):
            h.update(piece)
    return h.hexdigest()


def check_id(job: str) -> str:
    if not IDS.fullmatch(job):
        raise WorkerError(400, "Invalid job ID")
    return job


class RenderWorker:
    def __init__(self, root: Path, *, binary: Path, token: str,
                 receiver: str = "", receiver_token: str = "", renderer=None,
                 deliver=None, backend="native"):
        if len(token) < 32:
            raise RuntimeError("RENDERER_API_TOKEN must have at least 32 characters")
        self.root = root.resolve()
        self.binary = binary
        self.token = token
        self.receiver = receiver.rstrip("/")
        self.receiver_token = receiver_token
        if backend not in ("native", "cuda_nvenc"):
            raise RuntimeError("CONCAT_RENDER_BACKEND must be native or cuda_nvenc")
        self.backend = backend
        self.renderer = renderer or (self._cuda_render if backend == "cuda_nvenc" else self._native_render)
        self.deliver = deliver or self._deliver
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="concat-export")
        self.state = self.root / "state"
        self.inputs = self.root / "jobs"
        self.outputs = self.root / "outputs"
        for folder in (self.root, self.state, self.inputs, self.outputs):
            if folder.is_symlink():
                raise RuntimeError("Worker folders must not be symlinks")
            folder.mkdir(parents=True, exist_ok=True)
            folder.chmod(0o700)
        self.spool = ResumableUpload(self)
        # Do not pretend a stopped or interrupted render succeeded.
        for path in self.state.glob("*.json"):
            if not IDS.fullmatch(path.stem) or path.is_symlink():
                continue
            try:
                item = json.loads(path.read_text())
                if item.get("state") in ("queued", "running", "delivering"):
                    self._save(path.stem, {**item, "state": "failed",
                        "error": "Interrupted by worker restart; source bundle retained"})
            except (OSError, ValueError):
                continue

    def _path(self, job: str) -> Path:
        return self.state / (check_id(job) + ".json")

    def _save(self, job: str, record: dict):
        path = self._path(job)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        os.replace(temp, path)

    def status(self, job: str) -> dict:
        path = self._path(job)
        if not path.is_file() or path.is_symlink():
            raise WorkerError(404, "Unknown job")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise WorkerError(500, "Corrupted job state") from None

    def _change(self, job: str, **fields):
        with self.lock:
            item = self.status(job)
            item.update(fields)
            self._save(job, item)

    def ready(self) -> bool:
        if self.backend == "cuda_nvenc":
            return self.state.is_dir() and probe_cuda()["cuda_nvenc_ready"]
        return self.binary.is_file() and os.access(self.binary, os.X_OK) and self.state.is_dir()

    def accept(self, job: str, stream, length: int, checksum: str) -> dict:
        job = check_id(job)
        if not 0 < length <= MAX_BUNDLE or not DIGESTS.fullmatch(checksum):
            raise WorkerError(400, "Invalid bundle size or SHA256")
        with self.lock:
            if self._path(job).exists() or (self.inputs / job).exists():
                raise WorkerError(409, "Job ID already exists")
            busy = sum(1 for path in self.state.glob("*.json")
                       if self._read_state(path) in ("queued", "running", "delivering"))
            if busy >= MAX_QUEUE:
                raise WorkerError(429, "Worker queue is full")
            directory = self.inputs / job
            directory.mkdir(mode=0o700)
            target = directory / "project.zip"
            try:
                h = hashlib.sha256()
                remaining = length
                with target.open("xb") as output:
                    while remaining:
                        part = stream.read(min(CHUNK, remaining))
                        if not part:
                            raise WorkerError(400, "Incomplete project upload")
                        output.write(part)
                        h.update(part)
                        remaining -= len(part)
                    output.flush()
                    os.fsync(output.fileno())
                if not hmac.compare_digest(h.hexdigest(), checksum):
                    raise WorkerError(422, "Bundle SHA256 mismatch")
                self._save(job, {"job": job, "state": "queued", "size": length,
                                 "sha256": checksum, "created": int(time.time()), "error": ""})
            except BaseException:
                shutil.rmtree(directory)
                raise
            self.pool.submit(self._process, job)
            return self.status(job)

    @staticmethod
    def _read_state(path: Path) -> str:
        try:
            return json.loads(path.read_text()).get("state", "")
        except (OSError, ValueError):
            return ""

    def _process(self, job: str):
        directory = self.inputs / job
        output = self.outputs / (job + ".mp4")
        try:
            self._change(job, state="running")
            extracted = directory / "project"
            restore(directory / "project.zip", extracted)
            report = self.renderer(extracted, output)
            if isinstance(report, dict) and report.get("event_log"):
                source_log = directory / report["event_log"]
                if source_log.is_file() and not source_log.is_symlink():
                    target_log = self.state / (job + ".events.jsonl")
                    shutil.copyfile(source_log, target_log)
            if not output.is_file() or output.is_symlink() or output.stat().st_size < 12:
                raise RuntimeError("Concat did not produce an MP4")
            with output.open("rb") as handle:
                header = handle.read(12)
            if header[4:8] != MP4_SIGNATURE:
                raise RuntimeError("Concat output MP4 signature invalid")
            details = report if isinstance(report, dict) else {}
            self._change(job, state="rendered", output_size=output.stat().st_size,
                         output_sha256=digest_file(output), backend=self.backend,
                         skipped_effects=details.get("skipped_effects", 0),
                         event_log=details.get("event_log", ""))
            if self.receiver:
                self._change(job, state="delivering")
                self.deliver(job, output)
                self._change(job, state="delivered")
                # GPU side cache only: VPS has confirmed the complete file.
                output.unlink()
                shutil.rmtree(directory)
        except Exception as exc:
            # Source bundle and output, if any, remain for diagnosis/retry.
            prior = self.status(job)
            if self.backend == "cuda_nvenc":
                log = self.state / (job + ".events.jsonl")
                failure = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                           "job_id": job, "renderer": "cuda-nvenc-candidate-v1",
                           "decision": "failed", "reason_code": "CUDA_RENDER_FAILED",
                           "message": str(exc)[:500]}
                with log.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(failure) + "\n")
            self._change(job, state="delivery_failed" if prior["state"] == "delivering" else "failed",
                         error=str(exc)[:500])

    def _cuda_render(self, project: Path, output: Path):
        return render_cuda(project, output, job_id=output.stem)

    def _native_render(self, project: Path, output: Path):
        # Upstream API preserves all project effects/settings and audio.
        calls = [
            {"jsonrpc": "2.0", "id": 1, "method": "project.open", "params": {"path": str(project)}},
            {"jsonrpc": "2.0", "id": 2, "method": "export.run",
             "params": {"path": str(project), "output": str(output)}},
        ]
        data = ("\n".join(json.dumps(item) for item in calls) + "\n").encode()
        log = project.parent / "engine.jsonl"
        err = project.parent / "engine.stderr"
        with log.open("wb") as stdout, err.open("wb") as stderr:
            result = subprocess.run([str(self.binary), "api"], input=data, stdout=stdout,
                                    stderr=stderr, timeout=int(os.getenv("CONCAT_RENDER_TIMEOUT", "7200")),
                                    check=False, cwd=str(project.parent))
        seen_open = seen_start = seen_done = False
        error = ""
        with log.open("rb") as lines:
            for raw in lines:
                if len(raw) > 1024 * 1024:
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if msg.get("id") == 1:
                    seen_open = "result" in msg and "error" not in msg
                    if "error" in msg:
                        error = "project.open: " + str(msg["error"])[:250]
                if msg.get("id") == 2:
                    seen_start = "result" in msg and "error" not in msg
                    if "error" in msg:
                        error = "export.run: " + str(msg["error"])[:250]
                if msg.get("method") == "export.done":
                    seen_done = True
                if msg.get("method") == "export.failed":
                    error = "Concat export.failed: " + str(msg.get("params", {}))[:250]
        if result.returncode or error or not (seen_open and seen_start and seen_done):
            raise RuntimeError(error or f"Concat API incomplete/exit {result.returncode}")

    def _deliver(self, job: str, output: Path):
        if not self.receiver.startswith("https://") or not self.receiver_token:
            raise RuntimeError("HTTPS VPS receiver and RENDER_OUTPUT_TOKEN required")
        host = urllib.parse.urlsplit(self.receiver)
        if host.username or host.password or host.query or host.fragment or host.path not in ("", "/"):
            raise RuntimeError("VPS receiver must be an HTTPS origin, not a URL path")
        length = output.stat().st_size
        checksum = digest_file(output)
        base = self.receiver + "/api/cloud-render/ingest/" + job + "/chunk"
        with output.open("rb") as stream:
            for index in range((length + CHUNK - 1) // CHUNK):
                chunk = stream.read(CHUNK)
                url = base + "?" + urllib.parse.urlencode({
                    "index": index, "name": job + ".mp4", "size": length, "sha256": checksum})
                request = urllib.request.Request(url, data=chunk, method="POST",
                    headers={"X-Cloud-Render-Token": self.receiver_token,
                             "Content-Type": "application/octet-stream"})
                with urllib.request.urlopen(request, timeout=120) as response:
                    payload = json.load(response)
                if not payload.get("ok"):
                    raise RuntimeError("VPS chunk receiver rejected upload")
                if index == (length + CHUNK - 1) // CHUNK - 1 and payload.get("state") != "ready":
                    raise RuntimeError("VPS did not verify finished MP4")

    def retry_delivery(self, job: str) -> dict:
        with self.lock:
            entry = self.status(job)
            if entry["state"] not in ("rendered", "delivery_failed") or not self.receiver:
                raise WorkerError(409, "Job is not awaiting delivery")
            target = self.outputs / (job + ".mp4")
            if not target.is_file() or target.is_symlink():
                raise WorkerError(409, "Output missing")
            self._change(job, state="delivering", error="")
            self.pool.submit(self._retry, job, target)
            return self.status(job)

    def _retry(self, job: str, output: Path):
        try:
            self.deliver(job, output)
            self._change(job, state="delivered")
            output.unlink()
            shutil.rmtree(self.inputs / job, ignore_errors=True)
        except Exception as exc:
            self._change(job, state="delivery_failed", error=str(exc)[:500])


class SaladDualStackHTTPServer(ThreadingHTTPServer):
    """Listen on IPv6 for Salad Gateway and IPv4 for local/CI clients.

    Salad Gateway forwards requests over IPv6 to the container. Explicitly
    clear IPV6_V6ONLY so the existing IPv4 health checks still work.
    Fail startup instead of silently exposing a broken IPv4-only gateway.
    """
    address_family = socket.AF_INET6

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


class Handler(BaseHTTPRequestHandler):
    server_version = "ConcatGPU/1"
    protocol_version = "HTTP/1.1"

    @property
    def worker(self) -> RenderWorker:
        return self.server.worker

    def _json(self, code: int, data: dict):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _authorize(self):
        expected = "Bearer " + self.worker.token
        actual = self.headers.get("Authorization", "")
        if not hmac.compare_digest(actual, expected):
            raise WorkerError(401, "Bearer token required")

    def _route(self, action: str):
        path = urllib.parse.urlsplit(self.path).path
        if action == "GET" and path == "/health/live":
            return self._json(200, {"ok": True})
        if action == "GET" and path == "/health/ready":
            ok = self.worker.ready()
            return self._json(200 if ok else 503, {"ok": ok,
                "engine": self.worker.backend, "renderer_version": "cuda-nvenc-candidate-v1"
                if self.worker.backend == "cuda_nvenc" else "concat-cli-v0.2.8"})
        self._authorize()
        if action == "GET" and path == "/api/diagnostics/gpu":
            result = probe_vulkan()
            result.update(probe_cuda())
            result["selected_backend"] = self.worker.backend
            return self._json(200, result)
        upload = re.fullmatch(
            r"/api/uploads/([A-Za-z0-9_-]{1,80})(?:/chunks/([0-9]{1,4})|/(complete))?", path)
        if upload:
            job, part, complete = upload.groups()
            if action == "GET" and part is None and complete is None:
                return self._json(200, self.worker.spool.status(job))
            if action == "POST" and part is None and complete is None:
                if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    raise WorkerError(415, "Start upload must be application/json")
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    raise WorkerError(400, "Invalid Content-Length") from None
                if not 0 < length <= 1024:
                    raise WorkerError(400, "Invalid upload manifest length")
                try:
                    manifest = json.loads(self.rfile.read(length))
                except (ValueError, UnicodeDecodeError):
                    raise WorkerError(400, "Invalid upload manifest JSON") from None
                if not isinstance(manifest, dict):
                    raise WorkerError(400, "Upload manifest must be an object")
                return self._json(200, self.worker.spool.begin(
                    job, manifest.get("size"), manifest.get("sha256")))
            if action == "PUT" and part is not None:
                if self.headers.get("Content-Type", "").split(";")[0] != "application/octet-stream":
                    raise WorkerError(415, "Chunk must be application/octet-stream")
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    raise WorkerError(400, "Invalid Content-Length") from None
                return self._json(200, self.worker.spool.put_chunk(
                    job, int(part), self.rfile, length,
                    self.headers.get("X-Content-SHA256", "")))
            if action == "POST" and complete is not None:
                return self._json(202, self.worker.spool.complete(job))
            raise WorkerError(405, "Method not allowed")
        m = re.fullmatch(r"/api/jobs/([A-Za-z0-9_-]{1,80})(/output|/retry-delivery|/logs)?", path)
        if not m:
            raise WorkerError(404, "Unknown route")
        job, suffix = m.groups()
        if action == "GET" and not suffix:
            return self._json(200, self.worker.status(job))
        if action == "GET" and suffix == "/logs":
            self.worker.status(job)  # no logs for unknown job
            file = self.worker.state / (job + ".events.jsonl")
            if not file.is_file():
                file = self.worker.inputs / job / "engine-events.jsonl"
            if not file.is_file() or file.is_symlink():
                return self._json(200, {"job": job, "events": []})
            # Bounded, authenticated structured events (no secrets).
            if file.stat().st_size > 2 * 1024 * 1024:
                raise WorkerError(413, "Render log too large")
            lines = [json.loads(line) for line in file.read_text().splitlines() if line]
            return self._json(200, {"job": job, "events": lines})
        if action == "POST" and not suffix:
            if self.headers.get("Content-Type", "").split(";")[0] != "application/zip":
                raise WorkerError(415, "Upload must be application/zip")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise WorkerError(400, "Invalid Content-Length") from None
            entry = self.worker.accept(job, self.rfile, length,
                                       self.headers.get("X-Content-SHA256", ""))
            return self._json(202, entry)
        if action == "POST" and suffix == "/retry-delivery":
            return self._json(202, self.worker.retry_delivery(job))
        if action == "GET" and suffix == "/output":
            item = self.worker.status(job)
            output = self.worker.outputs / (job + ".mp4")
            if item["state"] not in ("rendered", "delivery_failed") or not output.is_file() or output.is_symlink():
                raise WorkerError(404, "Output unavailable")
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(output.stat().st_size))
            self.send_header("Cache-Control", "private, no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            with output.open("rb") as handle:
                shutil.copyfileobj(handle, self.wfile, CHUNK)
            return
        raise WorkerError(405, "Method not allowed")

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def _handle(self, action):
        self.connection.settimeout(180)
        try:
            self._route(action)
        except (WorkerError, UploadError) as exc:
            self.close_connection = True
            self._json(exc.status, {"ok": False, "error": str(exc)})
        except Exception:
            self.close_connection = True
            self._json(500, {"ok": False, "error": "Internal worker failure"})

    def log_message(self, format, *args):
        # Do not log Authorization headers, receiver tokens or URLs with secrets.
        super().log_message(format, *args)


def main():
    token = os.getenv("RENDERER_API_TOKEN", "")
    worker = RenderWorker(Path(os.getenv("CONCAT_WORK_ROOT", "/worker")),
                          binary=Path(os.getenv("CONCAT_BINARY", "/usr/local/bin/concat-cli")),
                          token=token,
                          receiver=os.getenv("CLOUD_RENDER_VPS_BASE_URL", ""),
                          receiver_token=os.getenv("RENDER_OUTPUT_TOKEN", ""),
                          backend=os.getenv("CONCAT_RENDER_BACKEND", "native"))
    port = int(os.getenv("PORT", "8080"))
    server = SaladDualStackHTTPServer(("::", port), Handler)
    server.worker = worker
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        worker.pool.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    main()

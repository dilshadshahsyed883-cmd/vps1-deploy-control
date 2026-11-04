"""Authenticated Salad GPU job client, checksum-verified/resumable transfer.

The Salad gateway API key and renderer bearer token are always separate.
Secrets are never written to files or included in log output.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid

from cloud_render.concat.project_bundle import pack

CHUNK = 8 * 1024 * 1024


class GatewayError(RuntimeError):
    pass


def origin_only(value: str) -> str:
    parsed = urllib.parse.urlsplit(value.strip())
    if (parsed.scheme != "https" or not parsed.netloc or parsed.username or
        parsed.password or parsed.path not in ("", "/") or parsed.query or
        parsed.fragment or parsed.port not in (None, 443)):
        raise GatewayError("Enter a Salad HTTPS gateway origin (no URL path)")
    return "https://" + parsed.netloc.lower()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise GatewayError("Gateway redirected the request; refusing to forward credentials")


class Gateway:
    def __init__(self, url: str, salad_key: str, renderer_token: str):
        self.base = origin_only(url)
        if not salad_key or not renderer_token:
            raise GatewayError("Both Salad API Key and Renderer API Token are required")
        self.headers = {"Salad-Api-Key": salad_key,
                        "Authorization": "Bearer " + renderer_token}
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, method: str, endpoint: str, body: bytes | None = None,
                extra: dict | None = None, timeout: int = 95):
        headers = {**self.headers, **(extra or {})}
        req = urllib.request.Request(self.base + endpoint, body,
                                     headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise GatewayError("Salad gateway HTTP " + str(exc.code)) from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise GatewayError("Salad gateway unavailable or returned invalid data") from None

    def diagnose(self) -> dict:
        result = self.request("GET", "/api/diagnostics/gpu", timeout=30)
        if not isinstance(result, dict):
            raise GatewayError("Invalid GPU diagnostics")
        return result

    def require_hardware(self):
        info = self.diagnose()
        if info.get("nvidia_vulkan_visible") is not True:
            raise GatewayError("NVIDIA Vulkan not visible; refusing paid GPU render")
        return info

    def job(self, job_id: str):
        return self.request("GET", "/api/jobs/" + job_id, timeout=30)

    def upload(self, project: Path, on_progress=None) -> str:
        """Pack from canonical Concat project, upload each SHA-verified chunk."""
        on_progress = on_progress or (lambda message: None)
        project = Path(project).resolve(strict=True)
        if not project.is_dir() or not (project / "concat.json").is_file():
            raise GatewayError("Select a saved native Concat project folder")
        self.require_hardware()
        with tempfile.TemporaryDirectory(prefix="concat-studio-") as tmp:
            bundle = Path(tmp) / "project.zip"
            pack(project, bundle)
            total = bundle.stat().st_size
            if total > 4 * 1024**3:
                raise GatewayError("GPU bundle is above worker's 4 GiB limit")
            whole = sha256_file(bundle)
            # Never reuse a previous job id for a different archive.
            job_id = "concat-" + uuid.uuid4().hex[:20]
            base = "/api/uploads/" + job_id
            started = self.request("POST", base,
                                   json.dumps({"size": total, "sha256": whole}).encode(),
                                   {"Content-Type": "application/json"}, 30)
            chunk_size = started.get("chunk_size")
            count = started.get("total_chunks")
            if not isinstance(chunk_size, int) or not 0 < chunk_size <= CHUNK:
                raise GatewayError("Invalid upload chunk size")
            expected = (total + chunk_size - 1) // chunk_size
            if count != expected:
                raise GatewayError("Invalid upload chunk count")
            completed = set(started.get("uploaded", []))
            with bundle.open("rb") as stream:
                for index in range(count):
                    raw = stream.read(chunk_size)
                    if index in completed:
                        on_progress("Chunk " + str(index + 1) + "/" + str(count) + " already verified")
                        continue
                    digest = hashlib.sha256(raw).hexdigest()
                    for attempt in range(3):
                        try:
                            self.request("PUT", base + "/chunks/" + str(index), raw,
                                         {"Content-Type": "application/octet-stream",
                                          "X-Content-SHA256": digest})
                            break
                        except GatewayError:
                            resume = self.request("GET", base, timeout=30)
                            if index in resume.get("uploaded", []):
                                break
                            if attempt == 2:
                                raise
                    on_progress("Uploaded " + str(index + 1) + "/" + str(count) + " chunks")
            confirmed = self.request("POST", base + "/complete", b"", timeout=90)
            if confirmed.get("state") not in ("queued", "running", "rendered", "delivering", "delivered"):
                raise GatewayError("GPU worker did not accept completed upload")
            return job_id

    def download(self, job_id: str, destination: Path):
        status = self.job(job_id)
        if status.get("state") not in ("rendered", "delivery_failed"):
            raise GatewayError("MP4 not available on GPU; check VPS for delivered jobs")
        expected = status.get("output_sha256", "")
        if len(expected) != 64:
            raise GatewayError("GPU output checksum unavailable")
        destination = Path(destination).expanduser()
        if destination.exists():
            raise GatewayError("Destination exists; refusing overwrite")
        destination.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(self.base + "/api/jobs/" + job_id + "/output",
                                     headers=self.headers, method="GET")
        scratch = destination.with_suffix(destination.suffix + ".partial")
        if scratch.exists():
            raise GatewayError("Partial output exists; choose another destination")
        try:
            with self.opener.open(req, timeout=120) as response, scratch.open("xb") as out:
                while True:
                    part = response.read(CHUNK)
                    if not part:
                        break
                    out.write(part)
            if sha256_file(scratch) != expected:
                raise GatewayError("GPU MP4 SHA256 mismatch")
            with scratch.open("rb") as stream:
                if stream.read(12)[4:8] != b"ftyp":
                    raise GatewayError("GPU output is not an MP4")
            scratch.replace(destination)
            return destination
        except Exception:
            scratch.unlink(missing_ok=True)
            raise


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


class WindowsSecrets:
    """Explicit Windows Credential Manager backend, never plaintext fallback."""
    SERVICE = "ConcatStudio.SaladCloud.v1"
    def __init__(self):
        import sys
        if sys.platform != "win32":
            raise GatewayError("Saved GPU secrets require Windows Credential Manager")
        try:
            from keyring.backends.Windows import WinVaultKeyring
        except ImportError as exc:
            raise GatewayError("Windows keyring dependency not installed") from exc
        self.vault = WinVaultKeyring()

    def get(self, key: str) -> str:
        return self.vault.get_password(self.SERVICE, key) or ""

    def set(self, key: str, value: str):
        if value.strip():
            self.vault.set_password(self.SERVICE, key, value)

    def delete(self, key: str):
        try:
            self.vault.delete_password(self.SERVICE, key)
        except Exception:
            pass

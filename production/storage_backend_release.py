#!/usr/bin/env python3
"""Guarded updater for the existing VPS_1 Storage Manager candidate service.

This adapter owns only the immutable Storage Manager source pointer and its
existing candidate service. It preserves the candidate environment, state
directory and unit, and restores the previous pointer if restart or probes fail.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request

BASE = Path("/srv/fresh-website")
CURRENT = BASE / "current/vps-storage-manager"
RELEASES = BASE / "releases/vps-storage-manager"
STATE = Path("/var/lib/fresh-website/vps-storage-manager")
ENV = Path("/etc/fresh-website/vps-storage-manager.env")
UNIT = "vps1-storage-candidate.service"
RECORD = BASE / "control/state/vps-storage-manager.json"
PORT = 8149


def _run(args: list[str], *, timeout: int = 90) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=timeout, check=False)


def _token() -> str:
    if not ENV.is_file() or ENV.is_symlink() or ENV.stat().st_size > 16384:
        raise RuntimeError("Storage Manager candidate environment is unavailable")
    values = []
    for line in ENV.read_text(encoding="utf-8").splitlines():
        if line.startswith("STORAGE_MANAGER_TOKEN="):
            values.append(line.split("=", 1)[1].strip().strip("'").strip('"'))
    if len(values) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]{24,256}", values[0]):
        raise RuntimeError("Storage Manager candidate token is invalid")
    return values[0]


def _request(path: str, token: str | None = None) -> tuple[int, bytes]:
    headers = {"Connection": "close"}
    if token is not None:
        headers["X-Storage-Token"] = token
    request = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}", headers=headers
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=12) as response:
            return response.status, response.read(512 * 1024)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(512 * 1024)


def _verify() -> dict:
    status, body = _request("/health")
    try:
        health = json.loads(body)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("Storage Manager health returned invalid JSON") from exc
    if status != 200 or health.get("ok") is not True:
        raise RuntimeError("Storage Manager health failed")
    status, body = _request("/v1/metrics", _token())
    try:
        metrics = json.loads(body)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("Storage Manager metrics returned invalid JSON") from exc
    if status != 200 or metrics.get("ok") is not True:
        raise RuntimeError("Storage Manager metrics failed")
    for field in ("cpu", "memory", "disk"):
        if not isinstance(metrics.get(field), dict):
            raise RuntimeError("Storage Manager required metric missing: " + field)
    for field in ("services", "service_details", "startable_services", "processes"):
        if field in metrics:
            raise RuntimeError("obsolete service/process metric returned: " + field)
    return {
        "health": health,
        "metrics": {"cpu": True, "memory": True, "disk": True},
        "retired_metric_fields": "ABSENT",
        "service": UNIT,
    }


def _stage(revision: str) -> Path:
    from rebuild.fresh_vps import sources
    files = sources("vps-storage-manager")
    target = RELEASES / revision
    manifest = {
        "app": "vps-storage-manager",
        "revision": revision,
        "files": {name: hashlib.sha256(content).hexdigest() for name, content in files},
    }
    manifest_path = target / "_storage_backend_manifest.json"
    if target.exists():
        if not manifest_path.is_file() or json.loads(manifest_path.read_text()) != manifest:
            raise RuntimeError("existing Storage Manager release is not owned by this adapter")
        for name, digest in manifest["files"].items():
            path = target / name
            if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise RuntimeError("Storage Manager source drift")
        return target
    target.mkdir(parents=True)
    try:
        for name, content in files:
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            path.chmod(0o644)
        manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n")
        manifest_path.chmod(0o644)
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return target


def _current() -> Path:
    if not CURRENT.is_symlink():
        raise RuntimeError("Storage Manager current pointer is not a symlink")
    previous = CURRENT.resolve(strict=True)
    if not previous.is_relative_to(RELEASES):
        raise RuntimeError("Storage Manager current pointer escaped its release root")
    return previous


def _restart_and_verify() -> dict:
    result = _run(["systemctl", "restart", UNIT], timeout=90)
    if result.returncode:
        raise RuntimeError("Storage Manager restart failed")
    deadline = time.monotonic() + 45
    last = ""
    while time.monotonic() < deadline:
        result = _run(["systemctl", "is-active", "--quiet", UNIT], timeout=15)
        if result.returncode == 0:
            try:
                return _verify()
            except RuntimeError as exc:
                last = str(exc)
        time.sleep(.5)
    raise RuntimeError("Storage Manager did not recover: " + last)


def _rollback(previous: Path) -> None:
    from production.runtime import atomic_link
    atomic_link(CURRENT, previous)
    result = _run(["systemctl", "restart", UNIT], timeout=90)
    if result.returncode:
        raise RuntimeError("Storage Manager rollback restart failed")
    _verify()


def install(revision: str) -> None:
    from production.runtime import atomic_json, atomic_link, backup_databases, event

    previous = _current()
    if not ENV.is_file() or not STATE.is_dir():
        raise RuntimeError("Storage Manager candidate ownership is incomplete")
    if _run(["systemctl", "is-active", "--quiet", UNIT], timeout=15).returncode:
        raise RuntimeError("Storage Manager candidate service is inactive")
    before = _verify()
    target = _stage(revision)
    if previous == target:
        event(app="vps-storage-manager", result="PASS_ALREADY_INSTALLED",
              revision=revision, port=PORT, verification=before)
        return

    journal = BASE / "transactions/storage-backend" / (revision + "-" + str(time.time_ns()))
    journal.mkdir(parents=True)
    atomic_json(journal / "previous.json",
                {"current": str(previous), "unit": UNIT, "state": str(STATE)})
    backups = backup_databases(STATE, journal / "sqlite-backup")
    changed = False
    try:
        atomic_link(CURRENT, target)
        changed = True
        verification = _restart_and_verify()
        atomic_json(RECORD, {
            "app": "vps-storage-manager", "revision": revision, "status": "ACTIVE",
            "unit": UNIT, "previous": str(previous), "transaction": str(journal),
        })
        event(app="vps-storage-manager", result="PASS_BACKEND_RETIREMENT",
              revision=revision, port=PORT, verification=verification,
              sqlite_backups=len(backups), rollback=str(journal))
    except BaseException:
        if changed:
            _rollback(previous)
            event(app="vps-storage-manager", rollback="PASS", data="POINTER_AND_SERVICE_PRESERVED")
        raise


def rollback() -> None:
    from production.runtime import atomic_link
    if not RECORD.is_file():
        raise RuntimeError("Storage Manager rollback record is unavailable")
    record = json.loads(RECORD.read_text())
    previous = Path(record["previous"]).resolve()
    if not previous.is_relative_to(RELEASES):
        raise RuntimeError("unsafe Storage Manager rollback target")
    if not (previous / "_storage_backend_manifest.json").is_file():
        raise RuntimeError("Storage Manager rollback source is unavailable")
    atomic_link(CURRENT, previous)
    result = _run(["systemctl", "restart", UNIT], timeout=90)
    if result.returncode:
        raise RuntimeError("Storage Manager rollback restart failed")
    _verify()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("install", "rollback"))
    parser.add_argument("--revision", required=False)
    args = parser.parse_args()
    if args.action == "install":
        if not args.revision or not re.fullmatch(r"[a-f0-9]{40}", args.revision):
            raise ValueError("exact protected-main revision required")
        install(args.revision)
    else:
        rollback()


if __name__ == "__main__":
    main()

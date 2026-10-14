#!/usr/bin/env python3
"""Read-only release artifact validator. It NEVER extracts or deploys anything."""
from __future__ import annotations
import argparse
import hashlib
import json
import pathlib
import re
import sys
import tarfile

MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
MAX_CONTENT_BYTES = 32 * 1024 * 1024
MAX_MEMBERS = 128
_ALLOWED_APP = "pilot-canary"  # no real app IDs until separately reviewed
_HEX = re.compile(r"[0-9a-f]{64}\Z")


def validate(manifest_path: pathlib.Path) -> str:
    manifest_path = manifest_path.resolve(strict=True)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {"schema_version", "app_id", "artifact", "sha256"}:
        raise ValueError("unrecognized manifest fields")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ValueError("unsupported schema_version")
    if data["app_id"] != _ALLOWED_APP:
        raise ValueError("app_id not allowlisted")
    name = data["artifact"]
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{1,100}\.tar\.gz", name) or name.startswith("."):
        raise ValueError("invalid artifact basename")
    expected = data["sha256"]
    if not isinstance(expected, str) or not _HEX.fullmatch(expected):
        raise ValueError("invalid sha256")
    path = manifest_path.parent / name
    if path.is_symlink() or not path.is_file():
        raise ValueError("artifact must be a regular file")
    if path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("artifact too large")
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != expected:
        raise ValueError("sha256 mismatch")
    with tarfile.open(path, "r:gz") as tar:
        items = tar.getmembers()
        if len(items) != 1 or len(items) > MAX_MEMBERS:
            raise ValueError("pilot must contain exactly one expected file")
        m = items[0]
        if m.name != "pilot/index.html" or not m.isfile() or m.size > MAX_CONTENT_BYTES:
            raise ValueError("unsupported archive contents")
        if m.mode & 0o111 or m.mode & 0o6000 or m.pax_headers:
            raise ValueError("unsafe archive metadata")
        with tar.extractfile(m) as extracted:
            site = extracted.read(MAX_CONTENT_BYTES+1)
        if b'id="pilot-test"' not in site or b"Pilot UI PASS" not in site:
            raise ValueError("pilot UI contract missing")
    return digest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=pathlib.Path, required=True)
    args = parser.parse_args()
    try:
        digest = validate(args.manifest)
    except (OSError, ValueError, json.JSONDecodeError, tarfile.TarError, EOFError) as exc:
        print(f"VALIDATION_BLOCKED: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print(f"VALIDATION_PASS sha256={digest}")

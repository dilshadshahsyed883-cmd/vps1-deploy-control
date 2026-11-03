#!/usr/bin/env python3
"""Verify a pinned official Concat Windows release asset before publishing it.

No untrusted installer is executed by CI. The upstream release is not rebuilt.
"""
from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path

ASSET_NAME = "Concat-0.2.8-windows-x86_64-setup.exe"
MIN_INSTALLER_SIZE = 1_000_000
SUM_RE = re.compile(r"([0-9a-fA-F]{64})\s+\*?(.+)")


def expected_digest(checksum_text: str, asset_name: str = ASSET_NAME) -> str:
    found = []
    for line in checksum_text.splitlines():
        match = SUM_RE.fullmatch(line.strip())
        if not match:
            continue
        filename = match.group(2).strip().replace("\\", "/").split("/")[-1]
        if filename == asset_name:
            found.append(match.group(1).lower())
    if len(found) != 1:
        raise ValueError(f"Expected exactly one SHA256SUMS entry for {asset_name}, got {len(found)}")
    return found[0]


def verify(installer: Path, checksums: Path) -> str:
    if installer.name != ASSET_NAME:
        raise ValueError(f"Unexpected installer name: {installer.name}")
    if not installer.is_file() or not checksums.is_file():
        raise FileNotFoundError("Installer or SHA256SUMS is missing")
    if installer.stat().st_size < MIN_INSTALLER_SIZE:
        raise ValueError("Installer is suspiciously small")
    expected = expected_digest(checksums.read_text(encoding="utf-8-sig"))
    with installer.open("rb") as fp:
        if fp.read(2) != b"MZ":
            raise ValueError("Installer lacks Windows PE MZ header")
        fp.seek(0)
        actual = hashlib.file_digest(fp, "sha256").hexdigest()
    if actual != expected:
        raise ValueError(f"Checksum mismatch: expected {expected}, got {actual}")
    return actual


def main() -> None:
    p = argparse.ArgumentParser(description="Verify Concat official Windows EXE")
    p.add_argument("--installer", type=Path, required=True)
    p.add_argument("--checksums", type=Path, required=True)
    args = p.parse_args()
    digest = verify(args.installer, args.checksums)
    print(f"Verified {args.installer.name}: SHA256={digest}")


if __name__ == "__main__":
    main()

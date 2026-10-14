#!/usr/bin/env python3
"""Build deterministic, no-secrets pilot tarball and manifest. No VPS actions."""
from __future__ import annotations
import gzip
import hashlib
import io
import json
import pathlib
import tarfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
DST = ROOT / "dist"


def build() -> pathlib.Path:
    html = (ROOT / "pilot" / "index.html").read_bytes()
    if b"pilot-test" not in html or b"Pilot UI PASS" not in html:
        raise ValueError("pilot UI test contract missing")
    DST.mkdir(exist_ok=True)
    archive = DST / "pilot-site.tar.gz"
    # Fixed tar metadata + gzip timestamp for repeatable hash.
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo("pilot/index.html")
        info.size = len(html)
        info.mode = 0o644
        info.mtime = 0
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        tar.addfile(info, io.BytesIO(html))
    with archive.open("wb") as stream:
        with gzip.GzipFile(fileobj=stream, mode="wb", filename="", mtime=0) as zipped:
            zipped.write(buf.getvalue())
    manifest = {"schema_version": 1, "app_id": "pilot-canary", "artifact": "pilot-site.tar.gz", "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}
    (DST / "release.json").write_text(json.dumps(manifest, indent=2, sort_keys=True)+"\n", encoding="utf-8")
    return DST / "release.json"


if __name__ == "__main__":
    print(f"PILOT_BUILD_PASS: {build()}")

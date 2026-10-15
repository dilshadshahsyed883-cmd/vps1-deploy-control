#!/usr/bin/env python3
"""Build an immutable, strictly allowlisted Auto Script source-only release."""
import argparse
import ast
import gzip
import hashlib
import io
import json
import pathlib
import re
import tarfile

FILES = (
    "auto_script.py",
    "templates/auto_script.html",
    "static/auto_script.js",
    "static/auto_script.css",
)
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?im)^\\s*(?:secret_key|password|api_key|access_token|refresh_token|client_secret)\\s*=\\s*['\\\"][^'\\\"]+['\\\"]"),
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),
    re.compile(r"AIza[A-Za-z0-9_-]{25,}"),
)


def validate_source(root):
    root = pathlib.Path(root)
    items = []
    for name in FILES:
        path = root / name
        if not path.is_file() or path.is_symlink():
            raise ValueError("Missing, nonregular, or symlink source: " + name)
        if path.stat().st_size > 2_000_000:
            raise ValueError("Oversize source: " + name)
        data = path.read_bytes()
        text = data.decode("utf-8")
        if "\\x00" in text or any(p.search(text) for p in SECRET_PATTERNS):
            raise ValueError("Unsafe source content: " + name)
        if name.endswith(".py"):
            ast.parse(text, filename=name)
        items.append((name, data))
    return items


def build(root, output):
    items = validate_source(root)
    output = pathlib.Path(output)
    output.mkdir(parents=True, exist_ok=True)
    catalog = {
        "app_id": "auto-script",
        "schema_version": 1,
        "files": [
            {"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in items
        ],
    }
    catalog_bytes = (json.dumps(catalog, sort_keys=True, separators=(",", ":")) + "\\n").encode()
    archive = output / "auto-script.tar.gz"
    with archive.open("wb") as fh:
        with gzip.GzipFile(fileobj=fh, mode="wb", mtime=0, filename="") as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.USTAR_FORMAT) as tar:
                for name, data in items + [("release-files.json", catalog_bytes)]:
                    entry = tarfile.TarInfo("auto-script/" + name)
                    entry.size = len(data)
                    entry.mode = 0o644
                    entry.mtime = 0
                    entry.uid = entry.gid = 0
                    entry.uname = entry.gname = ""
                    tar.addfile(entry, io.BytesIO(data))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    manifest = {
        "app_id": "auto-script",
        "artifact": archive.name,
        "schema_version": 1,
        "sha256": digest,
    }
    (output / "release.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\\n")
    print("AUTO_SCRIPT_RELEASE_SHA256=" + digest)
    return digest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="apps/auto-script")
    parser.add_argument("--out", default="dist/auto-script")
    args = parser.parse_args()
    build(args.source, args.out)

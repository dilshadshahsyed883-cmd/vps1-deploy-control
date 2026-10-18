#!/usr/bin/env python3
"""Recover only missing Scene Builder part02 modules from owner's SHA-verified ZIP.

Usage: python3 rebuild/tools/recover_scene_missing_from_owner_zip.py OWNER.zip
Dry-run by default; --apply on a GitHub source branch exclusively creates absent
paths. Existing files, even differing historical versions, are NEVER overwritten.
This is a SOURCE importer, not a VPS installer or production worker activator.
"""
from __future__ import annotations
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import zipfile

MODULES = (
    "audio_library_v174", "audio_mixer", "caption_sync",
    "creative_v21", "creative_v23", "ffmpeg_utils", "lut_engine",
    "persistence", "project_model", "reliability_engine",
    "scene_txt_pipeline", "transition_engine", "v2_renderer", "v3_engine",
)
RELATIVE = Path("apps/scene-builder/part02/smart_scroll")
OWNER_SHA256 = "0a8867fd3be32ded4a72cd67fadb5e0fef6f1fc3d73ca0f49809076164f60a40"

def recover(source: Path, checkout: Path, *, apply: bool = False) -> dict:
    if not (checkout / "apps/scene-builder/part02/api_server.py").is_file():
        raise RuntimeError("not a Scene Builder source checkout")
    source = source.resolve()
    if hashlib.sha256(source.read_bytes()).hexdigest() != OWNER_SHA256:
        raise RuntimeError("only the owner-provided archived source is accepted")
    data = {}
    with zipfile.ZipFile(source) as archive:
        files = archive.infolist()
        if len(files) > 300 or sum(f.file_size for f in files) > 25_000_000:
            raise RuntimeError("archive larger than verified owner source inventory")
        for entry in files:
            if entry.filename.startswith("/") or ".." in Path(entry.filename).parts or entry.flag_bits & 1:
                raise RuntimeError("unsafe ZIP member")
        names = set(archive.namelist())
        if "FILE_SHA256SUMS.txt" not in names:
            raise RuntimeError("original owner ZIP SHA manifest required")
        expected = {}
        for line in archive.read("FILE_SHA256SUMS.txt").decode().splitlines():
            match = re.fullmatch(r"([a-f0-9]{64})\s+(.*)", line)
            if match:
                expected[match[2].removeprefix("./")] = match[1]
        if len(expected) != 84:
            raise RuntimeError("unexpected source manifest count")
        for name, sha in expected.items():
            if name not in names or hashlib.sha256(archive.read(name)).hexdigest() != sha:
                raise RuntimeError("source SHA mismatch: " + name)
        for name in MODULES:
            path = "app/smart_scroll/" + name + ".py"
            if path not in names:
                raise RuntimeError("missing owner source: " + path)
            content = archive.read(path)
            ast.parse(content.decode("utf-8-sig"), filename=path)
            data[name] = content
    root = checkout / RELATIVE
    existing, missing, written = [], [], []
    for name, content in data.items():
        dst = root / (name + ".py")
        if dst.is_symlink():
            raise RuntimeError("refusing symlink: " + str(dst))
        if dst.exists():
            existing.append({"path": str(dst.relative_to(checkout)),
                             "same_as_zip": dst.read_bytes() == content})
        else:
            missing.append(str(dst.relative_to(checkout)))
    if apply:
        root.mkdir(parents=True, exist_ok=True)
        init = root / "__init__.py"
        if not init.exists():
            with init.open("x", encoding="utf-8") as stream:
                stream.write("# Scene Builder worker package source\n")
        for name, content in data.items():
            dst = root / (name + ".py")
            if dst.exists():
                continue
            with dst.open("xb") as stream:
                stream.write(content)
            written.append(str(dst.relative_to(checkout)))
    return {"verified_source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "manifest_files_verified": len(expected), "existing_preserved": existing,
            "missing": missing, "added": written}

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("zip", type=Path)
    parser.add_argument("--checkout", type=Path, default=Path.cwd())
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(recover(args.zip, args.checkout.resolve(), apply=args.apply), indent=2))

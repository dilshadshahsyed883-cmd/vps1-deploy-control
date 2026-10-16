#!/usr/bin/env python3
"""Stage parallel-owned Auto Script from its verified source manifest into /tmp only.

This intentionally never starts the shared Scene Builder router or changes
production services. Auto Script source remains owned by its parallel branch.
"""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

from rebuild import fresh_vps

SOURCE = Path(__file__).resolve().parents[1] / "apps/auto-script"
REQUIRED = {"auto_script.py", "templates/auto_script.html",
            "static/auto_script.js", "static/auto_script.css"}


def validated_files(source=SOURCE):
    manifest_path = source / "SOURCE_SHA256.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("Auto Script source manifest unavailable")
    manifest = json.loads(manifest_path.read_text())
    files = manifest["files"]
    names = [item["path"] for item in files]
    if set(names) != REQUIRED or len(names) != len(REQUIRED):
        raise ValueError("Auto Script source manifest does not match approved files")
    verified = {}
    for item in files:
        rel = PurePosixPath(item["path"])
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError("unsafe source path")
        file = source.joinpath(*rel.parts)
        if file.is_symlink() or not file.is_file():
            raise ValueError("missing or linked source")
        data = file.read_bytes()
        if len(data) != item["bytes"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise ValueError("Auto Script source digest mismatch")
        verified[item["path"]] = data
    return verified


def paths(root, revision):
    root = fresh_vps.root_dir(root)
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("invalid immutable revision")
    base = root / "srv/fresh-website"
    target = base / "releases/auto-script" / revision
    current = base / "current/auto-script"
    state = root / "var/lib/fresh-website/auto-script"
    return root, target, current, state


def stage(root, revision, source=SOURCE):
    root, target, current, state = paths(root, revision)
    files = validated_files(source)
    expected = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    candidate = {"app": "auto-script", "revision": revision, "files": expected}
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not (target / "_fresh_manifest.json").is_file():
            raise ValueError("invalid existing Auto Script release")
        if json.loads((target / "_fresh_manifest.json").read_text()) != candidate:
            raise ValueError("immutable Auto Script release conflict")
    else:
        target.mkdir(parents=True)
        for name, data in files.items():
            dst = target / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)
            dst.chmod(0o644)
        (target / "_fresh_manifest.json").write_text(
            json.dumps(candidate, sort_keys=True) + "\n")
    for sub in ("db", "queue", "uploads", "work"):
        (state / sub).mkdir(parents=True, exist_ok=True)
    if current.is_symlink():
        old = current.resolve(strict=True)
        if not old.is_relative_to(root / "srv/fresh-website/releases/auto-script"):
            raise ValueError("unsafe existing Auto Script release pointer")
        if old != target:
            (state / "previous.json").write_text(json.dumps({"revision": old.name}) + "\n")
    elif current.exists():
        raise ValueError("refusing to overwrite an existing application path")
    fresh_vps.atomic_link(current, target)
    return verify(root, revision, source)


def verify(root, revision, source=SOURCE):
    root, target, current, state = paths(root, revision)
    expected = validated_files(source)
    if not current.is_symlink() or current.resolve(strict=True) != target:
        raise ValueError("Auto Script staging pointer mismatch")
    manifest = json.loads((target / "_fresh_manifest.json").read_text())
    if manifest["revision"] != revision or manifest["app"] != "auto-script":
        raise ValueError("Auto Script staged manifest mismatch")
    if manifest["files"] != {n: hashlib.sha256(d).hexdigest() for n, d in expected.items()}:
        raise ValueError("Auto Script release source hashes mismatch")
    for name, data in expected.items():
        file = target / name
        if file.is_symlink() or not file.is_file() or hashlib.sha256(file.read_bytes()).digest() != hashlib.sha256(data).digest():
            raise ValueError("Auto Script staged source mismatch")
    for name in ("db", "queue", "uploads", "work"):
        if not (state / name).is_dir():
            raise ValueError("isolated app state missing")
    return {"app": "auto-script", "source": "PASS", "release": revision,
            "isolated_dirs": 4, "startup": "DEFERRED_SHARED_ROUTER",
            "production_modified": False, "cutover": "DISABLED"}


def rollback(root):
    root = fresh_vps.root_dir(root)
    state = root / "var/lib/fresh-website/auto-script"
    marker = state / "previous.json"
    if not marker.is_file():
        raise ValueError("no Auto Script previous release")
    previous = json.loads(marker.read_text())["revision"]
    _, target, current, _ = paths(root, previous)
    if not (target / "_fresh_manifest.json").is_file() or not current.is_symlink():
        raise ValueError("unsafe Auto Script rollback target")
    old = current.resolve(strict=True)
    if not old.is_relative_to(root / "srv/fresh-website/releases/auto-script"):
        raise ValueError("Auto Script current release escaped staging")
    fresh_vps.atomic_link(current, target)
    marker.write_text(json.dumps({"revision": old.name}) + "\n")
    return {"app": "auto-script", "restored_revision": previous,
            "production_modified": False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("stage", "verify", "rollback"))
    parser.add_argument("--root", required=True)
    parser.add_argument("--sha")
    args = parser.parse_args()
    if args.action == "rollback":
        result = rollback(args.root)
    else:
        if not args.sha:
            parser.error("--sha is required")
        result = stage(args.root, args.sha) if args.action == "stage" else verify(args.root, args.sha)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()

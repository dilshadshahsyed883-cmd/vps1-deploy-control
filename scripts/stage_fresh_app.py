#!/usr/bin/env python3
"""Non-root, no-service GitHub source artifact staging after the OVH snapshot.

This is NOT production deployment or cutover. The only writable staging root is
~/vps1-ci-pilot/fresh-staging. systemd, Nginx, DBs and live app paths are untouched.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile
import tempfile

ALLOWED = frozenset(["scene-builder","script-work","voice-studio","auto-caption-studio","local-upload-manager","short-stream","short-maker","video-merger","self-media-library","vps-storage-manager"])
MAX_FILES = 10000
MAX_UNPACKED = 300 * 1024 * 1024


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def approved_root():
    home = Path.home()
    base = home / "vps1-ci-pilot"
    root = base / "fresh-staging"
    if base.is_symlink() or root.is_symlink():
        raise ValueError("staging path must not be a symlink")
    if not root.resolve().is_relative_to(home.resolve()):
        raise ValueError("staging root escaped home")
    return root


def safe_app(app):
    if app not in ALLOWED:
        raise ValueError("offline or unknown app: " + str(app))
    return app


def safe_revision(revision):
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("invalid immutable commit SHA")
    return revision


def load_manifest(app, revision, archive, manifest):
    payload = json.loads(Path(manifest).read_text())
    if payload.get("app") != app or payload.get("commit_sha") != revision:
        raise ValueError("release identity mismatch")
    if payload.get("archive") != Path(archive).name:
        raise ValueError("archive filename mismatch")
    if sha256(Path(archive).read_bytes()) != payload.get("sha256"):
        raise ValueError("archive checksum mismatch")
    expected = {}
    for item in payload.get("files", []):
        rel = item.get("path")
        if not isinstance(rel, str):
            raise ValueError("invalid manifest path")
        pure = PurePosixPath(rel)
        if not pure.parts or pure.is_absolute() or ".." in pure.parts or "." in pure.parts or str(pure) != rel:
            raise ValueError("unsafe manifest path")
        if rel in expected or not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256", ""))):
            raise ValueError("duplicate/invalid manifest member")
        expected[rel] = item["sha256"]
    if not expected or len(expected) > MAX_FILES:
        raise ValueError("invalid manifest count")
    return payload, expected


def check_release(target, expected):
    if target.is_symlink() or not target.is_dir():
        raise ValueError("bad existing release")
    for rel, digest in expected.items():
        file = target / rel
        if file.is_symlink() or not file.is_file() or sha256(file.read_bytes()) != digest:
            raise ValueError("existing release content differs")
    if len([x for x in target.rglob("*") if x.is_file() and x.name != "_staging_manifest.json"]) != len(expected):
        raise ValueError("existing release has unexpected content")


def stage(app, revision, archive, manifest, *, root=None):
    safe_app(app)
    safe_revision(revision)
    root = approved_root() if root is None else Path(root)
    root.mkdir(parents=True, exist_ok=True)
    payload, expected = load_manifest(app, revision, archive, manifest)
    release_base = root / "releases" / app
    release_base.mkdir(parents=True, exist_ok=True)
    target = release_base / revision
    if not target.exists() and not target.is_symlink():
        with tempfile.TemporaryDirectory(prefix=".release-", dir=release_base) as temporary:
            partial = Path(temporary)
            seen = set()
            total = 0
            with tarfile.open(archive, "r:gz") as tar:
                for member in tar:
                    if not member.isfile() or member.issym() or member.islnk():
                        raise ValueError("not a regular release member")
                    p = PurePosixPath(member.name)
                    if len(p.parts) < 2 or p.parts[0] != app or ".." in p.parts or "." in p.parts:
                        raise ValueError("unsafe archive member")
                    rel = str(PurePosixPath(*p.parts[1:]))
                    if rel not in expected or rel in seen or member.size > 4_000_000:
                        raise ValueError("unexpected/oversized/duplicate archive member")
                    seen.add(rel)
                    total += member.size
                    if total > MAX_UNPACKED:
                        raise ValueError("release unpacked size exceeded")
                    data = tar.extractfile(member).read()
                    if len(data) != member.size or sha256(data) != expected[rel]:
                        raise ValueError("release member SHA256 mismatch")
                    dest = partial / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(data)
                    dest.chmod(0o644)
            if seen != set(expected):
                raise ValueError("missing archive member")
            (partial / "_staging_manifest.json").write_text(json.dumps({
                "app": app, "revision": revision, "source_sha256": payload["sha256"], "files": expected
            }, sort_keys=True) + "\n")
            os.rename(partial, target)
    check_release(target, expected)
    current_dir = root / "current"
    previous_dir = root / "previous"
    current_dir.mkdir(exist_ok=True)
    previous_dir.mkdir(exist_ok=True)
    current = current_dir / app
    previous = previous_dir / app
    if current.exists() and not current.is_symlink():
        raise ValueError("refusing to replace unexpected current path")
    if current.is_symlink():
        old = current.resolve(strict=True)
        if not old.is_relative_to(release_base) or old.parent != release_base:
            raise ValueError("current pointer escaped app release directory")
        if old != target:
            temporary = previous_dir / ("." + app + ".next")
            if temporary.exists() or temporary.is_symlink():
                raise ValueError("conflicting previous pointer")
            temporary.symlink_to(old)
            os.replace(temporary, previous)
    pending = current_dir / ("." + app + ".next")
    if pending.exists() or pending.is_symlink():
        raise ValueError("conflicting current pointer")
    pending.symlink_to(target)
    os.replace(pending, current)
    if current.resolve(strict=True) != target:
        raise ValueError("readback revision pointer mismatch")
    return {"status": "STAGED_ONLY", "app": app, "revision": revision,
            "verified_files": len(expected), "production_modified": False,
            "service_started": False, "cutover": "DISABLED"}


def rollback(app, *, root=None):
    safe_app(app)
    root = approved_root() if root is None else Path(root)
    current = root / "current" / app
    previous = root / "previous" / app
    release_base = root / "releases" / app
    if not current.is_symlink() or not previous.is_symlink():
        raise ValueError("no staged rollback pointer")
    target = previous.resolve(strict=True)
    if not target.is_relative_to(release_base) or target.parent != release_base:
        raise ValueError("unsafe rollback revision")
    old = current.resolve(strict=True)
    if not old.is_relative_to(release_base) or old.parent != release_base:
        raise ValueError("unsafe current revision")
    pending = root / "current" / ("." + app + ".next")
    if pending.exists() or pending.is_symlink():
        raise ValueError("rollback pointer conflict")
    pending.symlink_to(target)
    os.replace(pending, current)
    newprevious = root / "previous" / ("." + app + ".next")
    newprevious.symlink_to(old)
    os.replace(newprevious, previous)
    return {"status":"STAGING_ROLLBACK_PASS","app":app,
            "revision":target.name,"production_modified":False}


def audit(app, revision, archive, manifest, *, root=None):
    """Read-only proof of the GitHub artifact, active staging pointer and files."""
    safe_app(app)
    safe_revision(revision)
    root = approved_root() if root is None else Path(root)
    payload, expected = load_manifest(app, revision, archive, manifest)
    release_base = root / "releases" / app
    target = release_base / revision
    current = root / "current" / app
    for directory in (root, root / "releases", release_base, root / "current"):
        if directory.is_symlink():
            raise ValueError("staging directory replaced by symlink")
    if not current.is_symlink() or not target.is_dir() or target.is_symlink():
        raise ValueError("staged release or pointer missing")
    if current.resolve(strict=True) != target.resolve(strict=True):
        raise ValueError("active source revision differs from expected commit")
    if any(p.is_symlink() for p in target.rglob("*")):
        raise ValueError("release contains symlinks")
    metadata_path = target / "_staging_manifest.json"
    if not metadata_path.is_file() or metadata_path.is_symlink():
        raise ValueError("release provenance missing")
    metadata = json.loads(metadata_path.read_text())
    if metadata != {"app": app, "revision": revision,
                    "source_sha256": payload["sha256"], "files": expected}:
        raise ValueError("release provenance differs from GitHub artifact")
    check_release(target, expected)
    return {"status": "STAGING_AUDIT_PASS", "app": app,
            "revision": revision, "verified_files": len(expected),
            "production_modified": False, "service_started": False}



def inspect_active(app, *, revision=None, root=None):
    """Read-only local release integrity check, not GitHub-origin attestation."""
    safe_app(app)
    if revision is not None:
        safe_revision(revision)
    root = approved_root() if root is None else Path(root)
    releases = root / "releases" / app
    current = root / "current" / app
    for folder in (root, root / "releases", releases, root / "current"):
        if folder.is_symlink():
            raise ValueError("symlink in staging root")
    if not current.is_symlink():
        raise ValueError("active stage missing")
    target = current.resolve(strict=True)
    if not target.is_dir() or target.is_symlink() or target.parent != releases.resolve():
        raise ValueError("active stage escaped app release directory")
    safe_revision(target.name)
    if revision is not None and target.name != revision:
        raise ValueError("wrong active commit SHA")
    if any(p.is_symlink() for p in target.rglob("*")):
        raise ValueError("symlink found in immutable release")
    mp = target / "_staging_manifest.json"
    if not mp.is_file() or mp.is_symlink():
        raise ValueError("missing stored source manifest")
    record = json.loads(mp.read_text())
    files = record.get("files")
    if (record.get("app") != app or record.get("revision") != target.name
            or not re.fullmatch(r"[a-f0-9]{64}", str(record.get("source_sha256", "")))
            or not isinstance(files, dict) or not 0 < len(files) <= MAX_FILES):
        raise ValueError("invalid local release metadata")
    for name, digest in files.items():
        if not isinstance(name, str):
            raise ValueError("invalid release path")
        rel = PurePosixPath(name)
        if (not rel.parts or rel.is_absolute() or ".." in rel.parts
                or "." in rel.parts or str(rel) != name
                or not re.fullmatch(r"[a-f0-9]{64}", str(digest))):
            raise ValueError("invalid file checksum record")
    check_release(target, files)
    return {"status": "STAGING_LOCAL_DRIFT_CHECK_PASS", "app": app,
            "revision": target.name, "files_verified": len(files),
            "github_manifest_compared": False, "production_modified": False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("stage", "rollback", "audit", "inspect-active"))
    parser.add_argument("--app", required=True)
    parser.add_argument("--sha")
    parser.add_argument("--archive")
    parser.add_argument("--manifest")
    a = parser.parse_args()
    if os.geteuid() == 0:
        parser.error("must be run by an unprivileged account")
    if a.action in ("stage", "audit"):
        if not all((a.sha, a.archive, a.manifest)):
            parser.error(a.action + " requires SHA, archive and manifest")
        action = stage if a.action == "stage" else audit
        outcome = action(a.app, a.sha, a.archive, a.manifest)
    elif a.action == "inspect-active":
        outcome = inspect_active(a.app, revision=a.sha)
    else:
        outcome = rollback(a.app)
    print(json.dumps(outcome, sort_keys=True))


if __name__ == "__main__":
    main()

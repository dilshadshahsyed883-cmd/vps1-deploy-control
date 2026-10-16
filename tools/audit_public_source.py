#!/usr/bin/env python3
"""Fail-closed public repository file-inventory and sensitive data gate.

No credential values, content excerpts, home paths or usernames are printed.
This scan complements (does not replace) the full-history Gitleaks job.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

BLOCKED_EXTENSIONS = frozenset({
    ".db", ".sqlite", ".sqlite3", ".mdb", ".pem", ".key", ".p12", ".pfx",
    ".jks", ".kdbx", ".gpg", ".asc", ".zip", ".tar", ".gz", ".7z", ".rar",
    ".mp3", ".wav", ".mp4", ".mov", ".jpg", ".jpeg", ".png", ".webp",
    ".pdf", ".xls", ".xlsx", ".csv", ".jsonl",
})
BLOCKED_NAMES = frozenset({
    ".env", ".npmrc", ".pypirc", ".netrc", ".git-credentials",
    "id_rsa", "id_ed25519", "authorized_keys", "known_hosts",
    "credentials.json", "service_account.json", "service-account.json",
    "client_secret.json", "token.json", "cookies.txt",
})
BAD_PARTS = frozenset({"node_modules", ".venv", "venv", "__pycache__", ".ssh", ".aws", ".gnupg"})
EMAIL = re.compile(rb"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[A-Za-z]{2,}")
PRIVATE_KEY = re.compile(rb"(?m)^ *-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----")
ABS_HOME = re.compile(rb"/home/([^/\s\"']+)/")
ROOT_USER = re.compile(rb"/root/")


def classify_path(name):
    p = Path(name)
    if any(component in BAD_PARTS for component in p.parts):
        return "runtime_directory"
    if p.name.lower() in BLOCKED_NAMES or p.name.lower().startswith(".env."):
        return "credential_filename"
    if p.suffix.lower() in BLOCKED_EXTENSIONS:
        return "mutable_or_binary_artifact"
    return None


def audit(root, paths):
    counts = {}
    scanned = 0

    def flag(category):
        counts[category] = counts.get(category, 0) + 1

    for rel in paths:
        if not rel:
            continue
        scanned += 1
        reason = classify_path(rel)
        if reason:
            flag(reason)
        path = root / rel
        try:
            raw = path.read_bytes()
        except (OSError, ValueError):
            flag("unreadable_tracked_file")
            continue
        if b"\x00" in raw:
            flag("binary_or_nul_data")
        if PRIVATE_KEY.search(raw):
            flag("private_key_material")
        # Source code has example email/credentials in tests; these checks are
        # scoped to committed manifests and backup/snapshot artifacts.
        lower = rel.lower()
        if ("manifest" in lower or "/.acs_backups/" in lower or "/snapshots/" in lower):
            if EMAIL.search(raw):
                flag("personally_identifying_contact_in_manifest_or_backup")
            if ROOT_USER.search(raw):
                flag("root_home_path_in_manifest_or_backup")
            if any(m.group(1) != b"ubuntu" for m in ABS_HOME.finditer(raw)):
                flag("nonservice_user_home_path_in_manifest_or_backup")
            if PRIVATE_KEY.search(raw):
                flag("private_key_in_manifest_or_backup")
        if rel.startswith("rebuild/snapshots/") and rel.endswith(".json"):
            try:
                doc = json.loads(raw.decode("utf-8"))
                if not isinstance(doc, dict):
                    raise ValueError("expected object")
            except (UnicodeError, ValueError):
                flag("malformed_source_manifest")

    return scanned, counts


def tracked_paths(root):
    raw = subprocess.check_output(
        ["git", "-C", str(root), "ls-files", "-z", "--cached"], timeout=60
    )
    return [os.fsdecode(x) for x in raw.split(b"\x00") if x]


def main():
    root = Path(os.environ.get("GITHUB_WORKSPACE", Path.cwd())).resolve()
    try:
        scanned, counts = audit(root, tracked_paths(root))
    except (OSError, subprocess.SubprocessError):
        print("PUBLIC_SOURCE_AUDIT=BLOCKED; inventory not available")
        return 1
    print(f"PUBLIC_SOURCE_AUDIT={'BLOCKED' if counts else 'PASS'}; TRACKED={scanned}; CATEGORIES={len(counts)}")
    # Deliberately never publish filenames, matched source, metadata or raw data.
    for key, count in sorted(counts.items()):
        print(f"PUBLIC_SOURCE_CATEGORY={key}; COUNT={count}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("### Public repository data gate\n")
            f.write(f"- Files inventoried: {scanned}\n")
            f.write(f"- Restricted artifact/data categories: {len(counts)}\n")
            f.write("- Results do not publish file contents, personal data or paths.\n")
            f.write("- Separate Gitleaks history scan, third-party licensing and external OIDC/ACL verification remain distinct.\n")
    return int(bool(counts))


if __name__ == "__main__":
    sys.exit(main())

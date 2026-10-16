"""Classify Git changes into safe per-application release candidates.

This module does not deploy. Shared/control-plane changes fail closed so that a
generic merge cannot accidentally roll out an unrelated application.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SHARED_FILES = frozenset({
    "rebuild/groups.json",
    "rebuild/install-profiles.json",
    "rebuild/builder.py",
    "rebuild/fresh_vps.py",
    "rebuild/dependency_budget.py",
    "rebuild/dependency-budget.json",
    "rebuild/website_shell.py",
    "scripts/stage_fresh_app.py",
})
SHARED_PREFIXES = ("rebuild/shell/",)
SOURCE_SNAPSHOTS = {"kokoro": "voice-studio"}
SHA = re.compile(r"[a-f0-9]{40}\Z")


def classify(paths, registry):
    groups = {g["id"]: g for g in registry["groups"]}
    candidates = set()
    ignored_offline = set()
    requires_review = set()
    for path in sorted(set(paths)):
        if not path or path.startswith("/") or ".." in Path(path).parts:
            raise ValueError("unsafe changed path")
        parts = path.split("/")
        if parts[0] == "apps":
            if len(parts) >= 3 and parts[1] in SOURCE_SNAPSHOTS:
                requires_review.add("snapshot requires owned app source sync: " + path)
                continue
            if len(parts) < 3 or parts[1] not in groups:
                requires_review.add("unregistered app path: " + path)
                continue
            group = groups[parts[1]]
            if group.get("deployment_mode") == "offline":
                ignored_offline.add(parts[1])
            elif group.get("parallel_owner"):
                requires_review.add("parallel-owned app: " + parts[1])
            else:
                candidates.add(parts[1])
        elif path.startswith(("docs/", "tests/", "rebuild/tests/")) or path in ("README.md", "SECURITY.md"):
            # Test and documentation edits are not executable release changes.
            continue
        elif path in SHARED_FILES or any(path.startswith(p) for p in SHARED_PREFIXES):
            requires_review.add("shared deployment contract: " + path)
        else:
            # Fail closed for unknown scripts, workflows, infra, hidden files or
            # future rootfs tooling; no implicit safe-default for new paths.
            requires_review.add("unreviewed control-plane change: " + path)

    # One changed central contract blocks every app rollout in this commit.
    manual = bool(requires_review)
    apps = [] if manual else sorted(candidates)
    return {
        "apps": apps,
        "count": len(apps),
        "matrix": {"include": [{"app": app} for app in apps]},
        "manual_review": manual,
        "reasons": sorted(requires_review),
        "offline_ignored": sorted(ignored_offline),
    }


def changed_paths(base, head):
    if not SHA.fullmatch(base) or not SHA.fullmatch(head) or base == "0" * 40:
        raise ValueError("valid existing base and head commit SHAs required")
    output = subprocess.check_output(
        ["git", "diff", "--name-only", "-z", base, head], cwd=ROOT
    )
    return [p.decode("utf-8") for p in output.split(b"\0") if p]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    args = parser.parse_args()
    registry = json.loads((ROOT / "rebuild/groups.json").read_text())
    print(json.dumps(classify(changed_paths(args.base, args.head), registry), sort_keys=True))


if __name__ == "__main__":
    main()

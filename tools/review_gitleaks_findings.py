#!/usr/bin/env python3
"""Public-log-safe Gitleaks review: only exact verified historical test fixtures."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

FIXTURE = "rebuild/tests/test_builder.py"
LINE_HASH = {
    114: "e4086f94841f93d7d0f229b9463e480f65206b3fce08f73597c34f71154ab057",
    116: "ce236acbde0f04ad0978f3e233c6fddaecd59d85565f6e0cd0bfde07759cdc7a",
}
EXPECTED_NEXT = "with self.assertRaises(ValueError):builder.file_list(p)"


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def reviewed_legacy_fixture(finding, mode, root):
    if mode != "git" or not isinstance(finding, dict):
        return False  # Current source must have zero findings.
    path = str(finding.get("File", "")).removeprefix("/repo/").removeprefix("./")
    if path != FIXTURE or finding.get("RuleID") != "generic-api-key":
        return False
    try:
        number = int(finding.get("StartLine", -1))
    except (ValueError, TypeError):
        return False
    if number not in LINE_HASH:
        return False
    # Scanner --redact=100 also redacts the JSON secret field.
    # Instead validate the exact historical source-line SHA256 below.
    commit = str(finding.get("Commit", ""))
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        return False
    try:
        old_source = subprocess.check_output(
            ["git", "-C", str(root), "show", f"{commit}:{FIXTURE}"],
            stderr=subprocess.DEVNULL, timeout=15,
        ).decode("utf-8")
        lines = old_source.splitlines()
        return (
            digest(lines[number - 1]) == LINE_HASH[number]
            and lines[number].strip() == EXPECTED_NEXT
        )
    except (subprocess.SubprocessError, IndexError, UnicodeError, OSError):
        return False


def main():
    temp = Path(sys.argv[1])
    root = Path(os.environ["GITHUB_WORKSPACE"])
    statuses = {"git": int(sys.argv[2]), "dir": int(sys.argv[3])}
    failed = False
    lines = ["### Redacted Gitleaks verification"]
    for mode, status in statuses.items():
        report = temp / f"vps1-gitleaks-{mode}.json"
        try:
            data = json.loads(report.read_text(encoding="utf-8"))
            if data is None:
                data = []
            if not isinstance(data, list):
                raise ValueError("Invalid scanner JSON")
            ignored = sum(reviewed_legacy_fixture(item, mode, root) for item in data)
            unresolved = len(data) - ignored
            valid_exit = (status == 0 and len(data) == 0) or (status == 1 and len(data) > 0)
            passed = (unresolved == 0 and valid_exit)
            lines.append(
                f"- {mode}: total={len(data)}, exact-reviewed-legacy-fixtures={ignored}, "
                f"unresolved={unresolved}, exit={status}, status={'PASS' if passed else 'BLOCKED'}"
            )
            print(f"GITLEAKS_{mode.upper()}={'PASS' if passed else 'BLOCKED'}; TOTAL={len(data)}; REVIEWED={ignored}; UNRESOLVED={unresolved}; EXIT={status}")
            failed |= not passed
        except (ValueError, OSError, KeyError):
            failed = True
            lines.append(f"- {mode}: report unavailable or invalid; BLOCKED")
            print(f"GITLEAKS_{mode.upper()}=BLOCKED")
        finally:
            report.unlink(missing_ok=True)
    lines.append("- Only two explicitly hashed historical synthetic rejection-test lines may be reviewed.")
    lines.append("- No secret values, matching source lines, paths or Git identifiers are published.")
    lines.append("- Other findings fail. Source tree must be clear. Scan covers fetched reachable refs.")
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as output:
        output.write("\n".join(lines) + "\n")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

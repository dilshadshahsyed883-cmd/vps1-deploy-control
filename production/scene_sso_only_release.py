#!/usr/bin/env python3
"""Guarded Scene SSO-only source release; no LUT, GPU, worker, or Nginx changes.

This is a separate operator-invoked lane. It never weakens the existing
vps1-production-deploy allowlist or the full Scene release contract.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import stat
import subprocess
import sys
import time

BASE = Path("/srv/fresh-website")
CURRENT = BASE / "current/scene-builder"
RELEASES = BASE / "releases/scene-builder"
STATE = Path("/var/lib/fresh-website/scene-builder")
ENV = Path("/etc/fresh-website/scene-builder.env")
CONTROL = BASE / "control/releases"
JOURNALS = BASE / "transactions/scene-sso-only"
UNIT = "vps1-scene-candidate.service"
REMOTE = "https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control.git"
EXPECTED_PREVIOUS = "9379c166026794994a64ffaffe4efaf3812c02ed"
ALLOWED_CHANGES = frozenset({
    "part01/app.py", "part01/concat_sso_issuer.py",
})


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError("SCENE_SSO_ONLY_BLOCKED: " + message)


def changed_files(old: dict[str, str], new: dict[str, str]) -> set[str]:
    return {name for name in set(old) | set(new) if old.get(name) != new.get(name)}


def require_sso_only_diff(old: dict[str, str], new: dict[str, str]) -> None:
    differences = changed_files(old, new)
    require(differences == ALLOWED_CHANGES,
            "candidate has unrelated changes: " + ",".join(sorted(differences - ALLOWED_CHANGES)))
    require("part01/concat_sso_issuer.py" not in old and
            "part01/concat_sso_issuer.py" in new and
            "part01/app.py" in old and "part01/app.py" in new,
            "expected exact additive issuer + Scene wiring")


def candidate_files() -> dict[str, bytes]:
    # Identical source selection to the existing Scene candidate release adapter,
    # but build the digest map without touching the VPS.
    from rebuild.fresh_vps import sources
    from rebuild.stage_auto_script import validated_files
    rows = sources("scene-builder")
    result: dict[str, bytes] = {}
    for name, data in rows:
        require(name not in result, "duplicate Scene source path")
        result[name] = data
    for name, data in validated_files().items():
        path = "part01/" + name
        require(path not in result, "Auto Script source collision")
        result[path] = data
    return result


def digest_files(rows: dict[str, bytes]) -> dict[str, str]:
    return {name: hashlib.sha256(data).hexdigest() for name, data in rows.items()}


def environment_presence(raw: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        require("=" in line, "invalid Scene environment line")
        name, value = line.split("=", 1)
        name = name.strip()
        require(name not in values, "duplicate Scene environment key")
        values[name] = value.strip().strip("\"'")
    key = values.get("CONCAT_SCENE_SSO_SECRET", "")
    require(len(key.encode("utf-8")) >= 32, "Scene SSO signing key not configured")
    require(key not in {values.get("SESSION_SECRET"), values.get("WEB_ACCESS_KEY")},
            "Scene SSO key must be separate")
    require(values.get("WEB_DATA_DIR") == "/var/lib/fresh-website/scene-builder/web",
            "unexpected Scene database directory")
    return values


def host_checks(revision: str) -> tuple[Path, dict[str, str], Path]:
    require(os.geteuid() == 0 and socket.gethostname().split(".")[0] == "vps-01a23801",
            "approved VPS1 root host required")
    require(bool(re.fullmatch(r"[a-f0-9]{40}", revision)), "invalid source SHA")
    repo = Path(__file__).resolve().parents[1]
    require(repo == CONTROL / revision and (repo / ".git").is_dir(),
            "run from the root-owned, exact-SHA VPS1 control checkout")
    require(subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                    text=True).strip() == revision,
            "local checkout revision mismatch")
    actual = subprocess.check_output(["git", "ls-remote", REMOTE, "refs/heads/main"],
                                     text=True, timeout=45).split()[0]
    require(actual == revision, "source is not current protected main")
    require(CURRENT.is_symlink() and ENV.is_file() and STATE.is_dir(),
            "existing Scene ownership not ready")
    previous = CURRENT.resolve(strict=True)
    require(previous == RELEASES / EXPECTED_PREVIOUS,
            "only the audited pre-issuer Scene release is eligible")
    require(subprocess.run(["systemctl", "is-active", "--quiet", UNIT]).returncode == 0,
            "Scene service inactive")
    require(subprocess.run(["systemctl", "is-active", "--quiet",
                            "vps1-scene-production-worker.service"]).returncode == 0,
            "Scene worker service unexpectedly inactive")
    s = ENV.lstat()
    require(stat.S_ISREG(s.st_mode) and s.st_uid == 0 and stat.S_IMODE(s.st_mode) == 0o600,
            "Scene environment must remain root-owned mode 0600")
    values = environment_presence(ENV.read_text())
    db = Path(values["WEB_DATA_DIR"]) / "web.sqlite3"
    require(db.is_file() and not db.is_symlink(), "live Scene SQLite missing")
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("SELECT username,is_active,is_admin FROM users LIMIT 0")
    require(subprocess.run(["nginx", "-t"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL).returncode == 0,
            "existing Nginx syntax invalid")
    return previous, values, db


def verify_previous_manifest(previous: Path) -> dict[str, str]:
    manifest = json.loads((previous / "_scene_candidate_manifest.json").read_text())
    require(manifest.get("revision") == EXPECTED_PREVIOUS and
            manifest.get("app") == "scene-builder",
            "previous Scene release manifest mismatch")
    files = manifest["files"]
    for name, digest in files.items():
        path = previous / name
        require(not path.is_symlink() and path.is_file() and
                hashlib.sha256(path.read_bytes()).hexdigest() == digest,
                "previous immutable Scene release has drifted")
    return files


def healthy() -> None:
    from production.scene_candidate_release import _health, _request
    payload = _health(timeout=45)
    require(payload.get("ok") is True, "Scene health check failed")
    require(_request("/login")[0] == 200, "existing Scene login page unavailable")
    anonymous, _, _ = _request("/internal/workspace-auth?app=short-maker",
                               follow_redirects=False)
    require(anonymous == 401, "Scene workspace anonymous access not denied")


def verify_issuer(db: Path) -> None:
    from production.scene_candidate_release import _request
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("SELECT sid_hash,username_key,expires_at,revoked_at "
                     "FROM concat_sso_sessions LIMIT 0")
    from urllib.parse import urlencode
    route = "/concat-sso/authorize?" + urlencode({
        "client_id": "concat-web",
        "redirect_uri": "https://concat.scenebuilder.online/auth/scene/callback",
        "state": "s" * 48,
    })
    status, headers, _ = _request(route, follow_redirects=False)
    require(status in {303, 401, 403}, "unexpected anonymous SSO authorization")
    if status == 303:
        location = {k.lower(): v for k, v in headers.items()}.get("location", "")
        require(location.startswith("/login") and "ticket=" not in location,
                "anonymous SSO issued a ticket")
    print("SCENE_SSO_ONLY_SCHEMA=PASS ANONYMOUS_HANDOFF=DENIED", flush=True)


def execute(revision: str, mode: str) -> None:
    require(mode in {"preflight", "install"}, "invalid operation")
    previous, _, db = host_checks(revision)
    old = verify_previous_manifest(previous)
    candidate = candidate_files()
    new = digest_files(candidate)
    require_sso_only_diff(old, new)
    for name in ALLOWED_CHANGES:
        compile(candidate[name], name, "exec")
    healthy()
    if mode == "preflight":
        print("SCENE_SSO_ONLY_PREFLIGHT=PASS DIFF=EXACT_TWO_FILES DEPLOYMENT=NONE")
        return

    # The same host-wide mutex used by vps1-production-deploy. No worker,
    # browser, LUT, FFmpeg, Nginx or Drive Bridge release functions are called.
    from production.runtime import atomic_link
    from production.scene_candidate_release import _stage
    with open("/run/lock/vps1-production-control.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        require(CURRENT.resolve(strict=True) == previous, "Scene changed since preflight")
        target = _stage(revision)
        staged = json.loads((target / "_scene_candidate_manifest.json").read_text())["files"]
        require(staged == new, "staged source differs from reviewed SSO-only source")
        JOURNALS.mkdir(parents=True, exist_ok=True)
        journal = JOURNALS / (revision + "-" + str(time.time_ns()))
        journal.mkdir(mode=0o700)
        (journal / "previous.json").write_text(json.dumps({
            "previous": str(previous), "candidate": str(target), "unit": UNIT,
        }) + "\n")
        # Online consistent SQLite snapshot, including WAL contents. Preserve
        # additive table migration on rollback; never restore a stale live DB.
        backup = journal / "scene-web-before-sso.sqlite3"
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10) as source:
            with sqlite3.connect(backup) as destination:
                source.backup(destination)
        backup.chmod(0o600)
        switched = False
        try:
            atomic_link(CURRENT, target)
            switched = True
            subprocess.run(["systemctl", "restart", UNIT], check=True, timeout=80)
            healthy()
            verify_issuer(db)
            require(CURRENT.resolve(strict=True) == target, "unexpected Scene release pointer")
            print("SCENE_SSO_ONLY_INSTALL=PASS SCENE_ONLY=YES WORKER_RESTARTS=ZERO", flush=True)
        except BaseException:
            if switched:
                atomic_link(CURRENT, previous)
                subprocess.run(["systemctl", "restart", UNIT], check=True, timeout=80)
                healthy()
                print("SCENE_SSO_ONLY_ROLLBACK=PASS DATA_PRESERVED", flush=True)
            raise


def main() -> None:
    require(len(sys.argv) == 3, "usage: python -m production.scene_sso_only_release preflight|install SHA")
    execute(sys.argv[2], sys.argv[1])


if __name__ == "__main__":
    main()

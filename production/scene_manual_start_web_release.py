"""Guarded Scene Builder web-only manual Start policy repair.

This is a narrow compatibility release when the independent Scene worker is
busy. It never restarts, relinks, edits or reads writable worker queue data.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import tempfile
import time
import urllib.request

from production.runtime import BASE, REPO, atomic_json, atomic_link

CURRENT = BASE / "current/scene-builder"
RELEASES = BASE / "releases/scene-builder"
TRANSACTIONS = BASE / "transactions/scene-manual-start-web"
UNIT = "vps1-scene-candidate.service"
POLICY_SOURCE = REPO / "apps/scene-builder/part01/precutover_policy.py"
PROJECT = "a" * 32


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError("SCENE_MANUAL_START_WEB_BLOCKED: " + message)


def policy_allows(path: Path) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    policy = runpy.run_path(str(path))
    allowed = policy["mutation_allowed"]
    prefix = "/queue/" + PROJECT
    return (
        allowed("POST", prefix + "/start", True)
        and allowed("POST", prefix + "/delete", True)
        and not allowed("GET", prefix + "/start", True)
        and not allowed("POST", prefix + "/start", False)
        and not allowed("POST", "/queue/not-a-uuid/start", True)
    )


def healthy() -> bool:
    for _ in range(35):
        try:
            with urllib.request.urlopen("http://127.0.0.1:18080/health", timeout=3) as response:
                if response.status == 200 and json.load(response).get("ok") is True:
                    return True
        except (OSError, ValueError):
            pass
        time.sleep(0.5)
    return False


def restart_web() -> None:
    subprocess.run(["systemctl", "restart", UNIT], check=True,
                   capture_output=True, timeout=50)
    require(healthy(), "web did not become healthy after restart")


def install(revision: str) -> None:
    require(os.geteuid() == 0, "root-owned release required")
    require(re.fullmatch(r"[0-9a-f]{40}", revision) is not None, "invalid revision")
    require(shutil.disk_usage("/").free >= 5_000_000_000, "disk reserve")
    require(CURRENT.is_symlink(), "active Scene release symlink missing")
    old = CURRENT.resolve(strict=True)
    require(old.is_dir() and old.is_relative_to(RELEASES), "unowned Scene release")
    require(subprocess.run(["systemctl", "is-active", "--quiet", UNIT],
                           capture_output=True, timeout=10).returncode == 0 and healthy(),
            "existing Scene candidate not healthy")
    require(POLICY_SOURCE.is_file() and not POLICY_SOURCE.is_symlink()
            and policy_allows(POLICY_SOURCE), "trusted candidate policy not valid")
    web = old / "part01/app.py"
    require(web.is_file() and not web.is_symlink(), "existing Scene web source missing")
    code = web.read_text(encoding="utf-8")
    require('@app.post("/queue/{job_id}/start")' in code
            and 'require_job_access(request, job_id)' in code
            and 'worker_request("POST", f"/jobs/{job_id}/start")' in code
            and "from precutover_policy import mutation_allowed as candidate_mutation_allowed" in code,
            "existing web lacks owner-checked Start route")
    target = RELEASES / (revision + "-start-policy-only")
    journal = TRANSACTIONS / revision
    require(not target.exists() and not target.is_symlink(), "release target already exists")
    require(not journal.exists(), "release transaction already exists")
    changed = False
    copied = False
    try:
        shutil.copytree(old, target, symlinks=True)
        copied = True
        destination = target / "part01/precutover_policy.py"
        require(destination.is_file() and not destination.is_symlink(),
                "existing candidate policy is not a regular file")
        # The immutable overlay differs in precisely one text source file.
        dst = destination.with_name(destination.name + ".new")
        shutil.copyfile(POLICY_SOURCE, dst)
        os.chmod(dst, 0o644)
        os.replace(dst, destination)
        require(policy_allows(destination), "staged policy verification failed")
        journal.mkdir(mode=0o700, parents=True)
        atomic_json(journal / "state.json", {
            "revision": revision, "old": str(old), "new": str(target),
            "modified_files": ["part01/precutover_policy.py"], "status": "STAGED",
        })
        atomic_link(CURRENT, target)
        changed = True
        restart_web()
        require(CURRENT.resolve() == target and policy_allows(CURRENT / "part01/precutover_policy.py"),
                "live web policy did not match staged source")
        atomic_json(journal / "state.json", {
            "revision": revision, "old": str(old), "new": str(target),
            "modified_files": ["part01/precutover_policy.py"], "status": "ACTIVE",
            "worker": "UNTOUCHED", "user_jobs": "UNTOUCHED",
        })
        print("SCENE_MANUAL_START_WEB=PASS POLICY=LIVE WORKER=UNTOUCHED JOBS=UNTOUCHED", flush=True)
    except BaseException:
        if changed:
            atomic_link(CURRENT, old)
            restart_web()
            print("SCENE_MANUAL_START_WEB_ROLLBACK=PASS WORKER=UNTOUCHED", flush=True)
        elif copied:
            shutil.rmtree(target, ignore_errors=True)
        raise

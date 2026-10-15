#!/usr/bin/env python3
"""GitHub-only shared-Python/uv dependency builder and 30 GB admission gate.

All installation writes are restricted to an isolated /tmp fresh-rootfs.
No production service, Nginx, system Python site-packages, or VPS_1 is changed.
This is an admission/monitoring check, not a filesystem quota for future writes.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

from rebuild import builder, fresh_vps

REPO = Path(__file__).resolve().parents[1]
CONFIG_FILE = REPO / "rebuild/dependency-budget.json"


def config():
    data = json.loads(CONFIG_FILE.read_text())
    groups = builder.config()
    registered = {g["id"] for g in groups["groups"] if g.get("deployment_mode") != "offline"}
    entries = data["install_groups"]
    apps = [entry["app"] for entry in entries]
    if len(apps) != 10 or len(set(apps)) != 10 or set(apps) != registered:
        raise ValueError("dependency manifest must exactly cover ten active groups")
    if "auto-script" in apps or data["application_budget_bytes"] != 30_000_000_000:
        raise ValueError("offline Auto Script or wrong fixed storage budget")
    if data["minimum_free_bytes"] < 5_000_000_000 or data["python_minor"] != "3.12":
        raise ValueError("safety reserve or shared Python version changed")
    for entry in entries:
        if entry["category"] not in {"standard", "heavy-ai", "browser", "stdlib"}:
            raise ValueError("unrecognized application dependency category")
        for rel in entry["requires"]:
            path = Path(rel)
            if path.is_absolute() or ".." in path.parts or not path.parts:
                raise ValueError("unsafe requirements file path")
            actual = REPO / path
            if actual.is_symlink() or not actual.is_file() or not actual.resolve().is_relative_to(REPO):
                raise ValueError("dependency file missing or symlinked: " + rel)
    return data


def dirs(root):
    root = fresh_vps.root_dir(str(root))
    cfg = config()
    cache = root / cfg["cache_path"]
    models = root / cfg["models_path"]
    environments = root / "srv/fresh-website/venvs"
    return root, cache, models, environments


def disk_bytes(root):
    """Count allocated blocks for each physical inode once, including hardlinks.

    Directory entries and unique files in the sandbox are counted. Symlinks
    are NOT followed. st_blocks tracks allocated blocks, not logical file size.
    """
    total = 0
    seen = set()
    for directory, subdirs, files in os.walk(root, followlinks=False):
        for name in subdirs + files:
            path = Path(directory) / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                continue
            key = (info.st_dev, info.st_ino)
            if key not in seen:
                seen.add(key)
                total += info.st_blocks * 512
        # Do not inspect symlink directory trees; os.walk does not follow them.
    return total


def snapshot(root):
    root, cache, models, environments = dirs(root)
    cfg = config()
    allocated = disk_bytes(root) if root.is_dir() else 0
    free = shutil.disk_usage(root if root.exists() else Path("/tmp")).free
    report = {
        "budget_bytes": cfg["application_budget_bytes"],
        "allocated_unique_inode_bytes": allocated,
        "remaining_budget_bytes": cfg["application_budget_bytes"] - allocated,
        "host_free_bytes": free,
        "minimum_free_bytes": cfg["minimum_free_bytes"],
        "within_budget": allocated <= cfg["application_budget_bytes"],
        "host_reserve_ok": free >= cfg["minimum_free_bytes"],
        "active_apps": 10, "auto_script": "OFFLINE",
        "python_binary_shared": True, "uv_cache_shared": str(cache),
        "model_store_shared": str(models), "production_modified": False,
        "quota_enforced_on_future_uploads": False,
    }
    return report


def require_capacity(root):
    s = snapshot(root)
    if not s["within_budget"] or not s["host_reserve_ok"]:
        raise ValueError("30 GB admission budget or host 5 GB reserve exceeded")
    return s


def python_binary(raw):
    exe = Path(raw).resolve(strict=True)
    r = subprocess.run([str(exe), "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                       capture_output=True, text=True, check=True)
    if r.stdout.strip() != "3.12":
        raise ValueError("exact shared Python 3.12 interpreter required")
    return str(exe)


def install(root_raw, python, uv_exe="uv", apps=None):
    root, cache, models, environments = dirs(root_raw)
    # In CI /tmp only. Deployment to /srv requires separate authorized installer.
    if root.exists() and root.is_symlink():
        raise ValueError("symlinked staging root")
    root.mkdir(parents=True, exist_ok=True)
    require_capacity(root)
    interpreter = python_binary(python)
    cfg = config()
    cache.mkdir(parents=True, exist_ok=True)
    models.mkdir(parents=True, exist_ok=True)
    environments.mkdir(parents=True, exist_ok=True)
    # Explicit hardlink mode must not silently become a cross-device copy.
    if cache.stat().st_dev != environments.stat().st_dev:
        raise ValueError("uv cache and venvs must be on the same filesystem")
    env = os.environ.copy()
    env.update({"UV_CACHE_DIR": str(cache),
                "UV_LINK_MODE": "hardlink",
                "UV_NO_MANAGED_PYTHON": "1",
                "PYTHONDONTWRITEBYTECODE": "1"})
    selected = set(apps or [entry["app"] for entry in cfg["install_groups"]])
    known = {entry["app"] for entry in cfg["install_groups"]}
    if not selected or not selected.issubset(known):
        raise ValueError("offline or unknown app requested")
    results = []
    for entry in cfg["install_groups"]:
        app = entry["app"]
        if app not in selected:
            continue
        require_capacity(root)
        venv = environments / app
        if venv.is_symlink():
            raise ValueError("symlinked venv prohibited")
        subprocess.run([uv_exe, "venv", "--python", interpreter, str(venv)],
                       check=True, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       text=True)
        if entry["requires"]:
            args = [uv_exe, "pip", "sync", "--python", str(venv / "bin/python")]
            for rel in entry["requires"]:
                args.append(str(REPO / rel))
            subprocess.run(args, check=True, env=env, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, text=True)
        report = require_capacity(root)
        results.append({"app": app, "category": entry["category"],
                        "venv": str(venv), "requirements": entry["requires"],
                        "budget_remaining_bytes": report["remaining_budget_bytes"]})
    return {"status": "PASS_ISOLATED_DEPENDENCIES", "python": interpreter,
            "uv_link_mode": "hardlink", "applications": results,
            "disk": require_capacity(root), "production_modified": False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["plan", "check", "install"])
    parser.add_argument("--root", default="/tmp/fresh-dependency-plan")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--app", action="append")
    args = parser.parse_args()
    if args.action == "plan":
        outcome = {"config": config(), "production_modified": False,
                   "installer": "isolated /tmp only"}
    elif args.action == "check":
        outcome = require_capacity(args.root)
    else:
        outcome = install(args.root, args.python, apps=args.app)
    print(json.dumps(outcome, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print("BLOCKED_DEPENDENCY_BUDGET:", str(exc), file=sys.stderr)
        sys.exit(1)

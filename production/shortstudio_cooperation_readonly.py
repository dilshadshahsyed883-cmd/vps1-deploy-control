"""Read-only Short Studio cooperative-lock VPS preflight; no mutations.

Never echoes EnvironmentFile contents, credentials, usernames, media names,
private paths, unit text, or service parameters. This is *host* evidence only:
mount namespaces, actual live workers, active claims and safe migration require
separate release gates. It cannot activate the guarded retirement executor.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

SERVICES = {
    "maker": "vps1-production-short-maker.service",
    "web": "vps1-production-lum-shortstudio-web.service",
    "worker": "vps1-production-lum-shortstudio-worker.service",
    "scheduler": "vps1-production-lum-shortstudio-scheduler.service",
}
REQUIRED_KEYS = {
    "maker": "SHORT_MAKER_COOPERATIVE_LOCK_ENABLED",
    "web": "LUM_SHORT_STUDIO_COOPERATIVE_LOCK_ENABLED",
    "worker": "LUM_SHORT_STUDIO_COOPERATIVE_LOCK_ENABLED",
    "scheduler": "LUM_SHORT_STUDIO_COOPERATIVE_LOCK_ENABLED",
}
ROOT_KEY = "SHORT_STUDIO_VPS_FOLDER_ROOT"


def _read_explicit_environment(unit):
    """Exactly one absolute EnvironmentFile in a regular systemd unit."""
    if unit.is_symlink() or not unit.is_file():
        raise ValueError("Unsafe service unit")
    text = unit.read_text(encoding="utf-8")
    # Overrides from systemd Environment= or drop-in service files may
    # disagree with EnvironmentFile. Without a complete systemd merge proof,
    # fail closed instead of reporting an apparently consistent runtime.
    if re.search(r"^Environment=", text, re.MULTILINE):
        raise ValueError("Direct service environment overrides are not verified")
    dropin = Path(str(unit) + ".d")
    if dropin.exists() or dropin.is_symlink():
        raise ValueError("Unverified systemd drop-in environment override")
    matches = re.findall(r"^EnvironmentFile=(.+)$", text, re.MULTILINE)
    if len(matches) != 1:
        raise ValueError("Service unit must have one EnvironmentFile")
    raw = matches[0].strip().strip('"').strip("'")
    if raw.startswith("-"):
        raise ValueError("Optional EnvironmentFile cannot prove cooperation")
    path = Path(raw)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError("Unsafe or unavailable EnvironmentFile")
    parsed = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key,value=line.split("=",1)
        key=key.strip()
        if key in (ROOT_KEY,"SHORT_MAKER_COOPERATIVE_LOCK_ENABLED",
                   "LUM_SHORT_STUDIO_COOPERATIVE_LOCK_ENABLED"):
            if key in parsed:
                raise ValueError("Ambiguous duplicate studio coordination setting")
            parsed[key]=value.strip().strip('"').strip("'")
    return parsed


def inspect(unit_dir: Path = Path("/etc/systemd/system")) -> dict:
    """No unit restart, file creation, SQLite writes, media reads or network."""
    unit_dir=Path(unit_dir)
    results={}
    roots=[]
    for kind,name in SERVICES.items():
        entry={"configuration_present":False, "folder_root_safe":False,
               "lock_enabled":False}
        results[kind]=entry
        try:
            values=_read_explicit_environment(unit_dir/name)
            root_value=values.get(ROOT_KEY,"")
            folder=Path(root_value)
            if (not root_value or not folder.is_absolute()
                    or folder.is_symlink() or not folder.is_dir()):
                continue
            identity=os.stat(folder)
            entry["configuration_present"]=True
            entry["folder_root_safe"]=True
            entry["lock_enabled"]=values.get(REQUIRED_KEYS[kind],"0")=="1"
            roots.append((kind,str(folder.resolve(strict=True)),
                          identity.st_dev,identity.st_ino))
        except (OSError, ValueError, TypeError):
            continue
    # The same canonical host path and inode are necessary but *not*
    # sufficient to prove all running service mount namespaces are identical.
    host_root_match=(len(roots)==len(SERVICES) and len({
        (path,device,inode) for _,path,device,inode in roots})==1)
    lock_flags_consistent=(len(roots)==len(SERVICES) and len({
        entry["lock_enabled"] for entry in results.values()})==1)
    all_enabled=all(x["lock_enabled"] for x in results.values())
    return {
        "mode":"READ_ONLY",
        "scope":"short-studio-cooperation",
        "units":results,
        "same_host_root_proven":host_root_match,
        "lock_flags_consistent":lock_flags_consistent,
        "all_guards_enabled":all_enabled,
        "live_mount_namespace_verified":False,
        "active_upload_drain_verified":False,
        "two_user_isolation_verified":False,
        "retirement_enabled":False,
        "production_activation_ready":False,
        "status":"HOST_CONFIGURATION_READY_FOR_LIVE_GATES"
            if host_root_match and lock_flags_consistent and all_enabled else "BLOCKED",
    }


def main() -> None:
    print("SHORT_STUDIO_COOP_READONLY=" + json.dumps(
        inspect(), sort_keys=True),flush=True)


if __name__=="__main__":
    main()

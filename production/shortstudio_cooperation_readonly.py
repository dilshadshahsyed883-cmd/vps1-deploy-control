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
import subprocess
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
DRAIN_PATH_KEYS = ("SHORT_MAKER_DATA", "SHORT_UPLOADER_APP_DB", "SHORT_UPLOADER_ENGINE_DB")


def _read_explicit_environment(unit):
    """Exactly one absolute EnvironmentFile in a regular systemd unit."""
    if unit.is_symlink() or not unit.is_file():
        raise ValueError("Unsafe service unit")
    text = unit.read_text(encoding="utf-8")
    # Overrides from systemd Environment= or drop-in service files may
    # disagree with EnvironmentFile. Without a complete systemd merge proof,
    # fail closed instead of reporting an apparently consistent runtime.
    # Generated managed units legitimately set Environment=PATH=...;
    # reject only a direct override of the security-sensitive coordination
    # settings (including multiple quoted assignments on one line).
    for definition in re.findall(r"^Environment=(.*)$", text, re.MULTILINE):
        if any(re.search(r"(?<![A-Za-z0-9_])" + key + r"\s*=",
                         definition) for key in (
                             ROOT_KEY, "SHORT_MAKER_COOPERATIVE_LOCK_ENABLED",
                             "LUM_SHORT_STUDIO_COOPERATIVE_LOCK_ENABLED", *DRAIN_PATH_KEYS)):
            raise ValueError("Direct studio cooperation override is not verified")
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
                   "LUM_SHORT_STUDIO_COOPERATIVE_LOCK_ENABLED", *DRAIN_PATH_KEYS):
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



def _systemd_main_pid(unit_name):
    """Read only MainPID, not service environment or any private paths."""
    result = subprocess.run(
        ["systemctl", "show", unit_name, "--property=MainPID", "--value"],
        capture_output=True, text=True, timeout=4, check=False)
    candidate = result.stdout.strip()
    if result.returncode != 0 or not re.fullmatch(r"[0-9]{1,9}", candidate):
        raise ValueError("Service MainPID could not be verified")
    pid = int(candidate)
    if pid < 2:
        raise ValueError("Service is not running")
    return pid


def _process_env(proc_root, pid):
    """Consume process environment privately; never return raw values."""
    content = (Path(proc_root) / str(pid) / "environ").read_bytes()
    result = {}
    for chunk in content.split(b"\x00"):
        if not chunk or b"=" not in chunk:
            continue
        key, value = chunk.split(b"=", 1)
        if key in (b"SHORT_STUDIO_VPS_FOLDER_ROOT",
                   b"SHORT_MAKER_COOPERATIVE_LOCK_ENABLED",
                   b"LUM_SHORT_STUDIO_COOPERATIVE_LOCK_ENABLED",
                   b"LUM_ENGINE_INSTANCE"):
            if key in result:
                raise ValueError("Duplicate process environment setting")
            result[key] = value
    return result


def inspect_live_processes(unit_dir=Path("/etc/systemd/system"),
                           proc_root=Path("/proc"), pid_lookup=None):
    """Read-only evidence of actual four-process mount identity and flags.

    Does NOT authorize deleting folders or prove uploads are idle. On a host
    with unavailable /proc permissions, returns a fail-closed boolean report.
    Never prints PIDs, paths, unit text, credentials, or environment values.
    """
    unit_dir = Path(unit_dir)
    proc_root = Path(proc_root)
    pid_lookup = pid_lookup or _systemd_main_pid
    out = {key: {"running_process_verified": False,
                 "process_lock_flag_verified": False,
                 "process_root_verified": False}
           for key in SERVICES}
    mounted = []
    for kind, unit in SERVICES.items():
        item = out[kind]
        try:
            explicit = _read_explicit_environment(unit_dir / unit)
            root_value = explicit.get(ROOT_KEY, "")
            folder = Path(root_value)
            if not root_value or not folder.is_absolute() or folder.is_symlink() or not folder.is_dir():
                continue
            pid = pid_lookup(unit)
            if not isinstance(pid, int) or pid < 2:
                continue
            running = _process_env(proc_root, pid)
            marker = REQUIRED_KEYS[kind].encode("ascii")
            flag = running.get(marker) == b"1" and explicit.get(REQUIRED_KEYS[kind]) == "1"
            root_matches = running.get(ROOT_KEY.encode("ascii")) == os.fsencode(root_value)
            if kind != "maker":
                flag = flag and running.get(b"LUM_ENGINE_INSTANCE") == b"shortstudio"
            item["running_process_verified"] = True
            item["process_lock_flag_verified"] = bool(flag)
            if not (flag and root_matches):
                continue
            # /proc/<pid>/root/<path> resolves inside that service's own
            # mount namespace. Same st_dev+st_ino proves identical directory,
            # not just equal text configuration or symbolic lock filenames.
            candidate = proc_root / str(pid) / "root" / root_value.lstrip("/")
            actual = os.stat(candidate)
            host = os.stat(folder)
            correct = actual.st_dev == host.st_dev and actual.st_ino == host.st_ino
            item["process_root_verified"] = bool(correct)
            if correct:
                mounted.append((actual.st_dev, actual.st_ino))
            # Fail if systemd MainPID changed during the read (except injected
            # deterministic test resolver).
            if pid_lookup is _systemd_main_pid and _systemd_main_pid(unit) != pid:
                item["running_process_verified"] = False
                item["process_root_verified"] = False
                mounted.pop() if correct else None
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            continue
    all_verified = len(mounted) == len(SERVICES) and len(set(mounted)) == 1
    all_flags = all(x["process_lock_flag_verified"] for x in out.values())
    return {
        "mode": "READ_ONLY", "scope": "short-studio-running-services",
        "units": out,
        "four_processes_same_mount_verified": bool(all_verified),
        "four_processes_lock_flags_verified": bool(all_flags),
        "active_upload_drain_verified": False,
        "production_activation_ready": False,
        "status": "LIVE_MOUNT_AND_FLAGS_PROVEN"
          if all_verified and all_flags else "BLOCKED",
    }



def inspect_active_drain(unit_dir=Path("/etc/systemd/system")):
    """Sanitized, read-only *snapshot* of all known Maker/Short Uploader work.

    Requires explicitly configured paths; refuses incomplete or unsafe state.
    This is NOT a durable drain/worker quiescence certificate: a concurrent
    scheduler could start work after the read. The guarded installer must
    independently hold the release/drain barrier before enabling deletion.
    """
    import sqlite3

    result = {
        "mode": "READ_ONLY",
        "scope": "short-studio-upload-drain-snapshot",
        "maker_jobs_idle": False,
        "ready_imports_idle": False,
        "uploader_jobs_idle": False,
        "uploader_source_claims_idle": False,
        "active_upload_drain_snapshot_verified": False,
        "active_upload_drain_verified": False,
        "production_activation_ready": False,
        "status": "BLOCKED",
    }
    try:
        values = _read_explicit_environment(
            Path(unit_dir) / SERVICES["maker"])
        paths = []
        for key in DRAIN_PATH_KEYS:
            value = values.get(key, "")
            if not value:
                return result
            candidate = Path(value)
            if not candidate.is_absolute() or candidate.is_symlink():
                return result
            paths.append(candidate)
        maker, app_db, engine_db = paths
        if not maker.is_dir() or maker.is_symlink():
            return result
        jobs = maker / "jobs"
        if not jobs.is_dir() or jobs.is_symlink():
            return result
        terminal_maker = {"completed", "cancelled", "failed"}
        for directory in jobs.iterdir():
            if directory.is_symlink() or not directory.is_dir():
                return result
            state = directory / "state.json"
            if state.is_symlink() or not state.is_file():
                return result
            payload = json.loads(state.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or str(
                    payload.get("status", "")).lower() not in terminal_maker:
                return result
        result["maker_jobs_idle"] = True

        imports = maker / "ready_video_uploads"
        if imports.is_symlink() or (imports.exists() and not imports.is_dir()):
            return result
        if imports.is_dir():
            for directory in imports.iterdir():
                if directory.is_symlink() or not directory.is_dir():
                    return result
                manifest = directory / "manifest.json"
                if manifest.is_symlink() or not manifest.is_file():
                    return result
                payload = json.loads(manifest.read_text(encoding="utf-8"))
                if not isinstance(payload, dict) or str(
                        payload.get("status", "")).lower() not in {
                            "completed", "cancelled", "failed"}:
                    return result
        result["ready_imports_idle"] = True

        def query_idle(filename, table, column, safe_states):
            if filename.is_symlink() or not filename.is_file():
                raise ValueError("Uploader evidence database unavailable")
            with sqlite3.connect(filename.as_uri() + "?mode=ro",
                                 uri=True, timeout=3) as db:
                db.execute("PRAGMA query_only=ON")
                columns = {row[1] for row in db.execute(
                    'PRAGMA table_info("' + table + '")')}
                if column not in columns:
                    raise ValueError("Uploader evidence schema unknown")
                row = db.execute(
                    f'SELECT COUNT(*) FROM "{table}" WHERE '
                    f'UPPER(COALESCE("{column}", "")) NOT IN ('
                    + ",".join("?" for _ in safe_states) + ")",
                    tuple(safe_states)).fetchone()
                return row is not None and row[0] == 0

        result["uploader_jobs_idle"] = query_idle(
            app_db, "videos", "status",
            ("COMPLETE", "COMPLETED", "UPLOADED", "CANCELLED",
             "SKIPPED", "FAILED"))
        if not result["uploader_jobs_idle"]:
            return result
        result["uploader_source_claims_idle"] = query_idle(
            engine_db, "job_sources", "state", ("COMPLETE", "CANCELLED"))
        if not result["uploader_source_claims_idle"]:
            return result
        result["active_upload_drain_snapshot_verified"] = True
        result["status"] = "SNAPSHOT_NO_ACTIVE_UPLOADS"
    except (OSError, ValueError, TypeError, json.JSONDecodeError,
            sqlite3.Error, UnicodeError):
        # No credentials, filenames, owners, DB paths or error text in report.
        pass
    return result


def main() -> None:
    print("SHORT_STUDIO_COOP_READONLY=" + json.dumps(
        inspect(), sort_keys=True),flush=True)


if __name__=="__main__":
    main()

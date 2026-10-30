"""Sanitized read-only evidence for VPS_1 Short Studio's blocked first install.

Invoked only by the guarded protected-main local-upload-manager installer lane
with the explicit diagnostic commit marker. No sudoers or SSH changes, no state
mutation, no worker/queue/service operation, no token or media inspection.
"""
from __future__ import annotations

import json
import socket
import sqlite3
import subprocess
from pathlib import Path

def unit_metadata(unit_dir: Path, suffix: str) -> dict:
    unit = unit_dir / ("vps1-production-lum-shortstudio-" + suffix + ".service")
    exists = unit.exists() or unit.is_symlink()
    state = "absent"
    if exists:
        # The status only reports active/inactive, never unit content or env.
        p = subprocess.run(["systemctl", "is-active", "--quiet", unit.name],
                           capture_output=True, timeout=4)
        state = "active" if p.returncode == 0 else "inactive"
    return {"exists": exists, "state": state, "symlink": unit.is_symlink()}

def port_8019() -> str:
    with socket.socket() as s:
        s.settimeout(1.5)
        return "listening" if s.connect_ex(("127.0.0.1", 8019)) == 0 else "unreachable"

def sqlite_summary(path: Path, tables: tuple[str, ...]) -> dict:
    if path.is_symlink(): return {"present": True, "safe": False, "reason": "symlink"}
    if not path.exists(): return {"present": False}
    if not path.is_file(): return {"present": True, "safe": False, "reason": "not_file"}
    result = {"present": True, "tables": {}}
    # Strict metadata/aggregates only; no SQL rows, paths or credentials output.
    try:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3) as con:
            con.execute("PRAGMA query_only=ON")
            existing = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for name in tables:
                if name not in existing: continue
                if name == "engine_state":
                    rows = con.execute("SELECT running, scheduler_armed FROM engine_state").fetchall()
                    result["tables"][name] = {
                        "count":len(rows), "running":bool(any(int(a) for a,_ in rows)),
                        "scheduler_armed":bool(any(int(b) for _,b in rows))}
                else:
                    result["tables"][name] = {"count": int(con.execute(
                        "SELECT COUNT(*) FROM " + name).fetchone()[0])}
    except (sqlite3.Error, OSError, ValueError, TypeError):
        result["safe"] = False
        result["reason"] = "unreadable"
    return result

def folder_summary(path: Path) -> dict:
    if path.is_symlink(): return {"present":True,"safe":False,"reason":"symlink"}
    if not path.exists(): return {"present":False}
    if not path.is_dir(): return {"present":True,"safe":False,"reason":"not_dir"}
    try:
        # Boolean only. No filenames, no recursive filesystem traversal.
        return {"present":True,"nonempty":any(path.iterdir())}
    except OSError:
        return {"present":True,"safe":False,"reason":"unreadable"}

def secure_structure(path: Path) -> dict:
    """Only known scaffold names and aggregate unknown counts; never file names."""
    if path.is_symlink(): return {"safe":False,"reason":"symlink"}
    if not path.exists(): return {"present":False}
    if not path.is_dir(): return {"safe":False,"reason":"not_dir"}
    names=("oauth_clients","oauth_sessions")
    try:
        children=list(path.iterdir())
        other=[p for p in children if p.name not in names]
        expected={}
        for name in names:
            p=path/name
            if not p.exists() and not p.is_symlink():
                expected[name]={"present":False}
            elif p.is_symlink():
                expected[name]={"present":True,"type":"symlink"}
            elif p.is_dir():
                expected[name]={"present":True,"type":"dir","nonempty":any(p.iterdir())}
            else:
                expected[name]={"present":True,"type":"file"}
        return {"present":True,"entries":len(children),"expected":expected,
                "unexpected_count":len(other),
                "unexpected_dirs":sum(p.is_dir() and not p.is_symlink() for p in other),
                "unexpected_files":sum(p.is_file() and not p.is_symlink() for p in other),
                "unexpected_symlinks":sum(p.is_symlink() for p in other)}
    except OSError:
        return {"present":True,"safe":False,"reason":"unreadable"}

def collect(state: Path, *, unit_dir: Path = Path("/etc/systemd/system")) -> dict:
    from production.runtime import shortstudio_rolled_back_empty
    root = state / "lum-shortstudio"
    marker = root / ".vps1-independent-engine"
    owned = False
    if marker.is_file() and not marker.is_symlink():
        try: owned = marker.read_text().strip() == "shortstudio"
        except OSError: pass
    out = {
        "scope": "shortstudio",
        "mode": "READ_ONLY",
        "root_present": root.is_dir() and not root.is_symlink(),
        "owner_marker_valid": owned,
        "units": {name:unit_metadata(unit_dir,name) for name in ("web","worker","scheduler")},
        "port_8019": port_8019(),
        "secure_structure": secure_structure(root/"secure"),
        "core_db": sqlite_summary(root/"core/data/uploader.db",
            ("videos","channels","auto_engine_profiles","accounts")),
        "v6_db": sqlite_summary(root/"auto_engine_v6/auto_engine_v6.sqlite3",
            ("profile_sources","drive_videos","thumbnails","job_sources","upload_records",
             "script_history","missed_alarms","engine_state")),
        "folders": {name:folder_summary(root/name) for name in
            ("secure","auto_engine_v6/staging","auto_engine_v6/thumbnails",
             "auto_engine_v6/thumbnail_trash","auto_engine_v6/reports")},
    }
    try: out["empty_first_install_proven"] = bool(shortstudio_rolled_back_empty(state,unit_dir=unit_dir))
    except (OSError,ValueError,TypeError,sqlite3.Error): out["empty_first_install_proven"] = False
    return out

def report(state: Path) -> None:
    print("SHORT_STUDIO_READONLY_DIAGNOSTIC=" + json.dumps(collect(state),sort_keys=True),flush=True)

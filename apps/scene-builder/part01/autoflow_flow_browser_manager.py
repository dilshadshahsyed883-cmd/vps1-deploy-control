from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

from autoflow_control_backend import DB_PATH, _now

POLL_SECONDS = 3.0


def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")
    return con


def update(pid: int, **fields):
    if not fields:
        return
    fields["updated_at"] = _now()
    sets = ",".join(f"{k}=?" for k in fields)
    with db() as con:
        con.execute(f"UPDATE autoflow_flow_profiles SET {sets} WHERE id=?", (*fields.values(), pid))
        con.commit()


def pg_kill(proc):
    if not proc:
        return
    try:
        if proc.poll() is None:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            deadline = time.time() + 4
            while time.time() < deadline and proc.poll() is None:
                time.sleep(0.1)
            if proc.poll() is None:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        pass


@dataclass
class Runtime:
    profile_id: int
    restart_seq: int
    procs: list = field(default_factory=list)

    def stop(self):
        for p in reversed(self.procs):
            pg_kill(p)
        self.procs.clear()
        update(self.profile_id, browser_pid=0, browser_state="STOPPED")


def spawn(cmd, env=None):
    return subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def x_socket(display_num: int):
    return Path(f"/tmp/.X11-unix/X{display_num}")


def launch(p: dict) -> Runtime:
    pid = int(p["id"])
    display = int(p["display_num"])
    debug = int(p["debug_port"])
    vnc = int(p["vnc_port"])
    viewer = int(p["viewer_port"])
    profile_dir = str(p["profile_dir"])
    tool_url = str(p["tool_url"])
    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    os.chmod(profile_dir, 0o700)
    rt = Runtime(pid, int(p.get("restart_seq") or 0))
    update(pid, browser_state="STARTING", last_error="")
    xv = spawn(["/usr/bin/Xvfb", f":{display}", "-screen", "0", "1440x900x24", "-nolisten", "tcp", "-ac"])
    rt.procs.append(xv)
    deadline = time.time() + 10
    while time.time() < deadline and not x_socket(display).exists() and xv.poll() is None:
        time.sleep(0.2)
    if not x_socket(display).exists():
        raise RuntimeError(f"Xvfb display :{display} did not start")
    env = os.environ.copy(); env["DISPLAY"] = f":{display}"; env["HOME"] = "/home/ubuntu"
    vn = spawn(["/usr/bin/x11vnc", "-display", f":{display}", "-rfbport", str(vnc), "-localhost", "-forever", "-shared", "-nopw", "-quiet"], env=env)
    rt.procs.append(vn)
    ws = spawn(["/usr/bin/websockify", "--web", "/usr/share/novnc", f"127.0.0.1:{viewer}", f"127.0.0.1:{vnc}"])
    rt.procs.append(ws)
    ch = spawn([
        "/usr/bin/google-chrome",
        f"--user-data-dir={profile_dir}",
        "--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={debug}",
        "--no-first-run", "--no-default-browser-check", "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled", "--password-store=basic",
        "--window-size=1440,900", "--start-maximized", tool_url,
    ], env=env)
    rt.procs.append(ch)
    update(pid, browser_pid=ch.pid, browser_state="STARTING")
    return rt


def cdp(p: dict):
    try:
        pages = requests.get(f"http://127.0.0.1:{int(p['debug_port'])}/json/list", timeout=2).json()
        pages = [x for x in pages if x.get("type") == "page"]
        url = str(pages[0].get("url") or "") if pages else ""
        title = str(pages[0].get("title") or "") if pages else ""
        joined = " ".join(str(x.get("url") or "") for x in pages).lower()
        login = "LOGIN_REQUIRED" if ("accounts.google.com" in joined or "signin" in joined) else ("FLOW_OPEN" if "flow.google.com/project/" in joined else ("FLOW_HOME" if "flow.google.com" in joined else "BROWSER_READY"))
        return True, url, title, login
    except Exception:
        return False, "", "", "UNKNOWN"


def rows():
    with db() as con:
        return [dict(r) for r in con.execute("SELECT * FROM autoflow_flow_profiles ORDER BY id").fetchall()]


def main():
    runtimes: dict[int, Runtime] = {}
    try:
        while True:
            seen = set()
            for p in rows():
                pid = int(p["id"]); seen.add(pid)
                desired = str(p.get("browser_desired") or "STOPPED").upper()
                enabled = bool(p.get("enabled"))
                rt = runtimes.get(pid)
                restart_seq = int(p.get("restart_seq") or 0)
                if not enabled or desired != "RUNNING":
                    if rt:
                        rt.stop(); runtimes.pop(pid, None)
                    elif str(p.get("browser_state") or "") != "STOPPED":
                        update(pid, browser_state="STOPPED", browser_pid=0)
                    continue
                if rt and (rt.restart_seq != restart_seq or any(proc.poll() is not None for proc in rt.procs)):
                    rt.stop(); runtimes.pop(pid, None); rt = None
                if not rt:
                    try:
                        rt = launch(p); runtimes[pid] = rt
                    except Exception as exc:
                        update(pid, browser_state="ERROR", last_error=f"{type(exc).__name__}: {exc}"[:700], browser_pid=0)
                        time.sleep(2)
                        continue
                ok, url, title, login = cdp(p)
                if ok:
                    update(pid, browser_state="RUNNING", last_browser_url=url, last_browser_title=title, last_browser_seen_at=_now(), last_health_at=_now(), login_state=login, health=80 if login != "READY" else 100, last_error="")
                else:
                    update(pid, browser_state="STARTING")
            for pid in list(runtimes):
                if pid not in seen:
                    runtimes.pop(pid).stop()
            time.sleep(POLL_SECONDS)
    finally:
        for rt in list(runtimes.values()):
            rt.stop()


if __name__ == "__main__":
    main()

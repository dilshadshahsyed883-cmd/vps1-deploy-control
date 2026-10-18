#!/usr/bin/env python3
"""VPS1 first-app GitHub pilot. Only isolated loopback Storage Manager service.
Invoked by the minimal approved Agent1 Drive Bridge carrier. Never via GitHub SSH.
Read-only app API; no legacy imports, Nginx, Docker, workers or Auto Script.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

REPO = "dilshadshahsyed883-cmd/vps1-deploy-control"
HOST = "vps-01a23801"
UNIT = "vps1-storage-candidate.service"
PORT = 8149
UNIT_FILE = Path("/etc/systemd/system") / UNIT
ENV_FILE = Path("/etc/fresh-website/vps-storage-manager.env")
STATE = Path("/var/lib/fresh-website/vps-storage-manager")
OLD = (Path("/opt/vps-storage-manager"), Path("/var/lib/vps-storage-manager"))
CURRENT = Path("/srv/fresh-website/current/vps-storage-manager")
RELEASES = Path("/srv/fresh-website/releases/vps-storage-manager")
SOURCE_FILE = "apps/vps-storage-manager/part01/storage_manager.py"
TEMPLATE_FILE = "apps/vps-storage-manager/candidate/vps1-storage-candidate.service"


def event(msg):
    print("GITHUB_STORAGE_PILOT: " + msg, flush=True)


def run(*args, timeout=30):
    return subprocess.run(args, check=True, timeout=timeout, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def guard(ok, reason):
    if not ok:
        raise RuntimeError("BLOCKED: " + reason)


def git_blob_digest(data):
    header = ("blob " + str(len(data)) + "\0").encode()
    return hashlib.sha1(header + data).hexdigest()


def fetch(commit, path, blob):
    guard(re.fullmatch("[0-9a-f]{40}", commit) is not None, "invalid GitHub source commit")
    guard(re.fullmatch("[0-9a-f]{40}", blob) is not None, "invalid expected git blob SHA")
    url = "https://raw.githubusercontent.com/" + REPO + "/" + commit + "/" + path
    req = urllib.request.Request(url, headers={"User-Agent": "vps1-storage-pilot/1"})
    with urllib.request.urlopen(req, timeout=35) as response:
        data = response.read(180001)
    guard(0 < len(data) <= 180000, "source file out of size bounds")
    guard(git_blob_digest(data) == blob, "GitHub immutable blob integrity mismatch for " + path)
    event("SOURCE_VERIFIED=" + path + " SHA256=" + hashlib.sha256(data).hexdigest())
    return data


def preflight(release):
    guard(os.geteuid() == 0, "root requires pre-existing sudo -n authority")
    guard(socket.gethostname().split(".")[0] == HOST, "unexpected fresh VPS hostname")
    guard(sys.platform == "linux" and os.uname().machine == "x86_64", "unexpected architecture")
    os_info = dict(x.strip().split("=", 1) for x in Path("/etc/os-release").read_text().splitlines() if "=" in x)
    guard(os_info.get("ID", "").strip('"') == "ubuntu" and os_info.get("VERSION_ID", "").strip('"') == "24.04", "unexpected OS")
    required = ("systemctl", "systemd-analyze", "ss", "useradd", "groupadd", "runuser", "getent")
    guard(all(shutil.which(b) for b in required), "missing service management binary")
    guard(subprocess.run(["systemctl", "is-active", "--quiet", "master-drive-agent.service"]).returncode == 0, "Agent 1 unavailable")
    guard(not any(x.exists() or x.is_symlink() for x in (UNIT_FILE, ENV_FILE, CURRENT, release, *OLD)), "candidate/pre-existing app resources found")
    s = subprocess.run(["ss", "-lntH"], check=True, text=True, capture_output=True).stdout
    guard(not any(line.split()[3].rsplit(":", 1)[-1] == str(PORT) for line in s.splitlines()), "port 8149 occupied")
    free = shutil.disk_usage("/").free
    guard(free >= 5_000_000_000, "5 GB minimum free-disk floor not met")
    event("PREFLIGHT=PASS HOST=" + HOST + " FREE_BYTES=" + str(free))


def check_http(token):
    url = "http://127.0.0.1:8149"
    def request(path, method="GET", auth=False):
        req = urllib.request.Request(url + path, method=method,
            headers=({"X-Storage-Token": token} if auth else {}))
        try:
            with urllib.request.urlopen(req, timeout=2) as resp:
                return resp.status, resp.read(32768)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(32768)
    for _ in range(35):
        try:
            health, body = request("/health")
            if health == 200 and json.loads(body).get("ok") is True:
                break
        except (OSError, ValueError):
            pass
        time.sleep(0.2)
    else:
        raise RuntimeError("health HTTP probe did not pass")
    guard(request("/v1/policy")[0] == 403, "anonymous policy exposed")
    status, body = request("/v1/policy", auth=True)
    guard(status == 200 and json.loads(body)["policy"]["auto_enabled"] is False, "authenticated policy/scheduler check failed")
    guard(request("/v1/cleanup", "POST", True)[0] == 403, "cleanup POST not blocked")
    guard(request("/v1/services/start", "POST", True)[0] == 403, "service start POST not blocked")
    ss = subprocess.run(["ss", "-lntH"], text=True, check=True, capture_output=True).stdout
    listeners = [ln.split()[3] for ln in ss.splitlines() if ln.split()[3].rsplit(":", 1)[-1] == "8149"]
    guard(listeners == ["127.0.0.1:8149"], "service not exclusively loopback")
    guard(STATE.joinpath("storage-manager.sqlite3").is_file(), "fresh state DB missing")
    guard(STATE.stat().st_uid == pwd.getpwnam("vps1storage").pw_uid, "state owner mismatch")
    event("HEALTH=PASS AUTH_ALLOW_DENY=PASS POST_DENY=PASS SCHEDULER=OFF BIND=LOOPBACK")


def deploy(commit, source_blob, unit_blob):
    release = RELEASES / commit
    preflight(release)
    source = fetch(commit, SOURCE_FILE, source_blob)
    template = fetch(commit, TEMPLATE_FILE, unit_blob)
    compile(source, SOURCE_FILE, "exec")
    guard(all(g.encode() in source for g in
        ("validate_candidate_runtime()", "candidate read-only", "FRESH_WEBSITE_SMOKE_TEST")), "source missing security guards")
    guard(all(g.encode() in template for g in
        ("User=vps1storage", "ProtectSystem=strict", "NoNewPrivileges=yes",
         "Environment=STORAGE_MANAGER_HOST=127.0.0.1",
         "Environment=FRESH_WEBSITE_SMOKE_TEST=1", "Environment=VPS1_RUNTIME_CANDIDATE=1")), "systemd template is not guarded")
    token = secrets.token_urlsafe(42)
    changed = False
    try:
        changed = True
        if subprocess.run(["getent", "group", "vps1storage"], stdout=subprocess.DEVNULL).returncode:
            run("groupadd", "--system", "vps1storage")
        if subprocess.run(["id", "vps1storage"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
            run("useradd", "--system", "--gid", "vps1storage", "--home-dir", "/nonexistent",
                "--shell", "/usr/sbin/nologin", "vps1storage")
        uid = pwd.getpwnam("vps1storage").pw_uid
        gid = pwd.getpwnam("vps1storage").pw_gid
        for root in (Path("/srv/fresh-website"), Path("/srv/fresh-website/releases"), RELEASES,
                     CURRENT.parent, Path("/var/lib/fresh-website")):
            root.mkdir(parents=True, exist_ok=True)
            root.chmod(0o755)
        app_dir = release / "part01"
        app_dir.mkdir(parents=True, exist_ok=False)
        src_path = app_dir / "storage_manager.py"
        src_path.write_bytes(source)
        src_path.chmod(0o644)
        STATE.mkdir(mode=0o750, parents=True, exist_ok=False)
        STATE.chmod(0o750)
        os.chown(STATE, uid, gid)
        ENV_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        # Token exists exclusively on VPS, not in GitHub, Drive job, stdout or reports.
        fd = os.open(str(ENV_FILE), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("STORAGE_MANAGER_TOKEN=" + token + "\n")
        CURRENT.symlink_to(release)
        UNIT_FILE.write_bytes(template)
        UNIT_FILE.chmod(0o644)
        run("systemctl", "daemon-reload")
        run("systemd-analyze", "verify", str(UNIT_FILE))
        run("systemctl", "enable", "--now", UNIT, timeout=30)
        run("systemctl", "is-active", "--quiet", UNIT)
        check_http(token)
        guard(CURRENT.resolve() == release and shutil.disk_usage("/").free >= 5_000_000_000,
              "release pointer or 5GB floor violated")
        event("UNIT=" + UNIT + " ACTIVE=YES SOURCE_COMMIT=" + commit)
        event("ROLLBACK_BOUNDARY=APP_ONLY_NO_LEGACY_IMPORT_NO_PUBLIC_ROUTE")
        event("RESULT=PASS_ISOLATED_STORAGE_CANDIDATE APP_INSTALL_COUNT=1 EXIT_CODE=0")
    except Exception as exc:
        if changed:
            subprocess.run(["systemctl", "disable", "--now", UNIT], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if UNIT_FILE.exists(): UNIT_FILE.unlink()
            subprocess.run(["systemctl", "daemon-reload"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if CURRENT.is_symlink(): CURRENT.unlink()
            if ENV_FILE.exists(): ENV_FILE.unlink()
            if release.exists(): shutil.rmtree(release)
            # State is intentionally preserved for investigation/recovery even on failure.
            event("ROLLBACK=APP_ONLY_UNIT_POINTER_ENV_RELEASE_CLEANED STATE_PRESERVED")
        raise RuntimeError(str(exc)) from exc


def main():
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["deploy"])
    p.add_argument("commit")
    p.add_argument("source_blob")
    p.add_argument("unit_blob")
    args = p.parse_args()
    try:
        deploy(args.commit, args.source_blob, args.unit_blob)
    except Exception as exc:
        event("RESULT=BLOCKED_INSTALL " + str(exc).splitlines()[0][:300])
        raise SystemExit(24)


if __name__ == "__main__":
    main()

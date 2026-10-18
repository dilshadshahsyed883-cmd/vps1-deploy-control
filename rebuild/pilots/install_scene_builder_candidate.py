#!/usr/bin/env python3
"""GitHub-pinned, Scene Builder only, internal read-only VPS_1 pilot.
Called by one hash-verified Drive Bridge V3 Agent 1 carrier. No GitHub Actions SSH.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HOST="vps-01a23801"
GIT_URL="https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control.git"
UNIT="vps1-scene-candidate.service"
UNIT_FILE=Path("/etc/systemd/system")/UNIT
ENV=Path("/etc/fresh-website/scene-builder.env")
STATE=Path("/var/lib/fresh-website/scene-builder")
CURRENT=Path("/srv/fresh-website/current/scene-builder")
RELEASES=Path("/srv/fresh-website/releases/scene-builder")
VENV=Path("/srv/fresh-website/venvs/scene-builder")
PORT=18080

def message(s):
    print("GITHUB_SCENE_PILOT: "+s,flush=True)

def guard(ok,s):
    if not ok:raise RuntimeError("BLOCKED: "+s)

def execute(*cmd,timeout=60,env=None,cwd=None):
    p=subprocess.run(cmd,check=False,timeout=timeout,stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,text=True,env=env,cwd=cwd)
    if p.returncode:raise RuntimeError("command failed: "+str(cmd[0])+
        " "+str(cmd[1] if len(cmd)>1 else "")+" exit="+str(p.returncode)+
        " (sanitized stderr omitted)")
    return p

def http(path,method="GET",headers=None):
    req=urllib.request.Request("http://127.0.0.1:18080"+path,method=method,headers=headers or {})
    try:
        with urllib.request.urlopen(req,timeout=2) as res:
            return res.status,res.read(32768)
    except urllib.error.HTTPError as err:
        return err.code,err.read(32768)

def live_checks(probe):
    for _ in range(80):
        if subprocess.run(["systemctl","is-active","--quiet",UNIT]).returncode:
            raise RuntimeError("candidate service not active")
        try:
            status,data=http("/health")
            if status==200 and json.loads(data).get("mode")=="shadow-read-only":
                break
        except (OSError,ValueError):pass
        time.sleep(.25)
    else:
        raise RuntimeError("scene-builder health did not pass")
    guard(http("/internal/runtime-ready")[0]==403,"runtime probe public")
    status,data=http("/internal/runtime-ready",headers={"X-Runtime-Probe-Token":probe})
    guard(status==200 and json.loads(data).get("side_effect_workers")=="disabled","probe token or workers gate")
    guard(http("/login",method="POST")[0]==403,"write/login POST unblocked")
    guard(http("/api/jobs",method="POST")[0]==403,"queued write POST unblocked")
    listeners=subprocess.run(["ss","-lntH"],check=True,text=True,capture_output=True).stdout
    app=[line.split()[3] for line in listeners.splitlines() if line.split()[3].rsplit(":",1)[-1]=="18080"]
    guard(app==["127.0.0.1:18080"],"not exclusively loopback bound")
    guard(STATE.joinpath("web","web.sqlite3").is_file(),"fresh web schema not present")
    guard(STATE.stat().st_uid==pwd.getpwnam("vps1scene").pw_uid,"state owner differs")
    guard(CURRENT.is_symlink() and CURRENT.resolve().is_dir(),"release pointer absent")
    message("HEALTH=PASS PROBE_ALLOW_DENY=PASS POST_DENY=PASS WORKERS=OFF BIND=127.0.0.1:18080")

def preflight(commit):
    guard(os.geteuid()==0,"sudo -n for Agent1 required")
    guard(len(commit)==40 and all(ch in "0123456789abcdef" for ch in commit),"immutable commit invalid")
    guard(socket.gethostname().split(".")[0]==HOST,"unexpected host identity")
    guard(os.uname().machine=="x86_64","unexpected architecture")
    osrel=dict(line.split("=",1) for line in Path("/etc/os-release").read_text().splitlines() if "=" in line)
    guard(osrel.get("ID","").strip('"')=="ubuntu" and osrel.get("VERSION_ID","").strip('"')=="24.04","unexpected Ubuntu release")
    guard(all(shutil.which(x) for x in ("git","python3","systemctl","ss","systemd-analyze","runuser")),"minimum GitHub/Python/systemd utilities absent")
    guard(subprocess.run(["systemctl","is-active","--quiet","master-drive-agent.service"]).returncode==0,"Agent1 inactive")
    guard(subprocess.run(["systemctl","is-active","--quiet","vps1-storage-candidate.service"]).returncode==0,"Storage Manager dependency not active")
    status=urllib.request.urlopen("http://127.0.0.1:8149/health",timeout=3).status
    guard(status==200,"Storage Manager dependency health failed")
    guard(not any(p.exists() or p.is_symlink() for p in (CURRENT,UNIT_FILE,ENV,RELEASES/commit,VENV)),"Scene existing state/unit/release/venv requires scoped upgrade")
    guard(not STATE.exists(),"Scene state already exists, refusing to overwrite")
    out=subprocess.run(["ss","-lntH"],text=True,check=True,capture_output=True).stdout
    guard(not any(line.split()[3].rsplit(":",1)[-1]=="18080" for line in out.splitlines()),"port 18080 already bound")
    free=shutil.disk_usage("/").free
    guard(free>=5_000_000_000,"5GB minimum free floor unavailable")
    message("PREFLIGHT=PASS STORAGE_DEPENDENCY=PASS HOST="+HOST+" FREE_BYTES="+str(free))

def checkout_pinned(commit,directory):
    execute("git","init","-q",str(directory),timeout=12)
    execute("git","-C",str(directory),"sparse-checkout","init","--cone",timeout=12)
    execute("git","-C",str(directory),"sparse-checkout","set","apps/scene-builder/part01","apps/scene-builder/candidate",timeout=12)
    execute("git","-C",str(directory),"fetch","-q","--no-tags","--depth=1",GIT_URL,commit,timeout=110)
    execute("git","-C",str(directory),"checkout","-q","--force","FETCH_HEAD",timeout=45)
    head=subprocess.check_output(["git","-C",str(directory),"rev-parse","HEAD"],text=True,timeout=10).strip()
    guard(head==commit,"fetched GitHub commit identity mismatch")
    source=directory/"apps/scene-builder/part01"
    template=directory/"apps/scene-builder/candidate/vps1-scene-candidate.service"
    requirements=source/"requirements.txt"
    guard(all(p.is_file() for p in (source/"app.py",source/"runtime_guard.py",source/"run.py",template,requirements)),"release source incomplete")
    for file in (source/"app.py",source/"runtime_guard.py",source/"run.py"):
        compile(file.read_text(encoding="utf-8"),str(file),"exec")
    unit=template.read_text(encoding="utf-8")
    for required in ("User=vps1scene","NoNewPrivileges=yes","ProtectSystem=strict",
        "Environment=FRESH_AUTO_SCRIPT_READY=0","Environment=WEB_HOST=127.0.0.1",
        "Environment=WEB_PORT=18080"):
        guard(required in unit,"unsafe unit / missing "+required)
    guard('itsdangerous==2.2.0' in requirements.read_text(),"reproduced startup dependency not pinned")
    message("GITHUB_PINNED_SOURCE=PASS COMMIT="+head)
    return source,template

def install(commit):
    preflight(commit)
    stage=Path(tempfile.mkdtemp(prefix="vps1-scene-pilot-"))
    changed=False
    try:
        checkout=stage/"checkout"
        source,template=checkout_pinned(commit,checkout)
        changed=True
        if subprocess.run(["getent","group","vps1scene"],stdout=subprocess.DEVNULL).returncode:
            execute("groupadd","--system","vps1scene")
        if subprocess.run(["id","vps1scene"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode:
            execute("useradd","--system","--gid","vps1scene","--home-dir","/nonexistent","--shell","/usr/sbin/nologin","vps1scene")
        uid=pwd.getpwnam("vps1scene").pw_uid
        gid=pwd.getpwnam("vps1scene").pw_gid
        release=RELEASES/commit
        RELEASES.mkdir(parents=True,exist_ok=True)
        target=release/"part01"
        shutil.copytree(source,target,symlinks=False)
        for parent in (CURRENT.parent,VENV.parent):
            parent.mkdir(parents=True,exist_ok=True)
        # Existing host Python 3.12; Debian may need python3.12-venv package.
        test=subprocess.run(["python3","-m","venv",str(VENV)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        if test.returncode:
            shutil.rmtree(VENV,ignore_errors=True)
            execute("apt-get","update","-qq",timeout=110)
            installenv=dict(os.environ,DEBIAN_FRONTEND="noninteractive")
            execute("apt-get","install","-y","--no-install-recommends","python3.12-venv",timeout=110,env=installenv)
            execute("python3","-m","venv",str(VENV),timeout=40)
        execute(str(VENV/"bin/python"),"-m","pip","install","--disable-pip-version-check","-q",
                "-r",str(target/"requirements.txt"),timeout=170)
        STATE.mkdir(mode=0o750,parents=True,exist_ok=False)
        os.chown(STATE,uid,gid)
        dirs=["web","permanent","script-work","flow-results"]
        for sub in dirs:
            p=STATE/sub
            p.mkdir(mode=0o750)
            os.chown(p,uid,gid)
        ENV.parent.mkdir(parents=True,exist_ok=True,mode=0o750)
        access=secrets.token_urlsafe(42)
        session=secrets.token_urlsafe(42)
        probe=secrets.token_urlsafe(42)
        settings={
            "WEB_ACCESS_KEY":access,"SESSION_SECRET":session,"SCENE_RUNTIME_PROBE_TOKEN":probe,
            "WEB_DATA_DIR":str(STATE/"web"),
            "SCENE_PERMANENT_ROOT":str(STATE/"permanent"),
            "SCRIPT_WORK_DATA_DIR":str(STATE/"script-work"),
            "MASTER_CONTROLLER_DB_PATH":str(STATE/"master-controller.sqlite3"),
            "ATOMIC_FLOW_DB_PATH":str(STATE/"atomic-flow.sqlite3"),
            "AUTOFLOW_FLOW_RESULT_ROOT":str(STATE/"flow-results"),
            "SCENE_CHANNELS_FILE":str(STATE/"channels.json"),
            "WORKER_URL":"http://127.0.0.1:1",
            "STORAGE_MANAGER_URL":"http://127.0.0.1:8149",
        }
        fd=os.open(str(ENV),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,"w",encoding="utf-8") as out:
            for k,v in settings.items():out.write(k+"="+v+"\n")
        CURRENT.symlink_to(release)
        UNIT_FILE.write_bytes(template.read_bytes())
        UNIT_FILE.chmod(0o644)
        execute("systemctl","daemon-reload")
        execute("systemd-analyze","verify",str(UNIT_FILE))
        execute("systemctl","enable","--now",UNIT,timeout=45)
        live_checks(probe)
        guard(shutil.disk_usage("/").free>=5_000_000_000,"5GB free reserve violated")
        message("SERVICE=ACTIVE UNIT="+UNIT+" SOURCE_COMMIT="+commit)
        message("ROLLBACK_BOUNDARY=SCENE_ONLY PRESERVE_NEW_FRESH_STATE=YES")
        message("RESULT=PASS_ISOLATED_SCENE_BUILDER_CANDIDATE APP_INSTALL_COUNT=2 EXIT_CODE=0")
    except Exception as exc:
        if changed:
            subprocess.run(["systemctl","disable","--now",UNIT],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            if UNIT_FILE.exists():UNIT_FILE.unlink()
            subprocess.run(["systemctl","daemon-reload"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            if CURRENT.is_symlink():CURRENT.unlink()
            if ENV.exists():ENV.unlink()
            if (RELEASES/commit).exists():shutil.rmtree(RELEASES/commit)
            if VENV.exists():shutil.rmtree(VENV)
            # Preserve isolated state/SQLite on a failed first install for recovery.
            message("ROLLBACK=SCENE_ONLY_UNIT_POINTER_ENV_CODE_VENV_REVERTED STATE_PRESERVED")
        raise RuntimeError(str(exc).splitlines()[0][:250]) from None
    finally:
        shutil.rmtree(stage,ignore_errors=True)

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("action",choices=["deploy"])
    parser.add_argument("commit")
    args=parser.parse_args()
    try:install(args.commit)
    except Exception as exc:
        message("RESULT=BLOCKED_INSTALL "+str(exc).splitlines()[0][:300])
        raise SystemExit(24)

if __name__=="__main__":main()

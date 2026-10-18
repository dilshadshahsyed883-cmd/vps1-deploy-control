#!/usr/bin/env python3
"""Reversible update of existing Scene Builder, without touching any other app.
Only GitHub main SHA, only VPS1 Agent1. Enables owner login + safe preset edits;
render queue remains gated until worker runtime and end-to-end tests are ready.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode

HOST="vps-01a23801"
REPO="https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control.git"
UNIT="vps1-scene-candidate.service"
OLD="608ffe99293c8aaca400a8c328e05f7090ff8b9c"
ROOT=Path("/srv/fresh-website")
CURRENT=ROOT/"current/scene-builder"
RELEASES=ROOT/"releases/scene-builder"
ENV=Path("/etc/fresh-website/scene-builder.env")
DATA=Path("/var/lib/fresh-website/scene-builder")
TX=ROOT/"transactions/scene-builder-login"

def event(s):print("SCENE_UPGRADE: "+s,flush=True)
def guard(c,m):
    if not c:raise RuntimeError("BLOCKED: "+m)
def run(cmd,timeout=120,cwd=None):
    r=subprocess.run([str(p) for p in cmd],cwd=cwd,capture_output=True,text=True,timeout=timeout)
    if r.returncode:raise RuntimeError("command failed: "+str(cmd[0])+" exit="+str(r.returncode))
    return r.stdout.strip()

def get_health(timeout=35):
    """Wait for the actual HTTP listener after systemd Type=simple restart.

    systemctl restart can return before uvicorn has bound the local port.
    Fail closed when the health probe never becomes ready.
    """
    deadline=time.monotonic()+timeout
    while True:
        try:
            with urllib.request.urlopen("http://127.0.0.1:18080/health",timeout=3) as resp:
                if resp.status==200 and json.load(resp).get("ok") is True:
                    return True
        except (urllib.error.URLError,TimeoutError,ConnectionError,ValueError):
            pass
        remaining=deadline-time.monotonic()
        if remaining<=0:
            return False
        time.sleep(min(1.0,remaining))

def require_host(sha):
    guard(os.geteuid()==0,"pre-existing Agent1 sudo authority required")
    guard(socket.gethostname().split(".")[0]==HOST,"host mismatch")
    guard(re.fullmatch("[0-9a-f]{40}",sha) is not None,"invalid commit")
    guard(shutil.disk_usage("/").free>=5_000_000_000,"5GB free-space reserve")
    head=run(["git","ls-remote",REPO,"refs/heads/main"],timeout=30).split()[0]
    guard(sha==head,"approved release not latest protected main")
    for s in ("master-drive-agent.service",UNIT,"vps1-storage-candidate.service"):
        guard(subprocess.run(["systemctl","is-active","--quiet",s]).returncode==0,s+" inactive")
    guard(get_health(),"existing Scene health failed")
    guard(ENV.is_file() and DATA.is_dir() and CURRENT.is_symlink(),"original app ownership/state not verified")
    guard(CURRENT.resolve().is_relative_to(RELEASES),"existing release points outside allowed directory")

def checkout_source(sha):
    base=Path(tempfile.mkdtemp(prefix="vps1-scene-source-"))
    run(["git","init","-q",str(base)],timeout=15)
    run(["git","-C",str(base),"sparse-checkout","init","--cone"],timeout=15)
    run(["git","-C",str(base),"sparse-checkout","set","apps/scene-builder/part01"],timeout=15)
    run(["git","-C",str(base),"fetch","-q","--depth=1",REPO,sha],timeout=120)
    run(["git","-C",str(base),"checkout","-q","FETCH_HEAD"],timeout=30)
    guard(run(["git","-C",str(base),"rev-parse","HEAD"])==sha,"commit mismatch")
    source=base/"apps/scene-builder/part01"
    for filename in ("app.py","precutover_policy.py","runtime_guard.py","run.py","requirements.txt"):
        guard((source/filename).is_file(),"required app source absent "+filename)
    guard("VPS1_SCENE_PRECUTOVER_EDITING" in (source/"app.py").read_text(),"candidate write guard absent")
    import ast
    for name in ("app.py","precutover_policy.py","runtime_guard.py"):
        ast.parse((source/name).read_text())
    return base,source

def sql_backups(dst):
    target=dst/"sqlite"
    target.mkdir(mode=0o700)
    databases=list(DATA.rglob("*.sqlite3"))
    guard(len(databases)<=25,"too many databases; abort safe backup")
    for src in databases:
        guard(src.is_file() and not src.is_symlink(),"unsafe database path")
        guard(src.stat().st_size<=2_000_000_000,"database backup too large for guarded pilot")
        rel=src.relative_to(DATA)
        out=target/rel
        out.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        with sqlite3.connect(f"file:{src}?mode=ro",uri=True) as a,sqlite3.connect(out) as b:
            a.backup(b)
            guard(b.execute("PRAGMA integrity_check").fetchone()[0]=="ok","SQLite backup integrity failed")
        out.chmod(0o600)
    event("SQLITE_BACKUPS="+str(len(databases))+" STATUS=PASS")

def test_login():
    raw=dict(line.split("=",1) for line in ENV.read_text().splitlines() if "=" in line and not line.startswith("#"))
    guard(raw.get("VPS1_SCENE_PRECUTOVER_EDITING")=="1","editing flag not enabled")
    user=raw.get("WEB_USERNAME","team")
    secret=raw.get("WEB_ACCESS_KEY","")
    guard(len(secret)>=24,"admin credential absent")
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*a,**k):return None
    body=urlencode({"username":user,"secret_key":secret}).encode()
    req=urllib.request.Request("http://127.0.0.1:18080/login",data=body,method="POST")
    try:
        urllib.request.build_opener(NoRedirect).open(req,timeout=6)
        guard(False,"login not redirected")
    except urllib.error.HTTPError as e:
        guard(e.code==303 and "session=" in e.headers.get("Set-Cookie",""),"login/session error")
    blocked=urllib.request.Request("http://127.0.0.1:18080/queue/submit",data=b"",method="POST")
    try:
        urllib.request.urlopen(blocked,timeout=5)
        guard(False,"queue mutations unexpectedly enabled")
    except urllib.error.HTTPError as e:
        guard(e.code==403,"queue gate not fail-closed")
    event("OWNER_LOGIN=PASS COOKIE=SECURE QUEUE_WRITES=DENIED")

def upgrade(sha):
    require_host(sha)
    existing=CURRENT.resolve()
    guard(existing==RELEASES/OLD,"unexpected existing Scene release; no upgrade")
    target=RELEASES/sha
    transaction=TX/sha
    guard(not target.exists() and not transaction.exists(),"release/rollback journal exists")
    temp=None
    changed=False
    env_bytes=ENV.read_bytes()
    try:
        temp,src=checkout_source(sha)
        release_src=target/"part01"
        release_src.parent.mkdir(mode=0o755,parents=True)
        shutil.copytree(src,release_src)
        for p in release_src.rglob("*"):
            if p.is_file():p.chmod(0o644)
        transaction.mkdir(mode=0o700,parents=True)
        (transaction/"environment.previous").write_bytes(env_bytes)
        (transaction/"environment.previous").chmod(0o600)
        (transaction/"previous_commit").write_text(OLD+"\n")
        sql_backups(transaction)
        content=env_bytes.decode()
        guard("VPS1_SCENE_PRECUTOVER_EDITING=" not in content,"old environment already has edit flag")
        changed=True
        with tempfile.NamedTemporaryFile(dir=ENV.parent,delete=False) as f:
            f.write(env_bytes+b"\nVPS1_SCENE_PRECUTOVER_EDITING=1\n")
            f.flush();os.fsync(f.fileno());name=f.name
        os.chmod(name,0o600);os.replace(name,ENV)
        next_link=CURRENT.with_name(".scene-builder-new")
        guard(not next_link.exists() and not next_link.is_symlink(),"temporary link collision")
        next_link.symlink_to(target);os.replace(next_link,CURRENT)
        run(["systemctl","restart",UNIT],timeout=35)
        guard(get_health(),"post-upgrade service unhealthy")
        test_login()
        guard(shutil.disk_usage("/").free>=5_000_000_000,"reserve exhausted")
        event("SOURCE_COMMIT="+sha+" RESULT=PASS_SCENE_LOGIN_PRECUTOVER")
    except Exception as exc:
        if changed:
            ENV.write_bytes(env_bytes);ENV.chmod(0o600)
            if CURRENT.is_symlink():
                tmp=CURRENT.with_name(".scene-builder-revert")
                tmp.symlink_to(existing);os.replace(tmp,CURRENT)
            try:run(["systemctl","restart",UNIT],timeout=35)
            except Exception:pass
            event("ROLLBACK=PREVIOUS_SCENE_RELEASE_AND_ENV_RESTORED STATE_PRESERVED")
        raise
    finally:
        if temp:shutil.rmtree(temp,ignore_errors=True)

def main():
    p=argparse.ArgumentParser();p.add_argument("mode",choices=["upgrade"]);p.add_argument("revision")
    a=p.parse_args()
    try:upgrade(a.revision)
    except Exception as e:
        event("RESULT=BLOCKED "+str(e).splitlines()[0][:220])
        raise SystemExit(24)
if __name__=="__main__":main()

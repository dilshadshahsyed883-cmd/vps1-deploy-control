#!/usr/bin/env python3
"""VPS_1 Agent 1 only: pinned GitHub Scene worker admission, zero render threads.
Does not touch the existing 18080/8080/8149 services or nginx/TLS.
"""
from __future__ import annotations
import fcntl
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

HOST="vps-01a23801"
GIT="https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control.git"
UNIT="vps1-scene-worker-admission.service"
UNIT_FILE=Path("/etc/systemd/system")/UNIT
BASE=Path("/srv/fresh-website")
RELEASES=BASE/"releases/scene-worker-admission"
CURRENT=BASE/"current/scene-worker-admission"
VENV_BASE=BASE/"venvs/scene-worker-admission"
STATE=Path("/var/lib/fresh-website/scene-worker-admission")
ENV=Path("/etc/fresh-website/scene-worker-admission.env")
LOCK=Path("/run/lock/vps1-scene-worker-admission.lock")
PORT=8000
DEPENDENCIES=("fastapi==0.115.12","uvicorn==0.34.2",
 "python-multipart==0.0.20","requests==2.32.3",
 "pillow==11.1.0","httpx==0.28.1")

def event(message):
    print("SCENE_WORKER_ADMISSION: "+message,flush=True)

def require(test,reason):
    if not test:
        raise RuntimeError("BLOCKED: "+reason)

def command(argv,timeout=120):
    r=subprocess.run([str(x) for x in argv],capture_output=True,timeout=timeout)
    if r.returncode:
        raise RuntimeError("command failure "+str(argv[0])+" rc="+str(r.returncode))
    return r.stdout.decode(errors="replace").strip()

def status(port,path,token=None,method="GET"):
    headers={"x-scene-worker-token":token} if token else {}
    req=urllib.request.Request("http://127.0.0.1:"+str(port)+path,
                               method=method,headers=headers)
    try:
        with urllib.request.urlopen(req,timeout=3) as resp:
            return resp.status,resp.read(32768)
    except urllib.error.HTTPError as exc:
        return exc.code,exc.read(32768)

def preflight(sha):
    require(os.geteuid()==0,"must run with preapproved Agent1 sudo -n")
    require(socket.gethostname().split(".")[0]==HOST,"VPS identity changed")
    require(re.fullmatch("[0-9a-f]{40}",sha) is not None,"not an immutable source commit")
    require(os.uname().machine=="x86_64","unexpected architecture")
    rel=dict(line.split("=",1) for line in Path("/etc/os-release").read_text().splitlines() if "=" in line)
    require(rel.get("ID","").strip('"')=="ubuntu" and rel.get("VERSION_ID","").strip('"')=="24.04","unexpected OS")
    require(all(shutil.which(x) for x in ("python3","git","systemctl","systemd-analyze","runuser","ss")),"missing native tools")
    for unit in ("master-drive-agent.service","vps1-scene-candidate.service",
                 "vps1-production-scene-builder.service","vps1-storage-candidate.service",
                 "nginx.service"):
        require(subprocess.run(["systemctl","is-active","--quiet",unit]).returncode==0,
                unit+" must remain active")
    for port in (18080,8080,8149):
        health,data=status(port,"/health")
        require(health==200 and json.loads(data).get("ok") is True,
                "existing app health failed port "+str(port))
    for p in (UNIT_FILE,ENV,CURRENT,STATE,RELEASES/sha,VENV_BASE/sha):
        require(not p.exists() and not p.is_symlink(),"existing admission resource; no overwrite")
    s=command(["ss","-lntH"],timeout=10)
    require(not any(l.split()[3].rsplit(":",1)[-1]==str(PORT) for l in s.splitlines()),
            "port 8000 not free")
    require(shutil.disk_usage("/").free>=5_000_000_000,"less than 5GB free")
    upstream=command(["git","ls-remote",GIT,"refs/heads/main"],timeout=30).split()[0]
    require(sha==upstream,"release SHA must equal current protected main")
    require(pwd.getpwnam("vps1scene").pw_uid>0,"isolated existing vps1scene account missing")
    event("PREFLIGHT=PASS HOST="+HOST+" PORT_8000=FREE OTHER_APPS=HEALTHY")

def checkout(revision,folder):
    command(["git","init","-q",str(folder)],timeout=12)
    command(["git","-C",str(folder),"sparse-checkout","init","--cone"],timeout=12)
    command(["git","-C",str(folder),"sparse-checkout","set","apps/scene-builder/part02"],timeout=12)
    command(["git","-C",str(folder),"fetch","-q","--no-tags","--depth=1",GIT,revision],timeout=135)
    command(["git","-C",str(folder),"checkout","-q","FETCH_HEAD"],timeout=35)
    require(command(["git","-C",str(folder),"rev-parse","HEAD"],timeout=10)==revision,
            "source Git SHA mismatch")
    part=folder/"apps/scene-builder/part02"
    for name in ("api_server.py","worker_registry.py","internal_audio_engine.py",
                 "smart_scroll/v2_renderer.py","smart_scroll/project_model.py",
                 "smart_scroll/caption_sync.py"):
        require((part/name).is_file(),"required source missing: "+name)
    src=(part/"api_server.py").read_text()
    require('WORKER_ADMISSION_ONLY' in src and
            '"admission_only": WORKER_ADMISSION_ONLY' in src and
            'if WORKER_ADMISSION_ONLY:' in src,
            "read-only worker guard absent")
    import ast
    ast.parse(src)
    return part

def unit_file(revision):
    release=RELEASES/revision
    venv=VENV_BASE/revision
    return f"""[Unit]
Description=VPS1 Scene Builder worker API admission only, no render threads
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=vps1scene
Group=vps1scene
WorkingDirectory={release}
EnvironmentFile={ENV}
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PYTHONPATH={release}:{release}/part02
ExecStart={venv}/bin/uvicorn vps_engine.api_server:app --host 127.0.0.1 --port 8000 --workers 1
Restart=on-failure
RestartSec=4
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=true
ReadWritePaths={STATE}
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
UMask=0077
[Install]
WantedBy=multi-user.target
"""

def verify(token):
    for i in range(55):
        if subprocess.run(["systemctl","is-active","--quiet",UNIT]).returncode:
            raise RuntimeError("worker admission unit inactive")
        try:
            code,data=status(8000,"/health")
            if code==200 and json.loads(data).get("admission_only") is True:
                break
        except (OSError,ValueError):
            pass
        time.sleep(.4)
    else:
        raise RuntimeError("worker admission health did not initialize")
    doc=json.loads(data)
    require(doc.get("ok") is False and doc.get("queue_counts")=={},"admission must NOT indicate worker online")
    require(doc.get("drive_upload_enabled") is False,"Drive auto upload unexpectedly on")
    require(status(8000,"/jobs")[0]==401,"anonymous jobs API exposed")
    require(status(8000,"/jobs",token="wrong")[0]==401,"incorrect worker token accepted")
    c,body=status(8000,"/jobs",token=token)
    require(c==200 and json.loads(body).get("jobs")==[],"authenticated queue not empty")
    for path in ("/jobs","/queue/mode","/pipeline/resume","/jobs/unknown/clean-now"):
        require(status(8000,path,method="POST",token=token)[0]==403,
                "mutation route not blocked "+path)
    binding=command(["ss","-lntH"],timeout=10)
    listeners=[z.split()[3] for z in binding.splitlines() if z.split()[3].rsplit(":",1)[-1]=="8000"]
    require(listeners==["127.0.0.1:8000"],"worker not exclusively loopback")
    import sqlite3
    db=STATE/"queue.sqlite3"
    require(db.is_file(),"worker sqlite schema absent")
    with sqlite3.connect("file:"+str(db)+"?mode=ro",uri=True) as connection:
        require(connection.execute("select count(*) from jobs").fetchone()[0]==0,
                "queue suddenly nonempty")
    for port in (18080,8080,8149):
        c,body=status(port,"/health")
        require(c==200 and json.loads(body).get("ok") is True,
                "existing app regressed port "+str(port))
    # Existing public login stays reachable via TLS, no Nginx edits.
    probe=subprocess.run(["curl","-fsS","--connect-timeout","5",
       "--max-time","10","--resolve","app.scenebuilder.online:443:127.0.0.1",
       "https://app.scenebuilder.online/login"],capture_output=True,timeout=15)
    require(probe.returncode==0 and b"html" in probe.stdout.lower(),"existing HTTPS login regressed")
    event("HEALTH=PASS TOKEN_ALLOW_DENY=PASS EMPTY_QUEUE=PASS NO_WORKER_THREADS=PASS")
    event("HTTPS_LOGIN=PRESERVED APP_18080=PRESERVED APP_8080=PRESERVED STORAGE_8149=PRESERVED")

def install(revision):
    preflight(revision)
    working=Path(tempfile.mkdtemp(prefix="scene-worker-admission-git-"))
    changed=False
    try:
        part=checkout(revision,working/"checkout")
        release=RELEASES/revision
        venv=VENV_BASE/revision
        changed=True
        release.parent.mkdir(parents=True,exist_ok=True)
        release.mkdir()
        shutil.copytree(part,release/"part02")
        (release/"vps_engine").symlink_to("part02",target_is_directory=True)
        venv.parent.mkdir(parents=True,exist_ok=True)
        command(["python3","-m","venv",str(venv)],timeout=45)
        command([str(venv/"bin/python"),"-m","pip","install","--disable-pip-version-check",
                 "--no-input","--quiet",*DEPENDENCIES],timeout=220)
        state_uid=pwd.getpwnam("vps1scene").pw_uid
        state_gid=pwd.getpwnam("vps1scene").pw_gid
        STATE.mkdir(mode=0o750)
        os.chown(STATE,state_uid,state_gid)
        ENV.parent.mkdir(parents=True,exist_ok=True)
        token=secrets.token_urlsafe(48)
        data=(
            "SCENE_WORKER_DATA="+str(STATE)+"\n"
            "SCENE_WORKER_ADMISSION_ONLY=1\n"
            "SCENE_WORKER_TOKEN="+token+"\n"
            "SCENE_INTERNAL_AUDIO_ENGINE_ROOT="+str(STATE/"internal_audio")+"\n"
            "DRIVE_UPLOAD_ENABLED=0\nAUTO_CLEAN_MEDIA_AFTER_RENDER=0\n"
            "COMPRESSOR_ENABLED=0\nMIN_FREE_SPACE_GB=5\n"
            "PYTHONDONTWRITEBYTECODE=1\n"
        )
        fd=os.open(str(ENV),os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,"w") as file:file.write(data)
        CURRENT.parent.mkdir(parents=True,exist_ok=True)
        CURRENT.symlink_to(release)
        UNIT_FILE.write_text(unit_file(revision))
        UNIT_FILE.chmod(0o644)
        command(["systemctl","daemon-reload"],timeout=30)
        command(["systemd-analyze","verify",str(UNIT_FILE)],timeout=15)
        command(["systemctl","enable","--now",UNIT],timeout=35)
        verify(token)
        require(shutil.disk_usage("/").free>=5_000_000_000,"5GB reserve breached")
        event("SOURCE_COMMIT="+revision)
        event("RESULT=PASS_SCENE_WORKER_ADMISSION_ONLY JOB_SUBMISSION=OFF PORT=8000")
    except Exception:
        if changed:
            subprocess.run(["systemctl","disable","--now",UNIT],capture_output=True)
            if UNIT_FILE.exists():UNIT_FILE.unlink()
            subprocess.run(["systemctl","daemon-reload"],capture_output=True)
            if CURRENT.is_symlink():CURRENT.unlink()
            if ENV.exists():ENV.unlink()
            if (RELEASES/revision).exists():shutil.rmtree(RELEASES/revision)
            if (VENV_BASE/revision).exists():shutil.rmtree(VENV_BASE/revision)
            event("ROLLBACK=WORKER_UNIT_RELEASE_ENV_ONLY NEW_STATE_PRESERVED")
        raise
    finally:
        shutil.rmtree(working,ignore_errors=True)

def main():
    if len(sys.argv)!=3 or sys.argv[1]!="deploy":
        raise SystemExit("usage: install_scene_worker_admission.py deploy 40hexsha")
    with LOCK.open("a+") as stream:
        fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:install(sys.argv[2])
        except Exception as exc:
            event("RESULT=BLOCKED "+str(exc).splitlines()[0][:230])
            raise SystemExit(24)
if __name__=="__main__":
    main()

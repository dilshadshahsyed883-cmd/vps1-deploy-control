#!/usr/bin/env python3
"""Ephemeral Ubuntu GitHub runner: activate real Storage Manager via systemd and rollback.

Never run on VPS_1. Refuses execution unless GitHub Actions is active and the
staging root is under /tmp. Installs only a transient unit on the disposable
CI runner, never a persistent production service.
"""
import argparse,json,os,pathlib,secrets,subprocess,sys,time,urllib.error,urllib.request
from rebuild import fresh_vps
APP="vps-storage-manager"
PORT=18454

def run(*args):
    return subprocess.run(args,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)

def probe(port):
    url=f"http://127.0.0.1:{port}/health"
    with urllib.request.urlopen(url,timeout=2) as r:
        obj=json.loads(r.read())
    if not obj.get("ok") or not obj.get("version"):
        raise ValueError("invalid real storage manager health contract")
    return obj

def ready(unit):
    deadline=time.monotonic()+20
    while time.monotonic()<deadline:
        status=subprocess.run(["sudo","systemctl","is-active","--quiet",unit])
        if status.returncode==0:
            try:return probe(PORT)
            except (urllib.error.URLError,TimeoutError,ConnectionError):pass
        time.sleep(0.25)
    logs=subprocess.run(["sudo","systemctl","status",unit,"--no-pager","-n","12"],capture_output=True,text=True)
    raise RuntimeError("transient systemd service not healthy: "+logs.stdout[-1200:])

def main(a):
    if os.getenv("GITHUB_ACTIONS")!="true" or not os.getenv("GITHUB_RUN_ID"):
        raise ValueError("must be run on an isolated GitHub Actions runner")
    root=fresh_vps.root_dir(a.root)
    current=root/"srv/fresh-website/current"/APP
    if not current.is_symlink():raise ValueError("real source release not staged")
    data=root/"var/lib/fresh-website"/APP/"storage"
    data.mkdir(parents=True,exist_ok=True)
    unit="fresh-storage-ci-"+str(os.environ["GITHUB_RUN_ID"])+".service"
    port=str(PORT)
    token="ci-"+secrets.token_hex(20)
    script=str(current/"part01/storage_manager.py")
    username=os.getenv("USER","runner")
    if username in ("root",""):raise ValueError("refuse unit running as root")
    if subprocess.run(["sudo","systemctl","is-system-running"],stdout=subprocess.DEVNULL).returncode not in (0,1):
        raise RuntimeError("ephemeral runner systemd unavailable")
    params=[
       "sudo","systemd-run","--quiet","--unit="+unit,"--collect",
       "--property=User="+username,
       "--property=NoNewPrivileges=yes",
       "--property=ProtectSystem=strict",
       "--property=ReadWritePaths="+str(data),
       "--setenv=FRESH_WEBSITE_SMOKE_TEST=1",
       "--setenv=STORAGE_MANAGER_STATE_ROOT="+str(data),
       "--setenv=STORAGE_MANAGER_PORT="+port,
       "--setenv=STORAGE_MANAGER_HOST=127.0.0.1",
       "--setenv=STORAGE_MANAGER_TOKEN="+token,
       sys.executable,script,"serve"
    ]
    originally=current.resolve(strict=True)
    try:
        run(*params)
        ready(unit)
        try:
            urllib.request.urlopen(urllib.request.Request(
                f"http://127.0.0.1:{PORT}/v1/cleanup",method="POST"),timeout=2)
            raise RuntimeError("storage cleanup was unexpectedly accessible")
        except urllib.error.HTTPError as exc:
            if exc.code!=403:raise RuntimeError("unexpected cleanup API response")
        changed="e"*40
        if a.revision==changed:changed="d"*40
        fresh_vps.deploy_app(root,APP,changed)
        run("sudo","systemctl","restart",unit)
        ready(unit)
        outcome=fresh_vps.rollback(root,APP)
        if outcome["restored_revision"]!=a.revision or current.resolve()!=originally:
            raise RuntimeError("atomic rollback pointer did not restore original source")
        run("sudo","systemctl","restart",unit)
        ready(unit)
        return {"status":"PASS","service":APP,"real_systemd_activation":True,
                "restart_after_release":True,"rollback_restart":True,
                "destructive_api_blocked":True,"production_modified":False}
    finally:
        subprocess.run(["sudo","systemctl","stop",unit],capture_output=True,text=True)
        subprocess.run(["sudo","systemctl","reset-failed",unit],capture_output=True,text=True)

if __name__=="__main__":
    try:
        a=argparse.ArgumentParser();a.add_argument("--root",required=True);a.add_argument("--revision",required=True);args=a.parse_args()
        print(json.dumps(main(args),indent=2,sort_keys=True))
    except Exception as e:
        print("ISOLATED_SYSTEMD_ROLLBACK_FAILED: "+str(e),file=sys.stderr)
        sys.exit(1)

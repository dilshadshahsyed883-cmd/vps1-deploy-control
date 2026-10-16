#!/usr/bin/env python3
"""Boot known FastAPI apps from staged GitHub source; verify fresh local health."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

APPS=("short-stream","short-maker")

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1",0))
        return s.getsockname()[1]

def smoke(root, app):
    path=Path(root)/"srv/fresh-website/current"/app/"part01"
    if not path.is_dir():
        raise ValueError("staged source missing: "+app)
    port=free_port()
    env=os.environ.copy()
    if app=="short-stream":
        env["SHORT_STREAM_DATA"]=str(Path(root)/"var/lib/fresh-website"/app)
    else:
        env["SHORT_MAKER_DATA"]=str(Path(root)/"var/lib/fresh-website"/app)
    process=subprocess.Popen(
        [sys.executable,"-m","uvicorn","app:app","--host","127.0.0.1","--port",str(port)],
        cwd=path,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    try:
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            if process.poll() is not None:
                raise RuntimeError(app+" startup failed: "+process.stderr.read().decode(errors="replace")[-1500:])
            try:
                with urllib.request.urlopen("http://127.0.0.1:"+str(port)+"/health",timeout=1) as r:
                    data=json.loads(r.read())
                if data.get("ok") is not True or data.get("app")!=app:
                    raise ValueError("health contract mismatch "+app)
                if not str(data.get("data_root","")).startswith(str(Path(root)/"var/lib/fresh-website"/app)):
                    raise ValueError("app escaped fresh isolated data root")
                return {"app":app,"health":"PASS","data_root_isolated":True}
            except (urllib.error.URLError,TimeoutError,ConnectionError):
                time.sleep(0.25)
        raise TimeoutError(app+" did not become healthy")
    finally:
        process.terminate()
        try:process.communicate(timeout=4)
        except subprocess.TimeoutExpired:process.kill();process.communicate()

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--root",required=True)
    args=p.parse_args()
    root=Path(args.root).resolve()
    if not root.is_relative_to(Path("/tmp")) or root==Path("/tmp"):
        raise ValueError("smoke root must be isolated under /tmp")
    result=[smoke(root,app) for app in APPS]
    print(json.dumps({"result":"PASS","services":result,"production_touched":False},indent=2))

if __name__=="__main__":
    try:main()
    except Exception as e:
        print("FAILED:",e,file=sys.stderr)
        sys.exit(1)

#!/usr/bin/env python3
"""Prove Nginx auth and real local service proxy inside isolated Ubuntu rootfs."""
import argparse
import base64
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from rebuild import fresh_vps

SERVICES=[("short-maker",8146),("short-stream",8145)]
NGINX_PORT=18888

def probe(url,username=None,password=None):
    headers={}
    if username is not None:
        encoded=base64.b64encode((username+":"+password).encode()).decode()
        headers["Authorization"]="Basic "+encoded
    req=urllib.request.Request(url,headers=headers)
    try:
        with urllib.request.urlopen(req,timeout=2) as r:
            return r.status,r.read(65536)
    except urllib.error.HTTPError as e:
        return e.code,e.read(2048)

def run(root):
    root=fresh_vps.root_dir(root)
    env_root=root/"etc/fresh-website"
    env_root.mkdir(parents=True,exist_ok=True)
    auth=env_root/"ci-auth.htpasswd"
    password=secrets.token_urlsafe(22)
    subprocess.run(["htpasswd","-bc",str(auth),"ci-test-user",password],
                   check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    # Nginx worker runs as www-data; only root and that group may read hashes.
    import grp
    os.chown(auth,0,grp.getgrnam("www-data").gr_gid)
    auth.chmod(0o640)
    runtime=root/"nginx-smoke"
    runtime.mkdir(parents=True,exist_ok=True)
    log=runtime/"error.log"
    ngconf=runtime/"nginx.conf"
    locations=[]
    processes=[]
    try:
        for app,port in SERVICES:
            cwd=root/"srv/fresh-website/current"/app/"part01"
            env=os.environ.copy()
            env["SHORT_MAKER_DATA" if app=="short-maker" else "SHORT_STREAM_DATA"]=str(root/"var/lib/fresh-website"/app)
            env["PYTHONDONTWRITEBYTECODE"]="1"
            p=subprocess.Popen([sys.executable,"-m","uvicorn","app:app",
                                "--host","127.0.0.1","--port",str(port)],
                               cwd=cwd,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            processes.append(p)
            locations.append(
                "  location /"+app+"/ { proxy_pass http://127.0.0.1:"+str(port)+
                "/; proxy_set_header Host $host; proxy_set_header X-Forwarded-Proto $scheme; }\n")
        config=(
          "worker_processes 1;\n"+
          "pid "+str(runtime/"nginx.pid")+";\n"+
          "error_log "+str(log)+" error;\n"+
          "events { worker_connections 128; }\n"+
          "http {\n  access_log off;\n  server {\n"
          "  listen 127.0.0.1:"+str(NGINX_PORT)+";\n"
          "  server_name localhost;\n"
          "  auth_basic \"Fresh website test\";\n"
          "  auth_basic_user_file "+str(auth)+";\n"
          +"".join(locations)+"  location / { return 404; }\n"
          "  }\n}\n")
        ngconf.write_text(config)
        subprocess.run(["nginx","-t","-c",str(ngconf)],check=True,
                       stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        gateway=subprocess.Popen(["nginx","-c",str(ngconf),"-g","daemon off;"],
                                 stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        processes.append(gateway)
        deadline=time.monotonic()+25
        while time.monotonic()<deadline:
            if any(p.poll() is not None for p in processes):
                raise RuntimeError("test service exited early; "+log.read_text()[-800:] if log.exists() else "process died")
            try:
                result=[]
                for app,_ in SERVICES:
                    url="http://127.0.0.1:"+str(NGINX_PORT)+"/"+app+"/health"
                    unauthorized,_=probe(url)
                    authorized,body=probe(url,"ci-test-user",password)
                    if unauthorized!=401 or authorized!=200:
                        raise ValueError(app+" authentication bypass/route mismatch: "+str((unauthorized,authorized))+(" nginx_log="+log.read_text()[-1400:] if log.exists() else ""))
                    payload=json.loads(body)
                    if payload.get("app")!=app or payload.get("ok") is not True:
                        raise ValueError("wrong upstream health for "+app)
                    result.append({"app":app,"auth_required":True,"health":"PASS"})
                return {"nginx_syntax":"PASS","basic_auth_enforcement":"PASS",
                        "reverse_proxy_apps":result,"nginx_listen":"loopback_only"}
            except (urllib.error.URLError,TimeoutError,ConnectionError):
                time.sleep(0.3)
        raise TimeoutError("protected Nginx routes never became ready")
    finally:
        for p in reversed(processes):
            if p.poll() is None:
                p.terminate()
                try:p.communicate(timeout=4)
                except subprocess.TimeoutExpired:p.kill();p.communicate()

if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--root",required=True)
    args=parser.parse_args()
    try:print(json.dumps(run(args.root),indent=2))
    except Exception as e:
        print("NGINX_INTEGRATION_FAILED: "+str(e),file=sys.stderr)
        sys.exit(1)

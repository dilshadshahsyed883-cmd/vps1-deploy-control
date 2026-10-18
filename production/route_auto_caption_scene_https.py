#!/usr/bin/env python3
"""VPS_1 Scene HTTPS: add ONLY /auto-caption-studio to existing authenticated gateway.

Run exclusively through locked Agent 1's minimal, hash-pinned Drive Bridge V3
carrier after protected GitHub-main CI. Never reinstall apps or change queues.
"""
from __future__ import annotations
import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

DOMAIN="app.scenebuilder.online"
HOST="vps-01a23801"
REPO="https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control.git"
SITE=Path("/etc/nginx/sites-available/vps1-scene-public.conf")
ENABLED=Path("/etc/nginx/sites-enabled/vps1-scene-public.conf")
AUTH=Path("/root/vps1-production-login.json")
TX=Path("/srv/fresh-website/transactions/auto-caption-scene-https")
MARKER="# VPS1_AUTO_CAPTION_HTTPS_ROUTE_V1"
ANCHOR=" location = /internal/runtime-ready { return 404; }\n location / {"

ROUTE = """ # VPS1_AUTO_CAPTION_HTTPS_ROUTE_V1: delegate Basic authentication to existing local Nginx gateway.
 location = /auto-caption-studio { return 308 /auto-caption-studio/; }
 location ^~ /auto-caption-studio/ {
    proxy_pass http://127.0.0.1:8888;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header Authorization $http_authorization;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;
 }
"""

def event(s):
    print("VPS1_ACS_HTTPS: "+s,flush=True)

def guard(flag,problem):
    if not flag:
        raise RuntimeError("BLOCKED: "+problem)

def run(argv,timeout=25):
    p=subprocess.run(argv,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout)
    if p.returncode:
        raise RuntimeError("command failed: "+argv[0]+" exit="+str(p.returncode))
    return p.stdout.decode(errors="replace").strip()

def add_route(text):
    guard(MARKER not in text,"already routed by this release; use verification instead")
    guard("location ^~ /auto-caption-studio/" not in text,
          "unowned Auto Caption route already exists; refusing overwrite")
    guard(text.count(ANCHOR)==1,"existing Scene HTTPS site differs; fail closed")
    guard(text.count("listen 443 ssl;")==1 and
          text.count("server_name app.scenebuilder.online;")==2,
          "domain or HTTPS server structure unexpected")
    return text.replace(ANCHOR,
                        " location = /internal/runtime-ready { return 404; }\n"+ROUTE+" location / {",1)

def atomic(path,data,mode=0o644):
    with tempfile.NamedTemporaryFile(mode="wb",dir=path.parent,
                                     prefix=".acs-route.",delete=False) as tmp:
        name=Path(tmp.name)
        try:
            tmp.write(data)
            tmp.flush()
            os.fsync(tmp.fileno())
        except Exception:
            name.unlink(missing_ok=True)
            raise
    name.chmod(mode)
    os.replace(name,path)

def local_get(url,headers=None):
    req=urllib.request.Request(url,headers=headers or {})
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req,timeout=7) as response:
            return response.status,response.headers.get("Content-Type",""),response.read(65536)
    except urllib.error.HTTPError as err:
        return err.code,err.headers.get("Content-Type",""),err.read(65536)

def tls_get(path,authorization=None):
    # Real certificate validation and SNI without relying on public DNS/proxy.
    context=ssl.create_default_context()
    with socket.create_connection(("127.0.0.1",443),timeout=7) as raw:
        with context.wrap_socket(raw,server_hostname=DOMAIN) as tls:
            payload=("GET "+path+" HTTP/1.1\r\nHost: "+DOMAIN+
                     "\r\nConnection: close\r\nUser-Agent: VPS1-ACS-Cutover/1\r\n")
            if authorization:
                payload+="Authorization: "+authorization+"\r\n"
            tls.sendall((payload+"\r\n").encode())
            response=http.client.HTTPResponse(tls)
            response.begin()
            return response.status,response.getheader("Content-Type",""),response.read(65536)

def credentials():
    guard(AUTH.is_file(),"existing gateway auth record missing")
    raw=json.loads(AUTH.read_text())
    user,password=raw.get("username"),raw.get("password")
    guard(isinstance(user,str) and user and isinstance(password,str) and len(password)>=20,
          "existing gateway auth record incomplete")
    return "Basic "+base64.b64encode((user+":"+password).encode()).decode()

def active(unit):
    return subprocess.run(["systemctl","is-active","--quiet",unit],
                          stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0

def checks(auth,route_installed=False):
    # HTTPS Scene site, Nginx workspace gateway, and ACS must all stay healthy.
    c,_,body=local_get("http://127.0.0.1:8144/health")
    guard(c==200 and json.loads(body).get("ok") is True
          and json.loads(body).get("database_ready") is True,
          "Auto Caption application database/health unavailable")
    for unit in ("nginx.service","vps1-production-auto-caption-studio.service",
                 "vps1-scene-candidate.service"):
        guard(active(unit),unit+" not active")
    for role in ("ingress","render","compress","upload"):
        guard(active("vps1-production-acs-"+role+".service"),
              "ACS "+role+" worker inactive")
    c,_,_=local_get("http://127.0.0.1:8888/auto-caption-studio/health")
    guard(c==401,"anonymous existing gateway health not restricted")
    c,_,body=local_get("http://127.0.0.1:8888/auto-caption-studio/health",
                       {"Authorization":auth})
    guard(c==200 and json.loads(body).get("ok") is True,
          "existing authenticated gateway health unavailable")
    c,ct,body=local_get("http://127.0.0.1:8888/auto-caption-studio/",
                       {"Authorization":auth})
    guard(c==200 and "text/html" in ct.lower() and b"<html" in body.lower(),
          "existing authenticated Auto Caption UI unavailable")
    c,_,body=tls_get("/health")
    guard(c==200 and json.loads(body).get("ok") is True,
          "existing Scene HTTPS health changed")
    c,ct,body=tls_get("/login")
    guard(c==200 and "html" in ct.lower() and b"<html" in body.lower(),
          "existing Scene login regressed")
    c,_,_=tls_get("/internal/runtime-ready")
    guard(c==404,"internal Scene probe unexpectedly public")
    if route_installed:
        c,_,_=tls_get("/auto-caption-studio/health")
        guard(c==401,"unauthenticated public Auto Caption route exposed")
        c,_,body=tls_get("/auto-caption-studio/health",auth)
        guard(c==200 and json.loads(body).get("ok") is True
              and json.loads(body).get("database_ready") is True,
              "authenticated ACS HTTPS health failed")
        c,ct,body=tls_get("/auto-caption-studio/",auth)
        guard(c==200 and "html" in ct.lower() and b"<html" in body.lower(),
              "authenticated ACS HTTPS UI failed")
        c,_,_=tls_get("/auto-caption-studio")
        guard(c==308,"nontrailing ACS path redirect missing")
        event("TLS_CERT=PASS ACS_AUTH_401_200=PASS ACS_HTML=PASS PREFIX_REDIRECT=PASS")
    event("SCENE_LOGIN=PASS ACS_BACKEND_DB=PASS WORKERS_4=ACTIVE GATEWAY_AUTH=PASS")

def preflight(revision):
    guard(os.geteuid()==0,"requires preapproved Agent 1 sudo -n")
    guard(socket.gethostname().split(".")[0]==HOST,"unexpected VPS")
    guard(re.fullmatch("[a-f0-9]{40}",revision) is not None,"invalid GitHub commit")
    guard(shutil.disk_usage("/").free>=5_000_000_000,"5 GB free-disk minimum violated")
    guard(active("master-drive-agent.service"),"Agent 1 not active")
    guard(SITE.is_file() and ENABLED.is_symlink() and ENABLED.resolve()==SITE,
          "existing dedicated Scene HTTPS site not owned/mapped as expected")
    main=run(["git","ls-remote",REPO,"refs/heads/main"],timeout=30).split()[0]
    guard(main==revision,"GitHub source commit not current protected main")
    guard(shutil.which("nginx") is not None,"nginx absent")
    text=SITE.read_text()
    new=add_route(text)
    return text.encode(),new.encode()

def deploy(revision):
    old,new=preflight(revision)
    auth=credentials()
    checks(auth)
    tx=TX/revision
    guard(not tx.exists(),"transaction already exists")
    tx.mkdir(parents=True,mode=0o700)
    before=tx/"site.before.conf"
    before.write_bytes(old)
    before.chmod(0o600)
    changed=False
    try:
        guard(SITE.read_bytes()==old,"site modified concurrently")
        changed=True
        atomic(SITE,new)
        run(["nginx","-t"])
        run(["systemctl","reload","nginx"])
        checks(auth,route_installed=True)
        after_hash=hashlib.sha256(new).hexdigest()
        (tx/"release.json").write_text(json.dumps({
            "source_commit":revision,"old_sha256":hashlib.sha256(old).hexdigest(),
            "new_sha256":after_hash,"site":str(SITE),"path":"/auto-caption-studio/",
            "nginx_gateway_port":8888,"direct_backend_port":8144,
            "mode":"authenticated existing gateway"
        },sort_keys=True)+"\n")
        (tx/"release.json").chmod(0o600)
        event("SOURCE_COMMIT="+revision+" SITE_SHA256="+after_hash)
        event("RESULT=PASS_AUTO_CAPTION_HTTPS_LIVE APP_URL=https://app.scenebuilder.online/auto-caption-studio/")
    except Exception:
        if changed:
            atomic(SITE,old)
            try:
                run(["nginx","-t"])
                run(["systemctl","reload","nginx"])
                event("ROLLBACK=SCENE_HTTPS_SITE_ONLY RESTORED=YES")
            except Exception:
                event("ROLLBACK=FAILED_MANUAL_RECOVERY_REQUIRED")
        raise

def main():
    if len(sys.argv)!=3 or sys.argv[1]!="deploy":
        raise SystemExit("usage: route_auto_caption_scene_https.py deploy SHA")
    try:
        deploy(sys.argv[2])
    except Exception as e:
        event("RESULT=BLOCKED "+str(e).splitlines()[0][:280])
        raise SystemExit(24)

if __name__=="__main__":
    main()

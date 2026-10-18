#!/usr/bin/env python3
"""Scene Builder HTTPS only; source-controlled, root-executed via Agent1 Bridge V3.
Preserves unrelated production gateway and all prior certificate directories.
Requires actual login POST proof before making the domain public.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

HOST="vps-01a23801"
DOMAIN="app.scenebuilder.online"
SITE=Path("/etc/nginx/sites-available/vps1-scene-public.conf")
ENABLED=Path("/etc/nginx/sites-enabled/vps1-scene-public.conf")
CERT=Path("/etc/letsencrypt/live")/DOMAIN/"fullchain.pem"
KEY=Path("/etc/letsencrypt/live")/DOMAIN/"privkey.pem"
WEBROOT=Path("/var/www/vps1-scene-acme")
APP="http://127.0.0.1:18080"
REPO="https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control.git"

def note(v):
    print("VPS1_SCENE_PUBLIC: "+v,flush=True)

def guard(ok,message):
    if not ok:
        raise RuntimeError("BLOCKED: "+message)

def run(cmd,timeout=90):
    x=subprocess.run([str(a) for a in cmd],capture_output=True,text=True,timeout=timeout)
    if x.returncode:
        raise RuntimeError("command failed: "+str(cmd[0])+" rc="+str(x.returncode)+"; output suppressed")
    return x.stdout

def nginx_http_only():
    return """# Scene-only ACME challenge (no other Nginx site changed).
server {
 listen 80;
 server_name app.scenebuilder.online;
 location ^~ /.well-known/acme-challenge/ {
     root /var/www/vps1-scene-acme;
     default_type text/plain;
 }
 location / { return 503; }
}
"""

def nginx_https():
    return """# Dedicated Scene Builder public HTTPS; preserves 6-app default gateway.
server {
 listen 80;
 server_name app.scenebuilder.online;
 location ^~ /.well-known/acme-challenge/ {
     root /var/www/vps1-scene-acme;
     default_type text/plain;
 }
 location / { return 308 https://$host$request_uri; }
}
server {
 listen 443 ssl;
 server_name app.scenebuilder.online;
 ssl_certificate /etc/letsencrypt/live/app.scenebuilder.online/fullchain.pem;
 ssl_certificate_key /etc/letsencrypt/live/app.scenebuilder.online/privkey.pem;
 ssl_protocols TLSv1.2 TLSv1.3;
 client_max_body_size 2048m;
 proxy_read_timeout 300s;
 proxy_send_timeout 300s;
 location = /internal/runtime-ready { return 404; }
 location / {
    proxy_pass http://127.0.0.1:18080;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
 }
}
"""

def live_login_ready():
    with urllib.request.urlopen(APP+"/login",timeout=5) as response:
        guard(response.status==200,"/login unavailable")
    # Admin secret is used only in memory. Do not print its value or session cookie.
    env=Path("/etc/fresh-website/scene-builder.env")
    guard(env.is_file(),"local admin credential/env absent")
    settings=dict(line.split("=",1) for line in env.read_text().splitlines() if "=" in line and not line.startswith("#"))
    guard(settings.get("VPS1_SCENE_PRECUTOVER_EDITING")=="1","reviewed app login mode not enabled")
    username=settings.get("WEB_USERNAME","team")
    secret=settings.get("WEB_ACCESS_KEY","")
    guard(len(secret)>=24,"admin bootstrap credential missing")
    from urllib.parse import urlencode
    body=urlencode({"username":username,"secret_key":secret}).encode()
    req=urllib.request.Request(APP+"/login",data=body,method="POST")
    try:
        with urllib.request.urlopen(req,timeout=6) as response:
            status=response.status
            headers=response.headers
    except urllib.error.HTTPError as exc:
        status=exc.code;headers=exc.headers
    # urllib follows 303 / --> app login; use no-redirect for accurate status.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):
            return None
    opener=urllib.request.build_opener(NoRedirect)
    try:
        opener.open(req,timeout=6)
        guard(False,"login returned no redirect")
    except urllib.error.HTTPError as e:
        guard(e.code==303 and "session=" in e.headers.get("Set-Cookie",""),"login session not verified")
    note("SCENE_OWNER_LOGIN=PASS")

def preflight(revision):
    guard(os.geteuid()==0,"preexisting sudo -n required")
    guard(socket.gethostname().split(".")[0]==HOST,"VPS host mismatch")
    guard(re.fullmatch("[a-f0-9]{40}",revision) is not None,"invalid pinned commit")
    guard(not SITE.exists() and not ENABLED.exists(),"existing Scene public site requires separate upgrade/rollback")
    guard(shutil.disk_usage("/").free>=5_000_000_000,"5GB disk reserve not available")
    guard(shutil.which("nginx") and shutil.which("curl"),"nginx/curl missing")
    ips={v[4][0] for v in socket.getaddrinfo(DOMAIN,None,family=socket.AF_INET)}
    guard(ips=={"137.74.162.143"},"DNS not exclusively routed to current VPS")
    current=run(["git","ls-remote",REPO,"refs/heads/main"],timeout=25).split()[0]
    guard(revision==current,"requested SHA not current protected main")
    for unit in ("vps1-scene-candidate.service","vps1-storage-candidate.service","nginx.service"):
        guard(subprocess.run(["systemctl","is-active","--quiet",unit]).returncode==0,unit+" not active")
    with urllib.request.urlopen(APP+"/health",timeout=5) as r:
        guard(r.status==200 and json.load(r).get("ok") is True,"Scene health failed")
    live_login_ready()
    guard(not KEY.exists() or CERT.exists(),"incomplete existing certificate")
    note("PREFLIGHT=PASS DNS=137.74.162.143 SCENE_LOGIN=PASS")

def install(revision):
    preflight(revision)
    created=False
    try:
        WEBROOT.joinpath(".well-known","acme-challenge").mkdir(parents=True,exist_ok=True)
        SITE.write_text(nginx_http_only())
        SITE.chmod(0o644)
        ENABLED.symlink_to(SITE)
        created=True
        run(["nginx","-t"],timeout=10)
        run(["systemctl","reload","nginx"],timeout=15)
        if not (CERT.is_file() and KEY.is_file()):
            if not shutil.which("certbot"):
                env=dict(os.environ,DEBIAN_FRONTEND="noninteractive")
                x=subprocess.run(["apt-get","-o","DPkg::Lock::Timeout=180","update","-qq"],
                    env=env,capture_output=True,timeout=240)
                guard(x.returncode==0,"certbot package index failed")
                x=subprocess.run(["apt-get","-o","DPkg::Lock::Timeout=180","install","-y","--no-install-recommends","certbot"],
                    env=env,capture_output=True,timeout=300)
                guard(x.returncode==0,"certbot install failed")
            run(["certbot","certonly","--webroot","-w",str(WEBROOT),
                 "-d",DOMAIN,"--non-interactive","--agree-tos",
                 "--register-unsafely-without-email","--preferred-challenges","http"],timeout=240)
        guard(CERT.is_file() and KEY.is_file(),"certificate missing after issue")
        SITE.write_text(nginx_https())
        run(["nginx","-t"],timeout=10)
        run(["systemctl","reload","nginx"],timeout=15)
        # Curl validates signed TLS certificate and expected Host despite 127.0.0.1 socket.
        health=run(["curl","-fsS","--connect-timeout","6","--max-time","18",
          "--resolve",DOMAIN+":443:127.0.0.1","https://"+DOMAIN+"/health"],timeout=23)
        guard(json.loads(health).get("ok") is True,"HTTPS health mismatch")
        login=run(["curl","-fsS","--connect-timeout","6","--max-time","15",
          "--resolve",DOMAIN+":443:127.0.0.1","https://"+DOMAIN+"/login"],timeout=22)
        guard("<html" in login.lower() or "<!doctype" in login.lower(),"HTTPS login not HTML")
        marker=run(["curl","-sS","-o","/dev/null","-w","%{http_code}",
          "--resolve",DOMAIN+":443:127.0.0.1","https://"+DOMAIN+"/internal/runtime-ready"],timeout=18)
        guard(marker.strip()=="404","internal runtime token endpoint exposed")
        # Old six-app gateway should remain protected and reachable on loopback.
        p=subprocess.run(["curl","-sS","-o","/dev/null","-w","%{http_code}",
             "--connect-timeout","4","http://127.0.0.1:8888/"],capture_output=True,text=True,timeout=12)
        guard(p.returncode==0 and p.stdout.strip()=="401","six-app gateway auth regression")
        note("HTTPS_CERT=PASS DOMAIN_ROUTE=PASS LOGIN_HTML=PASS INTERNAL_ENDPOINT=DENIED EXISTING_GATEWAY=PASS")
        note("RESULT=PASS_DOMAIN_SCENE_PUBLIC_HTTPS PORT=443")
    except Exception:
        if created:
            if ENABLED.is_symlink():ENABLED.unlink()
            if SITE.exists():SITE.unlink()
            subprocess.run(["nginx","-t"],capture_output=True)
            subprocess.run(["systemctl","reload","nginx"],capture_output=True)
            note("ROLLBACK=SCENE_SITE_ONLY_EXISTING_GATEWAY_PRESERVED")
        raise


# Scoped Voice HTTPS publication called only after Voice Studio's shared gateway
# release has passed all seven existing app checks.
VOICE_ANCHOR = ' location / {\n    proxy_pass http://127.0.0.1:18080;'
VOICE_EXTRA = """ location = /voice-studio { return 302 /voice-studio/; }
 location ^~ /voice-studio/ {
    proxy_pass http://127.0.0.1:8888;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;
 }
"""

def publish_voice_on_scene_https():
    """Append only the authenticated Voice path; rollback precisely on failure."""
    import base64
    import http.client
    import ssl
    from production.gateway import LOGIN as WORKSPACE_LOGIN
    from production.runtime import BASE as PROD_BASE

    guard(os.geteuid() == 0 and socket.gethostname().split('.')[0] == HOST,
          "protected VPS_1 root required")
    guard(SITE.is_file() and ENABLED.is_symlink() and ENABLED.resolve() == SITE,
          "existing Scene HTTPS site mismatch")
    guard(CERT.is_file() and KEY.is_file(), "existing signed TLS certificate absent")
    state=json.loads((PROD_BASE/'production-status.json').read_text())
    required={'short-maker','short-stream','self-media-library','video-merger',
              'auto-caption-studio','local-upload-manager','voice-studio'}
    guard(state.get('gateway')=='ACTIVE' and required.issubset(state.get('apps',[])),
          "seven-app gateway is not ready")
    for service in ('nginx.service','vps1-production-voice-studio.service',
                    'vps1-scene-candidate.service'):
        guard(subprocess.run(['systemctl','is-active','--quiet',service]).returncode==0,
              service+" is not active")
    guard(WORKSPACE_LOGIN.is_file(), "existing gateway credentials unavailable")
    creds=json.loads(WORKSPACE_LOGIN.read_text())
    guard(len(creds.get('username',''))>0 and len(creds.get('password',''))>0,
          "invalid gateway credentials")
    auth='Basic '+base64.b64encode(
        (creds['username']+':'+creds['password']).encode()).decode()
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def http_gateway(path, authenticated=False):
        headers={'Authorization':auth} if authenticated else {}
        req=urllib.request.Request('http://127.0.0.1:8888'+path,headers=headers)
        try:
            with opener.open(req,timeout=10) as r:return r.status,r.read(260000)
        except urllib.error.HTTPError as e:return e.code,b''
    def https_host(path, authenticated=False):
        # Correct SNI/certificate verification without public DNS/network dependence.
        raw=socket.create_connection(('127.0.0.1',443),timeout=10)
        tls=ssl.create_default_context().wrap_socket(raw,server_hostname=DOMAIN)
        try:
            conn=http.client.HTTPSConnection(DOMAIN)
            conn.sock=tls
            headers={'Host':DOMAIN}
            if authenticated:headers['Authorization']=auth
            conn.request('GET',path,headers=headers)
            response=conn.getresponse()
            return response.status,response.read(260000)
        finally:tls.close()
    status,_=http_gateway('/voice-studio/healthz')
    guard(status==401,"gateway anonymous Voice auth is not enforced")
    status,body=http_gateway('/voice-studio/healthz',True)
    guard(status==200 and json.loads(body).get('ok') is True,
          "authenticated Voice gateway is not healthy")
    status,_=https_host('/login')
    guard(status==200,"existing Scene HTTPS login is not available")
    original=SITE.read_text()
    guard(original.count(VOICE_ANCHOR)==1,"unrecognized Scene TLS proxy location")
    guard(original.count('server_name app.scenebuilder.online;')==2 and
          'listen 443 ssl;' in original, "unexpected Scene HTTPS site config")
    guard('location ^~ /voice-studio/' not in original or VOICE_EXTRA in original,
          "Voice route owned by unknown config")
    changed=VOICE_EXTRA not in original
    def swap(text):
        with tempfile.NamedTemporaryFile('w',dir=str(SITE.parent),prefix='.voice-web-',
                                         delete=False) as f:
            tmp=Path(f.name)
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        try:
            tmp.chmod(0o644)
            os.replace(tmp,SITE)
        finally:tmp.unlink(missing_ok=True)
    try:
        if changed:
            swap(original.replace(VOICE_ANCHOR,VOICE_EXTRA+VOICE_ANCHOR,1))
            run(['nginx','-t'],timeout=15)
            run(['systemctl','reload','nginx'],timeout=18)
        status,_=https_host('/voice-studio/healthz')
        guard(status==401,"HTTPS Voice anonymous protection failed")
        status,body=https_host('/voice-studio/healthz',True)
        guard(status==200 and json.loads(body).get('ok') is True,"HTTPS Voice health failed")
        status,body=https_host('/voice-studio/',True)
        guard(status==200 and (b'<html' in body.lower() or b'<!doctype' in body.lower()),
              "HTTPS Voice UI HTML failed")
        status,_=https_host('/login')
        guard(status==200,"Scene login regression")
        status,_=https_host('/internal/runtime-ready')
        guard(status==404,"Scene internal endpoint exposed")
        for route in ['/short-maker/health','/auto-caption-studio/health',
                      '/local-upload-manager/health']:
            status,_=http_gateway(route)
            guard(status==401,"existing gateway auth regression")
        note('VOICE_HTTPS=LIVE URL=https://app.scenebuilder.online/voice-studio/')
        note('VOICE_HTTPS=AUTH_401_AND_UI_HEALTH_200_PASS; SCENE_AND_GATEWAY_PRESERVED')
    except BaseException:
        if changed:
            swap(original)
            subprocess.run(['nginx','-t'],capture_output=True,timeout=12)
            subprocess.run(['systemctl','reload','nginx'],capture_output=True,timeout=18)
            note('VOICE_HTTPS=ROLLED_BACK_PREVIOUS_SCENE_SITE_RESTORED')
        raise


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("action",choices=["deploy"])
    parser.add_argument("revision")
    a=parser.parse_args()
    try:install(a.revision)
    except Exception as exc:
        note("RESULT=BLOCKED "+str(exc).splitlines()[0][:230])
        raise SystemExit(24)

if __name__=="__main__":main()

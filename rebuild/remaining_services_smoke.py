#!/usr/bin/env python3
"""Additional real service boots on clean Ubuntu, without production side effects."""
import argparse,base64,grp,json,os,secrets,subprocess,sys,time
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import URLError,HTTPError
from rebuild import fresh_vps

SERVICES=[
 ("voice-studio",18431,"/healthz","uvicorn","vps1_omnivoice_app:app"),
 ("local-upload-manager",18432,"/LUM/health","uvicorn","lum_engine.standalone_app:app"),
 ("video-merger",18433,"/video-merger/health","script","app.py"),
 ("vps-storage-manager",18434,"/health","script","storage_manager.py")
]
def hit(url,login=None,method="GET"):
    headers={}
    if login:headers["Authorization"]="Basic "+base64.b64encode(login.encode()).decode()
    try:
        with urlopen(Request(url,headers=headers,method=method),timeout=2) as r:
            return r.status,r.read()
    except HTTPError as e:return e.code,e.read()
def config(root,group,port):
    base=root/"var/lib/fresh-website"
    env=os.environ.copy()
    env.update({"FRESH_WEBSITE_SMOKE_TEST":"1","FRESH_WEBSITE_INSTALL":"1",
                "PYTHONDONTWRITEBYTECODE":"1"})
    if group=="voice-studio":
        for key,folder in (("VOICE_CLONE_ROOT","omnivoice"),
                           ("KOKORO_STUDIO_ROOT","kokoro"),
                           ("EDGE_STUDIO_ROOT","edge"),
                           ("AUDIO_DOWNLOADER_ROOT","audio-downloader")):
            env[key]=str(base/"voice-studio"/folder)
    if group=="local-upload-manager":
        root_data=base/group/"lum1"
        env.update({"LUM_DATA_DIR":str(root_data),
                    "YOUTUBE_UPLOAD_MANAGER_DATA_DIR":str(root_data/"core"),
                    "LUM_SESSION_SECRET":"ci-"+secrets.token_hex(32),
                    "LUM_BASE_PATH":"/LUM","LUM_SUITE_AUTH":"0"})
    if group=="video-merger":
        d=base/group/"video-merger"
        env.update({"VIDEO_MERGER_ROOT":str(d),
                    "VIDEO_MERGER_PROXY_CONFIG_DIR":str(d/"config"),
                    "VIDEO_MERGER_PORT":str(port)})
    if group=="vps-storage-manager":
        env.update({"STORAGE_MANAGER_STATE_ROOT":str(base/group/"storage"),
                    "STORAGE_MANAGER_TOKEN":"ci-"+secrets.token_hex(32),
                    "STORAGE_MANAGER_HOST":"127.0.0.1",
                    "STORAGE_MANAGER_PORT":str(port)})
    return env
def run(root_arg):
    root=fresh_vps.root_dir(root_arg)
    data=root/"nginx-extra";data.mkdir(parents=True,exist_ok=True)
    ht=root/"etc/fresh-website/ci-extra.htpasswd"
    password=secrets.token_urlsafe(28)
    subprocess.run(["htpasswd","-bc",str(ht),"fresh-check",password],
                   stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,check=True)
    os.chown(ht,0,grp.getgrnam("www-data").gr_gid);ht.chmod(0o640)
    active=[];names=[];locations=[]
    try:
        for name,port,route,kind,entry in SERVICES:
            wd=root/"srv/fresh-website/current"/name/"part01"
            if not wd.is_dir():raise ValueError("source unavailable "+name)
            cmd=([sys.executable,"-m","uvicorn",entry,"--host","127.0.0.1","--port",str(port)]
                 if kind=="uvicorn" else [sys.executable,entry])
            p=subprocess.Popen(cmd,cwd=wd,env=config(root,name,port),
                               stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            active.append(p);names.append(name)
            locations.append("location /"+name+"/ { proxy_pass http://127.0.0.1:"+str(port)+"/; }\n")
        nginx=data/"nginx.conf"
        nginx.write_text("user www-data;\nworker_processes 1;\npid "+str(data/"pid")+";\n"
             "error_log "+str(data/"error.log")+" error;\nevents {worker_connections 128;}\n"
             "http { access_log off; server {listen 127.0.0.1:18889;\n"
             "auth_basic \"Smoke Test\";\nauth_basic_user_file "+str(ht)+";\n"+
             "".join(locations)+"location / {return 404;}\n}}\n")
        subprocess.run(["nginx","-t","-c",str(nginx)],check=True,capture_output=True)
        np=subprocess.Popen(["nginx","-c",str(nginx),"-g","daemon off;"],
                            stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        active.append(np);names.append("nginx")
        finished={}
        end=time.monotonic()+40
        while time.monotonic()<end:
            for p,name in zip(active,names):
                if p.poll() is not None:
                    err=p.stderr.read().decode(errors="replace")[-2000:]
                    raise RuntimeError(name+" exited "+str(p.returncode)+": "+err)
            for name,port,route,kind,entry in SERVICES:
                if name in finished:continue
                url="http://127.0.0.1:18889/"+name+route
                try:
                    anonymous,_=hit(url)
                    if anonymous!=401:raise RuntimeError("unauthenticated API exposed "+name)
                    status,body=hit(url,"fresh-check:"+password)
                    if status in (502,503,504):continue
                    if status!=200:raise RuntimeError(name+" returned "+str(status)+" "+body[-350:].decode(errors="replace"))
                    payload=json.loads(body)
                    if payload.get("ok") is not True:raise RuntimeError("unhealthy "+name)
                    if name=="voice-studio" and int(payload.get("jobs",-1))!=0:
                        raise RuntimeError("nonempty voice queue")
                    if name=="vps-storage-manager":
                        denied,_=hit("http://127.0.0.1:"+str(port)+"/v1/metrics")
                        post,_=hit("http://127.0.0.1:"+str(port)+"/v1/cleanup",method="POST")
                        if denied!=403 or post!=403:raise RuntimeError("unsafe storage privileges")
                    finished[name]={"authenticated_health":"PASS","isolated_data":True}
                except (URLError,TimeoutError,ConnectionError):
                    continue
            if len(finished)==len(SERVICES):
                return {"result":"PASS","services":finished,"production_modified":False,
                        "external_jobs_executed":False}
            time.sleep(.25)
        raise TimeoutError("failed health: "+",".join(sorted(set(s[0] for s in SERVICES)-set(finished))))
    finally:
        for p in reversed(active):
            if p.poll() is None:
                p.terminate()
                try:p.communicate(timeout=4)
                except subprocess.TimeoutExpired:p.kill();p.communicate()
def main():
    parser=argparse.ArgumentParser();parser.add_argument("--root",required=True)
    try:print(json.dumps(run(parser.parse_args().root),indent=2))
    except Exception as exc:
        print("REMAINING_STARTUP_FAILED: "+str(exc),file=sys.stderr)
        sys.exit(1)
if __name__=="__main__":main()

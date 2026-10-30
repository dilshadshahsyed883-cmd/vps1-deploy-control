#!/usr/bin/env python3
"""Isolated rootfs release builder for the imported VPS_1 website source.

Never touches the host's /etc, /opt, /var, systemd or nginx. All destination
paths MUST be under /tmp. Production cutover is a separate authorization.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from rebuild import builder, website_shell

ROOT=Path(__file__).resolve().parents[1]
PROFILE_PATH=ROOT/"rebuild/install-profiles.json"
SKIP=builder.BANNED_SEGMENTS | {".acs_backups",".script-work-rollbacks",".pytest_cache","archives","archive",".history"}
SHELL_DENY=re.compile(r"reset|wipe|production.clean|cleanup|purge|destroy|delete",re.I)
EXT={".py",".js",".css",".html",".txt",".json",".md",".sh",".sql",".toml",".ini",".cfg",".yaml",".yml",".svg"}

def root_dir(raw):
    p=Path(raw)
    if not p.is_absolute(): raise ValueError("absolute root path required")
    if p.is_symlink(): raise ValueError("symlink root prohibited")
    p=p.resolve()
    if p==Path("/tmp") or not p.is_relative_to(Path("/tmp")):
        raise ValueError("builder is restricted to an isolated /tmp rootfs")
    return p

def registry():
    p=json.loads(PROFILE_PATH.read_text())
    groups=builder.config()["groups"]
    assert p["version"]==1
    assert set(p["groups"])=={x["id"] for x in groups}
    assert p["groups"]["auto-script"]["parallel_owner"] is True
    assert p["groups"]["auto-script"]["deployment_mode"] == "offline"
    used=set()
    for app,group in p["groups"].items():
        if not re.fullmatch(r"[a-z][a-z0-9-]+",app):raise ValueError("unsafe app")
        for service in group.get("services",[]):
            part=service["part"]
            if not re.fullmatch(r"part\d{2}(/web_backend)?",part):raise ValueError("unsafe part")
            port=service["port"]
            if not isinstance(port,int) or not 1024<=port<=65535 or port in used:
                raise ValueError("conflicting service port")
            used.add(port)
            kind=service.get("kind","asgi")
            if kind=="script":
                safe={"video-merger":("app.py",[]),"vps-storage-manager":("storage_manager.py",["serve"])}
                if app not in safe or (service.get("entry"),service.get("args",[]))!=safe[app]:
                    raise ValueError("unapproved standalone service")
            elif kind=="asgi":
                if not re.fullmatch(r"[a-zA-Z][\w.]*(?<!\.):[a-zA-Z][\w]*",service["module"]):
                    raise ValueError("unsafe ASGI target")
            else:
                raise ValueError("unknown service kind")
    return p

def managed_groups():
    return [x["id"] for x in builder.config()["groups"] if not x["parallel_owner"] and x.get("deployment_mode") != "offline"]

def sources(app):
    # Copyright Studio is a new independent production app, deliberately not
    # added to the historical fixed 11-group rebuild inventory.
    if app != "copyright-uploader" and app not in managed_groups():
        raise ValueError("parallel-owned or unknown source")
    src=ROOT/"apps"/app
    if not src.is_dir():raise ValueError("missing source "+app)
    out=[]
    for path in sorted(src.rglob("*")):
        rel=path.relative_to(src)
        # A Python *source package* named db must not be confused with a
        # mutable SQLite database directory. Only the exact LUM repository
        # code package is exempt; actual .db/.sqlite files are never imported.
        lum_db_code = (
            app in {"local-upload-manager", "copyright-uploader"}
            and len(rel.parts) >= 5
            and rel.parts[0] in ("part01", "part02", "part03")
            and rel.parts[1:4] == ("desktop_core", "modules", "db")
            and path.suffix.lower() == ".py"
        )
        banned = {segment.lower() for segment in rel.parts if segment.lower() in SKIP}
        if "algrow" in str(rel).lower() or (banned and not (lum_db_code and banned == {"db"})):
            continue
        if path.is_symlink():raise ValueError("source symlink "+str(rel))
        if not path.is_file() or path.suffix.lower() not in EXT:continue
        if path.suffix==".sh" and SHELL_DENY.search(path.name):continue
        if builder.DENY_FILENAME.search(path.name) and not builder._is_lum_sensitive_named_python_helper(src,rel,path):
            raise ValueError("secret filename "+str(rel))
        if path.stat().st_size>4_000_000:raise ValueError("oversize source")
        data=path.read_bytes()
        if b"\0" in data or builder.embedded_credential(data,path):
            raise ValueError("secret or binary source "+str(rel))
        out.append((str(rel),data))
    if not out:raise ValueError("source group empty "+app)
    return out

def atomic_link(path,target):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name("."+path.name+".next")
    if temp.exists() or temp.is_symlink():temp.unlink()
    temp.symlink_to(target)
    os.replace(temp,path)

def deploy_app(root,app,sha):
    if app not in managed_groups() or not re.fullmatch(r"[0-9a-f]{12,40}",sha):
        raise ValueError("unknown app or invalid source revision")
    base=root/"srv/fresh-website"
    dst=base/"releases"/app/sha
    files=sources(app)
    manifest={"app":app,"revision":sha,"files":{name:hashlib.sha256(data).hexdigest() for name,data in files}}
    if dst.exists():
        if json.loads((dst/"_fresh_manifest.json").read_text())!=manifest:
            raise ValueError("immutable release version conflict")
    else:
        dst.mkdir(parents=True)
        for name,data in files:
            p=dst/name;p.parent.mkdir(parents=True,exist_ok=True)
            p.write_bytes(data);p.chmod(0o644)
        (dst/"_fresh_manifest.json").write_text(json.dumps(manifest,sort_keys=True)+"\n")
    current=base/"current"/app
    appdata=root/"var/lib/fresh-website"/app
    appdata.mkdir(parents=True,exist_ok=True)
    for name in ("db","queue","uploads","work"):
        (appdata/name).mkdir(exist_ok=True)
    previous=appdata/"previous.json"
    if current.is_symlink():
        old=current.resolve(strict=True)
        if not old.is_relative_to(base/"releases"/app):
            raise ValueError("invalid preexisting release")
        if old!=dst:previous.write_text(json.dumps({"revision":old.name})+"\n")
    elif current.exists():raise ValueError("refusing to overwrite existing application path")
    atomic_link(current,dst)
    return {"app":app,"files":len(files),"release":sha}

def service_unit(app,service):
    part=service["part"];port=service["port"]
    if service.get("kind","asgi") == "script":
        exec_command=" ".join(["/srv/fresh-website/venvs/"+app+"/bin/python",service["entry"],*service.get("args",[])])
    else:
        exec_command="/srv/fresh-website/venvs/"+app+"/bin/python -m uvicorn "+service["module"]+" --host 127.0.0.1 --port "+str(port)
    return f"""[Unit]
Description=Fresh Website {app}
After=network-online.target
ConditionPathExists=/etc/fresh-website/enabled/{app}

[Service]
User=freshweb
Group=freshweb
WorkingDirectory=/srv/fresh-website/current/{app}/{part}
EnvironmentFile=/etc/fresh-website/{app}.env
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart={exec_command}
Restart=on-failure
NoNewPrivileges=true
ProtectSystem=strict
PrivateTmp=true
ReadWritePaths=/var/lib/fresh-website/{app}

[Install]
WantedBy=multi-user.target
"""

def render(root):
    p=registry()
    units=root/"etc/systemd/system"
    env=root/"etc/fresh-website"
    nginx=root/"etc/nginx/sites-available"
    for path in (units,env/"enabled",nginx):path.mkdir(parents=True,exist_ok=True)
    routes=[]
    blockers={}
    for app,item in p["groups"].items():
        if item.get("deployment_mode") == "offline":continue
        templates={
          "scene-builder": "FRESH_WEBSITE_INSTALL=1\nFRESH_AUTO_SCRIPT_READY=0\nWEB_DATA_DIR=/var/lib/fresh-website/scene-builder/scene\nMASTER_CONTROLLER_DB_PATH=/var/lib/fresh-website/scene-builder/scene/master_controller.sqlite3\nSCENE_PERMANENT_ROOT=/var/lib/fresh-website/scene-builder/scene/permanent\nSCRIPT_WORK_DATA_DIR=/var/lib/fresh-website/scene-builder/scene/script-work\n",
          "auto-caption-studio": "FRESH_WEBSITE_INSTALL=1\nAUTO_CAPTION_APP_ROOT=/var/lib/fresh-website/auto-caption-studio/acs\nAUTO_CAPTION_DB_PATH=/var/lib/fresh-website/auto-caption-studio/acs/data/database/auto_caption.db\n",
          "local-upload-manager": "LUM_DATA_DIR=/var/lib/fresh-website/local-upload-manager/lum1\nYOUTUBE_UPLOAD_MANAGER_DATA_DIR=/var/lib/fresh-website/local-upload-manager/lum1/core\n",
          "voice-studio": "VOICE_CLONE_ROOT=/var/lib/fresh-website/voice-studio/omnivoice\nKOKORO_STUDIO_ROOT=/var/lib/fresh-website/voice-studio/kokoro\nEDGE_STUDIO_ROOT=/var/lib/fresh-website/voice-studio/edge\nAUDIO_DOWNLOADER_ROOT=/var/lib/fresh-website/voice-studio/audio-downloader\n",
          "short-stream": "SHORT_STREAM_DATA=/var/lib/fresh-website/short-stream\n",
          "short-maker": "SHORT_MAKER_DATA=/var/lib/fresh-website/short-maker\n",
          "self-media-library": "SML_DATA_ROOT=/var/lib/fresh-website/self-media-library/sml\n",
          "script-work": "SCRIPT_WORK_DATA_DIR=/var/lib/fresh-website/scene-builder/script-work\n",
          "video-merger": "VIDEO_MERGER_ROOT=/var/lib/fresh-website/video-merger/video-merger\nVIDEO_MERGER_PROXY_CONFIG_DIR=/var/lib/fresh-website/video-merger/video-merger/config\nVIDEO_MERGER_PORT=8148\n",
          "vps-storage-manager": "STORAGE_MANAGER_STATE_ROOT=/var/lib/fresh-website/vps-storage-manager/storage\nSTORAGE_MANAGER_HOST=127.0.0.1\nSTORAGE_MANAGER_PORT=8149\n"
        }
        (env/(app+".env.example")).write_text(
            "# Values for a separately provisioned chmod-600 .env file; no secrets in Git\n"
            f"FRESH_APP_DATA=/var/lib/fresh-website/{app}\n"
            + templates.get(app,""))
        services=item.get("services",[])
        if not services: blockers[app]=item.get("pending","startup contract not confirmed")
        for service in services:
            part=service["part"]
            path=root/"srv/fresh-website/current"/app/part
            module=service["entry"] if service.get("kind")=="script" else service["module"].split(":")[0].replace(".","/")+".py"
            if not (path/module).is_file():
                raise ValueError("service entrypoint missing: "+app+"/"+module)
            (units/("fresh-"+app+".service")).write_text(service_unit(app,service))
            suffix="" if app=="video-merger" else "/"
            routes.append(f"    location /{app}/ {{ proxy_pass http://127.0.0.1:{service['port']}{suffix}; }}\n")
            if not (path/service.get("requirements","requirements.txt")).is_file():
                blockers[app]="runtime Python dependency lock not supplied"
            if item.get("pending"):blockers[app]=item["pending"]
    shell = website_shell.build(root, builder.config(), p)
    (nginx/"fresh-website.conf").write_text(
        "# CANDIDATE ONLY: never enabled automatically; operator must create auth file\n"
        "server {\n    listen 127.0.0.1:8888;\n    server_name _;\n"
        "    auth_basic \"Fresh Website - Authentication Required\";\n"
        "    auth_basic_user_file /etc/fresh-website/auth.htpasswd;\n"
        "    location = / { alias /srv/fresh-website/shell/index.html; }\n"
        "    location ^~ /shell-assets/ { alias /srv/fresh-website/shell/assets/; }\n"
        + "".join(routes)+"}\n")
    return {"service_unit_candidates":len(list(units.glob("fresh-*.service"))),
            "enabled_services":0,"nginx_enabled":False,"runtime_blockers":blockers,
            "website_shell":shell}

def assemble(raw,sha):
    root=root_dir(raw);root.mkdir(parents=True,exist_ok=True)
    apps=[deploy_app(root,a,sha) for a in managed_groups()]
    rendered=render(root)
    state={"status":"ISOLATED_RELEASE_STAGED","apps":apps,"configuration":rendered,
           "cutover":"DISABLED","database_state":"EMPTY_DIRECTORIES_NO_SCHEMA_IMPORTED",
           "auto_script":"PARALLEL_BRANCH_PENDING"}
    report=root/"srv/fresh-website/installation-report.json"
    report.write_text(json.dumps(state,sort_keys=True,indent=2)+"\n")
    return state

def verify(raw):
    root=root_dir(raw);base=root/"srv/fresh-website"
    count=0
    for app in managed_groups():
        current=base/"current"/app
        if not current.is_symlink():raise ValueError("missing version pointer")
        path=current.resolve(strict=True)
        if not path.is_relative_to(base/"releases"/app):raise ValueError("invalid symlink")
        manifest=json.loads((path/"_fresh_manifest.json").read_text())
        if manifest["app"]!=app:raise ValueError("wrong manifest")
        for name,sha in manifest["files"].items():
            f=path/name
            if f.is_symlink() or not f.is_file() or hashlib.sha256(f.read_bytes()).hexdigest()!=sha:
                raise ValueError("source integrity failure "+name)
            count+=1
    if list((root/"etc/fresh-website/enabled").iterdir()):
        raise ValueError("live service unexpectedly enabled")
    return {"result":"PASS","groups":len(managed_groups()),"sha256_verified_files":count,
            "production_services_modified":False}

def rollback(raw,app):
    if app not in managed_groups():raise ValueError("invalid rollback target")
    root=root_dir(raw);base=root/"srv/fresh-website"
    marker=root/"var/lib/fresh-website"/app/"previous.json"
    if not marker.is_file():raise ValueError("previous version not available")
    sha=json.loads(marker.read_text())["revision"]
    if not re.fullmatch(r"[a-f0-9]{12,40}",sha):raise ValueError("unsafe stored release")
    prev=base/"releases"/app/sha
    if not (prev/"_fresh_manifest.json").is_file():raise ValueError("rollback source missing")
    current=base/"current"/app
    old=current.resolve(strict=True)
    if not old.is_relative_to(base/"releases"/app):raise ValueError("unsafe release")
    atomic_link(current,prev)
    marker.write_text(json.dumps({"revision":old.name})+"\n")
    return {"app":app,"restored_revision":sha,"host_services_touched":False}

def main():
    p=argparse.ArgumentParser()
    p.add_argument("action",choices=["plan","assemble","verify","rollback"])
    p.add_argument("--root");p.add_argument("--sha");p.add_argument("--app")
    a=p.parse_args()
    if a.action=="plan":result={"groups":managed_groups(),"profiles":registry(),"production_enabled":False}
    elif a.action=="assemble":
        if not a.root or not a.sha:p.error("assemble requires --root and --sha")
        result=assemble(a.root,a.sha)
    elif a.action=="verify":
        if not a.root:p.error("verify requires --root")
        result=verify(a.root)
    else:
        if not a.root or not a.app:p.error("rollback requires --root and --app")
        result=rollback(a.root,a.app)
    print(json.dumps(result,indent=2,sort_keys=True))

if __name__=="__main__":
    try:main()
    except (ValueError,KeyError,OSError,json.JSONDecodeError,AssertionError) as e:
        print("BLOCKED:",e,file=sys.stderr);sys.exit(1)

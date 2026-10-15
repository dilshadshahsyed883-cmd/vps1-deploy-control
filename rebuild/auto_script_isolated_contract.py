#!/usr/bin/env python3
"""Isolated Auto Script source/auth/UI/empty-database contract without scheduler."""
from __future__ import annotations
import argparse,base64,hashlib,importlib,json,sqlite3,sys
from pathlib import Path
from rebuild import fresh_vps

def verify(root_arg):
    root=fresh_vps.root_dir(root_arg)
    auto=Path(__file__).resolve().parents[1]/"apps/auto-script"
    checks=json.loads((auto/"SOURCE_SHA256.json").read_text())
    digests={x["path"]:x["sha256"] for x in checks["files"]}
    required=("auto_script.py","templates/auto_script.html","static/auto_script.js","static/auto_script.css")
    for name in required:
        if name not in digests or hashlib.sha256((auto/name).read_bytes()).hexdigest()!=digests[name]:
            raise RuntimeError("Auto Script imported source integrity mismatch: "+name)
    data=root/"var/lib/fresh-website/auto-script"
    data.mkdir(parents=True,exist_ok=True)
    db=data/"auto_script.sqlite3"
    scene=root/"srv/fresh-website/current/scene-builder/part01"
    sys.path.insert(0,str(scene))
    sys.path.insert(0,str(auto))
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from fastapi.templating import Jinja2Templates
    from starlette.middleware.sessions import SessionMiddleware
    from itsdangerous import TimestampSigner
    module=importlib.import_module("auto_script")
    module.DB_PATH=db
    app=FastAPI()
    app.add_middleware(SessionMiddleware,secret_key="fresh-ci-auto-script-sessions")
    templates=Jinja2Templates(directory=[str(auto/"templates"),str(scene/"templates")])
    module.register_auto_script(app,templates)
    # Do not enter TestClient lifespan: registered scheduler must remain OFF.
    client=TestClient(app)
    unauth=client.get("/api/auto-script/health")
    if unauth.status_code not in (401,403):
        raise RuntimeError("unauthenticated Auto Script API access possible")
    signer=TimestampSigner("fresh-ci-auto-script-sessions")
    cookie=signer.sign(base64.b64encode(json.dumps({"username":"fresh-ci"}).encode())).decode()
    client.cookies.set("session",cookie)
    health=client.get("/api/auto-script/health")
    if health.status_code!=200:
        raise RuntimeError("signed-session health not accessible "+str(health.status_code))
    response=health.json()
    if not response.get("ok") or response.get("channels")!=0 or response.get("active_items")!=0 or response.get("scheduler"):
        raise RuntimeError("Auto Script state not clean or scheduler active")
    state=client.get("/api/auto-script/state")
    if state.status_code!=200 or state.json().get("items")!=[]:
        raise RuntimeError("Auto Script state or queue invalid")
    page=client.get("/auto-script")
    if page.status_code!=200 or "auto-script-app" not in page.text:
        raise RuntimeError("Auto Script template not renderable")
    with sqlite3.connect(db) as conn:
        for table in ("channels","items","uploader_writebacks"):
            if conn.execute('SELECT COUNT(*) FROM "'+table+'"').fetchone()[0]:
                raise RuntimeError("nonempty Auto Script database: "+table)
    return {"result":"PASS","source_sha256":"PASS","anonymous_api":"DENIED",
            "signed_session_api":"PASS","ui_template":"PASS","fresh_database":"PASS",
            "queue_empty":True,"scheduler_started":False,
            "external_credentials_used":False,"production_modified":False}

if __name__=="__main__":
    p=argparse.ArgumentParser();p.add_argument("--root",required=True)
    try:print(json.dumps(verify(p.parse_args().root),sort_keys=True,indent=2))
    except Exception as e:
        print("AUTO_SCRIPT_INTEGRATION_FAILED: "+str(e),file=sys.stderr)
        sys.exit(1)

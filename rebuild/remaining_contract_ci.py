#!/usr/bin/env python3
"""Target only the unfinished Script Work, Auto Script and LUM contracts."""
import argparse,hashlib,importlib,json,os,pathlib,sqlite3,sys
from rebuild import fresh_vps

def check(root_arg):
    root=fresh_vps.root_dir(root_arg)
    source=root/"srv/fresh-website/current/scene-builder/part01"
    data=root/"var/lib/fresh-website/script-work"
    data.mkdir(parents=True,exist_ok=True)
    os.environ["SCRIPT_WORK_DATA_DIR"]=str(data)
    sys.path.insert(0,str(source))
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from starlette.middleware.sessions import SessionMiddleware
    mod=importlib.import_module("script_extractor_service")
    # Source's real schema and HTTP route are exercised. Never start its
    # YouTube/Google background worker while testing fresh empty queues.
    mod._start_worker=mod._init_storage
    app=FastAPI()
    mod.register_script_work_api(app)
    app.add_middleware(SessionMiddleware,secret_key="ci-temporary-script-work-session")
    response=TestClient(app).get("/api/script-work/state")
    if response.status_code!=200:raise RuntimeError("Script Work route unhealthy")
    with sqlite3.connect(str(mod.DB_PATH)) as db:
        tables={x[0] for x in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"jobs","batches","groups"}.issubset(tables):
            raise RuntimeError("Script Work SQLite schema missing")
        for table in ("jobs","batches","groups"):
            if db.execute("SELECT COUNT(*) FROM "+table).fetchone()[0]:
                raise RuntimeError("Script Work queue not empty")
    groups={"script-work":{"routes":"PASS","db":"PASS","queue_empty":True,
                            "external_worker_not_started":True}}
    # Auto Script is preserved in GitHub, but explicitly excluded from the
    # active rebuild and not a source/runtime readiness prerequisite.
    root_repo=pathlib.Path(__file__).resolve().parents[1]
    groups["auto-script"]={"source":"OFFLINE_USER_EXCLUDED","functional_gate":"NOT_APPLICABLE"}
    lum=root_repo/"apps/local-upload-manager"
    dirs=[lum/f"part0{x}/desktop_core/modules/db/__init__.py" for x in (1,2,3)]
    missing_lum=[str(p.relative_to(root_repo)) for p in dirs if not p.is_file()]
    groups["local-upload-manager"]=({"source":"BLOCKED_MISSING_DB_REPOSITORY","missing":missing_lum}
        if missing_lum else {"source":"PRESENT","service_boot":"PENDING_TEST"})
    blocked=[k for k,v in groups.items() if v.get("source","").startswith("BLOCKED")]
    return {"result":"PASS_VERIFIED_SUBSET_BLOCKED_FULL_11" if blocked else "PASS_SOURCE_GATES",
            "groups":groups,"remaining_source_blockers":blocked,
            "active_application_target":10,"auto_script":"OFFLINE","11_app_complete":False,"production_modified":False}

if __name__=="__main__":
    parser=argparse.ArgumentParser();parser.add_argument("--root",required=True)
    try:print(json.dumps(check(parser.parse_args().root),indent=2,sort_keys=True))
    except Exception as e:
        print("REMAINING_CONTRACT_FAILED: "+str(e),file=sys.stderr);sys.exit(1)

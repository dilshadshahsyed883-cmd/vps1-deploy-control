"""Read-only compatibility map for N7G3, never an Nginx config writer."""
import argparse
from collections import Counter
import json
from pathlib import Path
from rebuild.deploy_control import registry

INVENTORY=Path(__file__).resolve().parents[1]/"infrastructure/registry/n7g3-sanitized-routes.json"
OWNERS={
 "auto-caption-studio":("/auto-caption","/acs-audio-engine","/_internal/auto-caption-auth"),
 "local-upload-manager":("/LUM","/local-upload-manager-engine2",
                          "/local-upload-manager-engine3","/lumconnection",
                          "/lum-oauth","/lum-privacy","/lum-terms",
                          "/_internal/local-upload-manager-auth"),
 "short-stream":("/short-stream",),
 "short-maker":("/short-maker",),
 "video-merger":("/video-merger",),
 "script-work":("/script-work",),
 "voice-studio":("/algrow-voice","/audio-engine","/_internal/audio-engine-auth"),
 "scene-builder":("/api/master-controller",)
}
CRITICAL=("/_internal/","oauth","websockify","/ws","auth","/api/","/health","/media/","/app")

def load(path=INVENTORY):
    obj=json.loads(Path(path).read_text())
    if obj.get("source")!="N7G3" or obj.get("completeness")!="PARTIAL_FILE_SCOPED":
        raise ValueError("unverified or unsupported inventory")
    rows=obj.get("routes")
    if not isinstance(rows,list) or len(rows)!=43:
        raise ValueError("N7G3 requires 43 intact raw entries")
    for r in rows:
        if (set(r)!={"path","upstream_port","auth_mentioned","websocket"}
            or not isinstance(r["path"],str) or not r["path"].startswith("/")
            or (r["upstream_port"] is not None and (type(r["upstream_port"]) is not int
                or not 1<=r["upstream_port"]<=65535))
            or r["auth_mentioned"] not in ("YES","UNKNOWN")
            or type(r["websocket"]) is not bool):
            raise ValueError("unsafe N7G3 route row")
    return rows

def owner(path):
    if path=="/":return "scene-builder"
    for app,prefixes in OWNERS.items():
        if any(path==prefix or path.startswith(prefix+"/") for prefix in prefixes):
            return app
    return None

def report(rows):
    count=Counter(x["path"] for x in rows)
    _,_,active,_=registry()
    output=[]
    for r in rows:
        warnings=[]
        if count[r["path"]]>1:warnings.append("DUPLICATE_LOCATION_PRECEDENCE_UNKNOWN")
        if r["upstream_port"] is None:warnings.append("UPSTREAM_UNKNOWN")
        if r["auth_mentioned"]=="UNKNOWN":warnings.append("AUTH_UNKNOWN")
        if (r["websocket"] or r["auth_mentioned"]=="YES" or r["path"]=="/"
              or any(token in r["path"].lower() for token in CRITICAL)):
            warnings.append("PROTECT_TLS_SSO_OAUTH_CSRF_COOKIE_WEBSOCKET_BEHAVIOR")
        candidate=owner(r["path"])
        if candidate is None:warnings.append("OWNER_UNRESOLVED")
        output.append({**r,"candidate_owner":candidate,"warnings":warnings,
                       "switch_allowed":False})
    return {"source":"N7G3","entries":len(rows),"distinct_paths":len(count),
            "inventory_complete":False,"production_switch_allowed":False,
            "routes":output,"unmapped_active_apps":sorted(active-{x["candidate_owner"] for x in output})}

def plan(app,rows):
    _,_,active,_=registry()
    if app not in active:raise ValueError("unknown/offline app")
    affected=[x for x in report(rows)["routes"] if x["candidate_owner"]==app]
    return {"app":app,"action":"SHADOW_ONLY_NO_ROUTE_CHANGE","switch_allowed":False,
            "affected_entries":len(affected),
            "paths":[x["path"] for x in affected],
            "blockers":["sanitized file-scoped rows not effective nginx -T",
             "preserve protected existing TLS, auth_request, SSO, OAuth callbacks, CSRF, cookies, websocket",
             "prove precise include order, duplicate locations and upstream ownership",
             "backup old Nginx config, test auth and real health, prove rollback",
             "separate per-app approval and Agent 1 Drive Bridge V3"]}

if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--app")
    a=parser.parse_args()
    rows=load()
    print(json.dumps(plan(a.app,rows) if a.app else report(rows),indent=2))

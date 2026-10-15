#!/usr/bin/env python3
"""Targeted live idle startup for eight fresh isolated workers, no remote jobs.

Not a full publishing/media end-to-end test. All queues begin empty; network
proxies point to an unreachable loopback endpoint, never production.
"""
import argparse,json,os,sqlite3,subprocess,sys,time
from pathlib import Path
from rebuild import fresh_vps,full_site_test

WORKERS=[
 ("acs-render","auto-caption-studio","part01",["-m","web_backend.stage_worker","render"]),
 ("acs-compress","auto-caption-studio","part01",["-m","web_backend.stage_worker","compress"]),
 ("acs-upload","auto-caption-studio","part01",["-m","web_backend.stage_worker","upload"]),
 ("lum-engine1","local-upload-manager","part01",["-m","lum_engine.auto_engine_v6_worker"]),
 ("lum-engine2","local-upload-manager","part02",["-m","lum_engine.auto_engine_v6_worker"]),
 ("lum-engine3","local-upload-manager","part03",["-m","lum_engine.auto_engine_v6_worker"]),
 ("omnivoice-dispatch","voice-studio","part01",["voice_clone_dispatcher.py"]),
 ("omnivoice-spooler","voice-studio","part01",["voice_clone_drive_spooler.py"])
]
def db_queue_counts(root):
    records={}
    for p in (root/"var/lib/fresh-website").rglob("*"):
        if p.suffix.lower() not in (".db",".sqlite",".sqlite3") or not p.is_file():continue
        if p.is_symlink():raise RuntimeError("runtime DB symlink")
        with sqlite3.connect(p) as c:
            tables=[x[0] for x in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            for t in tables:
                if t.lower() in ("jobs","queue","job_queue","upload_jobs","items"):
                    t_quoted='"'+t.replace('"','""')+'"'
                    rows=c.execute("SELECT COUNT(*) FROM "+t_quoted).fetchone()[0]
                    if rows:raise RuntimeError("fresh worker queue unexpectedly populated")
                    records[str(p.relative_to(root))+"/"+t]=rows
    return records
def run(arg):
    root=fresh_vps.root_dir(arg)
    native=full_site_test.schemas(root)
    baseline=db_queue_counts(root)
    if len(native)!=13:raise RuntimeError("fresh native schema count mismatch")
    processes=[]
    env_base=os.environ.copy()
    env_base.update({
        "FRESH_WEBSITE_INSTALL":"1","FRESH_WEBSITE_SMOKE_TEST":"1",
        "PYTHONDONTWRITEBYTECODE":"1",
        "HTTP_PROXY":"http://127.0.0.1:9","HTTPS_PROXY":"http://127.0.0.1:9",
        "ALL_PROXY":"http://127.0.0.1:9","NO_PROXY":"127.0.0.1,localhost"
    })
    try:
        for label,app,part,argv in WORKERS:
            env=env_base.copy()
            folder=root/"srv/fresh-website/current"/app/part
            if not folder.is_dir():raise RuntimeError("worker source missing "+label)
            if app=="auto-caption-studio":
                d=root/"var/lib/fresh-website"/app/"acs"
                env.update({"AUTO_CAPTION_APP_ROOT":str(d),"AUTO_CAPTION_CODE_ROOT":str(folder),
                            "AUTO_CAPTION_DB_PATH":str(d/"data/database/auto_caption.db")})
            elif app=="local-upload-manager":
                idx=part.replace("part0","")
                d=root/"var/lib/fresh-website"/app/("lum"+idx)
                env.update({"LUM_DATA_DIR":str(d),
                            "YOUTUBE_UPLOAD_MANAGER_DATA_DIR":str(d/"core"),
                            "LUM_AUTO_ENGINE_V6_ROOT":str(d/"auto_engine_v6"),
                            "LUM_SUITE_AUTH":"0"})
            elif app=="voice-studio":
                d=root/"var/lib/fresh-website"/app/"omnivoice"
                env.update({"VOICE_CLONE_ROOT":str(d),"VOICE_CLONE_WORKER_URL":"http://127.0.0.1:9",
                            "KOKORA_RCLONE_BIN":"/bin/false"})
            cmd=[sys.executable,*argv]
            p=subprocess.Popen(cmd,cwd=folder,env=env,stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            processes.append((label,p))
        # These real service entrypoints must reach an idle loop without
        # crashing or processing remote jobs. No model/credential is supplied.
        until=time.monotonic()+3
        while time.monotonic()<until:
            for name,p in processes:
                if p.poll() is not None:
                    details=p.stderr.read().decode(errors="replace")[-1750:]
                    raise RuntimeError("worker early exit "+name+" rc="+str(p.returncode)+" "+details)
            time.sleep(0.2)
        after=db_queue_counts(root)
        if any(n!=0 for n in after.values()):raise RuntimeError("worker filled previously empty queue")
        return {"result":"PASS_SAFE_IDLE_SUBSET","workers":[k for k,_ in processes],
                "startup_loops_healthy":len(processes),"queue_tables_verified":len(after),
                "external_processing_started":False,"production_modified":False,
                "remaining_blockers":{
                    "kokoro_onnx":"real ONNX model+voice catalog unavailable on clean CI host",
                    "edge_tts":"online voice catalog/external delivery credentials unavailable",
                    "youtube_google_delivery":"credential-dependent end-to-end publishing not exercised"
                },
                "full_11_app_end_to_end_verified":False}
    finally:
        for name,p in reversed(processes):
            if p.poll() is None:
                p.terminate()
                try:p.communicate(timeout=6)
                except subprocess.TimeoutExpired:p.kill();p.communicate()

if __name__=="__main__":
    a=argparse.ArgumentParser();a.add_argument("--root",required=True)
    try:print(json.dumps(run(a.parse_args().root),indent=2))
    except Exception as e:
        print("ISOLATED_WORKER_STARTUP_FAILED: "+str(e),file=sys.stderr)
        sys.exit(1)

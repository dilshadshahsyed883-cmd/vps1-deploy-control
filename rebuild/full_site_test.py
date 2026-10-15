#!/usr/bin/env python3
"""Single coordinated isolated clean-Ubuntu fresh website contract test.

Intentionally *not* an authorized production installer. This command runs
native database schema bootstraps in subprocesses against an isolated /tmp
rootfs and emits a machine-readable readiness report. Missing 11-group
contracts are explicit failures for full-site readiness.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from rebuild import fresh_vps

SCHEMAS = (
    ("scene", "scene-builder", "part01",
     "from app import init_db; init_db()",
     "WEB_DATA_DIR", "scene"),
    ("script-work", "scene-builder", "part01",
     "from script_extractor_service import _init_storage; _init_storage()",
     "SCRIPT_WORK_DATA_DIR", "script-work"),
    ("audio-downloader", "voice-studio", "part01",
     "from audio_downloader_api import init_storage; init_storage()",
     "AUDIO_DOWNLOADER_ROOT", "audio-downloader"),
    ("storage-manager", "vps-storage-manager", "part01",
     "from storage_manager import init_state; init_state()",
     "STORAGE_MANAGER_STATE_ROOT", "storage"),
    ("acs", "auto-caption-studio", "part01",
     "from web_backend.database import initialize_storage, run_migrations; initialize_storage(); run_migrations()",
     "AUTO_CAPTION_APP_ROOT", "acs"),
    ("lum1", "local-upload-manager", "part01",
     "from lum_engine.project_store import ProjectStore; ProjectStore()",
     "LUM_DATA_DIR", "lum1"),
    ("lum2", "local-upload-manager", "part02",
     "from lum_engine.project_store import ProjectStore; ProjectStore()",
     "LUM_DATA_DIR", "lum2"),
    ("lum3", "local-upload-manager", "part03",
     "from lum_engine.project_store import ProjectStore; ProjectStore()",
     "LUM_DATA_DIR", "lum3"),
    ("omnivoice", "voice-studio", "part01",
     "from voice_clone_api import init_storage; init_storage()",
     "VOICE_CLONE_ROOT", "omnivoice"),
    ("kokoro", "voice-studio", "part02",
     "from kokoro_api import init_storage; init_storage()",
     "KOKORO_STUDIO_ROOT", "kokoro"),
    ("edge", "voice-studio", "part03",
     "from edge_api import init_storage; init_storage()",
     "EDGE_STUDIO_ROOT", "edge"),
    ("sml", "self-media-library", "part01",
     "import app; app.init_db()",
     "SML_DATA_ROOT", "sml"),
    ("video-merger", "video-merger", "part01",
     "import app; app.init_db()",
     "VIDEO_MERGER_ROOT", "video-merger"),
)

WORKERS = (
    ("acs-stage-worker","auto-caption-studio","part01","python -m web_backend.stage_worker"),
    ("acs-queue-worker","auto-caption-studio","part01","python -m web_backend.worker"),
    ("lum1-worker","local-upload-manager","part01","python -m lum_engine.auto_engine_v6_worker"),
    ("lum2-worker","local-upload-manager","part02","python -m lum_engine.auto_engine_v6_worker"),
    ("lum3-worker","local-upload-manager","part03","python -m lum_engine.auto_engine_v6_worker"),
    ("omnivoice-dispatcher","voice-studio","part01","python voice_clone_dispatcher.py"),
    ("omnivoice-drive-spooler","voice-studio","part01","python voice_clone_drive_spooler.py"),
    ("kokoro-worker","voice-studio","part02","python kokoro_worker.py"),
    ("edge-worker","voice-studio","part03","python edge_worker.py"),
)

def safe_root(value):
    return fresh_vps.root_dir(value)

def _db_summary(base):
    result={}
    for path in sorted(base.rglob("*")):
        if path.is_symlink(): raise ValueError("symlink in runtime DB location")
        if path.suffix.lower() not in (".db",".sqlite",".sqlite3"): continue
        if not path.resolve().is_relative_to(base.resolve()):
            raise ValueError("DB escaped runtime sandbox")
        with sqlite3.connect(path) as c:
            tables=[row[0] for row in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            if not tables: raise ValueError("missing fresh schema in "+str(path))
            queue_counts={}
            for table in tables:
                if table.lower() in ("jobs","projects","queue","job_queue","upload_jobs","sessions","clips","sources"):
                    name='"'+table.replace('"','""')+'"'
                    n=c.execute("SELECT COUNT(*) FROM "+name).fetchone()[0]
                    queue_counts[table]=n
                    if n: raise ValueError("unexpected legacy entries in fresh "+str(path))
            result[str(path.relative_to(base))]={"tables":len(tables),"empty_queues":queue_counts,
                                                  "sha256":hashlib.sha256(path.read_bytes()).hexdigest()}
    if not result: raise ValueError("no fresh SQLite database generated")
    return result

def schemas(root):
    base=root/"var/lib/fresh-website"
    results=[]
    for name,app,part,code,env_key,folder in SCHEMAS:
        source=root/"srv/fresh-website/current"/app/part
        target=base/app/folder
        target.mkdir(parents=True,exist_ok=True)
        env=os.environ.copy()
        env[env_key]=str(target)
        if name=="scene":
            env["FRESH_WEBSITE_INSTALL"]="1"
            env["SESSION_SECRET"]="ci-scene-site-only"
            env["WEB_ACCESS_KEY"]=""
            env["SCRIPT_WORK_DATA_DIR"]=str(target/"script-work")
            env["SCENE_PERMANENT_ROOT"]=str(target/"permanent")
            env["MASTER_CONTROLLER_DB_PATH"]=str(target/"master_controller.sqlite3")
            env["AUTOFLOW_FLOW_RESULT_ROOT"]=str(target/"flow-results")
        if name=="acs":
            env["AUTO_CAPTION_CODE_ROOT"]=str(source)
            env["AUTO_CAPTION_DB_PATH"]=str(target/"data/database/auto_caption.db")
        if name.startswith("lum"):
            env["YOUTUBE_UPLOAD_MANAGER_DATA_DIR"]=str(target/"core")
        if name=="video-merger":
            env["VIDEO_MERGER_PROXY_CONFIG_DIR"]=str(target/"config")
        env["PYTHONDONTWRITEBYTECODE"]="1"
        result=subprocess.run([sys.executable,"-c",code],cwd=source,env=env,
                              stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                              text=True,timeout=45)
        if result.returncode!=0:
            raise RuntimeError("native fresh schema failed "+name+" rc="+str(result.returncode)+
                               " stderr="+result.stderr[-1400:])
        dbs=_db_summary(target)
        results.append({"schema":name,"app":app,"fresh_databases":len(dbs),"databases":dbs})
    return results

def worker_candidates(root):
    units=root/"etc/systemd/system"
    units.mkdir(parents=True,exist_ok=True)
    created=[]
    for unit,group,part,command in WORKERS:
        if command.startswith("python -m "):
            entry=command.split("python -m ",1)[1]
            candidate=entry.replace(".","/")+".py"
        else:candidate=command.split()[-1]
        source=root/"srv/fresh-website/current"/group/part
        if not (source/candidate).exists():
            raise ValueError("worker entrypoint missing "+unit+" "+candidate)
        path=units/("fresh-"+unit+".service")
        argv=command.replace("python ","/srv/fresh-website/venvs/"+group+"/bin/python ",1)
        extras={
            "lum1-worker": "LUM_DATA_DIR=/var/lib/fresh-website/local-upload-manager/lum1\nYOUTUBE_UPLOAD_MANAGER_DATA_DIR=/var/lib/fresh-website/local-upload-manager/lum1/core\n",
            "lum2-worker": "LUM_DATA_DIR=/var/lib/fresh-website/local-upload-manager/lum2\nYOUTUBE_UPLOAD_MANAGER_DATA_DIR=/var/lib/fresh-website/local-upload-manager/lum2/core\n",
            "lum3-worker": "LUM_DATA_DIR=/var/lib/fresh-website/local-upload-manager/lum3\nYOUTUBE_UPLOAD_MANAGER_DATA_DIR=/var/lib/fresh-website/local-upload-manager/lum3/core\n",
            "kokoro-worker": "KOKORO_STUDIO_ROOT=/var/lib/fresh-website/voice-studio/kokoro\n",
            "edge-worker": "EDGE_STUDIO_ROOT=/var/lib/fresh-website/voice-studio/edge\n",
            "omnivoice-dispatcher": "VOICE_CLONE_ROOT=/var/lib/fresh-website/voice-studio/omnivoice\n",
            "omnivoice-drive-spooler": "VOICE_CLONE_ROOT=/var/lib/fresh-website/voice-studio/omnivoice\n",
            "acs-stage-worker": "AUTO_CAPTION_APP_ROOT=/var/lib/fresh-website/auto-caption-studio/acs\n",
            "acs-queue-worker": "AUTO_CAPTION_APP_ROOT=/var/lib/fresh-website/auto-caption-studio/acs\n"
        }
        (root/"etc/fresh-website"/(unit+".env.example")).write_text(
            "# Configure runtime secrets separately; never commit .env files\n"+
            extras.get(unit,"")
        )
        path.write_text(
            "[Unit]\nDescription=Fresh Website "+unit+"\nAfter=network-online.target\n"
            "ConditionPathExists=/etc/fresh-website/enabled/"+unit+"\n\n"
            "[Service]\nType=simple\nUser=freshweb\nGroup=freshweb\n"
            "WorkingDirectory=/srv/fresh-website/current/"+group+"/"+part+"\n"
            "EnvironmentFile=/etc/fresh-website/"+unit+".env\n"
            "Environment=PYTHONDONTWRITEBYTECODE=1\nExecStart="+argv+"\n"
            "Restart=on-failure\nNoNewPrivileges=true\nProtectSystem=strict\n"
            "ReadWritePaths=/var/lib/fresh-website/"+group+"\n\n"
            "[Install]\nWantedBy=multi-user.target\n")
        created.append(unit)

    # Standalone guarded services are not workers, but own independent unit
    # candidates. No unit is enabled or deployed by this isolated builder.
    for name,group,entry in (
        ("video-merger","video-merger","app.py"),
        ("vps-storage-manager","vps-storage-manager","storage_manager.py serve"),
    ):
        source=root/"srv/fresh-website/current"/group/"part01"
        if not (source/entry.split()[0]).is_file():
            raise ValueError("standalone service source unavailable "+name)
        template=(root/"etc/fresh-website"/(group+".env.example"))
        if not template.is_file():
            raise ValueError("service environment template missing "+group)
        unit=units/("fresh-"+name+".service")
        unit.write_text("[Unit]\nDescription=Fresh Website "+name+"\nAfter=network-online.target\n"
                        "ConditionPathExists=/etc/fresh-website/enabled/"+name+"\n\n"
                        "[Service]\nUser=freshweb\nGroup=freshweb\n"
                        "WorkingDirectory=/srv/fresh-website/current/"+group+"/part01\n"
                        "EnvironmentFile=/etc/fresh-website/"+group+".env\n"
                        "ExecStart=/srv/fresh-website/venvs/"+group+"/bin/python "+entry+"\n"
                        "NoNewPrivileges=true\nPrivateTmp=true\nProtectSystem=strict\n"
                        "ReadWritePaths=/var/lib/fresh-website/"+group+"\n"
                        "Restart=on-failure\n\n[Install]\nWantedBy=multi-user.target\n")
    return created

def build(root,revision):
    # A single integrated run. No historical media, jobs, or DB imported.
    staged=fresh_vps.assemble(str(root),revision)
    ver=fresh_vps.verify(str(root))
    native=schemas(root)
    workers=worker_candidates(root)
    untouched=True
    auth_status="CANDIDATE_ONLY_REQUIRE_EXTERNAL_SECRET"
    blockers={
      "auto-script":"parallel-owned application source is not yet integrated into PR #7",
      "scene-builder":"Algrow import and parallel-owned Auto Script integration must be decoupled",
      "script-work":"Chrome/Xvfb browser setup and authenticated startup not validated",
      "voice-studio":"OmniVoice/Kokoro/Edge worker packages, ONNX models and external APIs not runtime-verified",
      "auto-caption-studio":"FFmpeg/PySide/faster-whisper workers and auth need runtime testing",
      "local-upload-manager":"three workers, Google OAuth and YouTube permissions need runtime testing",
      "short-stream":"stream publishing delivery needs authenticated external destination",
      "short-maker":"FFmpeg, media inputs and Drive output need live integration",
      "video-merger":"direct serving and proxy flow require external Drive dependencies",
      "self-media-library":"media processing and external Drive need authorized credentials",
      "vps-storage-manager":"administrative cleanup cannot be enabled without guarded operator controls"
    }
    report={
       "source_groups_staged":len(staged["apps"]),
       "source_integrity":ver,
       "native_fresh_database_contracts":native,
       "worker_unit_candidates":workers,
       "runtime_records_imported":0,
       "legacy_database_imported":False,
       "legacy_queues_imported":False,
       "nginx_auth":auth_status,
       "production_modified":not untouched,
       "cutover":"DISABLED",
       "all_11_runtime_healthy":False,
       "full_website_rebuild_pass":False,
       "remaining_external_and_runtime_gates":blockers,
       "test_status":"PASS_ISOLATED_SCHEMA_AND_BUILD_NOT_FULL_INSTALL"
    }
    target=root/"srv/fresh-website/integrated-test-report.json"
    target.write_text(json.dumps(report,indent=2,sort_keys=True)+"\n")
    return report

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--root",required=True)
    p.add_argument("--revision",required=True)
    a=p.parse_args()
    if not __import__("re").fullmatch("[a-f0-9]{40}",a.revision):
        p.error("40-hex commit required")
    out=build(safe_root(a.root),a.revision)
    print(json.dumps(out,indent=2,sort_keys=True))

if __name__=="__main__":
    try:main()
    except (ValueError,OSError,RuntimeError,sqlite3.Error,subprocess.TimeoutExpired) as e:
        print("FAILED_INTEGRATED_REBUILD:",str(e),file=sys.stderr)
        sys.exit(1)

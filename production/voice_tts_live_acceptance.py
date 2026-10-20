"""Protected live acceptance on disposable paused rows. No TTS or Drive mutations."""
from __future__ import annotations
import http.client
import json
import secrets
import socket
import sqlite3
import ssl
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from production.voice_tts_controls_release import DATA_ROOTS, require
from production.route_uploader_scene_https import DOMAIN, scene_cookie

MARKER = "vps1-tts-qa-"

def https_json(method, path, cookie=""):
    require(method in ("GET", "POST") and path.startswith("/voice-studio/tts/api/")
            and ".." not in path, "invalid QA route")
    with socket.create_connection(("127.0.0.1", 443), timeout=10) as sock:
        with ssl.create_default_context().wrap_socket(sock, server_hostname=DOMAIN) as tls:
            con = http.client.HTTPSConnection(DOMAIN, timeout=10)
            con.sock = tls
            headers = {"Host": DOMAIN, "Accept": "application/json", "Connection": "close"}
            if cookie:
                headers["Cookie"] = cookie
            con.request(method, path, body=b"" if method=="POST" else None, headers=headers)
            response = con.getresponse()
            status = response.status
            body = response.read(1_000_000)
    try:
        value = json.loads(body)
    except (ValueError, TypeError, UnicodeDecodeError):
        value = {}
    return status, value if isinstance(value, dict) else {}

def insert_inert(conn, engine, jid, marker):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    values = dict(job_id=jid, batch_id=marker, source_filename="qa-inert.txt",
                  output_basename="qa_inert", status="paused",
                  delivery_status="waiting_generation", created_at=now,
                  input_file="/nonexistent/vps1-tts-qa-inert-source.txt")
    if engine == "kokoro":
        values.update(language="en-us",voice="af_heart",speed=1.0)
    else:
        values.update(voice="en-US-JennyNeural",locale="en-US",rate=0,
                      pitch=0,volume=0,updated_at=now)
    with conn:
        conn.execute("INSERT INTO jobs("+",".join(values)+") VALUES("+
                     ",".join("?" for _ in values)+")",tuple(values.values()))

def validate_engine(engine, db_path, cookie, request=https_json):
    require(engine in ("kokoro","edge"), "invalid engine")
    require(db_path.is_file() and not db_path.is_symlink(), "missing engine DB")
    jid=("KOJ-" if engine=="kokoro" else "EDJ-")+secrets.token_hex(8).upper()
    marker=MARKER+secrets.token_hex(8)
    url=f"/voice-studio/tts/api/{engine}/jobs/{jid}"
    con=sqlite3.connect(f"file:{db_path}?mode=rw",uri=True,timeout=15)
    try:
        con.row_factory=sqlite3.Row
        con.execute("PRAGMA busy_timeout=15000")
        insert_inert(con,engine,jid,marker)
        code,_=request("POST",url+"/skip",cookie)
        require(code==409,engine+": invalid paused-to-skip transition accepted")
        code,body=request("POST",url+"/cancel",cookie)
        require(code==200 and body.get("status")=="cancelled",
                engine+": paused-to-cancel failed")
        code,body=request("POST",url+"/cancel",cookie)
        require(code==200 and body.get("idempotent") is True,
                engine+": duplicate cancel was not idempotent")
        code,body=request("POST",url+"/delete",cookie)
        require(code==200 and body.get("hidden") is True,
                engine+": history-only delete failed")
        code,_=request("POST",url+"/cancel",cookie)
        require(code==404,engine+": hidden job still allows mutation")
        code,_=request("POST",url+"/cancel","")
        require(code in (302,303,401,403),
                engine+": anonymous POST was not blocked")
        row=con.execute("SELECT status,hidden_at,delivery_status FROM jobs "
                        "WHERE job_id=? AND batch_id=?",(jid,marker)).fetchone()
        require(row is not None and row["status"]=="cancelled"
                and row["hidden_at"] and row["delivery_status"]=="not_applicable",
                engine+": persisted controls mismatch")
        return {"engine":engine,"controls":"PASS","anonymous_post":"BLOCKED",
                "data":"DISPOSABLE_RECORD_ONLY"}
    finally:
        # Delete ONLY our test row by both unique job ID and private batch marker.
        # Do not touch any owner job, input, audio, or Drive object.
        with con:
            con.execute("DELETE FROM jobs WHERE job_id=? AND batch_id=?",(jid,marker))
        con.close()

def check_existing_remote_receipt(engine,db_path):
    """Read-only remote check for at most one pre-existing delivered output."""
    col="drive_wav" if engine=="kokoro" else "drive_mp3"
    with sqlite3.connect(f"file:{db_path}?mode=ro",uri=True,timeout=15) as db:
        counts=db.execute("""SELECT count(*),sum(CASE
            WHEN drive_file_id IS NOT NULL AND drive_verified_at IS NOT NULL
            AND drive_files_json NOT IN ('','{}') THEN 1 ELSE 0 END)
            FROM jobs WHERE status='completed'
            AND delivery_status IN ('delivered','cleaned')""").fetchone()
        row=db.execute(f"""SELECT drive_file_id,drive_files_json,{col} FROM jobs
            WHERE status='completed' AND delivery_status IN ('delivered','cleaned')
            AND drive_file_id IS NOT NULL AND drive_verified_at IS NOT NULL
            AND {col} LIKE 'gdrive:%' ORDER BY created_at DESC LIMIT 1""").fetchone()
    report={"completed_deliveries":int(counts[0] or 0),
            "stored_receipts":int(counts[1] or 0),
            "remote_status":"NO_EXISTING_VERIFIED_SAMPLE"}
    if row is None:
        return report
    remote=str(row[2])
    exe=shutil.which("rclone")
    config=Path("/home/ubuntu/.config/masterctl/rclone.conf")
    if not remote.startswith("gdrive:Omni Voice/") or ".." in remote:
        report["remote_status"]="INVALID_RECEIPT_PATH"
    elif not exe or not config.is_file():
        report["remote_status"]="RCLONE_CONFIG_UNAVAILABLE"
    else:
        try:
            p=subprocess.run([exe,"--config",str(config),"--retries","1",
                              "--low-level-retries","1","lsjson","--stat","--hash",remote],
                             capture_output=True,text=True,timeout=45)
            if p.returncode==0:
                metadata=json.loads(p.stdout)
                expected=json.loads(row[1])[("wav" if engine=="kokoro" else "mp3")]
                valid=(str(metadata.get("ID") or "")==str(row[0]) and
                       int(metadata.get("Size",-1))==int(expected["size"]))
                report["remote_status"]="PASS" if valid else "RECEIPT_MISMATCH"
            else:
                report["remote_status"]="REMOTE_READ_FAILED"
        except (ValueError,TypeError,KeyError,OSError,subprocess.TimeoutExpired):
            report["remote_status"]="REMOTE_PROBE_ERROR"
    return report

def run_acceptance():
    cookie=scene_cookie()
    jobs=[validate_engine(engine,db,cookie)
          for engine,db in zip(("kokoro","edge"),DATA_ROOTS)]
    evidence={engine:check_existing_remote_receipt(engine,db)
              for engine,db in zip(("kokoro","edge"),DATA_ROOTS)}
    return {"result":"PASS_VOICE_TTS_LIVE_CONTROL_QA","engines":jobs,
            "drive_readonly_evidence":evidence,
            "synthesis":"NOT_TESTED","owner_jobs_mutated":False,
            "auto_script":"OFFLINE"}

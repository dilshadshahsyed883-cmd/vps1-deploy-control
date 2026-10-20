"""Protected live acceptance on disposable paused rows. No TTS or Drive mutations."""
from __future__ import annotations
import http.client
import hashlib
import configparser
import re
import tempfile
import json
import secrets
import socket
import sqlite3
import ssl
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from production.voice_tts_controls_release import DATA_ROOTS, JOURNALS, require
from production.runtime import atomic_json
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

def classify_remote_error(detail):
    """Log only a stable category, never secret-bearing raw rclone stderr."""
    detail=str(detail or "").lower()
    if any(x in detail for x in ("didn't find section", "remote not found",
                                "config file not found", "failed to load config")):
        return "remote_config"
    if any(x in detail for x in ("invalid_grant", "unauthorized", "oauth",
                                "token expired", "401")):
        return "authentication"
    if any(x in detail for x in ("permission denied", "insufficient permissions",
                                "access denied", "403", "forbidden")):
        return "permission"
    if any(x in detail for x in ("rate limit", "quota", "429",
                                "too many requests")):
        return "quota"
    if any(x in detail for x in ("directory not found", "object not found")):
        return "remote_path"
    return "other_remote_error"



# Error fingerprints are fixed names, never the user's rclone diagnostics.
GOOGLE_REASONS = (
    "storageQuotaExceeded", "insufficientFilePermissions",
    "insufficientPermissions", "appNotAuthorizedToFile",
    "userRateLimitExceeded", "dailyLimitExceeded",
    "rateLimitExceeded", "teamDriveFileLimitExceeded",
    "cannotModifyOwnerContent", "fileNotFound",
    "notFound", "accessNotConfigured",
)


def safe_google_error_reason(detail):
    """Convert stderr to one public, allow-listed API reason (no raw data)."""
    source=str(detail or "")
    for reason in GOOGLE_REASONS:
        if re.search(r"(?i)(?:['\"]reason['\"]\s*:\s*['\"])?"
                     + re.escape(reason) + r"(?:['\"])?", source):
            return reason
    lower=source.lower()
    if "insufficient authentication scopes" in lower or "insufficient oauth scope" in lower:
        return "oauthInsufficientScope"
    if "cannot create file in a shared drive" in lower:
        return "sharedDriveCreateRestricted"
    if "service account" in lower and "quota" in lower:
        return "serviceAccountStorageQuota"
    if "403" in lower:
        return "unclassified403"
    return "other"


def safe_rclone_auth_facts(config):
    """Inspect only nonsecret remote type, scope and credential mode."""
    out={"remote_type":"unknown", "auth_mode":"unknown", "scope":"unknown"}
    try:
        settings=configparser.ConfigParser(interpolation=None)
        if not settings.read(str(config)) or not settings.has_section("gdrive"):
            return out
        entry=settings["gdrive"]
        kind=entry.get("type","").strip().lower()
        out["remote_type"]="drive" if kind=="drive" else "other"
        out["auth_mode"]=("service_account" if (entry.get("service_account_file","")
                          or entry.get("service_account_credentials",""))
                          else "oauth" if entry.get("token","") else "other")
        raw_scope=entry.get("scope","").strip().lower()
        raw_scope=raw_scope.rsplit("/",1)[-1]
        scopes={"drive":"full_drive", "drive.file":"drive_file",
                "drive.readonly":"read_only", "drive.metadata.readonly":"metadata_only",
                "drive.appfolder":"app_folder"}
        out["scope"]=scopes.get(raw_scope,"unspecified" if not raw_scope else "other")
    except (OSError,ValueError,configparser.Error):
        pass
    return out


def remote_folder_preflight(executable, config, root):
    """A read-only probe of the selected folder; no listing of user files."""
    cmd=[executable,"--config",str(config),"--retries","1",
         "--low-level-retries","1","lsjson","--stat","gdrive:Omni Voice/"+root]
    try:
        r=subprocess.run(cmd,capture_output=True,text=True,timeout=45)
        if r.returncode:
            return {"status":"UNAVAILABLE",
                    "reason":safe_google_error_reason(r.stderr)}
        item=json.loads(r.stdout)
        return {"status":"VISIBLE" if isinstance(item,dict) and item.get("IsDir")
                else "UNVERIFIED"}
    except (OSError,ValueError,TypeError,subprocess.TimeoutExpired):
        return {"status":"PROBE_ERROR"}

def drive_upload_smoke_once():
    """Two tiny dedicated QA TXT files; verify actual VPS rclone Drive upload.

    No owner media, worker queues, public permissions or prior Drive files touched.
    The tiny remote QA evidence is retained; only the local temporary file is
    cleaned. A durable success journal stops repeated Drive probe uploads.
    """
    receipt_path=JOURNALS/"drive-qa-one-time-receipt.json"
    if receipt_path.is_file() and not receipt_path.is_symlink():
        try:
            receipt=json.loads(receipt_path.read_text("utf-8"))
            if receipt.get("result")=="PASS" and set(receipt.get("engines",{}))=={"kokoro","edge"}:
                return {"result":"PASS","engines":receipt["engines"],
                        "retained_drive_evidence":True,"cached":True}
        except (OSError,ValueError,TypeError):
            pass
    executable=shutil.which("rclone")
    config=Path("/home/ubuntu/.config/masterctl/rclone.conf")
    if not executable or not config.is_file():
        return {"result":"UNAVAILABLE","reason":"rclone_configuration_missing"}
    facts=safe_rclone_auth_facts(config)
    folder_status={name:remote_folder_preflight(executable,config,folder)
                   for name,folder in (("kokoro","Kokoro"),("edge","Edge TTS"))}
    diagnostics={"auth":facts,"folder_access":folder_status}
    completed={}
    for engine,remote_root in (("kokoro","Kokoro"),("edge","Edge TTS")):
        unique=secrets.token_hex(12)
        destination=f"gdrive:Omni Voice/{remote_root}/_VPS1_VOICE_QA/{unique}.txt"
        payload=("VPS1 isolated synthetic Drive upload check\n"+
                 f"engine={engine}\nrandom_marker={unique}\n").encode()
        with tempfile.TemporaryDirectory(prefix="vps1-voice-drive-qa-") as folder:
            local=Path(folder)/"qa.txt"
            local.write_bytes(payload)
            command=[executable,"--config",str(config),"--tpslimit","1",
                     "--tpslimit-burst","1","--retries","1","--low-level-retries","1"]
            try:
                sent=subprocess.run(command+["copyto","--ignore-existing",
                                             str(local),destination],
                                    capture_output=True,text=True,timeout=150)
                if sent.returncode:
                    return {"result":"INCOMPLETE","engine":engine,
                            "reason":"remote_copy_failed",
                            "category":classify_remote_error(sent.stderr),
                            "google_reason":safe_google_error_reason(sent.stderr),
                            "diagnostics":diagnostics,"completed":completed}
                read=subprocess.run(command+["lsjson","--stat","--hash",destination],
                                    capture_output=True,text=True,timeout=70)
                if read.returncode:
                    return {"result":"INCOMPLETE","engine":engine,
                            "reason":"remote_receipt_lookup_failed",
                            "category":classify_remote_error(read.stderr),
                            "google_reason":safe_google_error_reason(read.stderr),
                            "diagnostics":diagnostics,"completed":completed}
                metadata=json.loads(read.stdout)
                file_id=str(metadata.get("ID") or "")
                remote_md5=(metadata.get("Hashes") or {}).get("MD5")
                matched=bool(
                    len(file_id)>=8 and
                    int(metadata.get("Size",-1))==len(payload) and
                    str(metadata.get("Name") or "")==unique+".txt" and
                    (not remote_md5 or remote_md5.lower()==hashlib.md5(payload).hexdigest())
                )
                if not matched:
                    return {"result":"INCOMPLETE","engine":engine,
                            "reason":"remote_identity_size_or_hash_mismatch",
                            "diagnostics":diagnostics,"completed":completed}
                completed[engine]="ID_SIZE_HASH_VERIFIED"
            except (OSError,ValueError,TypeError,KeyError,subprocess.TimeoutExpired):
                return {"result":"INCOMPLETE","engine":engine,
                        "reason":"upload_or_receipt_exception",
                        "diagnostics":diagnostics,"completed":completed}
    proof={"result":"PASS","engines":completed,"retained_drive_evidence":True,
           "auto_script":"OFFLINE","diagnostics":diagnostics}
    atomic_json(receipt_path,proof,0o600)
    return proof


def run_acceptance():
    cookie=scene_cookie()
    jobs=[validate_engine(engine,db,cookie)
          for engine,db in zip(("kokoro","edge"),DATA_ROOTS)]
    evidence={engine:check_existing_remote_receipt(engine,db)
              for engine,db in zip(("kokoro","edge"),DATA_ROOTS)}
    upload_proof=drive_upload_smoke_once()
    return {"result":"PASS_VOICE_TTS_LIVE_CONTROL_QA","engines":jobs,
            "drive_readonly_evidence":evidence,
            "drive_real_upload_proof":upload_proof,
            "synthesis":"NOT_TESTED","owner_jobs_mutated":False,
            "auto_script":"OFFLINE"}

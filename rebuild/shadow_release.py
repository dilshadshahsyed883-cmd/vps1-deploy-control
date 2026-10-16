"""Source-only offline shadow release executor. NEVER a production VPS adapter."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile
import tempfile
from rebuild.deploy_control import registry
from rebuild.deploy_engine import verify_release_candidate

SHA=re.compile(r"[a-f0-9]{40}\Z")
CLEARANCES={"backup_restore_proven","capacity_current","runtime_verified",
 "auth_negative_tested","worker_ownership_verified","rollback_proven",
 "port_ownership_verified","security_reviewed"}
MAX_EXTRACT=100*1024*1024

class ShadowBlocked(RuntimeError): pass

def safe_root(path):
    p=Path(path).absolute()
    if p==Path("/tmp") or not p.is_relative_to(Path("/tmp")):
        raise ShadowBlocked("offline root must be under a dedicated /tmp directory")
    if any(x.is_symlink() for x in (p,*p.parents)):
        raise ShadowBlocked("symlinked root denied")
    return p

@contextmanager
def host_lock(root):
    root.mkdir(parents=True,exist_ok=True)
    p=root/".shadow-host-lock"
    if p.is_symlink(): raise ShadowBlocked("unsafe lock")
    with p.open("a+") as f:
        fcntl.flock(f.fileno(),fcntl.LOCK_EX)
        yield

def write_journal(p,rec):
    p.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w",dir=p.parent,delete=False) as f:
        temp=Path(f.name)
        json.dump(rec,f,indent=2,sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp,p)

def admit(app,sha,manifest,evidence,occupied_ports=()):
    groups,profiles,active,offline=registry()
    if app not in active or groups[app].get("parallel_owner") or not SHA.fullmatch(sha):
        raise ShadowBlocked("invalid/offline app or immutable commit")
    if not isinstance(evidence,dict): raise ShadowBlocked("admission evidence missing")
    missing=[k for k in sorted(CLEARANCES) if evidence.get(k) is not True]
    if missing: raise ShadowBlocked("clearance missing: "+",".join(missing))
    for key in ("free_bytes","peak_new_bytes","host_reserve_bytes","rollback_bytes"):
        if type(evidence.get(key)) is not int or evidence[key]<0:
            raise ShadowBlocked("measured capacity missing: "+key)
    if (evidence["free_bytes"]-evidence["host_reserve_bytes"]
        -evidence["rollback_bytes"]<evidence["peak_new_bytes"]):
        raise ShadowBlocked("insufficient peak capacity")
    port=evidence.get("shadow_port")
    if type(port) is not int or not 1024<=port<=65535 or port in occupied_ports:
        raise ShadowBlocked("port conflict or invalid port")
    if evidence.get("loopback_only") is not True: raise ShadowBlocked("non-loopback port")
    deps=evidence.get("verified_dependencies")
    if not isinstance(deps,list) or any(x not in active for x in deps) or (
        not set(groups[app]["needs"]).issubset(set(deps))):
        raise ShadowBlocked("unverified dependency order")
    if evidence.get("app_id")!=app or evidence.get("source_sha")!=sha:
        raise ShadowBlocked("admission bound to wrong app/revision")
    try: digest=verify_release_candidate(app,sha,manifest)
    except (ValueError,OSError,KeyError,tarfile.TarError) as ex:
        raise ShadowBlocked("artifact integrity failed: "+str(ex)) from ex
    return {"app":app,"sha":sha,"sha256":digest,"lock_group":groups[app]["lock"],
            "shadow_port":port,"production_modified":False}

def pointer(root,app):
    p=root/"shadow-current"/app
    if p.is_symlink():
        active=p.resolve(strict=True)
        if active.parent!=(root/"releases"/app).resolve() or not SHA.fullmatch(active.name):
            raise ShadowBlocked("shadow pointer escapes app releases")
        return p,active
    if p.exists(): raise ShadowBlocked("non-symlink shadow pointer")
    return p,None

def switch(p,target):
    p.parent.mkdir(parents=True,exist_ok=True)
    temp=p.with_name("."+p.name+".next")
    if temp.exists() or temp.is_symlink(): raise ShadowBlocked("stale pointer transition")
    try:
        if target is None: p.unlink(missing_ok=True)
        else:
            temp.symlink_to(target)
            os.replace(temp,p)
    finally:
        if temp.is_symlink(): temp.unlink()

def unpack(manifest,app,dest):
    m=json.loads(Path(manifest).read_text())
    total=0
    with tarfile.open(Path(manifest).parent/m["archive"],"r:gz") as tar:
        for member in tar:
            bits=PurePosixPath(member.name).parts
            if not member.isfile() or len(bits)<2 or bits[0]!=app or ".." in bits:
                raise ShadowBlocked("unsafe archive member")
            total+=member.size
            if total>MAX_EXTRACT: raise ShadowBlocked("archive size cap")
            path=dest.joinpath(*bits[1:])
            path.parent.mkdir(parents=True,exist_ok=True)
            if path.exists() or path.is_symlink(): raise ShadowBlocked("member collision")
            with tar.extractfile(member) as src,path.open("xb") as out:
                shutil.copyfileobj(src,out)
    (dest/".source-sha256").write_text(m["sha256"]+"\n")

def shadow_install(root,app,sha,manifest,evidence,probe,occupied_ports=()):
    """Only /tmp writes and injected health probes. No socket, SSH, systemd or Nginx."""
    root=safe_root(root)
    if not callable(probe): raise ShadowBlocked("probe required")
    with host_lock(root):  # host-wide mutex also serializes all shared-resource locks
        ticket=admit(app,sha,manifest,evidence,occupied_ports)
        log=root/"transactions"/app/(sha+".json")
        p,old=pointer(root,app)
        if old is not None and old.name==sha:
            if probe(app,old,"existing") is not True:
                raise ShadowBlocked("existing shadow unhealthy")
            return {"status":"NO_CHANGE_HEALTHY",**ticket}
        if log.exists() or log.is_symlink(): raise ShadowBlocked("duplicate or interrupted intent")
        release=root/"releases"/app/sha
        if release.exists() or release.is_symlink(): raise ShadowBlocked("immutable release collision")
        rec={"status":"PREPARED","previous":old.name if old else None,**ticket}
        write_journal(log,rec)
        changed=False
        temp=None
        try:
            release.parent.mkdir(parents=True,exist_ok=True)
            temp=Path(tempfile.mkdtemp(prefix=".stage-",dir=release.parent))
            unpack(manifest,app,temp)
            os.replace(temp,release)
            temp=None
            rec["status"]="STAGED"
            write_journal(log,rec)
            if probe(app,release,"staged") is not True: raise ShadowBlocked("staged health failed")
            switch(p,release)
            changed=True
            if probe(app,release,"shadow") is not True: raise ShadowBlocked("shadow health failed")
            rec["status"]="SHADOW_VERIFIED"
            write_journal(log,rec)
            return rec
        except Exception as ex:
            if changed:
                try:
                    switch(p,old)
                    _,actual=pointer(root,app)
                    if actual!=old or (old is not None and probe(app,old,"rollback") is not True):
                        raise ShadowBlocked("rollback health/readback failed")
                    rec["status"]="ROLLED_BACK"
                except Exception:
                    rec["status"]="NEEDS_ATTENTION"
            else: rec["status"]="ABORTED"
            rec["error"]=str(ex)[:300]
            write_journal(log,rec)
            raise ShadowBlocked(rec["status"]+": "+str(ex)) from ex
        finally:
            if temp is not None: shutil.rmtree(temp)

def rollback_shadow(root,app,sha,probe):
    root=safe_root(root)
    if not SHA.fullmatch(sha) or not callable(probe): raise ShadowBlocked("rollback SHA/probe required")
    with host_lock(root):
        p,old=pointer(root,app)
        target=root/"releases"/app/sha
        if old is None or not target.is_dir() or target.is_symlink() or not (
            target/".source-sha256").is_file(): raise ShadowBlocked("previous verified release missing")
        if probe(app,target,"preflight") is not True: raise ShadowBlocked("previous unhealthy")
        switch(p,target)
        try:
            if p.resolve(strict=True)!=target or probe(app,target,"rollback") is not True:
                raise ShadowBlocked("rollback verification failed")
        except Exception:
            switch(p,old)
            raise
        return {"status":"ROLLED_BACK","restored":sha,"production_modified":False}

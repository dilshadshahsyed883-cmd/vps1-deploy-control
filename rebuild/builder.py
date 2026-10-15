#!/usr/bin/env python3
"""Fail-closed source inventory, deterministic artifact and offline rootfs builder.

This module never writes to /etc, /opt or /var; production operations remain
outside the source-only CI lane until per-app cutover is explicitly verified.
"""
import argparse
import ast
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tarfile
import tempfile

ROOT=Path(__file__).resolve().parents[1]
GROUPS=ROOT/'rebuild/groups.json'
BANNED_SEGMENTS={'.git','node_modules','__pycache__','data','database','db','cache','caches','logs','log','queues','queue','uploads','output','outputs','media','generated','runtime','secrets','credentials','venv','.venv','backups','backup','releases','.ssh','sessions','.next','dist'}
DENY_FILENAME=re.compile(r'(^\.env(?:\.|$)|^id_(rsa|ed25519)|rclone\.conf$|oauth.*\.json$|secret|credentials?|token\.json|cookie|\.pem$|\.key$|\.pfx$|\.p12$|\.sqlite|\.db$|\.mp4$|\.mp3$|\.wav$|\.log$)',re.I)
SECRET_PATTERNS=[re.compile(x,re.I|re.M) for x in (rb'-----BEGIN [A-Z ]*PRIVATE KEY-----',rb'gh[pousr]_[A-Za-z0-9_]{20,}',rb'AKIA[A-Z0-9]{16}',rb'AIza[A-Za-z0-9_-]{35}',rb'ya29\.[A-Za-z0-9._-]{20,}',rb'(?m)^\s*(?:api[_-]?key|client[_-]?secret|refresh[_-]?token|access[_-]?token|password)\s*[:=]\s*[\x22\x27]?[A-Za-z0-9_./+\-=]{12,}')]


def embedded_credential(content,filename):
    """Reject hardcoded credentials but allow runtime environment lookups."""
    if any(p.search(content) for p in SECRET_PATTERNS[:5]):
        return True
    sensitive=re.compile(r'(?:^|_)(?:password|api_key|client_secret|refresh_token|access_token|token)$',re.I)
    filename=Path(filename)
    if filename.suffix.lower()=='.py':
        try:tree=ast.parse(content.decode('utf-8'))
        except (UnicodeError,SyntaxError):return True
        for node in ast.walk(tree):
            targets=[];value=None
            if isinstance(node,ast.Assign):
                targets=[v.id for v in node.targets if isinstance(v,ast.Name)]
                value=node.value
            elif isinstance(node,ast.AnnAssign):
                targets=[node.target.id] if isinstance(node.target,ast.Name) else []
                value=node.value
            if (isinstance(value,ast.Constant) and isinstance(value.value,str)
                and len(value.value)>=12 and any(sensitive.search(k) for k in targets)
                and not any(value.value == k and k.isupper() for k in targets)):
                return True
            if isinstance(node,ast.Dict):
                for k,v in zip(node.keys,node.values):
                    if (isinstance(k,ast.Constant) and isinstance(k.value,str)
                        and sensitive.search(k.value) and isinstance(v,ast.Constant)
                        and isinstance(v.value,str) and len(v.value)>=12):
                        return True
    elif filename.suffix.lower()=='.json':
        try:doc=json.loads(content)
        except (ValueError,UnicodeError):return True
        def check(item):
            if isinstance(item,dict):
                return any((sensitive.search(str(k)) and isinstance(v,str) and len(v)>=12)
                    or check(v) for k,v in item.items())
            if isinstance(item,list):return any(check(v) for v in item)
            return False
        return check(doc)
    return False


def config():
    c=json.loads(GROUPS.read_text())
    assert c['version']==1
    assert all(c[k] is False for k in ('production_deploy_enabled','fresh_install_enabled','automatic_database_migrations','legacy_database_import','legacy_queue_import','legacy_media_import'))
    names=[g['id'] for g in c['groups']]
    if len(names)!=11 or len(set(names))!=11 or 'algrow-voice' in names:
        raise ValueError('fixed 11-group scope violated')
    for g in c['groups']:
        if not re.fullmatch(r'[a-z][a-z0-9-]+',g['id']) or not re.fullmatch(r'[a-z][a-z0-9-]+',g['lock']):
            raise ValueError('unsafe app ID / concurrency lock')
        if any(n not in names or n==g['id'] for n in g['needs']):
            raise ValueError('invalid dependency mapping')
    return c


def source_path(g):
    # Auto Script is owned by the existing parallel branch; its files MUST NOT
    # be generated/replaced by this branch. Reconcile after that branch merges.
    return ROOT/'apps'/g['id']


def _is_lum_db_python_source(folder, rel, path):
    """Only the LUM *source package* named db, never runtime database files.

    Exempts the exact part01/02/03/desktop_core/modules/db Python package
    from the banned-directory rule. The other banned segments remain strict.
    """
    if folder != ROOT / 'apps' / 'local-upload-manager':
        return False
    parts=rel.parts
    if len(parts) not in (4, 5):
        return False
    if parts[0] not in ('part01','part02','part03') or parts[1:4] != ('desktop_core','modules','db'):
        return False
    if len(parts)==4:
        return path.is_dir()
    return path.is_file() and path.suffix.lower()=='.py'


def _is_lum_sensitive_named_python_helper(folder,rel,path):
    """Only the code helper secret_store.py within LUM's three owned parts.

    A module name containing 'secret' is not itself a secret. All file-content
    scanners still run and literal credentials remain prohibited.
    """
    return (folder == ROOT/'apps'/'local-upload-manager'
            and len(rel.parts)==4
            and rel.parts[0] in ('part01','part02','part03')
            and rel.parts[1:]==('desktop_core','modules','secret_store.py')
            and path.is_file())


def file_list(folder):
    result=[]
    for p in sorted(folder.rglob('*')):
        rel=p.relative_to(folder)
        if p.is_symlink():raise ValueError('symlinks prohibited '+str(p))
        banned={part.lower() for part in rel.parts if part.lower() in BANNED_SEGMENTS}
        if banned and not (banned == {'db'} and _is_lum_db_python_source(folder,rel,p)):
            raise ValueError('runtime directory found '+str(rel))
        if 'algrow' in str(rel).lower():
            raise ValueError('Algrow is strictly out of scope '+str(rel))
        if not p.is_file():continue
        if DENY_FILENAME.search(p.name) and not _is_lum_sensitive_named_python_helper(folder,rel,p):
            raise ValueError('sensitive file name '+str(rel))
        if p.stat().st_size>4_000_000:raise ValueError('oversize source '+str(rel))
        content=p.read_bytes()
        if b'\0' in content:raise ValueError('binary source '+str(rel))
        if embedded_credential(content,p):raise ValueError('potential credential '+str(rel))
        if p.suffix=='.py':compile(content,str(p),'exec',dont_inherit=True)
        result.append((str(rel),content))
    if not result:raise ValueError('no source files: '+str(folder))
    return result


def state():
    c=config(); found=[]; missing=[]
    for g in c['groups']:
        if g['parallel_owner']:
            # Only ready when separately-owned branch is merged AND tested.
            missing.append({'app':g['id'],'status':'PARALLEL_OWNER_UNVERIFIED'})
        elif source_path(g).is_dir():
            items=file_list(source_path(g))
            found.append({'app':g['id'],'files':len(items),'lock_group':g['lock']})
        else:missing.append({'app':g['id'],'status':'SOURCE_MISSING'})
    return {'source_present':found,'pending':missing,'ready':not missing}


def ordered_groups():
    todo={g['id']:set(g['needs']) for g in config()['groups']}
    result=[]
    while todo:
        ready=sorted(k for k,v in todo.items() if not v)
        if not ready:raise ValueError('dependency cycle detected')
        for k in ready:result.append(k);todo.pop(k)
        for v in todo.values():v.difference_update(ready)
    return result


def artifact(app, sha, out_dir):
    if not re.fullmatch(r'[a-fA-F0-9]{40}',sha):raise ValueError('commit SHA required')
    groups={x['id']:x for x in config()['groups']}
    if app not in groups or groups[app]['parallel_owner']:
        raise ValueError('unknown or parallel-owned app: '+app)
    files=file_list(source_path(groups[app]))
    out_dir.mkdir(parents=True,exist_ok=True)
    name=f'{app}-{sha[:12]}.tar.gz'
    path=out_dir/name
    with tarfile.open(path,'w:gz') as out:
        for n,b in files:
            entry=tarfile.TarInfo(app+'/'+n)
            entry.mode=0o644;entry.uid=0;entry.gid=0;entry.mtime=0;entry.size=len(b)
            out.addfile(entry,io.BytesIO(b))
    report={'app':app,'commit_sha':sha,'archive':name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'files':[{'path':n,'sha256':hashlib.sha256(b).hexdigest()} for n,b in files]}
    mp=out_dir/(name+'.manifest.json');mp.write_text(json.dumps(report,sort_keys=True,indent=2)+'\n')
    return mp


def verify(manifest):
    m=json.loads(manifest.read_text());p=manifest.parent/m['archive']
    if hashlib.sha256(p.read_bytes()).hexdigest()!=m['sha256']:raise ValueError('archive digest mismatch')
    expected={n['path']:n['sha256'] for n in m['files']};actual={}
    with tarfile.open(p,'r:gz') as tar:
        for item in tar.getmembers():
            name=PurePosixPath(item.name);parts=name.parts
            if not item.isfile() or item.issym() or item.islnk() or len(parts)<2 or parts[0]!=m['app'] or '..' in parts:
                raise ValueError('unsafe archive member')
            rel=str(PurePosixPath(*parts[1:]))
            if rel in actual:raise ValueError('duplicate archive member')
            actual[rel]=hashlib.sha256(tar.extractfile(item).read()).hexdigest()
    if actual!=expected:raise ValueError('manifest/file mismatch')
    return m['app']


def stage_rootfs(destination):
    # Offline rehearsal builds a target filesystem; never invokes sudo/systemd,
    # never installs packages, never opens real production paths.
    status=state()
    if not status['ready']:raise ValueError('not ready: '+json.dumps(status['pending']))
    if destination.resolve()==Path('/') or str(destination.resolve()).startswith(('/etc/','/opt/','/var/')):
        raise ValueError('unsafe destination')
    for group in config()['groups']:
        src=source_path(group);target=destination/'srv/fresh-website/apps'/group['id']
        target.mkdir(exist_ok=True,parents=True)
        for rel,data in file_list(src):
            dest=target/rel;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(data)
    (destination/'srv/fresh-website/READY_FOR_CUTOVER').write_text('NO: explicit rebuild cutover approval required\n')
    return status


def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['state','plan','package','verify','stage-rootfs']);parser.add_argument('--app');parser.add_argument('--sha');parser.add_argument('--output',default='dist/fresh-website');parser.add_argument('--manifest');a=parser.parse_args()
    if a.action=='state':print(json.dumps(state(),indent=2))
    elif a.action=='plan':print(json.dumps({'installation_order':ordered_groups(),'deploy_enabled':False,'cutover':'BLOCKED_UNTIL_EXPLICIT_APPROVAL'},indent=2))
    elif a.action=='package':print(artifact(a.app,a.sha,Path(a.output)))
    elif a.action=='verify':print('PASS artifact '+verify(Path(a.manifest)))
    elif a.action=='stage-rootfs':print(json.dumps(stage_rootfs(Path(a.output)),indent=2))

if __name__=='__main__':
    try:main()
    except (ValueError,OSError,SyntaxError,AssertionError,KeyError) as e:
        print('BLOCKED: '+str(e),file=sys.stderr);sys.exit(1)

"""Guarded Scene navigation overlay; existing rendering worker remains untouched."""
import hashlib,json,shutil
from production.runtime import BASE,REPO,atomic_json,atomic_link,run
def install(revision):
    ptr=BASE/'current/scene-builder';old=ptr.resolve(strict=True)
    if not ptr.is_symlink() or not old.is_relative_to(BASE/'releases/scene-builder'):
        raise RuntimeError('Unowned Scene current pointer')
    names=['part01/templates/base.html','part01/static/vps1_native_site_shell.js']
    source={n:(REPO/'apps/scene-builder'/n).read_bytes() for n in names}
    if b'Copyright Studio' not in source[names[0]] or b'Copyright Uploader' not in source[names[1]]:
        raise RuntimeError('Missing reviewed Copyright navigation')
    worker='vps1-scene-production-worker.service'
    pid=run(['systemctl','show','--property=MainPID','--value',worker],quiet=True).strip()
    if pid in ('','0'):raise RuntimeError('Renderer not ready')
    target=BASE/'releases/scene-builder'/(revision+'-copyright-nav')
    receipt=BASE/'transactions/copyright-scene-nav'/(revision+'.json')
    if target.exists() or receipt.exists():raise RuntimeError('Immutable Scene navigation release already exists')
    shutil.copytree(old,target,symlinks=True)
    manifest_path=target/'_scene_candidate_manifest.json'
    manifest=json.loads(manifest_path.read_text())
    for n,data in source.items():
        if n not in manifest['files']:raise RuntimeError('Manifest source drift')
        (target/n).write_bytes(data);manifest['files'][n]=hashlib.sha256(data).hexdigest()
    manifest.update(revision=revision,overlay='copyright-studio-navigation')
    atomic_json(manifest_path,manifest,0o644)
    changed=False
    try:
        atomic_link(ptr,target);changed=True
        run(['systemctl','restart','vps1-scene-candidate.service'],quiet=True,timeout=75)
        from production.scene_candidate_release import _health
        _health()
        from production.route_uploader_scene_https import scene_cookie,https_get
        code,_,html=https_get('/scene-builder/',scene_cookie())
        if code!=200 or b'Copyright Studio' not in html:raise RuntimeError('Public Scene navigation missing')
        if run(['systemctl','show','--property=MainPID','--value',worker],quiet=True).strip()!=pid:
            raise RuntimeError('Renderer unexpectedly restarted')
        receipt.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        atomic_json(receipt,{
            'revision':revision,'previous':str(old),'after':str(target),
            'renderer_pid':pid,
            'nav_sha256':hashlib.sha256(source[names[1]]).hexdigest(),
            'state':'ACTIVE'
        },0o600)
    except BaseException:
        if changed:
            atomic_link(ptr,old)
            run(['systemctl','restart','vps1-scene-candidate.service'],quiet=True,timeout=75)
        raise

def rollback(revision):
    """Compare-and-swap only the exact Copyright Scene nav overlay.

    The renderer is never restarted; existing workers/jobs remain untouched.
    """
    receipt=BASE/'transactions/copyright-scene-nav'/(revision+'.json')
    if not receipt.is_file() or receipt.is_symlink():
        raise RuntimeError('Copyright Scene rollback receipt absent')
    data=json.loads(receipt.read_text())
    ptr=BASE/'current/scene-builder'
    root=BASE/'releases/scene-builder'
    after=root/(revision+'-copyright-nav')
    previous=__import__('pathlib').Path(data.get('previous',''))
    if (data.get('revision')!=revision or data.get('state')!='ACTIVE'
            or data.get('after')!=str(after)
            or not ptr.is_symlink() or ptr.resolve(strict=True)!=after.resolve(strict=True)
            or not previous.is_relative_to(root) or not previous.is_dir()
            or hashlib.sha256((after/'part01/static/vps1_native_site_shell.js').read_bytes()).hexdigest()!=data.get('nav_sha256')):
        raise RuntimeError('Copyright Scene rollback state drift')
    worker='vps1-scene-production-worker.service'
    pid=run(['systemctl','show','--property=MainPID','--value',worker],quiet=True).strip()
    if not pid or pid in ('0',) or pid!=data.get('renderer_pid'):
        raise RuntimeError('Scene renderer changed; refuse unsafe rollback')
    changed=False
    try:
        atomic_link(ptr,previous);changed=True
        run(['systemctl','restart','vps1-scene-candidate.service'],quiet=True,timeout=75)
        from production.scene_candidate_release import _health
        _health()
        if run(['systemctl','show','--property=MainPID','--value',worker],quiet=True).strip()!=pid:
            raise RuntimeError('Scene render worker restarted during rollback')
        atomic_json(receipt,{**data,'state':'ROLLED_BACK'},0o600)
    except BaseException:
        if changed:
            atomic_link(ptr,after)
            run(['systemctl','restart','vps1-scene-candidate.service'],quiet=True,timeout=75)
        raise

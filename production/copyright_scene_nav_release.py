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
    if target.exists():raise RuntimeError('Target already exists')
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
    except BaseException:
        if changed:
            atomic_link(ptr,old)
            run(['systemctl','restart','vps1-scene-candidate.service'],quiet=True,timeout=75)
        raise

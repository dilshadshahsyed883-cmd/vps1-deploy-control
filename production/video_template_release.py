"""Scoped Video Studio shared-asset overlay; no dependencies or worker changes."""
from __future__ import annotations
import hashlib
import html
import json
import re
import shutil
import urllib.request
from pathlib import Path

from production.runtime import BASE, CATALOG, DATA, REPO, atomic_json, atomic_link, event, http_check, run

PAGES = {
    'short-maker': 'part01/app.py',
    'video-merger': 'part01/templates/index.html',
    'auto-caption-studio': 'part01/web_backend/acs_studio.html',
}
ASSETS = ('vps1_native_site_shell.css','vps1_native_site_shell.js','vps1_video_studio_geometry.css')
TERMINAL = {'completed','complete','done','failed','cancelled','cleaned','ready'}


def require(ok, why):
    if not ok:
        raise RuntimeError('VIDEO_UI_RELEASE_BLOCKED: '+why)


def patch_assets(payload: bytes) -> bytes:
    """Change only three shared asset references, preserving active backend code."""
    for asset in ASSETS:
        pattern = rb'/(?:static|&#115;tatic)/'+re.escape(asset.encode())+rb'\?v=[\w-]+(?:&(?:amp;)?layout=[\w-]+)?'
        matches = list(re.finditer(pattern,payload))
        require(len(matches)==1,'shared asset missing/duplicated: '+asset)
        m=matches[0]
        version=m.group().split(b'?v=',1)[1].split(b'&',1)[0]
        replacement=b'/&#115;tatic/'+asset.encode()+b'?v='+version+b'&layout=video-v2'
        payload=payload[:m.start()]+replacement+payload[m.end():]
    return payload


def read_json(url):
    with urllib.request.urlopen(url,timeout=15) as response:
        require(response.status==200,'idle gate HTTP error')
        return json.load(response)


def idle(app):
    """Do not interrupt in-process renders, downloads, imports or waiting jobs."""
    if app=='auto-caption-studio':
        return  # ACS execution belongs to independent, untouched worker units.
    if app=='video-merger':
        # The UI state endpoint also performs a recursive Drive folder listing
        # (rclone timeout up to 120s). It is not a safe deployment idle probe.
        # Resolve the actual running service's data root, then inspect the
        # existing SQLite job and library statuses strictly read-only.
        # A missing/locked DB, waiting merge, or importing media fails closed.
        from production.video_merger_import_release import idle_database, live_paths
        state, _ = live_paths(DATA / 'video-merger/video-merger')
        idle_database(state)
        return
    port=CATALOG[app]['port']
    path='/api/jobs'
    data=read_json(f'http://127.0.0.1:{port}'+path)
    require(isinstance(data,dict) and isinstance(data.get('jobs'),list),'idle gate jobs missing')
    records=list(data['jobs'])
    if app=='video-merger':
        require(isinstance(data.get('library'),list),'idle gate library missing')
        records+=data['library']
    require(all(isinstance(r,dict) and str(r.get('status','')).lower() in TERMINAL for r in records),
            app+' has unfinished work; UI overlay deferred')


def verify(app):
    from production.route_uploader_scene_https import scene_cookie, https_get
    http_check(app)
    cookie=scene_cookie()
    code,kind,page=https_get('/'+app+'/',cookie)
    require(code==200 and 'html' in kind.lower(),'authenticated app HTML unavailable')
    decoded=html.unescape(page.decode())
    for asset in ASSETS:
        require(re.search(r'(?:src|href)="/static/'+re.escape(asset)+r'\?[^\"]*layout=video-v2',decoded),
                'public shared asset URL still rewritten: '+asset)
        code,kind,body=https_get('/static/'+asset+'?layout=video-v2',cookie)
        require(code==200 and len(body)>100 and 'html' not in kind.lower(), 'shared asset load failed')
    code,_,_=https_get('/'+app+'/')
    require(code in {401,403,303},'anonymous app access changed')
    http_check(app)


def install(app, revision):
    require(app in PAGES,'unsupported app')
    require(re.fullmatch('[a-f0-9]{40}',revision),'invalid revision')
    current=BASE/'current'/app
    require(current.is_symlink(),'current release not linked')
    previous=current.resolve(strict=True)
    releases=BASE/'releases'/app
    require(previous.is_relative_to(releases),'unowned release')
    path=Path(PAGES[app])
    original=(previous/path).read_bytes()
    updated=patch_assets(original)
    # Validate the repository's future full releases use the same safe asset form.
    source=(REPO/'apps'/app/path).read_bytes()
    require(patch_assets(source)==source,'source shared assets not canonical')
    unit='vps1-production-'+app+'.service'
    run(['systemctl','is-active','--quiet',unit],quiet=True)
    idle(app)
    if updated==original:
        verify(app)
        event(app=app,result='PASS_VIDEO_TEMPLATE_ALREADY_LIVE',workers='PRESERVED')
        return
    target=releases/(revision+'-video-ui-only')
    require(not target.exists(),'immutable overlay exists')
    journal=BASE/'transactions/video-ui-only'/app/revision
    journal.mkdir(parents=True,exist_ok=False)
    atomic_json(journal/'previous.json',{'previous':str(previous),'revision':revision})
    shutil.copytree(previous,target,symlinks=True)
    require((target/path).is_file() and not (target/path).is_symlink(),'template linked or missing')
    (target/path).write_bytes(updated)
    manifest=target/'_production_manifest.json'
    value=json.loads(manifest.read_text())
    require(value.get('app')==app and str(path) in value.get('files',{}),'manifest ownership invalid')
    value['revision']=revision
    value['files'][str(path)]=hashlib.sha256(updated).hexdigest()
    value['ui_only_overlay']=str(previous)
    atomic_json(manifest,value,0o644)
    idle(app)
    changed=False
    try:
        atomic_link(current,target)
        changed=True
        run(['systemctl','restart',unit],timeout=60,quiet=True)
        verify(app)
        atomic_json(journal/'result.json',{'status':'ACTIVE','workers':'PRESERVED','data':'UNCHANGED'})
        event(app=app,result='PASS_VIDEO_TEMPLATE_ONLY',workers='PRESERVED',data='UNCHANGED')
    except BaseException:
        if changed:
            atomic_link(current,previous)
            run(['systemctl','restart',unit],timeout=60,quiet=True)
            http_check(app)
            event(app=app,rollback='PASS',data='PRESERVED')
        raise

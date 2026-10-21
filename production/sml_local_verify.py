"""SML-only release acceptance; synthetic lifecycle state and existing Scene renderer."""
from pathlib import Path
import json
import subprocess
import sqlite3
import urllib.request
from production.runtime import REPO


def verify(venv):
    # Tests create their own temporary SQLite/catalog; production library is never cut or cleared.
    subprocess.run(['/usr/sbin/runuser','-u','ubuntu','--',str(Path(venv)/'bin/python'),
                    '-W','ignore','-m','unittest','discover','-s','tests',
                    '-p','test_self_media_library_local.py','-v'],cwd=REPO,check=True,timeout=180)
    from production.sml_scene_activation import _entries, _ensure_private, SML_ENV, SCENE_CURRENT
    config=_entries(_ensure_private(SML_ENV))
    token=config.get('SML_EDITOR_API_TOKEN','')
    # Refuse a local-only cutover if any retained catalog media would become unavailable.
    root=Path(config['SML_DATA_ROOT'])
    with sqlite3.connect((root/'library.db').as_uri()+'?mode=ro',uri=True) as conn:
        for table,folder,column in [('clips','clips','filename'),('image_assets','images','stored_name')]:
            for (name,) in conn.execute(f'SELECT {column} FROM {table} WHERE deleted_at IS NULL'):
                if not name or Path(name).name!=name or not (root/folder/name).is_file():
                    raise RuntimeError('Retained catalog media is not local; preserve old release and restore media before cutover')
    if not token:raise RuntimeError('Existing SML Editor token is required')
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for route in ('/api/health','/api/local/storage','/api/v1/media/status'):
        with opener.open('http://127.0.0.1:8147'+route,timeout=20) as response:
            result=json.load(response)
        if route=='/api/health':assert result['ok'] and result['storage_mode']=='vps_only' and not result['drive_enabled']
        if route=='/api/v1/media/status':assert result['editor_token_configured']
    request=urllib.request.Request('http://127.0.0.1:8147/api/v1/media/search?media_type=video&limit=3',headers={'X-SML-Editor-Token':token})
    with opener.open(request,timeout=20) as response:assert isinstance(json.load(response)['items'],list)
    from production.scene_sml_worker import SceneSMLWorker
    # render_proof only uses existing authenticated acquisition/render functions; no install/restart.
    try:
        proof=SceneSMLWorker(SCENE_CURRENT.resolve(strict=True),token).render_proof()
    except Exception:
        raise RuntimeError('SML Scene compatibility verification failed; source rollback required') from None
    assert proof['scene_count']==3 and proof['sml_scenes']>=1
    print('SML_LOCAL_PAIR_CLEANUP_AND_SCENE_COMPATIBILITY=PASS',flush=True)

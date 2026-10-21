"""Disposable real-source downloads; no production queue, DB or Drive writes."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile


def smoke(source, state, proxy_config=None):
    with tempfile.TemporaryDirectory(prefix='vm-import-smoke-') as directory:
        root = Path(directory)
        os.environ['VIDEO_MERGER_ROOT'] = str(root)
        os.environ['VIDEO_MERGER_PROXY_CONFIG_DIR'] = str(root/'config')
        os.environ['DENO_DIR'] = str(root/'deno_cache')
        spec = importlib.util.spec_from_file_location('vm_smoke', source)
        app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(app)
        # Read existing proxy settings only, without changing permissions/config.
        app.PROXY_CONFIG_PATH = proxy_config or Path(state)/'config/proxy_profiles.json'
        original_run=app.run
        def bounded_run(args, timeout=None, **kwargs):
            return original_run(args, timeout=min(timeout or 180,180), **kwargs)
        app.run=bounded_run
        app.init_db()
        config=app.load_proxy_config()
        # The old VPS already stored a selected proxy, but fresh VPS has the
        # app's toggle off. Exercise it in a disposable config, never the live JSON.
        candidate = None
        if not config.get('enabled') and config.get('profiles'):
            candidate = next((p for p in config['profiles']
                              if p.get('id') == config.get('active_id')), None)
            if candidate is None:
                raise RuntimeError('Saved proxy exists but has no valid active selection')
            trial = dict(config)
            trial['enabled'] = True
            trial_path = root / 'config' / 'proxy_profiles.json'
            trial_path.parent.mkdir(parents=True, exist_ok=True)
            trial_path.write_text(json.dumps(trial), encoding='utf-8')
            trial_path.chmod(0o600)
            app.PROXY_CONFIG_PATH = trial_path
        results = {'proxy_enabled':bool(config.get('enabled')),
                   'saved_proxy_profiles':len(config.get('profiles',[])),
                   'candidate_saved_proxy':bool(candidate),
                   'js_runtime':str(app.DENO) if app.DENO.is_file() else 'MISSING'}
        try:
            profile = app.active_proxy_profile()
            if profile:
                tested = app.test_proxy_profile(profile['id'])
                results['proxy_test'] = 'REAL_DOWNLOAD_PASS' if tested.get('download_verified') else 'NOT_VERIFIED'
            else:
                results['proxy_test'] = 'DIRECT_MODE'
            app.source_to_file('https://youtu.be/jNQXAC9IVRw', root/'youtube.bin', None, 1)
            results['youtube'] = 'PASS'
        except Exception as exc:
            results['youtube'] = 'FAIL: '+app.scrub_proxy_error(exc)
        if candidate:
            results['candidate_proxy_test'] = results.get('proxy_test', 'NOT_VERIFIED')
            results['candidate_youtube'] = results.get('youtube', 'NOT_VERIFIED')
        try:
            database = Path(state)/'data/video_merger.db'
            with sqlite3.connect(database.as_uri()+'?mode=ro', uri=True) as conn:
                conn.execute('PRAGMA query_only=ON')
                rows = conn.execute("SELECT drive_url,source_url FROM library_items ORDER BY CASE WHEN status='ready' THEN 0 ELSE 1 END, created_at DESC").fetchall()
            urls = [u for row in rows for u in row if u and app.looks_drive(u)]
            if not urls:
                raise RuntimeError('No existing Drive video source available for read-only smoke')
            app.source_to_file(urls[0], root/'drive.bin', None, 1)
            results['drive'] = 'PASS'
        except Exception as exc:
            results['drive'] = 'FAIL: '+app.scrub_proxy_error(exc)
        # Persist a new Library entry against disposable storage, exercising the
        # production import/transcode/verification path without Drive mutations.
        media = root/'drive.bin' if results['drive']=='PASS' else root/'youtube.bin'
        if media.exists():
            original_source = app.source_to_file
            import shutil
            app.source_to_file = lambda url,dst,*a,**kw: shutil.copy2(media,dst)
            remote = root/'remote'; remote.mkdir()
            def local_remote(args, **kw):
                if args[0]=='mkdir': return ''
                if args[0]=='copyto': shutil.copy2(args[1],remote/'asset.mp4'); return ''
                if args[0]=='lsjson': return json.dumps({'Size':(remote/'asset.mp4').stat().st_size,'ID':'disposable'})
                raise AssertionError('unexpected remote operation')
            app.rclone = local_remote
            # Avoid asynchronous imports; call the actual worker synchronously.
            now=app.utcnow()
            with app.db() as conn:
                conn.execute("INSERT INTO library_items(id,name,filename,created_at,updated_at) VALUES(?,?,?,?,?)",('smoke','smoke','smoke.mp4',now,now))
            app.process_library_import('smoke','https://example.com/smoke.mp4',{'url':'https://example.com/video.mp4','trim_enabled':False})
            item=app.get_library_item('smoke')
            results['library_persistence'] = 'PASS' if item['status']=='ready' and item['size_bytes']>0 else 'FAIL'
        print(json.dumps(results,sort_keys=True),flush=True)
        if results.get('youtube')!='PASS' or results.get('drive')!='PASS' or results.get('library_persistence')!='PASS':
            raise RuntimeError('Real-source smoke failed; candidate not promoted')
        if candidate and results.get('proxy_test')!='REAL_DOWNLOAD_PASS':
            raise RuntimeError('Saved proxy real media canary failed; live setting left disabled')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--state',type=Path,required=True)
    parser.add_argument('--proxy-config',type=Path)
    args=parser.parse_args()
    smoke(args.source,args.state,args.proxy_config)

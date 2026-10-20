"""Real subprocess startup and bounded functional checks, using temporary data."""
from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

from production.runtime import CATALOG, REPO, environment


def render_short(client):
    from PIL import Image
    png = io.BytesIO()
    Image.new('RGB', (96, 96), '#4675c0').save(png, format='PNG')
    response = client.post('/api/jobs', files={'images': ('test.png', png.getvalue(), 'image/png')},
                           data={'video_duration': '.3', 'image_duration': '.3', 'fps': '24',
                                 'motion': 'none', 'output_count': '1',
                                 'upload_to_drive': 'false', 'verified_clean': 'false'})
    response.raise_for_status()
    job = response.json()['job_id']
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        state = client.get('/api/jobs/' + job).json()
        if state.get('status') == 'failed':
            raise RuntimeError('real render failed: ' + str(state.get('error')))
        if state.get('status') == 'completed':
            output = client.get('/' + state['outputs'][0]['url'])
            output.raise_for_status()
            if len(output.content) < 1000 or b'ftyp' not in output.content[:32]:
                raise RuntimeError('rendered output is not an MP4')
            return {'render': 'PASS', 'output_bytes': len(output.content), 'drive_transfer': False}
        time.sleep(.25)
    raise TimeoutError('render did not finish')



def smoke_isolated_uploader_pages(state):
    """Exercise real independent app HTML; health alone missed embedded HTTP 500.

    All subprocesses and databases are disposable, never real OAuth/media/queues.
    """
    import httpx
    source = REPO / "apps" / "local-upload-manager" / "part01"
    checked = {}
    for mode, leaf in (("engine2", "lum2"), ("shorts", "lum3")):
        root = state / leaf
        root.mkdir(parents=True, exist_ok=True)
        env = {
            **os.environ,
            **environment("local-upload-manager", state),
            "LUM_DATA_DIR": str(root),
            "YOUTUBE_UPLOAD_MANAGER_DATA_DIR": str(root / "core"),
            "LUM_AUTO_ENGINE_V6_ROOT": str(root / "auto_engine_v6"),
            "LUM_GOOGLE_OAUTH_ROOT": str(root / "secure"),
            "LUM_BASE_PATH": f"/local-upload-manager/modes/{mode}",
            "LUM_ENGINE_INSTANCE": mode,
            "LUM_SUITE_AUTH": "1",
            "FRESH_WEBSITE_SMOKE_TEST": "1",
            "RCLONE_CONFIG": str(state / "absent-rclone.conf"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        command = [
            sys.executable, "-m", "uvicorn", "lum_engine.standalone_app:app",
            "--host", "127.0.0.1", "--port", str(port),
        ]
        logpath = state / (mode + "-embedded-runtime.log")
        with logpath.open("w+") as log:
            proc = subprocess.Popen(
                command, cwd=source, env=env,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            )
            try:
                with httpx.Client(
                    base_url=f"http://127.0.0.1:{port}", timeout=7,
                    trust_env=False, follow_redirects=False,
                    headers={
                        "X-Auth-User": "isolated-smoke",
                        "X-App-Access": "lum",
                        "X-Is-Admin": "0",
                    },
                ) as client:
                    base = f"/local-upload-manager/modes/{mode}"
                    deadline = time.monotonic() + 35
                    while time.monotonic() < deadline:
                        if proc.poll() is not None:
                            log.seek(0)
                            raise RuntimeError(
                                f"{mode} startup failed: " + log.read()[-3000:]
                            )
                        try:
                            health = client.get(base + "/health")
                            if (health.status_code == 200 and
                                    health.json().get("ok") is True and
                                    health.json().get("engine_instance") == mode):
                                break
                        except (httpx.TransportError, ValueError):
                            pass
                        time.sleep(.2)
                    else:
                        log.seek(0)
                        raise RuntimeError(
                            f"{mode} health timeout: " + log.read()[-3000:]
                        )
                    page = client.get(base + "/app?embedded=1&tab=auto_engine")
                    expected = "Engine 2" if mode == "engine2" else "Short Uploader"
                    if (page.status_code != 200 or
                            "text/html" not in page.headers.get("content-type", "") or
                            "Auto Engine" not in page.text or expected not in page.text or
                            f'data-engine-instance="{mode}"' not in page.text or
                            page.text.count('aria-label="Auto Uploader modes"') != 1 or
                            page.text.count('aria-label="Auto Uploader workspace"') != 1 or
                            "lum-primary-workspaces" not in page.text or
                            "vps1_native_site_shell.js" not in page.text or
                            "<iframe" in page.text.lower() or
                            "Internal Server Error" in page.text):
                        log.seek(0)
                        raise RuntimeError(
                            f"{mode} embedded UI HTTP {page.status_code}: " +
                            log.read()[-4000:]
                        )
                    checked[mode] = "NATIVE_HTML_200_NO_IFRAME"
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
    return checked


def smoke(app):
    import httpx
    spec = CATALOG[app]
    with tempfile.TemporaryDirectory(prefix='vps1-runtime-') as tmp:
        state = Path(tmp)
        env = {**os.environ, **environment(app, state), 'FRESH_WEBSITE_SMOKE_TEST': '1'}
        # Test data never points at a production path or a Drive credential.
        env['RCLONE_CONFIG'] = str(state / 'absent-rclone.conf')
        env['PATH'] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get('PATH', '')
        if app == 'scene-builder':
            env.update(PYTHONPATH=str(REPO), WEB_USERNAME='runtime-test', WEB_ACCESS_KEY='runtime-test-only')
        if app == 'voice-studio':
            # Start the genuine empty-queue downloader worker; no network job is submitted.
            env['FRESH_WEBSITE_SMOKE_TEST'] = '0'
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        if 'script' in spec:
            env['VIDEO_MERGER_PORT'] = str(port)
            command = [sys.executable, spec['script']]
        else:
            command = [sys.executable, '-m', 'uvicorn', spec['module'],
                       '--host', '127.0.0.1', '--port', str(port)]
        source = REPO / 'apps' / app / 'part01'
        if app == 'auto-caption-studio':
            env['AUTO_CAPTION_CODE_ROOT'] = str(source)
        log = (state / 'runtime.log').open('w+')
        proc = subprocess.Popen(command, cwd=source, env=env,
                                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        try:
            with httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=5, trust_env=False,
                              headers={'X-Auth-User': 'runtime-test', 'X-App-Access': 'both', 'X-Is-Admin': '1'},
                              follow_redirects=False) as client:
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    if proc.poll() is not None:
                        log.seek(0)
                        raise RuntimeError('startup failed: ' + log.read()[-4500:])
                    try:
                        response = client.get(spec['health'])
                        if response.status_code == 200 and response.json().get('ok') is True:
                            break
                    except (httpx.TransportError, ValueError):
                        pass
                    time.sleep(.2)
                else:
                    log.seek(0)
                    raise TimeoutError('health unavailable: ' + log.read()[-4500:])
                ui = client.get(spec['ui'])
                if ui.status_code != 200 or 'text/html' not in ui.headers.get('content-type', ''):
                    log.seek(0)
                    raise RuntimeError(f'UI failed: {ui.status_code} {ui.text[:400]}\n' + log.read()[-4500:])
                result = {'app': app, 'health': 'PASS', 'html_ui': 'PASS', 'external_jobs': False}
                if app == 'scene-builder':
                    channels = client.post('/api/scene-builder/channels/add', json={'name': 'Runtime check'})
                    channels.raise_for_status()
                    if not channels.json().get('ok') or 'Runtime check' not in channels.json()['channels']:
                        raise RuntimeError('Scene channel editing failed')
                    saved = json.loads((state / 'scene_channels.json').read_text())
                    if 'Runtime check' not in str(saved):
                        raise RuntimeError('Scene channel state was not persisted')
                    result['channel_editing'] = 'PASS'
                if app == 'short-maker':
                    result.update(render_short(client))
                if app == 'self-media-library':
                    stats = client.get('/api/stats')
                    stats.raise_for_status()
                    if stats.json()['sources'] != 0:
                        raise RuntimeError('unexpected imported media state')
                    result['empty_library'] = 'PASS'
                if app == 'auto-caption-studio':
                    projects = client.get('/api/projects')
                    if projects.status_code != 200:
                        raise RuntimeError('authenticated ACS project API unavailable')
                    result['project_api'] = 'PASS'
                if app == 'local-upload-manager':
                    # Exercise the real authenticated Auto Engine screen using only
                    # isolated temporary state. No queue, schedule or upload POST.
                    auto_ui = client.get(spec['ui'] + '?tab=auto_engine')
                    if auto_ui.status_code != 200 or 'text/html' not in auto_ui.headers.get('content-type', ''):
                        raise RuntimeError('Auto Engine UI unavailable: ' + str(auto_ui.status_code))
                    if 'auto engine' not in auto_ui.text.lower():
                        raise RuntimeError('Auto Engine tab did not render')
                    if not response.json().get('worker_alive'):
                        raise RuntimeError('local upload manager web queue worker unavailable')
                    # Every mode must render separately without an upload POST.
                    mode_status = client.get('/local-upload-manager/api/auto-modes')
                    mode_status.raise_for_status()
                    modes = mode_status.json()
                    if set(modes['selected_modes']) != {'engine1', 'engine2', 'shorts', 'short_stream'}:
                        raise RuntimeError('four-mode registry incomplete')
                    if modes['cross_mode_uploads_enabled']:
                        raise RuntimeError('unverified cross-mode uploader was enabled')
                    native_engine1 = client.get(spec['ui'] + '?tab=auto_engine&auto_mode=engine1')
                    if (native_engine1.status_code != 200 or
                            'data-engine-instance="engine1"' not in native_engine1.text or
                            '<iframe' in native_engine1.text.lower()):
                        raise RuntimeError('Engine 1 native Auto Engine view failed')
                    # Legacy parent URLs must redirect to the independent native
                    # document; silently rendering an iframe is not acceptable.
                    for mode in ('engine2', 'shorts'):
                        page = client.get(spec['ui'] + '?tab=auto_engine&auto_mode=' + mode)
                        expected_url = (
                            f'/local-upload-manager/modes/{mode}/app'
                            '?embedded=1&tab=auto_engine'
                        )
                        if (page.status_code != 303 or
                                page.headers.get('location') != expected_url):
                            raise RuntimeError(
                                'mode switching must redirect to native page: ' + mode
                            )
                    stream_page = client.get(spec['ui'] + '?tab=auto_stream')
                    if stream_page.status_code != 200 or 'Independent Auto Stream' not in stream_page.text:
                        raise RuntimeError('independent Auto Stream subtab is not available')
                    old_bookmark = client.get(spec['ui'] + '?tab=auto_engine&auto_mode=short_stream')
                    if old_bookmark.status_code != 303 or not old_bookmark.headers.get('location', '').endswith('?tab=auto_stream'):
                        raise RuntimeError('legacy stream bookmark did not redirect to independent Auto Stream')
                    if client.get(spec['ui'] + '?tab=auto_engine&auto_mode=../../').status_code != 400:
                        raise RuntimeError('unknown upload mode was accepted')
                    # Launch both real isolated services against separate throwaway
                    # stores. A parent-only health/UI smoke never exercises the iframe.
                    result['isolated_embedded_pages'] = smoke_isolated_uploader_pages(state)
                    result['three_mode_isolation_ui'] = 'PASS'
                    result['auto_stream_independent_navigation'] = 'PASS'
                    result['auto_engine_read_only_ui'] = 'PASS'
                    result['web_queue_worker'] = 'PASS'
                if app == 'voice-studio':
                    downloader = client.get('/tts/api/audio-downloader/status')
                    downloader.raise_for_status()
                    if not downloader.json().get('ready') or not downloader.json().get('worker'):
                        raise RuntimeError('audio downloader toolchain unavailable')
                    result['audio_downloader_toolchain'] = 'PASS'
                    readiness = client.get('/tts/api/studio/readiness')
                    readiness.raise_for_status()
                    payload = readiness.json()
                    engines = payload.get('engines', {})
                    if not payload.get('ok') or set(engines) != {
                        'kokoro', 'edge', 'omnivoice', 'audio_downloader'
                    }:
                        raise RuntimeError('Voice Studio three-engine readiness API failed')
                    for mode, endpoint in [
                        ('kokoro', '/tts/api/kokoro/status'),
                        ('edge', '/tts/api/edge/status'),
                    ]:
                        response = client.get(endpoint)
                        response.raise_for_status()
                        if 'ready' not in response.json():
                            raise RuntimeError('Voice engine status unavailable: ' + mode)
                    config = client.get('/tts/api/clone/omnivoice/config')
                    config.raise_for_status()
                    if config.json().get('engine') != 'omnivoice':
                        raise RuntimeError('OmniVoice configuration API failed')
                    result['voice_three_engine_apis'] = 'PASS'
                    result['omnivoice_external_gpu_tested'] = False
                return result
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            log.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--app', required=True, choices=sorted(CATALOG))
    args = parser.parse_args()
    print(json.dumps(smoke(args.app), sort_keys=True))


if __name__ == '__main__':
    main()

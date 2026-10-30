"""Owner-authorized sQ74S7cZ3Kg route comparison. No owner queue or Drive writes.

Run with the live Voice Python/environment as ubuntu. Each route is explicit;
no cookies, automatic route fallback, config writes or raw provider logs.
"""
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

VIDEO = 'sQ74S7cZ3Kg'


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def receipt(path, ffmpeg):
    probe = Path(ffmpeg).with_name('ffprobe')
    if not probe.is_file():
        probe = shutil.which('ffprobe')
    value = subprocess.run([str(probe), '-v', 'error', '-show_entries',
                            'format=duration:stream=codec_name,codec_type', '-of', 'json', str(path)],
                           capture_output=True, text=True, timeout=30, check=True)
    info = json.loads(value.stdout)
    duration = float(info['format']['duration'])
    assert duration > 0 and path.stat().st_size > 1024
    assert any(s.get('codec_name') == 'mp3' and s.get('codec_type') == 'audio'
               for s in info['streams'])
    return {'result': 'PASS', 'bytes': path.stat().st_size, 'duration_seconds': duration,
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def worker(source, root, config, name, state):
    os.environ['AUDIO_DOWNLOADER_ROOT'] = str(root)
    m = load(source, name)
    m.init_storage()
    m.deps = lambda: state
    m._write_proxy_config(config)
    # Compare unauthenticated access even when a private live session exists.
    m._cookie_bytes = lambda: None
    updates = {}
    m._update = lambda jid, status, **kw: updates.update(status=status, **kw)
    original = m._run_tracked
    def bounded(command, **kw):
        kw['timeout'] = 140
        return original(command, **kw)
    m._run_tracked = bounded
    job = {'job_id': 'ADJ-' + 'A'*16, 'video_id': VIDEO,
           'url': 'https://www.youtube.com/watch?v=' + VIDEO,
           'format': 'mp3', 'quality': 192, 'keep_metadata': False,
           'auto_upload': False, 'auto_song_maker': False, 'download_thumbnail': True}
    try:
        m.run_job(job)
        result = receipt(Path(updates['local_file']), m.deps()['ffmpeg'])
        image = Path(updates['local_thumbnail'])
        assert image.name == VIDEO + '.jpg' and image.read_bytes().startswith(b'\xff\xd8\xff')
        result.update(thumbnail_bytes=image.stat().st_size, session='NONE', drive='NOT_USED')
        return result
    except Exception as exc:
        return {'result': getattr(exc, 'code', 'DOWNLOAD_OR_VALIDATION_FAILED'),
                'session': 'NONE', 'drive': 'NOT_USED'}


def api_reference(source, root, config):
    """Reference Python API options, retaining the explicitly tested VPS route."""
    os.environ['AUDIO_DOWNLOADER_ROOT'] = str(root)
    m = load(source, 'restore_api_reference')
    m.init_storage(); m._write_proxy_config(config); m._cookie_bytes = lambda: None
    import yt_dlp
    try:
        with m.youtube_execution() as (options, _):
            # parse_options only loads the private explicit route/runtime snapshot.
            opts = yt_dlp.parse_options(options).ydl_opts
            opts.update(format='bestaudio[ext=m4a]/bestaudio/best',
                        outtmpl=str(root / '%(id)s.%(ext)s'), noplaylist=True,
                        continuedl=True, overwrites=False, quiet=True,
                        ffmpeg_location=m.deps()['ffmpeg'],
                        postprocessors=[{'key': 'FFmpegExtractAudio',
                                         'preferredcodec': 'mp3', 'preferredquality': '192'}])
            # Raw provider output must never expose configuration or credentials.
            class Silent:
                def debug(self, *args): pass
                def warning(self, *args): pass
                def error(self, *args): pass
            opts['logger'] = Silent()
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.extract_info('https://www.youtube.com/watch?v=' + VIDEO, download=True)
        return receipt(root / (VIDEO + '.mp3'), m.deps()['ffmpeg'])
    except Exception as exc:
        return {'result': m.download_failure(exc).code, 'exception': type(exc).__name__,
                'reason': m._safe_proxy_error(str(exc))}


def main(source, baseline=None):
    live_root = Path(os.environ['AUDIO_DOWNLOADER_ROOT'])
    original = load(source, 'restore_live_dependencies')
    state = original.deps()
    command = state['ytdlp_command']
    baseline_state = load(baseline, 'restore_baseline_dependencies').deps() if baseline else state
    config_path = live_root / 'data/proxy_settings.json'
    cfg = json.loads(config_path.read_text()) if config_path.is_file() else original._proxy_blank()
    report = {'video_id': VIDEO, 'owner_jobs_mutated': False, 'cookies_used': False,
              'config_sha256': hashlib.sha256(config_path.read_bytes()).hexdigest() if config_path.is_file() else None,
              'ytdlp_cli_version': subprocess.run(command + ['--version'], capture_output=True,
                                                 text=True, timeout=20, check=True).stdout.strip(),
              'ytdlp_api_version': importlib.metadata.version('yt-dlp'),
              'deno_present': (Path(sys.executable).parent / 'deno').is_file(),
              'proxy_environment_overridden': any(k in os.environ for k in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY')),
              'cli_kind': 'executable' if state.get('ytdlp') else 'python_module',
              'ejs_present': importlib.util.find_spec('yt_dlp_ejs') is not None,
              'routes': {}, 'result': 'FAIL'}
    if report['deno_present']:
        report['deno_version'] = subprocess.run([str(Path(sys.executable).parent / 'deno'), '--version'],
                                                 capture_output=True, text=True, timeout=15, check=True).stdout.splitlines()[0]
    try:
        report['ejs_version'] = importlib.metadata.version('yt-dlp-ejs')
    except importlib.metadata.PackageNotFoundError:
        report['ejs_version'] = None
    configurations = {'direct': {**cfg, 'enabled': False}}
    if original._selected_proxy({**cfg, 'enabled': True}):
        configurations['selected_proxy'] = {**cfg, 'enabled': True}
    active = 'selected_proxy' if cfg.get('enabled') else 'direct'
    with tempfile.TemporaryDirectory(prefix='audio-restore-qa-') as tmp:
        for route, config in configurations.items():
            current = Path(tmp) / route
            value = {}
            if baseline:
                value['baseline'] = worker(baseline, current / 'baseline', config, 'baseline_' + route, baseline_state)
            value['candidate'] = worker(source, current / 'candidate', config, 'candidate_' + route, state)
            value['python_api_reference'] = api_reference(source, current / 'api', config)
            report['routes'][route] = value
        passed = [r for r in configurations if report['routes'][r]['candidate']['result'] == 'PASS']
        selected = active if active in passed else (passed[0] if passed else None)
        report['verified_route'] = selected
        if selected:
            report['result'] = 'PASS'
    print(json.dumps(report, sort_keys=True), flush=True)
    return 0 if report['result'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main(Path(sys.argv[1]), Path(sys.argv[2]) if len(sys.argv) > 2 else None))

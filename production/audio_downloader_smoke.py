"""Disposable authorized yt-dlp test clip, using the exact deployed worker path.

Runs as the Voice service owner. Never inserts into the owner queue. Only its
own temporary media and uniquely named QA Drive file may be removed.
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid


def owns_qa_drive_file(remote, nonce, drive_remote, drive_base, safe_folder):
    """Confirm exactly the disposable QA output; reject any owner file."""
    if not isinstance(remote, str) or not isinstance(nonce, str):
        return False
    prefix = (f'{drive_remote}:{drive_base}/'
              f'{safe_folder("__Audio_Proxy_QA__")}/{safe_folder(nonce)}/')
    return remote == prefix + 'jNQXAC9IVRw.mp3'


def proxy_disabled_report(report):
    """No proxy configured: never treat a direct-only YouTube failure as release failure."""
    if report.get('proxy', {}).get('enabled'):
        return False
    report['result'] = 'PROXY_DISABLED_ON_VPS'
    report['reason'] = 'Audio Downloader proxy disabled; external download and Drive not verified'
    return True


def main(source):
    live_root = Path(os.environ['AUDIO_DOWNLOADER_ROOT'])
    # The short public canary also used by the verified Video Merger extractor.
    nonce = uuid.uuid4().hex[:16].upper()
    with tempfile.TemporaryDirectory(prefix='audio-proxy-qa-') as tmp:
        os.environ['AUDIO_DOWNLOADER_ROOT'] = tmp
        default_session = live_root / 'data/youtube-cookies.txt'
        if 'AUDIO_DOWNLOADER_COOKIES_FILE' not in os.environ and default_session.is_file():
            os.environ['AUDIO_DOWNLOADER_COOKIES_FILE'] = str(default_session)
        spec = importlib.util.spec_from_file_location('audio_proxy_qa', source)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        m.init_storage()
        config = live_root / 'data/proxy_settings.json'
        # No cross-app fallback/import during QA; an absent app config is explicit.
        m._write_proxy_config(json.loads(config.read_text()) if config.is_file() else m._proxy_blank())
        state = m.deps()
        cmd = state.get('ytdlp_command') or []
        version = subprocess.run(cmd + ['--version'], capture_output=True, text=True, timeout=15).stdout.strip() if cmd else 'MISSING'
        report = {'ytdlp_version': version, 'proxy': m.proxy_runtime_status(),
                  'session': m.cookie_status(), 'owner_jobs_mutated': False,
                  'audio': 'NOT_VERIFIED', 'drive': 'NOT_VERIFIED'}
        if proxy_disabled_report(report):
            print(json.dumps(report, sort_keys=True))
            return 0
        jid = 'ADJ-' + nonce
        job = {'job_id': jid, 'video_id': 'jNQXAC9IVRw', 'url': 'https://www.youtube.com/watch?v=jNQXAC9IVRw',
               'format': 'mp3', 'quality': 128, 'keep_metadata': False,
               'auto_upload': state['upload_ready'], 'folder': '__Audio_Proxy_QA__', 'project': nonce}
        # Capture only this synthetic job's state; the real queue is never opened.
        result = {}
        def update(_jid, status, **values):
            assert _jid == jid
            result.update(status=status, **values)
        m._update = update
        try:
            m.run_job(job)
            report['audio'] = 'PASS'
            report['drive'] = 'PASS' if result.get('status') == 'delivered' else 'NOT_CONFIGURED'
            report['result'] = 'PASS' if report['proxy']['enabled'] else 'DIRECT_ONLY_PROXY_NOT_ENABLED'
        except Exception as exc:
            report['audio'] = 'PASS' if result.get('local_file') else 'NOT_VERIFIED'
            report['result'] = getattr(exc, 'code', 'DELIVERY_OR_RUNTIME_FAILURE')
            # No raw subprocess error, credentials, cookie contents or argv.
            report['reason'] = m._safe_proxy_error(str(exc)) if isinstance(exc, m.DownloadFailure) else 'Worker failed after stage ' + result.get('status', 'download')
        finally:
            remote = result.get('drive_path')
            if result.get('status') == 'delivered' and owns_qa_drive_file(remote, nonce, m.DRIVE_REMOTE, m.DRIVE_BASE, m.safe_folder):
                clean = subprocess.run([state['rclone'], '--config', m.RCLONE_CONFIG, 'deletefile', remote],
                                       capture_output=True, timeout=60)
                report['qa_remote_cleanup'] = 'PASS' if clean.returncode == 0 else 'PENDING'
        print(json.dumps(report, sort_keys=True))
        return 0 if report['result'] in ('PASS', 'DIRECT_ONLY_PROXY_NOT_ENABLED', 'YOUTUBE_AUTH_REQUIRED',
                                        'YOUTUBE_PROVIDER_CHALLENGE', 'YOUTUBE_RATE_LIMIT', 'YOUTUBE_ACCESS_DENIED',
                                        'PROXY_AUTH_FAILED', 'PROXY_CONNECTION_FAILED', 'SESSION_CONFIGURATION') else 1


if __name__ == '__main__':
    sys.exit(main(Path(sys.argv[1])))

"""Install the real existing queue workers, preserving their native leases."""
from __future__ import annotations

import hashlib
import json
import os
import json
from pathlib import Path
import subprocess
import time
import urllib.request

from production.runtime import BASE, CONFIG, DATA, event, run

SPECS = {
 'auto-caption-studio': [
   ('acs-ingress', 'part01', '-m web_backend.pipeline_v3'),
   ('acs-render', 'part01', '-m web_backend.stage_worker render'),
   ('acs-compress', 'part01', '-m web_backend.stage_worker compress'),
   ('acs-upload', 'part01', '-m web_backend.stage_worker upload')],
 'local-upload-manager': [
   ('lum-worker', 'part01', '-m lum_engine.auto_engine_v6_worker'),
   ('lum-scheduler', 'part01', '-m lum_engine.auto_engine_v6_scheduler'),
   # Newly provisioned, independent web+worker+scheduler processes. Each
   # instance has a different data root and starts with scheduling OFF.
   ('lum-engine2-web', 'part01', '-m uvicorn lum_engine.standalone_app:app --host 127.0.0.1 --port 8016'),
   ('lum-engine2-worker', 'part01', '-m lum_engine.auto_engine_v6_worker'),
   ('lum-engine2-scheduler', 'part01', '-m lum_engine.auto_engine_v6_scheduler'),
   ('lum-shorts-web', 'part01', '-m uvicorn lum_engine.standalone_app:app --host 127.0.0.1 --port 8017'),
   ('lum-shorts-worker', 'part01', '-m lum_engine.auto_engine_v6_worker'),
   ('lum-shorts-scheduler', 'part01', '-m lum_engine.auto_engine_v6_scheduler'),
   ('lum-song-web', 'part01', '-m uvicorn lum_engine.standalone_app:app --host 127.0.0.1 --port 8018'),
   ('lum-song-worker', 'part01', '-m lum_engine.auto_engine_v6_worker'),
   ('lum-song-scheduler', 'part01', '-m lum_engine.auto_engine_v6_scheduler')],
 'voice-studio': [
   ('edge-worker', 'part03', 'edge_worker.py'),
   ('kokoro-worker', 'part02', 'kokoro_worker.py'),
   ('voice-dispatcher', 'part01', 'voice_clone_dispatcher.py'),
   ('voice-drive-spooler', 'part01', 'voice_clone_drive_spooler.py')],
}


# The new engines NEVER reuse Engine 1's database, OAuth directory or queue.
MODE_ROOTS = {"engine2": "lum2", "shorts": "lum3", "song": "lum-song"}
MODE_MARKER = ".vps1-independent-engine"


def prepare_mode_roots() -> None:
    """Provision only empty/unowned isolated roots, never inspect or migrate history."""
    base = DATA / "local-upload-manager"
    for mode, leaf in MODE_ROOTS.items():
        root = base / leaf
        if root.is_symlink():
            raise RuntimeError("Mode root symlink denied")
        marker = root / MODE_MARKER
        if root.exists():
            if not root.is_dir():
                raise RuntimeError("Mode root must be a real directory")
            if marker.is_file():
                if marker.read_text().strip() != mode:
                    raise RuntimeError("Mode root ownership mismatch")
            elif any(root.iterdir()):
                raise RuntimeError("Existing unowned mode data must not be touched")
        else:
            root.mkdir(parents=True, mode=0o700)
        if not marker.exists():
            marker.write_text(mode + "\n")
        os.chown(root, 1000, 1000)
        os.chown(marker, 1000, 1000)
        root.chmod(0o700)
        marker.chmod(0o600)


def mode_exec(key: str, python: Path, entry: str) -> str:
    # /usr/bin/env wins over the parent EnvironmentFile values for this ExecStart.
    mode = "engine2" if key.startswith("lum-engine2-") else (
        "shorts" if key.startswith("lum-shorts-") else ("song" if key.startswith("lum-song-") else "")
    )
    if not mode:
        return f"{python} {entry}"
    root = DATA / "local-upload-manager" / MODE_ROOTS[mode]
    params = [
        f"LUM_DATA_DIR={root}",
        f"YOUTUBE_UPLOAD_MANAGER_DATA_DIR={root / 'core'}",
        f"LUM_AUTO_ENGINE_V6_ROOT={root / 'auto_engine_v6'}",
        f"LUM_GOOGLE_OAUTH_ROOT={root / 'secure'}",
        f"LUM_BASE_PATH=/local-upload-manager/modes/{mode}",
        f"LUM_ENGINE_INSTANCE={mode}",
        "LUM_SUITE_AUTH=1",
    ]
    return "/usr/bin/env " + " ".join(params) + f" {python} {entry}"


def unit_names(app):
    return ['vps1-production-' + key + '.service' for key, _, _ in SPECS.get(app, [])]


def snapshot(app):
    output = {}
    for name in unit_names(app):
        path = Path('/etc/systemd/system') / name
        output[name] = {'text': path.read_text() if path.is_file() else None,
                        'active': subprocess.run(['systemctl', 'is-active', '--quiet', name]).returncode == 0}
    return output


def stop(app):
    for name in unit_names(app):
        subprocess.run(['systemctl', 'stop', name], capture_output=True)


def restore(saved):
    for name, item in saved.items():
        subprocess.run(['systemctl', 'stop', name], capture_output=True)
        path = Path('/etc/systemd/system') / name
        if item['text'] is None:
            subprocess.run(['systemctl', 'disable', name], capture_output=True)
            path.unlink(missing_ok=True)
        else:
            path.write_text(item['text'])
    run(['systemctl', 'daemon-reload'], quiet=True)
    for name, item in saved.items():
        if item['active']:
            run(['systemctl', 'start', name], quiet=True)


def unit(app, revision, key, part, entry):
    python = BASE / 'venvs' / app / revision / 'bin/python'
    dependency = 'Wants' if app == 'voice-studio' and key in ('edge-worker', 'kokoro-worker') else 'Requires'
    return f'''[Unit]
Description=VPS1 {key}
After=vps1-production-{app}.service
{dependency}=vps1-production-{app}.service
StartLimitIntervalSec=120
StartLimitBurst=5
[Service]
User=ubuntu
Group=ubuntu
WorkingDirectory={BASE}/current/{app}/{part}
EnvironmentFile={CONFIG}/production-{app}.env
Environment=PATH={python.parent}:/usr/local/bin:/usr/bin:/bin
Environment=QT_QPA_PLATFORM=offscreen
Environment=OMP_NUM_THREADS=2
Environment=HF_HOME={DATA}/{app}/model-cache
Environment=XDG_CACHE_HOME={DATA}/{app}/cache
ExecStart={mode_exec(key, python, entry)}
Restart=on-failure
RestartSec=3
TimeoutStopSec=40
UMask=0077
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=yes
ReadWritePaths={DATA}/{app} -/home/ubuntu/.config/masterctl
[Install]
WantedBy=multi-user.target
'''


def model_files(models):
    models.mkdir(parents=True, exist_ok=True)
    files = [('kokoro-v1.0.int8.onnx', 92361271), ('voices-v1.0.bin', 28214398)]
    records = {}
    for filename, size in files:
        path = models / filename
        if not path.is_file() or path.stat().st_size != size:
            url = 'https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/' + filename
            temporary = path.with_suffix(path.suffix + '.download')
            with urllib.request.urlopen(url, timeout=180) as response, temporary.open('wb') as stream:
                while chunk := response.read(1024 * 1024):
                    stream.write(chunk)
            if temporary.stat().st_size != size:
                raise RuntimeError('official Kokoro model size mismatch')
            os.replace(temporary, path)
        path.chmod(0o644)
        records[filename] = hashlib.sha256(path.read_bytes()).hexdigest()
    (models / 'source-manifest.json').write_text(json.dumps(records, sort_keys=True) + '\n')
    return files


def prepare_kokoro(revision):
    models = DATA / 'voice-studio/kokoro/models'
    files = model_files(models)
    python = BASE / 'venvs/voice-studio' / revision / 'bin/python'
    output = DATA / 'voice-studio/kokoro/installation-check.wav'
    code = ('from kokoro_onnx import Kokoro; import soundfile as sf; '
            f'e=Kokoro({str(models / files[0][0])!r},{str(models / files[1][0])!r}); '
            "a,s=e.create('The voice service is ready.',voice='af_heart',speed=1.0,lang='en-us'); "
            f'sf.write({str(output)!r},a,s); assert len(a)>1000')
    run(['runuser', '-u', 'ubuntu', '--', python, '-c', code], timeout=180, quiet=True)
    event(kokoro_cpu_synthesis='PASS', model_bytes=sum(size for _, size in files), output_bytes=output.stat().st_size)


def wait_for_active_units(names, *, attempts=18, interval=1.0) -> None:
    """Check complete systemd readiness, identify the precise unit on failure.

    Restart=on-failure can briefly expose 'activating' between Python startup
    and service stabilization. This replaces the old one-shot 2-second check.
    No user databases, history or tokens are inspected by this check.
    """
    for name in names:
        last = "unknown"
        for attempt in range(attempts):
            status = subprocess.run(
                ["systemctl", "is-active", name],
                capture_output=True, text=True, check=False,
            )
            last = status.stdout.strip()[:24] or "unknown"
            if status.returncode == 0 and last == "active":
                break
            if attempt < attempts - 1:
                time.sleep(interval)
        else:
            detail = subprocess.run(
                ["systemctl", "show", name,
                 "--property=ActiveState,SubState,Result,ExecMainStatus"],
                capture_output=True, text=True, check=False,
            ).stdout.strip().replace("\n", " ")
            raise RuntimeError(
                f"Auto Uploader systemd unit {name} did not stabilize "
                f"(state={last}; {detail[:240]})"
            )



def install(app, revision):
    if app not in SPECS:
        return
    if app == 'local-upload-manager':
        prepare_mode_roots()
    if app == 'voice-studio':
        prepare_kokoro(revision)
    for key, part, entry in SPECS[app]:
        name = 'vps1-production-' + key + '.service'
        path = Path('/etc/systemd/system') / name
        path.write_text(unit(app, revision, key, part, entry))
        run(['systemd-analyze', 'verify', path], quiet=True)
    run(['systemctl', 'daemon-reload'], quiet=True)
    for name in unit_names(app):
        run(['systemctl', 'enable', name], quiet=True)
        run(['systemctl', 'restart', name], quiet=True)
    if app == 'local-upload-manager':
        wait_for_active_units(unit_names(app))
    else:
        time.sleep(2)
        for name in unit_names(app):
            run(['systemctl', 'is-active', '--quiet', name], quiet=True)
    if app == "local-upload-manager":
        # An active unit is not enough: require the correct app/engine response
        # before treating the independent mode installation as successful.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for mode, port in (("engine2", 8016), ("shorts", 8017), ("song", 8018)):
            address = f"http://127.0.0.1:{port}/local-upload-manager/modes/{mode}/health"
            last = None
            for attempt in range(25):
                try:
                    with opener.open(address, timeout=3) as response:
                        body = json.loads(response.read(2048))
                    if response.status == 200 and body.get("ok") is True and body.get("engine_instance") == mode:
                        last = None
                        break
                    last = "unexpected engine identity"
                except (OSError, ValueError, TypeError) as exc:
                    last = type(exc).__name__
                time.sleep(1)
            if last:
                raise RuntimeError(f"Independent {mode} health failed: {last}")
            # The health endpoint can be green while the embedded uploader
            # fails during app_page() rendering (historical false PASS).
            page_url = (
                f"http://127.0.0.1:{port}/local-upload-manager/modes/"
                f"{mode}/app?embedded=1&tab=auto_engine"
            )
            page_request = urllib.request.Request(page_url, headers={
                "X-Auth-User": "vps1-release-probe",
                "X-App-Access": "lum",
                "X-Is-Admin": "0",
                "Accept": "text/html",
            })
            try:
                with opener.open(page_request, timeout=7) as response:
                    page = response.read(1_500_000)
                    if (response.status != 200 or
                            "text/html" not in response.headers.get("Content-Type", "") or
                            b"Auto Engine" not in page or
                            b"Internal Server Error" in page):
                        raise RuntimeError("embedded uploader page not healthy")
            except (OSError, ValueError, TypeError, RuntimeError) as exc:
                raise RuntimeError(
                    f"Independent {mode} embedded HTML failed: "
                    f"{type(exc).__name__}"
                ) from exc
            event(app=app, isolated_engine=mode, health="PASS",
                  embedded_html="PASS", port=port)
    event(app=app, queue_workers='ACTIVE', workers=unit_names(app))

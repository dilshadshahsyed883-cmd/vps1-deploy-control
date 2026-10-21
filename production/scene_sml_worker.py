"""Pair the existing owned Scene renderer with SML in an idle-only transaction."""
from __future__ import annotations
import json
from pathlib import Path
import subprocess
import time
from production.scene_production_worker import (
    BASE, ENV, RECORD, STATE, UNIT, UNIT_FILE, URL, VENV,
    ENTRYPOINT, entries, health, require, unit_text,
)
from production.scene_worker_home import database_idle, expected_units, _atomic_unit
from production.scene_whisper_compat import idle_worker


class SceneSMLWorker:
    def __init__(self, source: Path, token: str):
        self.source, self.token = source, token
        self.original_env = self.original_unit = self.original_record = None
        self.changed = self.stopped = False
        self.auth = ''

    def _run(self, args, timeout=60):
        return subprocess.run(args, check=True, capture_output=True, text=True,
                              timeout=timeout).stdout.strip()

    def _ready(self):
        for _ in range(60):
            result = health(URL, self.auth)
            if result.get('ok') is True and result.get('admission_only') is False:
                return result
            time.sleep(.5)
        raise RuntimeError('SML renderer health verification failed')

    def _verify(self):
        self._ready()
        pid = self._run(['systemctl', 'show', '--property=MainPID', '--value', UNIT])
        require(pid.isdecimal() and int(pid) > 1, 'renderer process missing')
        values = dict(x.split(b'=', 1) for x in (Path('/proc') / pid / 'environ').read_bytes().split(b'\0') if b'=' in x)
        require(values.get(b'SCENE_SML_EDITOR_TOKEN') == self.token.encode() and
                values.get(b'SCENE_SML_URL') == b'http://127.0.0.1:8147',
                'actual renderer process lacks paired SML credentials')
        return {'renderer_token_pair': 'PASS', 'renderer_source': self.source.name}

    def install(self):
        from production.sml_scene_activation import _ensure_private, _patched, _atomic_replace
        record = json.loads(RECORD.read_text())
        require(record.get('owner') == 'scene-production-worker-v1' and
                record.get('unit') == UNIT and record.get('state') == str(STATE),
                'renderer ownership mismatch')
        legacy, desired_old = expected_units(Path(record['source']))
        self.original_env = _ensure_private(ENV)
        self.original_unit = UNIT_FILE.read_text()
        self.original_record = _ensure_private(RECORD)
        require(self.original_unit in {legacy, desired_old}, 'unrecognized renderer unit')
        config = entries(self.original_env.decode())
        self.auth = config['SCENE_WORKER_TOKEN']
        require(config.get('SCENE_WORKER_DATA') == str(STATE), 'renderer state mismatch')
        new_env = _patched(self.original_env, {'SCENE_SML_EDITOR_TOKEN': self.token,
                                            'SCENE_SML_URL': 'http://127.0.0.1:8147'})
        # expected_units also fences the immutable source path before creating a runner.
        _, desired = expected_units(self.source)
        runner = BASE / 'releases/scene-production-worker' / self.source.name
        if runner.exists():
            require((runner/'vps_engine').is_symlink() and
                    (runner/'vps_engine').resolve() == self.source/'part02' and
                    (runner/'entry.py').read_text() == ENTRYPOINT, 'unowned source runner')
        else:
            runner.mkdir(parents=True)
            (runner/'vps_engine').symlink_to(self.source/'part02', target_is_directory=True)
            (runner/'entry.py').write_text(ENTRYPOINT)
        if new_env == self.original_env and self.original_unit == desired:
            return self._verify()
        idle_worker(health(URL, self.auth))
        self.stopped = True
        self._run(['systemctl', 'stop', UNIT])
        database_idle()  # Catch submissions racing the health preflight.
        self.changed = True
        _atomic_replace(ENV, new_env)
        _atomic_unit(desired)
        record['source'] = str(self.source)
        _atomic_replace(RECORD, json.dumps(record).encode())
        self._run(['systemctl', 'daemon-reload'])
        self._run(['systemctl', 'start', UNIT]); self.stopped = False
        return self._verify()

    def render_proof(self):
        # Use the real authenticated catalog/acquisition and existing V2 renderer,
        # as the worker user, without adding or altering any user project.
        code = Path(__file__).with_name('scene_sml_render_proof.py').resolve()
        env = entries(ENV.read_text())
        args = ['/usr/sbin/runuser', '-u', 'vps1scene', '--', '/usr/bin/env']
        args += [k+'='+v for k,v in env.items()]
        args += ['HOME='+str(STATE/'worker-home'), 'TMPDIR='+str(STATE/'tmp'),
                 str(VENV/'bin/python'), str(code), str(self.source/'part02'), str(STATE)]
        result = json.loads(self._run(args, timeout=600))
        require(result.get('sml_scenes', 0) >= 1 and result.get('scene_count') == 3
                and result.get('video_codec') == 'h264'
                and result.get('audio_codec') == 'aac', 'real SML three-scene render failed')
        print('SML_THREE_SCENE_RENDER='+json.dumps(result, sort_keys=True), flush=True)
        return result

    def rollback(self):
        from production.sml_scene_activation import _atomic_replace
        if self.changed:
            if not self.stopped:
                idle_worker(health(URL, self.auth))
                self.stopped = True
                self._run(['systemctl', 'stop', UNIT])
            database_idle()
            _atomic_replace(ENV, self.original_env)
            _atomic_unit(self.original_unit)
            _atomic_replace(RECORD, self.original_record)
            self._run(['systemctl', 'daemon-reload'])
        if self.stopped:
            self._run(['systemctl', 'start', UNIT]); self.stopped = False
            self._ready()

"""Repair only the owned Scene production worker's unwritable service home.

The existing failed project, model cache, private environment and admission-only
worker are not modified. All restarts are gated on an empty active queue.
"""
from __future__ import annotations

import importlib.metadata
import json
import os
from pathlib import Path
import pwd
import re
import sqlite3
import subprocess
import tempfile
import time

from production.scene_production_worker import (
    BASE, ENV, RECORD, STATE, UNIT, UNIT_FILE, URL, VENV,
    entries, health, require, unit_text,
)
from production.scene_whisper_compat import idle_worker, TERMINAL

HOME = STATE / 'worker-home'
CONFIG = HOME / '.config'
TMP = STATE / 'tmp'
HOME_LINES = (
    f'Environment=HOME={HOME}\n'
    f'Environment=XDG_CONFIG_HOME={CONFIG}\n'
    f'Environment=TMPDIR={TMP}\n'
)


def expected_units(source: Path) -> tuple[str, str]:
    """Return only the owned legacy unit and its minimal HOME repair."""
    require(source.is_absolute() and
            source.is_relative_to(BASE / 'releases/scene-builder') and
            re.fullmatch('[a-f0-9]{40}', source.name),
            'unrecognized Scene worker source ownership')
    runner = BASE / 'releases/scene-production-worker' / source.name
    desired = unit_text(runner)
    require(desired.count(HOME_LINES) == 1, 'unit HOME template drift')
    legacy = desired.replace(HOME_LINES, '')
    return legacy, desired


def prepare_home_dirs() -> None:
    """Create only missing worker-owned directories under the existing state."""
    require(STATE.is_dir() and not STATE.is_symlink(), 'Scene state path unavailable')
    owner = pwd.getpwnam('vps1scene')
    uid, gid = owner.pw_uid, owner.pw_gid
    for path in (HOME, CONFIG, TMP):
        require(path.parent.resolve().is_relative_to(STATE.resolve()),
                'worker cache path escaped Scene state')
        if path.is_symlink():
            raise RuntimeError('Scene worker writable path is a symlink')
        if path.exists():
            require(path.is_dir() and path.stat().st_uid == uid,
                    'preexisting Scene worker writable directory owner mismatch')
        else:
            path.mkdir(mode=0o700)
            os.chown(path, uid, gid)
            path.chmod(0o700)


def database_idle() -> None:
    with sqlite3.connect('file:' + str(STATE / 'queue.sqlite3') + '?mode=ro', uri=True) as db:
        counts = dict(db.execute('SELECT status,count(*) FROM jobs GROUP BY status').fetchall())
    require(not any(int(n) for s, n in counts.items() if s not in TERMINAL),
            'Scene submission raced worker maintenance')


def _atomic_unit(payload: str) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix='.scene-worker-home-', dir=UNIT_FILE.parent)
    try:
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, UNIT_FILE)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class SceneWorkerHome:
    def __init__(self, journal: Path):
        self.journal = Path(journal) / 'scene-worker-home'
        self.previous: str | None = None
        self.changed = False
        self.stopped = False
        self.token = ''

    def _run(self, command, timeout=50):
        return subprocess.run(command, check=True, capture_output=True,
                              text=True, timeout=timeout).stdout.strip()

    def _ready(self):
        for _ in range(60):
            payload = health(URL, self.token)
            if payload.get('ok') is True and payload.get('admission_only') is False:
                return payload
            time.sleep(.5)
        raise RuntimeError('Scene worker did not recover after HOME repair')

    def _verify(self):
        self._ready()
        pid_text = self._run(['systemctl', 'show', '--property=MainPID', '--value', UNIT])
        require(pid_text.isdecimal() and int(pid_text) > 1,
                'Scene worker process unavailable for environment proof')
        environ = Path('/proc') / pid_text / 'environ'
        variables = dict(part.split(b'=', 1) for part in environ.read_bytes().split(b'\0')
                         if b'=' in part)
        required = {
            b'HOME': str(HOME).encode(),
            b'XDG_CONFIG_HOME': str(CONFIG).encode(),
            b'TMPDIR': str(TMP).encode(),
            b'HF_HOME': str(STATE / 'model-cache').encode(),
            b'XDG_CACHE_HOME': str(STATE / 'cache').encode(),
        }
        require(all(variables.get(k) == v for k, v in required.items()),
                'Scene worker actual process lacks writable HOME/cache environment')
        versions = json.loads(self._run([
            str(VENV / 'bin/python'), '-c',
            "import json,importlib.metadata as m;"
            "print(json.dumps({'av':m.version('av'),"
            "'faster_whisper':m.version('faster-whisper')}))"
        ]))
        require(versions == {'av': '16.1.0', 'faster_whisper': '1.2.0'},
                'production decoder versions changed unexpectedly')
        return {
            'worker_home': 'WRITABLE_SCENE_STATE',
            'actual_process_env': 'PASS',
            'av': versions['av'],
            'faster_whisper': versions['faster_whisper'],
            'project_retry': 'NONE',
        }

    def install(self):
        require(os.geteuid() == 0, 'protected Scene release required')
        record = json.loads(RECORD.read_text())
        require(record.get('owner') == 'scene-production-worker-v1' and
                record.get('unit') == UNIT and record.get('state') == str(STATE),
                'Scene worker ownership mismatch')
        legacy, desired = expected_units(Path(record['source']))
        self.previous = UNIT_FILE.read_text()
        require(self.previous in {legacy, desired},
                'unrecognized existing production worker systemd unit')
        credentials = entries(ENV.read_text())
        require(credentials.get('HF_HOME') == str(STATE / 'model-cache') and
                credentials.get('XDG_CACHE_HOME') == str(STATE / 'cache') and
                bool(credentials.get('SCENE_WORKER_TOKEN')),
                'Scene model cache environment mismatch')
        for name, value in (('HOME', HOME), ('XDG_CONFIG_HOME', CONFIG), ('TMPDIR', TMP)):
            require(name not in credentials or credentials[name] == str(value),
                    'private environment overrides protected worker HOME')
        self.token = credentials['SCENE_WORKER_TOKEN']
        prepare_home_dirs()
        if self.previous == desired:
            result = self._verify()
            print('SCENE_WORKER_HOME_ALREADY_VERIFIED=PASS', flush=True)
            return result

        idle_worker(health(URL, self.token))
        self.journal.mkdir(parents=True, exist_ok=False, mode=0o700)
        backup = self.journal / 'original-unit'
        with backup.open('x', encoding='utf-8') as stream:
            stream.write(self.previous)
        backup.chmod(0o600)
        idle_worker(health(URL, self.token))
        self.stopped = True
        self._run(['systemctl', 'stop', UNIT])
        database_idle()  # Detect submissions that raced the first idle check.
        self.changed = True
        _atomic_unit(desired)
        self._run(['systemctl', 'daemon-reload'])
        self._run(['systemd-analyze', 'verify', str(UNIT_FILE)])
        self._run(['systemctl', 'start', UNIT])
        self.stopped = False
        result = self._verify()
        print('SCENE_WORKER_HOME_REPAIR=PASS ACTUAL_PROCESS=VERIFIED', flush=True)
        return result

    def rollback(self):
        if self.previous is None:
            return
        if self.changed:
            if not self.stopped:
                idle_worker(health(URL, self.token))
                self.stopped = True
                self._run(['systemctl', 'stop', UNIT])
            database_idle()
            _atomic_unit(self.previous)
            self._run(['systemctl', 'daemon-reload'])
        if self.stopped:
            self._run(['systemctl', 'start', UNIT])
            self.stopped = False
            self._ready()
        print('SCENE_WORKER_HOME_ROLLBACK=PASS PROJECTS=PRESERVED', flush=True)

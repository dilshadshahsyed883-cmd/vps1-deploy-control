from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

ROOT = Path(os.environ.get('SONG_MAKER_DATA', '/var/lib/fresh-website/song-maker'))
ACTIVE = {'Preparing Inputs', 'Rendering', 'Validating MP4', 'Drive Uploading', 'Verifying Drive Upload'}
TERMINAL = {'Completed', 'Failed', 'Cancelled', 'Waiting for Drive', 'Needs Attention'}


class Store:
    def __init__(self, root=ROOT):
        self.root = Path(root)
        for name in ('inputs', 'temp', 'outputs', 'staging', 'logs', 'state'):
            (self.root / name).mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / 'state/jobs.sqlite3'
        with self.db() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY, owner TEXT NOT NULL, source_key TEXT NOT NULL,
              stage TEXT NOT NULL, progress INTEGER DEFAULT 0, created REAL NOT NULL,
              updated REAL NOT NULL, cancel INTEGER DEFAULT 0, deleted INTEGER DEFAULT 0,
              lane TEXT DEFAULT '', data TEXT NOT NULL,
              UNIQUE(owner, source_key));
            CREATE INDEX IF NOT EXISTS pending ON jobs(stage, created);
            CREATE TABLE IF NOT EXISTS settings (owner TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sync (owner TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS workers (lane TEXT PRIMARY KEY, heartbeat REAL NOT NULL);
            ''')

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.path, timeout=20)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def unpack(self, row):
        if row is None:
            return None
        item = dict(row)
        item.update(json.loads(item.pop('data')))
        return item

    def get(self, job_id):
        with self.db() as db:
            return self.unpack(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())

    def jobs(self, owner):
        with self.db() as db:
            return [self.unpack(r) for r in db.execute(
                'SELECT * FROM jobs WHERE owner=? AND deleted=0 ORDER BY created DESC', (owner,))]

    def enqueue(self, owner, source, data):
        key = hashlib.sha256(json.dumps(source, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        now = time.time()
        job_id = uuid.uuid4().hex
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO jobs(id,owner,source_key,stage,created,updated,data) '
                       'VALUES(?,?,?,?,?,?,?)', (job_id, owner, key, 'Queued', now, now,
                                                 json.dumps(data, ensure_ascii=False)))
            return self.unpack(db.execute('SELECT * FROM jobs WHERE owner=? AND source_key=?',
                                          (owner, key)).fetchone())

    def update(self, job_id, **updates):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            job = self.unpack(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())
            if not job:
                raise KeyError(job_id)
            columns = {'stage', 'progress', 'cancel', 'deleted', 'lane'}
            if updates.get('stage') == 'Ready to Upload' and job['cancel']:
                updates['stage'] = 'Cancelled'
            values = {k: updates.pop(k) for k in list(updates) if k in columns}
            extra = {k: v for k, v in job.items() if k not in {
                'id', 'owner', 'source_key', 'created', 'updated', *columns}}
            extra.update(updates)
            values.update(data=json.dumps(extra, ensure_ascii=False), updated=time.time())
            db.execute('UPDATE jobs SET '+','.join(k+'=?' for k in values)+' WHERE id=?',
                       (*values.values(), job_id))
        return self.get(job_id)

    def claim(self, lane):
        ready = ('Queued',) if lane == 'render' else ('Ready to Upload',)
        stage = 'Preparing Inputs' if lane == 'render' else 'Drive Uploading'
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM jobs WHERE stage=? AND cancel=0 AND deleted=0 '
                             'ORDER BY created,id LIMIT 1', ready).fetchone()
            if not row:
                return None
            db.execute('UPDATE jobs SET stage=?,lane=?,updated=? WHERE id=?',
                       (stage, lane, time.time(), row['id']))
        return self.get(row['id'])

    def recover(self, lane):
        # Called ONLY with the exclusive, lifetime OS lock for this lane held.
        # No expiring lease can steal work from a slow but still-live process.
        with self.db() as db:
            db.execute("UPDATE jobs SET stage=CASE WHEN cancel=1 THEN 'Cancelled' "
                       "WHEN lane='render' THEN 'Queued' ELSE 'Ready to Upload' END, lane='' "
                       "WHERE lane=?", (lane,))

    def record(self, table, owner, data=None):
        if table not in ('settings', 'sync'):
            raise ValueError('invalid record')
        with self.db() as db:
            if data is not None:
                db.execute(f'INSERT INTO {table}(owner,data) VALUES(?,?) ON CONFLICT(owner) '
                           'DO UPDATE SET data=excluded.data', (owner, json.dumps(data, ensure_ascii=False)))
            row = db.execute(f'SELECT data FROM {table} WHERE owner=?', (owner,)).fetchone()
            return json.loads(row[0]) if row else {}

    def pulse(self, lane):
        with self.db() as db:
            db.execute('INSERT INTO workers VALUES(?,?) ON CONFLICT(lane) DO UPDATE '
                       'SET heartbeat=excluded.heartbeat', (lane, time.time()))

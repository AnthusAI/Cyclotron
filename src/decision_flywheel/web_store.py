"""SQLite catalog and append-only API history; one connection per operation."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from uuid import uuid4


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


class WebStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS web_runs (
                  id TEXT PRIMARY KEY, name TEXT NOT NULL, mode TEXT NOT NULL,
                  status TEXT NOT NULL, created_at TEXT NOT NULL, config TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS web_events (
                  sequence INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES web_runs(id),
                  source_id TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(run_id,source_id));
                CREATE INDEX IF NOT EXISTS web_events_run ON web_events(run_id,sequence);
                CREATE TABLE IF NOT EXISTS web_jobs (
                  id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES web_runs(id),
                  request_id TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
                  status TEXT NOT NULL, result TEXT, UNIQUE(run_id,request_id));
                CREATE TABLE IF NOT EXISTS web_items (
                  run_id TEXT NOT NULL REFERENCES web_runs(id), id TEXT NOT NULL,
                  payload TEXT NOT NULL, prediction TEXT, reviewed INTEGER NOT NULL DEFAULT 0,
                  PRIMARY KEY(run_id,id));
                CREATE TABLE IF NOT EXISTS web_summaries (
                  run_id TEXT PRIMARY KEY REFERENCES web_runs(id), payload TEXT NOT NULL);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _run(row):
        return {**dict(row), 'config': json.loads(row['config'])}

    def create_run(self, name, mode, config, *, items=()):
        if not name.strip() or mode not in ('live', 'recorded'):
            raise ValueError('name and supported run mode required')
        run_id = str(uuid4())
        with self.connect() as db:
            db.execute('INSERT INTO web_runs VALUES (?,?,?,?,?,?)',
                (run_id, name, mode, 'ready', datetime.now(timezone.utc).isoformat(), encode(config)))
            for item in items:
                db.execute('INSERT INTO web_items(run_id,id,payload) VALUES (?,?,?)', (run_id,item['id'],encode(item)))
        return self.run(run_id)

    def run(self, run_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM web_runs WHERE id=?', (run_id,)).fetchone()
        if row is None:
            raise ValueError('unknown run')
        return self._run(row)

    def runs(self):
        with self.connect() as db:
            return [self._run(r) for r in db.execute('SELECT * FROM web_runs ORDER BY rowid DESC')]

    def counts(self, run_id):
        self.run(run_id)
        with self.connect() as db:
            counts = dict(db.execute("SELECT json_extract(payload,'$.kind'),COUNT(*) FROM web_events WHERE run_id=? GROUP BY json_extract(payload,'$.kind')",(run_id,)))
        return {name:counts.get(kind,0) for name,kind in (
            ('cycles','cycle-started'),('predictions','prediction'),('labels','human-feedback'),('optimizations','optimizer-request'))}

    def set_status(self, run_id, status):
        self.run(run_id)
        with self.connect() as db:
            db.execute('UPDATE web_runs SET status=? WHERE id=?', (status,run_id))

    def summary(self, run_id):
        self.run(run_id)
        with self.connect() as db:
            row = db.execute('SELECT payload FROM web_summaries WHERE run_id=?',(run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def complete_run(self, run_id, summary):
        self.run(run_id)
        data = encode(summary)
        with self.connect() as db:
            previous = db.execute('SELECT payload FROM web_summaries WHERE run_id=?',(run_id,)).fetchone()
            if previous and previous[0] != data:
                raise ValueError('completed run already has different summary')
            if not previous:
                db.execute('INSERT INTO web_summaries VALUES (?,?)',(run_id,data))
                event = {'kind':'run-completed','summary':summary,'created_at':datetime.now(timezone.utc).isoformat()}
                db.execute('INSERT INTO web_events(run_id,source_id,payload) VALUES (?,?,?)',(run_id,'run:completed',encode(event)))
            db.execute("UPDATE web_runs SET status='completed' WHERE id=?",(run_id,))
        return self.run(run_id)

    def append_batch(self, run_id, events):
        self.run(run_id)
        if not 1 <= len(events) <= 200:
            raise ValueError('ingest between one and 200 events')
        results = []
        with self.connect() as db:
            for source_id, payload in events:
                if not source_id or not isinstance(payload, dict) or not payload.get('kind'):
                    raise ValueError('event identity and kind required')
                data = encode(payload)
                if len(data.encode()) > 4_000_000:
                    raise ValueError('event exceeds size limit')
                row = db.execute('SELECT * FROM web_events WHERE run_id=? AND source_id=?', (run_id,source_id)).fetchone()
                if row and row['payload'] != data:
                    raise ValueError('event identity already has different content')
                if not row:
                    db.execute('INSERT INTO web_events(run_id,source_id,payload) VALUES (?,?,?)', (run_id,source_id,data))
                    row = db.execute('SELECT * FROM web_events WHERE run_id=? AND source_id=?', (run_id,source_id)).fetchone()
                results.append(self._event(row))
        return results

    def append_event(self, run_id, source_id, payload):
        return self.append_batch(run_id, [(source_id,payload)])[0]

    @staticmethod
    def _event(row):
        return {**dict(row), 'payload': json.loads(row['payload'])}

    def events(self, run_id, *, after=0, limit=1000):
        self.run(run_id)
        if not 1 <= limit <= 1000 or after < 0:
            raise ValueError('event page must be bounded')
        with self.connect() as db:
            return [self._event(r) for r in db.execute('SELECT * FROM web_events WHERE run_id=? AND sequence>? ORDER BY sequence LIMIT ?',
                (run_id,after,limit))]

    def all_events(self, run_id):
        events, cursor = [], 0
        while page := self.events(run_id, after=cursor):
            events.extend(page)
            cursor = page[-1]['sequence']
        return events

    def command(self, run_id, request_id, kind, payload):
        if self.run(run_id)['mode'] != 'live' or kind not in ('prepare','label','skip','undo') or not request_id:
            raise ValueError('only live runs accept supported labeling commands')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM web_jobs WHERE run_id=? AND request_id=?', (run_id,request_id)).fetchone()
            if row:
                if row['kind'] != kind or row['payload'] != encode(payload):
                    raise ValueError('command identity has different content')
                return self._job(row)
            if db.execute("SELECT 1 FROM web_jobs WHERE run_id=? AND status IN ('pending','running')", (run_id,)).fetchone():
                raise ValueError('run already has work in progress')
            job_id = str(uuid4())
            db.execute('INSERT INTO web_jobs VALUES (?,?,?,?,?,?,?)', (job_id,run_id,request_id,kind,encode(payload),'pending',None))
            row = db.execute('SELECT * FROM web_jobs WHERE id=?', (job_id,)).fetchone()
        return self._job(row)

    @staticmethod
    def _job(row):
        return {**dict(row), 'payload': json.loads(row['payload']), 'result':json.loads(row['result']) if row['result'] else None}

    def jobs(self, run_id):
        self.run(run_id)
        with self.connect() as db:
            return [self._job(r) for r in db.execute('SELECT * FROM web_jobs WHERE run_id=? ORDER BY rowid DESC LIMIT 20', (run_id,))]

    def claim_command(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM web_jobs WHERE status='pending' ORDER BY rowid LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE web_jobs SET status='running' WHERE id=?", (row['id'],))
        return {**self._job(row), 'status':'running'}

    def finish_command(self, job_id, status, result):
        with self.connect() as db:
            db.execute('UPDATE web_jobs SET status=?,result=? WHERE id=?', (status,encode(result),job_id))

    def recover_interrupted(self):
        with self.connect() as db:
            db.execute("UPDATE web_jobs SET status='interrupted' WHERE status IN ('running','pending')")

    def current_item(self, run_id):
        self.run(run_id)
        with self.connect() as db:
            row = db.execute('SELECT * FROM web_items WHERE run_id=? AND reviewed=0 AND prediction IS NOT NULL ORDER BY rowid LIMIT 1', (run_id,)).fetchone()
        return {'item':json.loads(row['payload']), 'prediction':json.loads(row['prediction'])} if row else None

    def update_item(self, run_id, item_id, *, prediction=None, reviewed=False):
        with self.connect() as db:
            cursor = db.execute('UPDATE web_items SET prediction=?,reviewed=? WHERE run_id=? AND id=?',
                               (encode(prediction) if prediction else None, reviewed, run_id,item_id))
            if cursor.rowcount != 1:
                raise ValueError('unknown item')

    def items(self, run_id):
        self.run(run_id)
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT payload FROM web_items WHERE run_id=? ORDER BY rowid', (run_id,))]

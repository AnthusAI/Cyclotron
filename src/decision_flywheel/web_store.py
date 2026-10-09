"""SQLite catalog and append-only API history; one connection per operation."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from uuid import uuid4
from .workspace_catalog import WorkspaceCatalog
from .cyclotrons import Cyclotrons, load_config
from .cyclotron_definitions import CyclotronDefinitions


_UNSET = object()


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


class WebStore(WorkspaceCatalog,Cyclotrons,CyclotronDefinitions):
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
        self.initialize_catalog()
        self.initialize_cyclotrons()
        self.initialize_cyclotron_definitions()

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
        return {**dict(row), 'config': load_config(row['config'])}

    def create_run(self, name, mode, config, *, items=(), frozen_feedback=(), initial_events=()):
        if not name.strip() or mode not in ('live', 'recorded'):
            raise ValueError('name and supported run mode required')
        run_id = str(uuid4())
        with self.connect() as db:
            db.execute('INSERT INTO web_runs VALUES (?,?,?,?,?,?)',
                (run_id, name, mode, 'ready', datetime.now(timezone.utc).isoformat(), encode(config)))
            for item in items:
                db.execute('INSERT INTO web_items(run_id,id,payload) VALUES (?,?,?)', (run_id,item['id'],encode(item)))
            for item_id,classifier_id,feedback in frozen_feedback:
                db.execute('INSERT INTO replay_feedback VALUES (?,?,?,?)',(run_id,item_id,classifier_id,encode(feedback)))
            for source_id,payload in initial_events:
                if not source_id or not isinstance(payload,dict) or not payload.get('kind'):
                    raise ValueError('event identity and kind required')
                db.execute('INSERT INTO web_events(run_id,source_id,payload) VALUES (?,?,?)',(run_id,source_id,encode(payload)))
            if config.get('cyclotron_definition_revision'):
                self.register_run_definition(db,run_id,config['cyclotron_id'],name,config)
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

    def run_cyclotron_id(self, run_id):
        """Resolve current family membership without rewriting a run snapshot."""
        with self.connect() as db:
            row=db.execute('SELECT r.config,v.cyclotron_id FROM web_runs r LEFT JOIN cyclotron_versions v ON v.run_id=r.id WHERE r.id=?',(run_id,)).fetchone()
        if row is None:raise ValueError('unknown run')
        return row['cyclotron_id'] or load_config(row['config']).get('cyclotron_id')

    def labeling_access(self, run_id):
        """Describe existing command eligibility without authorizing any work."""
        with self.connect() as db:
            run=db.execute('SELECT mode FROM web_runs WHERE id=?',(run_id,)).fetchone()
            if run is None:raise ValueError('unknown run')
            if run['mode']!='live':
                return {'allowed':False,'reason':'Recorded runs are read-only.'}
            edition=db.execute('SELECT v.revision,s.active_revision FROM cyclotron_versions v JOIN cyclotrons s ON s.id=v.cyclotron_id WHERE v.run_id=?',(run_id,)).fetchone()
            if edition and edition['revision']!=edition['active_revision']:
                return {'allowed':False,'reason':'Activate this cyclotron version before continuing labeling.'}
        return {'allowed':True,'reason':None}

    def counts(self, run_id):
        run=self.run(run_id)
        with self.connect() as db:
            counts = dict(db.execute("SELECT json_extract(payload,'$.kind'),COUNT(*) FROM web_events WHERE run_id=? GROUP BY json_extract(payload,'$.kind')",(run_id,)))
        result={name:counts.get(kind,0) for name,kind in (
            ('cycles','cycle-started'),('predictions','prediction'),('labels','human-feedback'),('optimizations','optimizer-request'))}
        if run['config'].get('classifiers'):
            with self.connect() as db:
                result['cycles']=db.execute("SELECT COUNT(DISTINCT json_extract(payload,'$.cycle_number')) FROM web_events WHERE run_id=? AND json_extract(payload,'$.kind')='cycle-started'",(run_id,)).fetchone()[0]
        return result

    def set_status(self, run_id, status):
        self.run(run_id)
        with self.connect() as db:
            db.execute('UPDATE web_runs SET status=? WHERE id=?', (status,run_id))

    def update_run_limits(self,run_id,max_requests,max_optimizer_calls):
        if any(type(value) is not int or value<1 for value in (max_requests,max_optimizer_calls)):
            raise ValueError('operating limits must be positive integers')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT config,mode FROM web_runs WHERE id=?',(run_id,)).fetchone()
            if row is None or row['mode']!='live':raise ValueError('only live runs accept operating limits')
            if db.execute("SELECT 1 FROM web_jobs WHERE run_id=? AND status IN ('pending','running')",(run_id,)).fetchone():
                raise ValueError('wait for current work before changing limits')
            config={**load_config(row['config']),'max_requests':max_requests,'max_optimizer_calls':max_optimizer_calls}
            db.execute('UPDATE web_runs SET config=? WHERE id=?',(encode(config),run_id))
        return self.run(run_id)

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

    def event_cursor(self,run_id):
        self.run(run_id)
        with self.connect() as db:
            return db.execute('SELECT COALESCE(MAX(sequence),0) FROM web_events WHERE run_id=?',(run_id,)).fetchone()[0]

    def command(self, run_id, request_id, kind, payload):
        if self.run(run_id)['mode'] != 'live' or kind not in ('prepare','label','correct','skip','undo','optimize','replay-next') or not request_id:
            raise ValueError('only live runs accept supported labeling commands')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            edition=db.execute('SELECT v.revision,s.active_revision FROM cyclotron_versions v JOIN cyclotrons s ON s.id=v.cyclotron_id WHERE v.run_id=?',(run_id,)).fetchone()
            if edition and edition['revision']!=edition['active_revision']:
                raise ValueError('activate this cyclotron version before continuing its labeling')
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

    def resume_feedback_command(self,run_id,job_id):
        """Explicitly recover cyclotron corrections/undo, never paid work."""
        run=self.run(run_id)
        if run['mode']!='live' or not run['config'].get('classifiers'):
            raise ValueError('feedback recovery requires a cyclotron run')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM web_jobs WHERE run_id=? AND id=?',(run_id,job_id)).fetchone()
            if row is None or row['kind'] not in ('correct','undo'):
                raise ValueError('only a cyclotron correction or undo command can be recovered')
            if row['status'] in ('pending','running','completed'):return self._job(row)
            if row['status'] not in ('failed','interrupted'):raise ValueError('command is not recoverable')
            edition=db.execute('SELECT v.revision,s.active_revision FROM cyclotron_versions v JOIN cyclotrons s ON s.id=v.cyclotron_id WHERE v.run_id=?',(run_id,)).fetchone()
            if edition and edition['revision']!=edition['active_revision']:
                raise ValueError('activate this cyclotron version before recovering its feedback')
            if db.execute("SELECT 1 FROM web_jobs WHERE run_id=? AND status IN ('pending','running')",(run_id,)).fetchone():
                raise ValueError('run already has work in progress')
            event={'kind':'feedback-command-resumed','job_id':job_id,'command':row['kind'],
                   'previous_status':row['status'],'previous_result':json.loads(row['result']) if row['result'] else None,
                   'created_at':datetime.now(timezone.utc).isoformat()}
            db.execute('INSERT INTO web_events(run_id,source_id,payload) VALUES (?,?,?)',
                       (run_id,f'feedback-recovery:{uuid4()}',encode(event)))
            db.execute("UPDATE web_jobs SET status='pending',result=NULL WHERE id=?",(job_id,))
            db.execute("UPDATE web_runs SET status='ready' WHERE id=?",(run_id,))
            return self._job(db.execute('SELECT * FROM web_jobs WHERE id=?',(job_id,)).fetchone())

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
            if status == 'completed' and isinstance(result, dict) and result.get('finished') is True:
                # Exhaustion is a durable runtime result, not a second commit
                # which can be lost after acknowledging the completed command.
                db.execute("""UPDATE web_runs SET status='completed' WHERE mode='live'
                    AND id=(SELECT run_id FROM web_jobs WHERE id=? AND rowid=(
                        SELECT MAX(newer.rowid) FROM web_jobs newer WHERE newer.run_id=web_jobs.run_id))""",
                    (job_id,))

    def recover_interrupted(self):
        with self.connect() as db:
            # Pending commands have never started: keep the user's durable
            # submission queued. Running work may have made paid calls or
            # partial changes and must not be retried automatically.
            db.execute("UPDATE web_jobs SET status='interrupted' WHERE status='running'")
            # Repair older split commits only from the latest successful command.
            # Newer pending, interrupted or failed work must remain visible.
            db.execute("""UPDATE web_runs SET status='completed' WHERE mode='live'
                AND status IN ('ready','working') AND EXISTS (
                    SELECT 1 FROM web_jobs job WHERE job.run_id=web_runs.id
                    AND job.rowid=(SELECT MAX(latest.rowid) FROM web_jobs latest WHERE latest.run_id=web_runs.id)
                    AND job.status='completed' AND json_valid(job.result)
                    AND json_type(job.result,'$.finished')='true')""")

    def current_item(self, run_id):
        self.run(run_id)
        with self.connect() as db:
            row = db.execute('SELECT * FROM web_items WHERE run_id=? AND reviewed=0 AND prediction IS NOT NULL ORDER BY rowid LIMIT 1', (run_id,)).fetchone()
        return {'item':json.loads(row['payload']), 'prediction':json.loads(row['prediction'])} if row else None

    def update_item(self, run_id, item_id, *, prediction=_UNSET, reviewed=_UNSET):
        """Apply only the fields the runtime explicitly changed.

        A command that records human feedback must not erase the prediction that
        was shown to that human. A command that prepares a prediction must not
        guess a review status.
        """
        assignments, values = [], []
        if prediction is not _UNSET:
            assignments.append('prediction=?')
            values.append(encode(prediction) if prediction is not None else None)
        if reviewed is not _UNSET:
            if type(reviewed) is not bool:
                raise ValueError('reviewed must be a boolean')
            assignments.append('reviewed=?')
            values.append(reviewed)
        if not assignments:
            return
        with self.connect() as db:
            cursor = db.execute(f"UPDATE web_items SET {','.join(assignments)} WHERE run_id=? AND id=?",
                               (*values, run_id, item_id))
            if cursor.rowcount != 1:
                raise ValueError('unknown item')

    def items(self, run_id):
        self.run(run_id)
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT payload FROM web_items WHERE run_id=? ORDER BY rowid', (run_id,))]

    def unreviewed_items(self,run_id):
        self.run(run_id)
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT payload FROM web_items WHERE run_id=? AND reviewed=0 ORDER BY rowid',(run_id,))]

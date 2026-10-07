"""Immutable scorecard editions and complete learned-state checkpoints."""
from datetime import datetime, timezone
from dataclasses import asdict
import hashlib
import json
from uuid import uuid4


def encode(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)


class Scorecards:
    def initialize_scorecards(self):
        with self.connect() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS scorecards(id TEXT PRIMARY KEY,name TEXT NOT NULL,active_revision INTEGER NOT NULL);
              CREATE TABLE IF NOT EXISTS scorecard_versions(scorecard_id TEXT,revision INTEGER,run_id TEXT UNIQUE,
                parent_run_id TEXT,created_at TEXT NOT NULL,PRIMARY KEY(scorecard_id,revision));
              CREATE TABLE IF NOT EXISTS replay_feedback(run_id TEXT,item_id TEXT,classifier_id TEXT,payload TEXT NOT NULL,
                PRIMARY KEY(run_id,item_id,classifier_id));
              CREATE TABLE IF NOT EXISTS scorecard_checkpoints(id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT,
                fingerprint TEXT,payload TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(run_id,fingerprint));
              CREATE TABLE IF NOT EXISTS matched_evaluation_inputs(run_id TEXT PRIMARY KEY REFERENCES web_runs(id),
                payload TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS matched_evaluation_authorizations(request_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES web_runs(id),payload TEXT NOT NULL);
            ''')

    def extend_scorecard(self,parent_run_id,classifier_ids,*,name):
        parent=self.run(parent_run_id)
        with self.connect() as db:
            if db.execute("SELECT 1 FROM web_jobs WHERE run_id=? AND status IN ('pending','running')",(parent_run_id,)).fetchone():
                raise ValueError('wait for current work before extending the scorecard')
        if parent['mode']!='live' or not parent['config'].get('classifiers'):
            raise ValueError('extend a live multi-classifier run')
        old=parent['config']['classifiers'];old_ids={row['id'] for row in old}
        if not classifier_ids or len(set(classifier_ids))!=len(classifier_ids) or old_ids.intersection(classifier_ids):
            raise ValueError('add distinct new classifiers')
        added=[self.classifier(identifier) for identifier in classifier_ids]
        for classifier in added:
            definition=classifier['config'];positive=next((row['label'] for row in definition['classes'] if row.get('role')=='positive'),None)
            classifier['config']={**definition,'selection_policy':definition.get('selection_policy',{'primary':'f1','aggregation':'positive' if positive else 'macro','positive_class':positive})}
        config={**parent['config'],'classifiers':[*old,*added],'parent_run_id':parent_run_id,
                'learning_policy':'fresh-replay','review_policy':'missing-labels-first'}
        config.pop('scorecard_definition_revision',None)
        config.pop('scorecard_definition_fingerprint',None)
        # Preserve the original class definitions, item revisions, split seed,
        # and comments. Never inherit fitted models or future labels.
        items=self.items(parent_run_id);inherited={}
        for item in items:
            votes=[row for row in self.item_labels(config['item_list_id'],item['id'],item['revision'])
                   if any(c['id']==row['classifier_id'] and c['revision']==row['classifier_revision'] for c in old)]
            if votes:inherited[item['id']]=votes
        historical=[row for row in items if row['id'] in inherited]
        unseen=[row for row in items if row['id'] not in inherited]
        run_id=str(uuid4());now=datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM web_jobs WHERE run_id=? AND status IN ('pending','running')",(parent_run_id,)).fetchone():
                raise ValueError('wait for current work before extending the scorecard')
            previous=db.execute('SELECT * FROM scorecard_versions WHERE run_id=?',(parent_run_id,)).fetchone()
            scorecard_id=previous['scorecard_id'] if previous else str(uuid4())
            if not previous:
                db.execute('INSERT INTO scorecards VALUES (?,?,1)',(scorecard_id,name))
                db.execute('INSERT INTO scorecard_versions VALUES (?,1,?,NULL,?)',(scorecard_id,parent_run_id,parent['created_at']))
            revision=db.execute('SELECT MAX(revision)+1 FROM scorecard_versions WHERE scorecard_id=?',(scorecard_id,)).fetchone()[0]
            config.update(scorecard_id=scorecard_id,scorecard_revision=revision,scorecard_name=name,backfill_count=len(historical))
            db.execute('INSERT INTO web_runs VALUES (?,?,?,?,?,?)',(run_id,f'{name} · v{revision} · missing-label review','live','ready',now,encode(config)))
            for item in [*historical,*unseen]:
                db.execute('INSERT INTO web_items(run_id,id,payload) VALUES (?,?,?)',(run_id,item['id'],encode(item)))
                for vote in inherited.get(item['id'],[]):
                    db.execute('INSERT INTO replay_feedback VALUES (?,?,?,?)',(run_id,item['id'],vote['classifier_id'],encode(vote)))
            db.execute('INSERT INTO scorecard_versions VALUES (?,?,?,?,?)',(scorecard_id,revision,run_id,parent_run_id,now))
            db.execute('UPDATE scorecards SET active_revision=? WHERE id=?',(revision,scorecard_id))
            self.register_run_definition(db,parent_run_id,scorecard_id,name,parent['config'])
            definition_revision=self.register_run_definition(db,run_id,scorecard_id,name,config)
            definition=db.execute('SELECT fingerprint FROM scorecard_definitions WHERE id=? AND revision=?',(scorecard_id,definition_revision)).fetchone()
            config.update(scorecard_definition_revision=definition_revision,scorecard_definition_fingerprint=definition[0])
            db.execute('UPDATE web_runs SET config=? WHERE id=?',(encode(config),run_id))
            db.execute('INSERT INTO scorecard_catalog VALUES (?,?) ON CONFLICT(id) DO UPDATE SET active_revision=excluded.active_revision',(scorecard_id,definition_revision))
        return self.run(run_id)

    def inherited_labels(self,run_id,item_id):
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute('SELECT payload FROM replay_feedback WHERE run_id=? AND item_id=?',(run_id,item_id))]

    def scorecards(self):
        with self.connect() as db:
            return [{**dict(row),'versions':[dict(version) for version in db.execute('SELECT revision,run_id FROM scorecard_versions WHERE scorecard_id=? ORDER BY revision',(row['id'],))]} for row in db.execute('SELECT * FROM scorecards ORDER BY name,id')]

    def scorecard_versions(self,scorecard_id):
        with self.connect() as db:
            versions=[dict(row) for row in db.execute('SELECT * FROM scorecard_versions WHERE scorecard_id=? ORDER BY revision',(scorecard_id,))]
        for version in versions:
            version['classifiers']=self.run(version['run_id'])['config']['classifiers']
            latest={}
            for event in self.all_events(version['run_id']):
                if event['payload'].get('kind')=='cycle-metrics':
                    latest[event['payload']['classifier_id']]=event['payload']['metrics']
            version['metrics']=latest
        return versions

    def activate_scorecard_version(self,scorecard_id,revision):
        with self.connect() as db:
            row=db.execute('SELECT run_id FROM scorecard_versions WHERE scorecard_id=? AND revision=?',(scorecard_id,revision)).fetchone()
            if row is None:raise ValueError('unknown scorecard version')
            if db.execute("SELECT 1 FROM web_jobs WHERE run_id IN (SELECT run_id FROM scorecard_versions WHERE scorecard_id=?) AND status IN ('pending','running')",(scorecard_id,)).fetchone():
                raise ValueError('wait for current work before reverting')
            db.execute('UPDATE scorecards SET active_revision=? WHERE id=?',(revision,scorecard_id))
        return self.run(row['run_id'])

    def checkpoint_scorecard(self,run_id,wheels):
        config=self.run(run_id)['config']
        snapshot={'classifiers':{identifier:{'fingerprint':wheel.active.fingerprint,'state':asdict(wheel.active)} for identifier,wheel in wheels.items()},
                  'definitions':[{key:row[key] for key in ('id','revision')} for row in config['classifiers']],
                  'decisions_model':config.get('decisions_model'),'optimizer_model':config.get('optimizer_model'),
                  'selection_policy':config.get('selection_policy'),'seed':config.get('seed'),
                  'event_cursor':self.event_cursor(run_id)}
        fingerprint=hashlib.sha256(encode(snapshot).encode()).hexdigest()
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO scorecard_checkpoints(run_id,fingerprint,payload,created_at) VALUES (?,?,?,?)',
                       (run_id,fingerprint,encode(snapshot),datetime.now(timezone.utc).isoformat()))
        return fingerprint

    def scorecard_checkpoints(self,run_id):
        self.run(run_id)
        with self.connect() as db:
            return [{**dict(row),'payload':json.loads(row['payload'])} for row in db.execute('SELECT * FROM scorecard_checkpoints WHERE run_id=? ORDER BY id',(run_id,))]

    def matched_run_preflight(self,before_run_id,after_run_id,*,limit=200):
        from .matched_run_plan import plan_matched_runs
        return plan_matched_runs(**self.matched_run_sources(before_run_id,after_run_id),limit=limit)

    def matched_run_sources(self,before_run_id,after_run_id):
        result={}
        for side,identifier in (('before',before_run_id),('after',after_run_id)):
            run=self.run(identifier);checkpoints=self.scorecard_checkpoints(identifier)
            if not checkpoints:raise ValueError('run has no recorded joint scorecard checkpoint')
            result[side]={**run,'checkpoint':checkpoints[-1],'items':self.items(identifier),'events':self.all_events(identifier)}
        return result

    def matched_evaluation_authorization(self,request_id,authorization):
        if request_id is None:return None
        if not isinstance(request_id,str) or not request_id.strip():raise ValueError('comparison identity required')
        with self.connect() as db:
            row=db.execute('SELECT run_id,payload FROM matched_evaluation_authorizations WHERE request_id=?',(request_id,)).fetchone()
        if row:
            if row['payload']!=encode(authorization):raise ValueError('command identity has different content')
            return self.run(row['run_id'])
        return None

    def create_matched_evaluation(self,name,plan,endpoints,max_requests,*,request_id=None,authorization=None):
        """Atomic immutable input snapshot and queued comparison, not an edition."""
        if not name.strip():raise ValueError('comparison name required')
        identifier,job_id=str(uuid4()),str(uuid4())
        config={'input_mode':'comparison','plan':plan,'max_requests':max_requests}
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if request_id is not None:
                if not isinstance(request_id,str) or not request_id.strip() or authorization is None:
                    raise ValueError('comparison identity and authorization required')
                existing=db.execute('SELECT run_id,payload FROM matched_evaluation_authorizations WHERE request_id=?',(request_id,)).fetchone()
                if existing:
                    if existing['payload']!=encode(authorization):raise ValueError('command identity has different content')
                    return self.run(existing['run_id'])
            db.execute('INSERT INTO web_runs VALUES (?,?,?,?,?,?)',(identifier,name,'recorded','ready',datetime.now(timezone.utc).isoformat(),encode(config)))
            db.execute('INSERT INTO matched_evaluation_inputs VALUES (?,?)',(identifier,encode(endpoints)))
            db.execute('INSERT INTO web_jobs VALUES (?,?,?,?,?,?,?)',(job_id,identifier,'approved-comparison','matched-evaluate','{}','pending',None))
            if request_id is not None:
                db.execute('INSERT INTO matched_evaluation_authorizations VALUES (?,?,?)',(request_id,identifier,encode(authorization)))
        return self.run(identifier)

    def matched_evaluation_inputs(self,run_id):
        with self.connect() as db:
            row=db.execute('SELECT payload FROM matched_evaluation_inputs WHERE run_id=?',(run_id,)).fetchone()
        if not row:raise ValueError('comparison has no frozen inputs')
        return json.loads(row['payload'])

    def matched_evaluation_target(self,run_id,event_id):
        if self.run(run_id)['config'].get('input_mode')!='comparison':
            raise ValueError('select a recorded comparison run')
        with self.connect() as db:
            row=db.execute("SELECT payload FROM web_events WHERE run_id=? AND json_extract(payload,'$.kind')='matched-evaluation-target' AND json_extract(payload,'$.event_id')=?",(run_id,event_id)).fetchone()
        return json.loads(row['payload']) if row else None

    def resume_matched_evaluation(self,run_id,request_id,*,max_requests,retry_failed=False):
        """Queue one explicitly authorized resume without replacing prior jobs."""
        if not request_id or type(retry_failed) is not bool:raise ValueError('resume identity and retry boolean required')
        payload={'max_requests':max_requests,'retry_failed':retry_failed}
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            run=db.execute('SELECT * FROM web_runs WHERE id=?',(run_id,)).fetchone()
            if not run:raise ValueError('unknown run')
            config=json.loads(run['config'])
            if config.get('input_mode')!='comparison':raise ValueError('select a comparison run')
            if type(max_requests) is not int or max_requests<config['plan']['request_upper_bound']:
                raise ValueError('request ceiling is below preflight upper bound')
            existing=db.execute('SELECT * FROM web_jobs WHERE run_id=? AND request_id=?',(run_id,request_id)).fetchone()
            if existing:
                if existing['kind']!='matched-evaluate' or existing['payload']!=encode(payload):
                    raise ValueError('command identity has different content')
                return self._job(existing)
            if max_requests<config['max_requests']:
                raise ValueError('resume ceiling cannot decrease prior authorization')
            latest=db.execute('SELECT status FROM web_jobs WHERE run_id=? ORDER BY rowid DESC LIMIT 1',(run_id,)).fetchone()
            if not latest or latest['status'] not in ('failed','interrupted'):
                raise ValueError('only failed or interrupted comparisons can resume')
            if db.execute("SELECT 1 FROM web_jobs WHERE run_id=? AND status IN ('pending','running')",(run_id,)).fetchone():
                raise ValueError('comparison already has work in progress')
            job_id=str(uuid4())
            db.execute('INSERT INTO web_jobs VALUES (?,?,?,?,?,?,?)',(job_id,run_id,request_id,'matched-evaluate',encode(payload),'pending',None))
            config={**config,'max_requests':max_requests}
            db.execute('UPDATE web_runs SET status="ready",config=? WHERE id=?',(encode(config),run_id))
            db.execute('INSERT INTO web_events(run_id,source_id,payload) VALUES (?,?,?)',(run_id,f'resume:{request_id}',encode({
                'kind':'comparison-resume-authorized','timestamp':datetime.now(timezone.utc).isoformat(),
                'plan_fingerprint':config['plan']['fingerprint'],**payload})))
            return self._job(db.execute('SELECT * FROM web_jobs WHERE id=?',(job_id,)).fetchone())

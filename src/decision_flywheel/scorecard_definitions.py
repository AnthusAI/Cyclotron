"""Versioned user definitions, independent of runs and learned checkpoints."""
from datetime import datetime, timezone
import hashlib
import json


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


class ScorecardDefinitions:
    def initialize_scorecard_definitions(self):
        with self.connect() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS scorecard_catalog(id TEXT PRIMARY KEY,active_revision INTEGER NOT NULL);
              CREATE TABLE IF NOT EXISTS scorecard_definitions(id TEXT,revision INTEGER,name TEXT NOT NULL,
                classifiers TEXT NOT NULL,settings TEXT NOT NULL,fingerprint TEXT NOT NULL,created_at TEXT NOT NULL,
                PRIMARY KEY(id,revision));
              CREATE TABLE IF NOT EXISTS run_scorecard_definitions(run_id TEXT PRIMARY KEY,scorecard_id TEXT NOT NULL,
                definition_revision INTEGER NOT NULL);
            ''')
            # Existing run editions are retained verbatim. Definitions capture
            # their frozen inputs, not their subsequently learned runtime state.
            for row in db.execute('SELECT v.scorecard_id,v.revision,v.run_id,r.config,s.name,s.active_revision FROM scorecard_versions v JOIN web_runs r ON r.id=v.run_id JOIN scorecards s ON s.id=v.scorecard_id ORDER BY v.scorecard_id,v.revision').fetchall():
                config=json.loads(row['config'])
                self.register_run_definition(db,row['run_id'],row['scorecard_id'],row['name'],config)
            for row in db.execute('SELECT id,active_revision FROM scorecards').fetchall():
                active=db.execute('SELECT d.definition_revision FROM run_scorecard_definitions d JOIN scorecard_versions v ON v.run_id=d.run_id WHERE v.scorecard_id=? AND v.revision=?',(row['id'],row['active_revision'])).fetchone()
                if active:db.execute('INSERT OR IGNORE INTO scorecard_catalog VALUES (?,?)',(row['id'],active[0]))

    def register_run_definition(self,db,run_id,identifier,name,config):
        existing=db.execute('SELECT definition_revision FROM run_scorecard_definitions WHERE run_id=?',(run_id,)).fetchone()
        if existing:return existing[0]
        refs=[{'id':c['id'],'revision':c['revision']} for c in config['classifiers']]
        settings={key:config[key] for key in ('decisions_provider','decisions_model','optimizer_model','optimizer_transport','selection_policy','seed','optimize_every','rubric_changes_every') if key in config}
        pinned=config.get('scorecard_definition_revision')
        if pinned is not None:
            found=db.execute('SELECT revision,classifiers FROM scorecard_definitions WHERE id=? AND revision=?',(identifier,pinned)).fetchone()
            if not found:raise ValueError('run pins an unknown scorecard definition')
            if found['classifiers']!=encode(refs):raise ValueError('run classifiers do not match pinned definition')
        else:
            found=db.execute('SELECT revision FROM scorecard_definitions WHERE id=? AND name=? AND classifiers=? AND settings=? ORDER BY revision LIMIT 1',(identifier,name,encode(refs),encode(settings))).fetchone()
        if found:revision=found[0]
        else:
            revision=db.execute('SELECT COALESCE(MAX(revision),0)+1 FROM scorecard_definitions WHERE id=?',(identifier,)).fetchone()[0]
            self._insert_definition(db,identifier,revision,name,refs,settings)
        db.execute('INSERT INTO run_scorecard_definitions VALUES (?,?,?)',(run_id,identifier,revision))
        return revision

    def run_scorecard_definition(self,run_id):
        with self.connect() as db:
            row=db.execute('SELECT * FROM run_scorecard_definitions WHERE run_id=?',(run_id,)).fetchone()
        if row is None:return None
        return self.scorecard_definition(row['scorecard_id'],row['definition_revision'])

    def scorecard_definition_comparison(self,identifier,before_revision,after_revision):
        from .definition_diff import compare_definitions
        before=self.scorecard_definition(identifier,before_revision)
        after=self.scorecard_definition(identifier,after_revision)
        changes=compare_definitions(before,after)
        members=[]
        for change in changes['members']:
            members.append({'id':change['id'],**{side:self.classifier(change['id'],change[side]['revision'])
                if change[side] is not None else None for side in ('before','after')}})
        return {'before':before,'after':after,'changes':changes,'classifiers':members}

    @staticmethod
    def _insert_definition(db,identifier,revision,name,refs,settings,ignore=False):
        fingerprint=hashlib.sha256(encode({'name':name,'classifiers':refs,'settings':settings}).encode()).hexdigest()
        db.execute(f"INSERT {'OR IGNORE ' if ignore else ''}INTO scorecard_definitions VALUES (?,?,?,?,?,?,?)",
                   (identifier,revision,name,encode(refs),encode(settings),fingerprint,datetime.now(timezone.utc).isoformat()))

    @staticmethod
    def _definition(row):
        return {**dict(row),'classifiers':json.loads(row['classifiers']),'settings':json.loads(row['settings'])}

    def save_scorecard_definition(self,identifier,name,classifiers,settings):
        if not isinstance(identifier,str) or not identifier.strip() or not isinstance(name,str) or not name.strip() or not isinstance(settings,dict):
            raise ValueError('scorecard identity, name and structured settings required')
        from .scorecard_settings import validate_shared_settings
        settings=validate_shared_settings(settings)
        refs=[{'id':row['id'],'revision':row['revision']} for row in classifiers]
        if not refs or len({row['id'] for row in refs})!=len(refs):
            raise ValueError('scorecard requires distinct ordered classifiers')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for row in refs:
                if type(row['revision']) is not int or not db.execute('SELECT 1 FROM classifiers WHERE id=? AND revision=?',(row['id'],row['revision'])).fetchone():
                    raise ValueError('unknown classifier revision')
            old=db.execute('SELECT d.* FROM scorecard_definitions d JOIN scorecard_catalog c ON c.id=d.id AND c.active_revision=d.revision WHERE d.id=?',(identifier,)).fetchone()
            if old and (old['name'],old['classifiers'],old['settings'])==(name,encode(refs),encode(settings)):
                return self._definition(old)
            revision=db.execute('SELECT COALESCE(MAX(revision),0)+1 FROM scorecard_definitions WHERE id=?',(identifier,)).fetchone()[0]
            self._insert_definition(db,identifier,revision,name,refs,settings)
            db.execute('INSERT INTO scorecard_catalog VALUES (?,?) ON CONFLICT(id) DO UPDATE SET active_revision=excluded.active_revision',(identifier,revision))
        return self.scorecard_definition(identifier,revision)

    def scorecard_definition(self,identifier,revision=None):
        with self.connect() as db:
            if revision is None:
                row=db.execute('SELECT d.* FROM scorecard_definitions d JOIN scorecard_catalog c ON c.id=d.id AND c.active_revision=d.revision WHERE d.id=?',(identifier,)).fetchone()
            else:row=db.execute('SELECT * FROM scorecard_definitions WHERE id=? AND revision=?',(identifier,revision)).fetchone()
        if row is None:raise ValueError('unknown scorecard definition')
        return self._definition(row)

    def scorecard_definitions(self):
        with self.connect() as db:
            return [self._definition(row) for row in db.execute('SELECT d.* FROM scorecard_definitions d JOIN scorecard_catalog c ON c.id=d.id AND c.active_revision=d.revision ORDER BY name,id')]

    def scorecard_definition_versions(self,identifier):
        with self.connect() as db:
            return [self._definition(row) for row in db.execute('SELECT * FROM scorecard_definitions WHERE id=? ORDER BY revision',(identifier,))]

    def activate_scorecard_definition(self,identifier,revision):
        definition=self.scorecard_definition(identifier,revision)
        with self.connect() as db:db.execute('UPDATE scorecard_catalog SET active_revision=? WHERE id=?',(revision,identifier))
        return definition

    def advance_classifier_definitions(self,db,identifier,revision):
        for old in db.execute('SELECT d.* FROM scorecard_definitions d JOIN scorecard_catalog c ON c.id=d.id AND c.active_revision=d.revision').fetchall():
            refs=json.loads(old['classifiers'])
            if not any(ref['id']==identifier for ref in refs):continue
            for ref in refs:
                if ref['id']==identifier:ref['revision']=revision
            next_revision=db.execute('SELECT MAX(revision)+1 FROM scorecard_definitions WHERE id=?',(old['id'],)).fetchone()[0]
            self._insert_definition(db,old['id'],next_revision,old['name'],refs,json.loads(old['settings']))
            db.execute('UPDATE scorecard_catalog SET active_revision=? WHERE id=?',(next_revision,old['id']))

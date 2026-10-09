"""Source-neutral, revisioned classifier and item-list catalog for the web store."""
from datetime import datetime, timezone
import hashlib
import json
from .models import DecisionTask


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace('Z','+00:00'))
    return parsed.replace(tzinfo=timezone.utc).isoformat() if parsed.tzinfo is None else parsed.astimezone(timezone.utc).isoformat()


class WorkspaceCatalog:
    def initialize_catalog(self):
        with self.connect() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS classifiers(id TEXT, revision INTEGER, name TEXT NOT NULL,
                config TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(id,revision));
              CREATE TABLE IF NOT EXISTS item_lists(id TEXT PRIMARY KEY, name TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS list_item_revisions(list_id TEXT REFERENCES item_lists(id),
                id TEXT, revision INTEGER, occurred_at TEXT NOT NULL, fingerprint TEXT NOT NULL,
                payload TEXT NOT NULL, imported_at TEXT NOT NULL, PRIMARY KEY(list_id,id,revision));
              CREATE INDEX IF NOT EXISTS list_item_order ON list_item_revisions(list_id,occurred_at,id);
              CREATE TABLE IF NOT EXISTS item_feedback(sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT UNIQUE NOT NULL, classifier_id TEXT NOT NULL, classifier_revision INTEGER NOT NULL,
                list_id TEXT NOT NULL, item_id TEXT NOT NULL, item_revision INTEGER NOT NULL,
                label TEXT NOT NULL, comment TEXT NOT NULL, created_at TEXT NOT NULL,
                FOREIGN KEY(classifier_id,classifier_revision) REFERENCES classifiers(id,revision),
                FOREIGN KEY(list_id,item_id,item_revision) REFERENCES list_item_revisions(list_id,id,revision));
              CREATE TABLE IF NOT EXISTS item_predictions(run_id TEXT, list_id TEXT, item_id TEXT,
                item_revision INTEGER, payload TEXT NOT NULL, PRIMARY KEY(run_id,list_id,item_id,item_revision));
              CREATE TABLE IF NOT EXISTS item_feedback_retractions(
                request_id TEXT PRIMARY KEY, feedback_request_id TEXT UNIQUE NOT NULL
                  REFERENCES item_feedback(request_id), created_at TEXT NOT NULL);
            ''')

    def save_classifier(self, identifier, name, config):
        classes = config.get('classes', [])
        labels = [row.get('label') for row in classes]
        if (not identifier or not name.strip() or not isinstance(config.get('question'),str)
                or not config['question'].strip() or len(labels)<2 or any(not isinstance(label,str) or not label.strip() for label in labels)
                or len(set(labels))!=len(labels) or any(row.get('role') not in (None,'positive','negative') for row in classes)):
            raise ValueError('classifier requires a question and distinct ordered classes')
        data = encoded(config)
        DecisionTask(identifier,tuple(labels),config['question'])
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM classifiers WHERE id=? ORDER BY revision DESC LIMIT 1',(identifier,)).fetchone()
            if old and old['config']==data and old['name']==name:
                return self._classifier(old)
            revision = old['revision']+1 if old else 1
            db.execute('INSERT INTO classifiers VALUES (?,?,?,?,?)',(identifier,revision,name,data,datetime.now(timezone.utc).isoformat()))
            self.advance_classifier_definitions(db,identifier,revision)
        return self.classifier(identifier,revision)

    @staticmethod
    def _classifier(row):
        return {**dict(row), 'config':json.loads(row['config'])}

    def classifier(self, identifier, revision=None):
        with self.connect() as db:
            row = db.execute('SELECT * FROM classifiers WHERE id=? AND (? IS NULL OR revision=?) ORDER BY revision DESC LIMIT 1',
                             (identifier,revision,revision)).fetchone()
        if row is None: raise ValueError('unknown classifier revision')
        return self._classifier(row)

    def classifiers(self):
        with self.connect() as db:
            return [self._classifier(row) for row in db.execute('SELECT c.* FROM classifiers c WHERE revision=(SELECT MAX(revision) FROM classifiers WHERE id=c.id) ORDER BY name,id')]

    def classifier_versions(self, identifier):
        with self.connect() as db:
            rows=[self._classifier(row) for row in db.execute('SELECT * FROM classifiers WHERE id=? ORDER BY revision',(identifier,))]
        if not rows:raise ValueError('unknown classifier')
        return rows

    def save_item_list(self, identifier, name):
        if not identifier or not name.strip(): raise ValueError('item list identity and name required')
        with self.connect() as db:
            db.execute('INSERT INTO item_lists VALUES (?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name',(identifier,name))
        return {'id':identifier,'name':name}

    def item_lists(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT l.*,COUNT(DISTINCT r.id) AS count FROM item_lists l LEFT JOIN list_item_revisions r ON r.list_id=l.id GROUP BY l.id ORDER BY l.name,l.id')]

    def upsert_list_items(self, list_id, items):
        if not 1<=len(items)<=200: raise ValueError('upsert between one and 200 items')
        counts = dict(inserted=0,updated=0,unchanged=0)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM item_lists WHERE id=?',(list_id,)).fetchone(): raise ValueError('unknown item list')
            for item in items:
                if not isinstance(item.get('id'),str) or not item['id'] or not isinstance(item.get('values'),dict):
                    raise ValueError('item identity and structured values required')
                occurred = timestamp(item['occurred_at'])
                data = encoded({**item,'occurred_at':occurred})
                if len(data.encode())>4_000_000: raise ValueError('item exceeds size limit')
                fingerprint = hashlib.sha256(data.encode()).hexdigest()
                old = db.execute('SELECT * FROM list_item_revisions WHERE list_id=? AND id=? ORDER BY revision DESC LIMIT 1',(list_id,item['id'])).fetchone()
                if old and old['fingerprint']==fingerprint:
                    counts['unchanged']+=1
                    continue
                revision = old['revision']+1 if old else 1
                db.execute('INSERT INTO list_item_revisions VALUES (?,?,?,?,?,?,?)',
                           (list_id,item['id'],revision,occurred,fingerprint,data,datetime.now(timezone.utc).isoformat()))
                counts['updated' if old else 'inserted']+=1
        return counts

    def list_items(self, list_id, *, after=0, limit=200):
        if after<0 or not 1<=limit<=200: raise ValueError('item page must be bounded')
        with self.connect() as db:
            if not db.execute('SELECT 1 FROM item_lists WHERE id=?',(list_id,)).fetchone(): raise ValueError('unknown item list')
            rows = db.execute('SELECT r.* FROM list_item_revisions r WHERE list_id=? AND revision=(SELECT MAX(revision) FROM list_item_revisions WHERE list_id=r.list_id AND id=r.id) ORDER BY occurred_at,id LIMIT ? OFFSET ?', (list_id,limit,after))
            return [{**json.loads(row['payload']),'revision':row['revision'],'fingerprint':row['fingerprint']} for row in rows]

    def item_revision(self, list_id, identifier, revision):
        with self.connect() as db:
            row = db.execute('SELECT payload FROM list_item_revisions WHERE list_id=? AND id=? AND revision=?',(list_id,identifier,revision)).fetchone()
        if row is None: raise ValueError('unknown item revision')
        return json.loads(row[0])

    def snapshot_items(self, list_id):
        """Freeze current membership and revisions without moving page offsets.

        Pagination is for browsing. Run manifests need one SQLite read snapshot
        so concurrent upserts cannot reorder items between successive pages.
        """
        with self.connect() as db:
            db.execute('BEGIN')
            if not db.execute('SELECT 1 FROM item_lists WHERE id=?',(list_id,)).fetchone():
                raise ValueError('unknown item list')
            rows=db.execute('''SELECT r.* FROM list_item_revisions r
                WHERE list_id=? AND revision=(SELECT MAX(revision) FROM list_item_revisions
                    WHERE list_id=r.list_id AND id=r.id)
                ORDER BY occurred_at,id''',(list_id,))
            return [{**json.loads(row['payload']),'revision':row['revision'],
                     'fingerprint':row['fingerprint']} for row in rows]

    def label_item(self, classifier_id, classifier_revision, list_id, item_id, item_revision, label, comment, request_id):
        config = self.classifier(classifier_id,classifier_revision)['config']
        self.item_revision(list_id,item_id,item_revision)
        label = DecisionTask(classifier_id,tuple(row['label'] for row in config['classes']),config['question']).validate_label(label)
        if not request_id or not isinstance(comment,str): raise ValueError('feedback identity and comment required')
        fields = dict(classifier_id=classifier_id,classifier_revision=classifier_revision,list_id=list_id,
                      item_id=item_id,item_revision=item_revision,label=label,comment=comment)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM item_feedback WHERE request_id=?',(request_id,)).fetchone()
            if old:
                if any(old[key]!=value for key,value in fields.items()): raise ValueError('feedback identity has different content')
                return dict(old)
            db.execute('INSERT INTO item_feedback(request_id,classifier_id,classifier_revision,list_id,item_id,item_revision,label,comment,created_at) VALUES (?,?,?,?,?,?,?,?,?)',
                       (request_id,classifier_id,classifier_revision,list_id,item_id,item_revision,label,comment,datetime.now(timezone.utc).isoformat()))
            return dict(db.execute('SELECT * FROM item_feedback WHERE request_id=?',(request_id,)).fetchone())

    def item_labels(self, list_id, item_id, item_revision):
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT f.* FROM item_feedback f WHERE list_id=? AND item_id=? AND item_revision=? AND sequence=(SELECT MAX(sequence) FROM item_feedback WHERE classifier_id=f.classifier_id AND classifier_revision=f.classifier_revision AND list_id=f.list_id AND item_id=f.item_id AND item_revision=f.item_revision) AND NOT EXISTS(SELECT 1 FROM item_feedback_retractions WHERE feedback_request_id=f.request_id) ORDER BY classifier_id,classifier_revision',
                                                    (list_id,item_id,item_revision))]

    def retract_item_label(self, feedback_request_id, request_id):
        """Append a tombstone for the current vote; never delete its history."""
        if not isinstance(request_id,str) or not request_id:raise ValueError('retraction identity required')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing=db.execute('SELECT * FROM item_feedback_retractions WHERE request_id=?',(request_id,)).fetchone()
            if existing:
                if existing['feedback_request_id']!=feedback_request_id:raise ValueError('retraction identity has different content')
                return dict(existing)
            vote=db.execute('SELECT * FROM item_feedback WHERE request_id=?',(feedback_request_id,)).fetchone()
            if vote is None:raise ValueError('unknown feedback identity')
            current=db.execute('SELECT MAX(sequence) FROM item_feedback WHERE classifier_id=? AND classifier_revision=? AND list_id=? AND item_id=? AND item_revision=?',
                (vote['classifier_id'],vote['classifier_revision'],vote['list_id'],vote['item_id'],vote['item_revision'])).fetchone()[0]
            if current!=vote['sequence']:raise ValueError('only the current label can be retracted')
            if db.execute('SELECT 1 FROM item_feedback_retractions WHERE feedback_request_id=?',(feedback_request_id,)).fetchone():
                raise ValueError('label already retracted by another command')
            db.execute('INSERT INTO item_feedback_retractions VALUES (?,?,?)',
                       (request_id,feedback_request_id,datetime.now(timezone.utc).isoformat()))
            return dict(db.execute('SELECT * FROM item_feedback_retractions WHERE request_id=?',(request_id,)).fetchone())

    def record_item_prediction(self,run_id,list_id,item_id,item_revision,prediction):
        self.item_revision(list_id,item_id,item_revision)
        with self.connect() as db:
            data=encoded(prediction)
            old=db.execute('SELECT payload FROM item_predictions WHERE run_id=? AND list_id=? AND item_id=? AND item_revision=?',(run_id,list_id,item_id,item_revision)).fetchone()
            if old and old[0]!=data:raise ValueError('displayed prediction is immutable')
            db.execute('INSERT OR IGNORE INTO item_predictions VALUES (?,?,?,?,?)',(run_id,list_id,item_id,item_revision,data))

    def item_results(self,list_id,item_id,item_revision):
        with self.connect() as db:
            return [{**dict(row),'payload':json.loads(row['payload'])} for row in db.execute('SELECT p.*,r.name AS run_name FROM item_predictions p JOIN web_runs r ON r.id=p.run_id WHERE list_id=? AND item_id=? AND item_revision=? ORDER BY r.created_at DESC',(list_id,item_id,item_revision))]

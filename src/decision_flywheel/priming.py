"""Host-registered transformations of untrusted inputs into reviewed examples.

Callables are trusted application code, never selected by an import path from a
client. They receive JSON copies of raw inputs, prior candidate and feedback.
They must treat content as data and own their network/tool permissions.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable
import hashlib
import json
from uuid import uuid4
from .workspace_catalog import encoded, timestamp


@dataclass(frozen=True)
class Transformer:
    id: str
    name: str
    fields: tuple[dict, ...]
    transform: Callable[[dict, dict | None, str], dict]
    validate: Callable[[dict], None] | None = None
    revision: str = '1'


def validate_item(item):
    if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not item['id'] or not isinstance(item.get('values'), dict):
        raise ValueError('item identity and structured values required')
    item = json.loads(encoded(item))
    item['occurred_at'] = timestamp(item['occurred_at'])
    if len(encoded(item).encode()) > 4_000_000:
        raise ValueError('item exceeds size limit')
    return item


def manual_transform(inputs, previous, feedback):
    """A safe default: operator-supplied content; no network or model calls."""
    return {'id': previous['id'] if previous else str(uuid4()),
            'occurred_at': inputs['occurred_at'],
            'values': json.loads(inputs['values']),
            'provenance': {'source_url': inputs.get('source_url', '')}}


MANUAL = Transformer('structured', 'Structured item / research paper', (
    {'name':'source_url','label':'Source URL (provenance only)','required':False},
    {'name':'occurred_at','label':'Item date (ISO 8601)','required':True},
    {'name':'values','label':'Item fields (JSON object)','required':True,'multiline':True},
), manual_transform)


class Priming:
    def __init__(self, store, transformers=None, *, redact=()):
        self.store = store
        self.redact = tuple(redact)
        registered = [MANUAL] if transformers is None else transformers
        self.transformers = {row.id:row for row in registered}
        if len(self.transformers) != len(registered): raise ValueError('duplicate transformer identity')
        with store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS priming_drafts(id TEXT PRIMARY KEY, payload TEXT NOT NULL)')

    def options(self):
        return [dict(id=t.id, name=t.name, fields=t.fields, revision=t.revision) for t in self.transformers.values()]

    def safe(self, value):
        data = encoded(value)
        if len(data.encode()) > 4_000_000: raise ValueError('import exceeds size limit')
        if any(secret and secret in data for secret in self.redact): raise ValueError('import contains a credential')
        return json.loads(data)

    def _read(self, db, identifier):
        row = db.execute('SELECT payload FROM priming_drafts WHERE id=?',(identifier,)).fetchone()
        if row is None: raise ValueError('unknown import draft')
        return json.loads(row[0])

    def _write(self, db, draft):
        db.execute('INSERT INTO priming_drafts VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
                   (draft['id'], encoded(draft)))

    def get(self, identifier):
        with self.store.connect() as db: return self._read(db,identifier)

    def preview(self, identifier, version, transformer_id, inputs, feedback):
        if not isinstance(identifier,str) or not identifier.strip(): raise ValueError('draft identity required')
        transformer = self.transformers.get(transformer_id)
        if transformer is None: raise ValueError('unknown transformer')
        if not isinstance(inputs,dict) or not isinstance(feedback,str): raise ValueError('structured inputs and feedback required')
        inputs = self.safe(inputs)
        self.safe(feedback)
        allowed = {field['name'] for field in transformer.fields}
        if set(inputs)-allowed: raise ValueError('unknown input fields')
        for field in transformer.fields:
            value = inputs.get(field['name'], '')
            if not isinstance(value,str) or (field.get('required') and not value.strip()):
                raise ValueError('required input missing or invalid')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT payload FROM priming_drafts WHERE id=?',(identifier,)).fetchone()
            draft = json.loads(old[0]) if old else dict(id=identifier,version=0,status='new',history=[],candidate=None)
            if draft['version'] != version or draft['status'] not in ('new','preview','failed'):
                raise ValueError('stale or closed import draft; reload its current state')
            if draft['history'] and (not feedback.strip() or draft['transformer'] != transformer_id):
                raise ValueError('revision needs feedback and the same transformer')
            previous = draft['candidate']
            draft.update(version=version+1,status='transforming',transformer=transformer_id,candidate=None,error=None)
            draft['history'].append(dict(inputs=inputs,feedback=feedback,transformer=transformer_id,transformer_revision=transformer.revision))
            self._write(db,draft)
        try:
            candidate = transformer.transform(self.safe(inputs), self.safe(previous), feedback)
            candidate = validate_item(candidate)
            if transformer.validate: transformer.validate(candidate)
            candidate = self.safe(candidate)
            status, error = 'preview', None
        except Exception:
            # Provider failures may contain credentials or fetched prompt content.
            candidate, status, error = None, 'failed', 'Transformation failed. Check inputs and retry with revision feedback.'
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = self._read(db,identifier)
            if current['status'] != 'transforming' or current['version'] != version+1: return current
            current.update(status=status,candidate=candidate,error=error)
            current['history'][-1].update(status=status,candidate=candidate)
            self._write(db,current)
            return current

    def cancel(self, identifier):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            draft = self._read(db,identifier)
            if draft['status'] == 'accepted': raise ValueError('accepted imports cannot be cancelled')
            draft.update(status='cancelled',candidate=None)
            self._write(db,draft)
            return draft

    def accept(self, identifier, version, list_id, classifier_id, classifier_revision, label, rationale):
        if not isinstance(rationale,str) or not rationale.strip(): raise ValueError('explicit human rationale required')
        acceptance = self.safe(dict(version=version,list_id=list_id,classifier_id=classifier_id,
                                    classifier_revision=classifier_revision,label=label,rationale=rationale))
        config = self.store.classifier(classifier_id,classifier_revision)['config']
        if label not in [row['label'] for row in config['classes']]: raise ValueError('explicit configured label required')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            draft = self._read(db,identifier)
            if draft['status'] == 'accepted':
                if draft['acceptance'] != acceptance: raise ValueError('acceptance has different content')
                return draft
            if draft['version'] != version or draft['status'] != 'preview': raise ValueError('stale or closed import draft')
            if not db.execute('SELECT 1 FROM item_lists WHERE id=?',(list_id,)).fetchone(): raise ValueError('unknown item list')
            item = validate_item(draft['candidate'])
            provenance = item.get('provenance', {})
            if not isinstance(provenance,dict): raise ValueError('provenance must be an object')
            item['provenance'] = {**provenance, 'priming': {'draft_id':identifier,'history':draft['history']}}
            item = self.safe(validate_item(item))
            # Curated imports never overwrite operational items or their labels.
            if db.execute('SELECT 1 FROM list_item_revisions WHERE list_id=? AND id=?',(list_id,item['id'])).fetchone():
                raise ValueError('item already exists; use the catalog correction workflow')
            data = encoded(item)
            now = datetime.now(timezone.utc).isoformat()
            db.execute('INSERT INTO list_item_revisions VALUES (?,?,?,?,?,?,?)',
                       (list_id,item['id'],1,item['occurred_at'],hashlib.sha256(data.encode()).hexdigest(),data,now))
            db.execute('INSERT INTO item_feedback(request_id,classifier_id,classifier_revision,list_id,item_id,item_revision,label,comment,created_at) VALUES (?,?,?,?,?,?,?,?,?)',
                       ('priming:'+identifier,classifier_id,classifier_revision,list_id,item['id'],1,label,rationale,now))
            draft.update(status='accepted',acceptance=acceptance)
            self._write(db,draft)
            return draft

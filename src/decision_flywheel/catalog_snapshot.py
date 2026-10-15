"""Versioned catalog records for migration, not a mutable cloud authority."""
from datetime import datetime
from hashlib import sha256
import json
from .models import DecisionTask


SCHEMA = 'cyclotron-catalog/v1'
COLUMNS = {
    'classifiers': ('id','revision','name','config','created_at'),
    'item_lists': ('id','name'),
    'list_item_revisions': ('list_id','id','revision','occurred_at','fingerprint','payload','imported_at'),
    'item_feedback': ('sequence','request_id','classifier_id','classifier_revision','list_id','item_id','item_revision','label','comment','created_at'),
    'item_predictions': ('run_id','list_id','item_id','item_revision','payload'),
    'item_feedback_retractions': ('request_id','feedback_request_id','created_at'),
}
KEYS = {
    'classifiers': ('id','revision'), 'item_lists': ('id',),
    'list_item_revisions': ('list_id','id','revision'), 'item_feedback': ('sequence',),
    'item_predictions': ('run_id','list_id','item_id','item_revision'),
    'item_feedback_retractions': ('request_id',),
}
INTEGERS = {'revision','sequence','classifier_revision','item_revision'}


def encode(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')


def _invalid_constant(value): raise ValueError('non-JSON numeric constant')


def decode(value): return json.loads(value,parse_constant=_invalid_constant)


def budgets(max_records,max_bytes):
    if type(max_records) is not int or max_records<1 or type(max_bytes) is not int or max_bytes<1:
        raise ValueError('explicit positive record and byte budgets required')


def validate(snapshot, *, max_records=10000, max_bytes=64000000):
    """Validate and copy before any destination mutation; retain JSON text as text."""
    budgets(max_records,max_bytes)
    content=encode(snapshot)
    if len(content)>max_bytes: raise ValueError('catalog snapshot byte budget exceeded')
    snapshot=decode(content)
    if not isinstance(snapshot,dict) or set(snapshot)!={'schema','tables','feedback_sequence'} or snapshot['schema']!=SCHEMA:
        raise ValueError('unsupported catalog snapshot')
    tables=snapshot['tables']
    if not isinstance(tables,dict) or set(tables)!=set(COLUMNS): raise ValueError('complete catalog tables required')
    if not all(isinstance(rows,list) for rows in tables.values()) or sum(map(len,tables.values()))>max_records:
        raise ValueError('catalog snapshot record budget exceeded')
    for table,columns in COLUMNS.items():
        seen=set()
        for row in tables[table]:
            if not isinstance(row,dict) or set(row)!=set(columns): raise ValueError('invalid catalog record columns')
            for name,value in row.items():
                if name in INTEGERS:
                    if type(value) is not int or not 1<=value<2**63: raise ValueError('bounded positive catalog revision/sequence required')
                elif not isinstance(value,str) or not value and name!='comment':
                    raise ValueError('catalog text field required')
            if 'name' in row and not row['name'].strip(): raise ValueError('catalog name required')
            key=tuple(row[column] for column in KEYS[table])
            if key in seen: raise ValueError('duplicate catalog record')
            seen.add(key)
            for name in ('created_at','imported_at','occurred_at'):
                if name in row: datetime.fromisoformat(row[name].replace('Z','+00:00'))
    classifiers={}
    generations={}
    for row in tables['classifiers']:
        config=decode(row['config'])
        if not isinstance(config,dict) or not isinstance(config.get('classes'),list) or not isinstance(config.get('question'),str) or not config['question'].strip():
            raise ValueError('invalid classifier configuration')
        labels=[item.get('label') if isinstance(item,dict) else None for item in config['classes']]
        if len(labels)<2 or any(not isinstance(label,str) or not label.strip() for label in labels) or len(set(labels))!=len(labels):
            raise ValueError('distinct classifier labels required')
        if any(item.get('role') not in (None,'positive','negative') for item in config['classes']):
            raise ValueError('invalid classifier class role')
        classifiers[row['id'],row['revision']]=DecisionTask(row['id'],tuple(labels),config['question'])
        generations.setdefault(('classifier',row['id']),set()).add(row['revision'])
    lists={row['id'] for row in tables['item_lists']}
    revisions=set()
    for row in tables['list_item_revisions']:
        payload=decode(row['payload'])
        if (row['list_id'] not in lists or not isinstance(payload,dict) or payload.get('id')!=row['id']
                or payload.get('occurred_at')!=row['occurred_at'] or not isinstance(payload.get('values'),dict)
                or sha256(row['payload'].encode()).hexdigest()!=row['fingerprint']):
            raise ValueError('invalid canonical item revision')
        revisions.add((row['list_id'],row['id'],row['revision']))
        generations.setdefault(('item',row['list_id'],row['id']),set()).add(row['revision'])
    if any(len(versions)!=max(versions) for versions in generations.values()):
        raise ValueError('incomplete catalog revision history')
    feedback={}
    for row in tables['item_feedback']:
        task=classifiers.get((row['classifier_id'],row['classifier_revision']))
        if task is None or (row['list_id'],row['item_id'],row['item_revision']) not in revisions:
            raise ValueError('feedback references unknown pinned revisions')
        task.validate_label(row['label'])
        if row['request_id'] in feedback: raise ValueError('duplicate feedback identity')
        feedback[row['request_id']]=row
    sequence=snapshot['feedback_sequence']
    if type(sequence) is not int or not max((row['sequence'] for row in feedback.values()),default=0)<=sequence<2**63:
        raise ValueError('feedback sequence watermark does not retain history')
    for row in tables['item_predictions']:
        if (row['list_id'],row['item_id'],row['item_revision']) not in revisions:
            raise ValueError('prediction references unknown item revision')
        decode(row['payload'])
    retracted=set()
    for row in tables['item_feedback_retractions']:
        identity=row['feedback_request_id']
        if identity not in feedback or identity in retracted: raise ValueError('invalid feedback retraction')
        retracted.add(identity)
    return snapshot

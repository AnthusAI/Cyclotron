"""Explicit human priming is training context, never scored operational feedback."""
from dataclasses import asdict
import json
from .models import Item,LabeledItem
from .workspace_catalog import encoded


def encode_priming(examples,tasks,*,redact=()):
    if not isinstance(examples,dict) or set(examples)-set(tasks):raise ValueError('priming maps known classifier ids to labeled examples')
    output={}
    for cid,rows in examples.items():
        if not isinstance(rows,(list,tuple)) or len(rows)>200:raise ValueError('priming is bounded to 200 examples per classifier')
        seen=set();output[cid]=[]
        for row in rows:
            if not isinstance(row,LabeledItem) or row.source!='trusted' or not isinstance(row.item.id,str) or not row.item.id.strip():raise ValueError('priming needs labeled items with identities')
            tasks[cid].validate_target(row.item)
            if row.label not in tasks[cid].labels:raise ValueError('priming needs an explicit configured label')
            if row.item.id in seen:raise ValueError('duplicate priming item')
            seen.add(row.item.id);output[cid].append(asdict(row))
    data=encoded(output)
    if len(data.encode())>4_000_000 or any(secret and secret in data for secret in redact):raise ValueError('priming exceeds size limit or contains a credential')
    return json.loads(data)


def decode_priming(rows):
    return {cid:tuple(LabeledItem(Item(row['item']['id'],row['item']['values']),row['label'],row['source'],row['context']) for row in values) for cid,values in rows.items()}


def catalog_priming(store,list_id,classifiers,operational_ids):
    """Pin accepted import items and their classifier-specific labels in one snapshot."""
    from .models import DecisionTask
    result={c['id']:[] for c in classifiers}
    with store.connect() as db:
        db.execute('BEGIN')
        if not db.execute('SELECT 1 FROM item_lists WHERE id=?',(list_id,)).fetchone():raise ValueError('unknown priming item list')
        items=db.execute('SELECT r.* FROM list_item_revisions r WHERE list_id=? AND revision=(SELECT MAX(revision) FROM list_item_revisions WHERE list_id=r.list_id AND id=r.id) ORDER BY occurred_at,id',(list_id,)).fetchall()
        for row in items:
            item=json.loads(row['payload'])
            if not item.get('provenance',{}).get('priming'):continue
            if item['id'] in operational_ids:raise ValueError('priming and operational items must be disjoint')
            for classifier in classifiers:
                cid=classifier['id']
                vote=db.execute('SELECT f.* FROM item_feedback f WHERE list_id=? AND item_id=? AND item_revision=? AND classifier_id=? AND classifier_revision=? ORDER BY sequence DESC LIMIT 1',(list_id,item['id'],row['revision'],cid,classifier['revision'])).fetchone()
                if vote and not db.execute('SELECT 1 FROM item_feedback_retractions WHERE feedback_request_id=?',(vote['request_id'],)).fetchone():
                    result[cid].append(LabeledItem(Item(item['id'],item['values']),vote['label'],'trusted',({'human_feedback':vote['comment']} if vote['comment'] and vote['comment'].strip() else {})))
    if not any(result.values()):raise ValueError('priming list has no accepted examples for the selected classifier revisions')
    tasks={c['id']:DecisionTask(c['id'],tuple(row['label'] for row in c['config']['classes']),c['config']['question'],c['config'].get('input_field','text')) for c in classifiers}
    return encode_priming(result,tasks)

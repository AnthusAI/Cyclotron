"""Read-only preflight for a matched protected joint-cyclotron evaluation."""
from collections import Counter
import hashlib
import json
import unicodedata
from .feedback_trigger import PROTECTED_ASSIGNMENTS


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def normalized_content(value):
    """Compare structured content conservatively, without guessing source fields."""
    if isinstance(value,str):return ' '.join(unicodedata.normalize('NFC',value).split())
    if isinstance(value,dict):return {key:normalized_content(item) for key,item in value.items()}
    if isinstance(value,list):return [normalized_content(item) for item in value]
    return value


def reviewed(source):
    active={};learnable=set()
    for row in source['events']:
        event=row['payload']
        if event.get('kind')!='human-feedback':continue
        feedback=event['feedback'];key=(feedback['item_id'],event.get('classifier_id'))
        active.pop(key,None)
        if event.get('action')=='submitted':
            active[key]=(feedback['final_answer_value'],event.get('assignment'),row['sequence'])
            if event.get('assignment') not in PROTECTED_ASSIGNMENTS:learnable.add(feedback['item_id'])
    return active,learnable


def plan_matched_runs(before,after,*,limit=200):
    if type(limit) is not int or not 1<=limit<=200:raise ValueError('comparison limit must be between one and 200')
    for source in (before,after):
        if source['config'].get('evaluation_protocol')!='protected-feedback-v1':
            raise ValueError('run has no verified protection protocol; use a fresh replay for a clean comparison')
        if type(source['checkpoint']['payload'].get('event_cursor')) is not int:
            raise ValueError('checkpoint has no recorded event boundary')
    refs={side:{row['id']:row for row in source['config']['classifiers']} for side,source in (('before',before),('after',after))}
    common=[identifier for identifier,row in refs['before'].items() if identifier in refs['after'] and row['revision']==refs['after'][identifier]['revision'] and row['config']['classes']==refs['after'][identifier]['config']['classes']]
    if not common:raise ValueError('no identical classifier definitions available for matched evaluation')
    items={side:{row['id']:row for row in source['items']} for side,source in (('before',before),('after',after))}
    votes_before,unsafe_before=reviewed(before);votes_after,unsafe_after=reviewed(after)
    unsafe=unsafe_before|unsafe_after
    if unsafe_before-set(items['before']) or unsafe_after-set(items['after']):
        raise ValueError('previously learnable item content is unavailable; cannot verify duplicate exclusion')
    unsafe_texts={fingerprint(normalized_content(row['values'])) for rows in items.values() for identifier,row in rows.items() if identifier in unsafe}
    eligible=[];excluded=0
    for identifier,item in items['after'].items():
        original=items['before'].get(identifier)
        same=original and (item['revision'],item['fingerprint'],item['values'])==(original['revision'],original['fingerprint'],original['values'])
        labels={};order=0
        if same and identifier not in unsafe and fingerprint(normalized_content(item['values'])) not in unsafe_texts:
            for cid in common:
                a=votes_before.get((identifier,cid));b=votes_after.get((identifier,cid))
                if not a or not b or a[0]!=b[0] or a[1] not in PROTECTED_ASSIGNMENTS or b[1] not in PROTECTED_ASSIGNMENTS:break
                if a[0] not in [row['label'] for row in refs['before'][cid]['config']['classes']]:break
                labels[cid]=a[0];order=max(order,b[2])
        if len(labels)==len(common):eligible.append((order,identifier,labels))
        else:excluded+=1
    eligible.sort()
    # One common target set preserves joint-request semantics. Stratify by
    # the first common classifier and disclose every classifier's counts.
    anchor=common[0];classes=[row['label'] for row in refs['before'][anchor]['config']['classes']]
    quota=limit//len(classes);selected=[];counts=Counter()
    for row in reversed(eligible):
        label=row[2][anchor]
        if counts[label]<quota:selected.append(row);counts[label]+=1
    chosen={row[1] for row in selected}
    for row in reversed(eligible):
        if len(selected)>=limit:break
        if row[1] not in chosen:selected.append(row);chosen.add(row[1])
    selected.sort()
    endpoints={side:{'run_id':source['id'],'checkpoint_fingerprint':source['checkpoint']['fingerprint'],
                    'checkpoint_event_cursor':source['checkpoint']['payload']['event_cursor'],
                    'source_event_cursor':max((row['sequence'] for row in source['events']),default=0)}
               for side,source in (('before',before),('after',after))}
    plan={'scope':'matched protected items; joint requests retain all endpoint classifiers; no fitting or promotion',
          'content_exclusion_policy':'unicode-nfc-and-whitespace-normalized-structured-values-v1',
          'endpoints':endpoints,'classifier_ids':common,'sampling_classifier_id':anchor,'limit':limit,
          'item_ids':[row[1] for row in selected],
          'items':[{'id':row[1],'revision':items['after'][row[1]]['revision'],'fingerprint':items['after'][row[1]]['fingerprint'],'labels':row[2]} for row in selected],
          'sample_count':len(selected),'available_count':len(eligible),'excluded_count':excluded,
          'class_counts':{cid:{label:sum(row[2][cid]==label for row in selected) for label in [r['label'] for r in refs['before'][cid]['config']['classes']]} for cid in common},
          'request_upper_bound':2*len(selected),'sampling':'latest stratified by anchor classifier; fill spare slots when a class is scarce'}
    return {**plan,'fingerprint':fingerprint(plan)}

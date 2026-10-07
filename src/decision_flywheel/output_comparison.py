"""Paired raw-decision and final-head metrics from recorded, labeled outputs."""
from collections import OrderedDict
from .rolling_metrics import recent_reviewed_metrics
from .calibration_metrics import reliability_curve


def compare_outputs(classes, records, *, limit=200, scope='reviewed-pre-vote'):
    if type(limit) is not int or not 1<=limit<=200:
        raise ValueError('comparison window must be between one and 200')
    if scope not in ('reviewed-pre-vote','protected-matched'):
        raise ValueError('unknown output comparison scope')
    classes=tuple(classes);unique=OrderedDict()
    for record in records:
        unique.pop(record['item_id'],None);unique[record['item_id']]=record
    window=list(unique.values())[-limit:]
    paired=[row for row in window if row.get('decision_model_label') in classes]
    eligible=[bool(row.get('decision_model_probabilities') and row.get('probabilities')) for row in paired]
    result={'count':len(paired),'missing_raw_count':len(window)-len(paired),
            'item_ids':[row['item_id'] for row in paired],
            'missing_paired_probability_count':sum(not valid for valid in eligible),
            'evaluation_scope':scope,
            'scope':'same protected matched targets; frozen versions; no fitting' if scope=='protected-matched' else 'same latest reviewed pre-vote items; descriptive, not held-out'}
    for side,label_key,probability_key in (('raw','decision_model_label','decision_model_probabilities'),('final','label','probabilities')):
        metrics=recent_reviewed_metrics(classes,[(row['item_id'],row['actual_label'],row[label_key],row.get(probability_key)) for row in paired],limit=limit)
        metrics['calibration']=reliability_curve(classes,[row['actual_label'] for row in paired],
            [row[label_key] for row in paired],[row.get(probability_key) if valid else None for row,valid in zip(paired,eligible)])
        result[side]=metrics
    def difference(raw,final):return final-raw if raw is not None and final is not None else None
    result['accuracy_delta']=difference(result['raw']['accuracy'],result['final']['accuracy'])
    for metric in ('ece','brier'):
        result[f'{metric}_delta']=difference(result['raw']['calibration'][metric],result['final']['calibration'][metric])
    return result

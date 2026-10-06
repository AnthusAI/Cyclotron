"""Ordered class display roles and explicit positive-vs-rest measurements."""
import math


def class_configuration(labels, config=None):
    labels = list(dict.fromkeys(labels))
    if config is None:
        return [{'label':label, 'role':'neutral'} for label in labels]
    if not isinstance(config, list) or any(not isinstance(row, dict) or
        not isinstance(row.get('label'), str) or row.get('role') not in ('positive','negative','neutral') for row in config):
        raise ValueError('class configuration must be an ordered list of labels with positive, negative, or neutral roles')
    declared = [row['label'] for row in config]
    if len(set(declared)) != len(declared) or set(declared) != set(labels):
        raise ValueError('class configuration must cover each recorded class exactly once')
    return [{'label':row['label'], 'role':row['role']} for row in config]


def positive_metrics(metrics, config):
    positives = [row['label'] for row in config if row['role']=='positive']
    result = {'positive_labels':positives, 'precision':None, 'recall':None}
    if not positives:
        return result
    labels = [row['label'] for row in config]
    matrix = metrics.get('confusion_matrix')
    # For a binary classification the saved count/correct pairs exactly
    # determine all four cells. No individual predictions are reconstructed.
    if matrix is None and len(labels)==2:
        groups = metrics.get('per_class', {})
        valid = set(groups)==set(labels) and all(not isinstance(groups.get(label,{}).get(key),bool) and isinstance(groups.get(label,{}).get(key), (int,float)) and
                    math.isfinite(groups[label][key]) for label in labels for key in ('count','correct'))
        if valid and all(0<=groups[label]['correct']<=groups[label]['count'] for label in labels):
            matrix = {label:{other:groups[label]['correct'] if other==label else groups[label]['count']-groups[label]['correct'] for other in labels} for label in labels}
    if matrix is None or any(label not in matrix or set(matrix[label])!=set(labels) for label in labels):
        return result
    if any(isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<0 for row in matrix.values() for value in row.values()):
        return result
    tp = sum(matrix[a][p] for a in positives for p in positives)
    actual = sum(matrix[a][p] for a in positives for p in labels)
    predicted = sum(matrix[a][p] for a in labels for p in positives)
    result.update(precision=tp/predicted if predicted else None, recall=tp/actual if actual else None,
                  predicted_positive_count=predicted, actual_positive_count=actual)
    return result


def running_metric_series(events, config):
    """Recorded cumulative prequential measurements, never candidate scores."""
    points=[]
    for index,event in enumerate(events):
        if event.get('kind')!='cycle-metrics' or not isinstance(event.get('metrics'),dict):
            continue
        metrics=event['metrics']
        positive=positive_metrics(metrics,config)
        points.append({'event_index':index,'cycle_number':event.get('cycle_number'),
                       'count':metrics.get('count'),'scope':event.get('metric_scope'),
                       'accuracy':metrics.get('accuracy'),'precision':positive['precision'],
                       'recall':positive['recall'],'positive_labels':positive['positive_labels']})
    return points

"""Matched endpoint evaluation; never optimize, fit, or activate a classifier."""
from datetime import datetime, timezone
from .trace_classification import class_configuration, positive_metrics


async def compare_endpoints(wheel, before, after, audit, training, *, class_config, now=None):
    if not audit:
        raise ValueError('endpoint comparison requires audit labels')
    if before.config.task != after.config.task or before.config.task != wheel.initial.task:
        raise ValueError('endpoint comparison requires the same task')
    classes = class_configuration(before.config.task.labels, class_config)
    wheel._validate_partitions(training, audit, (), {row.item.id:1. for row in training})
    now = now or datetime.now(timezone.utc)
    endpoints = {}
    for name, classifier in (('before',before),('after',after)):
        metrics = await wheel._score(classifier, audit, training, now)
        endpoints[name] = {'fingerprint':classifier.fingerprint, 'accuracy':metrics['accuracy'],
            **positive_metrics(metrics,classes), 'metrics':metrics}
    if endpoints['before']['metrics']['sample_ids'] != endpoints['after']['metrics']['sample_ids']:
        raise ValueError('endpoint comparison must use identical audit samples')
    selected = set(endpoints['before']['metrics']['sample_ids'])
    return {'scope':'Matched protected audit: initial empty classifier versus final classifier',
        'sample_count':len(selected), 'class_counts':{label:sum(row.label==label and row.item.id in selected for row in audit)
            for label in before.config.task.labels},
        'evaluated_at':now.isoformat(), 'class_config':classes, **endpoints}

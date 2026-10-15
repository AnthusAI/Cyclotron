"""Explicit frozen-cyclotron evaluation: no fitting, optimization, or promotion."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import json

from .matched_run_plan import plan_matched_runs, fingerprint
from .flywheel import DecisionFlywheel, _restore
from .models import Item, LabeledItem, DecisionResult
from .classifier_config import ClassifiedAnswers
from .shared_decisions import SharedDecisions, feature_context_identity
from .rolling_metrics import recent_reviewed_metrics
from .trace_classification import comparison_metrics
from .output_comparison import compare_outputs
from .batched_classification import batch_request
from .decision_cache import CacheOptions


def _endpoint(source):
    refs = source['config']['classifiers']
    states = source['checkpoint']['payload']['classifiers']
    if set(states) != {row['id'] for row in refs}:
        raise ValueError('checkpoint must contain every endpoint classifier')
    fitted = {row['id']: _restore(deepcopy(states[row['id']]['state'])) for row in refs}
    items = {row['id']: Item(row['id'], deepcopy(row['values'])) for row in source['items']}
    active = {}
    cursor = source['checkpoint']['payload']['event_cursor']
    for row in source['events']:
        if row['sequence'] > cursor:
            continue
        event = row['payload']
        if event.get('kind') != 'human-feedback':
            continue
        feedback = event['feedback']
        key = (event.get('classifier_id'), feedback['item_id'])
        active.pop(key, None)
        if event.get('action') == 'submitted' and event.get('assignment') == 'training':
            active[key] = feedback
    training = {cid: [] for cid in fitted}
    for (cid, identifier), feedback in active.items():
        if cid not in fitted or identifier not in items:
            raise ValueError('training feedback lacks frozen classifier or item')
        comment = feedback.get('edit_comment_value')
        training[cid].append(LabeledItem(items[identifier],
            fitted[cid].config.task.validate_label(feedback['final_answer_value']),
            context={'human_feedback': comment} if comment else {}))
    for cid, classifier in fitted.items():
        if tuple(classifier.config.task.labels) != tuple(
                row['label'] for ref in refs if ref['id'] == cid for row in ref['config']['classes']):
            raise ValueError('checkpoint classes differ from frozen definition')
        if not set(classifier.config.example_ids).issubset({row.item.id for row in training[cid]}):
            raise ValueError('checkpoint examples lack training-only evidence at its boundary')
    return fitted, items, training


async def evaluate_matched_runs(plan, endpoints, models, directory, *, max_requests, observer,retry_failed=False,
                                execution_storage=None,batch_storage_factory=None):
    """Execute an approved preflight; repeat invocations reuse complete requests.

    The application supplies models and routes observer events to its API. The
    persisted evaluation clock prevents dynamic state changing on cache resume.
    """
    if type(max_requests) is not int or max_requests < plan['request_upper_bound']:
        raise ValueError('request ceiling is below preflight upper bound')
    options=CacheOptions(retry_failed=retry_failed)
    if plan != plan_matched_runs(endpoints['before'], endpoints['after'], limit=plan['limit']):
        raise ValueError('preflight changed; create a new approved plan')
    frozen = {side: _endpoint(endpoints[side]) for side in ('before', 'after')}
    # Validate both sides before collecting either side. An immutable endpoint
    # must not silently refit or fall back to raw output during comparison.
    for side,(fitted,_,training) in frozen.items():
        configs={cid:classifier.config for cid,classifier in fitted.items()}
        expected=feature_context_identity(models[side].model_identity,configs,training)
        for classifier in fitted.values():
            if classifier.head and classifier.head.provenance.source_model_provenance!=expected:
                raise ValueError('frozen head does not match the joint decision feature context; replay and refit this endpoint')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    from .evaluation_storage import SQLiteEvaluationStorage
    execution=execution_storage if execution_storage is not None else SQLiteEvaluationStorage(directory/'evaluation.sqlite3')
    clock=datetime.fromisoformat(execution.bind_clock(plan['fingerprint'],datetime.now(timezone.utc).isoformat()))
    def emit(event):
        # Re-delivery after acknowledgement failure retains the original ID and
        # exact persisted payload, including its first timestamp.
        identity=fingerprint({'plan':plan['fingerprint'],'event':event})
        identifier,payload=execution.append_trace(identity,json.dumps(
            {**event,'timestamp':event.get('timestamp') or datetime.now(timezone.utc).isoformat()},allow_nan=False))
        observer({**json.loads(payload),'event_id':identifier})
        return identifier
    metrics = {cid: {} for cid in plan['classifier_ids']}
    requests = new_requests = 0
    records = []
    cache_paths={side:directory/f"{plan['fingerprint']}-{side}.sqlite3" for side in ('before','after')}
    from .batch_storage import SQLiteBatchStorage
    open_batches=batch_storage_factory if batch_storage_factory is not None else lambda side:SQLiteBatchStorage(cache_paths[side])
    attempts={}
    # Each factory call transfers one fresh handle; reuse the same fixed side's
    # durable namespace. Read totals through BatchStorage, never a SQL counter.
    for side in cache_paths:
        storage=open_batches(side)
        try: attempts[side]=storage.requests()
        finally: storage.close()
    for side in ('before', 'after'):
        fitted, items, training = frozen[side]
        configs = {cid: classifier.config for cid, classifier in fitted.items()}
        shared = SharedDecisions(cache_paths[side], models[side],
            max_requests=max_requests-sum(count for key,count in attempts.items() if key!=side),
            observer=lambda event: emit({**event, 'endpoint': side}),storage=open_batches(side))
        start_requests = shared.requests
        output = {cid: [] for cid in metrics}
        try:
            for target in plan['items']:
                request,_=batch_request(configs,items[target['id']],training,now=clock)
                payload, _ = await shared.prepare(configs, items[target['id']], training, now=clock,options=options)
                for exchange in payload['exchanges']:
                    emit({**exchange, 'endpoint': side, 'target_id': target['id'],
                              'request_fingerprint': shared.identity})
                raw = payload['result']
                target_records=[]
                for cid in metrics:
                    batch = ClassifiedAnswers({name: DecisionResult(**answer)
                        for name, answer in raw['answers'][cid].items()}, raw['model'], None, raw['latency_ms'])
                    classifier = fitted[cid]
                    main = batch.answers['decision']
                    if classifier.head:
                        features = DecisionFlywheel._features(classifier.config, batch)
                        probabilities = classifier.head.probabilities(features)
                        label = classifier.head.predict(features)
                    else:
                        label, probabilities = main.label, main.probabilities
                    output[cid].append((target['id'], target['labels'][cid], label, probabilities))
                    target_records.append({'endpoint': side, 'classifier_id': cid, 'item_id': target['id'],
                        'actual_label':target['labels'][cid],
                        'label': label, 'probabilities': probabilities,
                        'decision_model_label': main.label, 'decision_model_probabilities': main.probabilities,
                        'request_fingerprint': shared.identity})
                trace_id=emit({'kind':'matched-evaluation-target','endpoint':side,'target_id':target['id'],
                    'item':dict(items[target['id']].values),'labels':target['labels'],
                    'request_fingerprint':shared.identity,'request':request,'response':raw,
                    'exchanges':payload['exchanges'],'checkpoint_fingerprint':plan['endpoints'][side]['checkpoint_fingerprint'],
                    'outputs':{record['classifier_id']:record for record in target_records}})
                records.extend({**record,'trace_event_id':trace_id} for record in target_records)
            for cid in metrics:
                summary = recent_reviewed_metrics(configs[cid].task.labels, output[cid])
                ref = next(row for row in endpoints[side]['config']['classifiers'] if row['id'] == cid)
                summary.update(comparison_metrics(summary, ref['config']['classes']))
                summary['class_config']=ref['config']['classes']
                summary['decision_model_comparison']=compare_outputs(configs[cid].task.labels,
                    [row for row in records if row['endpoint']==side and row['classifier_id']==cid],scope='protected-matched')
                metrics[cid][side] = summary
        finally:
            requests += shared.requests
            attempts[side]=shared.requests
            new_requests += shared.requests - start_requests
            shared.close()
    result = {'plan_fingerprint': plan['fingerprint'], 'scope': plan['scope'],
              'sample_count': plan['sample_count'], 'class_counts': plan['class_counts'],
              'classifiers': metrics, 'records': records, 'requests': requests, 'new_requests': new_requests}
    emit({'kind': 'matched-evaluation-completed', 'result': result})
    return result

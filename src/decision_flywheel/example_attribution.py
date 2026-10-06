"""Bounded, matched development experiments for individual context examples.

Effects are conditional on the other examples and their fixed display order.
Supporting-question confidence is not ground truth for those questions.
"""
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
from .classification_metrics import classification_metrics
from .flywheel import _hash, _json


def plan_swaps(config, training, *, preferred_ids=(), max_trials=8):
    if type(max_trials) is not int or max_trials<1:
        raise ValueError('swap trial ceiling must be positive')
    pool={row.item.id:row for row in training}
    if len(pool)!=len(training) or any(key not in pool for key in (*config.example_ids,*preferred_ids)):
        raise ValueError('examples and preferred candidates must be unique trusted training items')
    preferred=list(dict.fromkeys(preferred_ids))
    candidates=preferred+sorted(set(pool)-set(preferred))
    by_slot=[[key for key in candidates if key not in config.example_ids and
              pool[key].label==pool[old].label] for old in config.example_ids]
    trials=[]
    for rank in range(max((len(group) for group in by_slot),default=0)):
        for slot,group in enumerate(by_slot):
            if rank>=len(group):continue
            ids=list(config.example_ids);old=ids[slot];ids[slot]=group[rank]
            trials.append({'slot':slot,'removed_id':old,'added_id':ids[slot],
                'config':replace(config,example_ids=tuple(ids),parent_fingerprint=config.fingerprint)})
            if len(trials)==max_trials:return tuple(trials)
    return tuple(trials)


def _metrics(classifier, rows, batches):
    distributions=[];labels=[]
    for batch in batches:
        if classifier.head:
            from .flywheel import DecisionFlywheel
            features=DecisionFlywheel._features(classifier.config,batch)
            p=classifier.head.probabilities(features);label=classifier.head.predict(features)
        else:
            answer=batch.answers['decision'];p=answer.probabilities;label=answer.label
        distributions.append({k:v/sum(p.values()) for k,v in p.items()});labels.append(label)
    return classification_metrics(classifier.config.task.labels,[r.label for r in rows],labels,distributions)


def _question_means(config, rows, batches):
    tasks={'decision':config.task,**{task.name:task for task in config.tasks}}
    return {name:{label:{option:sum(batch.answers[name].probabilities[option]/sum(batch.answers[name].probabilities.values())
        for row,batch in zip(rows,batches) if row.label==label)/sum(row.label==label for row in rows)
        for option in task.labels} for label in config.task.labels} for name,task in tasks.items()}


async def measure_example_swaps(wheel, training, development, *, protected, propensities,
                               preferred_ids=(), max_trials=8, limit=200):
    """Measure but never fit or promote. Persist progress and reuse exact requests."""
    wheel._validate_partitions(training,development,protected,propensities)
    if type(limit) is not int or not 1<=limit<=200:
        raise ValueError('matched evaluation sample limit must be between 1 and 200')
    baseline=wheel.active
    swaps=plan_swaps(baseline.config,training,preferred_ids=preferred_ids,max_trials=max_trials)
    rows=wheel.evaluation_policy.select(development,baseline.config.task.labels)[:limit]
    if not swaps or not rows or any(not any(row.label==label for row in rows) for label in baseline.config.task.labels):
        return {'stage':'examples','rankings':[],'reason':'need an incumbent list, same-class alternatives and development coverage for every class','recommended_proposal':None}
    key=_hash({'baseline':baseline.fingerprint,'model':wheel.model.model_identity,
        'training':wheel._evidence(training),'development':wheel._evidence(rows),
        'samples':[row.item.id for row in rows],'swaps':[(s['slot'],s['added_id']) for s in swaps],
        'cache_options':asdict(wheel.cache_options)})
    wheel.db.execute('CREATE TABLE IF NOT EXISTS example_measurements (id TEXT PRIMARY KEY,status TEXT,payload TEXT)')
    saved=wheel.db.execute('SELECT status,payload FROM example_measurements WHERE id=?',(key,)).fetchone()
    if saved and saved[0]=='complete' and wheel.cache_options.policy!='refresh':return json.loads(saved[1])
    now=datetime.fromisoformat(json.loads(saved[1])['evaluation_time']) if saved else datetime.now(timezone.utc)
    configs=(baseline.config,*(swap['config'] for swap in swaps))
    bound=0
    for config in configs:
        for row in rows:
            request=config.request(row.item,training,now=now)
            request_key=_hash({'model':wheel.model.model_identity,'request':request})
            cached=wheel.db.execute('SELECT status FROM runtime_answers WHERE key=?',(request_key,)).fetchone()
            bound+=wheel.cache_options.policy=='refresh' or not cached or cached[0]!='complete'
    wheel._emit({'kind':'example-swaps-planned','stage':'examples','measurement_fingerprint':key,
        'sample_ids':[r.item.id for r in rows],'trial_count':len(swaps),'uncached_request_upper_bound':bound})
    if wheel.cache_options.policy!='cache_only' and wheel.requests+bound>wheel.max_requests:
        return {'stage':'examples','rankings':[],'recommended_proposal':None,'reason':'swap experiments exceed remaining request ceiling','request_upper_bound':bound}
    with wheel.db:
        wheel.db.execute("INSERT OR REPLACE INTO example_measurements VALUES (?,'pending',?)",(key,_json({'evaluation_time':now.isoformat()})))
    base_batches=[await wheel._answers(baseline.config,row.item,training,now) for row in rows]
    base_metrics=_metrics(baseline,rows,base_batches);base_means=_question_means(baseline.config,rows,base_batches)
    base_decision_metrics=_metrics(replace(baseline,head=None),rows,base_batches)
    examples={row.item.id:row for row in training}
    metric='balanced_brier' if wheel.evaluation_weighting=='equal_class' else 'brier'
    rankings=[]
    for swap in swaps:
        candidate=replace(baseline,config=swap['config'])
        batches=[await wheel._answers(candidate.config,row.item,training,now) for row in rows]
        metrics=_metrics(candidate,rows,batches);means=_question_means(candidate.config,rows,batches)
        effects={name:{label:{option:value-base_means[name][label][option] for option,value in options.items()}
            for label,options in groups.items()} for name,groups in means.items()}
        entry={k:v for k,v in swap.items() if k!='config'}
        entry['examples']={role:{'id':key,'label':examples[key].label,'values':dict(examples[key].item.values),
            'explanation':examples[key].context.get('human_feedback')}
            for role,key in (('removed',swap['removed_id']),('added',swap['added_id']))}
        entry.update(candidate=metrics,example_ids=list(candidate.config.example_ids),
            decision_metrics=_metrics(replace(candidate,head=None),rows,batches),
            brier_gain=base_metrics[metric]-metrics[metric],accuracy_change=metrics['accuracy']-base_metrics['accuracy'],
            question_effects=effects,question_means=means,
            recall_safeguards_pass=all(metrics['per_class'][label]['recall']>=base_metrics['per_class'][label]['recall'] for label in candidate.config.task.labels))
        rankings.append(entry)
        wheel._emit({'kind':'candidate-evaluated','stage':'examples','experiment':'single-example-swap',
            'measurement_fingerprint':key,'incumbent':base_metrics,**entry,'promoted':False})
    rankings.sort(key=lambda r:(-r['brier_gain'],-r['accuracy_change'],r['slot'],r['added_id']))
    winners=[r for r in rankings if r['brier_gain']>0 and r['recall_safeguards_pass']]
    report={'stage':'examples','measurement_fingerprint':key,'scope':'matched development selection; not held-out validation',
        'effect_scope':'conditional on fixed rubric, questions, remaining examples, order and learned head; not an intrinsic example score',
        'supporting_question_scope':'probability shifts grouped by final human label; not correctness labels for supporting questions',
        'sample_ids':[r.item.id for r in rows],'count':len(rows),'by_class':{label:sum(r.label==label for r in rows) for label in baseline.config.task.labels},
        'evaluation_time':now.isoformat(),'request_upper_bound':bound,'baseline_fingerprint':baseline.fingerprint,
        'training_evidence':wheel._evidence(training),'development_evidence':wheel._evidence(rows),
        'incumbent':base_metrics,'incumbent_decision_metrics':base_decision_metrics,
        'baseline_question_means':base_means,'promotion_metric':metric,'rankings':rankings,
        'recommended_proposal':{'example_ids':winners[0]['example_ids'],'rationale':'Best measured one-example swap on matched development items'} if winners else None}
    with wheel.db:
        wheel.db.execute("UPDATE example_measurements SET status='complete',payload=? WHERE id=?",(_json(report),key))
    wheel._emit({'kind':'example-ranking-completed',**report})
    return report

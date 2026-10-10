"""Sequential operational replay: predict first, reveal one historical vote, then act."""
from .feedback import FeedbackItem, LABEL_SOURCE_VETTED
from .calibration_history import reviewed_calibration_metrics
from .replay_feedback_policy import ReplayFeedbackPolicy
from .output_comparison import compare_outputs

TRIGGER_KINDS=('human-feedback','trigger-evaluated')
CALIBRATION_KINDS=('prediction','displayed-prediction-reused','human-feedback')


def _events_of(wheel,kinds):
    """Only the event kinds a per-cycle check reads; parsing every event made each cycle slower than the last."""
    import json
    marks=','.join('?'*len(kinds))
    rows=wheel.db.execute(f"SELECT id,payload FROM runtime_events WHERE json_extract(payload,'$.kind') IN ({marks}) ORDER BY id",
                          tuple(kinds)).fetchall()
    return tuple({**json.loads(payload),'event_id':event_id} for event_id,payload in rows)


async def run_cycle_replay(wheel, plan, *, optimize_every=20, retrain_every=20,
                           stages=('rubric','questions','examples'), min_evaluation_per_class=2,
                           on_cycle=None, rubric_changes_every=2, feedback_policy=None, negative_label=None,
                           max_rubric_optimizations=None, initial_training=(), initial_development=(),
                           resume=False, retry_failed_requests=False,
                           rubric_trigger_basis='label_transitions'):
    from .feedback_trigger import LabelTransitionTrigger
    if rubric_trigger_basis not in {'label_transitions','revealed_feedback_count'}:
        raise ValueError('rubric_trigger_basis must be label_transitions or revealed_feedback_count')
    rubric_trigger = (LabelTransitionTrigger(rubric_changes_every)
                      if rubric_changes_every is not None and 'rubric' in stages and rubric_trigger_basis=='label_transitions'
                      else None)
    feedback_rubric_trigger = bool(rubric_changes_every is not None and 'rubric' in stages and
                                   rubric_trigger_basis=='revealed_feedback_count')
    scheduled_stages = tuple(stage for stage in stages if not (rubric_trigger or feedback_rubric_trigger) or stage != 'rubric')
    if any(type(value) is not int or value<1 for value in (optimize_every,retrain_every,min_evaluation_per_class)):
        raise ValueError('cycle cadences and coverage must be positive integers')
    if not stages or any(stage not in {'rubric','questions','examples'} for stage in stages):
        raise ValueError('choose separate decision-context optimization stages')
    prior_events=wheel.history(100000)
    # Declaring a selection policy at construction records its configuration;
    # that alone does not make a runtime used.
    configuration_only={'selection-policy-configured'}
    if not resume and (any(event.get('kind') not in configuration_only for event in prior_events) or wheel.active.head or wheel.active.config.example_ids or wheel.active.config.tasks):
        raise ValueError('operational replay requires a fresh empty runtime')
    if max_rubric_optimizations is not None and (type(max_rubric_optimizations) is not int or max_rubric_optimizations < 1):
        raise ValueError('max_rubric_optimizations must be a positive integer or None')
    if type(retry_failed_requests) is not bool:
        raise ValueError('retry_failed_requests must be boolean')
    policy=feedback_policy or ReplayFeedbackPolicy()
    if not callable(getattr(policy,'select',None)) or not callable(getattr(policy,'manifest',None)):
        raise ValueError('feedback_policy must expose select() and manifest()')
    if negative_label is None:
        if policy.mode != 'all':
            raise ValueError('operational selective feedback requires an explicit negative_label')
        negative_label=wheel.initial.task.labels[-1]
    wheel.initial.task.validate_label(negative_label)
    initial_training,initial_development=tuple(initial_training),tuple(initial_development)
    roles={row.item.id:role for role,group in (('training',plan.training),('development',plan.development),('scoreboard',plan.scoreboard),('training',initial_training),('development',initial_development)) for row in group}
    rows_by_id={row.item.id:row for row in plan.ordered}
    if len(rows_by_id)!=len(plan.ordered):
        raise ValueError('operational replay requires unique item IDs')
    report={'protocol':plan.manifest(wheel.initial.task),'cycles':[], 'feedback_policy':policy.manifest(negative_label)}
    report['rubric_trigger'] = ({'policy':'label-transitions','every':rubric_changes_every}
                                if rubric_trigger else {'policy':'revealed-feedback-count','every':rubric_changes_every}
                                if feedback_rubric_trigger else {'policy':'feedback-cadence','every':optimize_every})
    optimization_number=0
    rubric_optimizations=0
    eligible_count=0
    revealed_rows=[*initial_training,*initial_development]
    revealed_propensities={row.item.id:1. for row in revealed_rows}
    all_item_evaluation=[]
    completed_ids=set()
    if resume:
        started={event.get('cycle_id'):event for event in prior_events if event.get('kind')=='cycle-started'}
        closed={event.get('cycle_id') for event in prior_events if event.get('kind') in {'cycle-completed','cycle-failed'}}
        unfinished=set(started)-closed
        if unfinished:
            raise ValueError('operational replay has an unfinished cycle; inspect or explicitly resume that cycle first')
        completed_ids={event.get('cycle_item_id') for cycle_id,event in started.items()
                       if cycle_id in closed and any(value.get('kind')=='cycle-completed' and value.get('cycle_id')==cycle_id for value in prior_events)}
        expected=[row.item.id for row in plan.ordered[:len(completed_ids)]]
        if set(expected)!=completed_ids:
            raise ValueError('completed replay cycles are not a contiguous prefix of the frozen plan')
        for event in prior_events:
            if event.get('kind')!='human-feedback':
                continue
            payload=event.get('feedback',{})
            identifier=payload.get('item_id')
            if identifier not in rows_by_id or identifier in revealed_propensities:
                raise ValueError('replay feedback does not match the frozen plan')
            row=rows_by_id[identifier]
            if payload.get('final_answer_value')!=row.label:
                raise ValueError('replay feedback label does not match the frozen reference')
            revealed_rows.append(row)
            revealed_propensities[identifier]=payload.get('selection_propensity')
        eligible_count=len(revealed_rows)-len(initial_training)-len(initial_development)
        rubric_optimizations=sum(event.get('kind')=='step-started' and event.get('step_stage')=='rubric'
                                 for event in prior_events)
        for event in prior_events:
            if event.get('kind')!='prediction' or event.get('target_id') not in completed_ids:
                continue
            row=rows_by_id[event['target_id']]
            all_item_evaluation.append({'item_id':row.item.id, 'actual_label':row.label,
                'decision_model_label':event['decision_model_label'], 'decision_model_probabilities':event['decision_model_probabilities'],
                'label':event['label'], 'probabilities':event['probabilities']})
        report['resumed_from_cycles']=len(completed_ids)
    for index,row in enumerate(plan.ordered):
        if row.item.id in completed_ids:
            continue
        train=tuple(value for value in revealed_rows if roles[value.item.id]=='training')
        dev=tuple(value for value in revealed_rows if roles[value.item.id]=='development')
        with wheel.cycle(row.item,reason='historical-feedback-replay') as cycle:
            if retry_failed_requests:
                from .decision_cache import CacheOptions
                result=await wheel.predict(row.item,train,cache_options=CacheOptions(retry_failed=True))
            else:
                result=await wheel.predict(row.item,train)
            selection=policy.select(item_id=row.item.id,predicted_label=result.label,negative_label=negative_label,
                                    cycle_number=index+1,confidence=result.probabilities[result.label])
            selected=selection.selected
            # This is an evaluator-only record.  It is deliberately not emitted
            # into wheel history, optimizer context, or a feedback event.
            prediction=next(event for event in reversed(wheel.history(20))
                            if event['kind']=='prediction' and event['target_id']==row.item.id)
            all_item_evaluation.append({'item_id':row.item.id, 'actual_label':row.label,
                'decision_model_label':prediction['decision_model_label'], 'decision_model_probabilities':prediction['decision_model_probabilities'],
                'label':result.label, 'probabilities':result.probabilities})
            if selected:
                # Scoreboard is a permanent evaluation firewall: do not reveal
                # its labels to the runtime, even when sampling would select it.
                if roles[row.item.id] == 'scoreboard':
                    selected=False
                else:
                    feedback=FeedbackItem(f'replay:{index+1}',row.item.id,wheel.initial.task.name,
                    initial_answer_value=result.label,final_answer_value=row.label,
                    edit_comment_value=row.context.get('human_feedback'),label_source=LABEL_SOURCE_VETTED,
                        selection_propensity=selection.propensity,review_provenance='historical-human-vote-replayed-after-prediction')
                    wheel.record_feedback_event(feedback,assignment=roles[row.item.id])
                    revealed_rows.append(row)
                    revealed_propensities[row.item.id]=selection.propensity
            eligible=selected and roles[row.item.id]!='scoreboard'
            eligible_count+=int(eligible)
            train=tuple(value for value in revealed_rows if roles[value.item.id]=='training')
            dev=tuple(value for value in revealed_rows if roles[value.item.id]=='development')
            wheel.set_optimizer_context(tuple(r.context['human_feedback'] for r in train if r.context.get('human_feedback')))
            protected=tuple(r.item for r in plan.ordered if r.item.id not in {r.item.id for r in (*train,*dev)})
            outcomes=[]
            kwargs={'protected':protected,'propensities':{r.item.id:revealed_propensities[r.item.id] for r in train},
                    'min_development_per_class':min_evaluation_per_class}
            if rubric_trigger:
                check=rubric_trigger.check(_events_of(wheel,TRIGGER_KINDS))
                cycle.check_trigger('rubric',**check)
                if check['due'] and (max_rubric_optimizations is None or rubric_optimizations < max_rubric_optimizations):
                    outcomes.append(await wheel.step('rubric',train,dev,trigger='label-transitions',**kwargs))
                    rubric_optimizations += 1
            elif feedback_rubric_trigger:
                prior_due=any(event.get('kind')=='trigger-evaluated' and event.get('stage')=='rubric' and
                              event.get('details',{}).get('policy')=='revealed-feedback-count' and
                              event.get('details',{}).get('feedback_count')==eligible_count
                              for event in _events_of(wheel,('trigger-evaluated',)))
                due=eligible and eligible_count%rubric_changes_every==0 and not prior_due
                cycle.check_trigger('rubric',due=due,
                    reason='revealed feedback cadence reached' if due else 'revealed feedback cadence not reached',
                    details={'policy':'revealed-feedback-count','threshold':rubric_changes_every,
                             'feedback_count':eligible_count})
                if due and (max_rubric_optimizations is None or rubric_optimizations < max_rubric_optimizations):
                    outcomes.append(await wheel.step('rubric',train,dev,trigger='revealed-feedback-count',**kwargs))
                    rubric_optimizations += 1
            if scheduled_stages:
                stage=scheduled_stages[optimization_number%len(scheduled_stages)]
                due=eligible and eligible_count%optimize_every==0
                cycle.check_trigger(stage,due=due,reason='feedback cadence reached' if due else 'feedback cadence not reached',
                                    details={'feedback_count':eligible_count,'threshold':optimize_every})
                if due:
                    outcomes.append(await wheel.step(stage,train,dev,trigger='feedback-cadence',**kwargs))
                    optimization_number+=1
            fit_due=eligible and eligible_count%retrain_every==0
            cycle.check_trigger('classifier',due=fit_due,reason='retraining cadence reached' if fit_due else 'retraining cadence not reached',
                                details={'feedback_count':eligible_count,'threshold':retrain_every})
            if fit_due:
                outcomes.append(await wheel.step('classifier',train,dev,trigger='retraining-cadence',**kwargs))
            metrics=reviewed_calibration_metrics(wheel.initial.task.labels,_events_of(wheel,CALIBRATION_KINDS))
            wheel._emit({'kind':'cycle-metrics','metric_scope':'latest 200 human-reviewed pre-vote predictions; descriptive, not held-out',
                         'metrics':metrics,'training_count':len(train),'development_count':len(dev)})
            report['cycles'].append({'cycle_id':cycle.context['cycle_id'],'cycle_number':index+1,
                'item_id':row.item.id,'predicted_label':result.label,'human_label':row.label if selected else None,'feedback_selected':selected,
                'feedback_propensity':selection.propensity if selected else None,
                'feedback_inclusion_propensity':selection.inclusion_propensity,
                'role':roles[row.item.id],'metrics':metrics,'outcomes':outcomes,
                'classifier_version':wheel.active.fingerprint,'requests':wheel.requests})
        if on_cycle:
            on_cycle(report)
        if any(outcome['status'] in {'failed','partial','paused'} for outcome in outcomes):
            report['stopped_reason']='triggered step did not complete; inspect its trace before retrying'
            break
    report['all_item_evaluator'] = compare_outputs(wheel.initial.task.labels, all_item_evaluation,
        limit=min(200, len(all_item_evaluation)), scope='replay-oracle') if all_item_evaluation else None
    return report

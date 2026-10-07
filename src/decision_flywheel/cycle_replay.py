"""Sequential operational replay: predict first, reveal one historical vote, then act."""
from .feedback import FeedbackItem, LABEL_SOURCE_VETTED
from .calibration_history import reviewed_calibration_metrics


async def run_cycle_replay(wheel, plan, *, optimize_every=20, retrain_every=20,
                           stages=('rubric','questions','examples'), min_evaluation_per_class=2,
                           on_cycle=None, rubric_changes_every=2):
    from .feedback_trigger import LabelTransitionTrigger
    rubric_trigger = LabelTransitionTrigger(rubric_changes_every) if rubric_changes_every is not None and 'rubric' in stages else None
    scheduled_stages = tuple(stage for stage in stages if not rubric_trigger or stage != 'rubric')
    if any(type(value) is not int or value<1 for value in (optimize_every,retrain_every,min_evaluation_per_class)):
        raise ValueError('cycle cadences and coverage must be positive integers')
    if not stages or any(stage not in {'rubric','questions','examples'} for stage in stages):
        raise ValueError('choose separate decision-context optimization stages')
    if wheel.history(1) or wheel.active.head or wheel.active.config.rubric or wheel.active.config.example_ids or wheel.active.config.tasks:
        raise ValueError('operational replay requires a fresh empty runtime')
    roles={row.item.id:role for role,group in (('training',plan.training),('development',plan.development),('scoreboard',plan.scoreboard)) for row in group}
    report={'protocol':plan.manifest(wheel.initial.task),'cycles':[]}
    report['rubric_trigger'] = {'policy':'label-transitions','every':rubric_changes_every} if rubric_trigger else {'policy':'feedback-cadence','every':optimize_every}
    optimization_number=0
    eligible_count=0
    for index,row in enumerate(plan.ordered):
        train,dev=plan.revealed(index)
        with wheel.cycle(row.item,reason='historical-feedback-replay') as cycle:
            result=await wheel.predict(row.item,train)
            feedback=FeedbackItem(f'replay:{index+1}',row.item.id,wheel.initial.task.name,
                initial_answer_value=result.label,final_answer_value=row.label,
                edit_comment_value=row.context.get('human_feedback'),label_source=LABEL_SOURCE_VETTED,
                selection_propensity=1.,review_provenance='historical-human-vote-replayed-after-prediction')
            wheel.record_feedback_event(feedback,assignment=roles[row.item.id])
            eligible=roles[row.item.id]!='scoreboard'
            eligible_count+=int(eligible)
            train,dev=plan.revealed(index+1)
            wheel.set_optimizer_context(tuple(r.context['human_feedback'] for r in train if r.context.get('human_feedback')))
            protected=tuple(r.item for r in plan.ordered if r.item.id not in {r.item.id for r in (*train,*dev)})
            outcomes=[]
            kwargs={'protected':protected,'propensities':{r.item.id:1. for r in train},
                    'min_development_per_class':min_evaluation_per_class}
            if rubric_trigger:
                import json
                check=rubric_trigger.check(json.loads(r[0]) for r in wheel.db.execute('SELECT payload FROM runtime_events ORDER BY id'))
                cycle.check_trigger('rubric',**check)
                if check['due']:
                    outcomes.append(await wheel.step('rubric',train,dev,trigger='label-transitions',**kwargs))
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
            metrics=reviewed_calibration_metrics(wheel.initial.task.labels,wheel.history(100000))
            wheel._emit({'kind':'cycle-metrics','metric_scope':'latest 200 human-reviewed pre-vote predictions; descriptive, not held-out',
                         'metrics':metrics,'training_count':len(train),'development_count':len(dev)})
            report['cycles'].append({'cycle_id':cycle.context['cycle_id'],'cycle_number':index+1,
                'item_id':row.item.id,'predicted_label':result.label,'human_label':row.label,
                'role':roles[row.item.id],'metrics':metrics,'outcomes':outcomes,
                'classifier_version':wheel.active.fingerprint,'requests':wheel.requests})
        if on_cycle:
            on_cycle(report)
        if any(outcome['status'] in {'failed','partial','paused'} for outcome in outcomes):
            report['stopped_reason']='triggered step did not complete; inspect its trace before retrying'
            break
    return report

"""Separately scheduled controls with persistent discovery and measurement."""
import json
from dataclasses import asdict
from datetime import datetime,timezone

from .flywheel import _hash, _json, track_answer_dependencies
from .optimizer_agent import FeedbackBriefing
from .question_measurement import measure_questions
from .candidate_fitting import fit_candidate


def _example_experiments(wheel, training, development):
    if not wheel.db.execute("SELECT name FROM sqlite_master WHERE name='example_measurements'").fetchone():return []
    current_train=wheel._evidence(training);current_dev=wheel._evidence(development)
    reports=[]
    for payload, in wheel.db.execute("SELECT payload FROM example_measurements WHERE status='complete' ORDER BY rowid DESC"):
        report=json.loads(payload)
        if report.get('answer_dependencies') is None or not wheel.answer_dependencies_current(report['answer_dependencies']):continue
        if any(current_train.get(k)!=v for k,v in report.get('training_evidence',{}).items()) or any(current_dev.get(k)!=v for k,v in report.get('development_evidence',{}).items()):continue
        if not report.get('development_evidence'):continue
        summary={key:report[key] for key in ('baseline_fingerprint','scope','effect_scope','by_class')}
        summary['rankings']=[{key:row[key] for key in ('examples','brier_gain','accuracy_change','question_effects')}
                            for row in report['rankings'][:4]]
        reports.append(summary)
        if len(reports)==3:break
    return reports


def stage_briefing(wheel, stage, training, development, protected):
    control = {"rubric": "rubric", "examples": "example_ids", "questions": "tasks"}.get(stage)
    if control is None:
        raise ValueError("this stage has no optimizer request")
    measurements=_example_experiments(wheel,training,development) if stage=='examples' else []
    return FeedbackBriefing.build(wheel.initial.task, training,
        current={**wheel.active.config.briefing_state(), "control_under_test": control,
                 "selection_policy":asdict(wheel.selection_policy) if wheel.selection_policy else None,
                 "stage": stage, "request_budget_bytes": wheel.max_request_bytes,
                 **({'example_experiments':measurements} if stage=='examples' else {})},
        protected=tuple(row.item for row in development)+tuple(protected),
        human_explanations=wheel.optimizer_context["human_explanations"])


@track_answer_dependencies
async def optimize_stage(wheel, stage, training, development, *, protected, propensities,
                         limit=200, retry_interrupted=False, min_development_per_class=20,
                         train_after_questions=True, max_example_trials=8):
    if stage == "classifier":
        from .classifier_training import train_classifier
        return await train_classifier(wheel, training, development, protected=protected,
            propensities=propensities, min_development_per_class=min_development_per_class,
            retry_interrupted=retry_interrupted)
    controls = {"rubric": "rubric", "examples": "example_ids", "questions": "tasks"}
    if stage not in controls:
        raise ValueError("stage must be rubric, examples or questions")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("retrospective limit must be a positive integer")
    if type(max_example_trials) is not int or max_example_trials<1:
        raise ValueError('example trial ceiling must be positive')
    if isinstance(min_development_per_class, bool) or not isinstance(min_development_per_class, int) or min_development_per_class < 1:
        raise ValueError("stage evaluation floor must be a positive integer")
    wheel._validate_partitions(training, development, protected, propensities)
    if not training:
        result = {"stage": stage, "promoted": False, "reason": "waiting for eligible human feedback"}
        wheel._emit({"kind": "optimization-stage-completed", **result})
        return result
    wheel.reconcile_feedback(training, development=development)
    wheel.reconcile_model_context(training)
    if stage=='examples' and _example_experiments(wheel,training,development):
        wheel.set_optimizer_context(wheel.optimizer_context['human_explanations'],evaluation_context_exposed=True)
    key_data = {"stage": stage, "context": wheel.active.config.fingerprint,
                 "model_context": wheel.model_context(wheel.active.config, training),
                 "training": wheel._evidence(training), "development": wheel._evidence(development),
                 "protected": sorted(item.id for item in protected), "propensities": propensities,
                 "limit": limit, "evaluation_floor": min_development_per_class,
                 "evaluation_weighting": wheel.evaluation_weighting, "training_class_weighting": wheel.training_class_weighting}
    key_data['context_validation_floor']=wheel.context_validation_floor
    key_data['head_fitting']={'refit_on_context_activation':True,'minimum_training_per_class':3}
    from dataclasses import asdict
    key_data['selection_policy']=asdict(wheel.selection_policy) if wheel.selection_policy else None
    key_data['evaluation_policy']=asdict(wheel.evaluation_policy)
    key_data["optimizer_context"] = wheel.optimizer_context
    predictions = {row.item.id: row.initial_answer_value for row in training
                   if row.initial_answer_value is not None}
    if predictions:
        key_data["initial_answers"] = predictions
    key_data["train_after_questions"] = train_after_questions
    if stage=='examples':key_data['max_example_trials']=max_example_trials
    key = _hash(key_data)
    wheel.db.execute("CREATE TABLE IF NOT EXISTS optimization_stages (id TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT)")
    key, saved = wheel.cached_artifact('optimization_stages', 'id', key)
    if saved and saved[0] == "complete":
        result = json.loads(saved[1])
        if stage == "questions" and train_after_questions:
            from .classifier_training import train_classifier
            result["classifier_training"] = await train_classifier(wheel, training, development,
                protected=protected, propensities=propensities,
                min_development_per_class=min_development_per_class, retry_interrupted=retry_interrupted)
        return result
    if saved and not retry_interrupted:
        return {"stage": stage, "promoted": False, "reason": "interrupted stage requires explicit retry"}
    proposal = json.loads(saved[1]).get("proposal") if saved and saved[1] else None
    pending_key=None
    if proposal is None and not saved:
        for old_key,payload in wheel.db.execute("SELECT id,payload FROM optimization_stages WHERE status='complete' ORDER BY rowid DESC"):
            previous=json.loads(payload)
            evidence=previous.get('proposal_training_evidence',{})
            if (previous.get('stage')==stage and previous.get('pending_evaluation') and
                previous.get('basis_context_version')==wheel.active.config.fingerprint and
                evidence and all(wheel._evidence(training).get(k)==v for k,v in evidence.items())):
                proposal=previous['proposal'];pending_key=old_key
                wheel._emit({'kind':'pending-proposal-reused','stage':stage,'source_stage_key':old_key,
                             'proposal':proposal,'reason':'reevaluate retained proposal with current development coverage'})
                break
    control = controls[stage]
    with wheel.db:
        wheel.db.execute("INSERT OR REPLACE INTO optimization_stages VALUES (?, 'pending', ?)",
                         (key, _json({"proposal": proposal})))
    wheel._emit({"kind": "optimization-stage-started", "stage": stage, "context_version": wheel.active.config.fingerprint})
    if proposal is None:
        briefing = stage_briefing(wheel, stage, training, development, protected)
        recorded = next((event for event in reversed(wheel.history(1000))
                         if event["kind"] == "optimizer-response" and
                         event.get("briefing_fingerprint") == briefing.fingerprint), None) if retry_interrupted else None
        proposal = json.loads(recorded["content"]) if recorded else wheel.optimizer.propose(briefing)
        with wheel.db:
            wheel.db.execute("UPDATE optimization_stages SET payload=? WHERE id=?", (_json({"proposal": proposal}), key))
        if recorded:
            wheel._emit({"kind": "optimizer-response-reused", "briefing_fingerprint": briefing.fingerprint,
                         "reason": "explicit recovery; no additional optimizer request"})
    if stage == "questions" and isinstance(proposal.get("tasks"), list):
        tasks = []
        for task in proposal["tasks"]:
            if isinstance(task, dict) and "input_field" in task:
                if task["input_field"] != wheel.initial.task.input_field:
                    raise ValueError("optimizer cannot change the task input field")
                task = {k: v for k, v in task.items() if k != "input_field"}
            tasks.append(task)
        proposal = {**proposal, "tasks": tasks}
    if set(proposal)-{"rationale", control} or control not in proposal:
        raise ValueError("stage proposal must change only its assigned control")
    config=wheel.active.config.apply(proposal, training)
    if stage=='rubric' and not config.rubric.strip():
        result={'stage':stage,'proposal':proposal,'activated':False,'promoted':False,
                'validation_status':'insufficient-evidence' if not wheel.active.config.rubric.strip() else 'invalid-proposal',
                'reason':'no usable rubric proposed; active configuration retained and future feedback may trigger another attempt',
                'basis_context_version':key_data['context'],'proposal_training_evidence':key_data['training'],
                'answer_dependencies':wheel.collected_answer_dependencies(),
                'evaluation_independent_of_optimizer_context':not wheel.optimizer_context['evaluation_context_exposed']}
        with wheel.db:
            wheel.db.execute("UPDATE optimization_stages SET status='complete',payload=? WHERE id=?",(_json(result),key))
        wheel._emit({'kind':'optimization-stage-completed',**result})
        return result
    with wheel.db:
        wheel.db.execute("UPDATE optimization_stages SET payload=? WHERE id=?", (_json({"proposal": proposal}), key))
    if stage == "questions":
        result = await measure_questions(wheel, training, proposal["tasks"],
            protected=tuple(row.item for row in development)+tuple(protected), propensities=propensities,
            limit=limit, retry_interrupted=retry_interrupted)
        if train_after_questions:
            from .classifier_training import train_classifier
            result["classifier_training"] = await train_classifier(wheel, training, development,
                protected=protected, propensities=propensities,
                min_development_per_class=min_development_per_class, retry_interrupted=retry_interrupted)
    elif stage=='examples' and wheel.active.config.example_ids:
        from .example_attribution import measure_example_swaps
        by_id={row.item.id:row for row in training}
        hard={}
        for event in wheel.history(10000):
            if event.get('kind')=='prediction' and event.get('target_id') in by_id:
                row=by_id[event['target_id']]
                if event.get('label')!=row.label:
                    hard[row.item.id]=event.get('confidence') or 0.
        preferred=tuple(dict.fromkeys((*sorted(hard,key=lambda key:(-hard[key],key)),*proposal['example_ids'])))
        measured=await measure_example_swaps(wheel,training,development,protected=protected,
            propensities=propensities,preferred_ids=preferred,max_trials=max_example_trials,limit=min(limit,200))
        best=measured['recommended_proposal']
        counts=measured.get('by_class',{label:0 for label in wheel.initial.task.labels})
        if best and min(counts.values())>=min_development_per_class:
            result=await wheel.improve(training,development,protected=protected,propensities=propensities,
                candidate_proposal=best,retry_interrupted=retry_interrupted,require_recall_safeguards=True)
        else:
            result={'promoted':False,'pending_evaluation':True,
                'reason':'swap evidence retained for later labels; no measured winner with adequate class coverage',
                'development_counts':counts,'minimum_development_per_class':min_development_per_class}
        result['example_measurement']=measured
    else:
        counts = {label: sum(row.label == label for row in development) for label in wheel.initial.task.labels}
        if stage=='rubric' and not wheel.active.config.rubric.strip() and wheel.active.head is None:
            previous=wheel.active.config.briefing_state()
            wheel._emit({'kind':'proposal-validated','proposal':proposal,'previous':previous,'candidate':config.briefing_state()})
            candidate,_=await fit_candidate(wheel,config,training,development,protected=protected,
                propensities=propensities,validation_status='provisional')
            wheel._activate(candidate)
            result={'promoted':False,'activated':True,'validation_status':'provisional','proposal':proposal,
                    'reason':'first nonempty rubric initialized provisionally; improvement has not been established',
                    'development_counts':counts,'minimum_development_per_class':min_development_per_class}
            wheel._emit({'kind':'context-initialized',**result,'configuration':config.briefing_state(),'previous':previous})
        elif stage=='rubric' and wheel.active.validation_status=='provisional' and wheel.evaluation_policy.recency_allowance(counts)>0:
            candidate,_=await fit_candidate(wheel,config,training,development,protected=protected,
                propensities=propensities,validation_status='provisional')
            measurements={}
            if min(counts.values())>0:
                now=datetime.now(timezone.utc)
                wheel.reconcile_model_context(training)
                measurements={'incumbent':await wheel._score(wheel.active,development,training,now),
                              'candidate':await wheel._score(candidate,development,training,now)}
            previous=wheel.active.config.briefing_state()
            wheel._emit({'kind':'proposal-validated','proposal':proposal,'previous':previous,'candidate':config.briefing_state()})
            allowance=wheel.evaluation_policy.recency_allowance(counts)
            accepted=(not measurements or measurements['candidate']['balanced_brier'] <=
                      measurements['incumbent']['balanced_brier'] + allowance)
            selection = None
            if measurements and wheel.selection_policy:
                selection = wheel.selection_policy.compare(measurements['incumbent'], measurements['candidate'],
                    primary_allowance=allowance if wheel.selection_policy.primary in ('brier','balanced_brier') else allowance / 2)
                accepted = selection['provisional_eligible']
            if accepted:
                wheel._activate(candidate)
            result={'promoted':False,'activated':accepted,'validation_status':'provisional','proposal':proposal,
                    'pending_evaluation':not accepted, 'recency_allowance':allowance, 'selection':selection,
                    'reason':('working rubric refined with a decaying recency preference; evaluation remains exploratory'
                              if accepted else 'proposal retained; measured regression exceeds current recency allowance'),
                    'development_counts':counts,'context_validation_floor':wheel.context_validation_floor,**measurements}
            wheel._emit({'kind':'context-refined' if accepted else 'candidate-rejected',**result,
                         'configuration':wheel.active.config.briefing_state(),'previous':previous})
        elif min(counts.values()) < min_development_per_class:
            result = {"promoted": False, "proposal": proposal,
                      'validation_status':'insufficient-evidence',
                      'pending_evaluation':True,
                      "reason": "proposal retained; waiting for adequate development class coverage",
                      "development_counts": counts, "minimum_development_per_class": min_development_per_class}
        else:
            result = await wheel.improve(training, development, protected=protected, propensities=propensities,
                candidate_proposal=proposal, retry_interrupted=retry_interrupted, require_recall_safeguards=True)
    result = {**result, "stage": stage, 'proposal':proposal,
              'answer_dependencies':wheel.collected_answer_dependencies(),
              'selection_policy':asdict(wheel.selection_policy) if wheel.selection_policy else None,
              'basis_context_version':key_data['context'], 'proposal_training_evidence':key_data['training'],
              "evaluation_independent_of_optimizer_context": not wheel.optimizer_context["evaluation_context_exposed"]}
    with wheel.db:
        if pending_key and not result.get('pending_evaluation') and result.get('reason')!='round failed':
            old=json.loads(wheel.db.execute('SELECT payload FROM optimization_stages WHERE id=?',(pending_key,)).fetchone()[0])
            old['pending_evaluation']=False
            wheel.db.execute('UPDATE optimization_stages SET payload=? WHERE id=?',(_json(old),pending_key))
        wheel.db.execute("UPDATE optimization_stages SET status='complete',payload=? WHERE id=?", (_json(result), key))
        if result.get('activated') or result.get("promoted") or result.get("classifier_training", {}).get("promoted"):
            # Restart after this stage's own promotion must not rediscover the
            # same feedback merely because this stage changed the active context.
            alias, _ = wheel.cached_artifact('optimization_stages', 'id',
                _hash({**key_data, "context": wheel.active.config.fingerprint}))
            wheel.db.execute("INSERT OR REPLACE INTO optimization_stages VALUES (?, 'complete', ?)",
                (alias, _json(result)))
    wheel._emit({"kind": "optimization-stage-completed", **result})
    return result

"""Separately scheduled controls with persistent discovery and measurement."""
import json
from datetime import datetime,timezone

from .flywheel import _hash, _json, FittedClassifier
from .optimizer_agent import FeedbackBriefing
from .question_measurement import measure_questions


def stage_briefing(wheel, stage, training, development, protected):
    control = {"rubric": "rubric", "examples": "example_ids", "questions": "tasks"}.get(stage)
    if control is None:
        raise ValueError("this stage has no optimizer request")
    return FeedbackBriefing.build(wheel.initial.task, training,
        current={**wheel.active.config.briefing_state(), "control_under_test": control,
                 "stage": stage, "request_budget_bytes": wheel.max_request_bytes},
        protected=tuple(row.item for row in development)+tuple(protected),
        human_explanations=wheel.optimizer_context["human_explanations"])


async def optimize_stage(wheel, stage, training, development, *, protected, propensities,
                         limit=200, retry_interrupted=False, min_development_per_class=20,
                         train_after_questions=True):
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
    if isinstance(min_development_per_class, bool) or not isinstance(min_development_per_class, int) or min_development_per_class < 1:
        raise ValueError("stage evaluation floor must be a positive integer")
    wheel._validate_partitions(training, development, protected, propensities)
    if not training:
        result = {"stage": stage, "promoted": False, "reason": "waiting for eligible human feedback"}
        wheel._emit({"kind": "optimization-stage-completed", **result})
        return result
    wheel.reconcile_feedback(training, development=development)
    key_data = {"stage": stage, "context": wheel.active.config.fingerprint,
                 "training": wheel._evidence(training), "development": wheel._evidence(development),
                 "protected": sorted(item.id for item in protected), "propensities": propensities,
                 "limit": limit, "evaluation_floor": min_development_per_class,
                 "evaluation_weighting": wheel.evaluation_weighting, "training_class_weighting": wheel.training_class_weighting}
    key_data['context_validation_floor']=wheel.context_validation_floor
    from dataclasses import asdict
    key_data['evaluation_policy']=asdict(wheel.evaluation_policy)
    key_data["optimizer_context"] = wheel.optimizer_context
    key_data["train_after_questions"] = train_after_questions
    key = _hash(key_data)
    wheel.db.execute("CREATE TABLE IF NOT EXISTS optimization_stages (id TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT)")
    saved = wheel.db.execute("SELECT status,payload FROM optimization_stages WHERE id=?", (key,)).fetchone()
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
    else:
        counts = {label: sum(row.label == label for row in development) for label in wheel.initial.task.labels}
        if stage=='rubric' and not wheel.active.config.rubric.strip() and wheel.active.head is None:
            previous=wheel.active.config.briefing_state()
            wheel._emit({'kind':'proposal-validated','proposal':proposal,'previous':previous,'candidate':config.briefing_state()})
            wheel._activate(FittedClassifier(config,training_evidence=wheel._evidence(training),validation_status='provisional'))
            result={'promoted':False,'activated':True,'validation_status':'provisional','proposal':proposal,
                    'reason':'first nonempty rubric initialized provisionally; improvement has not been established',
                    'development_counts':counts,'minimum_development_per_class':min_development_per_class}
            wheel._emit({'kind':'context-initialized',**result,'configuration':config.briefing_state(),'previous':previous})
        elif stage=='rubric' and wheel.active.validation_status=='provisional' and wheel.evaluation_policy.recency_allowance(counts)>0:
            candidate=FittedClassifier(config,training_evidence=wheel._evidence(training),validation_status='provisional')
            measurements={}
            if min(counts.values())>0:
                now=datetime.now(timezone.utc)
                measurements={'incumbent':await wheel._score(wheel.active,development,training,now),
                              'candidate':await wheel._score(candidate,development,training,now)}
            previous=wheel.active.config.briefing_state()
            wheel._emit({'kind':'proposal-validated','proposal':proposal,'previous':previous,'candidate':config.briefing_state()})
            allowance=wheel.evaluation_policy.recency_allowance(counts)
            accepted=(not measurements or measurements['candidate']['balanced_brier'] <=
                      measurements['incumbent']['balanced_brier'] + allowance)
            if accepted:
                wheel._activate(candidate)
            result={'promoted':False,'activated':accepted,'validation_status':'provisional','proposal':proposal,
                    'pending_evaluation':not accepted, 'recency_allowance':allowance,
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
            wheel.db.execute("INSERT OR REPLACE INTO optimization_stages VALUES (?, 'complete', ?)",
                (_hash({**key_data, "context": wheel.active.config.fingerprint}), _json(result)))
    wheel._emit({"kind": "optimization-stage-completed", **result})
    return result

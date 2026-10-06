"""Separately scheduled controls with persistent discovery and measurement."""
import json

from .flywheel import _hash, _json
from .optimizer_agent import FeedbackBriefing
from .question_measurement import measure_questions


async def optimize_stage(wheel, stage, training, development, *, protected, propensities,
                         limit=200, retry_interrupted=False, min_development_per_class=20):
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
    key = _hash(key_data)
    wheel.db.execute("CREATE TABLE IF NOT EXISTS optimization_stages (id TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT)")
    saved = wheel.db.execute("SELECT status,payload FROM optimization_stages WHERE id=?", (key,)).fetchone()
    if saved and saved[0] == "complete":
        result = json.loads(saved[1])
        if stage == "questions":
            from .classifier_training import train_classifier
            result["classifier_training"] = await train_classifier(wheel, training, development,
                protected=protected, propensities=propensities,
                min_development_per_class=min_development_per_class, retry_interrupted=retry_interrupted)
        return result
    if saved and not retry_interrupted:
        return {"stage": stage, "promoted": False, "reason": "interrupted stage requires explicit retry"}
    proposal = json.loads(saved[1]).get("proposal") if saved and saved[1] else None
    control = controls[stage]
    with wheel.db:
        wheel.db.execute("INSERT OR REPLACE INTO optimization_stages VALUES (?, 'pending', ?)",
                         (key, _json({"proposal": proposal})))
    wheel._emit({"kind": "optimization-stage-started", "stage": stage, "context_version": wheel.active.config.fingerprint})
    if proposal is None:
        briefing = FeedbackBriefing.build(wheel.initial.task, training,
            current={**wheel.active.config.briefing_state(), "control_under_test": control,
                     "stage": stage, "request_budget_bytes": wheel.max_request_bytes},
            protected=tuple(row.item for row in development)+tuple(protected))
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
    wheel.active.config.apply(proposal, training)
    with wheel.db:
        wheel.db.execute("UPDATE optimization_stages SET payload=? WHERE id=?", (_json({"proposal": proposal}), key))
    if stage == "questions":
        result = await measure_questions(wheel, training, proposal["tasks"],
            protected=tuple(row.item for row in development)+tuple(protected), propensities=propensities,
            limit=limit, retry_interrupted=retry_interrupted)
        from .classifier_training import train_classifier
        result["classifier_training"] = await train_classifier(wheel, training, development,
            protected=protected, propensities=propensities,
            min_development_per_class=min_development_per_class, retry_interrupted=retry_interrupted)
    else:
        counts = {label: sum(row.label == label for row in development) for label in wheel.initial.task.labels}
        if min(counts.values()) < min_development_per_class:
            result = {"promoted": False, "proposal": proposal,
                      "reason": "proposal retained; waiting for adequate development class coverage",
                      "development_counts": counts, "minimum_development_per_class": min_development_per_class}
        else:
            result = await wheel.improve(training, development, protected=protected, propensities=propensities,
                candidate_proposal=proposal, retry_interrupted=retry_interrupted, require_recall_safeguards=True)
    result = {**result, "stage": stage}
    with wheel.db:
        wheel.db.execute("UPDATE optimization_stages SET status='complete',payload=? WHERE id=?", (_json(result), key))
        if result.get("promoted") or result.get("classifier_training", {}).get("promoted"):
            # Restart after this stage's own promotion must not rediscover the
            # same feedback merely because this stage changed the active context.
            wheel.db.execute("INSERT OR REPLACE INTO optimization_stages VALUES (?, 'complete', ?)",
                (_hash({**key_data, "context": wheel.active.config.fingerprint}), _json(result)))
    wheel._emit({"kind": "optimization-stage-completed", **result})
    return result

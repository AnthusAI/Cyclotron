"""Fit and evaluate retained question features separately from context discovery."""
import json
from datetime import datetime, timezone

from .flywheel import _hash, _json


async def train_classifier(wheel, training, development, *, protected, propensities,
                           min_development_per_class=20, retry_interrupted=False,
                           apply_promotion=True):
    if type(min_development_per_class) is not int or min_development_per_class < 1:
        raise ValueError("development class floor must be positive")
    wheel._validate_partitions(training, development, protected, propensities)
    wheel.reconcile_feedback(training, development=development)
    counts = {label: sum(r.label == label for r in development) for label in wheel.initial.task.labels}
    if min(counts.values()) < min_development_per_class:
        result = {"stage": "classifier", "promoted": False, "development_counts": counts,
                  "evaluation_independent_of_optimizer_context": not wheel.optimizer_context["evaluation_context_exposed"],
                  "reason": "waiting for development class coverage", "minimum_development_per_class": min_development_per_class}
        wheel._emit({"kind": "classifier-training-completed", **result})
        return result
    # Keep the incumbent's questions. Add one retained revision per new concept;
    # revisions already deployed are not silently replaced by a bank entry.
    tasks = [{"name": t.name, "instructions": t.instructions, "labels": list(t.labels)} for t in wheel.active.config.tasks]
    names = {t["name"] for t in tasks}
    additions = []
    for entry in wheel.feature_bank():
        if entry["question"]["name"] not in names:
            additions.append(entry["question"])
            names.add(entry["question"]["name"])
    configs = [("current", tasks)]
    if additions:
        configs.append(("retained_questions", tasks + additions))
    evidence = {"version": wheel.active.fingerprint, "training": wheel._evidence(training),
                "optimizer_context": wheel.optimizer_context,
                "development": wheel._evidence(development), "protected": sorted(i.id for i in protected),
                "propensities": propensities, "bank": [e["id"] for e in wheel.feature_bank()], "floor": min_development_per_class,
                "apply_promotion": apply_promotion, "policy": "balanced-brier-no-recall-regression-v1"}
    key = _hash(evidence)
    wheel.db.execute("CREATE TABLE IF NOT EXISTS classifier_training (id TEXT PRIMARY KEY, status TEXT, payload TEXT)")
    saved = wheel.db.execute("SELECT status,payload FROM classifier_training WHERE id=?", (key,)).fetchone()
    if saved and saved[0] == "complete":
        return json.loads(saved[1])
    if saved and not retry_interrupted:
        return {"stage": "classifier", "promoted": False, "reason": "interrupted training requires explicit retry"}
    now = datetime.fromisoformat(json.loads(saved[1])["time"]) if saved else datetime.now(timezone.utc)
    with wheel.db:
        wheel.db.execute("INSERT OR REPLACE INTO classifier_training VALUES (?, 'pending', ?)", (key, _json({"time": now.isoformat()})))
    wheel._emit({"kind": "classifier-training-started", "configurations": len(configs), "weightings": ["natural", "equal_class"]})
    previous_weighting = wheel.training_class_weighting
    previous_evaluation = wheel.evaluation_weighting
    trials = []
    try:
        wheel.evaluation_weighting = "equal_class"
        for name, definitions in configs:
            for weighting in ("natural", "equal_class"):
                wheel.training_class_weighting = weighting
                trial = await wheel.improve(training, development, protected=protected, propensities=propensities,
                    candidate_proposal={"rationale": "Separate numerical classifier training", "tasks": definitions},
                    retry_interrupted=retry_interrupted, apply_promotion=False, evaluation_time=now)
                baseline, candidate = trial.get("incumbent"), trial.get("candidate")
                safe = bool(baseline and candidate and candidate["balanced_accuracy"] >= baseline["balanced_accuracy"]
                    and all(candidate["per_class"][label]["recall"] >= baseline["per_class"][label]["recall"]
                            for label in wheel.initial.task.labels))
                trials.append({**trial, "feature_set": name, "recall_safeguard_passed": safe})
        qualified = [t for t in trials if t.get("improved") and t["recall_safeguard_passed"]]
        best = min(qualified, key=lambda t: t["candidate"]["balanced_brier"]) if qualified else None
        if best and apply_promotion:
            wheel.promote_trial(best["trial_fingerprint"], training, development)
        result = {"stage": "classifier", "promoted": bool(best and apply_promotion), "trials": trials,
                  "evaluation_independent_of_optimizer_context": not wheel.optimizer_context["evaluation_context_exposed"],
                  "selected": best, "development_counts": counts,
                  "reason": "lower balanced Brier with no per-class recall regression" if best else "no candidate passed promotion safeguards"}
        with wheel.db:
            wheel.db.execute("UPDATE classifier_training SET status='complete',payload=? WHERE id=?", (_json(result), key))
            if result["promoted"]:
                wheel.db.execute("INSERT OR REPLACE INTO classifier_training VALUES (?, 'complete', ?)",
                    (_hash({**evidence, "version": wheel.active.fingerprint}), _json(result)))
        wheel._emit({"kind": "classifier-training-completed", **result})
        return result
    finally:
        wheel.training_class_weighting = previous_weighting
        wheel.evaluation_weighting = previous_evaluation

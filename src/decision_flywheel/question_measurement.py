"""Bounded retrospective feature measurement, never automatic deployment."""
from collections import Counter
from datetime import datetime, timezone
import json
import math

from .classification_metrics import classification_metrics
from .feature_bank import FeatureBank, probability_diagnostics, question_id
from .flywheel import _hash, _json


def rank_question(classes, options, rows, *, propensities=None):
    """Stratified OOF soft contingency mapping; supports inverse/multiclass factors."""
    if len({row[0] for row in rows}) != len(rows):
        raise ValueError("rank rows require unique IDs")
    props = propensities if propensities is not None else {row[0]: 1. for row in rows}
    if any(isinstance(props.get(row[0]), bool) or not isinstance(props.get(row[0]), (int, float))
           or not math.isfinite(props[row[0]]) or not 0 < props[row[0]] <= 1 for row in rows):
        raise ValueError("rank mapping requires valid recorded review propensities")
    diagnostics = probability_diagnostics(classes, options, [(label, p) for _, label, p in rows])
    counts = {label: sum(y == label for _, y, _ in rows) for label in classes}
    answered = sorted((row for row in rows if row[2] is not None), key=lambda r: r[0])
    usable = {label: sum(y == label for _, y, _ in answered) for label in classes}
    result = {"by_class": counts, "diagnostics": diagnostics, "direct_agreement": None,
              "cross_validated": None, "scope": "retrospective discovery; OOF mapping conditional on fixed context, not independent flywheel validation"}
    if not answered:
        return result
    actual = [y for _, y, _ in answered]
    majority = max(classes, key=lambda label: actual.count(label))
    result["majority_baseline"] = classification_metrics(classes, actual, [majority]*len(actual),
        [{label: float(label == majority) for label in classes} for _ in actual])
    if set(classes) == set(options):
        result["direct_agreement"] = classification_metrics(classes, actual,
            [max(options, key=p.get) for _, _, p in answered],
            [{label: p[label]/sum(p.values()) for label in classes} for _, _, p in answered])
    if min(usable.values()) < 3:
        result["reason"] = "need at least three answered items per final class for stratified mapping"
        return result
    folds = min(5, min(usable.values()))
    assignments = {}
    for label in classes:
        for index, (key, _, _) in enumerate(row for row in answered if row[1] == label):
            assignments[key] = index % folds
    predictions, distributions, evidence = [], [], []
    for key, _, probabilities in answered:
        fit = [row for row in answered if assignments[row[0]] != assignments[key]]
        fit_totals = {label: sum(1/props[row[0]] for row in fit if row[1] == label) for label in classes}
        # Each class contributes equal total weight. A fixed Laplace prior of
        # one per (option, class) is numerical smoothing, not an LLM parameter.
        table = {option: {label: 1. for label in classes} for option in options}
        for fit_id, label, p in fit:
            weight = len(fit)/(len(classes)*fit_totals[label]*props[fit_id])
            for option in options:
                table[option][label] += weight*p[option]
        scores = {label: sum(probabilities[option]*table[option][label]/sum(table[option].values())
                             for option in options) for label in classes}
        total = sum(scores.values())
        distribution = {label: value/total for label, value in scores.items()}
        predictions.append(max(classes, key=distribution.get))
        distributions.append(distribution)
        evidence.append({"target_id": key, "fold": assignments[key], "fit_ids": [row[0] for row in fit]})
    result["cross_validated"] = classification_metrics(classes, actual, predictions, distributions)
    result["mapping"] = "stratified OOF soft contingency, inverse-propensity and equal-class fit weighting, fixed Laplace=1"
    result["fold_evidence"] = evidence
    return result


def select_window(training, classes, limit):
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("retrospective limit must be a positive integer")
    selected = list(training[-limit:])
    # Caller supplies chronological order. Walk backward for scarce classes,
    # replacing the oldest surplus-class record without expanding the ceiling.
    for label in classes:
        for older in reversed(training[:-limit]):
            if sum(row.label == label for row in selected) >= 3:
                break
            if older.label != label:
                continue
            counts = Counter(row.label for row in selected)
            replace = next((i for i, row in enumerate(selected) if counts[row.label] > 3), None)
            if replace is None:
                break
            selected[replace] = older
    ids = {row.item.id for row in selected}
    return tuple(row for row in training if row.item.id in ids)


async def measure_questions(wheel, training, questions, *, protected, propensities, limit=200,
                            retry_interrupted=False):
    wheel._validate_partitions(training, (), protected, propensities)
    window = select_window(training, wheel.initial.task.labels, limit)
    if not window:
        return {"count": 0, "limit": limit, "rankings": [], "reason": "no eligible human labels"}
    wheel.reconcile_feedback(training)
    baseline = wheel.active.config
    definitions = {task.name: {"name": task.name, "instructions": task.instructions, "labels": list(task.labels)}
                   for task in baseline.tasks}
    for question in questions:
        definitions[question["name"]] = question
    config = baseline.apply({"tasks": list(definitions.values())}, training)
    key = _hash({"context": config.briefing_state(), "window": wheel._evidence(window),
                 "pool": wheel._evidence(training), "protected": sorted(item.id for item in protected),
                 "limit": limit, "ranking": "soft-contingency-oof-v1", "propensities": propensities})
    wheel.db.execute("CREATE TABLE IF NOT EXISTS question_measurements (id TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT)")
    saved = wheel.db.execute("SELECT status,payload FROM question_measurements WHERE id=?", (key,)).fetchone()
    if saved and saved[0] == "complete":
        return json.loads(saved[1])
    if saved and not retry_interrupted:
        return {"count": len(window), "rankings": [], "reason": "backfill interrupted; explicit retry authorization required"}
    progress = json.loads(saved[1]) if saved and saved[1] else {}
    now = datetime.fromisoformat(progress["request_time"]) if progress else datetime.now(timezone.utc)
    with wheel.db:
        wheel.db.execute("INSERT OR REPLACE INTO question_measurements VALUES (?, 'pending', ?)",
                         (key, _json({"request_time": now.isoformat()})))
    bank = FeatureBank(wheel.db)
    for question in questions:
        bank.register(question, rationale="separate question-discovery stage", evidence=wheel._evidence(window))
    wheel._emit({"kind": "question-backfill-started", "stage": "questions", "count": len(window), "limit": limit,
                 "context_version": config.fingerprint, "baseline_version": baseline.fingerprint})
    answers = []
    for index, row in enumerate(window):
        answers.append(await wheel._answers(config, row.item, training, now))
        wheel._emit({"kind": "question-backfill-progress", "completed": index+1, "total": len(window)})
    rankings = []
    for task in config.tasks:
        question = definitions[task.name]
        feature_id = bank.register(question, rationale="matched-window existing or proposed measurement",
                                   evidence=wheel._evidence(window))
        rows = [(row.item.id, row.label, batch.answers[task.name].probabilities) for row, batch in zip(window, answers)]
        rank = {"feature_id": feature_id, "question": question,
                **rank_question(config.task.labels, task.labels, rows, propensities=propensities)}
        rankings.append(rank)
        bank.record(feature_id, {"measurement_fingerprint": key, "context_version": config.fingerprint,
                                "cross_validated": rank["cross_validated"], "count": len(window),
                                "by_class": rank["by_class"]}, rank["diagnostics"])
    rankings.sort(key=lambda r: (r["cross_validated"] is None,
                                -(r["cross_validated"]["balanced_accuracy"] if r["cross_validated"] else 0), r["feature_id"]))
    report = {"stage": "questions", "measurement_fingerprint": key, "count": len(window), "limit": limit,
              "main_answer_agreement": classification_metrics(config.task.labels, [r.label for r in window],
                  [batch.answers["decision"].label for batch in answers],
                  [{label: p/sum(batch.answers["decision"].probabilities.values())
                    for label, p in batch.answers["decision"].probabilities.items()} for batch in answers]),
              "by_class": {label: sum(row.label == label for row in window) for label in config.task.labels},
              "window_ids": [row.item.id for row in window], "context_version": config.fingerprint,
              "baseline_version": baseline.fingerprint, "rankings": rankings, "promoted": False,
              "selection_policy": "chronological recent window with backward three-per-class reserve"}
    with wheel.db:
        wheel.db.execute("UPDATE question_measurements SET status='complete',payload=? WHERE id=?", (_json(report), key))
    wheel._emit({"kind": "question-ranking-completed", **report})
    return report

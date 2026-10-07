"""Natural-distribution and equal-class scores over the same fixed records."""
import math


def wilson_interval(correct,count):
    if not count:return None
    rate=correct/count;z=1.959963984540054
    denominator=1+z*z/count
    center=(rate+z*z/(2*count))/denominator
    half=z*math.sqrt(rate*(1-rate)/count+z*z/(4*count*count))/denominator
    return [max(0.,center-half),min(1.,center+half)]


def classification_metrics(classes, truth, predictions, probabilities, *, allow_missing_probabilities=False):
    classes, truth, predictions, probabilities = map(tuple, (classes, truth, predictions, probabilities))
    if not classes or len(set(classes)) != len(classes):
        raise ValueError("evaluation classes must be unique and nonempty")
    if len(truth) != len(predictions) or len(truth) != len(probabilities):
        raise ValueError("evaluation records must have equal lengths")
    groups = {label: {"count": 0, "correct": 0, "loss": 0., "probability_count":0} for label in classes}
    confusion = {actual:{predicted:0 for predicted in classes} for actual in classes}
    for actual, predicted, distribution in zip(truth, predictions, probabilities):
        if actual not in groups or predicted not in groups:
            raise ValueError("evaluation must cover the declared labels")
        group = groups[actual]
        confusion[actual][predicted] += 1
        group["count"] += 1
        group["correct"] += predicted == actual
        if distribution is None and allow_missing_probabilities:
            continue
        if distribution is None or set(distribution) != set(classes):
            raise ValueError("evaluation must cover the declared labels")
        values = list(distribution.values())
        if any(isinstance(p, bool) or not isinstance(p, (int, float)) or not math.isfinite(p)
               or not 0 <= p <= 1 for p in values) or not math.isclose(sum(values), 1., abs_tol=1e-6):
            raise ValueError("evaluation probabilities must be a normalized finite distribution")
        group['probability_count']+=1
        group["loss"] += sum((distribution[label] - (label == actual)) ** 2 for label in classes)
    per_class = {}
    for label, group in groups.items():
        n = group["count"]
        recall = group["correct"] / n if n else None
        interval = wilson_interval(group['correct'],n)
        predicted_count = sum(confusion[actual][label] for actual in classes)
        per_class[label] = {"count": n, "correct": group["correct"], "recall": recall,
                            "predicted_count":predicted_count,
                            "precision":group['correct']/predicted_count if predicted_count else None,
                            "f1":2*group['correct']/(n+predicted_count) if n+predicted_count else None,
                            "recall_interval_95": interval, "brier": group["loss"]/group['probability_count'] if group['probability_count'] else None}
    missing = [label for label in classes if not groups[label]["count"]]
    count = len(truth)
    probability_count=sum(g['probability_count'] for g in groups.values())
    correct=sum(g['correct'] for g in groups.values())
    return {"count": count, "accuracy": sum(g["correct"] for g in groups.values())/count if count else None,
            "accuracy_interval_95":wilson_interval(correct,count),
            "accuracy_interval_method":"Wilson 95%; descriptive, not selection-adjusted or a paired effect interval",
            "macro_f1":None if missing else sum(g['f1'] for g in per_class.values())/len(classes),
            "brier": sum(g["loss"] for g in groups.values())/probability_count if probability_count else None,
            "probability_count":probability_count,
            "balanced_accuracy": None if missing else sum(g["recall"] for g in per_class.values())/len(classes),
            "balanced_brier": None if any(g['brier'] is None for g in per_class.values()) else sum(g["brier"] for g in per_class.values())/len(classes),
            "per_class": per_class, "missing_classes": missing, "confusion_matrix":confusion,
            "interval_method": "Wilson 95% per-class recall; descriptive, not selection-adjusted"}

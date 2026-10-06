"""Natural-distribution and equal-class scores over the same fixed records."""
import math


def classification_metrics(classes, truth, predictions, probabilities):
    classes, truth, predictions, probabilities = map(tuple, (classes, truth, predictions, probabilities))
    if not classes or len(set(classes)) != len(classes):
        raise ValueError("evaluation classes must be unique and nonempty")
    if len(truth) != len(predictions) or len(truth) != len(probabilities):
        raise ValueError("evaluation records must have equal lengths")
    groups = {label: {"count": 0, "correct": 0, "loss": 0.} for label in classes}
    confusion = {actual:{predicted:0 for predicted in classes} for actual in classes}
    for actual, predicted, distribution in zip(truth, predictions, probabilities):
        if actual not in groups or predicted not in groups or set(distribution) != set(classes):
            raise ValueError("evaluation must cover the declared labels")
        values = list(distribution.values())
        if any(isinstance(p, bool) or not isinstance(p, (int, float)) or not math.isfinite(p)
               or not 0 <= p <= 1 for p in values) or not math.isclose(sum(values), 1., abs_tol=1e-6):
            raise ValueError("evaluation probabilities must be a normalized finite distribution")
        group = groups[actual]
        confusion[actual][predicted] += 1
        group["count"] += 1
        group["correct"] += predicted == actual
        group["loss"] += sum((distribution[label] - (label == actual)) ** 2 for label in classes)
    per_class = {}
    for label, group in groups.items():
        n = group["count"]
        recall = group["correct"] / n if n else None
        interval = None
        if n:
            z = 1.959963984540054
            denominator = 1 + z*z/n
            center = (recall + z*z/(2*n)) / denominator
            half = z * math.sqrt(recall*(1-recall)/n + z*z/(4*n*n)) / denominator
            interval = [max(0., center-half), min(1., center+half)]
        predicted_count = sum(confusion[actual][label] for actual in classes)
        per_class[label] = {"count": n, "correct": group["correct"], "recall": recall,
                            "precision":group['correct']/predicted_count if predicted_count else None,
                            "recall_interval_95": interval, "brier": group["loss"]/n if n else None}
    missing = [label for label in classes if not groups[label]["count"]]
    count = len(truth)
    return {"count": count, "accuracy": sum(g["correct"] for g in groups.values())/count if count else None,
            "brier": sum(g["loss"] for g in groups.values())/count if count else None,
            "balanced_accuracy": None if missing else sum(g["recall"] for g in per_class.values())/len(classes),
            "balanced_brier": None if missing else sum(g["brier"] for g in per_class.values())/len(classes),
            "per_class": per_class, "missing_classes": missing, "confusion_matrix":confusion,
            "interval_method": "Wilson 95% per-class recall; descriptive, not selection-adjusted"}

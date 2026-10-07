"""Fixed-bin, top-label reliability of already-issued predictions, not a fit."""
import math
from .classification_metrics import classification_metrics


def reliability_curve(classes, truth, predictions, probabilities):
    classes, truth, predictions, probabilities = map(tuple, (classes, truth, predictions, probabilities))
    metrics=classification_metrics(classes, truth, predictions, probabilities,allow_missing_probabilities=True)
    bins = [{'lower': i / 10, 'upper': (i + 1) / 10, 'count': 0,
             'mean_confidence': None, 'accuracy': None} for i in range(10)]
    sums = [[0., 0] for _ in bins]
    for actual, predicted, distribution in zip(truth, predictions, probabilities):
        if distribution is None:continue
        confidence = distribution[predicted]
        index = min(9, int(confidence * 10))
        bins[index]['count'] += 1
        sums[index][0] += confidence
        sums[index][1] += actual == predicted
    error = 0.
    for bucket, (confidence, correct) in zip(bins, sums):
        n = bucket['count']
        if n:
            bucket['mean_confidence'] = confidence / n
            bucket['accuracy'] = correct / n
            error += abs(confidence - correct)
    count=metrics['probability_count']
    return {'method': 'top-label reliability; ten fixed equal-width bins',
            'count': count, 'missing_probability_count':len(truth)-count,
            'ece': error / count if count else None, 'bins': bins,
            'brier':metrics['brier'],
            'log_loss':sum(-math.log(max(1e-15,row[actual])) for actual,row in zip(truth,probabilities) if row is not None)/count if count else None}

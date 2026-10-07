"""Descriptive live agreement over the latest human-reviewed items."""
from collections import OrderedDict
from .classification_metrics import classification_metrics
from .calibration_metrics import reliability_curve


def recent_reviewed_metrics(classes, records, *, limit=200):
    """Records are chronological (item ID, human label, prediction, probabilities).

    Corrections replace and refresh a sample, not create a duplicate. This
    natural recent window is separate from balanced candidate evaluation.
    """
    if type(limit) is not int or limit<1:
        raise ValueError('metric window must be a positive integer')
    available=OrderedDict()
    for identifier,actual,predicted,probabilities in records:
        available.pop(identifier,None)
        available[identifier]=(actual,predicted,probabilities)
    selected=list(available.values())[-limit:]
    truth=[row[0] for row in selected]
    predictions=[row[1] for row in selected]
    distributions=[row[2] for row in selected]
    return {**classification_metrics(classes,truth,predictions,distributions,allow_missing_probabilities=True),
            'calibration':reliability_curve(classes,truth,predictions,distributions),
            'window_size':limit,'available_count':len(available)}

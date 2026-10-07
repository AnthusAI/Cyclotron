"""One explicit development-selection rule shared by optimization stages."""
from dataclasses import asdict, dataclass
import math

METRICS = ('accuracy', 'precision', 'recall', 'f1', 'balanced_accuracy', 'brier', 'balanced_brier')


@dataclass(frozen=True)
class SelectionPolicy:
    primary: str = 'balanced_brier'
    secondary: str | None = None
    positive_class: str | None = None
    aggregation: str = 'positive'
    max_secondary_regression: float = 0.
    minimum_secondary: float | None = None

    def __post_init__(self):
        if self.primary not in METRICS or (self.secondary is not None and self.secondary not in METRICS):
            raise ValueError('unknown selection objective')
        if self.secondary == self.primary:
            raise ValueError('primary and secondary must differ')
        if self.aggregation not in ('positive', 'macro'):
            raise ValueError('aggregation must be positive or macro')
        if any(m in ('precision', 'recall', 'f1') for m in (self.primary, self.secondary)):
            if self.aggregation == 'positive' and not self.positive_class:
                raise ValueError('positive-class objectives require an explicit positive class')
        for value in (self.max_secondary_regression, self.minimum_secondary):
            if value is not None and (isinstance(value, bool) or not isinstance(value, (float, int))
                                      or not math.isfinite(value) or value < 0):
                raise ValueError('selection bounds must be finite and nonnegative')
        if self.minimum_secondary is not None and self.secondary is None:
            raise ValueError('secondary floor requires a secondary objective')
        if self.minimum_secondary is not None and (self.secondary in ('brier', 'balanced_brier') or self.minimum_secondary > 1):
            raise ValueError('secondary floor requires a rate objective and must be at most one')

    def score(self, metrics, metric):
        if metric not in ('precision', 'recall', 'f1'):
            value = metrics[metric]
        else:
            groups = metrics['per_class']
            selected = list(groups.values()) if self.aggregation == 'macro' else [groups[self.positive_class]]
            values = []
            for group in selected:
                if group['recall'] is None:
                    raise ValueError('selection requires class coverage')
                precision, recall = group['precision'] or 0., group['recall']
                values.append(precision if metric == 'precision' else recall if metric == 'recall'
                              else 2 * precision * recall / (precision + recall) if precision + recall else 0.)
            value = sum(values) / len(values)
        if value is None or not math.isfinite(value):
            raise ValueError('selection requires finite scores')
        return value

    def rank(self, metrics):
        return tuple(self.score(metrics, m) * (-1 if m in ('brier', 'balanced_brier') else 1)
                     for m in (self.primary, self.secondary) if m is not None)

    def compare(self, incumbent, candidate, *, primary_allowance=0.):
        before, after = self.rank(incumbent), self.rank(candidate)
        eligible, reason = True, 'primary objective did not improve'
        if self.secondary is not None:
            if after[1] < before[1] - self.max_secondary_regression - 1e-12:
                eligible, reason = False, 'secondary objective regression exceeds allowance'
            if self.minimum_secondary is not None and self.score(candidate, self.secondary) < self.minimum_secondary:
                eligible, reason = False, 'secondary objective floor not met'
        gain = after[0] - before[0]
        improved = eligible and (gain > 1e-12 or (abs(gain) <= 1e-12 and len(after) > 1 and after[1] > before[1] + 1e-12))
        if improved:
            reason = 'primary objective improved' if gain > 1e-12 else 'secondary objective broke primary tie'
        return {'policy': asdict(self), 'improved': improved, 'eligible': eligible,
                'provisional_eligible': eligible and gain >= -primary_allowance - 1e-12,
                'incumbent_scores': {m: self.score(incumbent, m) for m in (self.primary, self.secondary) if m},
                'candidate_scores': {m: self.score(candidate, m) for m in (self.primary, self.secondary) if m},
                'reason': reason}


def add_selection_arguments(parser):
    parser.add_argument('--selection-primary', choices=METRICS)
    parser.add_argument('--selection-secondary', choices=METRICS)
    parser.add_argument('--selection-positive-class')
    parser.add_argument('--selection-aggregation', choices=('positive', 'macro'), default='positive')
    parser.add_argument('--selection-max-secondary-regression', type=float, default=0.)
    parser.add_argument('--selection-minimum-secondary', type=float)


def selection_from_arguments(args):
    if not args.selection_primary:
        if (args.selection_secondary or args.selection_minimum_secondary is not None
                or args.selection_positive_class or args.selection_aggregation != 'positive'
                or args.selection_max_secondary_regression != 0):
            raise ValueError('selection options require --selection-primary')
        return None
    return SelectionPolicy(args.selection_primary, args.selection_secondary, args.selection_positive_class,
                           args.selection_aggregation, args.selection_max_secondary_regression,
                           args.selection_minimum_secondary)

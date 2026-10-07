"""Bounded development comparisons and an explicit cold-start recency prior."""
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class EvaluationPolicy:
    max_samples: int = 200
    initial_recency_allowance: float = 2.
    recency_decay_per_class: int = 20

    def __post_init__(self):
        for value in (self.max_samples, self.recency_decay_per_class):
            if type(value) is not int or value < 1:
                raise ValueError('sample limits and decay coverage must be positive integers')
        if (isinstance(self.initial_recency_allowance, bool) or
                not math.isfinite(self.initial_recency_allowance) or
                not 0 <= self.initial_recency_allowance <= 2):
            raise ValueError('recency allowance must be between zero and two Brier units')

    def recency_allowance(self, counts):
        support = min(counts.values(), default=0)
        return self.initial_recency_allowance * max(0., 1. - support / self.recency_decay_per_class)

    def select(self, rows, classes):
        """Recent-first, class-stratified budget; unused scarce-class slots are filled."""
        if len(rows) <= self.max_samples:
            return tuple(rows)
        classes = tuple(classes)
        if self.max_samples < len(classes):
            raise ValueError('evaluation limit must allow at least one sample per class')
        quota, remainder = divmod(self.max_samples, len(classes))
        selected = set()
        for index, label in enumerate(classes):
            candidates = [row for row in rows if row.label == label]
            selected.update(row.item.id for row in candidates[-(quota + (index < remainder)):])
        for row in reversed(rows):
            if len(selected) >= self.max_samples:
                break
            selected.add(row.item.id)
        return tuple(row for row in rows if row.item.id in selected)

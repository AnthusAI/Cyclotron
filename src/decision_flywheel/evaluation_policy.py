"""Development comparisons over every item by default, and an explicit cold-start recency prior.

Sampling is an operator choice: ``max_samples`` set to a positive integer selects a recent-first,
class-stratified subset. The selection is deterministic (no random draw, so no seed), and the
policy's ``manifest`` and ``fingerprint`` are recorded with every score so the subset can be reproduced.
"""

import hashlib
import json
import math
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class EvaluationPolicy:
    max_samples: int | None = None  # None: every development item
    initial_recency_allowance: float = 2.0
    recency_decay_per_class: int = 20

    def __post_init__(self):
        if self.max_samples is not None and (type(self.max_samples) is not int or self.max_samples < 1):
            raise ValueError("an explicit sample size must be a positive integer, or None for every item")
        if type(self.recency_decay_per_class) is not int or self.recency_decay_per_class < 1:
            raise ValueError("decay coverage must be a positive integer")
        if (
            isinstance(self.initial_recency_allowance, bool)
            or not math.isfinite(self.initial_recency_allowance)
            or not 0 <= self.initial_recency_allowance <= 2
        ):
            raise ValueError("recency allowance must be between zero and two Brier units")

    def manifest(self):
        selection = "all" if self.max_samples is None else "recent-first-class-stratified"
        return {**asdict(self), "selection": selection}

    @property
    def fingerprint(self):
        return hashlib.sha256(json.dumps(self.manifest(), sort_keys=True).encode()).hexdigest()

    def recency_allowance(self, counts):
        support = min(counts.values(), default=0)
        return self.initial_recency_allowance * max(0.0, 1.0 - support / self.recency_decay_per_class)

    def select(self, rows, classes):
        """Every row, or with an explicit ``max_samples`` a recent-first, class-stratified subset."""
        if self.max_samples is None or len(rows) <= self.max_samples:
            return tuple(rows)
        classes = tuple(classes)
        if self.max_samples < len(classes):
            raise ValueError("evaluation limit must allow at least one sample per class")
        quota, remainder = divmod(self.max_samples, len(classes))
        selected = set()
        for index, label in enumerate(classes):
            candidates = [row for row in rows if row.label == label]
            selected.update(row.item.id for row in candidates[-(quota + (index < remainder)) :])
        for row in reversed(rows):
            if len(selected) >= self.max_samples:
                break
            selected.add(row.item.id)
        return tuple(row for row in rows if row.item.id in selected)

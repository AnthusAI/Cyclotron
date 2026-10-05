"""Small, reproducible context selection for the local human-review baseline."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .reviewer_predictor import LabeledArticle, predict_article


@dataclass(frozen=True)
class ReviewerContextConfig:
    name: str
    include_metadata: bool


@dataclass(frozen=True)
class ReviewerOptimization:
    attempted: bool
    selected: ReviewerContextConfig
    selected_accuracy: float | None
    candidates: dict[str, float]
    reason: str


_CONTENT_ONLY = ReviewerContextConfig("content_only", False)
_WITH_METADATA = ReviewerContextConfig("content_and_metadata", True)


def optimize_reviewer_context(labels: Iterable[LabeledArticle], *, minimum_labels: int = 20) -> ReviewerOptimization:
    """Choose a request representation from train labels using leave-one-out accuracy."""
    rows = tuple(labels)
    if minimum_labels < 2:
        raise ValueError("minimum_labels must be at least two")
    if len(rows) < minimum_labels:
        return ReviewerOptimization(False, _WITH_METADATA, None, {},
                                    f"waiting for {minimum_labels} eligible labels")
    if {row.label for row in rows} != {"include", "exclude"}:
        return ReviewerOptimization(False, _WITH_METADATA, None, {},
                                    "waiting for both include and exclude labels")
    candidates: dict[str, float] = {}
    for configuration in (_CONTENT_ONLY, _WITH_METADATA):
        correct = 0
        for index, held_out in enumerate(rows):
            training = rows[:index] + rows[index + 1:]
            correct += predict_article(held_out.article, training,
                                       include_metadata=configuration.include_metadata).label == held_out.label
        candidates[configuration.name] = correct / len(rows)
    # A tie keeps the richer representation, which is the pre-optimization default.
    selected = _WITH_METADATA if candidates[_WITH_METADATA.name] >= candidates[_CONTENT_ONLY.name] else _CONTENT_ONLY
    return ReviewerOptimization(True, selected, candidates[selected.name], candidates,
                                "leave-one-out training comparison")

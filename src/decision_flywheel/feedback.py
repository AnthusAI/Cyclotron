"""Trusted human-feedback and scorecard-lineage contracts, independent of a model."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from numbers import Real
from typing import Sequence

from .models import DecisionTask, Item, normalize_label


# Primus-compatible provenance names.  Only explicit reviewed sources may teach
# a future learned head; imported model output is never silently promoted.
LABEL_SOURCE_VETTED = "vetted_feedback"
LABEL_SOURCE_FINAL = "regular_final_feedback"
LABEL_SOURCE_SCORE_RESULT_OR_IMPORTED = "score_result_or_imported"
LABEL_SOURCE_UNRESOLVED = "unresolved"
TRUSTED_LABEL_SOURCES = frozenset({LABEL_SOURCE_VETTED, LABEL_SOURCE_FINAL})


def _non_empty(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _fingerprint(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _hash(name: str, value: object) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{name} must be a lowercase SHA-256 fingerprint")
    return value


@dataclass(frozen=True)
class FeedbackItem:
    """One reviewed prediction; review fields stay distinct from model output.

    The default source is unresolved deliberately: receiving a final-looking
    string does not establish that a human reviewed it or that it is fit data.
    """

    id: str
    item_id: str
    score_name: str
    initial_answer_value: str | None = None
    final_answer_value: str | None = None
    edit_comment_value: str | None = None
    label_source: str = LABEL_SOURCE_UNRESOLVED
    selection_propensity: float | None = None
    review_provenance: str | None = None

    def __post_init__(self) -> None:
        _non_empty("feedback id", self.id)
        _non_empty("item_id", self.item_id)
        _non_empty("score_name", self.score_name)
        if self.label_source not in {
            LABEL_SOURCE_VETTED, LABEL_SOURCE_FINAL,
            LABEL_SOURCE_SCORE_RESULT_OR_IMPORTED, LABEL_SOURCE_UNRESOLVED,
        }:
            raise ValueError("label_source is not recognized")
        for name, value in (("initial_answer_value", self.initial_answer_value),
                            ("final_answer_value", self.final_answer_value),
                            ("edit_comment_value", self.edit_comment_value),
                            ("review_provenance", self.review_provenance)):
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{name} must be a string or None")
        if self.selection_propensity is not None:
            _propensity(self.selection_propensity)

    def trusted_label(self, task: DecisionTask) -> str | None:
        """Return a canonical reviewed label, never a guessed fallback."""
        if self.score_name != task.name:
            raise ValueError("feedback score_name must match the task name")
        if self.label_source not in TRUSTED_LABEL_SOURCES or self.final_answer_value is None:
            return None
        normalized = normalize_label(self.final_answer_value)
        if not normalized:
            return None
        return task.validate_label(self.final_answer_value)

    def training_label(self, task: DecisionTask) -> str:
        """Require both an explicit trusted review and its logged propensity."""
        label = self.trusted_label(task)
        if label is None:
            raise ValueError("feedback has no trusted reviewed label")
        if self.selection_propensity is None:
            raise ValueError("selection_propensity is required for training")
        return label


def _propensity(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value) or not 0.0 < value <= 1.0:
        raise ValueError("selection_propensity must be a finite probability in (0, 1]")
    return float(value)


@dataclass(frozen=True)
class Element:
    """A named source of features in a scorecard version."""

    key: str
    question_type: str
    feature_names: tuple[str, ...]
    definition_fingerprint: str

    def __post_init__(self) -> None:
        _non_empty("element key", self.key)
        _non_empty("question_type", self.question_type)
        if not self.feature_names or any(not isinstance(name, str) or not name for name in self.feature_names):
            raise ValueError("element feature_names must be non-empty strings")
        if len(set(self.feature_names)) != len(self.feature_names):
            raise ValueError("element feature_names must be unique")
        _hash("definition_fingerprint", self.definition_fingerprint)


@dataclass(frozen=True)
class Feature:
    """One finite, named value attributed to an element."""

    name: str
    value: float
    element_key: str

    def __post_init__(self) -> None:
        _non_empty("feature name", self.name)
        _non_empty("element_key", self.element_key)
        if isinstance(self.value, bool) or not isinstance(self.value, Real) or not math.isfinite(self.value):
            raise ValueError("feature value must be finite")


@dataclass(frozen=True)
class FeatureCoverage:
    """Declared and observed features; decisions ready for fitting require all."""

    declared: tuple[str, ...]
    observed: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.declared or any(not isinstance(name, str) or not name for name in self.declared):
            raise ValueError("declared features must be non-empty strings")
        if len(set(self.declared)) != len(self.declared) or len(set(self.observed)) != len(self.observed):
            raise ValueError("feature names must be unique")
        if set(self.observed) != set(self.declared):
            raise ValueError("feature coverage must include every declared feature exactly once")

    @property
    def complete(self) -> bool:
        return True


@dataclass(frozen=True)
class Decision:
    """A model-neutral decision tied to exact policy, request, and scorecard lineage."""

    value: str
    model_provenance: str
    policy_fingerprint: str
    context_artifact_fingerprint: str
    request_fingerprint: str
    scorecard_fingerprint: str
    feature_coverage: FeatureCoverage

    def __post_init__(self) -> None:
        _non_empty("decision value", self.value)
        _non_empty("model_provenance", self.model_provenance)
        _hash("policy_fingerprint", self.policy_fingerprint)
        _hash("context_artifact_fingerprint", self.context_artifact_fingerprint)
        _hash("request_fingerprint", self.request_fingerprint)
        _hash("scorecard_fingerprint", self.scorecard_fingerprint)

    @property
    def source_model_provenance(self) -> str:
        """Explicit alias for audit systems that call the producer a source model."""
        return self.model_provenance


@dataclass(frozen=True)
class ScoreResult:
    """A score result whose value and decision provenance cannot drift apart."""

    item_id: str
    score_name: str
    decision: Decision

    def __post_init__(self) -> None:
        _non_empty("item_id", self.item_id)
        _non_empty("score_name", self.score_name)

    @property
    def value(self) -> str:
        return self.decision.value


@dataclass(frozen=True)
class Scorecard:
    """Versioned scorecard lineage, with policy changes represented in its hash."""

    name: str
    version: int
    elements: tuple[Element, ...]
    policy_fingerprint: str
    parent_fingerprint: str | None = None

    def __post_init__(self) -> None:
        _non_empty("scorecard name", self.name)
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise ValueError("scorecard version must be a positive integer")
        if not self.elements or len({element.key for element in self.elements}) != len(self.elements):
            raise ValueError("scorecard elements must be present with unique keys")
        _hash("policy_fingerprint", self.policy_fingerprint)
        if self.parent_fingerprint is not None:
            _hash("parent_fingerprint", self.parent_fingerprint)

    @property
    def fingerprint(self) -> str:
        return _fingerprint({
            "elements": [
                {"definition_fingerprint": element.definition_fingerprint, "feature_names": element.feature_names,
                 "key": element.key, "question_type": element.question_type}
                for element in self.elements
            ],
            "name": self.name,
            "parent_fingerprint": self.parent_fingerprint,
            "policy_fingerprint": self.policy_fingerprint,
            "version": self.version,
        })

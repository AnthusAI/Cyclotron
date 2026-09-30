"""Stable, provider-neutral contracts for structured decisions."""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence as SequenceABC
from dataclasses import dataclass, replace
from numbers import Real
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

PROBABILITY_SUM_TOLERANCE = 0.01 + 1e-12


def normalize_label(value: Any) -> str:
    """Normalize a label the way Plexus Evaluation does.

    The task retains its supplied canonical strings; normalization is only used
    to recognize equivalent provider and feedback spellings.
    """
    if value is None:
        return ""
    text = str(value).lower().strip().rstrip(".!?").strip()
    if text == "nan":
        return ""
    if text == "n/a":
        return "na"
    return text


@dataclass(frozen=True)
class Item:
    id: str
    values: Mapping[str, Any]


@dataclass(frozen=True)
class LabeledItem:
    item: Item
    label: str
    source: str = "trusted"


@dataclass(frozen=True)
class DecisionTask:
    name: str
    labels: tuple[str, ...]
    instructions: str
    input_field: str = "text"

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("task name must be a non-empty string")
        if not isinstance(self.instructions, str) or not self.instructions.strip():
            raise ValueError("task instructions must be a non-empty string")
        if not isinstance(self.input_field, str) or not self.input_field.strip():
            raise ValueError("input_field must be a non-empty string")
        if isinstance(self.labels, str):
            raise ValueError("task labels must be a collection of strings, not a string")
        if not isinstance(self.labels, SequenceABC):
            raise ValueError("task labels must be an ordered sequence of strings")
        labels = tuple(self.labels)
        if not labels:
            raise ValueError("task must declare at least one label")
        normalized = []
        for label in labels:
            if not isinstance(label, str) or not normalize_label(label):
                raise ValueError("task labels must be non-empty strings")
            normalized.append(normalize_label(label))
        if len(set(normalized)) != len(normalized):
            raise ValueError("task labels must be unique after normalization")
        object.__setattr__(self, "labels", labels)

    @property
    def fingerprint(self) -> str:
        """A stable identifier for the task contract, not its request data."""
        contract = {
            "input_field": self.input_field,
            "instructions": self.instructions,
            "labels": self.labels,
            "name": self.name,
        }
        encoded = json.dumps(contract, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def validate_label(self, label: Any) -> str:
        """Return the task's canonical label for an equivalent input label."""
        normalized = normalize_label(label)
        for canonical in self.labels:
            if normalize_label(canonical) == normalized:
                return canonical
        raise ValueError(f"{label!r} is not one of {self.labels!r}")

    def validate_target(self, target: Item) -> None:
        value = target.values.get(self.input_field)
        if not isinstance(value, str):
            raise ValueError(f"target lacks string {self.input_field!r}")

    def validate_result(self, result: "DecisionResult") -> "DecisionResult":
        """Validate and canonicalize a provider result for this task."""
        label = self.validate_label(result.label)
        if result.probabilities is None:
            return replace(result, label=label)

        probabilities: dict[str, float] = {}
        for raw_label, probability in result.probabilities.items():
            canonical = self.validate_label(raw_label)
            if canonical in probabilities:
                raise ValueError("probability distribution contains duplicate normalized labels")
            probabilities[canonical] = probability
        if set(probabilities) != set(self.labels):
            raise ValueError("probability distribution must cover every task label and no others")
        if not all(isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value)
                   and 0.0 <= value <= 1.0 for value in probabilities.values()):
            raise ValueError("probabilities must be finite numbers between zero and one")
        # Providers commonly round each choice independently (for example,
        # three displayed 0.33 values).  Preserve those values for auditing;
        # validate their plausible rounding error instead of renormalizing.
        if not math.isclose(sum(probabilities.values()), 1.0, rel_tol=0.0,
                            abs_tol=PROBABILITY_SUM_TOLERANCE):
            raise ValueError("probabilities must sum to one within rounding tolerance")
        return replace(result, label=label, probabilities=probabilities)


@dataclass(frozen=True)
class DecisionResult:
    label: str
    probabilities: Mapping[str, float] | None = None
    model: str | None = None
    usage: Mapping[str, Any] | None = None
    latency_ms: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.label, str) or not self.label.strip():
            raise ValueError("result label must be a non-empty string")
        if self.probabilities is not None and not isinstance(self.probabilities, Mapping):
            raise ValueError("probabilities must be a mapping when present")
        if self.usage is not None and not isinstance(self.usage, Mapping):
            raise ValueError("usage must be a mapping when present")
        if self.latency_ms is not None and (
            isinstance(self.latency_ms, bool)
            or not isinstance(self.latency_ms, Real)
            or not math.isfinite(self.latency_ms)
            or self.latency_ms < 0
        ):
            raise ValueError("latency_ms must be a non-negative finite number when present")


@dataclass(frozen=True)
class ModelCapabilities:
    """Features an adapter is known to support without changing request meaning.

    A capability describes supported request or response modes, not a promise
    that every provider response includes an optional field.
    """
    supports_labeled_context: bool = True
    supports_probability_distributions: bool = False

    def validate_context(self, context: Sequence[LabeledItem]) -> None:
        if context and not self.supports_labeled_context:
            raise ValueError("model does not support labeled context")


@runtime_checkable
class DecisionModel(Protocol):
    name: str
    capabilities: ModelCapabilities

    async def decide(self, task: DecisionTask, target: Item,
                     context: Sequence[LabeledItem]) -> DecisionResult:
        ...

"""Stable, model-neutral types for structured decisions."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable


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

    def validate_label(self, label: str) -> None:
        if label not in self.labels:
            raise ValueError(f"{label!r} is not one of {self.labels!r}")


@dataclass(frozen=True)
class DecisionResult:
    label: str
    probabilities: Mapping[str, float] = field(default_factory=dict)
    model: str | None = None
    usage: Mapping[str, Any] | None = None
    latency_ms: float | None = None


@runtime_checkable
class DecisionModel(Protocol):
    name: str

    async def decide(self, task: DecisionTask, target: Item,
                     context: Sequence[LabeledItem]) -> DecisionResult:
        ...

"""Jev's TypeSafe System One adapter with an injectable synchronous client."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from numbers import Real
import time
from typing import Any, Sequence

from ..models import DecisionResult, DecisionTask, Item, LabeledItem, ModelCapabilities


@dataclass(frozen=True)
class JevConfiguration:
    """Stable cache/checkpoint identity; retries belong to the outer runner."""

    model: str = "jev-latest"
    base_url: str | None = None
    timeout_seconds: float = 30.0
    max_retries: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("model must be a non-empty string")
        if isinstance(self.timeout_seconds, bool) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_retries != 0:
            raise ValueError("Jev adapter retries must be disabled; the outer runner counts attempts")

    @property
    def model_identity(self) -> str:
        return f"jev:{self.model}"


class JevAdapter:
    """Use Jev's documented ``system_one(state, questions)`` contract.

    ``client`` is deliberately injected for tests and applications.  ``from_environment``
    is the only convenience factory and constructs the SDK lazily.
    """

    name = "jev"
    capabilities = ModelCapabilities(supports_labeled_context=True, supports_probability_distributions=True)

    def __init__(self, client: Any, *, configuration: JevConfiguration = JevConfiguration()):
        self.client = client
        self.configuration = configuration

    @property
    def model_identity(self) -> str:
        return self.configuration.model_identity

    @classmethod
    def from_environment(cls, *, configuration: JevConfiguration = JevConfiguration()) -> "JevAdapter":
        """Load a gitignored dotenv file, if installed, without exposing its values."""
        try:
            from dotenv import load_dotenv
        except ImportError as error:  # pragma: no cover - optional dependency
            raise ImportError("Install decision-flywheel[jev] to construct a Jev SDK client.") from error
        try:
            from typesafe_sdk import TypeSafeClient
            from typesafe_sdk._core.retry import RetryPolicy
        except ImportError as error:  # pragma: no cover - optional dependency
            raise ImportError("Install decision-flywheel[jev] to construct a Jev SDK client.") from error
        load_dotenv(override=False)
        client = TypeSafeClient(model=configuration.model, base_url=configuration.base_url,
                                timeout=configuration.timeout_seconds,
                                retry=RetryPolicy(max_retries=0))
        return cls(client, configuration=configuration)

    async def decide(self, task: DecisionTask, target: Item,
                     context: Sequence[LabeledItem]) -> DecisionResult:
        self.capabilities.validate_context(context)
        task.validate_target(target)
        for example in context:
            task.validate_target(example.item)
            task.validate_label(example.label)
        state = {
            "labeled_examples": [
                {"text": example.item.values[task.input_field], "label": example.label, **dict(example.context)}
                for example in context
            ],
            "target": {"text": target.values[task.input_field]},
        }
        question = {"type": "choice", "instructions": task.instructions,
                    "criteria": {label: None for label in task.labels}}
        started = time.perf_counter()
        response = await asyncio.to_thread(
            self.client.system_one, state=state, questions={task.name: question}
        )
        answer = _mapping(_mapping(getattr(response, "answers", {})).get(task.name))
        result = DecisionResult(
            answer["choice"], answer.get("probabilities"), _string_or_none(getattr(response, "model", None)),
            _numeric_usage(getattr(response, "usage", None)), round((time.perf_counter() - started) * 1000, 2),
            answer.get("confidence"),
        )
        return task.validate_result(result)


def _mapping(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if not isinstance(value, dict):
        raise ValueError("Jev response is missing a structured answer")
    return value.get("root", value)


def _numeric_usage(value: Any) -> dict[str, int | float] | None:
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if not isinstance(value, dict):
        return None
    usage = {key: amount for key, amount in value.items()
             if isinstance(key, str) and isinstance(amount, Real) and not isinstance(amount, bool)}
    return usage or None


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None

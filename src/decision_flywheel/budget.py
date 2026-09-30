"""Small, auditable context budgets and presentation plans.

Selection decides *which* demonstrations belong in a request.  This module
decides whether that request fits, and how the already-selected examples are
shown.  Its token figures are deterministic estimates, never provider usage.
"""
from __future__ import annotations

import hashlib
import json
import random
import re
from dataclasses import dataclass, replace
from typing import Protocol, Sequence

from .context import ContextPolicy, PolicyMetadata, validate_selected_context
from .models import DecisionTask, Item, LabeledItem


_WORDS = re.compile(r"\S+")
_DISPLAY_ORDERS = frozenset({"canonical", "interleaved", "reversed", "shuffled"})


class TokenCounter(Protocol):
    """A deterministic counter over the complete, serialized model request."""

    identity: str

    def count(self, serialized_request: str) -> int:
        ...


@dataclass(frozen=True)
class DeterministicTokenCounter:
    """A deliberately simple local estimate, suitable for comparisons and tests."""

    identity: str = "whitespace-request-estimate-v1"

    def count(self, serialized_request: str) -> int:
        return len(_WORDS.findall(serialized_request))


@dataclass(frozen=True)
class ContextBudget:
    """Separate example count and request-size constraints.

    ``max_tokens`` and ``provider_token_limit`` both apply to the complete
    request.  They intentionally do not claim to be provider-reported usage.
    """

    per_label: int = 0
    max_tokens: int | None = None
    provider_token_limit: int | None = None

    def __post_init__(self) -> None:
        _non_negative_int("per_label", self.per_label)
        _positive_or_none("max_tokens", self.max_tokens)
        _positive_or_none("provider_token_limit", self.provider_token_limit)


@dataclass(frozen=True)
class TokenAccounting:
    """Reproducible estimate metadata, distinct from a provider usage report."""

    estimated_request_tokens: int
    counter_identity: str
    serialized_request_fingerprint: str
    provider_usage: bool = False


@dataclass(frozen=True)
class ContextPlan:
    """An immutable request-context plan that can be stored with a decision."""

    examples: tuple[LabeledItem, ...]
    example_ids: tuple[str, ...]
    selection_policy: str
    policy_metadata: PolicyMetadata
    policy_fingerprint: str
    task_fingerprint: str
    order_seed: int
    display_order: str
    presentation_label_order: tuple[str, ...]
    budget: ContextBudget
    token_accounting: TokenAccounting


class BudgetExceededError(ValueError):
    """The requested context is infeasible; no examples were dropped."""


def build_context_plan(
    task: DecisionTask,
    target: Item,
    candidates: Sequence[LabeledItem],
    policy: ContextPolicy,
    *,
    budget: ContextBudget | None = None,
    display_order: str = "canonical",
    order_seed: int = 0,
    presentation_label_order: Sequence[str] | None = None,
    token_counter: TokenCounter | None = None,
) -> ContextPlan:
    """Select a balanced context, then check the fully serialized request.

    A zero-shot budget constructs an empty plan without invoking ``policy``;
    selectors remain free to require their positive ``per_label`` contract.
    """
    task.validate_target(target)
    _validate_order(display_order, order_seed)
    resolved_label_order = _resolve_presentation_label_order(task, presentation_label_order)
    resolved_budget = budget or ContextBudget()
    selected = () if resolved_budget.per_label == 0 else validate_selected_context(
        task, target, candidates,
        policy.select(task, target, candidates, per_label=resolved_budget.per_label),
        per_label=resolved_budget.per_label,
    )
    return _plan_from_selected(
        task, target, selected, policy, budget=resolved_budget, display_order=display_order,
        order_seed=order_seed, presentation_label_order=resolved_label_order,
        token_counter=token_counter,
    )


def build_context_ladder(
    task: DecisionTask,
    target: Item,
    candidates: Sequence[LabeledItem],
    policy: ContextPolicy,
    *,
    per_label_counts: Sequence[int],
    budget: ContextBudget | None = None,
    display_order: str = "canonical",
    order_seed: int = 0,
    presentation_label_order: Sequence[str] | None = None,
    token_counter: TokenCounter | None = None,
) -> tuple[ContextPlan, ...]:
    """Build nested count plans by selecting once at the largest requested count."""
    counts = tuple(per_label_counts)
    if not counts:
        raise ValueError("per_label_counts must not be empty")
    for count in counts:
        _non_negative_int("per_label_counts", count)
    task.validate_target(target)
    _validate_order(display_order, order_seed)
    resolved_label_order = _resolve_presentation_label_order(task, presentation_label_order)
    base_budget = budget or ContextBudget()
    maximum = max(counts)
    selected = () if maximum == 0 else validate_selected_context(
        task, target, candidates, policy.select(task, target, candidates, per_label=maximum),
        per_label=maximum,
    )
    by_label = {label: tuple(item for item in selected if item.label == label) for label in task.labels}
    for label, examples in by_label.items():
        if len(examples) < maximum:
            raise ValueError(f"selection policy returned fewer than {maximum} examples for label {label!r}")

    plans = []
    for count in counts:
        frozen_selection = tuple(item for label in task.labels for item in by_label[label][:count])
        plans.append(_plan_from_selected(
            task, target, frozen_selection, policy,
            budget=replace(base_budget, per_label=count), display_order=display_order,
            order_seed=order_seed, presentation_label_order=resolved_label_order,
            token_counter=token_counter,
        ))
    return tuple(plans)


def _plan_from_selected(
    task: DecisionTask,
    target: Item,
    selected: Sequence[LabeledItem],
    policy: ContextPolicy,
    *,
    budget: ContextBudget,
    display_order: str,
    order_seed: int,
    presentation_label_order: tuple[str, ...],
    token_counter: TokenCounter | None,
) -> ContextPlan:
    examples = _present(selected, display_order, order_seed, presentation_label_order)
    counter = token_counter or DeterministicTokenCounter()
    identity = getattr(counter, "identity", None)
    if not isinstance(identity, str) or not identity:
        raise ValueError("token_counter must declare a non-empty identity")
    serialized = _serialize_request(task, target, examples)
    estimated = counter.count(serialized)
    _non_negative_int("token counter result", estimated)
    accounting = TokenAccounting(
        estimated_request_tokens=estimated,
        counter_identity=identity,
        serialized_request_fingerprint=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
    )
    _check_limits(accounting, budget)
    return ContextPlan(
        examples=examples,
        example_ids=tuple(item.item.id for item in examples),
        selection_policy=policy.name,
        policy_metadata=policy.metadata,
        policy_fingerprint=policy.fingerprint,
        task_fingerprint=task.fingerprint,
        order_seed=order_seed,
        display_order=display_order,
        presentation_label_order=presentation_label_order,
        budget=budget,
        token_accounting=accounting,
    )


def _serialize_request(task: DecisionTask, target: Item,
                       examples: Sequence[LabeledItem]) -> str:
    """Serialize the actual System One wire shape for deterministic estimates.

    Mapping fields are inserted in a fixed order.  ``criteria`` intentionally
    retains ``task.labels`` order: options are a presentation contract, not an
    unordered JSON object to normalize away.
    """
    task.validate_target(target)
    state = {
        "labeled_examples": [
            {"text": _text(example.item, task), "label": task.validate_label(example.label)}
            for example in examples
        ],
        "target": {"text": _text(target, task)},
    }
    question = {
        "type": "choice",
        "instructions": task.instructions,
        "criteria": {label: None for label in task.labels},
    }
    return json.dumps({"state": state, "questions": {task.name: question}}, ensure_ascii=False,
                      separators=(",", ":"))


def _present(selected: Sequence[LabeledItem], order: str, seed: int,
             label_order: Sequence[str]) -> tuple[LabeledItem, ...]:
    # "Canonical" means lexical label order by default, deliberately independent
    # of task choice-option order.  ``presentation_label_order`` lets callers
    # preserve an explicit historical display convention instead.
    groups = {label: sorted((item for item in selected if item.label == label), key=lambda item: item.item.id)
              for label in label_order}
    canonical = [item for label in label_order for item in groups[label]]
    if order == "canonical":
        return tuple(canonical)
    if order == "reversed":
        return tuple(reversed(canonical))
    if order == "interleaved":
        return tuple(item for index in range(max((len(group) for group in groups.values()), default=0))
                     for label in label_order for item in groups[label][index:index + 1])
    shuffled = list(canonical)
    random.Random(seed).shuffle(shuffled)
    return tuple(shuffled)


def _check_limits(accounting: TokenAccounting, budget: ContextBudget) -> None:
    for name, limit in (("max_tokens", budget.max_tokens),
                        ("provider_token_limit", budget.provider_token_limit)):
        if limit is not None and accounting.estimated_request_tokens > limit:
            raise BudgetExceededError(
                f"{name}={limit} is infeasible: "
                f"estimated_request_tokens={accounting.estimated_request_tokens}"
            )


def _text(item: Item, task: DecisionTask) -> str:
    value = item.values.get(task.input_field)
    if not isinstance(value, str):
        raise ValueError(f"{item.id!r} has no string {task.input_field!r}")
    return value


def _validate_order(display_order: str, order_seed: int) -> None:
    if display_order not in _DISPLAY_ORDERS:
        raise ValueError(f"display_order must be one of {sorted(_DISPLAY_ORDERS)!r}")
    _non_negative_int("order_seed", order_seed)


def _resolve_presentation_label_order(task: DecisionTask,
                                      presentation_label_order: Sequence[str] | None) -> tuple[str, ...]:
    """Use lexical labels by default, or an explicit task-label permutation."""
    if presentation_label_order is None:
        return tuple(sorted(task.labels))
    if isinstance(presentation_label_order, str):
        raise ValueError("presentation_label_order must be a task-label permutation")
    resolved = tuple(presentation_label_order)
    if len(resolved) != len(task.labels) or set(resolved) != set(task.labels):
        raise ValueError("presentation_label_order must be an exact permutation of task.labels")
    return resolved


def _non_negative_int(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _positive_or_none(name: str, value: object) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
        raise ValueError(f"{name} must be a positive integer or None")

"""Development-only, resumable search over explicit context-policy trials."""
from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import MutableMapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Mapping

from .budget import ContextBudget, build_context_plan
from .context import ContextPolicy, PolicyMetadata
from .events import EventSink, FlywheelEvent
from .models import DecisionModel, DecisionResult, DecisionTask, Item, LabeledItem


Objective = Literal["accuracy", "macro-f1", "brier"]
TrialStatus = Literal["completed", "incomplete", "not-run"]
FailureReason = Literal["context-infeasible", "model-failure", "call-budget-exhausted",
                        "missing-probabilities"]
_CALL_ACCOUNTING_KEY = "model_calls_attempted"


@dataclass(frozen=True)
class TrialSpec:
    """One explicit policy/count/budget candidate for development search."""

    policy: ContextPolicy
    per_label: int
    name: str | None = None
    budget: ContextBudget | None = None

    def __post_init__(self) -> None:
        if isinstance(self.per_label, bool) or not isinstance(self.per_label, int) or self.per_label < 0:
            raise ValueError("per_label must be a non-negative integer")
        if self.name is not None and (not isinstance(self.name, str) or not self.name.strip()):
            raise ValueError("trial name must be a non-empty string when present")
        if self.budget is not None and self.budget.per_label != self.per_label:
            raise ValueError("trial budget per_label must match the trial per_label")

    @property
    def trial_name(self) -> str:
        return self.name or f"{self.policy.name}:{self.policy.fingerprint[:12]}:{self.per_label}"

    @property
    def context_budget(self) -> ContextBudget:
        return self.budget or ContextBudget(per_label=self.per_label)


@dataclass(frozen=True)
class DecisionHistory:
    """Source-text-free record of one development request outcome."""

    target_id: str
    request_fingerprint: str
    prediction: str
    from_checkpoint: bool
    probabilities: Mapping[str, float] | None = None  # recorded only for the Brier objective


@dataclass(frozen=True)
class TrialResult:
    """A development-only trial result; incomplete trials cannot become winners."""

    trial_name: str
    policy_metadata: PolicyMetadata
    policy_fingerprint: str
    per_label: int
    budget: ContextBudget
    display_order: str
    order_seed: int
    presentation_label_order: tuple[str, ...]
    status: TrialStatus
    objective: float | None
    decisions: tuple[DecisionHistory, ...]
    failure_count: int
    failure_reasons: tuple[FailureReason, ...]


@dataclass(frozen=True)
class OptimizationResult:
    """Frozen winner and audit trail, deliberately free of source texts and labels."""

    objective: Objective
    model_fingerprint: str
    task_fingerprint: str
    candidate_pool_fingerprint: str
    development_split_fingerprint: str
    search_fingerprint: str
    max_model_calls: int
    model_calls_attempted: int
    model_calls_succeeded: int
    cumulative_model_calls_attempted: int
    trials: tuple[TrialResult, ...]
    provisional_best: TrialResult | None
    winner: TrialResult | None
    checkpoint_entries: int


async def search_context_policies(
    task: DecisionTask,
    candidates: Sequence[LabeledItem],
    development: Sequence[LabeledItem],
    model: DecisionModel,
    trials: Sequence[TrialSpec],
    *,
    max_model_calls: int,
    objective: Objective = "accuracy",
    checkpoint: MutableMapping[str, dict[str, str]] | None = None,
    call_accounting: MutableMapping[str, int | str] | None = None,
    model_fingerprint: str | None = None,
    protected_ids: Sequence[str] = (),
    protected_text_hashes: Sequence[str] = (),
    display_order: str = "canonical",
    order_seed: int = 0,
    presentation_label_order: Sequence[str] | None = None,
    event_sink: EventSink | None = None,
) -> OptimizationResult:
    """Search declared trials only against trusted, disjoint development labels.

    ``max_model_calls`` is per invocation when ``call_accounting`` is omitted.
    When a caller supplies that serializable mapping, it is a cumulative ceiling
    across invocations.  The mapping contains only ``model_calls_attempted``
    and this search's provenance ``search_fingerprint``; its call count is
    updated after every attempted model call, including failures.  This makes
    an interrupted search resumable without quietly starting its allowance over
    or attaching it to a different task, pool, development split, or trial set.

    ``objective="brier"`` scores the provider's probability distributions (the
    label-averaged multi-class Brier score, which for two labels is the familiar
    binary Brier score); lower is better, so the winner is the lowest score.  A
    trial whose model returns no distribution is incomplete and cannot win.

    ``checkpoint`` is caller-owned and serializable: each entry is a hash key
    mapped to ``{"label": canonical_label}``, plus ``"probabilities"`` when the
    objective is Brier.  Under Brier a label-only entry is re-asked.  Its key includes model identity,
    the task contract, and the complete serialized request fingerprint, which
    includes the ordered context and target.  A checkpoint hit therefore reuses
    a response only for an identical effective request, never by policy name.
    """
    _validate_search_inputs(task, candidates, development, trials, max_model_calls,
                            objective, protected_ids, protected_text_hashes)
    resolved_label_order = _resolve_presentation_label_order(task, presentation_label_order)
    resolved_model_fingerprint = _resolve_model_fingerprint(model, model_fingerprint)
    candidate_pool_fingerprint = _split_fingerprint(task, candidates)
    development_split_fingerprint = _split_fingerprint(task, development)
    search_fingerprint = _search_fingerprint(
        task.fingerprint, candidate_pool_fingerprint, development_split_fingerprint,
        resolved_model_fingerprint, trials, objective, max_model_calls, display_order,
        order_seed, resolved_label_order,
    )
    prior_attempted = _read_call_accounting(call_accounting, search_fingerprint)
    if prior_attempted > max_model_calls:
        raise ValueError("call accounting already exceeds max_model_calls")
    store: MutableMapping[str, dict[str, str]] = checkpoint if checkpoint is not None else {}
    attempted = succeeded = 0
    results: list[TrialResult] = []
    _emit(event_sink, "round-started", None, None, attempted, succeeded)
    for spec in trials:
        _emit(event_sink, "trial-started", spec.trial_name, None, attempted, succeeded)
        decisions: list[DecisionHistory] = []
        failures = 0
        reasons: list[FailureReason] = []
        complete = True
        for development_item in development:
            try:
                plan = build_context_plan(
                    task, development_item.item, candidates, spec.policy,
                    budget=spec.context_budget, display_order=display_order, order_seed=order_seed,
                    presentation_label_order=resolved_label_order,
                )
            except ValueError:
                # An infeasible context cannot supply a valid objective; no
                # model request was attempted and no cache entry is written.
                complete = False
                failures += 1
                reasons.append("context-infeasible")
                break

            key = _checkpoint_key(resolved_model_fingerprint, task, plan)
            cached = store.get(key)
            if cached is not None:
                prediction, probabilities = _checkpoint_entry(task, cached)
                if objective != "brier":
                    decisions.append(DecisionHistory(development_item.item.id, key, prediction, True))
                    _emit(event_sink, "decision-reused", spec.trial_name, key, attempted, succeeded)
                    continue
                if probabilities is not None:
                    decisions.append(DecisionHistory(development_item.item.id, key, prediction, True,
                                                     probabilities))
                    _emit(event_sink, "decision-reused", spec.trial_name, key, attempted, succeeded)
                    continue
            if prior_attempted + attempted >= max_model_calls:
                complete = False
                reasons.append("call-budget-exhausted")
                break

            attempted += 1
            _write_call_accounting(call_accounting, prior_attempted + attempted, search_fingerprint)
            _emit(event_sink, "decision-requested", spec.trial_name, key, attempted, succeeded)
            try:
                result = task.validate_result(await model.decide(task, development_item.item, plan.examples))
                prediction = result.label
            except Exception:
                # Calls that raise still consume the ceiling, but are never
                # cached and never turn a partial trial into a winner.
                complete = False
                failures += 1
                reasons.append("model-failure")
                _emit(event_sink, "decision-failed", spec.trial_name, key, attempted, succeeded)
                break
            succeeded += 1
            _emit(event_sink, "decision-completed", spec.trial_name, key, attempted, succeeded)
            if objective != "brier":
                store[key] = {"label": prediction}
                decisions.append(DecisionHistory(development_item.item.id, key, prediction, False))
                continue
            if result.probabilities is None:
                complete = False
                failures += 1
                reasons.append("missing-probabilities")
                break
            probabilities = dict(result.probabilities)
            store[key] = {"label": prediction, "probabilities": probabilities}
            decisions.append(DecisionHistory(development_item.item.id, key, prediction, False, probabilities))

        score = _score(task, development, decisions, objective) if complete else None
        results.append(TrialResult(
            trial_name=spec.trial_name,
            policy_metadata=spec.policy.metadata,
            policy_fingerprint=spec.policy.fingerprint,
            per_label=spec.per_label,
            budget=spec.context_budget,
            display_order=display_order,
            order_seed=order_seed,
            presentation_label_order=resolved_label_order,
            status="completed" if complete else "incomplete",
            objective=score,
            decisions=tuple(decisions),
            failure_count=failures,
            failure_reasons=tuple(reasons),
        ))
        _emit(event_sink, "trial-completed" if complete else "trial-incomplete", spec.trial_name,
              None, attempted, succeeded)

    completed = [trial for trial in results if trial.status == "completed"]
    sign = -1 if objective == "brier" else 1
    provisional_best = (max(completed, key=lambda trial: (sign * trial.objective, trial.trial_name))
                        if completed else None)
    winner = provisional_best if len(completed) == len(results) else None
    result = OptimizationResult(
        objective=objective,
        model_fingerprint=resolved_model_fingerprint,
        task_fingerprint=task.fingerprint,
        candidate_pool_fingerprint=candidate_pool_fingerprint,
        development_split_fingerprint=development_split_fingerprint,
        search_fingerprint=search_fingerprint,
        max_model_calls=max_model_calls,
        model_calls_attempted=attempted,
        model_calls_succeeded=succeeded,
        cumulative_model_calls_attempted=prior_attempted + attempted,
        trials=tuple(results),
        provisional_best=provisional_best,
        winner=winner,
        checkpoint_entries=len(store),
    )
    _emit(event_sink, "round-completed", None, None, attempted, succeeded)
    return result


def _emit(sink: EventSink | None, event_type: str, trial_name: str | None,
          request_fingerprint: str | None, attempted: int, succeeded: int) -> None:
    if sink is not None:
        sink(FlywheelEvent(event_type, trial_name, request_fingerprint, attempted, succeeded))


def _validate_search_inputs(
    task: DecisionTask,
    candidates: Sequence[LabeledItem],
    development: Sequence[LabeledItem],
    trials: Sequence[TrialSpec],
    max_model_calls: int,
    objective: str,
    protected_ids: Sequence[str],
    protected_text_hashes: Sequence[str],
) -> None:
    if isinstance(max_model_calls, bool) or not isinstance(max_model_calls, int) or max_model_calls < 0:
        raise ValueError("max_model_calls must be a non-negative integer")
    if objective not in {"accuracy", "macro-f1", "brier"}:
        raise ValueError("objective must be 'accuracy', 'macro-f1' or 'brier'")
    if not trials:
        raise ValueError("trials must not be empty")
    names = [trial.trial_name for trial in trials]
    if len(set(names)) != len(names):
        raise ValueError("trial names must be unique")
    protected_id_set = set(protected_ids)
    protected_hash_set = set(protected_text_hashes)
    candidate_ids, candidate_texts = _validate_labeled_split(
        task, candidates, "candidates", protected_id_set, protected_hash_set
    )
    development_ids, development_texts = _validate_labeled_split(
        task, development, "development", protected_id_set, protected_hash_set
    )
    if candidate_ids & development_ids or candidate_texts & development_texts:
        raise ValueError("candidate and development inputs must not overlap by ID or normalized text")


def _validate_labeled_split(
    task: DecisionTask,
    rows: Sequence[LabeledItem],
    split: str,
    protected_ids: set[str],
    protected_hashes: set[str],
) -> tuple[set[str], set[str]]:
    ids: set[str] = set()
    text_hashes: set[str] = set()
    for row in rows:
        if row.source != "trusted":
            raise ValueError(f"{split} labels must have trusted sources")
        canonical = task.validate_label(row.label)
        if row.label != canonical:
            raise ValueError(f"{split} labels must use canonical task labels")
        text_hash = _text_hash(task, row.item)
        if row.item.id in protected_ids or text_hash in protected_hashes:
            raise ValueError(f"{split} includes a protected record")
        if row.item.id in ids or text_hash in text_hashes:
            raise ValueError(f"{split} contains duplicate IDs or normalized text")
        ids.add(row.item.id)
        text_hashes.add(text_hash)
    if not rows:
        raise ValueError(f"{split} must not be empty")
    return ids, text_hashes


def _resolve_model_fingerprint(model: DecisionModel, supplied: str | None) -> str:
    value = supplied if supplied is not None else getattr(model, "fingerprint", None)
    if not isinstance(value, str) or not value:
        raise ValueError("model_fingerprint is required unless the model declares a stable fingerprint")
    return value


def _text_hash(task: DecisionTask, item: Item) -> str:
    value = item.values.get(task.input_field)
    if not isinstance(value, str):
        raise ValueError(f"{item.id!r} has no string {task.input_field!r}")
    normalized = " ".join(unicodedata.normalize("NFKC", value).casefold().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _checkpoint_key(model_fingerprint: str, task: DecisionTask, plan: Any) -> str:
    payload = {
        "model_fingerprint": model_fingerprint,
        "task_fingerprint": task.fingerprint,
        "request_fingerprint": plan.token_accounting.serialized_request_fingerprint,
        "context_ids": plan.example_ids,
        "display_order": plan.display_order,
        "order_seed": plan.order_seed,
        "presentation_label_order": plan.presentation_label_order,
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _checkpoint_label(task: DecisionTask, value: object) -> str:
    return _checkpoint_entry(task, value)[0]


def _checkpoint_entry(task: DecisionTask, value: object) -> tuple[str, dict[str, float] | None]:
    """A label, plus a validated probability distribution when the entry has one."""
    if not isinstance(value, dict) or set(value) not in ({"label"}, {"label", "probabilities"}):
        raise ValueError("checkpoint entry must contain a label and optionally probabilities")
    label = task.validate_label(value["label"])
    if "probabilities" not in value:
        return label, None
    probabilities = value["probabilities"]
    if not isinstance(probabilities, dict):
        raise ValueError("checkpoint probabilities must be a mapping")
    validated = task.validate_result(DecisionResult(label, probabilities=probabilities))
    return validated.label, dict(validated.probabilities)


def _split_fingerprint(task: DecisionTask, rows: Sequence[LabeledItem]) -> str:
    """Hash an order-independent split manifest without retaining source text."""
    manifest = sorted(
        (row.item.id, task.validate_label(row.label), _text_hash(task, row.item),
         _context_hash(row.context))
        for row in rows
    )
    return _fingerprint(manifest)


def _context_hash(context: Mapping[str, str]) -> str:
    """Bind cache and split provenance to demo-only context without retaining it."""
    encoded = json.dumps(dict(context), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _search_fingerprint(
    task_fingerprint: str,
    candidate_pool_fingerprint: str,
    development_split_fingerprint: str,
    model_fingerprint: str,
    trials: Sequence[TrialSpec],
    objective: Objective,
    max_model_calls: int,
    display_order: str,
    order_seed: int,
    presentation_label_order: tuple[str, ...],
) -> str:
    """Bind cumulative call accounting to all objective-defining inputs."""
    declared_trials = [
        {
            "name": trial.trial_name,
            "policy_fingerprint": trial.policy.fingerprint,
            "per_label": trial.per_label,
            "budget": {
                "max_tokens": trial.context_budget.max_tokens,
                "per_label": trial.context_budget.per_label,
                "provider_token_limit": trial.context_budget.provider_token_limit,
            },
        }
        for trial in trials
    ]
    return _fingerprint({
        "candidate_pool_fingerprint": candidate_pool_fingerprint,
        "declared_trials": declared_trials,
        "development_split_fingerprint": development_split_fingerprint,
        "display_order": display_order,
        "max_model_calls": max_model_calls,
        "model_fingerprint": model_fingerprint,
        "objective": objective,
        "order_seed": order_seed,
        "presentation_label_order": presentation_label_order,
        "task_fingerprint": task_fingerprint,
    })


def _fingerprint(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _read_call_accounting(accounting: MutableMapping[str, int | str] | None,
                          search_fingerprint: str) -> int:
    if accounting is None:
        return 0
    if not accounting:
        _write_call_accounting(accounting, 0, search_fingerprint)
        return 0
    value = accounting.get(_CALL_ACCOUNTING_KEY, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("call accounting model_calls_attempted must be a non-negative integer")
    scope = accounting.get("search_fingerprint")
    if not isinstance(scope, str) or not scope:
        raise ValueError("call accounting must include a search fingerprint")
    if scope != search_fingerprint:
        raise ValueError("call accounting search fingerprint does not match this search")
    if set(accounting) != {_CALL_ACCOUNTING_KEY, "search_fingerprint"}:
        raise ValueError("call accounting may contain only model_calls_attempted and search_fingerprint")
    return value


def _write_call_accounting(accounting: MutableMapping[str, int | str] | None, attempted: int,
                           search_fingerprint: str) -> None:
    if accounting is not None:
        accounting[_CALL_ACCOUNTING_KEY] = attempted
        accounting["search_fingerprint"] = search_fingerprint


def _score(task: DecisionTask, development: Sequence[LabeledItem], decisions: Sequence[DecisionHistory],
           objective: Objective) -> float:
    predictions = {decision.target_id: decision.prediction for decision in decisions}
    if set(predictions) != {row.item.id for row in development}:
        raise ValueError("only complete development trials may be scored")
    actual = {row.item.id: row.label for row in development}
    if objective == "brier":
        distributions = {decision.target_id: decision.probabilities for decision in decisions}
        return brier_score(task, actual, distributions)
    if objective == "accuracy":
        return sum(predictions[item_id] == label for item_id, label in actual.items()) / len(actual)
    f1s = []
    for label in task.labels:
        true_positive = sum(actual[item_id] == label and predictions[item_id] == label for item_id in actual)
        false_positive = sum(actual[item_id] != label and predictions[item_id] == label for item_id in actual)
        false_negative = sum(actual[item_id] == label and predictions[item_id] != label for item_id in actual)
        denominator = 2 * true_positive + false_positive + false_negative
        f1s.append(2 * true_positive / denominator if denominator else 0.0)
    return sum(f1s) / len(f1s)


def _resolve_presentation_label_order(task: DecisionTask,
                                      label_order: Sequence[str] | None) -> tuple[str, ...]:
    """Mirror the explicit request-order contract of the budget planner."""
    if label_order is None:
        return tuple(sorted(task.labels))
    if isinstance(label_order, str):
        raise ValueError("presentation_label_order must be a task-label permutation")
    resolved = tuple(label_order)
    if len(resolved) != len(task.labels) or set(resolved) != set(task.labels):
        raise ValueError("presentation_label_order must be an exact permutation of task.labels")
    return resolved


def _not_run_result(spec: TrialSpec, display_order: str, order_seed: int,
                    presentation_label_order: tuple[str, ...]) -> TrialResult:
    return TrialResult(spec.trial_name, spec.policy.metadata, spec.policy.fingerprint,
                       spec.per_label, spec.context_budget, display_order, order_seed,
                       presentation_label_order, "not-run", None, (), 0, ())


def brier_score(task: DecisionTask, actual: Mapping[str, str],
                distributions: Mapping[str, Mapping[str, float] | None]) -> float:
    """Mean over items of the label-averaged squared error of a distribution.

    For two labels this equals the binary Brier score of either label.
    """
    total = 0.0
    for item_id, label in actual.items():
        distribution = distributions.get(item_id)
        if distribution is None:
            raise ValueError("the Brier objective needs a probability distribution for every item")
        total += sum((float(distribution[name]) - (1.0 if name == label else 0.0)) ** 2
                     for name in task.labels) / len(task.labels)
    return total / len(actual)

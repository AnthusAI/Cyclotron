"""``improve_example_list``: a small, native, deterministic optimizer for one fixed example list.

Each round declares at most three trials, all ``FixedExampleList`` policies of the same size:

1. **incumbent** -- the list being served (round 1: the ``PrototypeBalanced`` seed list);
2. **hard-swap** -- the incumbent with the ``ceil(k/2)`` most recently added examples of each
   label replaced by *hard demos*: labeled items the current classifier got wrong, most
   confidently wrong first (topped up from the prototype ranking when a label has too few);
3. **random-control** -- a fresh ``RandomBalanced(seed)`` draw.

They are scored with ``search_context_policies`` on a development split taken from the labels
only: by default every labeled item that is in none of the trial lists (or just
``development_ids``, when given), capped at ``dev_max``. Everything else labeled is the
candidate pool. The objective is Brier on the model's probabilities, falling back to accuracy
for a model that declares no probability distributions.

A challenger replaces the incumbent only if it improves Brier by at least ``min_brier_gain``
without lowering accuracy (under the accuracy fallback: only if it raises accuracy). Ties,
small gains and incomplete searches all keep the incumbent.
"""
from __future__ import annotations

import math
from collections.abc import MutableMapping, Sequence
from dataclasses import dataclass
from typing import Literal

from .context import FixedExampleList, PrototypeBalanced, RandomBalanced, input_hash
from .models import DecisionModel, DecisionTask, Item, LabeledItem
from .optimizer import Objective, OptimizationResult, TrialSpec, search_context_policies

TRIAL_NAMES = ("incumbent", "hard-swap", "random-control")
_NO_TARGET = "\x00fixed-list-builder"


@dataclass(frozen=True)
class ExampleListRound:
    """The declared trials and the development/candidate split, computed without a model."""

    trials: tuple[tuple[str, FixedExampleList], ...]
    incumbent_source: Literal["given", "prototype-seed"]
    development: tuple[LabeledItem, ...]
    candidates: tuple[LabeledItem, ...]


@dataclass(frozen=True)
class ExampleListImprovement:
    winner: FixedExampleList
    winner_trial: str
    promoted: bool
    reason: str
    scores: dict[str, dict[str, float]]
    round: ExampleListRound
    optimization: OptimizationResult


def _ranked(policy, task: DecisionTask, pool: Sequence[LabeledItem]) -> dict[str, list[LabeledItem]]:
    """A fixed-global policy's full per-label ranking (no target is involved)."""
    depth = min(sum(row.label == label for row in pool) for label in task.labels)
    target = Item(_NO_TARGET, {task.input_field: _NO_TARGET})
    chosen = policy.select(task, target, pool, per_label=depth)
    return {label: [row for row in chosen if row.label == label] for label in task.labels}


def _list_from_ranking(task: DecisionTask, ranking: dict[str, list[LabeledItem]], k: int) -> FixedExampleList:
    return FixedExampleList.from_items(task, [row for label in task.labels for row in ranking[label][:k]],
                                       [ranking[label][k] for label in task.labels])


def _hard_swap(task: DecisionTask, incumbent: FixedExampleList, by_id: dict[str, LabeledItem],
               hard_demo_ids: Sequence[str], prototype: dict[str, list[LabeledItem]]) -> FixedExampleList:
    k = incumbent.per_label
    drop = math.ceil(k / 2)
    examples, reserves = [], []
    for label in task.labels:
        current = [by_id[ref.id] for ref in incumbent.examples if ref.label == label]
        kept = current[:k - drop]                  # the most recently added are at the end
        used = {row.item.id for row in kept}
        fresh = [by_id[i] for i in hard_demo_ids if by_id[i].label == label and i not in used][:drop]
        used |= {row.item.id for row in fresh}
        fresh += [row for row in prototype[label] if row.item.id not in used][:drop - len(fresh)]
        used |= {row.item.id for row in fresh}
        examples += kept + fresh
        reserve = by_id[next(ref.id for ref in incumbent.reserves if ref.label == label)]
        if reserve.item.id in used:
            reserve = next(row for row in prototype[label] if row.item.id not in used)
        reserves.append(reserve)
    return FixedExampleList.from_items(task, examples, reserves)


def plan_example_list_round(
    task: DecisionTask,
    labeled: Sequence[LabeledItem],
    *,
    incumbent: FixedExampleList | None = None,
    hard_demo_ids: Sequence[str] = (),
    per_label: int = 4,
    seed: int = 0,
    development_ids: Sequence[str] | None = None,
    dev_max: int | None = None,
) -> ExampleListRound:
    """The round's trials and splits. Pure and deterministic, so a caller can prefetch."""
    by_id = {row.item.id: row for row in labeled}
    if len(by_id) != len(labeled):
        raise ValueError("labeled items must have unique IDs")
    unknown = [i for i in hard_demo_ids if i not in by_id]
    if unknown:
        raise ValueError(f"{len(unknown)} hard demos are not labeled items")
    for label in task.labels:
        if sum(row.label == label for row in labeled) < per_label + 1:
            raise ValueError(f"label {label!r} needs at least {per_label + 1} labeled items")
    if incumbent is not None and incumbent.per_label != per_label:
        raise ValueError("the incumbent list must have per_label examples per label")
    prototype = _ranked(PrototypeBalanced(), task, labeled)
    source: Literal["given", "prototype-seed"] = "given"
    if incumbent is None:
        incumbent, source = _list_from_ranking(task, prototype, per_label), "prototype-seed"
    for ref in incumbent.examples + incumbent.reserves:
        row = by_id.get(ref.id)
        if row is None or row.label != ref.label or input_hash(task, row.item) != ref.input_hash:
            raise ValueError(f"incumbent example {ref.id!r} is not a matching labeled item")
    trials = (
        ("incumbent", incumbent),
        ("hard-swap", _hard_swap(task, incumbent, by_id, hard_demo_ids, prototype)),
        ("random-control", _list_from_ranking(task, _ranked(RandomBalanced(seed=seed), task, labeled), per_label)),
    )
    in_lists = {i for _, fixed in trials for i in fixed.example_ids + fixed.reserve_ids}
    pool = sorted(development_ids if development_ids is not None else by_id)
    dev_ids = [i for i in pool if i not in in_lists]
    if dev_max is not None:
        dev_ids = dev_ids[:dev_max]
    chosen = set(dev_ids)
    return ExampleListRound(
        trials=trials, incumbent_source=source,
        development=tuple(by_id[i] for i in dev_ids),
        candidates=tuple(row for row in sorted(labeled, key=lambda r: r.item.id) if row.item.id not in chosen),
    )


def _accuracy(result, development: Sequence[LabeledItem]) -> float:
    actual = {row.item.id: row.label for row in development}
    return sum(d.prediction == actual[d.target_id] for d in result.decisions) / len(actual)


async def improve_example_list(
    task: DecisionTask,
    labeled: Sequence[LabeledItem],
    model: DecisionModel,
    *,
    max_model_calls: int,
    incumbent: FixedExampleList | None = None,
    hard_demo_ids: Sequence[str] = (),
    per_label: int = 4,
    seed: int = 0,
    development_ids: Sequence[str] | None = None,
    dev_max: int | None = None,
    min_brier_gain: float = 0.005,
    objective: Objective | None = None,
    checkpoint: MutableMapping[str, dict] | None = None,
    model_fingerprint: str | None = None,
    protected_ids: Sequence[str] = (),
    protected_text_hashes: Sequence[str] = (),
    display_order: str = "canonical",
    presentation_label_order: Sequence[str] | None = None,
) -> ExampleListImprovement:
    """Challenge the incumbent list once; return the list to serve next."""
    protected = set(protected_ids)
    if protected & ({row.item.id for row in labeled} | set(hard_demo_ids)):
        raise ValueError("protected (scoreboard) items may not be labels, examples or hard demos")
    if objective is None:
        objective = "brier" if model.capabilities.supports_probability_distributions else "accuracy"
    if objective not in ("brier", "accuracy"):
        raise ValueError("improve_example_list scores by 'brier' or 'accuracy'")
    plan = plan_example_list_round(task, labeled, incumbent=incumbent, hard_demo_ids=hard_demo_ids,
                                   per_label=per_label, seed=seed, development_ids=development_ids,
                                   dev_max=dev_max)
    optimization = await search_context_policies(
        task, plan.candidates, plan.development, model,
        [TrialSpec(fixed, per_label, name=name) for name, fixed in plan.trials],
        max_model_calls=max_model_calls, objective=objective, checkpoint=checkpoint,
        model_fingerprint=model_fingerprint, protected_ids=protected_ids,
        protected_text_hashes=protected_text_hashes, display_order=display_order,
        presentation_label_order=presentation_label_order,
    )
    lists = dict(plan.trials)
    results = {trial.trial_name: trial for trial in optimization.trials}
    scores = {name: {"accuracy": _accuracy(trial, plan.development), objective: trial.objective}
              for name, trial in results.items() if trial.status == "completed"}
    if "incumbent" not in scores:
        return ExampleListImprovement(lists["incumbent"], "incumbent", False,
                                      "kept the incumbent: its trial was incomplete", scores, plan, optimization)
    base = scores["incumbent"]
    best, best_gain = "incumbent", 0.0
    for name in TRIAL_NAMES[1:]:
        score = scores.get(name)
        if score is None or score["accuracy"] < base["accuracy"]:
            continue
        if objective == "brier":
            gain = base["brier"] - score["brier"]
            eligible = gain >= min_brier_gain
        else:
            gain = score["accuracy"] - base["accuracy"]
            eligible = gain > 0
        if eligible and gain > best_gain:
            best, best_gain = name, gain
    if best == "incumbent":
        reason = "kept the incumbent: no challenger cleared the promotion rule"
        if len(scores) < len(results):
            reason += " (some challenger trials were incomplete)"
    else:
        reason = f"promoted {best}: {objective} gain {best_gain:.6f}"
    return ExampleListImprovement(lists[best], best, best != "incumbent", reason, scores, plan, optimization)

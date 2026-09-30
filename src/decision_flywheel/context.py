"""Deterministic context policies that never inspect a target label."""
from __future__ import annotations

import math
import random
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from heapq import nlargest
from typing import Protocol, Sequence

from .models import DecisionTask, Item, LabeledItem

TOKEN = re.compile(r"[a-z]+")


@lru_cache(maxsize=None)
def _tokens(value: str) -> frozenset[str]:
    return frozenset(TOKEN.findall(value.lower()))


def _text(item: Item, task: DecisionTask) -> str:
    value = item.values.get(task.input_field)
    if not isinstance(value, str):
        raise ValueError(f"{item.id!r} has no string {task.input_field!r}")
    return value


def _normalized_text(item: Item, task: DecisionTask) -> str:
    """Normalize text only for exact target-duplication exclusion."""
    return " ".join(unicodedata.normalize("NFKC", _text(item, task)).casefold().split())


def _validated_candidate_pools(
    task: DecisionTask,
    target: Item,
    candidates: Sequence[LabeledItem],
    *,
    per_label: int,
) -> dict[str, list[LabeledItem]]:
    """Return complete, trusted, target-free pools for every task label.

    Selectors deliberately share this boundary so none can silently use a
    development or scoreboard label, guess a label, or undersample a class.
    """
    if isinstance(per_label, bool) or not isinstance(per_label, int) or per_label < 1:
        raise ValueError("per_label must be a positive integer")

    target_text = _normalized_text(target, task)
    pools = {label: [] for label in task.labels}
    for candidate in candidates:
        if candidate.source != "trusted":
            raise ValueError("context candidates must have trusted label sources")
        canonical_label = task.validate_label(candidate.label)
        if candidate.label != canonical_label:
            raise ValueError("context candidates must use canonical task labels")
        candidate_text = _normalized_text(candidate.item, task)
        if candidate.item.id == target.id or candidate_text == target_text:
            continue
        pools[candidate.label].append(candidate)

    for label, pool in pools.items():
        if len(pool) < per_label:
            raise ValueError(
                f"insufficient trusted candidates for label {label!r}: "
                f"requested {per_label}, found {len(pool)}"
            )
        pool.sort(key=lambda candidate: candidate.item.id)
    return pools


class ContextPolicy(Protocol):
    name: str

    def select(self, task: DecisionTask, target: Item,
               candidates: Sequence[LabeledItem], *, per_label: int) -> list[LabeledItem]:
        ...


@dataclass(frozen=True)
class RandomBalanced:
    seed: int
    name: str = "random-balanced"

    def select(self, task, target, candidates, *, per_label):
        pools = _validated_candidate_pools(task, target, candidates, per_label=per_label)
        return [
            item
            for offset, label in enumerate(task.labels)
            for item in random.Random(self.seed + offset).sample(pools[label], per_label)
        ]


@dataclass(frozen=True)
class PerLabelLexicalRetrieval:
    """Transparent cosine-normalized lexical overlap, balanced across labels."""
    name: str = "per-label-lexical-retrieval"

    def select(self, task, target, candidates, *, per_label):
        pools = _validated_candidate_pools(task, target, candidates, per_label=per_label)
        target_tokens = _tokens(_text(target, task))
        selected = []
        for label in task.labels:
            def score(item):
                value = _tokens(_text(item.item, task))
                return (len(target_tokens & value) / math.sqrt(max(1, len(target_tokens)) * max(1, len(value))), item.item.id)
            selected.extend(nlargest(per_label, pools[label], key=score))
        return selected

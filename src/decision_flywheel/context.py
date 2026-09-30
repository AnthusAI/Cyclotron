"""Deterministic context policies that never inspect a target label."""
from __future__ import annotations

import math
import random
import re
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
        return [item for offset, label in enumerate(task.labels)
                for item in random.Random(self.seed + offset).sample(
                    [item for item in candidates if item.label == label], per_label)]


@dataclass(frozen=True)
class PerLabelLexicalRetrieval:
    """Transparent cosine-normalized lexical overlap, balanced across labels."""
    name: str = "per-label-lexical-retrieval"

    def select(self, task, target, candidates, *, per_label):
        target_tokens = _tokens(_text(target, task))
        selected = []
        for label in task.labels:
            group = [item for item in candidates if item.label == label and item.item.id != target.id]
            def score(item):
                value = _tokens(_text(item.item, task))
                return (len(target_tokens & value) / math.sqrt(max(1, len(target_tokens)) * max(1, len(value))), item.item.id)
            selected.extend(nlargest(per_label, group, key=score))
        return selected

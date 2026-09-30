"""Deterministic context policies that never inspect a target label."""
from __future__ import annotations

import math
import random
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
from typing import Mapping, Protocol, Sequence

from .models import DecisionTask, Item, LabeledItem

TOKEN = re.compile(r"[a-z]+")
POLICY_VERSION = "1"


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
    seen_ids: set[str] = set()
    seen_texts: set[str] = set()
    for candidate in candidates:
        if candidate.source != "trusted":
            raise ValueError("context candidates must have trusted label sources")
        canonical_label = task.validate_label(candidate.label)
        if candidate.label != canonical_label:
            raise ValueError("context candidates must use canonical task labels")
        candidate_text = _normalized_text(candidate.item, task)
        if candidate.item.id in seen_ids:
            raise ValueError("duplicate candidate IDs are not allowed")
        if candidate_text in seen_texts:
            raise ValueError("duplicate candidate normalized text is not allowed")
        seen_ids.add(candidate.item.id)
        seen_texts.add(candidate_text)
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


def validate_selected_context(
    task: DecisionTask,
    target: Item,
    candidates: Sequence[LabeledItem],
    selected: Sequence[LabeledItem],
    *,
    per_label: int,
) -> tuple[LabeledItem, ...]:
    """Verify a custom selector cannot bypass the trusted-context firewall.

    The budget layer calls this after every positive-count selection, so the
    same target exclusion, candidate membership, label balance, and uniqueness
    rules apply to built-in and third-party policies alike.
    """
    pools = _validated_candidate_pools(task, target, candidates, per_label=per_label)
    allowed = {
        (candidate.item.id, _normalized_text(candidate.item, task), candidate.label)
        for pool in pools.values() for candidate in pool
    }
    frozen = tuple(selected)
    seen_ids: set[str] = set()
    counts = {label: 0 for label in task.labels}
    for example in frozen:
        if example.source != "trusted":
            raise ValueError("selected context examples must have trusted label sources")
        canonical_label = task.validate_label(example.label)
        if example.label != canonical_label:
            raise ValueError("selected context examples must use canonical task labels")
        if example.item.id in seen_ids:
            raise ValueError("selected context examples must have unique IDs")
        seen_ids.add(example.item.id)
        key = (example.item.id, _normalized_text(example.item, task), example.label)
        if key not in allowed:
            raise ValueError("selected context examples must match the trusted candidate pool")
        counts[example.label] += 1
    for label, count in counts.items():
        if count != per_label:
            raise ValueError(
                f"selection policy must return exactly {per_label} examples per label; "
                f"{label!r} had {count}"
            )
    return frozen


class ContextPolicy(Protocol):
    name: str
    metadata: "PolicyMetadata"
    fingerprint: str

    def select(self, task: DecisionTask, target: Item,
               candidates: Sequence[LabeledItem], *, per_label: int) -> list[LabeledItem]:
        ...


@dataclass(frozen=True)
class PolicyMetadata:
    """Auditable selector contract, separate from the selected examples."""

    name: str
    version: str
    selection_mode: str
    configuration: Mapping[str, object]

    @property
    def fingerprint(self) -> str:
        """Stable identifier for a policy's behavior and declared parameters."""
        encoded = json.dumps(
            {
                "configuration": self.configuration,
                "name": self.name,
                "selection_mode": self.selection_mode,
                "version": self.version,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _shuffled_rank(pool: Sequence[LabeledItem], *, seed: int, label: str) -> list[LabeledItem]:
    """Return one stable randomized order, so budget increases retain prefixes."""
    ranked = list(pool)
    random.Random(f"{seed}:{label}").shuffle(ranked)
    return ranked


@dataclass(frozen=True)
class RandomBalanced:
    """A fixed-global, balanced context selected from stable per-label ranks.

    The random rank does not inspect target text.  Target ID/text duplicates are
    nevertheless excluded by the shared firewall, so a target that overlaps a
    candidate can remove that candidate and expose the next ranked example.
    """

    seed: int
    name: str = "random-balanced"

    def __post_init__(self) -> None:
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            raise ValueError("seed must be a non-negative integer")

    @property
    def metadata(self) -> PolicyMetadata:
        return PolicyMetadata(self.name, POLICY_VERSION, "fixed-global", {"seed": self.seed})

    @property
    def fingerprint(self) -> str:
        return self.metadata.fingerprint

    def select(self, task, target, candidates, *, per_label):
        pools = _validated_candidate_pools(task, target, candidates, per_label=per_label)
        return [
            item
            for label in task.labels
            for item in _shuffled_rank(pools[label], seed=self.seed, label=label)[:per_label]
        ]


@dataclass(frozen=True)
class PrototypeBalanced:
    """A fixed-global class-centroid proxy extracted from the Emotion study.

    It counts each lexical token once per candidate, scores an item by the
    average within-label frequency of its tokens, then prefers shorter text and
    a descending ID on exact score ties.  It does not rank against target text;
    the shared target-duplicate exclusion may still alter a particular context.
    """

    name: str = "prototype-balanced"

    @property
    def metadata(self) -> PolicyMetadata:
        return PolicyMetadata(self.name, POLICY_VERSION, "fixed-global", {})

    @property
    def fingerprint(self) -> str:
        return self.metadata.fingerprint

    def select(self, task, target, candidates, *, per_label):
        pools = _validated_candidate_pools(task, target, candidates, per_label=per_label)
        selected = []
        for label in task.labels:
            pool = pools[label]
            frequency = Counter(token for candidate in pool for token in _tokens(_text(candidate.item, task)))

            def score(candidate: LabeledItem) -> tuple[float, int, str]:
                value = _text(candidate.item, task)
                tokens = _tokens(value)
                centrality = sum(frequency[token] for token in tokens) / len(tokens) if tokens else 0.0
                return centrality, -len(value), candidate.item.id

            selected.extend(sorted(pool, key=score, reverse=True)[:per_label])
        return selected


@dataclass(frozen=True)
class PerLabelLexicalRetrieval:
    """Target-conditioned cosine-normalized lexical overlap, balanced by label."""
    name: str = "per-label-lexical-retrieval"

    @property
    def metadata(self) -> PolicyMetadata:
        return PolicyMetadata(self.name, POLICY_VERSION, "target-conditioned", {})

    @property
    def fingerprint(self) -> str:
        return self.metadata.fingerprint

    def select(self, task, target, candidates, *, per_label):
        pools = _validated_candidate_pools(task, target, candidates, per_label=per_label)
        target_tokens = _tokens(_text(target, task))
        selected = []
        for label in task.labels:
            def score(item: LabeledItem) -> float:
                value = _tokens(_text(item.item, task))
                return len(target_tokens & value) / math.sqrt(max(1, len(target_tokens)) * max(1, len(value)))

            # Ascending candidate IDs make exact lexical ties auditable and do
            # not depend on the caller's candidate sequence order.
            selected.extend(sorted(pools[label], key=lambda item: (-score(item), item.item.id))[:per_label])
        return selected

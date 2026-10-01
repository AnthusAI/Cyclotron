"""Optional dynamic retrieval: a small retriever interface and its context policy.

Retrieval is off by default. The default product path is a fixed, optimized
example list (``FixedExampleList``); nothing in this module is used unless a
caller builds a ``PerLabelRetrieval`` policy, usually through
``decision_flywheel.retrieval_config.build_retrieval_policy``.

Three small protocols keep backends interchangeable:

- ``Retriever.index(task, pool)`` builds a ``RetrievalIndex`` over trusted labeled
  items only.
- ``RetrievalIndex.top_per_label(target, label, k, exclude_ids)`` returns up to
  ``k`` ``(id, score)`` pairs of one label, best first, ties by ascending ID, and
  never an excluded ID.
- ``VectorStore`` (``upsert`` / ``query``) sits under embedding indexes, so other
  vector stores can plug in without changing callers.

``PerLabelRetrieval`` is the ``ContextPolicy`` that uses an index per item, exactly
like ``PerLabelLexicalRetrieval``: target-conditioned, ``k`` examples per label,
target and protected items excluded, and every result re-checked by the shared
firewall in ``build_context_plan``.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Mapping, Protocol, Sequence, runtime_checkable

from .context import PolicyMetadata, _normalized_text, _validated_candidate_pools, input_hash
from .models import DecisionTask, Item, LabeledItem

RETRIEVAL_POLICY_VERSION = "2"
SCORE_DECIMALS = 9  # scores are rounded before tie-breaking so ordering is stable across backends


def stable_hash(value: object) -> str:
    """SHA-256 of canonical JSON, used for every retrieval fingerprint."""
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def rank(scores: Mapping[str, float], k: int) -> list[tuple[str, float]]:
    """Best ``k`` (id, score) pairs: higher rounded score first, then ascending ID."""
    rounded = [(item_id, round(score, SCORE_DECIMALS)) for item_id, score in scores.items()]
    return sorted(rounded, key=lambda pair: (-pair[1], pair[0]))[:k]


def validated_pool(task: DecisionTask, pool: Sequence[LabeledItem]) -> tuple[LabeledItem, ...]:
    """Check that an index is built from trusted, canonical, unique labeled items only."""
    seen_ids: set[str] = set()
    seen_texts: set[str] = set()
    for row in pool:
        if row.source != "trusted":
            raise ValueError("retrieval pools must have trusted label sources")
        if task.validate_label(row.label) != row.label:
            raise ValueError("retrieval pools must use canonical task labels")
        text = _normalized_text(row.item, task)
        if row.item.id in seen_ids:
            raise ValueError("duplicate candidate IDs are not allowed")
        if text in seen_texts:
            raise ValueError("duplicate candidate normalized text is not allowed")
        seen_ids.add(row.item.id)
        seen_texts.add(text)
    return tuple(sorted(pool, key=lambda row: row.item.id))


def pool_fingerprint(task: DecisionTask, pool: Sequence[LabeledItem]) -> str:
    """Text-free identity of a labeled pool: sorted (id, label, input hash) rows."""
    return stable_hash(sorted((row.item.id, row.label, input_hash(task, row.item)) for row in pool))


@runtime_checkable
class RetrievalIndex(Protocol):
    fingerprint: str

    def top_per_label(self, target: Item, label: str, k: int,
                      exclude_ids: set[str] | frozenset[str]) -> list[tuple[str, float]]:
        ...


@runtime_checkable
class Retriever(Protocol):
    configuration: Mapping[str, object]
    fingerprint: str

    def index(self, task: DecisionTask, pool: Sequence[LabeledItem]) -> RetrievalIndex:
        ...


@dataclass(frozen=True)
class VectorRow:
    """One stored vector. Only the ID, label and text hash travel with it; never text."""

    id: str
    label: str
    input_hash: str
    vector: tuple[float, ...]


@runtime_checkable
class VectorStore(Protocol):
    """Where embedding vectors live. ``approximate`` is recorded in fingerprints."""

    approximate: bool
    configuration: Mapping[str, object]

    def upsert(self, rows: Sequence[VectorRow]) -> None:
        ...

    def query(self, vector: Sequence[float], top_k: int,
              label_filter: str | None = None) -> list[tuple[str, float]]:
        ...


@dataclass(frozen=True)
class PerLabelRetrieval:
    """Target-conditioned, label-balanced context from any ``Retriever``.

    For every label it asks the index for ``per_label`` neighbours of the target,
    excluding the target (by ID and normalized text) and protected IDs or input
    hashes. If an index returns an excluded or unknown ID, selection fails rather
    than silently repairing it. The index is built once per candidate pool and
    reused for every target.
    """

    retriever: Retriever
    per_label: int | None = None
    protected_ids: frozenset[str] = frozenset()
    protected_input_hashes: frozenset[str] = frozenset()
    name: str = "per-label-retrieval"
    _indexes: dict = field(default_factory=dict, init=False, repr=False, compare=False, hash=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "protected_ids", frozenset(self.protected_ids))
        object.__setattr__(self, "protected_input_hashes", frozenset(self.protected_input_hashes))
        if self.per_label is not None and (
                isinstance(self.per_label, bool) or not isinstance(self.per_label, int) or self.per_label < 1):
            raise ValueError("per_label must be a positive integer or None")

    @property
    def metadata(self) -> PolicyMetadata:
        protected = sorted(self.protected_ids) + sorted(f"hash:{value}" for value in self.protected_input_hashes)
        return PolicyMetadata(self.name, RETRIEVAL_POLICY_VERSION, "target-conditioned", {
            "per_label": self.per_label,
            "protected_sha256": stable_hash(protected) if protected else None,
            "retriever": dict(self.retriever.configuration),
        })

    @property
    def fingerprint(self) -> str:
        return self.metadata.fingerprint

    def index_for(self, task: DecisionTask, candidates: Sequence[LabeledItem]) -> RetrievalIndex:
        """Build (or reuse) the index for exactly this candidate pool."""
        key = (task, frozenset((row.item.id, row.label, row.source, _normalized_text(row.item, task))
                               for row in candidates))
        index = self._indexes.get(key)
        if index is None:
            if len(self._indexes) >= 4:
                self._indexes.clear()
            index = self.retriever.index(task, candidates)
            self._indexes[key] = index
        return index

    def select(self, task, target, candidates, *, per_label):
        if self.per_label is not None and per_label != self.per_label:
            raise ValueError(f"this retrieval policy uses {self.per_label} examples per label, not {per_label}")
        pools = _validated_candidate_pools(task, target, candidates, per_label=per_label)
        allowed_by_label = {
            label: {row.item.id: row for row in pool if not self._protected(task, row)}
            for label, pool in pools.items()
        }
        target_text = _normalized_text(target, task)
        exclude = frozenset({target.id} | set(self.protected_ids) | {
            row.item.id for row in candidates
            if _normalized_text(row.item, task) == target_text or self._protected(task, row)
        })
        index = self.index_for(task, candidates)
        selected = []
        for label in task.labels:
            allowed = allowed_by_label[label]
            if len(allowed) < per_label:
                raise ValueError(f"insufficient unprotected candidates for label {label!r}: "
                                 f"requested {per_label}, found {len(allowed)}")
            ids = [item_id for item_id, _ in index.top_per_label(target, label, per_label, exclude)]
            if len(ids) != per_label or len(set(ids)) != per_label:
                raise ValueError(f"retrieval index returned {len(ids)} examples for label {label!r}, "
                                 f"expected {per_label} distinct")
            for item_id in ids:
                if item_id in exclude:
                    raise ValueError("retrieval index returned the target or a protected example")
                if item_id not in allowed:
                    raise ValueError(f"retrieval index returned {item_id!r}, which is not a "
                                     f"trusted {label!r} candidate")
            selected.extend(allowed[item_id] for item_id in ids)
        return selected

    def _protected(self, task: DecisionTask, row: LabeledItem) -> bool:
        return row.item.id in self.protected_ids or (
            bool(self.protected_input_hashes) and input_hash(task, row.item) in self.protected_input_hashes)


@dataclass(frozen=True)
class PurityReport:
    """Neighbour-label purity: a free, label-only proxy for retriever quality."""

    k: int
    items: int
    mean: float
    per_label: Mapping[str, float]


def neighbour_label_purity(task: DecisionTask, pool: Sequence[LabeledItem], retriever: Retriever,
                           *, k: int = 4) -> PurityReport:
    """For each labeled item, the share of its ``k`` nearest other items with the same label.

    Neighbours are global (not label-balanced) and exclude the item itself (the
    pool already forbids duplicate normalized text). It makes no model calls; with a cached or
    fake embedder it costs nothing. ``mean`` is over items, ``per_label`` averages
    within each label (labels with no items are omitted).
    """
    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise ValueError("k must be a positive integer")
    rows = validated_pool(task, pool)
    if len(rows) <= k:
        raise ValueError("the pool needs more than k items to measure neighbour purity")
    index = retriever.index(task, rows)
    labels = {row.item.id: row.label for row in rows}
    scores: dict[str, list[float]] = {label: [] for label in task.labels}
    for row in rows:
        exclude = frozenset({row.item.id})
        # The global top k is always inside the union of each label's top k.
        hits = [hit for label in task.labels for hit in index.top_per_label(row.item, label, k, exclude)]
        nearest = rank(dict(hits), k)
        scores[row.label].append(sum(labels[item_id] == row.label for item_id, _ in nearest) / len(nearest))
    every = [value for values in scores.values() for value in values]
    return PurityReport(k, len(every), sum(every) / len(every),
                        {label: sum(values) / len(values) for label, values in scores.items() if values})

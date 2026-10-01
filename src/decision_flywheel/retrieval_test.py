from dataclasses import dataclass, field

import pytest

from .budget import ContextBudget, build_context_plan
from .context import input_hash
from .embedding_retrieval import EmbeddingRetriever, HashingEmbedder, InMemoryVectorStore
from .lexical_retrieval import LexicalRetriever
from .models import DecisionTask, Item, LabeledItem
from .retrieval import (PerLabelRetrieval, RetrievalIndex, Retriever, VectorStore, neighbour_label_purity,
                        pool_fingerprint)

TASK = DecisionTask("topic", ("a", "b"), "Classify the target.")
POOL = [
    LabeledItem(Item("a-1", {"text": "apple banana fruit"}), "a"),
    LabeledItem(Item("a-2", {"text": "apple pie"}), "a"),
    LabeledItem(Item("a-3", {"text": "banana bread"}), "a"),
    LabeledItem(Item("b-1", {"text": "engine motor car"}), "b"),
    LabeledItem(Item("b-2", {"text": "car apple"}), "b"),
    LabeledItem(Item("b-3", {"text": "motor oil"}), "b"),
]


@dataclass(frozen=True)
class ScriptedIndex:
    answers: dict
    fingerprint: str = "scripted"

    def top_per_label(self, target, label, k, exclude_ids):
        return self.answers[label][:k]


@dataclass(frozen=True)
class ScriptedRetriever:
    answers: dict
    configuration: dict = field(default_factory=lambda: {"kind": "scripted"})
    fingerprint: str = "scripted"
    built: list = field(default_factory=list)

    def index(self, task, pool):
        self.built.append(len(pool))
        return ScriptedIndex(self.answers)


def test_retrieval_picks_the_nearest_examples_of_each_label_and_never_the_target():
    target = POOL[0].item  # a labeled item asking for its own context, leave-one-out

    context = PerLabelRetrieval(LexicalRetriever()).select(TASK, target, POOL, per_label=2)

    assert [row.item.id for row in context] == ["a-2", "a-3", "b-2", "b-1"]


def test_retrieval_skips_a_candidate_whose_text_duplicates_the_target():
    target = Item("new", {"text": "Apple  PIE"})

    context = PerLabelRetrieval(LexicalRetriever()).select(TASK, target, POOL, per_label=1)

    assert "a-2" not in [row.item.id for row in context]


def test_protected_ids_and_text_hashes_are_never_selected():
    protected_hash = input_hash(TASK, POOL[2].item)
    policy = PerLabelRetrieval(LexicalRetriever(), protected_ids={"b-2"}, protected_input_hashes={protected_hash})

    context = policy.select(TASK, Item("t", {"text": "apple banana car"}), POOL, per_label=2)

    assert [row.item.id for row in context] == ["a-1", "a-2", "b-1", "b-3"]
    with pytest.raises(ValueError, match="insufficient unprotected"):
        policy.select(TASK, Item("t", {"text": "apple"}), POOL, per_label=3)


@pytest.mark.parametrize("answer, message", [
    ([("t", 1.0), ("a-1", 0.5)], "target or a protected"),
    ([("b-1", 1.0), ("a-1", 0.5)], "not a trusted 'a' candidate"),
    ([("a-1", 1.0)], "expected 2 distinct"),
    ([("a-1", 1.0), ("a-1", 1.0)], "expected 2 distinct"),
])
def test_retrieval_refuses_an_index_that_breaks_the_contract(answer, message):
    retriever = ScriptedRetriever({"a": answer, "b": [("b-1", 1.0), ("b-2", 0.5)]})
    pool = POOL + [LabeledItem(Item("t", {"text": "the target itself"}), "a")]

    with pytest.raises(ValueError, match=message):
        PerLabelRetrieval(retriever).select(TASK, Item("t", {"text": "the target itself"}), pool, per_label=2)


def test_the_index_is_built_once_per_candidate_pool_and_reused_across_targets():
    retriever = ScriptedRetriever({"a": [("a-1", 1.0)], "b": [("b-1", 1.0)]})
    policy = PerLabelRetrieval(retriever)

    for number in range(3):
        policy.select(TASK, Item(f"t{number}", {"text": f"target {number}"}), POOL, per_label=1)
    policy.select(TASK, Item("t", {"text": "target"}), POOL[:5], per_label=1)

    assert retriever.built == [6, 5]


def test_a_retrieval_policy_plugs_into_build_context_plan_and_records_its_fingerprint():
    policy = PerLabelRetrieval(LexicalRetriever(weighting="bm25"), per_label=1)

    plan = build_context_plan(TASK, Item("t", {"text": "motor car"}), POOL, policy,
                              budget=ContextBudget(per_label=1))

    assert sorted(plan.example_ids) == ["a-1", "b-1"]  # nothing in "a" matches, so ascending ID decides
    assert plan.selection_policy == "per-label-retrieval"
    assert plan.policy_fingerprint == policy.fingerprint
    assert plan.policy_metadata.version == "2"
    assert plan.policy_metadata.selection_mode == "target-conditioned"


def test_the_policy_fingerprint_changes_with_retriever_settings_k_and_protected_items():
    base = PerLabelRetrieval(LexicalRetriever(), per_label=4)
    variants = [
        PerLabelRetrieval(LexicalRetriever(stopwords="none"), per_label=4),
        PerLabelRetrieval(LexicalRetriever(weighting="bm25"), per_label=4),
        PerLabelRetrieval(LexicalRetriever(), per_label=2),
        PerLabelRetrieval(LexicalRetriever(), per_label=4, protected_ids={"x"}),
        PerLabelRetrieval(EmbeddingRetriever.from_embedder(HashingEmbedder(64)), per_label=4),
        PerLabelRetrieval(EmbeddingRetriever.from_embedder(HashingEmbedder(32)), per_label=4),
    ]

    assert base.fingerprint == PerLabelRetrieval(LexicalRetriever(), per_label=4).fingerprint
    assert len({base.fingerprint, *(variant.fingerprint for variant in variants)}) == 7
    with pytest.raises(ValueError, match="uses 4 examples per label"):
        base.select(TASK, Item("t", {"text": "x"}), POOL, per_label=2)


def test_index_fingerprints_combine_the_retriever_and_the_pool_without_text():
    retriever = LexicalRetriever()

    first = retriever.index(TASK, POOL)

    assert first.fingerprint == retriever.index(TASK, list(reversed(POOL))).fingerprint
    assert first.fingerprint != retriever.index(TASK, POOL[:5]).fingerprint
    assert first.fingerprint != LexicalRetriever(weighting="bm25").index(TASK, POOL).fingerprint
    assert "apple" not in pool_fingerprint(TASK, POOL)


def test_retriever_indexes_accept_only_trusted_canonical_unique_items():
    untrusted = [LabeledItem(Item("a-9", {"text": "guess"}), "a", source="model")]
    duplicate = [POOL[0], LabeledItem(Item("a-1", {"text": "other"}), "a")]

    with pytest.raises(ValueError, match="trusted"):
        LexicalRetriever().index(TASK, POOL + untrusted)
    with pytest.raises(ValueError, match="duplicate"):
        EmbeddingRetriever.from_embedder(HashingEmbedder()).index(TASK, duplicate)


def test_built_in_retrievers_indexes_and_stores_satisfy_the_interface():
    for retriever in (LexicalRetriever(), EmbeddingRetriever.from_embedder(HashingEmbedder())):
        assert isinstance(retriever, Retriever)
        assert isinstance(retriever.index(TASK, POOL), RetrievalIndex)
    assert isinstance(InMemoryVectorStore(), VectorStore)


def test_neighbour_purity_is_one_for_separated_labels_and_lower_when_labels_mix():
    clean = [LabeledItem(Item(f"a-{n}", {"text": f"apple fruit {n}"}), "a") for n in range(3)] + \
            [LabeledItem(Item(f"b-{n}", {"text": f"motor engine {n}"}), "b") for n in range(3)]

    assert neighbour_label_purity(TASK, clean, LexicalRetriever(), k=2).mean == 1.0
    report = neighbour_label_purity(TASK, POOL, LexicalRetriever(), k=1)
    # Nearest other item: a-1->a-2 (tie, by ID), a-2->b-2, a-3->a-1, b-1->b-2 (tie), b-2->a-2, b-3->b-1.
    assert (report.k, report.items, report.mean) == (1, 6, 4 / 6)
    assert report.per_label == {"a": 2 / 3, "b": 2 / 3}


def test_neighbour_purity_runs_offline_with_a_fake_embedder_and_checks_k():
    report = neighbour_label_purity(TASK, POOL, EmbeddingRetriever.from_embedder(HashingEmbedder()), k=2)

    assert 0.0 <= report.mean <= 1.0 and report.items == 6
    with pytest.raises(ValueError, match="more than k"):
        neighbour_label_purity(TASK, POOL, LexicalRetriever(), k=6)

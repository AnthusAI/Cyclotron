import pytest

from .context import PerLabelLexicalRetrieval, PrototypeBalanced, RandomBalanced
from .models import DecisionTask, Item, LabeledItem

TASK = DecisionTask("topic", ("a", "b"), "Classify the target.")
CANDIDATES = [LabeledItem(Item(f"{label}-{number}", {"text": f"{label} shared token {number}"}), label) for label in TASK.labels for number in range(4)]


def test_a_random_context_is_balanced_and_repeatable():
    policy = RandomBalanced(seed=7)
    first = policy.select(TASK, Item("target", {"text": "shared"}), CANDIDATES, per_label=2)
    assert first == policy.select(TASK, Item("target", {"text": "shared"}), CANDIDATES, per_label=2)
    assert [item.label for item in first] == ["a", "a", "b", "b"]


def test_a_random_context_uses_nested_per_label_budget_prefixes():
    policy = RandomBalanced(seed=7)
    target = Item("target", {"text": "shared"})

    small = policy.select(TASK, target, CANDIDATES, per_label=1)
    large = policy.select(TASK, target, CANDIDATES, per_label=3)

    for label in TASK.labels:
        small_ids = [item.item.id for item in small if item.label == label]
        large_ids = [item.item.id for item in large if item.label == label]
        assert large_ids[:1] == small_ids


def test_policy_metadata_is_versioned_and_fingerprinted_from_its_configuration():
    random = RandomBalanced(seed=7)
    same_random = RandomBalanced(seed=7)
    other_random = RandomBalanced(seed=8)

    assert random.metadata.name == "random-balanced"
    assert random.metadata.version
    assert random.metadata.selection_mode == "fixed-global"
    assert random.metadata.configuration == {"seed": 7}
    assert random.fingerprint == same_random.fingerprint
    assert random.fingerprint != other_random.fingerprint
    assert PerLabelLexicalRetrieval().metadata.selection_mode == "target-conditioned"


@pytest.mark.parametrize("seed", [-1, True, "7"])
def test_a_random_policy_requires_a_non_negative_integer_seed(seed):
    with pytest.raises(ValueError, match="seed"):
        RandomBalanced(seed=seed)


def test_a_prototype_context_is_balanced_and_does_not_rank_against_the_target():
    candidates = [
        LabeledItem(Item("a-central", {"text": "common common"}), "a"),
        LabeledItem(Item("a-edge", {"text": "common rare"}), "a"),
        LabeledItem(Item("b-central", {"text": "other other"}), "b"),
        LabeledItem(Item("b-edge", {"text": "other rare"}), "b"),
    ]
    policy = PrototypeBalanced()

    first = policy.select(TASK, Item("one", {"text": "unrelated target"}), candidates, per_label=1)
    second = policy.select(TASK, Item("two", {"text": "another target"}), candidates, per_label=1)

    assert [item.item.id for item in first] == ["a-central", "b-central"]
    assert first == second
    assert policy.metadata.selection_mode == "fixed-global"


def test_lexical_retrieval_ranks_stronger_overlap_first_per_label():
    candidates = [
        LabeledItem(Item("a-weak", {"text": "alpha unrelated words"}), "a"),
        LabeledItem(Item("a-strong", {"text": "alpha beta"}), "a"),
        LabeledItem(Item("b-weak", {"text": "beta unrelated words"}), "b"),
        LabeledItem(Item("b-strong", {"text": "alpha beta unique"}), "b"),
    ]

    context = PerLabelLexicalRetrieval().select(
        TASK, Item("target", {"text": "alpha beta gamma"}), candidates, per_label=1
    )

    assert [item.item.id for item in context] == ["a-strong", "b-strong"]


def test_lexical_retrieval_breaks_equal_scores_by_stable_candidate_id():
    candidates = [
        LabeledItem(Item("a-z", {"text": "alpha apple"}), "a"),
        LabeledItem(Item("a-a", {"text": "alpha apricot"}), "a"),
        LabeledItem(Item("b-z", {"text": "beta berry"}), "b"),
        LabeledItem(Item("b-a", {"text": "beta banana"}), "b"),
    ]

    context = PerLabelLexicalRetrieval().select(
        TASK, Item("target", {"text": "alpha beta"}), candidates, per_label=2
    )

    assert [item.item.id for item in context] == ["a-a", "a-z", "b-a", "b-z"]


@pytest.mark.parametrize("policy", [RandomBalanced(seed=7), PrototypeBalanced(), PerLabelLexicalRetrieval()])
def test_a_policy_never_selects_the_target_itself(policy):
    target = CANDIDATES[0].item
    context = policy.select(TASK, target, CANDIDATES, per_label=1)
    assert target.id not in {item.item.id for item in context}


@pytest.mark.parametrize("policy", [RandomBalanced(seed=7), PrototypeBalanced(), PerLabelLexicalRetrieval()])
def test_a_target_text_duplicate_under_another_id_is_excluded(policy):
    target = Item("target", {"text": "  Shared\nTarget  "})
    duplicate = LabeledItem(Item("duplicate", {"text": "shared target"}), "a")
    candidates = [duplicate, *CANDIDATES]

    context = policy.select(TASK, target, candidates, per_label=1)

    assert duplicate not in context
    assert [item.label for item in context] == ["a", "b"]


@pytest.mark.parametrize("policy", [RandomBalanced(seed=7), PrototypeBalanced(), PerLabelLexicalRetrieval()])
@pytest.mark.parametrize("source", ["development", "scoreboard", "untrusted"])
def test_a_non_trusted_candidate_source_is_rejected(policy, source):
    candidates = [LabeledItem(CANDIDATES[0].item, "a", source=source), *CANDIDATES[1:]]

    with pytest.raises(ValueError, match="trusted"):
        policy.select(TASK, Item("target", {"text": "shared"}), candidates, per_label=1)


@pytest.mark.parametrize("policy", [RandomBalanced(seed=7), PrototypeBalanced(), PerLabelLexicalRetrieval()])
def test_a_candidate_label_outside_the_task_is_rejected(policy):
    candidates = [LabeledItem(CANDIDATES[0].item, "not-a-task-label"), *CANDIDATES[1:]]

    with pytest.raises(ValueError, match="not one of"):
        policy.select(TASK, Item("target", {"text": "shared"}), candidates, per_label=1)


@pytest.mark.parametrize("policy", [RandomBalanced(seed=7), PrototypeBalanced(), PerLabelLexicalRetrieval()])
def test_an_equivalent_but_noncanonical_candidate_label_is_rejected(policy):
    candidates = [LabeledItem(CANDIDATES[0].item, "A."), *CANDIDATES[1:]]

    with pytest.raises(ValueError, match="canonical"):
        policy.select(TASK, Item("target", {"text": "shared"}), candidates, per_label=1)


@pytest.mark.parametrize("policy", [RandomBalanced(seed=7), PrototypeBalanced(), PerLabelLexicalRetrieval()])
def test_a_unicode_normalized_target_text_duplicate_under_another_id_is_excluded(policy):
    target = Item("target", {"text": "\uff23\uff41\uff46\uff45\u0301"})
    duplicate = LabeledItem(Item("duplicate", {"text": "cafe\u0301"}), "a")

    context = policy.select(TASK, target, [duplicate, *CANDIDATES], per_label=1)

    assert duplicate not in context


@pytest.mark.parametrize("policy", [RandomBalanced(seed=7), PrototypeBalanced(), PerLabelLexicalRetrieval()])
def test_a_target_without_string_input_text_is_rejected(policy):
    with pytest.raises(ValueError, match="no string 'text'"):
        policy.select(TASK, Item("target", {"text": None}), CANDIDATES, per_label=1)


@pytest.mark.parametrize("policy", [RandomBalanced(seed=7), PrototypeBalanced(), PerLabelLexicalRetrieval()])
def test_a_candidate_without_string_input_text_is_rejected(policy):
    candidates = [LabeledItem(Item("bad", {"text": None}), "a"), *CANDIDATES[1:]]

    with pytest.raises(ValueError, match="no string 'text'"):
        policy.select(TASK, Item("target", {"text": "shared"}), candidates, per_label=1)


@pytest.mark.parametrize("policy", [RandomBalanced(seed=7), PrototypeBalanced(), PerLabelLexicalRetrieval()])
@pytest.mark.parametrize(
    "duplicate",
    [
        LabeledItem(Item("a-0", {"text": "different text"}), "a"),
        LabeledItem(Item("different-id", {"text": " A shared TOKEN 0 "}), "a"),
    ],
)
def test_duplicate_candidate_ids_or_normalized_text_are_rejected_before_pool_counting(policy, duplicate):
    with pytest.raises(ValueError, match="duplicate candidate"):
        policy.select(TASK, Item("target", {"text": "target"}), [duplicate, *CANDIDATES], per_label=1)


@pytest.mark.parametrize("policy", [RandomBalanced(seed=7), PrototypeBalanced(), PerLabelLexicalRetrieval()])
def test_context_selection_does_not_depend_on_candidate_input_order(policy):
    target = Item("target", {"text": "shared"})

    forward = policy.select(TASK, target, CANDIDATES, per_label=2)
    backward = policy.select(TASK, target, list(reversed(CANDIDATES)), per_label=2)

    assert [item.item.id for item in forward] == [item.item.id for item in backward]


@pytest.mark.parametrize("policy", [RandomBalanced(seed=7), PrototypeBalanced(), PerLabelLexicalRetrieval()])
@pytest.mark.parametrize("per_label", [0, -1, True, 1.5])
def test_an_invalid_context_budget_is_rejected(policy, per_label):
    with pytest.raises(ValueError, match="per_label"):
        policy.select(TASK, Item("target", {"text": "shared"}), CANDIDATES, per_label=per_label)


@pytest.mark.parametrize("policy", [RandomBalanced(seed=7), PerLabelLexicalRetrieval()])
def test_an_insufficient_label_pool_is_rejected_after_target_exclusion(policy):
    target = Item("target", {"text": "duplicate me"})
    candidates = [
        LabeledItem(Item("same-text", {"text": "duplicate me"}), "a"),
        LabeledItem(Item("b-1", {"text": "other"}), "b"),
    ]

    with pytest.raises(ValueError, match="label 'a'.*requested 1.*found 0"):
        policy.select(TASK, target, candidates, per_label=1)


# ---- FixedExampleList ---------------------------------------------------------------------

from .context import ExampleRef, FixedExampleList  # noqa: E402

LIST_POOL = [LabeledItem(Item(f"{label}-{number}", {"text": f"{label} example number {number}"}), label)
             for label in TASK.labels for number in range(6)]
BY_ID = {row.item.id: row for row in LIST_POOL}


def _fixed(primary=("a-0", "a-1", "b-0", "b-1"), reserves=("a-5", "b-5")):
    return FixedExampleList.from_items(TASK, [BY_ID[i] for i in primary], [BY_ID[i] for i in reserves])


def test_a_fixed_list_returns_its_own_examples_in_order_for_an_unrelated_target():
    chosen = _fixed().select(TASK, Item("target", {"text": "unrelated"}), LIST_POOL, per_label=2)
    assert [row.item.id for row in chosen] == ["a-0", "a-1", "b-0", "b-1"]


def test_a_fixed_list_swaps_in_its_reserve_when_the_target_is_one_of_its_examples():
    chosen = _fixed().select(TASK, BY_ID["a-1"].item, LIST_POOL, per_label=2)
    assert [row.item.id for row in chosen] == ["a-0", "a-5", "b-0", "b-1"]


def test_a_fixed_list_swaps_in_its_reserve_for_a_target_text_duplicate_under_another_id():
    duplicate = Item("other-id", {"text": "  B EXAMPLE number 0 "})
    chosen = _fixed().select(TASK, duplicate, LIST_POOL, per_label=2)
    assert [row.item.id for row in chosen] == ["a-0", "a-1", "b-5", "b-1"]


def test_a_fixed_list_passes_the_shared_target_firewall_and_stays_label_balanced():
    from .budget import ContextBudget, build_context_plan

    for target in [row.item for row in LIST_POOL] + [Item("new", {"text": "new"})]:
        plan = build_context_plan(TASK, target, LIST_POOL, _fixed(), budget=ContextBudget(per_label=2))
        assert target.id not in plan.example_ids
        assert [row.label for row in plan.examples].count("a") == 2
        assert [row.label for row in plan.examples].count("b") == 2


def test_a_fixed_list_refuses_an_example_missing_from_or_altered_in_the_candidates():
    without = [row for row in LIST_POOL if row.item.id != "b-0"]
    with pytest.raises(ValueError):
        _fixed().select(TASK, Item("t", {"text": "t"}), without, per_label=2)
    altered = [row if row.item.id != "a-0" else LabeledItem(Item("a-0", {"text": "edited"}), "a") for row in LIST_POOL]
    with pytest.raises(ValueError):
        _fixed().select(TASK, Item("t", {"text": "t"}), altered, per_label=2)


def test_a_fixed_list_refuses_a_budget_that_differs_from_its_size_per_label():
    with pytest.raises(ValueError):
        _fixed().select(TASK, Item("t", {"text": "t"}), LIST_POOL, per_label=1)


@pytest.mark.parametrize("primary,reserves", [
    (("a-0", "a-1", "b-0"), ("a-5", "b-5")),          # unbalanced
    (("a-0", "a-1", "b-0", "b-1"), ("a-5",)),         # a label without a reserve
    (("a-0", "a-1", "b-0", "b-1"), ("a-5", "a-4")),   # two reserves for one label
    (("a-0", "a-0", "b-0", "b-1"), ("a-5", "b-5")),   # duplicate example
    (("a-0", "a-1", "b-0", "b-1"), ("a-1", "b-5")),   # reserve is also an example
])
def test_a_fixed_list_must_be_balanced_with_one_distinct_reserve_per_label(primary, reserves):
    with pytest.raises(ValueError):
        _fixed(primary, reserves)


def test_a_fixed_list_fingerprint_is_deterministic_text_free_and_content_bound():
    first, second = _fixed(), _fixed()
    assert first.fingerprint == second.fingerprint
    assert first.metadata.name == "fixed-example-list" and first.metadata.selection_mode == "fixed-global"
    assert "example number" not in str(first.metadata.configuration)
    assert _fixed(("a-1", "a-0", "b-0", "b-1")).fingerprint != first.fingerprint   # order matters
    assert _fixed(reserves=("a-4", "b-5")).fingerprint != first.fingerprint
    edited = LabeledItem(Item("a-0", {"text": "a different text"}), "a")
    other = FixedExampleList.from_items(TASK, [edited, BY_ID["a-1"], BY_ID["b-0"], BY_ID["b-1"]],
                                        [BY_ID["a-5"], BY_ID["b-5"]])
    assert other.fingerprint != first.fingerprint
    assert FixedExampleList.from_configuration(first.metadata.configuration) == first
    assert first.example_ids == ("a-0", "a-1", "b-0", "b-1") and first.reserve_ids == ("a-5", "b-5")
    assert isinstance(first.examples[0], ExampleRef)

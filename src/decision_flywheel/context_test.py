import pytest

from .context import PerLabelLexicalRetrieval, RandomBalanced
from .models import DecisionTask, Item, LabeledItem

TASK = DecisionTask("topic", ("a", "b"), "Classify the target.")
CANDIDATES = [LabeledItem(Item(f"{label}-{number}", {"text": f"{label} shared token {number}"}), label) for label in TASK.labels for number in range(4)]


def test_a_random_context_is_balanced_and_repeatable():
    policy = RandomBalanced(seed=7)
    first = policy.select(TASK, Item("target", {"text": "shared"}), CANDIDATES, per_label=2)
    assert first == policy.select(TASK, Item("target", {"text": "shared"}), CANDIDATES, per_label=2)
    assert [item.label for item in first] == ["a", "a", "b", "b"]


@pytest.mark.parametrize("policy", [RandomBalanced(seed=7), PerLabelLexicalRetrieval()])
def test_a_policy_never_selects_the_target_itself(policy):
    target = CANDIDATES[0].item
    context = policy.select(TASK, target, CANDIDATES, per_label=1)
    assert target.id not in {item.item.id for item in context}


@pytest.mark.parametrize("policy", [RandomBalanced(seed=7), PerLabelLexicalRetrieval()])
def test_a_target_text_duplicate_under_another_id_is_excluded(policy):
    target = Item("target", {"text": "  Shared\nTarget  "})
    duplicate = LabeledItem(Item("duplicate", {"text": "shared target"}), "a")
    candidates = [duplicate, *CANDIDATES]

    context = policy.select(TASK, target, candidates, per_label=1)

    assert duplicate not in context
    assert [item.label for item in context] == ["a", "b"]


@pytest.mark.parametrize("policy", [RandomBalanced(seed=7), PerLabelLexicalRetrieval()])
@pytest.mark.parametrize("source", ["development", "scoreboard", "untrusted"])
def test_a_non_trusted_candidate_source_is_rejected(policy, source):
    candidates = [LabeledItem(CANDIDATES[0].item, "a", source=source), *CANDIDATES[1:]]

    with pytest.raises(ValueError, match="trusted"):
        policy.select(TASK, Item("target", {"text": "shared"}), candidates, per_label=1)


@pytest.mark.parametrize("policy", [RandomBalanced(seed=7), PerLabelLexicalRetrieval()])
def test_a_candidate_label_outside_the_task_is_rejected(policy):
    candidates = [LabeledItem(CANDIDATES[0].item, "not-a-task-label"), *CANDIDATES[1:]]

    with pytest.raises(ValueError, match="not one of"):
        policy.select(TASK, Item("target", {"text": "shared"}), candidates, per_label=1)


@pytest.mark.parametrize("policy", [RandomBalanced(seed=7), PerLabelLexicalRetrieval()])
def test_an_equivalent_but_noncanonical_candidate_label_is_rejected(policy):
    candidates = [LabeledItem(CANDIDATES[0].item, "A."), *CANDIDATES[1:]]

    with pytest.raises(ValueError, match="canonical"):
        policy.select(TASK, Item("target", {"text": "shared"}), candidates, per_label=1)


@pytest.mark.parametrize("policy", [RandomBalanced(seed=7), PerLabelLexicalRetrieval()])
def test_a_unicode_normalized_target_text_duplicate_under_another_id_is_excluded(policy):
    target = Item("target", {"text": "\uff23\uff41\uff46\uff45\u0301"})
    duplicate = LabeledItem(Item("duplicate", {"text": "cafe\u0301"}), "a")

    context = policy.select(TASK, target, [duplicate, *CANDIDATES], per_label=1)

    assert duplicate not in context


@pytest.mark.parametrize("policy", [RandomBalanced(seed=7), PerLabelLexicalRetrieval()])
def test_a_target_without_string_input_text_is_rejected(policy):
    with pytest.raises(ValueError, match="no string 'text'"):
        policy.select(TASK, Item("target", {"text": None}), CANDIDATES, per_label=1)


@pytest.mark.parametrize("policy", [RandomBalanced(seed=7), PerLabelLexicalRetrieval()])
def test_a_candidate_without_string_input_text_is_rejected(policy):
    candidates = [LabeledItem(Item("bad", {"text": None}), "a"), *CANDIDATES[1:]]

    with pytest.raises(ValueError, match="no string 'text'"):
        policy.select(TASK, Item("target", {"text": "shared"}), candidates, per_label=1)


@pytest.mark.parametrize("policy", [RandomBalanced(seed=7), PerLabelLexicalRetrieval()])
def test_context_selection_does_not_depend_on_candidate_input_order(policy):
    target = Item("target", {"text": "shared"})

    forward = policy.select(TASK, target, CANDIDATES, per_label=2)
    backward = policy.select(TASK, target, list(reversed(CANDIDATES)), per_label=2)

    assert [item.item.id for item in forward] == [item.item.id for item in backward]


@pytest.mark.parametrize("policy", [RandomBalanced(seed=7), PerLabelLexicalRetrieval()])
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

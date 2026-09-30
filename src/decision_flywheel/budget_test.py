import json

import pytest

from .budget import (
    BudgetExceededError,
    ContextBudget,
    DeterministicTokenCounter,
    build_context_ladder,
    build_context_plan,
)
from .context import PolicyMetadata, RandomBalanced
from .models import DecisionTask, Item, LabeledItem


TASK = DecisionTask("topic", ("yes", "no"), "Choose one label.")
TARGET = Item("target", {"text": "what is this?"})
CANDIDATES = [
    LabeledItem(Item(f"{label}-{number}", {"text": f"{label} example {number}"}), label)
    for label in TASK.labels
    for number in range(3)
]


class FixedCounter:
    identity = "test-fixed-counter-v1"

    def __init__(self, tokens):
        self.tokens = tokens
        self.requests = []

    def count(self, serialized_request):
        self.requests.append(serialized_request)
        return self.tokens


def test_a_zero_shot_plan_does_not_call_a_selector_that_requires_a_positive_count():
    class NeverSelect:
        name = "never"
        metadata = PolicyMetadata("never", "test", "fixed-global", {})
        fingerprint = metadata.fingerprint

        def select(self, *args, **kwargs):
            raise AssertionError("zero-shot context must not invoke selection")

    plan = build_context_plan(
        TASK, TARGET, CANDIDATES, NeverSelect(), budget=ContextBudget(per_label=0)
    )

    assert plan.examples == ()
    assert plan.example_ids == ()
    assert plan.budget.per_label == 0


def test_a_zero_shot_plan_still_rejects_a_target_without_input_text():
    class NeverSelect:
        name = "never"
        metadata = PolicyMetadata("never", "test", "fixed-global", {})
        fingerprint = metadata.fingerprint

        def select(self, *args, **kwargs):
            raise AssertionError("zero-shot context must not invoke selection")

    with pytest.raises(ValueError, match="target lacks string 'text'"):
        build_context_plan(
            TASK, Item("target", {"text": None}), CANDIDATES, NeverSelect(),
            budget=ContextBudget(per_label=0),
        )


class CustomPolicy:
    name = "custom"
    metadata = PolicyMetadata("custom", "test", "fixed-global", {})
    fingerprint = metadata.fingerprint

    def __init__(self, selected):
        self.selected = selected

    def select(self, *args, **kwargs):
        return self.selected


@pytest.mark.parametrize(
    ("selected", "error"),
    [
        ([CANDIDATES[0], CANDIDATES[1]], "exactly 1 examples per label"),
        ([CANDIDATES[0], CANDIDATES[3], CANDIDATES[0]], "unique IDs"),
        ([LabeledItem(Item("yes-0", {"text": "forged"}), "yes"), CANDIDATES[3]], "candidate pool"),
        ([LabeledItem(CANDIDATES[0].item, "yes", source="untrusted"), CANDIDATES[3]], "trusted"),
    ],
)
def test_a_custom_policy_cannot_bypass_context_membership_or_balance(selected, error):
    with pytest.raises(ValueError, match=error):
        build_context_plan(
            TASK, TARGET, CANDIDATES, CustomPolicy(selected), budget=ContextBudget(per_label=1)
        )


def test_a_custom_policy_cannot_return_a_target_candidate_excluded_from_context():
    target = Item("target", {"text": "target text"})
    candidates = [
        LabeledItem(Item("target", {"text": "target text"}), "yes"),
        LabeledItem(Item("no-1", {"text": "no example"}), "no"),
    ]

    with pytest.raises(ValueError, match="insufficient trusted candidates"):
        build_context_plan(
            TASK, target, candidates, CustomPolicy(candidates), budget=ContextBudget(per_label=1)
        )


def test_a_plan_records_an_injected_counter_and_counts_the_complete_serialized_request():
    counter = FixedCounter(17)

    plan = build_context_plan(
        TASK, TARGET, CANDIDATES, RandomBalanced(seed=3),
        budget=ContextBudget(per_label=1, max_tokens=20), token_counter=counter,
    )

    assert plan.token_accounting.estimated_request_tokens == 17
    assert plan.token_accounting.counter_identity == "test-fixed-counter-v1"
    assert plan.token_accounting.provider_usage is False
    assert plan.policy_metadata == RandomBalanced(seed=3).metadata
    assert plan.policy_fingerprint == RandomBalanced(seed=3).fingerprint
    wire = json.loads(counter.requests[0])
    assert wire == {
        "state": {
            "labeled_examples": [
                {"text": item.item.values["text"], "label": item.label}
                for item in plan.examples
            ],
            "target": {"text": "what is this?"},
        },
        "questions": {
            "topic": {
                "type": "choice",
                "instructions": "Choose one label.",
                "criteria": {"yes": None, "no": None},
            }
        },
    }


def test_an_oversized_request_is_rejected_instead_of_silently_truncated():
    with pytest.raises(BudgetExceededError, match="max_tokens=16.*estimated_request_tokens=17"):
        build_context_plan(
            TASK, TARGET, CANDIDATES, RandomBalanced(seed=3),
            budget=ContextBudget(per_label=1, max_tokens=16), token_counter=FixedCounter(17),
        )


def test_a_provider_limit_is_checked_against_the_full_request_not_only_examples():
    with pytest.raises(BudgetExceededError, match="provider_token_limit=16.*estimated_request_tokens=17"):
        build_context_plan(
            TASK, TARGET, CANDIDATES, RandomBalanced(seed=3),
            budget=ContextBudget(per_label=0, provider_token_limit=16), token_counter=FixedCounter(17),
        )


def test_display_order_changes_presentation_but_not_membership_or_task_wording():
    policy = RandomBalanced(seed=3)
    canonical = build_context_plan(
        TASK, TARGET, CANDIDATES, policy, budget=ContextBudget(per_label=2),
        display_order="canonical", order_seed=1,
    )
    shuffled = build_context_plan(
        TASK, TARGET, CANDIDATES, policy, budget=ContextBudget(per_label=2),
        display_order="shuffled", order_seed=99,
    )

    assert set(canonical.example_ids) == set(shuffled.example_ids)
    assert canonical.task_fingerprint == shuffled.task_fingerprint
    assert canonical.examples != shuffled.examples
    assert shuffled.order_seed == 99


def test_display_order_has_explicit_canonical_interleaved_reversed_and_seeded_shuffle_modes():
    policy = RandomBalanced(seed=3)
    plans = {
        order: build_context_plan(
            TASK, TARGET, CANDIDATES, policy, budget=ContextBudget(per_label=2),
            display_order=order, order_seed=8,
        )
        for order in ("canonical", "interleaved", "reversed", "shuffled")
    }

    assert [item.label for item in plans["canonical"].examples] == ["no", "no", "yes", "yes"]
    assert [item.label for item in plans["interleaved"].examples] == ["no", "yes", "no", "yes"]
    assert plans["reversed"].example_ids == tuple(reversed(plans["canonical"].example_ids))
    assert plans["shuffled"].example_ids == build_context_plan(
        TASK, TARGET, CANDIDATES, policy, budget=ContextBudget(per_label=2),
        display_order="shuffled", order_seed=8,
    ).example_ids


def test_presentation_is_independent_of_the_task_label_option_order():
    reversed_options = DecisionTask("topic", ("no", "yes"), "Choose one label.")

    first = build_context_plan(
        TASK, TARGET, CANDIDATES, RandomBalanced(seed=3),
        budget=ContextBudget(per_label=2), display_order="shuffled", order_seed=7,
    )
    second = build_context_plan(
        reversed_options, TARGET, CANDIDATES, RandomBalanced(seed=3),
        budget=ContextBudget(per_label=2), display_order="shuffled", order_seed=7,
    )

    assert first.example_ids == second.example_ids


def test_an_explicit_presentation_label_order_preserves_historical_display_order():
    reversed_options = DecisionTask("topic", ("no", "yes"), "Choose one label.")

    plan = build_context_plan(
        reversed_options, TARGET, CANDIDATES, RandomBalanced(seed=3),
        budget=ContextBudget(per_label=1), presentation_label_order=("yes", "no"),
    )

    assert [example.label for example in plan.examples] == ["yes", "no"]
    assert plan.presentation_label_order == ("yes", "no")


def test_a_presentation_label_order_must_be_an_exact_task_label_permutation():
    with pytest.raises(ValueError, match="exact permutation"):
        build_context_plan(
            TASK, TARGET, CANDIDATES, RandomBalanced(seed=3),
            presentation_label_order=("yes",),
        )


def test_request_fingerprint_preserves_task_name_and_choice_option_order():
    counter = FixedCounter(17)
    original = build_context_plan(
        TASK, TARGET, CANDIDATES, RandomBalanced(seed=3),
        budget=ContextBudget(per_label=1), token_counter=counter,
    )
    renamed = build_context_plan(
        DecisionTask("renamed", ("yes", "no"), "Choose one label."), TARGET, CANDIDATES,
        RandomBalanced(seed=3), budget=ContextBudget(per_label=1), token_counter=counter,
    )
    reversed_options = build_context_plan(
        DecisionTask("topic", ("no", "yes"), "Choose one label."), TARGET, CANDIDATES,
        RandomBalanced(seed=3), budget=ContextBudget(per_label=1), token_counter=counter,
    )

    assert original.token_accounting.serialized_request_fingerprint != renamed.token_accounting.serialized_request_fingerprint
    assert original.token_accounting.serialized_request_fingerprint != reversed_options.token_accounting.serialized_request_fingerprint
    assert '"criteria":{"yes":null,"no":null}' in counter.requests[0]
    assert '"criteria":{"no":null,"yes":null}' in counter.requests[2]


def test_count_ladder_selects_once_at_its_maximum_then_uses_per_label_prefixes():
    class CountingPolicy:
        name = "counting"
        metadata = PolicyMetadata("counting", "test", "fixed-global", {})
        fingerprint = metadata.fingerprint

        def __init__(self):
            self.counts = []

        def select(self, task, target, candidates, *, per_label):
            self.counts.append(per_label)
            return [
                item for label in task.labels for item in candidates
                if item.label == label
            ][: per_label] + [
                item for label in task.labels[1:] for item in candidates
                if item.label == label
            ][: per_label]

    policy = CountingPolicy()
    plans = build_context_ladder(
        TASK, TARGET, CANDIDATES, policy, per_label_counts=(0, 1, 2),
    )

    assert policy.counts == [2]
    assert plans[0].example_ids == ()
    assert set(plans[1].example_ids).issubset(plans[2].example_ids)


def test_the_builtin_counter_is_deterministic_and_marks_its_result_as_an_estimate():
    counter = DeterministicTokenCounter()
    first = build_context_plan(TASK, TARGET, CANDIDATES, RandomBalanced(seed=3),
                               budget=ContextBudget(per_label=1), token_counter=counter)
    second = build_context_plan(TASK, TARGET, CANDIDATES, RandomBalanced(seed=3),
                                budget=ContextBudget(per_label=1), token_counter=counter)

    assert first.token_accounting == second.token_accounting
    assert first.token_accounting.provider_usage is False

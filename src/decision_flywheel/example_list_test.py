import asyncio

import pytest

from .context import FixedExampleList
from .example_list import example_list_from_policy, improve_example_list, plan_example_list_round
from .models import DecisionResult, DecisionTask, Item, LabeledItem, ModelCapabilities

TASK = DecisionTask("tone", ("pos", "neg"), "Choose the tone.")
LABELED = [LabeledItem(Item(f"{label}-{n:02d}", {"text": f"{label} sample {n} with words"}), label)
           for label in TASK.labels for n in range(14)]
BY_ID = {row.item.id: row for row in LABELED}
HELPFUL = {"pos-11", "pos-12", "neg-11", "neg-12"}


class FakeModel:
    """Right every time; more confident the more helpful examples it is shown."""

    name = "fake"
    fingerprint = "fake-model-v1"
    capabilities = ModelCapabilities(supports_probability_distributions=True)

    def __init__(self, helpful=frozenset(), *, bonus=0.3):
        self.helpful, self.bonus, self.calls = set(helpful), bonus, []

    async def decide(self, task, target, context):
        self.calls.append((target.id, tuple(row.item.id for row in context)))
        right = target.id.split("-")[0]
        wrong = "neg" if right == "pos" else "pos"
        share = sum(row.item.id in self.helpful for row in context) / max(len(context), 1)
        p = 0.6 + self.bonus * share
        return DecisionResult(right, probabilities={right: p, wrong: 1 - p})


def _run(model, **kwargs):
    return asyncio.run(improve_example_list(TASK, LABELED, model, max_model_calls=500, **kwargs))


def test_a_round_declares_at_most_three_trials_and_scores_only_items_outside_every_list():
    plan = plan_example_list_round(TASK, LABELED, hard_demo_ids=sorted(HELPFUL), seed=1)
    names = [name for name, _ in plan.trials]
    assert names == ["incumbent", "hard-swap", "random-control"]
    assert plan.incumbent_source == "prototype-seed"
    in_lists = {i for _, fixed in plan.trials for i in fixed.example_ids + fixed.reserve_ids}
    dev_ids = {row.item.id for row in plan.development}
    candidate_ids = {row.item.id for row in plan.candidates}
    assert not dev_ids & in_lists and in_lists <= candidate_ids
    assert dev_ids | candidate_ids == set(BY_ID) and not dev_ids & candidate_ids


def test_the_hard_swap_replaces_the_most_recently_added_half_of_each_label_with_hard_demos():
    incumbent = FixedExampleList.from_items(
        TASK, [BY_ID[i] for i in ("pos-00", "pos-01", "pos-02", "pos-03", "neg-00", "neg-01", "neg-02", "neg-03")],
        [BY_ID["pos-13"], BY_ID["neg-13"]])
    plan = plan_example_list_round(TASK, LABELED, incumbent=incumbent,
                                   hard_demo_ids=["neg-12", "pos-11", "neg-11", "pos-12"])
    trials = dict(plan.trials)
    assert trials["incumbent"] == incumbent and plan.incumbent_source == "given"
    assert trials["hard-swap"].example_ids == ("pos-00", "pos-01", "pos-11", "pos-12",
                                               "neg-00", "neg-01", "neg-12", "neg-11")
    assert trials["hard-swap"].reserve_ids == ("pos-13", "neg-13")


def test_too_few_hard_demos_are_topped_up_from_the_prototype_ranking():
    plan = plan_example_list_round(TASK, LABELED, hard_demo_ids=["pos-11"])
    hard = dict(plan.trials)["hard-swap"]
    assert "pos-11" in hard.example_ids
    assert len(hard.example_ids) == 8 and len(set(hard.example_ids)) == 8


def test_the_incumbent_keeps_a_tie():
    result = _run(FakeModel(), hard_demo_ids=sorted(HELPFUL), seed=1)
    assert result.winner_trial == "incumbent" and result.promoted is False
    assert len({round(score["brier"], 12) for score in result.scores.values()}) == 1


def test_swapping_in_hard_demos_wins_when_they_help():
    result = _run(FakeModel(HELPFUL), hard_demo_ids=sorted(HELPFUL), seed=1)
    assert result.winner_trial == "hard-swap" and result.promoted is True
    assert set(result.winner.example_ids) >= HELPFUL
    assert result.scores["hard-swap"]["brier"] < result.scores["incumbent"]["brier"] - 0.005


def test_a_gain_smaller_than_the_minimum_keeps_the_incumbent():
    result = _run(FakeModel(HELPFUL, bonus=0.01), hard_demo_ids=sorted(HELPFUL), seed=1)
    assert result.scores["hard-swap"]["brier"] < result.scores["incumbent"]["brier"]
    assert result.winner_trial == "incumbent" and result.promoted is False


def test_protected_ids_are_refused_in_the_labels_and_in_the_hard_demos():
    with pytest.raises(ValueError):
        _run(FakeModel(), protected_ids=["pos-03"])
    with pytest.raises(ValueError):
        _run(FakeModel(), hard_demo_ids=["held-out-1"])


def test_the_driver_is_deterministic():
    first_model, second_model = FakeModel(HELPFUL), FakeModel(HELPFUL)
    first = _run(first_model, hard_demo_ids=sorted(HELPFUL), seed=3)
    second = _run(second_model, hard_demo_ids=sorted(HELPFUL), seed=3)
    assert first.winner.fingerprint == second.winner.fingerprint
    assert first.scores == second.scores and first_model.calls == second_model.calls


def test_accuracy_is_the_fallback_objective_for_a_model_without_probabilities():
    class LabelOnly(FakeModel):
        capabilities = ModelCapabilities()

    result = _run(LabelOnly(), hard_demo_ids=sorted(HELPFUL))
    assert result.optimization.objective == "accuracy"
    assert result.winner_trial == "incumbent"


def test_an_exhausted_budget_keeps_the_incumbent():
    result = asyncio.run(improve_example_list(TASK, LABELED, FakeModel(HELPFUL), max_model_calls=5,
                                              hard_demo_ids=sorted(HELPFUL)))
    assert result.winner_trial == "incumbent" and result.promoted is False
    assert "incomplete" in result.reason


def test_a_fixed_global_policy_pick_freezes_into_a_list_with_its_next_items_as_reserves():
    from .context import RandomBalanced

    fixed = example_list_from_policy(RandomBalanced(seed=2), TASK, LABELED, per_label=3)
    pick = RandomBalanced(seed=2).select(TASK, Item("x", {"text": "x"}), LABELED, per_label=4)
    assert fixed.example_ids == tuple(row.item.id for row in pick if row in pick[:3] or row in pick[4:7])
    assert fixed.reserve_ids == (pick[3].item.id, pick[7].item.id)

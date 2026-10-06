"""Specs for retrospective human-feedback replay, without a network."""
import asyncio
import json

from .classifier_config import ClassifierConfig
from .flywheel import DecisionFlywheel
from .flywheel_test import FakeModel, TASK, agent
from .models import Item, LabeledItem
from .replay import plan_replay, run_replay, recent_balanced


def rows():
    return tuple(LabeledItem(Item(f"r{i}", {"text": f"{'yes' if i%3 else 'no'} paper {i}"}),
                             "include" if i%3 else "exclude", context={"human_feedback": f"comment {i}"})
                 for i in range(36))


def test_replay_splits_are_disjoint_fixed_and_reveal_feedback_in_original_order():
    plan = plan_replay(TASK, rows(), seed="replay", batch_size=10)
    assert plan == plan_replay(TASK, rows(), seed="replay", batch_size=10)
    ids = [set(row.item.id for row in group) for group in (plan.training, plan.development, plan.scoreboard)]
    assert not (ids[0] & ids[1] or ids[0] & ids[2] or ids[1] & ids[2])
    train, dev = plan.revealed(10)
    assert set(row.item.id for row in (*train, *dev)) <= {row.item.id for row in rows()[:10]}
    assert plan.checkpoints[-1] == 36


def test_development_retains_majority_items_and_balances_by_scoring_not_discarding():
    plan = plan_replay(TASK, rows(), seed="replay", batch_size=10)
    counts = {label:sum(row.label==label for row in plan.development) for label in TASK.labels}
    assert counts == {'include':5,'exclude':3}
    assert plan.evaluation(len(plan.ordered),TASK.labels)==plan.scoreboard
    assert len(plan.scoreboard)==8
    assert plan.manifest(TASK)['development_policy']['weighting']=='equal_class'


def test_balancing_walks_back_to_older_minority_labels_but_keeps_only_recent_majority_labels():
    ordered = tuple(LabeledItem(Item(str(i), {"text": str(i)}), label) for i, label in enumerate(
        ("include", "exclude", "include", "exclude", "exclude", "exclude")))
    selected = recent_balanced(ordered, ("include", "exclude"), per_class=2)
    assert [row.item.id for row in selected] == ["0", "2", "4", "5"]
    assert recent_balanced(ordered[3:], ("include", "exclude"), per_class=2) == ()


def test_replay_starts_untrained_and_keeps_scoreboard_labels_out_of_every_optimizer_prompt(tmp_path):
    calls = []
    optimizer = agent(calls)
    # Use a valid empty structural proposal rather than fixture-specific IDs.
    from .optimizer_agent import OptimizerReply
    optimizer.complete = lambda _: OptimizerReply('{"rubric":"Practical papers","tasks":[]}', "fake")
    wheel = DecisionFlywheel(tmp_path / "replay.sqlite", ClassifierConfig(TASK), FakeModel(), optimizer)
    plan = plan_replay(TASK, rows(), seed="replay", batch_size=18)
    result = asyncio.run(run_replay(wheel, plan))
    assert result["checkpoints"][0]["revealed_labels"] == 0
    assert result["checkpoints"][0]["fitted_head"] is False
    assert len(result["checkpoints"]) == 3
    protected = {row.item.id for row in (*plan.development, *plan.scoreboard)}
    for event in calls:
        if event["kind"] == "optimizer-request":
            brief = json.loads(event["messages"][1]["content"])
            assert not protected & {row["id"] for row in brief["feedback"]}
    assert len(plan.evaluation(len(plan.ordered), TASK.labels)) == result["checkpoints"][-1]["scoreboard"]["count"]
    wheel.close()

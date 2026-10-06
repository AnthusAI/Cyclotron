"""Control trials are isolated; retained ideas are not permanently eliminated."""
import asyncio
import json

from .classifier_config import ClassifierConfig
from .control_scheduler import ControlScheduler
from .flywheel import DecisionFlywheel
from .flywheel_test import FakeModel, TASK, TRAIN, DEV
from .models import Item, LabeledItem
from .optimizer_agent import OptimizerAgent, OptimizerReply


def optimizer(calls):
    def complete(messages):
        control = json.loads(messages[-1]["content"])["current"]["control_under_test"]
        calls.append(control)
        value = {"rubric": "Practical work", "example_ids": ["t0", "t1"],
                 "tasks": [{"name": "practical", "instructions": "Is this practical?", "labels": ["yes", "no"]}]}[control]
        return OptimizerReply(json.dumps({"rationale": "A testable idea", control: value}), "fake")
    return OptimizerAgent(complete)


def test_all_three_controls_are_tested_against_one_incumbent_then_only_the_best_is_promoted(tmp_path):
    calls = []
    wheel = DecisionFlywheel(tmp_path / "wheel.sqlite", ClassifierConfig(TASK), FakeModel(), optimizer(calls))
    result = asyncio.run(ControlScheduler(wheel).run(TRAIN, DEV, protected=(), propensities={r.item.id: 1. for r in TRAIN}))
    assert calls == ["rubric", "example_ids", "tasks"]
    assert len(result["trials"]) == 3
    assert result["promoted"]
    assert wheel.active.config.tasks[0].name == "practical"
    assert wheel.active.config.rubric == ""
    assert wheel.active.config.example_ids == ()
    assert len(ControlScheduler(wheel).ideas()) == 3
    wheel.close()


def test_unchanged_feedback_does_not_repeat_discovery_and_retained_ideas_retry_with_more_labels(tmp_path):
    calls = []
    wheel = DecisionFlywheel(tmp_path / "wheel.sqlite", ClassifierConfig(TASK), FakeModel(), optimizer(calls))
    scheduler = ControlScheduler(wheel)
    kwargs = dict(protected=(), propensities={r.item.id: 1. for r in TRAIN})
    asyncio.run(scheduler.run(TRAIN, DEV, **kwargs))
    asyncio.run(scheduler.run(TRAIN, DEV, **kwargs))
    assert len(calls) == 3
    new = (*TRAIN, LabeledItem(Item("later", {"text": "yes later"}), "include"))
    asyncio.run(scheduler.run(new, DEV, protected=(), propensities={r.item.id: 1. for r in new}))
    assert any(len(idea["attempts"]) == 2 for idea in scheduler.ideas())
    wheel.close()


def test_restart_does_not_rediscover_a_completed_control_before_resuming_the_remaining_controls(tmp_path):
    import pytest
    calls = []
    normal = optimizer(calls)
    def interrupt(messages):
        if json.loads(messages[-1]["content"])["current"]["control_under_test"] == "example_ids":
            raise KeyboardInterrupt
        return normal.complete(messages)
    path = tmp_path / "wheel.sqlite"
    wheel = DecisionFlywheel(path, ClassifierConfig(TASK), FakeModel(), OptimizerAgent(interrupt))
    kwargs = dict(protected=(), propensities={r.item.id: 1. for r in TRAIN})
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(wheel.improve_controls(TRAIN, DEV, **kwargs))
    wheel.close()
    wheel = DecisionFlywheel(path, ClassifierConfig(TASK), FakeModel(), optimizer(calls))
    result = asyncio.run(wheel.improve_controls(TRAIN, DEV, retry_interrupted=True, **kwargs))
    assert calls == ["rubric", "example_ids", "tasks"]
    assert len(result["trials"]) == 3
    assert result["promoted"]
    wheel.close()


def test_old_unsuccessful_ideas_get_retry_priority_even_when_new_ideas_keep_arriving(tmp_path):
    calls = []
    original = optimizer(calls)
    def varied(messages):
        reply = original.complete(messages)
        proposal = json.loads(reply.content)
        if "rubric" in proposal:
            proposal["rubric"] += f" idea {len(calls)}"
        if "example_ids" in proposal:
            proposal["example_ids"] = [f"t{(len(calls)//3) % 6}"]
        if "tasks" in proposal:
            proposal["tasks"][0]["name"] += str(len(calls))
        return OptimizerReply(json.dumps(proposal), "fake")
    wheel = DecisionFlywheel(tmp_path / "wheel.sqlite", ClassifierConfig(TASK), FakeModel(), OptimizerAgent(varied),
                             max_requests=500)
    original_score = wheel._score
    async def neutral(*args):
        score = await original_score(*args)
        score["balanced_brier"] = .5
        return score
    wheel._score = neutral
    first_ids = []
    for cycle in range(4):
        training = (*TRAIN, *(LabeledItem(Item(f"new{i}", {"text": f"yes newer {i}"}), "include")
                              for i in range(cycle)))
        asyncio.run(wheel.improve_controls(training, DEV, protected=(),
                                          propensities={row.item.id: 1. for row in training}))
        if not cycle:
            first_ids = [idea["id"] for idea in ControlScheduler(wheel).ideas()]
    retained = {idea["id"]: idea for idea in ControlScheduler(wheel).ideas()}
    assert all(len(retained[key]["attempts"]) == 2 for key in first_ids)
    wheel.close()

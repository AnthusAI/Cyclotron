"""Example, rubric and question stages never silently run each other."""
import asyncio
import json

from .classifier_config import ClassifierConfig
from .flywheel import DecisionFlywheel
from .flywheel_test import FakeModel, TASK, TRAIN, DEV
from .optimizer_agent import OptimizerAgent, OptimizerReply
from .staged_optimization import optimize_stage


def test_question_stage_backfills_all_available_training_labels_without_a_development_gate(tmp_path):
    calls = []
    def complete(messages):
        calls.append(json.loads(messages[-1]["content"])["current"]["control_under_test"])
        return OptimizerReply('{"rationale":"Practical work","tasks":[{"name":"practical","instructions":"Practical?","labels":["yes","no"]}]}', "fake")
    wheel = DecisionFlywheel(tmp_path / "wheel.sqlite", ClassifierConfig(TASK, rubric="Existing"), FakeModel(), OptimizerAgent(complete))
    kwargs = dict(protected=tuple(row.item for row in DEV), propensities={r.item.id: 1. for r in TRAIN})
    report = asyncio.run(optimize_stage(wheel, "questions", TRAIN, (), **kwargs))
    assert calls == ["tasks"]
    assert report["count"] == 6
    assert not report["promoted"]
    assert asyncio.run(optimize_stage(wheel, "questions", TRAIN, (), **kwargs)) == report
    assert calls == ["tasks"]
    wheel.close()


def test_rubric_stage_can_propose_without_promoting_on_four_development_items(tmp_path):
    calls = []
    def complete(messages):
        calls.append(json.loads(messages[-1]["content"])["current"]["control_under_test"])
        return OptimizerReply('{"rationale":"Practical work","rubric":"Practical"}', "fake")
    model = FakeModel()
    wheel = DecisionFlywheel(tmp_path / "wheel.sqlite", ClassifierConfig(TASK), model, OptimizerAgent(complete))
    report = asyncio.run(optimize_stage(wheel, "rubric", TRAIN, DEV, protected=(), propensities={r.item.id: 1. for r in TRAIN}))
    assert calls == ["rubric"]
    assert report["proposal"]["rubric"] == "Practical"
    assert report["minimum_development_per_class"] == 20
    assert model.calls == 0
    assert wheel.active.config.rubric == ""
    wheel.close()


def test_a_stages_own_promotion_does_not_repeat_discovery_on_unchanged_feedback(tmp_path):
    calls = []
    def complete(messages):
        calls.append(messages)
        return OptimizerReply('{"rationale":"Practical","rubric":"Practical"}', "fake")
    wheel = DecisionFlywheel(tmp_path / "wheel.sqlite", ClassifierConfig(TASK), FakeModel(), OptimizerAgent(complete))
    score = wheel._score
    async def configured(classifier, *args):
        result = await score(classifier, *args)
        result["balanced_brier"] = .1 if classifier.head else .9
        return result
    wheel._score = configured
    kwargs = dict(protected=(), propensities={r.item.id: 1. for r in TRAIN}, min_development_per_class=1)
    first = asyncio.run(optimize_stage(wheel, "rubric", TRAIN, DEV, **kwargs))
    assert first["promoted"]
    assert asyncio.run(optimize_stage(wheel, "rubric", TRAIN, DEV, **kwargs)) == first
    assert len(calls) == 1
    wheel.close()


def test_redundant_task_metadata_is_canonicalized_but_cannot_change_the_input_field(tmp_path):
    import pytest
    for field in ("text", "secret_other_field"):
        def complete(messages):
            return OptimizerReply(json.dumps({"rationale": "Measurement", "tasks": [{"name": "practical",
                "instructions": "Practical?", "labels": ["yes", "no"], "input_field": field}]}), "fake")
        wheel = DecisionFlywheel(tmp_path / f"{field}.sqlite", ClassifierConfig(TASK), FakeModel(), OptimizerAgent(complete))
        kwargs = dict(protected=(), propensities={r.item.id: 1. for r in TRAIN})
        if field == "text":
            assert asyncio.run(optimize_stage(wheel, "questions", TRAIN, (), **kwargs))["count"] == 6
        else:
            with pytest.raises(ValueError, match="input field"):
                asyncio.run(optimize_stage(wheel, "questions", TRAIN, (), **kwargs))
        wheel.close()


def test_a_recorded_optimizer_reply_survives_interruption_before_proposal_validation(tmp_path):
    import pytest
    calls = []
    def complete(messages):
        calls.append(messages)
        return OptimizerReply('{"rationale":"Measured topic","tasks":[{"name":"practical","instructions":"Practical?","labels":["yes","no"]}]}', "fake")
    def interrupt(event):
        if event["kind"] == "optimizer-response":
            raise KeyboardInterrupt
    path = tmp_path / "wheel.sqlite"
    wheel = DecisionFlywheel(path, ClassifierConfig(TASK), FakeModel(), OptimizerAgent(complete), observer=interrupt)
    kwargs = dict(protected=(), propensities={r.item.id: 1. for r in TRAIN})
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(optimize_stage(wheel, "questions", TRAIN, (), **kwargs))
    wheel.close()
    def forbidden(messages):
        raise AssertionError("recovery must not rediscover a recorded proposal")
    wheel = DecisionFlywheel(path, ClassifierConfig(TASK), FakeModel(), OptimizerAgent(forbidden))
    report = asyncio.run(optimize_stage(wheel, "questions", TRAIN, (), retry_interrupted=True, **kwargs))
    assert report["count"] == 6
    assert len(calls) == 1
    assert any(e["kind"] == "optimizer-response-reused" for e in wheel.history())
    wheel.close()

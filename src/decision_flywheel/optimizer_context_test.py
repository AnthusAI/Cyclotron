"""Human explanation context is persistent and cannot masquerade as clean evaluation."""
import asyncio
import json

from .classifier_config import ClassifierConfig
from .flywheel import DecisionFlywheel
from .flywheel_test import FakeModel, TASK, TRAIN, DEV
from .optimizer_agent import OptimizerAgent, OptimizerReply


def test_explanations_reach_the_next_stage_and_survive_restarts(tmp_path):
    prompts = []
    def complete(messages):
        prompts.append(json.loads(messages[-1]["content"]))
        return OptimizerReply('{"rationale":"Use explanations","rubric":"Knowledge management"}', "fake")
    path = tmp_path / "runtime.sqlite"
    wheel = DecisionFlywheel(path, ClassifierConfig(TASK), FakeModel(), OptimizerAgent(complete))
    kwargs = dict(protected=(), propensities={r.item.id: 1. for r in TRAIN})
    asyncio.run(wheel.optimize_stage("rubric", TRAIN, DEV, **kwargs))
    wheel.set_optimizer_context(("knowledge base management",), evaluation_context_exposed=True)
    result = asyncio.run(wheel.optimize_stage("rubric", TRAIN, DEV, **kwargs))
    assert prompts[-1]["human_explanations"] == ["knowledge base management"]
    assert len(prompts) == 2
    assert not result["evaluation_independent_of_optimizer_context"]
    wheel.close()
    wheel = DecisionFlywheel(path, ClassifierConfig(TASK), FakeModel(), OptimizerAgent(complete))
    assert wheel.optimizer_context["human_explanations"] == ["knowledge base management"]
    wheel.set_optimizer_context((), evaluation_context_exposed=False)
    assert wheel.optimizer_context["evaluation_context_exposed"]
    wheel.close()

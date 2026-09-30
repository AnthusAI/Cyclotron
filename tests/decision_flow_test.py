import asyncio
from types import SimpleNamespace

import pytest

from decision_flywheel.adapters.system_one import SystemOneAdapter
from decision_flywheel.context import PerLabelLexicalRetrieval, RandomBalanced
from decision_flywheel.models import DecisionTask, Item, LabeledItem


class FakeSystemOneClient:
    async def system_one(self, *, state, questions):
        self.state = state
        answer = SimpleNamespace(
            model_dump=lambda: {"choice": "YES!", "probabilities": {"yes": 0.75, "NO!": 0.25}}
        )
        return SimpleNamespace(answers={"topic": answer}, model="fake-system-one", usage={"tokens": 12})


@pytest.mark.parametrize("policy", [RandomBalanced(seed=4), PerLabelLexicalRetrieval()])
def test_a_selected_context_produces_a_validated_offline_decision(policy):
    task = DecisionTask("topic", ("yes", "no"), "Classify the target.")
    target = Item("target", {"text": "target lexical signal"})
    candidates = [
        LabeledItem(Item("yes-1", {"text": "yes lexical signal"}), "yes"),
        LabeledItem(Item("no-1", {"text": "no lexical signal"}), "no"),
    ]
    context = policy.select(task, target, candidates, per_label=1)
    client = FakeSystemOneClient()

    result = asyncio.run(SystemOneAdapter(client).decide(task, target, context))

    assert result.label == "yes"
    assert result.probabilities == {"yes": 0.75, "no": 0.25}
    assert client.state["target"] == {"text": "target lexical signal"}
    assert {example["text"] for example in client.state["labeled_examples"]} == {
        "yes lexical signal", "no lexical signal"
    }

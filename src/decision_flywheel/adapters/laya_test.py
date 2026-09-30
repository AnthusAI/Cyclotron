import asyncio

import pytest

from .laya import LayaAdapter
from ..models import DecisionTask, Item, LabeledItem


class FakeLayaModel:
    def __init__(self):
        self.calls = 0

    def system_one(self, *, state, questions):
        self.calls += 1
        self.state, self.questions = state, questions
        return {"answers": {"topic": {"choice": "YES!"}}}


def test_a_laya_adapter_rejects_context_without_calling_its_model():
    model = FakeLayaModel()
    task = DecisionTask("topic", ("yes", "no"), "Classify only target.")

    with pytest.raises(NotImplementedError, match="context serialization"):
        asyncio.run(LayaAdapter(model).decide(
            task, Item("target", {"text": "target"}),
            [LabeledItem(Item("demo", {"text": "demo"}), "yes")],
        ))

    assert model.calls == 0


def test_a_laya_zero_shot_result_canonicalizes_labels_without_inventing_probabilities():
    model = FakeLayaModel()
    task = DecisionTask("topic", ("yes", "no"), "Classify only target.")
    adapter = LayaAdapter(model)

    result = asyncio.run(adapter.decide(task, Item("target", {"text": "target"}), []))

    assert result.label == "yes"
    assert result.probabilities is None
    assert model.calls == 1
    assert adapter.capabilities.supports_probability_distributions is False

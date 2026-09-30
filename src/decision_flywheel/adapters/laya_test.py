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

    with pytest.raises(NotImplementedError, match="no documented labeled-demonstration"):
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
    assert adapter.capabilities.supports_probability_distributions is True


def test_a_laya_adapter_rejects_a_zero_shot_state_that_upstream_would_truncate():
    class Tokenizer:
        def __call__(self, text, *, add_special_tokens):
            return {"input_ids": list(range(len(text.split())))}

    model = FakeLayaModel()
    model.tok = Tokenizer()
    model.cfg = {"max_len": 12, "head_max_len": 4}

    with pytest.raises(ValueError, match="would truncate"):
        asyncio.run(LayaAdapter(model).decide(
            DecisionTask("topic", ("yes", "no"), "Classify."),
            Item("target", {"text": "one two three four five"}), [],
        ))

    assert model.calls == 0

import asyncio
from types import SimpleNamespace

from .system_one import SystemOneAdapter
import pytest

from ..models import DecisionTask, Item, LabeledItem, ModelCapabilities


class FakeClient:
    async def system_one(self, *, state, questions):
        self.state, self.questions = state, questions
        answer = SimpleNamespace(model_dump=lambda: {"choice": "yes", "probabilities": {"yes": .8, "no": .2}})
        return SimpleNamespace(answers={"topic": answer}, model="fake", usage=None)


class ConfidenceOnlyClient:
    async def system_one(self, *, state, questions):
        self.called = True
        answer = SimpleNamespace(model_dump=lambda: {"choice": "yes", "confidence": 0.8})
        return SimpleNamespace(answers={"topic": answer}, model="fake", usage=None)


def test_a_system_one_adapter_keeps_target_and_context_separate():
    client = FakeClient(); task = DecisionTask("topic", ("yes", "no"), "Classify only target.")
    result = asyncio.run(SystemOneAdapter(client, name="jev").decide(task, Item("t", {"text": "target"}), [LabeledItem(Item("d", {"text": "demo"}), "yes")]))
    assert result.label == "yes"
    assert client.state == {"labeled_examples": [{"text": "demo", "label": "yes"}], "target": {"text": "target"}}
    assert client.questions["topic"]["criteria"] == {"yes": None, "no": None}


def test_a_confidence_is_not_fabricated_into_a_probability_distribution():
    client = ConfidenceOnlyClient(); task = DecisionTask("topic", ("yes", "no"), "Classify only target.")

    result = asyncio.run(SystemOneAdapter(client).decide(task, Item("t", {"text": "target"}), []))

    assert result.probabilities is None


def test_a_model_that_rejects_labeled_context_never_calls_its_provider():
    client = FakeClient(); task = DecisionTask("topic", ("yes", "no"), "Classify only target.")
    adapter = SystemOneAdapter(client, capabilities=ModelCapabilities(supports_labeled_context=False))

    with pytest.raises(ValueError, match="labeled context"):
        asyncio.run(adapter.decide(task, Item("t", {"text": "target"}), [LabeledItem(Item("d", {"text": "demo"}), "yes")]))

    assert not hasattr(client, "state")

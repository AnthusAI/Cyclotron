import asyncio
from types import SimpleNamespace

import pytest

from .jev import JevAdapter, JevConfiguration
from ..models import DecisionTask, Item, LabeledItem


TASK = DecisionTask("topic", ("yes", "no"), "Classify only the target.")
TARGET = Item("target", {"text": "target text"})
CONTEXT = [LabeledItem(Item("demo", {"text": "demo text"}), "yes")]


class FakeJevClient:
    def system_one(self, *, state, questions, **kwargs):
        self.calls = getattr(self, "calls", 0) + 1
        self.state, self.questions, self.kwargs = state, questions, kwargs
        return SimpleNamespace(
            answers={"topic": {"choice": "YES!", "probabilities": {"yes": 0.8, "no": 0.2}}},
            model="jev-2026-09-30", usage={"input_tokens": 12, "output_tokens": 3},
        )


def test_a_jev_adapter_changes_only_examples_between_zero_and_few_shot_requests():
    client = FakeJevClient()
    adapter = JevAdapter(client, configuration=JevConfiguration(model="jev-test"))

    asyncio.run(adapter.decide(TASK, TARGET, []))
    zero_state, zero_questions = client.state, client.questions
    asyncio.run(adapter.decide(TASK, TARGET, CONTEXT))

    assert zero_state["target"] == client.state["target"] == {"text": "target text"}
    assert zero_state["labeled_examples"] == []
    assert client.state["labeled_examples"] == [{"text": "demo text", "label": "yes"}]
    assert zero_questions == client.questions
    assert client.questions == {"topic": {"type": "choice", "instructions": "Classify only the target.", "criteria": {"yes": None, "no": None}}}


def test_a_jev_adapter_keeps_provider_probabilities_and_numeric_usage_without_article_text():
    client = FakeJevClient()
    result = asyncio.run(JevAdapter(client).decide(TASK, TARGET, CONTEXT))

    assert result.label == "yes"
    assert result.probabilities == {"yes": 0.8, "no": 0.2}
    assert result.model == "jev-2026-09-30"
    assert result.usage == {"input_tokens": 12, "output_tokens": 3}


def test_a_jev_adapter_disables_sdk_retries_per_call_so_outer_runners_own_attempt_counting():
    client = FakeJevClient()
    adapter = JevAdapter(client)

    asyncio.run(adapter.decide(TASK, TARGET, []))

    assert "retry" not in client.kwargs
    assert adapter.configuration.max_retries == 0


def test_a_jev_adapter_rejects_bad_context_before_calling_the_client():
    client = FakeJevClient()
    bad = [LabeledItem(Item("demo", {"text": "demo"}), "outside")]

    with pytest.raises(ValueError, match="not one of"):
        asyncio.run(JevAdapter(client).decide(TASK, TARGET, bad))

    assert not hasattr(client, "calls")

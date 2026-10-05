import asyncio
from types import SimpleNamespace

import pytest

from .jev import JevAdapter, JevConfiguration
from ..models import DecisionTask, Item, LabeledItem


TASK = DecisionTask("topic", ("yes", "no"), "Classify only the target.")
TARGET = Item("target", {"text": "target text"})
CONTEXT = [LabeledItem(Item("demo", {"text": "demo text"}), "yes")]


def test_a_complete_classifier_request_asks_all_feature_questions_in_one_jev_call():
    from ..classifier_config import ClassifierConfig
    class Client:
        calls = 0
        def system_one(self, *, state, questions):
            self.calls += 1
            self.state, self.questions = state, questions
            return SimpleNamespace(answers={key: {"choice": "yes", "probabilities": {"yes": .8, "no": .2}}
                                            for key in questions}, model="fake", usage={"input_tokens": 42})
    client = Client()
    config = ClassifierConfig(TASK, rubric="Practical work", example_ids=("demo",),
                              tasks=(DecisionTask("practical", ("yes", "no"), "Is this practical?"),))
    batch = asyncio.run(JevAdapter(client).classify(config, TARGET, CONTEXT))
    assert client.calls == 1
    assert client.state["rubric"] == "Practical work"
    assert set(client.questions) == {"decision", "practical"}
    assert client.questions["practical"]["criteria"] == {"yes": None, "no": None}
    assert "options" not in client.questions["practical"]
    assert batch.answers["decision"].probabilities == {"yes": .8, "no": .2}
    assert batch.usage == {"input_tokens": 42}
    assert all(answer.usage is None for answer in batch.answers.values())


def test_inspection_records_the_exact_provider_bound_state_questions_and_answers():
    from ..classifier_config import ClassifierConfig
    events = []
    class Client:
        def system_one(self, *, state, questions):
            self.state, self.questions = state, questions
            return SimpleNamespace(answers={"decision": {"choice": "yes", "probabilities": {"yes": .7, "no": .3}}},
                                   model="fake", usage={"input_tokens": 4})
    client = Client()
    asyncio.run(JevAdapter(client).classify(ClassifierConfig(TASK), TARGET, [], event_sink=events.append))
    assert events[0]["kind"] == "decision-request"
    assert events[0]["state"] == client.state
    assert events[0]["questions"] == client.questions
    assert events[1]["kind"] == "decision-response"
    assert events[1]["answers"]["decision"]["probabilities"]["yes"] == .7


class FakeJevClient:
    def system_one(self, *, state, questions, **kwargs):
        self.calls = getattr(self, "calls", 0) + 1
        self.state, self.questions, self.kwargs = state, questions, kwargs
        return SimpleNamespace(
            answers={"topic": {"choice": "YES!", "confidence": 0.31, "probabilities": {"yes": 0.8, "no": 0.2}}},
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
    assert result.confidence == 0.31
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


def test_a_jev_adapter_sends_generic_demo_context_only_with_labeled_examples():
    client = FakeJevClient()
    contextual = [LabeledItem(Item("demo", {"text": "demo text"}), "yes",
                               context={"human_feedback": "This is useful."})]

    asyncio.run(JevAdapter(client).decide(TASK, TARGET, contextual))

    assert client.state["labeled_examples"] == [
        {"text": "demo text", "label": "yes", "human_feedback": "This is useful."}
    ]
    assert client.state["target"] == {"text": "target text"}

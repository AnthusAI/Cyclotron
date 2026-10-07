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


def test_a_laya_adapter_sends_labeled_context_separately_from_the_target():
    model = FakeLayaModel()
    task = DecisionTask("topic", ("yes", "no"), "Classify only target.")

    asyncio.run(LayaAdapter(model).decide(
        task, Item("target", {"text": "target"}),
        [LabeledItem(Item("demo", {"text": "demo"}), "yes")],
    ))
    assert model.calls == 1
    assert model.state == {"target": {"text": "target"},
                           "labeled_examples": [{"text": "demo", "label": "yes"}]}


def test_laya_batches_scoped_rubrics_examples_and_feature_questions_with_exact_traces():
    from ..classifier_config import ClassifierConfig
    class JointLaya(FakeLayaModel):
        def system_one(self, *, state, questions):
            self.calls += 1
            self.state, self.questions = state, questions
            return {"model": "laya-pinned", "usage": {"input_tokens": 20}, "answers": {
                key: {"choice": "yes", "probabilities": {"yes": .8, "no": .2}, "confidence": .7}
                for key in questions}}
    model = JointLaya()
    task = DecisionTask("topic", ("yes", "no"), "Classify only target.")
    extra = DecisionTask("knowledge", ("yes", "no"), "Is this about knowledge bases?")
    configs = {"a": ClassifierConfig(task, rubric="Prefer knowledge", example_ids=("demo",), tasks=(extra,)),
               "b": ClassifierConfig(task, rubric="Prefer evidence")}
    example = LabeledItem(Item("demo", {"text": "Known useful article"}), "yes")
    events = []
    result = asyncio.run(LayaAdapter(model).classify_many(
        configs, Item("target", {"text": "New article"}), {"a": (example,), "b": ()}, event_sink=events.append))
    assert model.calls == 1
    assert set(model.questions) == {"q0", "q1", "q2"}
    assert model.state["classifiers"]["a"]["rubric"] == "Prefer knowledge"
    assert model.state["classifiers"]["a"]["examples"][0]["label"] == "yes"
    assert model.state["classifiers"]["a"]["examples"][0]["text"] == "Known useful article"
    assert model.state["classifiers"]["b"]["examples"] == []
    assert set(result.answers["a"]) == {"decision", "knowledge"}
    assert result.answers["a"]["knowledge"].probabilities == {"yes": .8, "no": .2}
    assert result.answers["a"]["knowledge"].confidence == .7
    assert result.usage == {"input_tokens": 20}
    assert [e["kind"] for e in events] == ["decision-request", "decision-response"]
    assert events[0]["state"] == model.state
    assert events[0]["questions"] == model.questions
    assert events[1]["model"] == "laya-pinned"
    assert events[0]["question_bindings"]["q1"]["question"] == "knowledge"


def test_laya_refuses_provider_reported_truncation_before_using_a_result():
    class Truncated(FakeLayaModel):
        def system_one(self, **kwargs):
            self.calls += 1
            return {"answers": {"topic": {"choice": "yes"}},
                    "usage": {"truncated": True, "state_tokens_dropped": 40}}
    model = Truncated()
    with pytest.raises(ValueError, match="truncat"):
        asyncio.run(LayaAdapter(model).decide(DecisionTask("topic", ("yes", "no"), "Choose."),
                                             Item("t", {"text": "Article"}), []))
    assert model.calls == 1


def test_laya_keeps_a_rejected_truncated_response_visible_in_scorecard_traces():
    from ..classifier_config import ClassifierConfig
    class Truncated(FakeLayaModel):
        def system_one(self, **kwargs):
            self.calls += 1
            return {"answers": {"q0": {"choice": "yes"}}, "usage": {"truncated": True}}
    events = []
    with pytest.raises(ValueError, match="truncat"):
        asyncio.run(LayaAdapter(Truncated()).classify_many(
            {"a": ClassifierConfig(DecisionTask("topic", ("yes", "no"), "Choose."))},
            Item("t", {"text": "Article"}), {"a": ()}, event_sink=events.append))
    assert [e["kind"] for e in events] == ["decision-request", "decision-response"]
    assert events[1]["usage"]["truncated"] is True


def test_laya_checks_the_whole_serialized_scorecard_not_just_the_article_for_truncation():
    from ..classifier_config import ClassifierConfig
    model = FakeLayaModel()
    model.tok = lambda text, **_: {"input_ids": list(range(len(text.split())))}
    model.cfg = {"max_len": 18, "head_max_len": 4}
    task = DecisionTask("topic", ("yes", "no"), "Choose.")
    with pytest.raises(ValueError, match="would truncate"):
        asyncio.run(LayaAdapter(model).classify_many(
            {"a": ClassifierConfig(task, rubric="This rubric has many important words to retain fully")},
            Item("t", {"text": "Short"}), {"a": ()}))
    assert model.calls == 0


def test_laya_refuses_question_instructions_that_the_upstream_head_budget_would_cut():
    model = FakeLayaModel()
    model.tok = lambda text, **_: {"input_ids": list(range(len(text.split())))}
    model.cfg = {"max_len": 512, "head_max_len": 12}
    with pytest.raises(ValueError, match="would truncate question"):
        asyncio.run(LayaAdapter(model).decide(
            DecisionTask("topic", ("yes", "no"), "Do not drop any of these important instructions that define this classification boundary."),
            Item("t", {"text": "Short"}), []))
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


def test_a_laya_adapter_preserves_an_explicit_confidence_when_upstream_exposes_one():
    class ConfidenceLaya(FakeLayaModel):
        def system_one(self, *, state, questions):
            self.calls += 1
            return {"answers": {"topic": {"choice": "YES!", "confidence": 0.23,
                                            "probabilities": {"yes": 0.8, "no": 0.2}}}}

    task = DecisionTask("topic", ("yes", "no"), "Classify only target.")
    result = asyncio.run(LayaAdapter(ConfidenceLaya()).decide(task, Item("target", {"text": "target"}), []))

    assert result.confidence == 0.23
    assert result.confidence != max(result.probabilities.values())


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

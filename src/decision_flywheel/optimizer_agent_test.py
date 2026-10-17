"""Specs for the observable optimizer boundary, with no paid clients."""
import json

import pytest

from .models import DecisionTask, Item, LabeledItem
from .optimizer_agent import DisabledOptimizer, FeedbackBriefing, OptimizerAgent, OptimizerReply

TASK = DecisionTask("include", ("include", "exclude"), "Should this item be included?")


def test_question_discovery_advertises_datetime_as_an_independent_alternative_not_a_mixed_edit():
    request = FeedbackBriefing.build(TASK, [example()], protected=(),
        current={'control_under_test':'tasks'})
    instructions = OptimizerAgent(lambda _: None).request_messages(request)[0]['content']
    assert 'exactly one of tasks or dynamic_elements, never both' in instructions
    assert 'keeps all questions, rubric and examples fixed' in instructions


def test_original_predictions_explain_errors_without_becoming_few_shot_context():
    from .classifier_config import ClassifierConfig
    row = LabeledItem(Item("train", {"text": "Recent research"}), "include",
                      context={"human_feedback": "Practical work belongs here"},
                      initial_answer_value="exclude")
    payload = briefing([row]).payload["feedback"][0]
    assert payload["initial_answer_value"] == "exclude"
    assert payload["prediction_matches_label"] is False
    request = ClassifierConfig(TASK, example_ids=("train",)).request(
        Item("new", {"text": "Another paper"}), [row])
    assert "initial_answer_value" not in request["state"]["examples"][0]
    assert "prediction_matches_label" not in request["state"]["examples"][0]


def test_a_missing_original_prediction_is_not_guessed_from_the_human_label():
    feedback = briefing().payload["feedback"][0]
    assert feedback["initial_answer_value"] is None
    assert feedback["prediction_matches_label"] is None


def test_original_prediction_evidence_changes_the_optimizer_briefing_fingerprint():
    from dataclasses import replace
    row = example()
    assert briefing([row]).fingerprint != briefing([replace(row, initial_answer_value="include")]).fingerprint


def example(key="train", text="Recent research", source="trusted"):
    return LabeledItem(Item(key, {"text": text}), "include", source,
                       {"human_feedback": "I want recent practical work"})


def briefing(rows=None, protected=()):
    return FeedbackBriefing.build(TASK, rows or [example()], protected=protected,
                                  current={"rubric": "", "tasks": [], "example_ids": []})


def test_the_optimizer_receives_actual_labels_comments_and_the_current_configuration():
    payload = briefing().payload
    assert payload["feedback"][0]["label"] == "include"
    assert payload["feedback"][0]["comment"] == "I want recent practical work"
    assert payload["feedback"][0]["values"]["text"] == "Recent research"
    assert payload["current"]["rubric"] == ""


def test_explicit_human_explanations_are_separate_from_item_examples_and_change_the_fingerprint():
    original = briefing()
    informed = FeedbackBriefing.build(TASK, [example()], current={}, protected=(),
        human_explanations=("knowledge base management", "information systems, information retrieval"))
    assert informed.payload["human_explanations"] == ["knowledge base management", "information systems, information retrieval"]
    assert informed.fingerprint != original.fingerprint
    assert len(informed.payload["feedback"]) == 1


def test_task_discovery_is_told_that_questions_are_individual_measurements_not_a_bundle():
    sent = []
    def complete(messages):
        sent.extend(messages)
        return OptimizerReply('{"rationale":"No new evidence","tasks":[]}', "fake")
    current = {"rubric": "", "tasks": [], "example_ids": [], "control_under_test": "tasks"}
    OptimizerAgent(complete).propose(FeedbackBriefing.build(TASK, [example()], current=current, protected=()))
    assert "one question at a time" in sent[0]["content"]
    assert "same name" in sent[0]["content"]


@pytest.mark.parametrize("protected", [Item("train", {"text": "different"}),
                                    Item("audit", {"text": " RECENT   research "})])
def test_protected_ids_and_normalized_text_cannot_reach_the_optimizer(protected):
    with pytest.raises(ValueError, match="protected"):
        briefing(protected=[protected])


def test_untrusted_labels_are_rejected_before_any_optimizer_call():
    with pytest.raises(ValueError, match="trusted"):
        briefing([example(source="model")])


def test_a_fake_optimizer_exposes_the_exact_prompt_reply_and_tools():
    observed = []
    sent = []
    def complete(messages):
        sent.append(messages)
        return OptimizerReply('{"rationale":"recency matters","rubric":"Recent work"}',
                              model="fake", usage={"total_tokens": 12},
                              tool_calls=({"name": "inspect_feedback", "arguments": {}},))
    agent = OptimizerAgent(complete, observer=observed.append)
    proposal = agent.propose(briefing())
    assert proposal["rubric"] == "Recent work"
    assert [event["kind"] for event in observed] == ["optimizer-request", "optimizer-response"]
    assert observed[0]["messages"] == sent[0]
    assert observed[1]["tool_calls"][0]["name"] == "inspect_feedback"
    assert observed[1]["usage"]["total_tokens"] == 12
    assert "current_datetime" in sent[0][0]["content"]
    assert json.loads(sent[0][1]["content"])["feedback"][0]["comment"]


def test_a_malformed_reply_remains_visible_and_cannot_become_a_proposal():
    events = []
    agent = OptimizerAgent(lambda _: OptimizerReply("not json", model="fake"), observer=events.append)
    with pytest.raises(ValueError, match="JSON object"):
        agent.propose(briefing())
    assert events[-1]["content"] == "not json"


def test_optimizer_failure_diagnostics_do_not_echo_provider_exception_secrets():
    events = []
    def fail(_):
        raise RuntimeError("secret-provider-key")
    with pytest.raises(RuntimeError, match="optimizer request failed"):
        OptimizerAgent(fail, observer=events.append).propose(briefing())
    assert "secret-provider-key" not in json.dumps(events)


def test_briefings_snapshot_mutable_input_and_have_stable_fingerprints():
    row = example()
    first = briefing([row])
    row.item.values["text"] = "changed"
    assert first.payload["feedback"][0]["values"]["text"] == "Recent research"
    assert first.fingerprint == briefing().fingerprint


def test_a_disabled_optimizer_refuses_structural_work_without_a_provider_call():
    optimizer = DisabledOptimizer()
    with pytest.raises(RuntimeError, match="optimizer is disabled"):
        optimizer.propose(None)


def _briefing():
    from .models import DecisionTask
    return FeedbackBriefing.build(DecisionTask("t", ("yes", "no"), "Q?"), (), current={"rubric": ""}, protected=())


def test_an_async_transport_is_awaited_and_events_stay_on_the_loop_thread():
    import asyncio
    import threading
    threads, events = [], []

    async def complete(messages):
        await asyncio.sleep(0)
        return OptimizerReply('{"rationale":"r","rubric":"x"}', "async-model")

    def observe(event):
        threads.append(threading.get_ident())
        events.append(event["kind"])

    async def scenario():
        loop_thread = threading.get_ident()
        proposal = await OptimizerAgent(complete, observer=observe).propose_async(_briefing())
        return loop_thread, proposal
    loop_thread, proposal = asyncio.run(scenario())
    assert proposal == {"rationale": "r", "rubric": "x"}
    assert events == ["optimizer-request", "optimizer-response"] and set(threads) == {loop_thread}


def test_a_synchronous_transport_behaves_exactly_as_before_through_the_async_methods():
    import asyncio
    calls, events = [], []
    agent = OptimizerAgent(lambda messages: calls.append(messages) or OptimizerReply('{"rubric":"x"}', "m"),
                           observer=lambda event: events.append(event["kind"]))
    assert asyncio.run(agent.propose_async(_briefing())) == agent.propose(_briefing()) == {"rubric": "x"}
    assert len(calls) == 2 and events == ["optimizer-request", "optimizer-response"] * 2


def test_an_async_only_transport_refuses_the_synchronous_methods():
    async def complete(messages):
        return OptimizerReply("{}", "m")
    with pytest.raises(TypeError, match="asynchronous"):
        OptimizerAgent(complete).propose(_briefing())


def test_a_failed_async_call_is_recorded_like_a_failed_sync_call():
    import asyncio
    events = []

    async def complete(messages):
        raise ConnectionError("down")
    with pytest.raises(RuntimeError, match="optimizer request failed"):
        asyncio.run(OptimizerAgent(complete, observer=lambda e: events.append(e)).propose_async(_briefing()))
    assert events[-1]["kind"] == "optimizer-failed" and events[-1]["error_type"] == "ConnectionError"


def test_optimizers_without_async_methods_are_called_synchronously():
    import asyncio

    from .optimizer_agent import ask_optimizer

    class Legacy:
        def propose(self, briefing):
            return {"rubric": "legacy"}
    assert asyncio.run(ask_optimizer(Legacy(), "propose", _briefing())) == {"rubric": "legacy"}


def test_an_acomplete_attribute_that_is_not_a_coroutine_function_does_not_make_a_transport_async():
    from unittest.mock import Mock
    transport = Mock(return_value=OptimizerReply('{"rubric":"x"}', "m"))
    transport.acomplete = Mock()   # a mock has every attribute; it is not an async transport
    assert OptimizerAgent(transport).propose(_briefing()) == {"rubric": "x"}
    transport.acomplete.assert_not_called()


def test_a_cancelled_async_call_is_recorded_and_the_cancellation_propagates():
    import asyncio
    events = []

    async def complete(messages):
        await asyncio.sleep(10)

    async def scenario():
        task = asyncio.create_task(OptimizerAgent(complete, observer=events.append).propose_async(_briefing()))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
    assert [e["kind"] for e in events] == ["optimizer-request", "optimizer-cancelled"]

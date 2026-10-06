"""Applications can advance one stage and replay a durable cursor-based trace."""
import asyncio
import json

from .classifier_config import ClassifierConfig
from .flywheel import DecisionFlywheel
from .flywheel_test import FakeModel, TASK, TRAIN, DEV
from .optimizer_agent import OptimizerAgent, OptimizerReply


def make_wheel(path, events):
    def complete(messages):
        return OptimizerReply('{"rationale":"Explicit guidance","tasks":[{"name":"practical","instructions":"Practical?","labels":["yes","no"]}]}',
            "fake", tool_calls=({"name": "inspect", "arguments": {}},))
    return DecisionFlywheel(path, ClassifierConfig(TASK), FakeModel(), OptimizerAgent(complete), observer=events.append)


def test_one_step_exposes_exact_inputs_outputs_tools_and_stops_before_training(tmp_path):
    observed = []
    wheel = make_wheel(tmp_path / "runtime.sqlite", observed)
    wheel.set_optimizer_context(("Knowledge management",))
    result = asyncio.run(wheel.step("questions", TRAIN, DEV, protected=(),
        propensities={r.item.id: 1. for r in TRAIN}, trigger="web-next"))
    trace = wheel.trace_events()
    events = trace["events"]
    assert events[0]["event_id"] > 0
    assert len({e["event_id"] for e in events}) == len(events)
    assert result["status"] == "completed"
    assert not any(e["kind"] == "fit-started" for e in events)
    assert not result["result"].get("classifier_training")
    request = next(e for e in events if e["kind"] == "optimizer-request")
    assert json.loads(request["messages"][-1]["content"])["human_explanations"] == ["Knowledge management"]
    response = next(e for e in events if e["kind"] == "optimizer-response")
    assert response["tool_calls"][0]["name"] == "inspect"
    assert request["step_id"] == response["step_id"] == result["step_id"]
    assert next(e for e in events if e["kind"] == "step-started")["trigger"] == "web-next"
    assert [e["event_id"] for e in observed] == [e["event_id"] for e in events]
    cursor = trace["cursor"]
    wheel.close()
    wheel = make_wheel(tmp_path / "runtime.sqlite", [])
    assert wheel.trace_events(after_event_id=cursor)["events"] == []
    assert wheel.trace_events(limit=2)["cursor"] == events[1]["event_id"]
    wheel.close()


def test_one_request_budget_can_pause_and_resume_without_repaying_cached_work(tmp_path):
    wheel = make_wheel(tmp_path / "runtime.sqlite", [])
    kwargs = dict(protected=(), propensities={r.item.id: 1. for r in TRAIN}, request_budget=1)
    first = asyncio.run(wheel.step("questions", TRAIN, DEV, **kwargs))
    assert first["status"] == "paused"
    assert wheel.model.calls == 1
    second = asyncio.run(wheel.step("questions", TRAIN, DEV, retry_interrupted=True, **kwargs))
    assert second["status"] == "paused"
    assert wheel.model.calls == 2
    assert sum(e["kind"] == "optimizer-request" for e in wheel.history()) == 1
    assert any(e["kind"] == "features-cached" and e.get("answers") for e in wheel.history())
    wheel.close()


def test_classifier_training_can_pause_before_fit_and_resume_the_same_cached_inputs(tmp_path):
    wheel = make_wheel(tmp_path / "runtime.sqlite", [])
    kwargs = dict(protected=(), propensities={r.item.id: 1. for r in TRAIN}, min_development_per_class=1)
    first = asyncio.run(wheel.step("classifier", TRAIN, DEV, request_budget=1, **kwargs))
    assert first["status"] == "paused"
    second = asyncio.run(wheel.step("classifier", TRAIN, DEV, request_budget=30, retry_interrupted=True, **kwargs))
    assert second["status"] == "completed"
    assert wheel.model.calls == 8
    fitted = next(e for e in wheel.history() if e["kind"] == "fit-completed")
    assert fitted["head"]["provenance"]["training_ids"]
    assert next(e for e in wheel.history() if e["kind"] == "fit-started")["rows"]
    wheel.close()


def test_request_preview_is_identical_to_the_sent_messages_and_makes_no_call(tmp_path):
    wheel = make_wheel(tmp_path / "runtime.sqlite", [])
    preview = wheel.preview_optimizer_request("questions", TRAIN, DEV, protected=())
    assert wheel.trace_events()["events"] == []
    asyncio.run(wheel.step("questions", TRAIN, DEV, protected=(), propensities={r.item.id: 1. for r in TRAIN}))
    actual = next(e for e in wheel.history() if e["kind"] == "optimizer-request")
    assert preview["messages"] == actual["messages"]
    assert preview["briefing_fingerprint"] == actual["briefing_fingerprint"]
    wheel.close()


def test_feedback_is_traceable_but_recording_it_does_not_train_or_send_protected_labels(tmp_path):
    from .feedback import FeedbackItem, LABEL_SOURCE_VETTED
    wheel = make_wheel(tmp_path / "runtime.sqlite", [])
    feedback = FeedbackItem("vote-1", "protected-item", TASK.name,
        initial_answer_value="exclude", final_answer_value="include",
        edit_comment_value="Knowledge management", label_source=LABEL_SOURCE_VETTED,
        selection_propensity=1., review_provenance="human-reviewed")
    event = wheel.record_feedback_event(feedback, assignment="final_audit")
    assert event["feedback"]["final_answer_value"] == "include"
    assert event["feedback"]["initial_answer_value"] == "exclude"
    assert event["assignment"] == "final_audit"
    assert wheel.model.calls == 0
    assert wheel.active.head is None
    wheel.record_feedback_event(feedback, action="retracted", assignment="final_audit")
    assert wheel.trace_events()["events"][-1]["action"] == "retracted"
    wheel.close()


def test_zero_decision_budget_can_show_a_proposal_before_any_new_jev_request(tmp_path):
    wheel = make_wheel(tmp_path / "runtime.sqlite", [])
    result = asyncio.run(wheel.step("questions", TRAIN, DEV, protected=(),
        propensities={r.item.id: 1. for r in TRAIN}, request_budget=0))
    assert result["status"] == "paused"
    assert wheel.model.calls == 0
    assert any(e["kind"] == "optimizer-response" for e in wheel.history())
    wheel.close()

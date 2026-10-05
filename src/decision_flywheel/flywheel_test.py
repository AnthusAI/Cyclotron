"""The reusable flywheel must complete the loop without a network client."""
import asyncio

import pytest

from .classifier_config import ClassifiedAnswers, ClassifierConfig
from .flywheel import DecisionFlywheel, development_assignment
from .models import DecisionResult, DecisionTask, Item, LabeledItem
from .optimizer_agent import OptimizerAgent, OptimizerReply


TASK = DecisionTask("inclusion", ("include", "exclude"), "Should this item be included?")
TRAIN = tuple(LabeledItem(Item(f"t{i}", {"text": f"{'yes' if i % 2 else 'no'} train {i}"}),
                          "include" if i % 2 else "exclude", context={"human_feedback": "practical work"})
              for i in range(6))
DEV = tuple(LabeledItem(Item(f"d{i}", {"text": f"{'yes' if i % 2 else 'no'} dev {i}"}),
                        "include" if i % 2 else "exclude") for i in range(2))


def test_development_assignment_is_fixed_before_labels_and_independent_of_arrival_order():
    first = {key: development_assignment("study", key) for key in ("a", "b", "c")}
    assert first == {key: development_assignment("study", key) for key in ("c", "b", "a")}
    with pytest.raises(ValueError):
        development_assignment("study", "a", rate=1.5)


class FakeModel:
    model_identity = "fake-fixed"
    calls = 0
    async def classify(self, config, target, training, *, now=None, event_sink=None):
        self.calls += 1
        p = .98 if target.values["text"].startswith("yes") else .02
        answers = {"decision": DecisionResult("exclude", {"include": .2, "exclude": .8})}
        for task in config.tasks:
            answers[task.name] = DecisionResult("yes" if p > .5 else "no", {"yes": p, "no": 1-p})
        return ClassifiedAnswers(answers, "fake-fixed", {"input_tokens": 10}, 1)


def agent(events):
    return OptimizerAgent(lambda _: OptimizerReply(
        '{"rationale":"Look for practical work","rubric":"Practical research",'
        '"example_ids":["t0","t1"],"tasks":[{"name":"practical",'
        '"instructions":"Is this practical?","labels":["yes","no"]}]}', "fake-optimizer"),
        observer=events.append)


def test_feedback_proposals_become_features_a_fitted_head_and_a_promoted_classifier(tmp_path):
    events = []
    model = FakeModel()
    wheel = DecisionFlywheel(tmp_path / "wheel.sqlite", ClassifierConfig(TASK), model, agent(events),
                             observer=events.append, max_requests=30)
    result = asyncio.run(wheel.improve(TRAIN, DEV, protected=(), propensities={r.item.id: 1.0 for r in TRAIN}))
    assert result["promoted"]
    assert result["candidate"]["brier"] < result["incumbent"]["brier"]
    assert wheel.active.head is not None
    assert wheel.active.config.rubric == "Practical research"
    prediction = asyncio.run(wheel.predict(Item("new", {"text": "yes new"}), TRAIN))
    assert prediction.label == "include"
    assert {e["kind"] for e in events} >= {"optimizer-request", "optimizer-response", "proposal-validated",
                                           "fit-started", "fit-completed", "candidate-evaluated", "promoted"}
    assert wheel.history()[-1]["kind"] == "prediction"
    wheel.close()


def test_restart_restores_the_head_and_does_not_repeat_cached_feature_calls(tmp_path):
    model = FakeModel()
    path = tmp_path / "wheel.sqlite"
    def open_wheel():
        return DecisionFlywheel(path, ClassifierConfig(TASK), model, agent([]), max_requests=30)
    wheel = open_wheel()
    asyncio.run(wheel.improve(TRAIN, DEV, protected=(), propensities={r.item.id: 1.0 for r in TRAIN}))
    target = Item("new", {"text": "yes new"})
    first = asyncio.run(wheel.predict(target, TRAIN))
    fingerprint, calls = wheel.active.fingerprint, model.calls
    wheel.close()
    wheel = open_wheel()
    assert wheel.active.fingerprint == fingerprint
    assert asyncio.run(wheel.predict(target, TRAIN)) == first
    assert model.calls == calls
    assert any(e["kind"] == "optimizer-response" for e in wheel.history())
    wheel.close()


def test_missing_propensities_or_protected_overlap_fail_before_paid_calls(tmp_path):
    model = FakeModel()
    wheel = DecisionFlywheel(tmp_path / "wheel.sqlite", ClassifierConfig(TASK), model, agent([]))
    with pytest.raises(ValueError, match="propensit"):
        asyncio.run(wheel.improve(TRAIN, DEV, protected=(), propensities={}))
    with pytest.raises(ValueError, match="disjoint|protected"):
        asyncio.run(wheel.improve(TRAIN, DEV, protected=(TRAIN[0].item,),
                                 propensities={r.item.id: 1.0 for r in TRAIN}))
    assert model.calls == 0
    wheel.close()


def test_a_failed_candidate_retains_the_active_version_and_has_visible_failure(tmp_path):
    model = FakeModel()
    bad = OptimizerAgent(lambda _: OptimizerReply('{"weights":[1]}', "fake"))
    wheel = DecisionFlywheel(tmp_path / "wheel.sqlite", ClassifierConfig(TASK), model, bad)
    before = wheel.active.fingerprint
    result = asyncio.run(wheel.improve(TRAIN, DEV, protected=(), propensities={r.item.id: 1.0 for r in TRAIN}))
    assert not result["promoted"]
    assert wheel.active.fingerprint == before
    assert wheel.history()[-1]["kind"] == "round-failed"
    wheel.close()


def test_a_corrected_training_label_invalidates_the_dependent_head(tmp_path):
    wheel = DecisionFlywheel(tmp_path / "wheel.sqlite", ClassifierConfig(TASK), FakeModel(), agent([]), max_requests=30)
    asyncio.run(wheel.improve(TRAIN, DEV, protected=(), propensities={r.item.id: 1.0 for r in TRAIN}))
    corrected = (LabeledItem(TRAIN[0].item, "include"), *TRAIN[1:])
    wheel.reconcile_feedback(corrected)
    assert wheel.active.head is None
    assert wheel.history()[-1]["kind"] == "classifier-invalidated"
    wheel.close()


def test_the_same_feedback_round_is_not_paid_for_again_after_restart(tmp_path):
    model = FakeModel()
    path = tmp_path / "wheel.sqlite"
    wheel = DecisionFlywheel(path, ClassifierConfig(TASK), model, agent([]), max_requests=30)
    first = asyncio.run(wheel.improve(TRAIN, DEV, protected=(), propensities={r.item.id: 1 for r in TRAIN}))
    calls = model.calls
    wheel.close()
    events = []
    wheel = DecisionFlywheel(path, ClassifierConfig(TASK), model, agent(events), max_requests=30)
    second = asyncio.run(wheel.improve(TRAIN, DEV, protected=(), propensities={r.item.id: 1 for r in TRAIN}))
    assert second == first
    assert model.calls == calls
    assert not events
    wheel.close()


def test_transcript_events_redact_credentials_before_persistence_and_observation(tmp_path):
    events = []
    wheel = DecisionFlywheel(tmp_path / "wheel.sqlite", ClassifierConfig(TASK), FakeModel(), agent([]),
                             observer=events.append, redact=("credential-value",))
    wheel._emit({"kind": "test", "content": "credential-value"})
    assert events[-1]["content"] == "[REDACTED]"
    assert "credential-value" not in str(wheel.history())
    wheel.close()


def test_correcting_a_development_label_invalidates_the_selected_version(tmp_path):
    wheel = DecisionFlywheel(tmp_path / "wheel.sqlite", ClassifierConfig(TASK), FakeModel(), agent([]), max_requests=30)
    asyncio.run(wheel.improve(TRAIN, DEV, protected=(), propensities={r.item.id: 1 for r in TRAIN}))
    corrected = (LabeledItem(DEV[0].item, "include"), DEV[1])
    wheel.reconcile_feedback(TRAIN, development=corrected)
    assert wheel.active.head is None
    wheel.close()


def test_reverting_a_correction_can_restore_the_original_valid_fit_without_paid_calls(tmp_path):
    model = FakeModel()
    wheel = DecisionFlywheel(tmp_path / "wheel.sqlite", ClassifierConfig(TASK), model, agent([]), max_requests=30)
    asyncio.run(wheel.improve(TRAIN, DEV, protected=(), propensities={r.item.id: 1 for r in TRAIN}))
    original = wheel.active.fingerprint
    wheel.reconcile_feedback((LabeledItem(TRAIN[0].item, "include"), *TRAIN[1:]))
    calls = model.calls
    asyncio.run(wheel.improve(TRAIN, DEV, protected=(), propensities={r.item.id: 1 for r in TRAIN}))
    assert wheel.active.fingerprint == original
    assert model.calls == calls
    wheel.close()

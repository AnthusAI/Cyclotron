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


def test_declared_no_context_capability_rejects_actual_examples_before_a_provider_call(tmp_path):
    from .models import ModelCapabilities
    model = FakeModel()
    model.capabilities = ModelCapabilities(supports_labeled_context=False)
    wheel = DecisionFlywheel(tmp_path/'wheel.sqlite', ClassifierConfig(TASK, example_ids=('t0',)), model, agent([]))
    with pytest.raises(ValueError, match='labeled context'):
        asyncio.run(wheel.predict(DEV[0].item, TRAIN))
    assert model.calls == 0
    assert wheel.requests == 0
    wheel.close()


@pytest.mark.parametrize('examples,target', [((),DEV[0].item), (('t0',),TRAIN[0].item)])
def test_no_context_capability_uses_only_effective_demonstrations_not_the_training_pool(tmp_path, examples, target):
    from .models import ModelCapabilities
    model = FakeModel()
    model.capabilities = ModelCapabilities(supports_labeled_context=False)
    wheel = DecisionFlywheel(tmp_path/'wheel.sqlite', ClassifierConfig(TASK, example_ids=examples), model, agent([]))
    asyncio.run(wheel.predict(target, TRAIN))
    assert model.calls == 1
    wheel.close()


def test_configured_f1_is_recorded_and_used_instead_of_the_default_brier(tmp_path):
    from .selection_policy import SelectionPolicy
    wheel = DecisionFlywheel(tmp_path / 'wheel.sqlite', ClassifierConfig(TASK), FakeModel(), agent([]),
                             selection_policy=SelectionPolicy('f1', positive_class='include'))
    result = asyncio.run(wheel.improve(TRAIN, DEV, protected=(), propensities={r.item.id: 1. for r in TRAIN}))
    assert result['promotion_metric'] == 'f1'
    assert result['selection']['policy']['positive_class'] == 'include'
    assert result['selection']['candidate_scores']['f1'] == 1.
    wheel.close()


def test_selection_configuration_survives_restart_without_a_model_call(tmp_path):
    from .selection_policy import SelectionPolicy
    path = tmp_path / 'wheel.sqlite'
    policy = SelectionPolicy('recall', 'accuracy', positive_class='include')
    wheel = DecisionFlywheel(path, ClassifierConfig(TASK), FakeModel(), agent([]), selection_policy=policy)
    wheel.close()
    model = FakeModel()
    wheel = DecisionFlywheel(path, ClassifierConfig(TASK), model, agent([]))
    assert wheel.selection_policy == policy
    assert model.calls == 0
    wheel.close()


def test_an_old_qualified_trial_cannot_be_promoted_after_the_objective_changes(tmp_path):
    from .selection_policy import SelectionPolicy
    path = tmp_path / 'wheel.sqlite'
    kwargs = dict(protected=(), propensities={r.item.id:1. for r in TRAIN})
    wheel = DecisionFlywheel(path, ClassifierConfig(TASK), FakeModel(), agent([]))
    result = asyncio.run(wheel.improve(TRAIN, DEV, apply_promotion=False, **kwargs))
    assert result['improved']
    wheel.close()
    wheel = DecisionFlywheel(path, ClassifierConfig(TASK), FakeModel(), agent([]),
        selection_policy=SelectionPolicy('f1', positive_class='include'))
    with pytest.raises(ValueError, match='selection policy changed'):
        wheel.promote_trial(result['trial_fingerprint'], TRAIN, DEV)
    wheel.close()


def test_development_assignment_is_fixed_before_labels_and_independent_of_arrival_order():
    first = {key: development_assignment("study", key) for key in ("a", "b", "c")}
    assert first == {key: development_assignment("study", key) for key in ("c", "b", "a")}
    with pytest.raises(ValueError):
        development_assignment("study", "a", rate=1.5)


def test_promotion_waits_for_every_declared_development_class_before_paid_calls(tmp_path):
    model = FakeModel()
    wheel = DecisionFlywheel(tmp_path / "wheel.sqlite", ClassifierConfig(TASK), model, agent([]),
                             evaluation_weighting="equal_class")
    result = asyncio.run(wheel.improve(TRAIN, DEV[:1], protected=(),
                                      propensities={r.item.id: 1. for r in TRAIN}))
    assert not result["promoted"]
    assert result["development_counts"] == {"include": 0, "exclude": 1}
    assert model.calls == 0
    assert not any(e["kind"] == "optimizer-request" for e in wheel.history())
    wheel.close()


def test_equal_class_promotion_records_its_metric_policy_and_both_score_views(tmp_path):
    wheel = DecisionFlywheel(tmp_path / "wheel.sqlite", ClassifierConfig(TASK), FakeModel(), agent([]),
                             evaluation_weighting="equal_class", training_class_weighting="equal_class")
    result = asyncio.run(wheel.improve(TRAIN, DEV, protected=(),
                                      propensities={r.item.id: 1. for r in TRAIN}))
    assert result["promotion_metric"] == "balanced_brier"
    assert result["candidate"]["balanced_accuracy"] == 1.
    assert result["candidate"]["per_class"]["include"]["count"] == 1
    assert wheel.active.head.provenance.training_class_weighting == "equal_class"
    wheel.close()


def test_an_isolated_trial_can_be_measured_without_changing_the_shared_incumbent(tmp_path):
    wheel = DecisionFlywheel(tmp_path / "wheel.sqlite", ClassifierConfig(TASK), FakeModel(), agent([]))
    before = wheel.active.fingerprint
    result = asyncio.run(wheel.improve(TRAIN, DEV, protected=(), propensities={r.item.id: 1. for r in TRAIN},
                                      apply_promotion=False))
    assert result["improved"]
    assert not result["promoted"]
    assert wheel.active.fingerprint == before
    wheel.promote_trial(result["trial_fingerprint"], TRAIN, DEV)
    assert wheel.active.fingerprint != before
    assert wheel.active.head is not None
    wheel.close()


@pytest.mark.parametrize("policy,promoted", [("equal_class", True), ("natural", False)])
def test_promotion_uses_the_selected_score_even_when_the_two_views_disagree(tmp_path, policy, promoted):
    wheel = DecisionFlywheel(tmp_path / "wheel.sqlite", ClassifierConfig(TASK), FakeModel(), agent([]),
                             evaluation_weighting=policy)
    scores = iter([{"brier": .2, "balanced_brier": .7}, {"brier": .3, "balanced_brier": .4}])
    async def score(*args):
        return next(scores)
    wheel._score = score
    result = asyncio.run(wheel.improve(TRAIN, DEV, protected=(),
                                      propensities={r.item.id: 1. for r in TRAIN}))
    assert result["promoted"] is promoted
    wheel.close()


def test_changing_evaluation_policy_does_not_reuse_an_old_promotion_decision(tmp_path):
    path = tmp_path / "wheel.sqlite"
    kwargs = dict(protected=(), propensities={r.item.id: 1. for r in TRAIN})
    wheel = DecisionFlywheel(path, ClassifierConfig(TASK), FakeModel(), agent([]), evaluation_weighting="natural")
    assert asyncio.run(wheel.improve(TRAIN, DEV, **kwargs))["promotion_metric"] == "brier"
    wheel.close()
    wheel = DecisionFlywheel(path, ClassifierConfig(TASK), FakeModel(), agent([]), evaluation_weighting="equal_class")
    result = asyncio.run(wheel.improve(TRAIN, DEV, **kwargs))
    assert result["promotion_metric"] == "balanced_brier"
    assert sum(event["kind"] == "optimizer-request" for event in wheel.history(1000)) == 2
    wheel.close()


def test_an_interrupted_optimizer_round_is_visible_and_requires_an_explicit_retry(tmp_path):
    path = tmp_path / "wheel.sqlite"
    def interrupt(_):
        raise KeyboardInterrupt
    wheel = DecisionFlywheel(path, ClassifierConfig(TASK), FakeModel(), OptimizerAgent(interrupt))
    kwargs = dict(protected=(), propensities={row.item.id: 1.0 for row in TRAIN})
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(wheel.improve(TRAIN, DEV, **kwargs))
    wheel.close()
    wheel = DecisionFlywheel(path, ClassifierConfig(TASK), FakeModel(), agent([]))
    result = asyncio.run(wheel.improve(TRAIN, DEV, **kwargs))
    assert "retry" in result["reason"]
    assert wheel.history()[-1]["kind"] == "round-interrupted"
    assert asyncio.run(wheel.improve(TRAIN, DEV, retry_interrupted=True, **kwargs))["promoted"]
    wheel.close()


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
    recorded=wheel.history()[-1]
    assert recorded['confidence'] == recorded['probabilities'][prediction.label]
    assert set(recorded['uncalibrated_probabilities']) == set(TASK.labels)
    assert set(recorded['ml_features']) == set(wheel.active.head.feature_names)
    assert recorded['calibration_provenance']['fit_on']=='out_of_fold'
    assert set(recorded['calibration_provenance']['training_ids'])==set(wheel.active.head.provenance.training_ids)
    assert not set(recorded['calibration_provenance']['training_ids']).intersection(row.item.id for row in DEV)
    assert recorded['calibration_temperature']==wheel.active.head.calibration.temperature
    assert recorded['decision_model_probabilities'] == {'include':.2,'exclude':.8}
    assert {e["kind"] for e in events} >= {"optimizer-request", "optimizer-response", "proposal-validated",
                                           "fit-started", "fit-completed", "candidate-evaluated", "promoted"}
    assert wheel.history()[-1]["kind"] == "prediction"
    wheel.close()


def test_a_rejected_hypothesis_survives_restart_and_can_be_refitted_without_another_optimizer_call(tmp_path):
    path = tmp_path / "wheel.sqlite"
    model = FakeModel()
    wheel = DecisionFlywheel(path, ClassifierConfig(TASK), model, agent([]))
    original_score = wheel._score
    async def reject(*args):
        score = await original_score(*args)
        score["balanced_brier"] = .5
        return score
    wheel._score = reject
    kwargs = dict(protected=(), propensities={r.item.id: 1. for r in TRAIN})
    assert not asyncio.run(wheel.improve(TRAIN, DEV, **kwargs))["promoted"]
    hypothesis = wheel.hypotheses()[0]
    assert hypothesis["attempts"][-1]["outcome"] == "candidate-rejected"
    wheel.close()
    def forbidden(_):
        raise AssertionError("retrying a retained hypothesis must not rediscover it through the optimizer")
    wheel = DecisionFlywheel(path, ClassifierConfig(TASK), model, OptimizerAgent(forbidden))
    extra = LabeledItem(Item("later", {"text": "yes later"}), "include")
    training = (*TRAIN, extra)
    result = asyncio.run(wheel.retry_hypothesis(hypothesis["id"], training, DEV, protected=(),
                                              propensities={row.item.id: 1. for row in training}))
    assert result["promoted"]
    assert len(wheel.hypotheses()[0]["attempts"]) == 2
    assert sum(e["kind"] == "optimizer-request" for e in wheel.history(1000)) == 1
    calls = model.calls
    asyncio.run(wheel.retry_hypothesis(hypothesis["id"], training, DEV, protected=(),
                                      propensities={row.item.id: 1. for row in training}))
    assert model.calls == calls
    assert len(wheel.hypotheses()[0]["attempts"]) == 2
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


def test_a_legacy_fit_is_retained_in_history_but_not_served_with_invented_response_provenance(tmp_path):
    import json
    from dataclasses import asdict
    from .candidate_fitting import fit_candidate
    from .flywheel import _restore
    config = ClassifierConfig(TASK, rubric='Practical research', example_ids=('t0', 't1'),
        tasks=(DecisionTask('practical', ('yes', 'no'), 'Is this practical?'),))
    path = tmp_path/'wheel.sqlite'
    model = FakeModel()
    wheel = DecisionFlywheel(path, config, model, agent([]))
    fitted, _ = asyncio.run(fit_candidate(wheel, config, TRAIN, DEV, protected=(),
        propensities={r.item.id: 1. for r in TRAIN}, validation_status='evaluated'))
    legacy = asdict(fitted)
    legacy.pop('answer_dependencies')
    encoded = json.dumps(legacy)
    legacy_version = _restore(json.loads(encoded)).fingerprint
    with wheel.db:
        wheel.db.execute("INSERT OR REPLACE INTO runtime_state VALUES ('active', ?)", (encoded,))
        wheel.db.execute("INSERT INTO runtime_rounds VALUES ('legacy-history', 'complete', ?)", (encoded,))
    wheel.close()
    calls = model.calls
    wheel = DecisionFlywheel(path, config, model, agent([]))
    try:
        assert wheel.active.fingerprint == legacy_version
        assert not wheel.active.answer_dependencies
        assert wheel.reconcile_model_context(TRAIN)
        assert wheel.active.head is None
        assert wheel.active.config == config
        assert model.calls == calls
        assert wheel.db.execute("SELECT payload FROM runtime_rounds WHERE key='legacy-history'").fetchone()[0] == encoded
        event = next(e for e in reversed(wheel.history()) if e['kind'] == 'head-invalidated')
        assert event['reason'] == 'decision response dependencies were not recorded'
        assert event['previous_classifier_snapshot']['head'] == json.loads(encoded)['head']
    finally:
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

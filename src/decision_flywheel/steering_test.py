import json

import pytest

from .context import RandomBalanced
from .feedback import Element, Feature, FeedbackItem, LABEL_SOURCE_FINAL, Cyclotron
from .head import HeadRow, fit_learned_head
from .models import DecisionTask
from .steering import ScriptedMockManager, run_steering_round, steering_observations


HASH = "a" * 64


def _cyclotron(policy=None):
    policy = policy or RandomBalanced(3)
    return Cyclotron("review", 1, (Element("tone", "binary", ("tone",), HASH),), policy.fingerprint)


def _scripted_factory(manager):
    """The analyst can only be built with the installed scripted client."""
    def parse(briefing):
        assert briefing.developer_ids == ("train-1",)
        assert "current_datetime" in briefing.dynamic_element_instruction
        return json.loads(manager.next_reply())

    return parse


def _head_for(cyclotron, policy, features):
    task = DecisionTask("review", ("approve", "reject"), "Choose.")
    rows = tuple(
        HeadRow(
            f"fit-{index}",
            FeedbackItem(f"feedback-{index}", f"fit-{index}", "review", final_answer_value=label,
                         label_source=LABEL_SOURCE_FINAL, selection_propensity=0.5),
            tuple(Feature(name, 1.0 if label == "approve" else -1.0, name) for name in features),
        )
        for index, label in enumerate(("approve", "reject", "approve", "reject"), 1)
    )
    return fit_learned_head(
        task, rows, declared_features=features, development_ids=("dev",), scoreboard_ids=("score",),
        cyclotron_fingerprint=cyclotron.fingerprint, policy_fingerprint=policy.fingerprint,
        context_artifact_fingerprint="c" * 64, source_model_provenance="offline", folds=2,
    )


def test_a_scripted_analyst_is_built_with_the_installed_mock_and_only_human_accepted_development_promotion_applies():
    fitted = []
    outcome = run_steering_round(
        _cyclotron(), RandomBalanced(3), analyst_factory=_scripted_factory,
        mock_manager=ScriptedMockManager([json.dumps({"add_element": {"key": "clarity", "question_type": "binary", "features": ["clarity"]}})]),
        developer_ids=("train-1",), developer_hashes=(HASH,), protected_ids=("score",),
        protected_hashes=("c" * 64,), human_accept=lambda proposal: True,
        development_objective=lambda candidate, candidate_policy, refit: 0.8, incumbent_development_objective=0.7,
        numerical_fitter=lambda candidate, candidate_policy, features: fitted.append((candidate, candidate_policy, features)) or _head_for(candidate, candidate_policy, features),
    )
    assert outcome.promoted and outcome.accepted and outcome.cyclotron.version == 2
    assert fitted[0][2] == ("tone", "clarity")
    assert outcome.history[-1].decision == "promoted"


def test_an_accepted_datetime_proposal_becomes_an_allowlisted_dynamic_cyclotron_element():
    outcome = run_steering_round(
        _cyclotron(), RandomBalanced(3), analyst_factory=_scripted_factory,
        mock_manager=ScriptedMockManager([json.dumps({"add_programmatic_element": {"kind": "current_datetime"}})]),
        developer_ids=("train-1",), developer_hashes=(HASH,), protected_ids=(), protected_hashes=(),
        human_accept=lambda proposal: proposal.add_programmatic_element == "current_datetime",
        development_objective=lambda candidate, policy, refit: .8, incumbent_development_objective=.7,
        numerical_fitter=lambda candidate, policy, features: _head_for(candidate, policy, features),
    )

    element = outcome.cyclotron.elements[-1]
    assert outcome.promoted
    assert (element.key, element.question_type, element.feature_names) == (
        "current_datetime", "programmatic_datetime", ("current_datetime",)
    )
    features, activity = steering_observations(outcome)
    assert [feature.key for feature in features] == ["tone", "current_datetime"]
    assert activity[0].feature_key == "current_datetime"
    assert activity[0].status == "promoted"


def test_rejected_or_disallowed_proposals_preserve_rollback_lineage_and_cannot_write_numbers():
    rejected = run_steering_round(
        _cyclotron(), RandomBalanced(3), analyst_factory=_scripted_factory,
        mock_manager=ScriptedMockManager([json.dumps({"add_element": {"key": "clarity", "question_type": "binary", "features": ["clarity"]}})]),
        developer_ids=("train-1",), developer_hashes=(HASH,), protected_ids=(), protected_hashes=(),
        human_accept=lambda proposal: False, development_objective=lambda candidate, policy, refit: 1.0,
        incumbent_development_objective=0.0, numerical_fitter=lambda candidate: pytest.fail("must not fit"),
    )
    assert not rejected.promoted and rejected.cyclotron == _cyclotron()
    assert rejected.history[-1].decision == "rejected"
    with pytest.raises(ValueError, match="not allowed"):
        run_steering_round(
            _cyclotron(), RandomBalanced(3), analyst_factory=_scripted_factory,
            mock_manager=ScriptedMockManager([json.dumps({"weights": {"approve": 1}})]),
            developer_ids=("train-1",), developer_hashes=(HASH,), protected_ids=(), protected_hashes=(),
            human_accept=lambda proposal: True, development_objective=lambda candidate, policy, refit: 1.0,
            incumbent_development_objective=0.0, numerical_fitter=lambda candidate, policy, features: _head_for(candidate, policy, features),
        )


def test_scoreboard_ids_or_hashes_never_enter_analyst_briefing_and_cannot_drive_promotion():
    with pytest.raises(ValueError, match="protected"):
        run_steering_round(
            _cyclotron(), RandomBalanced(3), analyst_factory=_scripted_factory,
            mock_manager=ScriptedMockManager(["{}"]), developer_ids=("score-1",), developer_hashes=(HASH,),
            protected_ids=("score-1",), protected_hashes=(), human_accept=lambda proposal: True,
            development_objective=lambda candidate, policy, refit: 1.0, incumbent_development_objective=0.0,
            numerical_fitter=lambda candidate, policy, features: _head_for(candidate, policy, features),
        )


def test_policy_edit_creates_a_policy_bound_cyclotron_child_and_evaluates_its_fitted_head():
    incumbent = RandomBalanced(3)
    candidate_policy = RandomBalanced(9)
    fitted = []
    evaluated = []
    outcome = run_steering_round(
        _cyclotron(), incumbent, analyst_factory=_scripted_factory,
        mock_manager=ScriptedMockManager([json.dumps({"context_policy": "seed-nine"})]),
        developer_ids=("train-1",), developer_hashes=(HASH,), protected_ids=(), protected_hashes=(),
        human_accept=lambda proposal: True, incumbent_development_objective=0.2,
        allowed_policies={"seed-nine": candidate_policy},
        numerical_fitter=lambda cyclotron, policy, features: fitted.append((cyclotron, policy, features)) or _head_for(cyclotron, policy, features),
        development_objective=lambda cyclotron, policy, refit: evaluated.append((cyclotron, policy, refit)) or 0.3,
    )
    assert outcome.promoted and outcome.policy is candidate_policy
    assert outcome.cyclotron.policy_fingerprint == candidate_policy.fingerprint
    assert outcome.cyclotron.parent_fingerprint == _cyclotron().fingerprint
    assert outcome.cyclotron.fingerprint != _cyclotron().fingerprint
    assert fitted[0][0] == outcome.cyclotron and fitted[0][1] is candidate_policy
    assert evaluated == [(outcome.cyclotron, candidate_policy, outcome.numerical_refit)]


def test_fit_failure_never_promotes_and_records_a_safe_reason():
    outcome = run_steering_round(
        _cyclotron(), RandomBalanced(3), analyst_factory=_scripted_factory,
        mock_manager=ScriptedMockManager([json.dumps({"add_element": {"key": "clarity", "question_type": "binary", "features": ["clarity"]}})]),
        developer_ids=("train-1",), developer_hashes=(HASH,), protected_ids=(), protected_hashes=(),
        human_accept=lambda proposal: True, incumbent_development_objective=0.2,
        numerical_fitter=lambda cyclotron, policy, features: (_ for _ in ()).throw(ValueError("missing coverage")),
        development_objective=lambda cyclotron, policy, refit: pytest.fail("must not evaluate an unfit candidate"),
    )
    assert not outcome.promoted and outcome.cyclotron == _cyclotron()
    assert outcome.history[-1].decision == "fit-failed"
    assert outcome.history[-1].reason == "ValueError"


def test_candidate_addition_is_not_promoted_when_the_core_head_omits_its_new_feature():
    outcome = run_steering_round(
        _cyclotron(), RandomBalanced(3), analyst_factory=_scripted_factory,
        mock_manager=ScriptedMockManager([json.dumps({"add_element": {"key": "clarity", "question_type": "binary", "features": ["clarity"]}})]),
        developer_ids=("train-1",), developer_hashes=(HASH,), protected_ids=(), protected_hashes=(),
        human_accept=lambda proposal: True, incumbent_development_objective=0.2,
        numerical_fitter=lambda cyclotron, policy, features: _head_for(cyclotron, policy, ("tone",)),
        development_objective=lambda cyclotron, policy, refit: pytest.fail("must not evaluate incomplete fit"),
    )
    assert not outcome.promoted
    assert outcome.history[-1].decision == "fit-failed"
    assert outcome.history[-1].reason == "ValueError"


@pytest.mark.parametrize("reply", [1, "yes"])
def test_human_review_requires_an_exact_boolean(reply):
    with pytest.raises(ValueError, match="exact boolean"):
        run_steering_round(
            _cyclotron(), RandomBalanced(3), analyst_factory=_scripted_factory,
            mock_manager=ScriptedMockManager([json.dumps({"context_policy": "seed-nine"})]),
            developer_ids=("train-1",), developer_hashes=(HASH,), protected_ids=(), protected_hashes=(),
            human_accept=lambda proposal: reply, incumbent_development_objective=0.2,
            allowed_policies={"seed-nine": RandomBalanced(9)},
            numerical_fitter=lambda cyclotron, policy, features: _head_for(cyclotron, policy, features),
            development_objective=lambda cyclotron, policy, refit: 0.3,
        )


@pytest.mark.parametrize("objective", [float("nan"), float("inf"), True])
def test_non_finite_or_boolean_objectives_cannot_promote(objective):
    with pytest.raises(ValueError, match="development objective"):
        run_steering_round(
            _cyclotron(), RandomBalanced(3), analyst_factory=_scripted_factory,
            mock_manager=ScriptedMockManager([json.dumps({"context_policy": "seed-nine"})]),
            developer_ids=("train-1",), developer_hashes=(HASH,), protected_ids=(), protected_hashes=(),
            human_accept=lambda proposal: True, incumbent_development_objective=0.2,
            allowed_policies={"seed-nine": RandomBalanced(9)},
            numerical_fitter=lambda cyclotron, policy, features: _head_for(cyclotron, policy, features),
            development_objective=lambda cyclotron, policy, refit: objective,
        )


def test_initial_cyclotron_policy_lineage_must_agree():
    with pytest.raises(ValueError, match="policy fingerprint"):
        run_steering_round(
            _cyclotron(RandomBalanced(3)), RandomBalanced(9), analyst_factory=_scripted_factory,
            mock_manager=ScriptedMockManager([]), developer_ids=(), developer_hashes=(),
            protected_ids=(), protected_hashes=(), human_accept=lambda proposal: True,
            incumbent_development_objective=0.2,
            numerical_fitter=lambda cyclotron, policy, features: _head_for(cyclotron, policy, features),
            development_objective=lambda cyclotron, policy, refit: 0.3,
        )


def test_scripted_factory_is_installed_before_construction_so_an_accepted_flow_never_builds_a_paid_client():
    paid_client_constructions = []

    class PaidClient:
        def __init__(self):
            paid_client_constructions.append("constructed")
            pytest.fail("scripted analyst construction must not reach a paid client")

    def factory(scripted_client):
        if not scripted_client.installed:
            PaidClient()

        def parse(briefing):
            return json.loads(scripted_client.next_reply())

        return parse

    outcome = run_steering_round(
        _cyclotron(), RandomBalanced(3), analyst_factory=factory,
        mock_manager=ScriptedMockManager([json.dumps({"context_policy": "seed-nine"})]),
        developer_ids=("train-1",), developer_hashes=(HASH,), protected_ids=(), protected_hashes=(),
        human_accept=lambda proposal: True, incumbent_development_objective=0.2,
        allowed_policies={"seed-nine": RandomBalanced(9)},
        numerical_fitter=lambda cyclotron, policy, features: _head_for(cyclotron, policy, features),
        development_objective=lambda cyclotron, policy, refit: 0.3,
    )
    assert outcome.promoted
    assert paid_client_constructions == []


def test_protected_scoreboard_ids_must_be_recorded_and_excluded_from_the_refit_training_ids():
    def fitter(cyclotron, policy, features):
        return _head_for(cyclotron, policy, features)

    outcome = run_steering_round(
        _cyclotron(), RandomBalanced(3), analyst_factory=_scripted_factory,
        mock_manager=ScriptedMockManager([json.dumps({"context_policy": "seed-nine"})]),
        developer_ids=("train-1",), developer_hashes=(HASH,), protected_ids=("score-hidden",), protected_hashes=(),
        human_accept=lambda proposal: True, incumbent_development_objective=0.2,
        allowed_policies={"seed-nine": RandomBalanced(9)}, numerical_fitter=fitter,
        development_objective=lambda cyclotron, policy, refit: 0.3,
    )
    assert not outcome.promoted
    assert outcome.history[-1].decision == "fit-failed"
    assert outcome.history[-1].reason == "ValueError"


def test_a_core_head_that_trains_on_a_protected_id_is_rejected_even_if_it_omits_that_scoreboard_metadata():
    task = DecisionTask("review", ("approve", "reject"), "Choose.")

    def protected_training_fitter(cyclotron, policy, features):
        rows = tuple(
            HeadRow(
                item_id,
                FeedbackItem(f"feedback-{index}", item_id, "review", final_answer_value=label,
                             label_source=LABEL_SOURCE_FINAL, selection_propensity=0.5),
                (Feature("tone", 1.0 if label == "approve" else -1.0, "tone"),),
            )
            for index, (item_id, label) in enumerate(
                (("score-hidden", "approve"), ("fit-2", "reject"), ("fit-3", "approve"), ("fit-4", "reject")), 1
            )
        )
        return fit_learned_head(
            task, rows, declared_features=features, development_ids=(), scoreboard_ids=(),
            cyclotron_fingerprint=cyclotron.fingerprint, policy_fingerprint=policy.fingerprint,
            context_artifact_fingerprint="c" * 64, source_model_provenance="offline", folds=2,
        )

    outcome = run_steering_round(
        _cyclotron(), RandomBalanced(3), analyst_factory=_scripted_factory,
        mock_manager=ScriptedMockManager([json.dumps({"context_policy": "seed-nine"})]),
        developer_ids=("train-1",), developer_hashes=(HASH,), protected_ids=("score-hidden",), protected_hashes=(),
        human_accept=lambda proposal: True, incumbent_development_objective=0.2,
        allowed_policies={"seed-nine": RandomBalanced(9)}, numerical_fitter=protected_training_fitter,
        development_objective=lambda cyclotron, policy, refit: pytest.fail("must not evaluate protected training"),
    )
    assert not outcome.promoted
    assert outcome.history[-1].decision == "fit-failed"

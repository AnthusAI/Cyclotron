import pytest

from .feedback import (
    LABEL_SOURCE_FINAL,
    LABEL_SOURCE_SCORE_RESULT_OR_IMPORTED,
    LABEL_SOURCE_UNRESOLVED,
    Decision,
    Element,
    Feature,
    FeatureCoverage,
    FeedbackItem,
    ScoreResult,
    Scorecard,
)
from .models import DecisionTask


TASK = DecisionTask("review", ("approve", "reject"), "Choose an outcome.")
HASH = "a" * 64


def test_a_review_correction_preserves_initial_final_comment_source_and_propensity():
    feedback = FeedbackItem(
        id="feedback-1", item_id="item-1", score_name="review",
        initial_answer_value="Reject", final_answer_value="Approve!",
        edit_comment_value="The policy exception applies.", label_source=LABEL_SOURCE_FINAL,
        selection_propensity=0.25,
    )

    assert feedback.initial_answer_value == "Reject"
    assert feedback.final_answer_value == "Approve!"
    assert feedback.edit_comment_value == "The policy exception applies."
    assert feedback.trusted_label(TASK) == "approve"
    assert feedback.selection_propensity == 0.25


def test_unverified_or_missing_propensity_feedback_never_becomes_a_trusted_training_label():
    unverified = FeedbackItem(
        id="feedback-1", item_id="item-1", score_name="review", final_answer_value="approve",
        label_source=LABEL_SOURCE_SCORE_RESULT_OR_IMPORTED, selection_propensity=0.5,
    )
    unresolved = FeedbackItem(
        id="feedback-2", item_id="item-2", score_name="review", final_answer_value="approve",
        label_source=LABEL_SOURCE_UNRESOLVED,
    )

    assert unverified.trusted_label(TASK) is None
    assert unresolved.trusted_label(TASK) is None
    with pytest.raises(ValueError, match="selection_propensity"):
        FeedbackItem(id="feedback-3", item_id="item-3", score_name="review",
                     final_answer_value="approve", label_source=LABEL_SOURCE_FINAL,
                     selection_propensity=None).training_label(TASK)


@pytest.mark.parametrize("propensity", [0, 1.1, True, float("inf")])
def test_a_feedback_propensity_must_be_a_finite_probability(propensity):
    with pytest.raises(ValueError, match="selection_propensity"):
        FeedbackItem(id="feedback", item_id="item", score_name="review", selection_propensity=propensity)


def test_decision_lineage_links_context_request_scorecard_and_complete_features():
    coverage = FeatureCoverage(("tone.positive", "policy.exception"), ("tone.positive", "policy.exception"))
    decision = Decision(
        value="approve", model_provenance="local-rule-model-v1", policy_fingerprint=HASH,
        context_artifact_fingerprint="b" * 64, request_fingerprint="c" * 64,
        scorecard_fingerprint="d" * 64, feature_coverage=coverage,
    )
    result = ScoreResult(item_id="item-1", score_name="review", decision=decision)

    assert result.value == "approve"
    assert result.decision.feature_coverage.complete
    assert result.decision.model_provenance == "local-rule-model-v1"


def test_incomplete_or_non_finite_features_are_rejected_before_a_learned_decision_can_use_them():
    with pytest.raises(ValueError, match="coverage"):
        FeatureCoverage(("a", "b"), ("a",))
    with pytest.raises(ValueError, match="finite"):
        Feature("tone.positive", float("nan"), "tone")


def test_scorecard_lineage_changes_when_its_policy_or_parent_changes():
    element = Element("tone", "choice", ("tone.positive",), definition_fingerprint=HASH)
    first = Scorecard("review", 1, (element,), policy_fingerprint="b" * 64)
    changed_policy = Scorecard("review", 1, (element,), policy_fingerprint="c" * 64)
    child = Scorecard("review", 2, (element,), policy_fingerprint="b" * 64,
                      parent_fingerprint=first.fingerprint)

    assert first.fingerprint != changed_policy.fingerprint
    assert child.parent_fingerprint == first.fingerprint
    assert child.fingerprint != first.fingerprint

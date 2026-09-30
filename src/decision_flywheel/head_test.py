import math

import pytest

from .feedback import LABEL_SOURCE_FINAL, LABEL_SOURCE_SCORE_RESULT_OR_IMPORTED, Feature, FeedbackItem
from .head import HeadRow, OutOfFoldPredictions, calibrate, fit_learned_head
from .models import DecisionTask


TASK = DecisionTask("review", ("approve", "reject"), "Choose one.")
HASH = "a" * 64


def _row(index, label, *, propensity=0.5, source=LABEL_SOURCE_FINAL, missing=False):
    return HeadRow(
        item_id=f"item-{index}",
        feedback=FeedbackItem(f"feedback-{index}", f"item-{index}", "review", final_answer_value=label,
                              label_source=source, selection_propensity=propensity),
        features=(Feature("signal", 1.0 if label == "approve" else -1.0, "rule"),) if not missing else (),
    )


def _rows():
    return tuple(_row(index, "approve" if index % 2 else "reject", propensity=0.25 if index == 1 else 0.5)
                 for index in range(1, 13))


def test_a_learned_head_uses_only_trusted_full_coverage_rows_and_records_inverse_propensity_provenance():
    result = fit_learned_head(
        TASK, _rows(), declared_features=("signal",), development_ids=("dev-1",), scoreboard_ids=("score-1",),
        scorecard_fingerprint=HASH, policy_fingerprint="b" * 64,
        context_artifact_fingerprint="c" * 64, source_model_provenance="generic-local-head-v1", folds=3,
    )

    assert result.provenance.training_ids == tuple(f"item-{index}" for index in range(1, 13))
    assert result.provenance.development_ids == ("dev-1",)
    assert result.provenance.scoreboard_ids == ("score-1",)
    assert result.provenance.weights[0] > result.provenance.weights[1]
    assert result.calibration.fit_on == "out_of_fold"
    assert result.refitted_scorecard_fingerprint != HASH
    assert result.predict({"signal": 1.0}) == "approve"


def test_untrusted_missing_propensity_or_missing_feature_rows_are_refused_not_guessed():
    common = dict(declared_features=("signal",), development_ids=(), scoreboard_ids=(),
                  scorecard_fingerprint=HASH, policy_fingerprint="b" * 64,
                  context_artifact_fingerprint="c" * 64, source_model_provenance="generic")
    with pytest.raises(ValueError, match="trusted"):
        fit_learned_head(TASK, (_row(1, "approve", source=LABEL_SOURCE_SCORE_RESULT_OR_IMPORTED),), **common)
    with pytest.raises(ValueError, match="selection_propensity"):
        bad = HeadRow("item-x", FeedbackItem("feedback-x", "item-x", "review", final_answer_value="approve",
                                              label_source=LABEL_SOURCE_FINAL), (Feature("signal", 1, "rule"),))
        fit_learned_head(TASK, (bad,), **common)
    with pytest.raises(ValueError, match="full feature coverage"):
        fit_learned_head(TASK, (_row(1, "approve", missing=True),), **common)


def test_intercept_is_reserved_for_the_internal_bias_not_a_declared_feature():
    with pytest.raises(ValueError, match="reserved"):
        fit_learned_head(
            TASK, _rows(), declared_features=("intercept",), development_ids=(), scoreboard_ids=(),
            scorecard_fingerprint=HASH, policy_fingerprint="b" * 64,
            context_artifact_fingerprint="c" * 64, source_model_provenance="generic",
        )


def test_scoreboard_ids_and_split_ids_cannot_leak_into_training_or_each_other():
    with pytest.raises(ValueError, match="disjoint"):
        fit_learned_head(TASK, _rows(), declared_features=("signal",), development_ids=("item-1",),
                         scoreboard_ids=(), scorecard_fingerprint=HASH, policy_fingerprint="b" * 64,
                         context_artifact_fingerprint="c" * 64, source_model_provenance="generic")
    with pytest.raises(ValueError, match="disjoint"):
        fit_learned_head(TASK, _rows(), declared_features=("signal",), development_ids=("dev",),
                         scoreboard_ids=("dev",), scorecard_fingerprint=HASH, policy_fingerprint="b" * 64,
                         context_artifact_fingerprint="c" * 64, source_model_provenance="generic")


def test_calibration_refuses_in_sample_predictions_and_fit_is_reproducible():
    with pytest.raises(ValueError, match="out-of-fold"):
        OutOfFoldPredictions(
            classes=("approve", "reject"), item_ids=("held",), logits=((1.0, 0.0),),
            labels=("approve",), weights=(1.0,), folds=(0,), fit_ids=((),), fit_labels=((),),
            normalization_fit_ids=((),), normalizers=({},),
            origin="in_sample",
        )
    args = dict(declared_features=("signal",), development_ids=(), scoreboard_ids=(),
                scorecard_fingerprint=HASH, policy_fingerprint="b" * 64,
                context_artifact_fingerprint="c" * 64, source_model_provenance="generic", folds=3)
    first = fit_learned_head(TASK, _rows(), **args)
    second = fit_learned_head(TASK, _rows(), **args)

    assert first.weights == second.weights
    assert first.calibration == second.calibration
    assert calibrate(first.out_of_fold).fit_on == "out_of_fold"


def test_refitted_head_fingerprint_binds_text_free_task_and_training_provenance():
    args = dict(declared_features=("signal",), development_ids=(), scoreboard_ids=(),
                scorecard_fingerprint=HASH, policy_fingerprint="b" * 64,
                context_artifact_fingerprint="c" * 64, source_model_provenance="generic", folds=3)
    baseline = fit_learned_head(TASK, _rows(), **args)
    changed_context = fit_learned_head(TASK, _rows(), **{**args, "context_artifact_fingerprint": "d" * 64})
    changed_source = fit_learned_head(TASK, _rows(), **{**args, "source_model_provenance": "another-generic"})
    changed_task = fit_learned_head(DecisionTask("review", ("approve", "reject"), "Different instructions."), _rows(), **args)
    uniformly_reselected = tuple(_row(index, "approve" if index % 2 else "reject", propensity=1.0)
                                  for index in range(1, 13))
    changed_propensities = fit_learned_head(TASK, uniformly_reselected, **args)

    assert len({baseline.refitted_scorecard_fingerprint, changed_context.refitted_scorecard_fingerprint,
                changed_source.refitted_scorecard_fingerprint, changed_task.refitted_scorecard_fingerprint,
                changed_propensities.refitted_scorecard_fingerprint}) == 5


def test_multiclass_oof_temperature_uses_full_logits_and_changes_serving_probabilities():
    task = DecisionTask("triage", ("alpha", "beta", "gamma"), "Choose one.")
    rows = tuple(
        HeadRow(
            item_id=f"triage-{index}",
            feedback=FeedbackItem(f"feedback-{index}", f"triage-{index}", "triage",
                                  final_answer_value=label, label_source=LABEL_SOURCE_FINAL,
                                  selection_propensity=0.01 if index == 1 else 0.5),
            features=(Feature("signal", value, "rule"),),
        )
        for index, (label, value) in enumerate(
            (("alpha", 3.0), ("beta", -3.0), ("gamma", 0.0)) * 4, start=1
        )
    )
    result = fit_learned_head(
        task, rows, declared_features=("signal",), development_ids=(), scoreboard_ids=(),
        scorecard_fingerprint=HASH, policy_fingerprint="b" * 64,
        context_artifact_fingerprint="c" * 64, source_model_provenance="generic", folds=3,
    )

    probabilities = result.probabilities({"signal": 3.0})
    uncalibrated = result.uncalibrated_probabilities({"signal": 3.0})
    assert set(probabilities) == {"alpha", "beta", "gamma"}
    assert math.isclose(sum(probabilities.values()), 1.0)
    assert result.calibration.temperature != 1.0
    assert probabilities != uncalibrated
    assert all(len(logits) == 3 for logits in result.out_of_fold.logits)


def test_oof_partition_proves_each_held_item_was_not_fit_and_each_fit_has_all_classes():
    result = fit_learned_head(
        TASK, _rows(), declared_features=("signal",), development_ids=(), scoreboard_ids=(),
        scorecard_fingerprint=HASH, policy_fingerprint="b" * 64,
        context_artifact_fingerprint="c" * 64, source_model_provenance="generic", folds=3,
    )
    oof = result.out_of_fold

    assert tuple(oof.item_ids) == tuple(f"item-{index}" for index in range(1, 13))
    assert all(item_id not in fit_ids for item_id, fit_ids in zip(oof.item_ids, oof.fit_ids))
    assert all(set(labels) == set(TASK.labels) for labels in oof.fit_labels)
    assert oof.folds == (0, 0, 1, 1, 2, 2, 0, 0, 1, 1, 2, 2)


def test_oof_rejects_leakage_missing_class_coverage_and_nonfinite_predictions():
    common = dict(classes=("approve", "reject"), item_ids=("held",), logits=((1.0, 0.0),),
                  labels=("approve",), weights=(1.0,), folds=(0,))
    with pytest.raises(ValueError, match="held"):
        OutOfFoldPredictions(**common, fit_ids=(("held",),), fit_labels=(("approve", "reject"),),
                             normalization_fit_ids=(("held",),), normalizers=({},))
    with pytest.raises(ValueError, match="coverage"):
        OutOfFoldPredictions(**common, fit_ids=(("train",),), fit_labels=(("approve",),),
                             normalization_fit_ids=(("train",),), normalizers=({},))
    with pytest.raises(ValueError, match="finite"):
        OutOfFoldPredictions(**{**common, "logits": ((float("nan"), 0.0),)},
                             fit_ids=(("train",),), fit_labels=(("approve", "reject"),),
                             normalization_fit_ids=(("train",),), normalizers=({},))


def test_oof_normalization_is_fit_only_on_each_partition_not_the_held_feature_value():
    baseline = fit_learned_head(
        TASK, _rows(), declared_features=("signal",), development_ids=(), scoreboard_ids=(),
        scorecard_fingerprint=HASH, policy_fingerprint="b" * 64,
        context_artifact_fingerprint="c" * 64, source_model_provenance="generic", folds=3,
    )
    altered_rows = list(_rows())
    altered_rows[0] = _row(1, "approve")
    altered_rows[0] = HeadRow(altered_rows[0].item_id, altered_rows[0].feedback,
                              (Feature("signal", 1e300, "rule"),))
    altered = fit_learned_head(
        TASK, altered_rows, declared_features=("signal",), development_ids=(), scoreboard_ids=(),
        scorecard_fingerprint=HASH, policy_fingerprint="b" * 64,
        context_artifact_fingerprint="c" * 64, source_model_provenance="generic", folds=3,
    )
    held = baseline.out_of_fold.item_ids.index("item-1")
    assert baseline.out_of_fold.normalization_fit_ids[held] == altered.out_of_fold.normalization_fit_ids[held]
    assert baseline.out_of_fold.normalizers[held] == altered.out_of_fold.normalizers[held]
    assert baseline.out_of_fold.logits[held] != altered.out_of_fold.logits[held]


def test_oof_fold_fit_weights_do_not_depend_on_held_out_propensity():
    baseline = fit_learned_head(
        TASK, _rows(), declared_features=("signal",), development_ids=(), scoreboard_ids=(),
        scorecard_fingerprint=HASH, policy_fingerprint="b" * 64,
        context_artifact_fingerprint="c" * 64, source_model_provenance="generic", folds=3,
    )
    changed_rows = list(_rows())
    changed_rows[0] = _row(1, "approve", propensity=0.9)
    changed = fit_learned_head(
        TASK, changed_rows, declared_features=("signal",), development_ids=(), scoreboard_ids=(),
        scorecard_fingerprint=HASH, policy_fingerprint="b" * 64,
        context_artifact_fingerprint="c" * 64, source_model_provenance="generic", folds=3,
    )
    held = baseline.out_of_fold.item_ids.index("item-1")
    assert baseline.out_of_fold.fit_ids[held] == changed.out_of_fold.fit_ids[held]
    assert baseline.out_of_fold.logits[held] == changed.out_of_fold.logits[held]


def test_feature_values_and_temperatures_are_numeric_finite_and_huge_features_stay_stable():
    result = fit_learned_head(
        TASK, _rows(), declared_features=("signal",), development_ids=(), scoreboard_ids=(),
        scorecard_fingerprint=HASH, policy_fingerprint="b" * 64,
        context_artifact_fingerprint="c" * 64, source_model_provenance="generic", folds=3,
    )
    with pytest.raises(ValueError, match="finite"):
        result.probabilities({"signal": float("inf")})
    with pytest.raises(ValueError, match="numeric"):
        result.probabilities({"signal": True})
    with pytest.raises(ValueError, match="temperature"):
        type(result.calibration)(0.0)

    huge_rows = tuple(
        HeadRow(
            item_id=f"huge-{index}",
            feedback=FeedbackItem(f"huge-feedback-{index}", f"huge-{index}", "review",
                                  final_answer_value=label, label_source=LABEL_SOURCE_FINAL,
                                  selection_propensity=1e-300 if index == 1 else 0.5),
            features=(Feature("signal", value, "rule"),),
        )
        for index, (label, value) in enumerate(
            (("approve", 1e300), ("reject", -1e300)) * 6, start=1
        )
    )
    huge = fit_learned_head(
        TASK, huge_rows, declared_features=("signal",), development_ids=(), scoreboard_ids=(),
        scorecard_fingerprint=HASH, policy_fingerprint="b" * 64,
        context_artifact_fingerprint="c" * 64, source_model_provenance="generic", folds=3,
    )
    assert all(math.isfinite(value) for value in huge.provenance.weights)
    assert all(math.isfinite(value) for value in huge.probabilities({"signal": 1e300}).values())


def test_feedback_for_another_task_cannot_become_a_trusted_training_label():
    wrong_task = HeadRow(
        "wrong", FeedbackItem("wrong-feedback", "wrong", "another-task", final_answer_value="approve",
                              label_source=LABEL_SOURCE_FINAL, selection_propensity=0.5),
        (Feature("signal", 1.0, "rule"),),
    )
    with pytest.raises(ValueError, match="score_name"):
        fit_learned_head(
            TASK, (wrong_task,), declared_features=("signal",), development_ids=(), scoreboard_ids=(),
            scorecard_fingerprint=HASH, policy_fingerprint="b" * 64,
            context_artifact_fingerprint="c" * 64, source_model_provenance="generic", folds=2,
        )

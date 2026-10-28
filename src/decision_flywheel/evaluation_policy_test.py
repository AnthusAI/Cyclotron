from .evaluation_policy import EvaluationPolicy


def test_recent_rubric_preference_decays_with_the_scarcest_class_not_the_majority():
    policy = EvaluationPolicy()
    assert policy.recency_allowance({"yes": 0, "no": 100}) == 2.0
    assert policy.recency_allowance({"yes": 10, "no": 100}) == 1.0
    assert policy.recency_allowance({"yes": 20, "no": 100}) == 0.0


def rows_of(count):
    from dataclasses import replace

    from .flywheel_test import DEV

    return tuple(replace(DEV[i % len(DEV)], item=replace(DEV[i % len(DEV)].item, id=str(i))) for i in range(count))


def test_evaluation_uses_every_one_of_500_development_items_by_default():
    from .flywheel_test import TASK

    assert len(EvaluationPolicy().select(rows_of(500), TASK.labels)) == 500
    assert EvaluationPolicy().manifest()["selection"] == "all"


def test_an_explicit_operator_sample_reserves_slots_for_scarce_classes():
    from .flywheel_test import TASK

    selected = EvaluationPolicy(max_samples=200).select(rows_of(500), TASK.labels)
    assert len({row.item.id for row in selected}) == 200
    assert all(sum(row.label == label for row in selected) == 100 for label in TASK.labels)
    assert EvaluationPolicy(max_samples=200).fingerprint != EvaluationPolicy().fingerprint


def test_scoring_sends_every_development_item_and_records_the_policy_and_its_fingerprint(tmp_path):
    import asyncio
    from datetime import datetime, timezone

    from .classifier_config import ClassifierConfig
    from .flywheel import DecisionFlywheel
    from .flywheel_test import TASK, FakeModel
    from .optimizer_agent import OptimizerAgent

    wheel = DecisionFlywheel(
        tmp_path / "wheel.sqlite", ClassifierConfig(TASK), FakeModel(), OptimizerAgent(lambda _: None), max_requests=1000
    )
    result = asyncio.run(wheel._score(wheel.active, rows_of(500), (), datetime.now(timezone.utc)))
    assert result["count"] == result["available_count"] == 500
    assert result["sample_limit"] is None
    assert result["evaluation_policy_fingerprint"] == wheel.evaluation_policy.fingerprint
    wheel.close()


def test_policy_parameters_cannot_be_invalid():
    import pytest

    for kwargs in ({"max_samples": 0}, {"recency_decay_per_class": 0}, {"initial_recency_allowance": -1}):
        with pytest.raises(ValueError):
            EvaluationPolicy(**kwargs)

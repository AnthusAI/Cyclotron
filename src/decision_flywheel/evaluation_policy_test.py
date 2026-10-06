from .evaluation_policy import EvaluationPolicy


def test_recent_rubric_preference_decays_with_the_scarcest_class_not_the_majority():
    policy = EvaluationPolicy()
    assert policy.recency_allowance({'yes': 0, 'no': 100}) == 2.
    assert policy.recency_allowance({'yes': 10, 'no': 100}) == 1.
    assert policy.recency_allowance({'yes': 20, 'no': 100}) == 0.


def test_evaluation_caps_samples_and_reserves_slots_for_scarce_classes():
    from .flywheel_test import DEV, TASK
    from dataclasses import replace
    rows = tuple(replace(DEV[i % len(DEV)], item=replace(DEV[i % len(DEV)].item, id=str(i))) for i in range(500))
    selected = EvaluationPolicy().select(rows, TASK.labels)
    assert len(selected) == 200
    assert len({row.item.id for row in selected}) == 200
    assert all(sum(row.label == label for row in selected) == 100 for label in TASK.labels)


def test_scoring_never_sends_more_than_the_configured_sample_limit(tmp_path):
    import asyncio
    from dataclasses import replace
    from datetime import datetime, timezone
    from .flywheel_test import DEV, TASK, FakeModel
    from .flywheel import DecisionFlywheel
    from .classifier_config import ClassifierConfig
    from .optimizer_agent import OptimizerAgent
    rows = tuple(replace(DEV[i % len(DEV)], item=replace(DEV[i % len(DEV)].item, id=str(i))) for i in range(500))
    wheel = DecisionFlywheel(tmp_path / 'wheel.sqlite', ClassifierConfig(TASK), FakeModel(),
                             OptimizerAgent(lambda _: None), max_requests=1000)
    result = asyncio.run(wheel._score(wheel.active, rows, (), datetime.now(timezone.utc)))
    assert result['count'] == 200
    assert result['available_count'] == 500
    assert result['sample_limit'] == 200
    assert len([e for e in wheel.history() if e['kind'] == 'decision-request']) <= 200
    wheel.close()


def test_policy_parameters_cannot_be_invalid():
    import pytest
    for kwargs in ({'max_samples': 0}, {'recency_decay_per_class': 0}, {'initial_recency_allowance': -1}):
        with pytest.raises(ValueError):
            EvaluationPolicy(**kwargs)

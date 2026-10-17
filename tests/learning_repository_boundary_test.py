"""Shared learning consumers use repositories while preserving evidence firewalls."""


def test_shared_control_and_training_algorithms_need_only_their_domain_repositories(tmp_path):
    import asyncio

    from decision_flywheel.classifier_config import ClassifierConfig
    from decision_flywheel.control_scheduler import ControlScheduler
    from decision_flywheel.control_scheduler_test import optimizer
    from decision_flywheel.flywheel import DecisionFlywheel
    from decision_flywheel.flywheel_test import DEV, TASK, TRAIN, FakeModel

    calls = []
    wheel = DecisionFlywheel(tmp_path / "runtime.sqlite", ClassifierConfig(TASK), FakeModel(), optimizer(calls))
    native = wheel.db

    class Forbidden:
        def __getattr__(self, name):
            raise AssertionError("learning algorithm issued SQL outside its repository")

    wheel.db = Forbidden()
    try:
        result = asyncio.run(
            ControlScheduler(wheel).run(TRAIN, DEV, protected=(), propensities={row.item.id: 1.0 for row in TRAIN})
        )
        assert result["promoted"] and calls == ["rubric", "example_ids", "tasks"]
        assert len(ControlScheduler(wheel).ideas()) == 3
        assert wheel.feature_bank()
    finally:
        wheel.db = native
        wheel.close()


def test_diagnosis_event_view_preserves_the_first_feedback_firewall_without_wheel_sql(tmp_path):
    from decision_flywheel.classifier_config import ClassifierConfig
    from decision_flywheel.diagnosis import recorded_predictions
    from decision_flywheel.flywheel import DecisionFlywheel
    from decision_flywheel.flywheel_test import TASK, FakeModel, agent

    wheel = DecisionFlywheel(tmp_path / "runtime.sqlite", ClassifierConfig(TASK), FakeModel(), agent([]))
    wheel._emit(
        {
            "kind": "prediction",
            "target_id": "one",
            "decision_model_label": "include",
            "decision_model_probabilities": {"include": 0.8, "exclude": 0.2},
            "version": "before-label",
        }
    )
    wheel._emit({"kind": "human-feedback", "feedback": None})
    wheel._emit({"kind": "human-feedback", "feedback": {"item_id": "one"}})
    wheel._emit(
        {
            "kind": "prediction",
            "target_id": "one",
            "decision_model_label": "exclude",
            "decision_model_probabilities": {"include": 0.1, "exclude": 0.9},
            "version": "after-label",
        }
    )
    native = wheel.db

    class Forbidden:
        def __getattr__(self, name):
            raise AssertionError("diagnosis issued SQL outside evidence repository")

    wheel.db = Forbidden()
    try:
        prediction = recorded_predictions(wheel, {"one"})["one"]
        assert prediction["label"] == "include" and prediction["version"] == "before-label"
        assert prediction["probabilities"] == {"include": 0.8, "exclude": 0.2}
    finally:
        wheel.db = native
        wheel.close()

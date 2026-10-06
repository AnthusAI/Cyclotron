"""Measured questions must reach a fitted classifier without another discovery call."""
import asyncio

from .classifier_config import ClassifierConfig
from .flywheel import DecisionFlywheel
from .flywheel_test import FakeModel, TASK, TRAIN, DEV
from .optimizer_agent import OptimizerAgent
from .classifier_training import train_classifier


def forbidden(_):
    raise AssertionError("fitting must not invoke the optimizer")


def test_training_waits_for_development_coverage_without_paid_calls(tmp_path):
    model = FakeModel()
    wheel = DecisionFlywheel(tmp_path / "runtime.sqlite", ClassifierConfig(TASK), model, OptimizerAgent(forbidden))
    result = asyncio.run(train_classifier(wheel, TRAIN, DEV, protected=(),
        propensities={r.item.id: 1. for r in TRAIN}))
    assert not result["promoted"]
    assert result["development_counts"] == {"include": 1, "exclude": 1}
    assert model.calls == 0
    wheel.close()


def test_measured_questions_are_fitted_and_restarts_reuse_the_selected_classifier(tmp_path):
    from .feature_bank import FeatureBank
    model = FakeModel()
    wheel = DecisionFlywheel(tmp_path / "runtime.sqlite", ClassifierConfig(TASK), model, OptimizerAgent(forbidden))
    FeatureBank(wheel.db).register({"name": "practical", "instructions": "Practical?", "labels": ["yes", "no"]},
        rationale="Feedback", evidence="training-only")
    kwargs = dict(protected=(), propensities={r.item.id: 1. for r in TRAIN}, min_development_per_class=1)
    result = asyncio.run(train_classifier(wheel, TRAIN, DEV, **kwargs))
    assert len(result["trials"]) == 4
    assert any("practical/yes" in event.get("features", []) for event in wheel.history() if event["kind"] == "fit-completed")
    assert result["promoted"]
    assert wheel.active.head is not None
    assert "practical/yes" in wheel.active.head.feature_names
    calls = model.calls
    assert asyncio.run(train_classifier(wheel, TRAIN, DEV, **kwargs)) == result
    assert model.calls == calls
    wheel.close()


def test_exploratory_training_does_not_deploy_or_use_protected_labels(tmp_path):
    from .models import Item
    model = FakeModel()
    wheel = DecisionFlywheel(tmp_path / "runtime.sqlite", ClassifierConfig(TASK), model, OptimizerAgent(forbidden))
    before = wheel.active.fingerprint
    protected = (Item("sealed", {"text": "sealed target"}),)
    result = asyncio.run(train_classifier(wheel, TRAIN, DEV, protected=protected,
        propensities={r.item.id: 1. for r in TRAIN}, min_development_per_class=1, apply_promotion=False))
    assert wheel.active.fingerprint == before
    assert not result["promoted"]
    assert not any(event.get("target_id") == "sealed" for event in wheel.history())
    assert all(t["candidate"]["count"] == len(DEV) for t in result["trials"])
    wheel.close()


def test_a_context_candidate_cannot_promote_by_sacrificing_a_class_for_lower_brier(tmp_path):
    wheel = DecisionFlywheel(tmp_path / "runtime.sqlite", ClassifierConfig(TASK), FakeModel(), OptimizerAgent(forbidden))
    async def score(classifier, *args):
        candidate = classifier.head is not None
        return {"balanced_brier": .1 if candidate else .9, "balanced_accuracy": .5 if candidate else 1.,
                "per_class": {"include": {"recall": 0. if candidate else 1.}, "exclude": {"recall": 1.}}}
    wheel._score = score
    result = asyncio.run(wheel.improve(TRAIN, DEV, protected=(), propensities={r.item.id: 1. for r in TRAIN},
        candidate_proposal={"rubric": "new rubric"}, require_recall_safeguards=True))
    assert result["brier_improved"]
    assert not result["recall_safeguards_passed"]
    assert not result["promoted"]
    assert wheel.active.config.rubric == ""
    wheel.close()

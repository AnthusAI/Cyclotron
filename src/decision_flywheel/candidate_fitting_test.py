"""Context and numeric head are built together without external services."""
import asyncio
from dataclasses import replace
import pytest

from .classifier_config import ClassifierConfig
from .flywheel import DecisionFlywheel
from .flywheel_test import FakeModel, TASK, TRAIN, DEV, agent
from .candidate_fitting import fit_candidate


def test_a_new_context_gets_a_head_from_all_its_probability_features(tmp_path):
    model = FakeModel()
    wheel = DecisionFlywheel(tmp_path/'runtime.sqlite3', ClassifierConfig(TASK), model, agent([]))
    config = wheel.active.config.apply({'rubric':'New criteria', 'tasks':[
        {'name':'support','instructions':'Support?','labels':['yes','no']}]}, TRAIN)
    candidate, _ = asyncio.run(fit_candidate(wheel, config, TRAIN, DEV,
        protected=(), propensities={r.item.id:1. for r in TRAIN}, validation_status='provisional'))
    assert candidate.head.feature_names == ('decision/include', 'support/yes')
    assert candidate.head.provenance.context_artifact_fingerprint == config.fingerprint
    assert wheel.active.head is None
    with pytest.raises(ValueError,match='head does not match'):
        wheel._activate(replace(candidate,config=wheel.active.config))
    assert wheel.active.config.rubric==''
    wheel.close()


def test_insufficient_training_coverage_is_explicit_and_makes_no_requests(tmp_path):
    model = FakeModel()
    wheel = DecisionFlywheel(tmp_path/'runtime.sqlite3', ClassifierConfig(TASK), model, agent([]))
    candidate, _ = asyncio.run(fit_candidate(wheel, wheel.active.config, TRAIN[:1], DEV,
        protected=(), propensities={TRAIN[0].item.id:1.}, validation_status='provisional'))
    assert candidate.head is None and model.calls == 0
    assert any(e['kind']=='head-fit-deferred' for e in wheel.history())
    wheel.close()

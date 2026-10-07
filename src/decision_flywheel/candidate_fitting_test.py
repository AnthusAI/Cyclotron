"""Context and numeric head are built together without external services."""
import asyncio
from dataclasses import replace
import pytest

from .classifier_config import ClassifierConfig
from .flywheel import DecisionFlywheel
from .flywheel_test import FakeModel, TASK, TRAIN, DEV, agent
from .candidate_fitting import fit_candidate


def test_a_changed_model_context_invalidates_only_the_head_and_preserves_the_rubric(tmp_path):
    class ContextModel(FakeModel):
        context='joint-context-v1'
        def feature_context_identity(self,config,training):return self.context
    model=ContextModel();wheel=DecisionFlywheel(tmp_path/'runtime.sqlite',ClassifierConfig(TASK),model,agent([]))
    config=wheel.active.config.apply({'rubric':'Keep these criteria'},TRAIN)
    candidate,_=asyncio.run(fit_candidate(wheel,config,TRAIN,DEV,protected=(),
        propensities={r.item.id:1. for r in TRAIN},validation_status='provisional'))
    wheel._activate(candidate)
    assert candidate.head.provenance.source_model_provenance=='joint-context-v1'
    assert not wheel.reconcile_model_context(TRAIN)
    model.context='joint-context-v2'
    assert wheel.reconcile_model_context(TRAIN)
    assert wheel.active.head is None and wheel.active.config.rubric=='Keep these criteria'
    assert any(e['kind']=='head-invalidated' for e in wheel.history())
    wheel.close()


def test_a_restart_cannot_predict_with_a_head_from_an_old_joint_context(tmp_path):
    class ContextModel(FakeModel):
        context='joint-v1'
        def feature_context_identity(self,config,training):return self.context
    path=tmp_path/'runtime.sqlite';model=ContextModel()
    wheel=DecisionFlywheel(path,ClassifierConfig(TASK),model,agent([]))
    config=wheel.active.config.apply({'rubric':'Preserve the learned criteria'},TRAIN)
    candidate,_=asyncio.run(fit_candidate(wheel,config,TRAIN,DEV,protected=(),
        propensities={r.item.id:1. for r in TRAIN},validation_status='provisional'))
    wheel._activate(candidate);wheel.close()
    model.context='joint-v2'
    wheel=DecisionFlywheel(path,ClassifierConfig(TASK),model,agent([]))
    try:
        asyncio.run(wheel.predict(DEV[0].item,TRAIN))
        assert wheel.active.head is None
        assert wheel.active.config.rubric=='Preserve the learned criteria'
        assert [e for e in wheel.history() if e['kind']=='prediction'][-1]['fitted_head'] is False
    finally:wheel.close()


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

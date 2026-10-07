import asyncio
import pytest
from .run_comparison import compare_endpoints
from .run_comparison import compare_head
from .trace_artifact import read_trace
from .flywheel_test import TASK, TRAIN, DEV, FakeModel, agent
from .flywheel import DecisionFlywheel
from .classifier_config import ClassifierConfig


def test_head_comparison_reuses_identical_requests_and_never_fits_or_promotes(tmp_path):
    model=FakeModel()
    wheel=DecisionFlywheel(tmp_path/'audit.sqlite',ClassifierConfig(TASK),model,agent([]))
    asyncio.run(wheel.improve(TRAIN,DEV,protected=(),propensities={r.item.id:1. for r in TRAIN}))
    before=wheel.active
    model.calls=0
    report=asyncio.run(compare_head(wheel,before,DEV,TRAIN,
        class_config=[{'label':'include','role':'positive'},{'label':'exclude','role':'negative'}]))
    assert report['before']['fingerprint'] != report['after']['fingerprint']
    assert report['before']['metrics']['sample_ids'] == report['after']['metrics']['sample_ids']
    assert model.calls==0
    assert wheel.active==before
    assert 'identical decision requests' in report['scope']
    wheel.close()


def test_endpoint_comparison_scores_the_same_audit_items_without_optimizing_or_changing_the_active_classifier(tmp_path):
    wheel = DecisionFlywheel(tmp_path/'audit.sqlite', ClassifierConfig(TASK), FakeModel(), agent([]))
    initial = wheel.active
    report = asyncio.run(compare_endpoints(wheel, initial, initial, DEV, TRAIN,
        class_config=[{'label':'include','role':'positive'},{'label':'exclude','role':'negative'}]))
    assert report['sample_count'] == 2
    assert report['class_counts'] == {'include':1,'exclude':1}
    assert report['before']['metrics']['sample_ids'] == report['after']['metrics']['sample_ids']
    assert report['before']['accuracy'] == report['after']['accuracy']
    assert wheel.active == initial
    assert not any(event['kind']=='optimizer-request' for event in read_trace(tmp_path/'audit.sqlite'))
    wheel.close()


def test_endpoint_comparison_rejects_leaking_training_items_before_model_calls(tmp_path):
    wheel = DecisionFlywheel(tmp_path/'audit.sqlite', ClassifierConfig(TASK), FakeModel(), agent([]))
    with pytest.raises(ValueError, match='disjoint'):
        asyncio.run(compare_endpoints(wheel,wheel.active,wheel.active,TRAIN,TRAIN,
            class_config=[{'label':'include','role':'positive'},{'label':'exclude','role':'negative'}]))
    assert wheel.requests == 0
    wheel.close()

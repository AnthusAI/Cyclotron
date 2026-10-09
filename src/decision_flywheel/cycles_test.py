import asyncio
import pytest
from .flywheel_test import FakeModel, TASK, agent
from .flywheel import DecisionFlywheel
from .classifier_config import ClassifierConfig
from .models import Item


def test_a_cycle_links_predictions_and_trigger_checks_and_records_before_and_after_configuration(tmp_path):
    wheel=DecisionFlywheel(tmp_path/'runtime.sqlite3', ClassifierConfig(TASK), FakeModel(), agent([]))
    with wheel.cycle(Item('paper', {'text':'yes paper'})) as cycle:
        asyncio.run(wheel.predict(Item('paper', {'text':'yes paper'}), ()))
        cycle.check_trigger('rubric', due=False, reason='not enough new feedback', details={'count':1,'threshold':10})
    events=wheel.history()
    assert events[0]['kind']=='cycle-started'
    assert events[-1]['kind']=='cycle-completed'
    assert len({event['cycle_id'] for event in events})==1
    assert events[0]['item']['id']=='paper'
    assert events[0]['classifier_snapshot']==events[-1]['classifier_snapshot']
    assert next(e for e in events if e['kind']=='prediction')['probabilities']
    assert next(e for e in events if e['kind']=='trigger-evaluated')['due'] is False
    wheel.close()


def test_cycles_do_not_leak_context_and_failures_close_the_cycle_without_hiding_the_error(tmp_path):
    path=tmp_path/'runtime.sqlite3'
    wheel=DecisionFlywheel(path, ClassifierConfig(TASK), FakeModel(), agent([]))
    with pytest.raises(RuntimeError):
        with wheel.cycle(None):
            raise RuntimeError('failed work')
    assert wheel.history()[-1]['kind']=='cycle-failed'
    wheel._emit({'kind':'outside'})
    assert 'cycle_id' not in wheel.history()[-1]
    with wheel.cycle(None):
        pass
    assert wheel.history()[-1]['cycle_number']==2
    wheel.close()
    wheel=DecisionFlywheel(path, ClassifierConfig(TASK), FakeModel(), agent([]))
    with wheel.cycle(None):
        pass
    assert wheel.history()[-1]['cycle_number']==3
    wheel.close()


def test_an_unfinished_cycle_can_resume_after_restart_without_predicting_again(tmp_path):
    path=tmp_path/'runtime.sqlite3'
    item=Item('paper',{'text':'yes paper'})
    wheel=DecisionFlywheel(path,ClassifierConfig(TASK),FakeModel(),agent([]))
    original=wheel.cycle(item).__enter__()
    asyncio.run(wheel.predict(item,()))
    cycle_id=original.context['cycle_id']
    wheel.close()
    reopened=DecisionFlywheel(path,ClassifierConfig(TASK),FakeModel(),agent([]))
    resumed=reopened.resume_cycle(item)
    assert resumed.context['cycle_id']==cycle_id
    assert reopened.model.calls==0
    resumed.__exit__(None,None,None)
    assert reopened.history()[-1]['kind']=='cycle-completed'
    assert reopened.resume_cycle(item) is None
    reopened.close()


def test_a_waiting_review_survives_a_later_completed_optimization_cycle(tmp_path):
    item=Item('waiting-paper',{'text':'yes paper'})
    wheel=DecisionFlywheel(tmp_path/'runtime.sqlite3',ClassifierConfig(TASK),FakeModel(),agent([]))
    cycle=wheel.cycle(item).__enter__()
    asyncio.run(wheel.predict(item,()))
    expected=cycle.context['cycle_id']
    cycle.suspend()
    with wheel.cycle(Item('older-paper',{'text':'previously reviewed'}),reason='optimization-resume'):
        pass
    resumed=wheel.resume_cycle(item)
    assert resumed is not None
    assert resumed.context['cycle_id']==expected
    assert wheel.model.calls==1
    resumed.__exit__(None,None,None)
    assert wheel.resume_cycle(item) is None
    wheel.close()


def test_cycle_boundaries_name_the_fit_proof_that_the_activation_records_in_full(tmp_path):
    from .flywheel_test import TRAIN, DEV
    wheel=DecisionFlywheel(tmp_path/'runtime.sqlite3', ClassifierConfig(TASK), FakeModel(), agent([]), max_requests=30)
    asyncio.run(wheel.improve(TRAIN, DEV, protected=(), propensities={r.item.id: 1.0 for r in TRAIN}))
    with wheel.cycle(Item('paper', {'text':'yes paper'})):
        pass
    events=wheel.history(1000)
    activated=next(e for e in reversed(events) if e['kind']=='classifier-activated')['classifier_snapshot']
    started=next(e for e in events if e['kind']=='cycle-started')['classifier_snapshot']
    full=activated['head']['out_of_fold']; compact=started['head']['out_of_fold']
    assert full['fit_ids'] and full['normalizers']
    assert not {'fit_ids','fit_labels','normalization_fit_ids','normalizers'} & set(compact)
    assert compact['per_item_fit_proof']['head_fingerprint']==activated['head']['refitted_cyclotron_fingerprint']
    assert {key:value for key,value in full.items() if key in compact}=={key:value for key,value in compact.items() if key!='per_item_fit_proof'}
    assert started['config']==activated['config']
    assert started['head']['provenance']==activated['head']['provenance']
    wheel.close()

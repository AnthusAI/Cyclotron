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

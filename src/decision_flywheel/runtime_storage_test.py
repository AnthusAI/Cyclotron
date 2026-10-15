"""Runtime state/evidence boundary remains local and provider neutral."""
import json
import pytest

from .runtime_storage import SQLiteRuntimeStateStorage
from .classifier_config import ClassifierConfig
from .flywheel import DecisionFlywheel
from .flywheel_test import TASK,FakeModel,agent


def test_contract_state_and_evidence_survive_restart_with_stable_trace_cursors(tmp_path):
    path=tmp_path/'runtime.sqlite'
    storage=SQLiteRuntimeStateStorage(path)
    storage.bind_contract('{"model":"synthetic"}')
    storage.put_state('optimizer_context','{ "human_explanations": ["雪"] }')
    assert storage.append_event('{"kind":"human-feedback","comment":"Exact explanation"}')==1
    storage.close()
    storage=SQLiteRuntimeStateStorage(path)
    storage.bind_contract('{"model":"synthetic"}')
    assert storage.state('optimizer_context')=='{ "human_explanations": ["雪"] }'
    assert storage.trace_events(after_event_id=0,limit=1)['cursor']==1
    with pytest.raises(ValueError): storage.bind_contract('{"model":"different"}')
    assert storage.state('contract')=='{"model":"synthetic"}'
    storage.close()


def test_runtime_state_observability_paths_use_domain_storage_instead_of_wheel_sql(tmp_path):
    wheel=DecisionFlywheel(tmp_path/'runtime.sqlite',ClassifierConfig(TASK),FakeModel(),agent([]))
    native=wheel.db
    class Forbidden:
        def __getattr__(self,name): raise AssertionError('wheel issued SQL for converted runtime state/evidence')
    wheel.db=Forbidden()
    wheel.set_optimizer_context(['Preserve the explanation'])
    assert wheel.optimizer_context['human_explanations']==['Preserve the explanation']
    emitted=wheel._emit({'kind':'synthetic','explanation':'Same monitoring'})
    assert wheel.history()[-1]==emitted
    assert wheel.trace_events()['events'][-1]==emitted
    assert wheel.hypotheses()==()
    wheel.db=native
    wheel.close()


def test_racing_first_contract_binds_cannot_overwrite_each_other(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    path=tmp_path/'runtime.sqlite'
    SQLiteRuntimeStateStorage(path).close()
    barrier=Barrier(2)
    def bind(index):
        store=SQLiteRuntimeStateStorage(path)
        try:
            barrier.wait()
            try: store.bind_contract(json.dumps({'model':index})); return True
            except ValueError: return False
        finally: store.close()
    with ThreadPoolExecutor(max_workers=2) as pool: assert sorted(pool.map(bind,(0,1)))==[False,True]

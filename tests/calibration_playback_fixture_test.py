"""Recorded playback evidence comes from the real offline API/worker boundary."""
import importlib.util
from pathlib import Path
import pytest


def fixture_script():
    path=Path(__file__).parents[1]/'scripts/seed_calibration_playback.py'
    spec=importlib.util.spec_from_file_location('calibration_fixture',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_offline_playback_records_three_cycles_for_two_independent_classifiers(tmp_path):
    from decision_flywheel.web_store import WebStore
    result=fixture_script().build_fixture(tmp_path)
    store=WebStore(tmp_path/'workspace.sqlite3')
    events=[row['payload'] for row in store.all_events(result['run_id'])]
    assert result['model_calls']==3
    for classifier in ('relevance','practicality'):
        rows=[event for event in events if event.get('classifier_id')==classifier]
        snapshots=[event for event in rows if event['kind']=='cycle-metrics']
        assert [event['metrics']['calibration']['count'] for event in snapshots]==[1,2,3]
        assert len({event['metrics']['calibration']['ece'] for event in snapshots})==3
        assert len({event['cycle_id'] for event in snapshots})==3
        assert [event['metrics']['calibration']['source_counts'] for event in snapshots]==[
            {'decision-passthrough':1},{'decision-passthrough':2},{'decision-passthrough':3}]
        assert all(event['metrics']['calibration']['samples'][-1]['feedback_event_id'] for event in snapshots)
    assert not any(event['kind']=='optimizer-request' for event in events)
    assert len(store.item_labels('fixture-items','fixture-1',1))==2
    assert all(job['status']=='completed' for job in store.jobs(result['run_id']))


def test_fixture_generation_refuses_to_merge_synthetic_labels_into_an_existing_workspace(tmp_path):
    sentinel=tmp_path/'workspace.sqlite3'
    sentinel.write_bytes(b'existing workspace must stay untouched')
    with pytest.raises(ValueError,match='new empty fixture directory'):
        fixture_script().build_fixture(tmp_path)
    assert sentinel.read_bytes()==b'existing workspace must stay untouched'


def test_the_offline_browser_workspace_enables_replay_using_only_injected_fake_models(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    module=fixture_script()
    def forbidden(*args,**kwargs):raise AssertionError('offline acceptance must never construct a paid model')
    monkeypatch.setattr(module.JevAdapter,'from_environment',forbidden)
    app,fixture=module.create_offline_app(tmp_path,port=8785)
    with TestClient(app) as client:
        result=client.post('/graphql',json={'query':'{capabilities{liveEnabled} runs{id}}'}).json()
        assert result['data']['capabilities']['liveEnabled'] is True
        assert result['data']['runs'][0]['id']==fixture['run_id']
        worker=app.state.offline_worker
        run=worker.create_replay('OFFLINE REPLAY',fixture['run_id'],{'cyclotron_id':'fixture-cyclotron'})
        assert run['config']['input_mode']=='replay'
        model,_=worker.model_factory(run['config'])
        answer=model.client.system_one(state={'target':{'text':'Synthetic item 2; not research data.'}},
            questions={'q0':{},'q1':{}})
        assert answer.model=='offline-fixture'
        assert set(answer.answers)=={'q0','q1'}


def test_offline_browser_serving_refuses_an_existing_database_before_modifying_it(tmp_path):
    sentinel=tmp_path/'workspace.sqlite3'
    sentinel.write_bytes(b'leave real work alone')
    with pytest.raises(ValueError,match='new empty fixture directory'):
        fixture_script().create_offline_app(tmp_path,port=8785)
    assert sentinel.read_bytes()==b'leave real work alone'


def test_the_labeling_fixture_keeps_a_pending_prediction_after_three_calibration_snapshots(tmp_path):
    from decision_flywheel.web_store import WebStore
    result=fixture_script().build_fixture(tmp_path,pending_item=True)
    store=WebStore(tmp_path/'workspace.sqlite3')
    current=store.current_item(result['run_id'])
    assert current['item']['id']=='fixture-4'
    assert result['model_calls']==4
    for classifier in ('relevance','practicality'):
        metrics=[row['payload'] for row in store.all_events(result['run_id'])
                 if row['payload'].get('kind')=='cycle-metrics' and row['payload'].get('classifier_id')==classifier]
        assert [event['metrics']['calibration']['count'] for event in metrics]==[1,2,3]
    assert store.item_labels('fixture-items','fixture-4',1)==[]


def test_fake_confidence_stays_a_valid_probability_when_browser_checks_make_many_requests():
    client=fixture_script().FakeDecisionClient()
    for _ in range(20):
        probabilities=client.system_one(questions={'q':{}}).answers['q']['probabilities']
        assert all(0<=value<=1 for value in probabilities.values())
        assert sum(probabilities.values())==pytest.approx(1)


def test_scripted_activity_records_real_worker_learning_phases_without_paid_clients(tmp_path,monkeypatch):
    from decision_flywheel.web_store import WebStore
    module=fixture_script()
    monkeypatch.setattr(module.JevAdapter,'from_environment',lambda *a,**k:pytest.fail('paid client forbidden'))
    result=module.build_fixture(tmp_path,pending_item=True,optimizer_activity=True)
    store=WebStore(tmp_path/'workspace.sqlite3')
    events=[row['payload'] for row in store.all_events(result['run_id'])]
    kinds={event['kind'] for event in events}
    assert len([event for event in events if event['kind']=='human-feedback' and event['action']=='submitted'])==80
    assert {'optimizer-request','optimizer-response','fit-started','fit-completed','candidate-evaluated'}<=kinds
    assert not kinds.intersection({'round-failed','step-failed','optimization-stage-failed'})
    assert all(job['status']=='completed' for job in store.jobs(result['run_id']))
    requests=[event for event in events if event['kind']=='optimizer-request']
    import json
    assert {json.loads(event['messages'][-1]['content'])['current']['control_under_test']
            for event in requests}=={'rubric','example_ids','tasks'}
    assert any(json.loads(event['messages'][-1]['content'])['human_explanations'] for event in requests)
    assert any(event.get('tool_calls') for event in events if event['kind']=='optimizer-response')
    # The fixture sends every engine event through the real GraphQL endpoint,
    # but a cyclotron command flushes bounded ordered batches rather than one
    # HTTP/SQLite transaction per emitted event.
    assert result['trace_requests'] * 10 < len(events)
    current=store.current_item(result['run_id'])
    assert current['item']['id']=='fixture-41'
    assert store.item_labels('fixture-items','fixture-41',1)==[]

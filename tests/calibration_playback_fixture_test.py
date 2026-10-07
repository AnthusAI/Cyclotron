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

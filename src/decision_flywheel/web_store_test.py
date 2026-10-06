"""The web API owns durable, isolated run history."""
import pytest
from .web_store import WebStore


def test_event_ingestion_is_idempotent_but_cannot_rewrite_a_committed_event(tmp_path):
    store = WebStore(tmp_path / 'web.sqlite')
    run = store.create_run('First', 'recorded', {'primary':'f1'})
    event = {'kind':'optimizer-request','messages':[{'role':'user','content':'Actual context'}]}
    first = store.append_event(run['id'], 'one', event)
    assert store.append_event(run['id'], 'one', event) == first
    with pytest.raises(ValueError, match='different'):
        store.append_event(run['id'], 'one', {'kind':'optimizer-response'})
    assert len(store.events(run['id'])) == 1


def test_run_history_and_cursor_survive_restart_without_mixing_runs(tmp_path):
    path = tmp_path / 'web.sqlite'
    store = WebStore(path)
    a = store.create_run('A', 'recorded', {})
    b = store.create_run('B', 'recorded', {})
    one = store.append_event(a['id'], 'a1', {'kind':'prediction'})
    store.append_event(b['id'], 'b1', {'kind':'prediction'})
    two = store.append_event(a['id'], 'a2', {'kind':'human-feedback'})
    reopened = WebStore(path)
    assert [r['name'] for r in reopened.runs()] == ['B','A']
    assert reopened.events(a['id'], after=one['sequence']) == [two]


def test_commands_are_durable_idempotent_and_interrupted_work_is_not_retried_silently(tmp_path):
    store = WebStore(tmp_path / 'web.sqlite')
    run = store.create_run('Live', 'live', {})
    first = store.command(run['id'], 'request1', 'prepare', {})
    assert store.command(run['id'], 'request1', 'prepare', {}) == first
    with pytest.raises(ValueError):
        store.command(run['id'], 'request1', 'label', {})
    claimed = store.claim_command()
    assert claimed['id'] == first['id']
    store.recover_interrupted()
    assert store.jobs(run['id'])[0]['status'] == 'interrupted'
    assert store.claim_command() is None


def test_event_batches_roll_back_together_and_read_pages_are_bounded(tmp_path):
    store = WebStore(tmp_path / 'web.sqlite')
    run = store.create_run('A', 'recorded', {})
    with pytest.raises(ValueError):
        store.append_batch(run['id'], [('one', {'kind':'prediction'}), ('bad', {})])
    assert store.events(run['id']) == []
    with pytest.raises(ValueError):
        store.events(run['id'], limit=10001)


def test_final_summary_is_persisted_without_rewriting_run_configuration(tmp_path):
    store = WebStore(tmp_path / 'web.sqlite')
    run = store.create_run('A','recorded',{'selection_policy':{'primary':'f1'}})
    summary = {'comparison':{'scope':'Protected audit','before':{'accuracy':.5},'after':{'accuracy':.8}}}
    store.complete_run(run['id'],summary)
    assert store.run(run['id'])['config'] == run['config']
    assert store.summary(run['id']) == summary
    assert store.run(run['id'])['status'] == 'completed'
    with pytest.raises(ValueError):
        store.complete_run(run['id'],{'comparison':'different'})

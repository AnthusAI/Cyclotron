"""The web API owns durable, isolated run history."""
import json
import pytest
from .web_store import WebStore


def test_exhaustion_and_its_completed_command_commit_the_run_status_together(tmp_path):
    store = WebStore(tmp_path/'web.sqlite')
    run = store.create_run('Replay', 'live', {'input_mode':'replay'})
    job = store.command(run['id'], 'last', 'replay-next', {})
    store.claim_command()
    store.set_status(run['id'], 'working')
    store.finish_command(job['id'], 'completed', {'finished':True})
    assert store.run(run['id'])['status'] == 'completed'
    assert store.jobs(run['id'])[0]['result'] == {'finished':True}


def test_startup_recovers_a_durable_exhaustion_result_without_replaying_or_rewriting_evidence(tmp_path):
    path = tmp_path/'web.sqlite'
    store = WebStore(path)
    run = store.create_run('Replay', 'live', {'input_mode':'replay','frozen':'unchanged'})
    store.append_event(run['id'], 'feedback', {'kind':'human-feedback','comment':'Preserve me'})
    job = store.command(run['id'], 'last', 'replay-next', {})
    store.claim_command()
    store.finish_command(job['id'], 'completed', {'finished':True})
    store.set_status(run['id'], 'ready')  # a pre-fix record or crash between old commits
    jobs, events = store.jobs(run['id']), store.all_events(run['id'])
    restored = WebStore(path)
    restored.recover_interrupted()
    assert restored.run(run['id'])['status'] == 'completed'
    assert restored.run(run['id'])['config'] == run['config']
    assert restored.jobs(run['id']) == jobs and restored.all_events(run['id']) == events
    assert restored.claim_command() is None
    restored.recover_interrupted()
    assert restored.jobs(run['id']) == jobs and restored.all_events(run['id']) == events


@pytest.mark.parametrize('value,status', [(False,'completed'), (1,'completed'),
    ('true','completed'), (None,'completed'), (True,'failed'), (True,'interrupted')])
def test_recovery_does_not_infer_exhaustion_from_nonboolean_or_unsuccessful_results(tmp_path, value, status):
    store = WebStore(tmp_path/'web.sqlite')
    run = store.create_run('Replay', 'live', {'input_mode':'replay'})
    job = store.command(run['id'], 'last', 'replay-next', {})
    store.claim_command()
    store.finish_command(job['id'], status, {'finished':value})
    store.recover_interrupted()
    assert store.run(run['id'])['status'] == 'ready'


@pytest.mark.parametrize('later_status', ['pending','running','failed'])
def test_old_exhaustion_cannot_hide_newer_pending_interrupted_or_failed_work(tmp_path, later_status):
    store = WebStore(tmp_path/'web.sqlite')
    run = store.create_run('Replay', 'live', {'input_mode':'replay'})
    job = store.command(run['id'], 'last', 'replay-next', {})
    store.claim_command()
    store.finish_command(job['id'], 'completed', {'finished':True})
    store.set_status(run['id'], 'ready')
    later = store.command(run['id'], 'later', 'prepare', {})
    if later_status != 'pending':
        store.claim_command()
        if later_status == 'failed': store.finish_command(later['id'], 'failed', {})
    store.recover_interrupted()
    assert store.run(run['id'])['status'] == 'ready'
    assert store.jobs(run['id'])[0]['status'] == ('interrupted' if later_status == 'running' else later_status)


def test_run_scorecard_identity_uses_version_membership_before_immutable_configuration(tmp_path):
    store=WebStore(tmp_path/'db')
    for key in ('old','new'):
        store.save_classifier(key,key,{'question':'Choose','classes':[{'label':'yes'},{'label':'no'}]})
    store.save_item_list('list','List')
    config={'classifiers':[store.classifier('old')],'item_list_id':'list','seed':'seed','scorecard_id':'original-definition'}
    run=store.create_run('Original','live',config)
    assert store.run_scorecard_id(run['id'])=='original-definition'
    edition=store.extend_scorecard(run['id'],['new'],name='Family')
    assert store.run_scorecard_id(run['id'])==edition['config']['scorecard_id']
    assert store.run(run['id'])['config']==config
    independent=store.create_run('Independent','recorded',{})
    assert store.run_scorecard_id(independent['id']) is None
    with pytest.raises(ValueError,match='unknown run'):store.run_scorecard_id('missing')


def test_explicit_feedback_recovery_requeues_only_non_paid_scorecard_commands_and_preserves_failure_history(tmp_path):
    store = WebStore(tmp_path / 'workspace.sqlite')
    run = store.create_run('Scorecard', 'live', {'classifiers': [{'id': 'a'}]})
    job = store.command(run['id'], 'undo', 'undo', {})
    store.claim_command()
    store.finish_command(job['id'], 'failed', {'error_type': 'RuntimeError', 'reason': 'state retained'})
    resumed = store.resume_feedback_command(run['id'], job['id'])
    assert resumed['id'] == job['id'] and resumed['status'] == 'pending'
    assert store.resume_feedback_command(run['id'], job['id']) == resumed
    events = store.all_events(run['id'])
    assert len(events) == 1 and events[0]['payload']['previous_result']['error_type'] == 'RuntimeError'
    assert store.claim_command()['id'] == job['id']
    store.finish_command(job['id'], 'completed', {'undone': 'paper'})
    assert store.resume_feedback_command(run['id'], job['id'])['status'] == 'completed'
    for kind in ('optimize', 'prepare', 'label', 'replay-next'):
        paid = store.command(run['id'], kind, kind, {})
        store.claim_command(); store.finish_command(paid['id'], 'failed', {})
        with pytest.raises(ValueError, match='correction or undo'):
            store.resume_feedback_command(run['id'], paid['id'])


def test_feedback_recovery_cannot_interleave_with_work_or_reactivate_an_inactive_edition(tmp_path):
    store = WebStore(tmp_path / 'workspace.sqlite')
    run = store.create_run('Scorecard', 'live', {'classifiers': [{'id': 'a'}]})
    job = store.command(run['id'], 'undo', 'undo', {})
    store.claim_command(); store.finish_command(job['id'], 'failed', {})
    busy = store.command(run['id'], 'prepare', 'prepare', {})
    with pytest.raises(ValueError, match='work in progress'):
        store.resume_feedback_command(run['id'], job['id'])
    store.claim_command(); store.finish_command(busy['id'], 'completed', {})
    other = store.create_run('New edition', 'live', {'classifiers': [{'id': 'a'}]})
    with store.connect() as db:
        db.execute('INSERT INTO scorecards VALUES (?,?,?)', ('card', 'Card', 2))
        db.execute('INSERT INTO scorecard_versions VALUES (?,?,?,?,?)', ('card', 1, run['id'], None, run['created_at']))
        db.execute('INSERT INTO scorecard_versions VALUES (?,?,?,?,?)', ('card', 2, other['id'], run['id'], other['created_at']))
    with pytest.raises(ValueError, match='activate'):
        store.resume_feedback_command(run['id'], job['id'])
    with pytest.raises(ValueError, match='correction or undo'):
        store.resume_feedback_command(other['id'], job['id'])
    assert store.all_events(run['id']) == []
    assert next(row for row in store.jobs(run['id']) if row['id'] == job['id'])['status'] == 'failed'


def test_operating_limits_can_change_without_resetting_labels_or_classifier_configuration(tmp_path):
    store=WebStore(tmp_path/'web.sqlite')
    run=store.create_run('Live','live',{'max_requests':500,'max_optimizer_calls':10,'classifiers':[{'id':'a'}]})
    changed=store.update_run_limits(run['id'],10000,1000)
    assert changed['config']['max_optimizer_calls']==1000
    assert changed['config']['classifiers']==[{'id':'a'}]
    with pytest.raises(ValueError):store.update_run_limits(run['id'],10000,0)


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


def test_a_restart_preserves_commands_that_have_not_started(tmp_path):
    store=WebStore(tmp_path/'web.sqlite')
    run=store.create_run('Live','live',{})
    job=store.command(run['id'],'saved-vote','label',{'labels':[]})
    store.recover_interrupted()
    assert store.jobs(run['id'])[0]['status']=='pending'
    assert store.claim_command()['id']==job['id']


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


def test_replay_creation_commits_its_items_feedback_and_initial_trace_as_one_transaction(tmp_path):
    import sqlite3
    store=WebStore(tmp_path/'web.sqlite')
    vote=('item','classifier',{'label':'yes','comment':'Reason'})
    with pytest.raises(sqlite3.IntegrityError):
        store.create_run('Replay','live',{'input_mode':'replay'},items=[{'id':'item'}],
                         frozen_feedback=[vote,vote],initial_events=[('created',{'kind':'replay-created'})])
    assert store.runs()==[]
    run=store.create_run('Replay','live',{'input_mode':'replay'},items=[{'id':'item'}],
                         frozen_feedback=[vote],initial_events=[('created',{'kind':'replay-created'})])
    assert store.items(run['id'])[0]['id']=='item'
    assert store.inherited_labels(run['id'],'item')[0]['comment']=='Reason'
    assert store.events(run['id'])[0]['payload']['kind']=='replay-created'
def test_a_review_status_update_does_not_erase_the_prediction_that_was_shown(tmp_path):
    store = WebStore(tmp_path / 'web.sqlite')
    run = store.create_run('A', 'live', {}, items=[{'id': 'item'}])
    prediction = {'label': 'include', 'presentation_id': 'shown'}
    store.update_item(run['id'], 'item', prediction=prediction)
    store.update_item(run['id'], 'item', reviewed=True)
    with store.connect() as db:
        row = db.execute('SELECT prediction,reviewed FROM web_items WHERE run_id=? AND id=?',
                         (run['id'], 'item')).fetchone()
    assert json.loads(row['prediction']) == prediction
    assert row['reviewed'] == 1

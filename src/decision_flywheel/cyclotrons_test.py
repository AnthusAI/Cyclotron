"""Cyclotron revisions retain history while learning again from replayed feedback."""
from .web_store import WebStore


def test_an_added_classifier_starts_a_fresh_version_with_historical_items_first(tmp_path):
    store=WebStore(tmp_path/'web.sqlite')
    for key in ('old','new'):
        store.save_classifier(key,key,{'question':'Include?','classes':[{'label':'include','role':'positive'},{'label':'exclude','role':'negative'}]})
    store.save_item_list('list','List')
    store.upsert_list_items('list',[{'id':key,'occurred_at':'2026-01-01','values':{'text':key}} for key in ('unseen','seen')])
    config={'classifiers':[store.classifier('old')],'item_list_id':'list','seed':'seed'}
    parent=store.create_run('Anthus','live',config,items=store.list_items('list'))
    store.label_item('old',1,'list','seen',1,'include','Reason','original-vote')
    version=store.extend_cyclotron(parent['id'],['new'],name='Anthus')
    assert version['config']['cyclotron_revision']==2
    assert [row['id'] for row in store.items(version['id'])]==['seen','unseen']
    assert [row['id'] for row in version['config']['classifiers']]==['old','new']
    assert store.inherited_labels(version['id'],'seen')[0]['comment']=='Reason'
    assert store.run(parent['id'])['config']==config
    assert store.counts(version['id'])['labels']==0
    assert store.cyclotron_versions(version['config']['cyclotron_id'])[0]['run_id']==parent['id']
    store.activate_cyclotron_version(version['config']['cyclotron_id'],1)
    assert store.cyclotrons()[0]['active_revision']==1
    import pytest
    with pytest.raises(ValueError,match='activate'):
        store.command(version['id'],'inactive','prepare',{})


def test_changing_one_classifier_changes_the_complete_cyclotron_checkpoint(tmp_path):
    from types import SimpleNamespace
    from .classifier_config import ClassifierConfig
    from .models import DecisionTask
    from .flywheel import FittedClassifier
    store=WebStore(tmp_path/'web.sqlite')
    row=store.save_classifier('a','A',{'question':'Choose','classes':[{'label':'yes'},{'label':'no'}]})
    run=store.create_run('A','live',{'classifiers':[row]})
    config=ClassifierConfig(DecisionTask('a',('yes','no'),'Choose','text'))
    wheel=SimpleNamespace(active=FittedClassifier(config))
    first=store.checkpoint_cyclotron(run['id'],{'a':wheel})
    assert store.checkpoint_cyclotron(run['id'],{'a':wheel})==first
    wheel.active=FittedClassifier(ClassifierConfig(config.task,'A refined rubric'))
    assert store.checkpoint_cyclotron(run['id'],{'a':wheel})!=first
    assert len(store.cyclotron_checkpoints(run['id']))==2


def test_a_cyclotron_cannot_be_extended_while_a_submission_is_in_progress(tmp_path):
    import pytest
    store=WebStore(tmp_path/'web.sqlite')
    parent=store.create_run('A','live',{'classifiers':[],'item_list_id':'list'})
    store.command(parent['id'],'pending','prepare',{})
    with pytest.raises(ValueError,match='current work'):
        store.extend_cyclotron(parent['id'],['new'],name='A')


def test_protected_comparison_preflight_reads_pinned_checkpoints_without_writing_or_models(tmp_path):
    from types import SimpleNamespace
    from .classifier_config import ClassifierConfig
    from .models import DecisionTask
    from .flywheel import FittedClassifier
    store=WebStore(tmp_path/'db')
    classifier=store.save_classifier('topic','Topic',{'question':'Choose','classes':[{'label':'yes'},{'label':'no'}]})
    config={'classifiers':[classifier],'evaluation_protocol':'protected-feedback-v1'}
    items=[{'id':'one','revision':1,'fingerprint':'one','values':{'text':'An abstract'}}]
    runs=[store.create_run(side,'live',config,items=items) for side in ('Before','After')]
    wheel=SimpleNamespace(active=FittedClassifier(ClassifierConfig(DecisionTask('topic',('yes','no'),'Choose'))))
    for run in runs:
        store.append_event(run['id'],'vote',{'kind':'human-feedback','classifier_id':'topic','assignment':'scoreboard','action':'submitted','feedback':{'item_id':'one','final_answer_value':'yes'}})
        store.checkpoint_cyclotron(run['id'],{'topic':wheel})
    original=[store.all_events(run['id']) for run in runs]
    plan=store.matched_run_preflight(runs[0]['id'],runs[1]['id'])
    assert plan['sample_count']==1 and plan['request_upper_bound']==2
    assert [store.all_events(run['id']) for run in runs]==original

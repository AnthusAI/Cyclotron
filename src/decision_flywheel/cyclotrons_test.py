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
    from .flywheel import FittedClassifier
    from .models import DecisionTask
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
    from .flywheel import FittedClassifier
    from .models import DecisionTask
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


def _run_with_supporting_question(tmp_path):
    from types import SimpleNamespace

    from .classifier_config import ClassifierConfig
    from .flywheel import FittedClassifier
    from .models import DecisionTask
    from .workspace_session import freeze_configuration
    store=WebStore(tmp_path/'web.sqlite')
    store.save_classifier('relevant','Relevant',{'question':'Include?','classes':[{'label':'include','role':'positive'},{'label':'exclude','role':'negative'}]})
    store.save_item_list('list','List')
    store.upsert_list_items('list',[{'id':key,'occurred_at':'2026-01-01','values':{'text':key}} for key in ('seen','unseen')])
    config=freeze_configuration(store,{'classifier_ids':['relevant'],'item_list_id':'list','max_requests':100,
        'max_optimizer_calls':0,'optimize_every':20,'rubric_changes_every':2,'seed':'seed'})
    parent=store.create_run('Newsroom','live',config,items=store.list_items('list'))
    store.label_item('relevant',1,'list','seen',1,'include','Primary source','vote-1')
    vendor=DecisionTask('names_vendor',('yes','no'),'Does it name a vendor?','text')
    learned=ClassifierConfig(DecisionTask('relevant',('include','exclude'),'Include?','text'),'Learned rubric',tasks=(vendor,))
    store.checkpoint_cyclotron(parent['id'],{'relevant':SimpleNamespace(active=FittedClassifier(learned))})
    return store,parent,config


def test_a_supporting_question_becomes_an_unlabeled_output_of_a_new_version(tmp_path):
    store,parent,config=_run_with_supporting_question(tmp_path)
    version=store.promote_supporting_question(parent['id'],'relevant','names_vendor',classifier_id='vendor',
                                              classifier_name='Names a vendor',name='Newsroom',positive_label='yes')
    assert version['config']['cyclotron_revision']==2
    assert [row['id'] for row in version['config']['classifiers']]==['relevant','vendor']
    promoted=store.classifier('vendor')['config']
    assert promoted['question']=='Does it name a vendor?' and promoted['seed_rubric']=='Does it name a vendor?'
    assert promoted['classes']==[{'label':'yes','role':'positive'},{'label':'no','role':'negative'}]
    assert promoted['promoted_from']['classifier_id']=='relevant' and promoted['promoted_from']['question']=='names_vendor'
    # Earlier evidence is kept; the new output starts unlabeled.
    assert [vote['classifier_id'] for vote in store.inherited_labels(version['id'],'seen')]==['relevant']
    # The source keeps its supporting question in the new version; the parent run is unchanged.
    assert version['config']['seed_supporting_questions']=={'relevant':[
        {'name':'names_vendor','instructions':'Does it name a vendor?','labels':['yes','no']}]}
    assert store.run(parent['id'])['config']==config


def test_the_new_version_starts_with_the_question_rubric_and_the_source_features_unchanged(tmp_path):
    from .batched_classification import BatchedAnswers
    from .flywheel_test import agent
    from .models import DecisionResult
    from .workspace_session import WorkspaceSession
    store,parent,_=_run_with_supporting_question(tmp_path)
    version=store.promote_supporting_question(parent['id'],'relevant','names_vendor',classifier_id='vendor',
                                              classifier_name='Names a vendor',name='Newsroom')
    class Model:
        model_identity='fake';requests=[]
        async def classify_many(self,configs,target,training,**kwargs):
            self.requests.append(configs)
            answer=lambda labels:DecisionResult(labels[0],{labels[0]:.6,labels[1]:.4})
            return BatchedAnswers({cid:{'decision':answer(config.task.labels),
                                        **{task.name:answer(task.labels) for task in config.tasks}}
                                   for cid,config in configs.items()},'fake',{},1)
    session=WorkspaceSession(store,version,tmp_path/'run',Model(),agent([]),lambda _:None)
    try:
        assert session.wheels['vendor'].active.config.rubric=='Does it name a vendor?'
        assert session.wheels['vendor'].active.config.tasks==()
        assert [task.name for task in session.wheels['relevant'].active.config.tasks]==['names_vendor']
    finally:
        session.close()


def test_promotion_needs_a_checkpointed_question_and_a_new_classifier_id(tmp_path):
    import pytest
    store,parent,_=_run_with_supporting_question(tmp_path)
    with pytest.raises(ValueError,match='no such supporting question'):
        store.promote_supporting_question(parent['id'],'relevant','missing',classifier_id='x',classifier_name='X',name='N')
    with pytest.raises(ValueError,match='unknown source classifier'):
        store.promote_supporting_question(parent['id'],'other','names_vendor',classifier_id='x',classifier_name='X',name='N')
    with pytest.raises(ValueError,match='new classifier id'):
        store.promote_supporting_question(parent['id'],'relevant','names_vendor',classifier_id='relevant',
                                          classifier_name='X',name='N')
    with pytest.raises(ValueError,match='positive label'):
        store.promote_supporting_question(parent['id'],'relevant','names_vendor',classifier_id='x',classifier_name='X',
                                          name='N',positive_label='maybe')
    bare=store.create_run('Bare','live',store.run(parent['id'])['config'],items=store.list_items('list'))
    with pytest.raises(ValueError,match='checkpoint'):
        store.promote_supporting_question(bare['id'],'relevant','names_vendor',classifier_id='x',classifier_name='X',name='N')

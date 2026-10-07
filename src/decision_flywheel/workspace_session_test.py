"""The source-neutral session predicts before learning independent human labels."""
import asyncio
import pytest
from .workspace_session import WorkspaceSession
from .web_store import WebStore
from .batched_classification import BatchedAnswers
from .models import DecisionResult
from .observability import StepFailed
from .flywheel_test import agent


def test_protected_audit_votes_do_not_trigger_learning_or_enter_optimizer_context(tmp_path):
    from types import SimpleNamespace
    from .workspace_session import freeze_configuration
    from .feedback import FeedbackItem,LABEL_SOURCE_VETTED
    store=WebStore(tmp_path/'db')
    store.save_classifier('a','A',{'question':'Choose','classes':[{'label':'yes'},{'label':'no'}]})
    store.save_item_list('items','Items')
    store.upsert_list_items('items',[{'id':'one','occurred_at':'2026-01-01','values':{'text':'Text'}}])
    config=freeze_configuration(store,{'classifier_ids':['a'],'item_list_id':'items','max_requests':10,'max_optimizer_calls':3,'optimize_every':1,'rubric_changes_every':1,'seed':'test'})
    run=store.create_run('Audit','live',config,items=store.list_items('items'))
    session=WorkspaceSession(store,run,tmp_path/'run',SimpleNamespace(model_identity='fake'),agent([]),lambda e:None)
    checks=[];calls=[]
    wheel=session.wheels['a']
    async def forbidden(*args,**kwargs):calls.append(True);raise AssertionError('protected feedback triggered optimization')
    wheel.step=forbidden
    try:
        for identifier,label,role,comment in [('earlier','yes','training','Eligible explanation'),('one','no','scoreboard','Sealed audit explanation')]:
            wheel.record_feedback_event(FeedbackItem(identifier,identifier,'a',final_answer_value=label,edit_comment_value=comment,label_source=LABEL_SOURCE_VETTED,selection_propensity=1.,review_provenance='human'),assignment=role)
        asyncio.run(session.optimize('a',SimpleNamespace(check_trigger=lambda stage,**check:checks.append((stage,check)))))
        assert not calls
        assert all(not check['due'] for _,check in checks)
        assert 'Sealed audit explanation' not in str(wheel.optimizer_context)
    finally:session.close()


def test_restart_finishes_a_cycle_with_already_durable_feedback_without_repeating_prediction_or_optimization(tmp_path):
    from .workspace_session import freeze_configuration
    from .feedback import FeedbackItem,LABEL_SOURCE_VETTED
    from .models import Item
    store=WebStore(tmp_path/'web.sqlite')
    store.save_classifier('a','A',{'question':'Choose','classes':[{'label':'yes'},{'label':'no'}]})
    store.save_item_list('list','List')
    store.upsert_list_items('list',[{'id':'one','occurred_at':'2026-01-01','values':{'text':'Text'}}])
    config=freeze_configuration(store,{'classifier_ids':['a'],'item_list_id':'list','max_requests':10,'max_optimizer_calls':3,'optimize_every':20,'rubric_changes_every':2,'seed':'test'})
    run=store.create_run('Restart','live',config,items=store.list_items('list'))
    class Model:
        model_identity='fake'
        calls=0
        async def classify_many(self,configs,target,training,**kwargs):
            self.calls+=1
            return BatchedAnswers({'a':{'decision':DecisionResult('yes',{'yes':.7,'no':.3})}},'fake',{},1)
    model=Model();events=[]
    session=WorkspaceSession(store,run,tmp_path/'run',model,agent([]),events.append)
    shown=asyncio.run(session.prepare())
    wheel=session.wheels['a'];cycle=wheel.resume_cycle(Item('one',{'text':'Text'}))
    wheel.record_feedback_event(FeedbackItem('vote:a','one','a',initial_answer_value='yes',final_answer_value='yes',edit_comment_value='Knowledge bases',label_source=LABEL_SOURCE_VETTED,selection_propensity=1.,review_provenance='interactive-human-vote'),assignment='training')
    # This durable point represents a process loss after recording feedback,
    # before metrics and cycle completion. No completed work may be retried.
    cycle.suspend();session.close()
    session=WorkspaceSession(store,run,tmp_path/'run',model,agent([]),events.append)
    async def forbidden(*args,**kwargs):raise AssertionError('completed feedback must not repeat optimization')
    session.optimize=forbidden
    try:
        asyncio.run(session.feedback({'item_id':'one','presentation_id':shown['prediction']['presentation_id'],'labels':[{'classifier_id':'a','label':'yes','comment':'Knowledge bases'}]},'vote'))
        assert model.calls==1
        history=session.wheels['a'].history(100000)
        assert len([e for e in history if e['kind']=='human-feedback'])==1
        assert len([e for e in history if e['kind']=='cycle-completed'])==1
        metrics=next(e for e in reversed(history) if e['kind']=='cycle-metrics')
        assert metrics['cycle_id']==next(e['cycle_id'] for e in history if e['kind']=='prediction')
        assert asyncio.run(session.prepare())=={'finished':True}
    finally:session.close()


def test_backfill_predicts_from_zero_then_replays_old_feedback_with_a_new_human_label(tmp_path):
    store=WebStore(tmp_path/'web.sqlite')
    for key in ('a','b'):
        store.save_classifier(key,key,{'question':'Choose','classes':[{'label':'yes'},{'label':'no'}]})
    store.save_item_list('list','List');store.upsert_list_items('list',[{'id':'one','occurred_at':'2026-01-01','values':{'text':'Paper'}}])
    from .workspace_session import freeze_configuration
    config=freeze_configuration(store,{'classifier_ids':['a'],'item_list_id':'list','max_requests':10,'max_optimizer_calls':3,'optimize_every':20,'rubric_changes_every':2,'seed':'seed'})
    parent=store.create_run('Anthus','live',config,items=store.list_items('list'))
    store.label_item('a',1,'list','one',1,'no','Original explanation','original')
    run=store.extend_scorecard(parent['id'],['b'],name='Anthus')
    class Model:
        model_identity='fake'
        async def classify_many(self,configs,target,training,**kwargs):
            assert all(not rows for rows in training.values())
            assert all(not c.rubric and not c.example_ids for c in configs.values())
            return BatchedAnswers({key:{'decision':DecisionResult('yes',{'yes':.8,'no':.2})} for key in configs},'fake',{},1)
    events=[];session=WorkspaceSession(store,run,tmp_path/'run',Model(),agent([]),events.append)
    try:
        shown=asyncio.run(session.prepare())
        assert shown['prediction']['recorded_labels'][0]['comment']=='Original explanation'
        asyncio.run(session.feedback({'item_id':'one','presentation_id':shown['prediction']['presentation_id'],'labels':[{'classifier_id':'b','label':'yes','comment':'New rubric'}]},'new-vote'))
        feedback=[e['feedback'] for e in events if e['kind']=='human-feedback']
        assert {f['item_id'] for f in feedback}=={'one'}
        assert {f['edit_comment_value'] for f in feedback}=={'Original explanation','New rubric'}
        assert len(store.item_labels('list','one',1))==2
    finally:session.close()


def test_two_classifiers_share_prediction_and_keep_feedback_separate_after_restart(tmp_path):
    store=WebStore(tmp_path/'web.sqlite')
    for key in ('a','b'):store.save_classifier(key,key,{'question':'Choose','classes':[{'label':'yes'},{'label':'no'}]})
    store.save_item_list('list','List');store.upsert_list_items('list',[{'id':'one','occurred_at':'2026-01-01','values':{'text':'Text'}}])
    config={'classifier_ids':['a','b'],'item_list_id':'list','max_requests':10,'max_optimizer_calls':3,'optimize_every':20,'rubric_changes_every':2,'seed':'test'}
    from .workspace_session import freeze_configuration
    config=freeze_configuration(store,config)
    run=store.create_run('Test','live',config,items=store.list_items('list'))
    class Model:
        model_identity='fake'
        calls=0
        async def classify_many(self,configs,target,training,**kwargs):
            self.calls+=1
            return BatchedAnswers({key:{'decision':DecisionResult('yes',{'yes':.7,'no':.3})} for key in configs},'fake',{},1)
    model=Model();events=[]
    session=WorkspaceSession(store,run,tmp_path/'run',model,agent([]),events.append)
    shown=asyncio.run(session.prepare()); assert len(shown['prediction']['classifiers'])==2
    assert model.calls==1
    session.close()
    session=WorkspaceSession(store,run,tmp_path/'run',model,agent([]),events.append)
    asyncio.run(session.feedback({'item_id':'one','presentation_id':shown['prediction']['presentation_id'],
        'labels':[{'classifier_id':'a','label':'yes','comment':'My first rubric'},{'classifier_id':'b','label':'no','comment':'Different rubric'}]},'vote'))
    assert model.calls==1
    labels=store.item_labels('list','one',1)
    assert {v['classifier_id']:v['label'] for v in labels}=={'a':'yes','b':'no'}
    assert store.item_results('list','one',1)[0]['payload']['classifiers']['a']['label']=='yes'
    assert asyncio.run(session.prepare())=={'finished':True}
    feedback=[event for event in events if event['kind']=='human-feedback']
    assert {e['classifier_id'] for e in feedback}=={'a','b'}
    assert all(e['metrics']['window_size']==200 and e['metrics']['count']==1 for e in events if e['kind']=='cycle-metrics')
    session.close()


def test_optimizer_traces_belong_only_to_the_classifier_that_requested_them(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from .workspace_session import freeze_configuration
    store=WebStore(tmp_path/'web.sqlite')
    for key in ('a','b'):store.save_classifier(key,key,{'question':'Choose','classes':[{'label':'yes'},{'label':'no'}]})
    store.save_item_list('list','List');store.upsert_list_items('list',[{'id':'one','occurred_at':'2026-01-01','values':{'text':'Text'}}])
    config=freeze_configuration(store,{'classifier_ids':['a','b'],'item_list_id':'list','max_requests':10,'max_optimizer_calls':3,'optimize_every':20,'rubric_changes_every':2,'seed':'test'})
    run=store.create_run('Test','live',config,items=store.list_items('list'))
    events=[]
    session=WorkspaceSession(store,run,tmp_path/'run',SimpleNamespace(model_identity='fake'),agent([]),events.append)
    try:
        session.wheels['a'].optimizer.observer({'kind':'optimizer-request','messages':[]})
        assert [event['classifier_id'] for event in events if event['kind']=='optimizer-request']==['a']
        transport=session.wheels['a'].optimizer.complete
        transport.calls=3;transport.max_calls=3
        monkeypatch.setattr('decision_flywheel.workspace_session.LabelTransitionTrigger.check',lambda *args:{'due':True,'reason':'test cadence','details':{}})
        cycle=SimpleNamespace(check_trigger=lambda *args,**kwargs:None)
        warnings=asyncio.run(session.optimize('a',cycle))
        assert warnings[0]['reason']=='optimizer call limit reached; labeling can continue'
        assert transport.calls==3
        assert len([event for event in events if event['kind']=='optimizer-request'])==1
        assert any(event['kind']=='optimization-paused' for event in events)
    finally:session.close()


@pytest.mark.parametrize('failure',[StepFailed,RuntimeError])
def test_failed_learning_does_not_lose_votes_or_block_the_remaining_classifier_labels(tmp_path,failure):
    from .workspace_session import freeze_configuration
    from .observability import StepFailed
    store=WebStore(tmp_path/'web.sqlite')
    for key in ('a','b'):store.save_classifier(key,key,{'question':'Choose','classes':[{'label':'yes'},{'label':'no'}]})
    store.save_item_list('list','List');store.upsert_list_items('list',[{'id':'one','occurred_at':'2026-01-01','values':{'text':'Text'}}])
    config=freeze_configuration(store,{'classifier_ids':['a','b'],'item_list_id':'list','max_requests':10,'max_optimizer_calls':3,'optimize_every':20,'rubric_changes_every':2,'seed':'test'})
    run=store.create_run('Test','live',config,items=store.list_items('list'))
    class Model:
        model_identity='fake'
        async def classify_many(self,configs,target,training,**kwargs):
            return BatchedAnswers({key:{'decision':DecisionResult('yes',{'yes':.7,'no':.3})} for key in configs},'fake',{},1)
    events=[];session=WorkspaceSession(store,run,tmp_path/'run',Model(),agent([]),events.append)
    shown=asyncio.run(session.prepare())
    attempts=[]
    async def fail_learning(identifier,cycle):
        attempts.append(identifier)
        raise failure('optimization step did not complete')
    session.optimize=fail_learning
    payload={'item_id':'one','presentation_id':shown['prediction']['presentation_id'],'labels':[{'classifier_id':key,'label':'yes'} for key in ('a','b')]}
    try:
        result=asyncio.run(session.feedback(payload,'vote'))
        assert result['reviewed']=='one'
        assert len(result['optimization_warnings'])==2
        assert len(store.item_labels('list','one',1))==2
        assert len([e for e in events if e['kind']=='cycle-metrics'])==2
        # Recovery of a persisted command must not repeat learning or votes.
        store.update_item(run['id'],'one',prediction=shown['prediction'],reviewed=False)
        asyncio.run(session.feedback(payload,'vote'))
        assert attempts==['a','b']
        assert len(store.item_labels('list','one',1))==2
        stages=[]
        async def resume_learning(identifier,cycle,*,resume_stages=()):
            stages.append((identifier,resume_stages))
            return []
        session.optimize=resume_learning
        result=asyncio.run(session.resume_optimization())
        assert len(result['resumed'])==2
        assert stages==[(key,('rubric','questions','examples','classifier')) for key in ('a','b')]
        assert len(store.item_labels('list','one',1))==2
    finally:session.close()

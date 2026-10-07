"""The web application drives the existing engine and acknowledges API traces."""
import json
from dataclasses import asdict
from .web_store import WebStore
from .web_worker import WebWorker
from .web_api import create_app
from .api_event_sink import GraphQLTraceSink
from .flywheel_test import FakeModel, agent
from .reviewer_store import Article
from fastapi.testclient import TestClient

def test_legacy_web_metrics_use_recorded_probability_vectors_and_calibration_provenance():
    from types import SimpleNamespace
    events=[{'kind':'prediction','event_id':1,'target_id':'paper','label':'include','version':'head-v1',
             'probabilities':{'include':.6,'exclude':.4},'confidence':.99,'fitted_head':True,
             'uncalibrated_probabilities':{'include':.9,'exclude':.1},'calibration_temperature':2.,
             'calibration_provenance':{'fit_on':'oof','training_ids':['trusted']}},
            {'kind':'human-feedback','event_id':2,'action':'submitted',
             'feedback':{'item_id':'paper','final_answer_value':'include'}}]
    emitted=[]
    reviewer=SimpleNamespace(core=SimpleNamespace(initial=SimpleNamespace(task=SimpleNamespace(labels=('include','exclude'))),
        history=lambda limit:tuple(events),_emit=emitted.append))
    WebWorker._metrics(reviewer)
    curve=emitted[0]['metrics']['calibration']
    assert curve['ece']==__import__('pytest').approx(.4)
    assert curve['samples'][0]['prediction_event_id']==1
    assert curve['samples'][0]['calibration_provenance']['training_ids']==['trusted']
    assert curve['matched_head_comparison']['raw']['ece']==__import__('pytest').approx(.1)


def test_a_live_run_pins_the_selected_scorecard_definition_not_latest_classifiers(tmp_path):
    store=WebStore(tmp_path/'db')
    config={'question':'Choose','input_field':'text','classes':[{'label':'yes'},{'label':'no'}]}
    store.save_classifier('a','A',config)
    store.save_scorecard_definition('card','Card',[{'id':'a','revision':1}],{})
    store.save_classifier('a','New A',{**config,'question':'New question'})
    store.save_item_list('items','Items')
    store.upsert_list_items('items',[{'id':'one','occurred_at':'2026-01-01','values':{'text':'Text'}}])
    worker=WebWorker(store,tmp_path/'runs',allow_live=True,sink_factory=lambda _:None,model_factory=lambda _:None)
    run=worker.create_run('Pinned',{'scorecard_id':'card','scorecard_definition_revision':1,'item_list_id':'items','selection_policy':{'primary':'accuracy','aggregation':'macro'}})
    assert run['config']['classifiers'][0]['revision']==1
    assert run['config']['scorecard_definition_revision']==1
    assert run['config']['scorecard_definition_fingerprint']==store.scorecard_definition('card',1)['fingerprint']
    assert store.counts(run['id'])['predictions']==0


def test_replay_creation_freezes_labels_and_explanations_but_starts_with_no_learning(tmp_path):
    store=WebStore(tmp_path/'db')
    classifier=store.save_classifier('a','A',{'question':'Choose','input_field':'text','classes':[{'label':'yes'},{'label':'no'}]})
    store.save_scorecard_definition('card','Card',[{'id':'a','revision':1}],{})
    store.save_item_list('items','Items')
    store.upsert_list_items('items',[{'id':'one','occurred_at':'2026-01-01','values':{'text':'Text'}}])
    source=store.create_run('Source','live',{'classifiers':[classifier],'item_list_id':'items'},items=store.list_items('items'))
    store.append_event(source['id'],'vote',{'kind':'human-feedback','classifier_id':'a','feedback':{'id':'original','item_id':'one','final_answer_value':'yes','edit_comment_value':'Because knowledge bases'}})
    worker=WebWorker(store,tmp_path/'runs',allow_live=True,sink_factory=lambda _:None,model_factory=lambda _:None)
    replay=worker.create_replay('Replay',source['id'],{'scorecard_id':'card','item_list_id':'items','selection_policy':{'primary':'accuracy','aggregation':'macro'}})
    assert replay['config']['input_mode']=='replay'
    assert store.inherited_labels(replay['id'],'one')[0]['comment']=='Because knowledge bases'
    assert store.counts(replay['id'])['labels']==0
    assert store.counts(replay['id'])['predictions']==0
    assert store.run(source['id'])['name']=='Source'
    from .workspace_session import WorkspaceSession
    from .batched_classification import BatchedAnswers
    from .models import DecisionResult
    class Model:
        model_identity='fake'
        calls=0
        async def classify_many(self,configs,target,training,**kwargs):
            self.calls+=1
            assert all(not examples for examples in training.values())
            assert all(not state.rubric and not state.example_ids for state in configs.values())
            return BatchedAnswers({key:{'decision':DecisionResult('yes',{'yes':.8,'no':.2})} for key in configs},'fake',{},1)
    model=Model()
    def observer(event):store.append_event(replay['id'],f"{event['classifier_id']}:{event.get('event_id',event['kind'])}",event)
    session=WorkspaceSession(store,replay,tmp_path/'replay',model,agent([]),observer)
    worker.sessions[replay['id']]=session
    try:
        store.command(replay['id'],'next','replay-next',{})
        worker.process(store.claim_command())
        assert store.jobs(replay['id'])[0]['status']=='completed'
        assert model.calls==1
        assert store.counts(replay['id'])['labels']==1
        metrics=[e['payload']['metrics'] for e in store.all_events(replay['id']) if e['payload'].get('kind')=='cycle-metrics']
        assert metrics[-1]['calibration']['count']==1
        assert len(store.item_labels('items','one',1))==0
    finally:session.close()


def test_a_temporary_storage_error_does_not_abandon_queued_feedback(tmp_path):
    import sqlite3
    from threading import Event
    store=WebStore(tmp_path/'web.sqlite')
    worker=WebWorker(store,tmp_path/'runs',sink_factory=lambda _:None,model_factory=lambda _:None)
    processed=Event()
    attempts=[]
    def claim():
        attempts.append(True)
        if len(attempts)==1:
            raise sqlite3.OperationalError('disk I/O error')
        return {'id':'already-saved-feedback'}
    store.claim_command=claim
    def process(job):
        assert job['id']=='already-saved-feedback'
        processed.set()
        worker.stopping.set()
    worker.process=process
    worker.start()
    try:
        assert processed.wait(3), 'the persisted command must survive a temporary read failure'
        assert len(attempts)==2
    finally:
        worker.close()


def test_replay_resumes_chronological_cycles_after_restart_without_repeating_completed_requests(tmp_path):
    from .batched_classification import BatchedAnswers
    from .models import DecisionResult
    store=WebStore(tmp_path/'db')
    classifier=store.save_classifier('a','A',{'question':'Choose','input_field':'text','classes':[{'label':'yes'},{'label':'no'}]})
    store.save_scorecard_definition('card','Card',[{'id':'a','revision':1}],{})
    store.save_item_list('items','Items')
    store.upsert_list_items('items',[{'id':key,'occurred_at':f'2026-01-0{i+1}','values':{'text':key}} for i,key in enumerate(('one','two'))])
    source=store.create_run('Source','live',{'classifiers':[classifier],'item_list_id':'items'},items=store.list_items('items'))
    for key in ('one','two'):
        store.append_event(source['id'],key,{'kind':'human-feedback','classifier_id':'a','feedback':{'id':f'original-{key}','item_id':key,'final_answer_value':'yes','edit_comment_value':f'Explanation {key}'}})
    class Model:
        model_identity='fake'
        calls=[]
        async def classify_many(self,configs,target,training,**kwargs):
            self.calls.append(target.id)
            assert all(row.item.id!=target.id for rows in training.values() for row in rows)
            if target.id=='one':
                assert all(not rows for rows in training.values())
                assert all(not c.rubric and not c.example_ids for c in configs.values())
            return BatchedAnswers({'a':{'decision':DecisionResult('yes',{'yes':.8,'no':.2})}},'fake',{},1)
    model=Model()
    with TestClient(create_app(store)) as client:
        factory=lambda run_id:GraphQLTraceSink('unused',run_id,transport=lambda body:client.post('/graphql',json=body).json())
        options=dict(allow_live=True,sink_factory=factory,model_factory=lambda _:(model,agent([])))
        worker=WebWorker(store,tmp_path/'runs',**options)
        replay=worker.create_replay('Replay',source['id'],{'scorecard_id':'card','item_list_id':'items','selection_policy':{'primary':'accuracy','aggregation':'macro'}})
        store.command(replay['id'],'step-one','replay-next',{})
        worker.process(store.claim_command())
        assert store.jobs(replay['id'])[0]['status']=='completed'
        worker.close()
        # Later source edits cannot change this replay's frozen explanations.
        store.append_event(source['id'],'later',{'kind':'human-feedback','classifier_id':'a','feedback':{'id':'changed','item_id':'two','final_answer_value':'no','edit_comment_value':'Later correction'}})
        worker=WebWorker(store,tmp_path/'runs',**options)
        try:
            store.command(replay['id'],'step-two','replay-next',{})
            worker.process(store.claim_command())
            assert model.calls==['one','two']
            events=[row['payload'] for row in store.all_events(replay['id'])]
            predictions=[e for e in events if e['kind']=='prediction']
            votes=[e for e in events if e['kind']=='human-feedback']
            assert [e['target_id'] for e in predictions]==['one','two']
            assert [e['feedback']['edit_comment_value'] for e in votes]==['Explanation one','Explanation two']
            assert [e['cycle_id'] for e in predictions]==[e['cycle_id'] for e in votes]
            assert len([e for e in events if e['kind']=='cycle-completed'])==2
            assert [e['metrics']['count'] for e in events if e['kind']=='cycle-metrics']==[1,2]
            assert not store.item_labels('items','one',1)
            store.command(replay['id'],'finish','replay-next',{})
            worker.process(store.claim_command())
            assert store.jobs(replay['id'])[0]['result']=={'finished':True}
            assert model.calls==['one','two']
        finally:worker.close()


def test_failed_command_identifies_code_locations_without_exposing_exception_text(tmp_path):
    store=WebStore(tmp_path/'web.sqlite')
    def fail(config):
        raise TypeError('private provider credential')
    worker=WebWorker(store,tmp_path/'runs',sink_factory=lambda _:None,model_factory=fail,allow_live=True)
    run=worker.create_run('Broken',{})
    store.command(run['id'],'prepare','prepare',{})
    worker.process(store.claim_command())
    result=store.jobs(run['id'])[0]['result']
    assert result['code_locations'][-1]['function']=='fail'
    assert 'private provider credential' not in json.dumps(result)
    worker.close()


def test_catalog_session_commands_use_one_shared_model_request_and_keep_labels_in_api(tmp_path):
    from .adapters.jev import JevAdapter
    from types import SimpleNamespace
    store=WebStore(tmp_path/'web.sqlite')
    for identifier in ('one','two'):store.save_classifier(identifier,identifier,{'question':'Choose','classes':[{'label':'yes'},{'label':'no'}]})
    store.save_item_list('items','Items')
    store.upsert_list_items('items',[{'id':'paper','occurred_at':'2026-01-01','values':{'text':'Article'}},
                                   {'id':'next-paper','occurred_at':'2026-01-02','values':{'text':'Next article'}}])
    class ModelClient:
        calls=0
        def system_one(self,**request):
            self.calls+=1
            return SimpleNamespace(answers={name:{'choice':'yes','probabilities':{'yes':.8,'no':.2}} for name in request['questions']},model='fake',usage={'tokens':5})
    model=ModelClient();client=TestClient(create_app(store))
    sink=lambda run_id:GraphQLTraceSink('unused',run_id,transport=lambda body:client.post('/graphql',json=body).json())
    worker=WebWorker(store,tmp_path/'runs',sink_factory=sink,model_factory=lambda config:(JevAdapter(model),agent([])),allow_live=True)
    run=worker.create_run('General',{'classifier_ids':['one','two'],'item_list_id':'items'})
    store.command(run['id'],'prepare','prepare',{});worker.process(store.claim_command())
    shown=store.current_item(run['id']);assert model.calls==1
    store.command(run['id'],'vote','label',{'item_id':'paper','presentation_id':shown['prediction']['presentation_id'],
        'labels':[{'classifier_id':'one','label':'yes','comment':'Reason one'},{'classifier_id':'two','label':'no','comment':'Reason two'}]})
    worker.process(store.claim_command())
    assert next(job for job in store.jobs(run['id']) if job['kind']=='label')['status']=='completed'
    assert len(store.item_labels('items','paper',1))==2
    assert len(store.item_results('items','paper',1))==1
    assert {event['payload']['classifier_id'] for event in store.all_events(run['id']) if event['payload']['kind']=='human-feedback'}=={'one','two'}
    assert sum((event['payload'].get('usage') or {}).get('tokens',0) for event in store.all_events(run['id']))==5
    continuation=store.claim_command()
    assert continuation is not None and continuation['kind']=='prepare'
    assert continuation['request_id'].startswith('after-feedback:')
    worker.process(continuation)
    assert store.current_item(run['id'])['item']['id']=='next-paper'
    assert model.calls==2
    assert len(store.item_labels('items','paper',1))==2
    worker.close()


def test_prepare_then_explained_vote_records_a_real_cycle_through_graphql(tmp_path):
    store = WebStore(tmp_path / 'web.sqlite')
    article = asdict(Article('paper','A research paper','Real abstract','2026-10-06',('cs.AI',)))
    run = store.create_run('Test','live',{'selection_policy':{'primary':'f1','positive_class':'include'},
        'max_requests':30,'max_optimizer_calls':2,'seed':'fixture','optimize_every':20,'rubric_changes_every':2},items=[article])
    client = TestClient(create_app(store))
    factory = lambda run_id: GraphQLTraceSink('not-used',run_id,transport=lambda body:client.post('/graphql',json=body).json())
    worker = WebWorker(store,tmp_path / 'runs',sink_factory=factory,
        model_factory=lambda config:(FakeModel(),agent([])),allow_live=True)
    prepare = store.command(run['id'],'prepare-1','prepare',{})
    worker.process(store.claim_command())
    current = store.current_item(run['id'])
    assert current['item']['title'] == 'A research paper'
    assert current['prediction']['label'] == 'exclude'
    assert store.jobs(run['id'])[0]['status'] == 'completed'
    label = store.command(run['id'],'vote-1','label',{'item_id':'paper','label':'include','comment':'About knowledge access',
        'presentation_id':current['prediction']['presentation_id']})
    worker.process(store.claim_command())
    events = [r['payload'] for r in store.all_events(run['id'])]
    prediction = next(e for e in events if e['kind']=='prediction')
    feedback = next(e for e in events if e['kind']=='human-feedback')
    assert prediction['cycle_id'] == feedback['cycle_id']
    assert feedback['feedback']['edit_comment_value'] == 'About knowledge access'
    assert any(e['kind']=='cycle-completed' for e in events)
    assert store.current_item(run['id']) is None
    worker.close()


def test_live_run_creation_requires_positive_ceiling_and_explicit_selection_policy(tmp_path):
    import pytest
    store = WebStore(tmp_path / 'web.sqlite')
    worker = WebWorker(store,tmp_path / 'runs',sink_factory=lambda _:None,model_factory=lambda _:None,allow_live=True)
    with pytest.raises(ValueError):
        worker.create_run('Bad', {'max_requests':0})
    assert store.runs() == []


def test_engine_optimizer_and_decision_exchanges_are_ingested_with_full_context(tmp_path):
    import asyncio
    from .adapters.jev import JevAdapter
    from .classifier_config import ClassifierConfig
    from .flywheel import DecisionFlywheel
    from .flywheel_test import TASK, TRAIN, DEV
    from .optimizer_agent import OptimizerAgent, OptimizerReply
    store = WebStore(tmp_path / 'web.sqlite')
    run = store.create_run('Exchanges', 'recorded', {})
    with TestClient(create_app(store)) as client:
        sink = GraphQLTraceSink('unused',run['id'],transport=lambda body:client.post('/graphql',json=body).json())
        class Client:
            def system_one(self, **request):
                from types import SimpleNamespace
                return SimpleNamespace(answers={name:{'value':'include','confidence':.8,
                    'probabilities':{'include':.8,'exclude':.2}} for name in request['questions']},model='fake',usage={})
        optimizer = OptimizerAgent(lambda messages: OptimizerReply('{"rubric":"Include knowledge-base research","rationale":"Human explanation"}', 'fake'))
        wheel = DecisionFlywheel(tmp_path / 'engine.sqlite',ClassifierConfig(TASK),JevAdapter(Client()),optimizer,observer=sink)
        wheel.set_optimizer_context(['Include papers about knowledge-base management'])
        asyncio.run(wheel.improve(TRAIN,DEV,protected=(),propensities={r.item.id:1. for r in TRAIN}))
        events = [row['payload'] for row in store.all_events(run['id'])]
        request = next(e for e in events if e['kind']=='optimizer-request')
        assert 'Include papers about knowledge-base management' in request['messages'][-1]['content']
        assert any(e['kind']=='optimizer-response' and 'knowledge-base research' in e['content'] for e in events)
        assert any(e['kind']=='decision-request' and e['state'] and e['questions'] for e in events)
        assert any(e['kind']=='decision-response' and e['answers'] for e in events)
        wheel.close()


def test_failed_api_ingestion_marks_work_failed_without_a_paid_model_call(tmp_path):
    store = WebStore(tmp_path / 'web.sqlite')
    worker = WebWorker(store,tmp_path / 'runs',articles=[asdict(Article('paper','Title','Abstract','2026-10-06',('cs.AI',)))],
        allow_live=True,sink_factory=lambda _:lambda event:None,model_factory=lambda _: (FakeModel(),agent([])))
    run = worker.create_run('API failure', {})
    reviewer = worker._runtime_session(run['id'])
    def unavailable(event):
        raise RuntimeError('API unavailable')
    reviewer.core.observer = unavailable
    store.command(run['id'],'prepare','prepare',{})
    worker.process(store.claim_command())
    assert store.jobs(run['id'])[0]['status'] == 'failed'
    assert reviewer.core.model.calls == 0
    worker.close()


def test_restart_accepts_feedback_for_the_displayed_prediction_in_the_same_cycle(tmp_path):
    store = WebStore(tmp_path / 'web.sqlite')
    client = TestClient(create_app(store))
    factory = lambda run_id:GraphQLTraceSink('unused',run_id,transport=lambda body:client.post('/graphql',json=body).json())
    kwargs = dict(sink_factory=factory,model_factory=lambda _: (FakeModel(),agent([])),allow_live=True,
        articles=[asdict(Article('paper','Title','Abstract','2026-10-06',('cs.AI',)))])
    worker = WebWorker(store,tmp_path / 'runs',**kwargs)
    run = worker.create_run('Restart', {})
    store.command(run['id'],'prepare','prepare',{})
    worker.process(store.claim_command())
    current = store.current_item(run['id'])
    prediction = next(row['payload'] for row in store.all_events(run['id']) if row['payload']['kind']=='prediction')
    worker.close()
    reopened = WebWorker(store,tmp_path / 'runs',**kwargs)
    store.command(run['id'],'vote','label',{'item_id':'paper','label':'include','comment':'Knowledge access',
        'presentation_id':current['prediction']['presentation_id']})
    reopened.process(store.claim_command())
    assert store.jobs(run['id'])[0]['status'] == 'completed'
    feedback = next(row['payload'] for row in store.all_events(run['id']) if row['payload']['kind']=='human-feedback')
    assert feedback['cycle_id'] == prediction['cycle_id']
    assert reopened.sessions[run['id']].core.model.calls == 0
    reopened.close()


def test_protected_feedback_cannot_fire_legacy_stage_cadences():
    from types import SimpleNamespace
    checks=[]; improvements=[]
    events=[{'kind':'human-feedback','action':'submitted','assignment':'train'},
            {'kind':'human-feedback','action':'submitted','assignment':'final_audit'}]
    reviewer=SimpleNamespace(core=SimpleNamespace(history=lambda _:events),
        feedback_trigger=lambda _:False,
        current_cycle=SimpleNamespace(check_trigger=lambda stage,**check:checks.append((stage,check))),
        improve=lambda **kwargs:improvements.append(kwargs))
    WebWorker._optimize(None,reviewer,{'optimize_every':1})
    assert improvements==[]
    assert all(not check['due'] for _,check in checks)
    assert all(check['details']['feedback_count']==1 for _,check in checks)


def test_new_runs_pin_shared_defaults_while_existing_runs_keep_their_settings(tmp_path):
    store=WebStore(tmp_path/'workspace.sqlite')
    store.save_classifier('topic','Topic',{'question':'Relevant?', 'classes':[{'label':'yes','role':'positive'},{'label':'no','role':'negative'}]})
    store.save_item_list('items','Items')
    store.upsert_list_items('items',[{'id':'paper','occurred_at':'2026-10-07','values':{'text':'An abstract'}}])
    refs=[{'id':'topic','revision':1}]
    settings={'optimize_every':10,'rubric_changes_every':2,'seed':'fixed','decisions_model':'fake-decision','optimizer_model':'fake-optimizer','selection_policy':{'primary':'recall','secondary':'accuracy','aggregation':'macro'}}
    store.save_scorecard_definition('card','Card',refs,settings)
    def forbidden(*args):raise AssertionError('creating a run must not instantiate providers')
    worker=WebWorker(store,tmp_path/'runs',sink_factory=forbidden,model_factory=forbidden,allow_live=True)
    first=worker.create_run('First',{'scorecard_id':'card','item_list_id':'items'})
    store.save_scorecard_definition('card','Card',refs,{**settings,'optimize_every':5})
    second=worker.create_run('Second',{'scorecard_id':'card','item_list_id':'items'})
    assert first['config']['optimize_every']==store.run(first['id'])['config']['optimize_every']==10
    assert second['config']['optimize_every']==5
    assert first['config']['classifiers'][0]['config']['selection_policy']=={'primary':'recall','secondary':'accuracy','aggregation':'positive','positive_class':'yes','max_secondary_regression':0.,'minimum_secondary':None}
    assert first['config']['decisions_model']=='fake-decision'

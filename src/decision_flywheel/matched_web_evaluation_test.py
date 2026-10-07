"""Comparison jobs are API-owned, approved, and independent of learning runs."""
from types import SimpleNamespace
import pytest
from .web_store import WebStore
from .web_worker import WebWorker
from .matched_run_evaluation_test import Model
from .flywheel import FittedClassifier
from .classifier_config import ClassifierConfig
from .models import DecisionTask


def runs(store):
    classifier=store.save_classifier('topic','Topic',{'question':'Choose','classes':[{'label':'yes','role':'positive'},{'label':'no','role':'negative'}]})
    items=[{'id':str(i),'revision':1,'fingerprint':str(i),'values':{'text':str(i)}} for i in range(4)]
    result=[]
    for name in ('Before','After'):
        run=store.create_run(name,'live',{'classifiers':[classifier],'evaluation_protocol':'protected-feedback-v1'},items=items)
        for i in range(4):
            store.append_event(run['id'],str(i),{'kind':'human-feedback','classifier_id':'topic','action':'submitted','assignment':'scoreboard','feedback':{'item_id':str(i),'final_answer_value':'yes' if i%2 else 'no'}})
        wheel=SimpleNamespace(active=FittedClassifier(ClassifierConfig(DecisionTask('topic',('yes','no'),'Choose'))))
        store.checkpoint_scorecard(run['id'],{'topic':wheel})
        result.append(run)
    return result


def test_creation_retry_recovers_the_same_frozen_job_after_restart_and_source_changes(tmp_path):
    store=WebStore(tmp_path/'db');before,after=runs(store)
    plan=store.matched_run_preflight(before['id'],after['id'])
    worker=WebWorker(store,tmp_path/'cache',allow_live=True)
    args=('Matched',before['id'],after['id'],plan['fingerprint'])
    first=worker.create_comparison(*args,max_requests=8,request_id='approval-1')
    frozen=store.matched_evaluation_inputs(first['id'])
    store.append_event(after['id'],'changed',{'kind':'human-feedback','classifier_id':'topic',
        'assignment':'training','feedback':{'item_id':'0','final_answer_value':'yes'}})
    restarted=WebWorker(WebStore(tmp_path/'db'),tmp_path/'cache',allow_live=True)
    assert restarted.create_comparison(*args,max_requests=8,request_id='approval-1')==first
    assert store.matched_evaluation_inputs(first['id'])==frozen
    assert len(store.jobs(first['id']))==1
    assert len(store.runs())==3
    with pytest.raises(ValueError,match='identity has different content'):
        restarted.create_comparison(*args,max_requests=9,request_id='approval-1')
    assert len(store.runs())==3


def test_duplicate_graphql_approval_returns_one_comparison_and_one_pending_job(tmp_path):
    from fastapi.testclient import TestClient
    from .web_api import create_app
    store=WebStore(tmp_path/'db');before,after=runs(store)
    plan=store.matched_run_preflight(before['id'],after['id'])
    worker=WebWorker(store,tmp_path/'cache',allow_live=True,
        model_factory=lambda _:pytest.fail('creation must not open a model'))
    client=TestClient(create_app(store,service=worker))
    query='mutation($before:ID!,$after:ID!,$plan:String!,$request:String!){createMatchedComparison(name:"Matched",beforeRunId:$before,afterRunId:$after,approvedFingerprint:$plan,maxRequests:8,confirmed:true,requestId:$request){id}}'
    variables={'before':before['id'],'after':after['id'],'plan':plan['fingerprint'],'request':'approval'}
    first=client.post('/graphql',json={'query':query,'variables':variables}).json()
    assert 'errors' not in first
    second=client.post('/graphql',json={'query':query,'variables':variables}).json()
    assert second==first
    assert len(store.jobs(first['data']['createMatchedComparison']['id']))==1
    assert len(store.runs())==3
    rejected=client.post('/graphql',json={'query':query,'variables':{**variables,'request':' '}}).json()
    assert 'identity required' in rejected['errors'][0]['message']
    assert len(store.runs())==3


def test_concurrent_creation_approvals_commit_one_frozen_snapshot_and_job(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    store=WebStore(tmp_path/'db')
    def create(_):
        return WebStore(tmp_path/'db').create_matched_evaluation('Matched',{'fingerprint':'p'},{},2,
            request_id='one-approval',authorization={'fingerprint':'p','max_requests':2})
    with ThreadPoolExecutor(max_workers=2) as pool:
        first,second=list(pool.map(create,range(2)))
    assert first==second
    assert len(store.runs())==1
    assert len(store.jobs(first['id']))==1


def test_comparison_job_freezes_sources_and_records_results_without_opening_learning_sessions(tmp_path):
    store=WebStore(tmp_path/'db');before,after=runs(store)
    plan=store.matched_run_preflight(before['id'],after['id'])
    models=[]
    def factory(_):
        model=Model();models.append(model);return model,None
    def sink(run_id):
        from fastapi.testclient import TestClient
        from .web_api import create_app
        from .api_event_sink import GraphQLTraceSink
        client=TestClient(create_app(store))
        return GraphQLTraceSink('offline',run_id,transport=lambda body:client.post('/graphql',json=body).json())
    worker=WebWorker(store,tmp_path/'cache',allow_live=True,model_factory=factory,sink_factory=sink)
    worker.session=lambda _:pytest.fail('comparison must not open a learning session')
    evaluation=worker.create_comparison('Matched',before['id'],after['id'],plan['fingerprint'],max_requests=8)
    assert not models
    original=store.all_events(before['id'])
    store.append_event(after['id'],'later',{'kind':'unrelated-later-event'})
    worker.process(store.claim_command())
    job=store.jobs(evaluation['id'])[0]
    assert job['status']=='completed',job
    assert job['result']['classifiers']['topic']['before']['accuracy']==.5
    assert sum(len(model.requests) for model in models)==8
    assert store.all_events(before['id'])==original
    assert store.all_events(evaluation['id'])[-1]['payload']['kind']=='matched-evaluation-completed'
    assert store.run(evaluation['id'])['mode']=='recorded'
    from fastapi.testclient import TestClient
    from .web_api import create_app
    client=TestClient(create_app(store))
    record=job['result']['records'][0]
    original_events=store.all_events(evaluation['id'])
    response=client.post('/graphql',json={'query':'query($run:ID!,$event:Int!){matchedEvaluationTarget(runId:$run,eventId:$event)}',
        'variables':{'run':evaluation['id'],'event':record['trace_event_id']}}).json()
    assert 'errors' not in response
    target=response['data']['matchedEvaluationTarget']
    assert target['request']['state']['target']=={'text':'0'}
    assert target['response']['answers']['topic']['decision']['label']=='yes'
    assert store.all_events(evaluation['id'])==original_events
    with pytest.raises(ValueError):store.command(evaluation['id'],'vote','label',{})


def test_comparison_creation_rejects_stale_approval_or_low_ceiling_without_partial_run(tmp_path):
    store=WebStore(tmp_path/'db');before,after=runs(store)
    worker=WebWorker(store,tmp_path/'cache',allow_live=True,model_factory=lambda _:pytest.fail('no calls'),sink_factory=lambda _:None)
    plan=store.matched_run_preflight(before['id'],after['id'])
    for fingerprint,ceiling in [('stale',8),(plan['fingerprint'],1)]:
        with pytest.raises(ValueError):worker.create_comparison('Matched',before['id'],after['id'],fingerprint,max_requests=ceiling)
    assert len(store.runs())==2


def test_failed_comparison_resume_is_explicit_idempotent_and_keeps_frozen_sources(tmp_path):
    store=WebStore(tmp_path/'db');before,after=runs(store)
    plan=store.matched_run_preflight(before['id'],after['id'])
    class FailsOnce(Model):
        failed=False
        async def classify_many(self,*args,**kwargs):
            if not self.failed:
                self.failed=True;self.requests.append(('failed',));raise RuntimeError('unavailable')
            return await super().classify_many(*args,**kwargs)
    model=FailsOnce()
    def sink(run_id):
        return lambda event:store.append_event(run_id,f"engine:{event['event_id']}",event)
    worker=WebWorker(store,tmp_path/'cache',allow_live=True,model_factory=lambda _:(model,None),sink_factory=sink)
    comparison=worker.create_comparison('Comparison',before['id'],after['id'],plan['fingerprint'],max_requests=8)
    worker.process(store.claim_command())
    assert store.jobs(comparison['id'])[0]['status']=='failed'
    assert store.claim_command() is None
    original=store.matched_evaluation_inputs(comparison['id'])
    job=worker.resume_comparison(comparison['id'],'resume-one',max_requests=9,retry_failed=True)
    assert store.run(comparison['id'])['config']['max_requests']==9
    with pytest.raises(ValueError,match='cannot decrease'):
        worker.resume_comparison(comparison['id'],'lower-budget',max_requests=8,retry_failed=True)
    assert worker.resume_comparison(comparison['id'],'resume-one',max_requests=9,retry_failed=True)['id']==job['id']
    with pytest.raises(ValueError,match='different content'):
        worker.resume_comparison(comparison['id'],'resume-one',max_requests=10,retry_failed=True)
    assert len(model.requests)==1
    worker.process(store.claim_command())
    assert store.jobs(comparison['id'])[0]['result']['requests']==9
    assert store.jobs(comparison['id'])[0]['status']=='completed'
    assert store.matched_evaluation_inputs(comparison['id'])==original
    assert len(store.jobs(comparison['id']))==2
    with pytest.raises(ValueError,match='failed or interrupted'):
        worker.resume_comparison(comparison['id'],'again',max_requests=10,retry_failed=True)


def test_graphql_resume_after_worker_restart_reuses_complete_calls_and_requires_failed_retry_authority(tmp_path):
    from fastapi.testclient import TestClient
    from .web_api import create_app
    from .api_event_sink import GraphQLTraceSink
    store=WebStore(tmp_path/'db');before,after=runs(store)
    original_sources={row['id']:store.all_events(row['id']) for row in (before,after)}
    class FailsSecond(Model):
        failed=False
        async def classify_many(self,*args,**kwargs):
            if len(self.requests)==1 and not self.failed:
                self.failed=True;self.requests.append(('failed',));raise RuntimeError('unavailable')
            return await super().classify_many(*args,**kwargs)
    model=FailsSecond()
    def sink(run_id):
        client=TestClient(create_app(store))
        return GraphQLTraceSink('offline',run_id,transport=lambda body:client.post('/graphql',json=body).json())
    def worker_for(store):
        return WebWorker(store,tmp_path/'cache',allow_live=True,model_factory=lambda _:(model,None),sink_factory=sink)
    first=worker_for(store)
    plan=store.matched_run_preflight(before['id'],after['id'])
    comparison=first.create_comparison('Restart comparison',before['id'],after['id'],plan['fingerprint'],max_requests=8)
    first.process(store.claim_command())
    assert len(model.requests)==2
    assert store.jobs(comparison['id'])[0]['status']=='failed'
    frozen=store.matched_evaluation_inputs(comparison['id'])
    # A new store/worker must rely on committed state, not the old worker's memory.
    restarted_store=WebStore(tmp_path/'db');restarted=worker_for(restarted_store)
    client=TestClient(create_app(restarted_store,service=restarted))
    query='mutation($run:ID!,$request:String!,$confirmed:Boolean!,$retry:Boolean!){resumeMatchedComparison(runId:$run,requestId:$request,maxRequests:9,confirmed:$confirmed,retryFailed:$retry){id status}}'
    def resume(request,confirmed,retry):
        return client.post('/graphql',json={'query':query,'variables':{'run':comparison['id'],'request':request,'confirmed':confirmed,'retry':retry}}).json()
    assert 'errors' in resume('unapproved',False,True)
    assert len(restarted_store.jobs(comparison['id']))==1
    accepted=resume('cache-only',True,False)
    assert 'errors' not in accepted
    restarted.process(restarted_store.claim_command())
    assert len(model.requests)==2  # completed response reused, failed call not retried
    assert restarted_store.jobs(comparison['id'])[0]['status']=='failed'
    approved=resume('retry-failed',True,True)
    assert 'errors' not in approved
    assert resume('retry-failed',True,True)['data']==approved['data']
    restarted.process(restarted_store.claim_command())
    job=restarted_store.jobs(comparison['id'])[0]
    assert job['status']=='completed',job
    assert job['result']['requests']==9 and job['result']['new_requests']==7
    assert len(model.requests)==9
    assert restarted_store.matched_evaluation_inputs(comparison['id'])==frozen
    assert {row['id']:restarted_store.all_events(row['id']) for row in (before,after)}==original_sources
    assert len(restarted_store.jobs(comparison['id']))==3
    targets=[event['payload'] for event in restarted_store.all_events(comparison['id']) if event['payload']['kind']=='matched-evaluation-target']
    assert len(targets)==8
    assert len({(event['endpoint'],event['target_id']) for event in targets})==8

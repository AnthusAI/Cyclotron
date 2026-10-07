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

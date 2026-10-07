import asyncio
from dataclasses import asdict
from .matched_run_plan_test import source
from .matched_run_plan import plan_matched_runs
from .matched_run_evaluation import evaluate_matched_runs
from .models import DecisionTask,DecisionResult
from .classifier_config import ClassifierConfig
from .flywheel import FittedClassifier
from .batched_classification import BatchedAnswers


class Model:
    model_identity='fake-joint'
    def __init__(self):self.requests=[]
    async def classify_many(self,configs,target,training,**kwargs):
        self.requests.append((tuple(configs),target.id,training))
        return BatchedAnswers({cid:{'decision':DecisionResult('yes',{'yes':.8,'no':.2})} for cid in configs},'fake',{},1.)


def test_matched_evaluation_preserves_joint_requests_uses_frozen_states_and_resumes_from_cache(tmp_path):
    endpoints={side:source(side) for side in ('before','after')}
    for endpoint in endpoints.values():
        endpoint['config']['classifiers'].append({'id':'extra','revision':1,'config':{'classes':[{'label':'yes'},{'label':'no'}]}})
        # Extra output remains in joint requests but is not a compared task.
        endpoint['checkpoint']['payload']['classifiers']={cid:{'state':asdict(FittedClassifier(ClassifierConfig(DecisionTask(cid,('yes','no'),'Choose'))))} for cid in ('topic','extra')}
    endpoints['after']['config']['classifiers'][-1]['revision']=2
    plan=plan_matched_runs(**endpoints)
    models={side:Model() for side in endpoints};events=[]
    result=asyncio.run(evaluate_matched_runs(plan,endpoints,models,tmp_path,max_requests=8,observer=events.append))
    assert result['classifiers']['topic']['before']['accuracy']==.5
    assert all(request[0]==('topic','extra') for model in models.values() for request in model.requests)
    assert result['requests']==8
    targets=[event for event in events if event['kind']=='matched-evaluation-target']
    assert len(targets)==8
    assert targets[0]['request']['state']['target']=={'text':'0'}
    assert set(targets[0]['request']['state']['classifiers'])=={'topic','extra'}
    assert targets[0]['labels']=={'topic':'no'}
    assert targets[0]['response']['answers']['topic']['decision']['label']=='yes'
    assert targets[0]['outputs']['topic']['actual_label']=='no'
    assert result['records'][0]['trace_event_id']==targets[0]['event_id']
    assert all(not request[2]['topic'] for model in models.values() for request in model.requests)
    repeated=asyncio.run(evaluate_matched_runs(plan,endpoints,models,tmp_path,max_requests=8,observer=events.append))
    assert sum(len(model.requests) for model in models.values())==8
    assert repeated['new_requests']==0
    assert result['classifiers']==repeated['classifiers']


def test_a_ceiling_below_preflight_is_rejected_before_any_model_call(tmp_path):
    import pytest
    endpoints={side:source(side) for side in ('before','after')};plan=plan_matched_runs(**endpoints)
    models={side:Model() for side in endpoints}
    with pytest.raises(ValueError,match='ceiling'):
        asyncio.run(evaluate_matched_runs(plan,endpoints,models,tmp_path,max_requests=1,observer=lambda _:None))
    assert not any(model.requests for model in models.values())


def test_changed_source_evidence_requires_a_new_preflight_before_calls(tmp_path):
    import pytest
    endpoints={side:source(side) for side in ('before','after')}
    plan=plan_matched_runs(**endpoints)
    endpoints['after']['events'][0]['payload']['feedback']['final_answer_value']='yes'
    models={side:Model() for side in endpoints}
    with pytest.raises(ValueError,match='preflight changed'):
        asyncio.run(evaluate_matched_runs(plan,endpoints,models,tmp_path,max_requests=8,observer=lambda _:None))
    assert not any(model.requests for model in models.values())


def test_incomplete_endpoint_checkpoint_is_rejected_before_calls(tmp_path):
    import pytest
    endpoints={side:source(side) for side in ('before','after')}
    plan=plan_matched_runs(**endpoints);models={side:Model() for side in endpoints}
    with pytest.raises(ValueError,match='every endpoint classifier'):
        asyncio.run(evaluate_matched_runs(plan,endpoints,models,tmp_path,max_requests=8,observer=lambda _:None))
    assert not any(model.requests for model in models.values())


def test_failed_trace_acknowledgement_keeps_paid_response_cached_for_explicit_resume(tmp_path):
    import pytest
    endpoints={side:source(side) for side in ('before','after')}
    for endpoint in endpoints.values():
        endpoint['checkpoint']['payload']['classifiers']={'topic':{'state':asdict(FittedClassifier(ClassifierConfig(DecisionTask('topic',('yes','no'),'Choose'))))}}
    plan=plan_matched_runs(**endpoints);models={side:Model() for side in endpoints}
    def unavailable(_):raise RuntimeError('trace acknowledgement unavailable')
    with pytest.raises(RuntimeError,match='acknowledgement'):
        asyncio.run(evaluate_matched_runs(plan,endpoints,models,tmp_path,max_requests=8,observer=unavailable))
    assert sum(len(model.requests) for model in models.values())==1
    events=[]
    result=asyncio.run(evaluate_matched_runs(plan,endpoints,models,tmp_path,max_requests=8,observer=events.append))
    assert sum(len(model.requests) for model in models.values())==8
    assert result['new_requests']==7
    assert all(type(event['event_id']) is int for event in events)
    assert events[0]['cached'] is True


def test_explicit_failed_call_retry_uses_one_total_ceiling_across_both_endpoints(tmp_path):
    import pytest
    endpoints={side:source(side) for side in ('before','after')}
    for endpoint in endpoints.values():
        endpoint['checkpoint']['payload']['classifiers']={'topic':{'state':asdict(FittedClassifier(ClassifierConfig(DecisionTask('topic',('yes','no'),'Choose'))))}}
    plan=plan_matched_runs(**endpoints)
    class FailsOnce(Model):
        failed=False
        async def classify_many(self,*args,**kwargs):
            if not self.failed:
                self.failed=True;self.requests.append(('failed',));raise RuntimeError('provider unavailable')
            return await super().classify_many(*args,**kwargs)
    models={'before':FailsOnce(),'after':Model()}
    with pytest.raises(RuntimeError,match='provider'):
        asyncio.run(evaluate_matched_runs(plan,endpoints,models,tmp_path,max_requests=8,observer=lambda _:None))
    with pytest.raises(RuntimeError,match='explicit retry'):
        asyncio.run(evaluate_matched_runs(plan,endpoints,models,tmp_path,max_requests=8,observer=lambda _:None))
    assert sum(len(model.requests) for model in models.values())==1
    with pytest.raises(RuntimeError,match='ceiling'):
        asyncio.run(evaluate_matched_runs(plan,endpoints,models,tmp_path,max_requests=8,observer=lambda _:None,retry_failed=True))
    assert sum(len(model.requests) for model in models.values())==8
    result=asyncio.run(evaluate_matched_runs(plan,endpoints,models,tmp_path,max_requests=9,observer=lambda _:None,retry_failed=True))
    assert result['requests']==9 and result['new_requests']==1

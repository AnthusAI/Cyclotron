import asyncio
import pytest
from .shared_decisions import SharedDecisions
from .classifier_config import ClassifierConfig
from .models import DecisionTask,Item,DecisionResult
from .batched_classification import BatchedAnswers
from .decision_cache import CacheOptions


def test_a_previous_joint_batch_is_not_reused_after_the_request_scope_changes(tmp_path):
    class Model:
        model_identity='fake'
        calls=0
        async def classify_many(self,configs,target,training,**kwargs):
            self.calls+=1
            probability=.8 if len(configs)==2 else .3
            return BatchedAnswers({name:{'decision':DecisionResult('yes' if probability>.5 else 'no',
                {'yes':probability,'no':1-probability})} for name in configs},'fake',None,1)
    model=Model();config=ClassifierConfig(DecisionTask('main',('yes','no'),'Choose'))
    target=Item('one',{'text':'Text'})
    shared=SharedDecisions(tmp_path/'cache.sqlite',model,max_requests=3,observer=lambda _:None)
    try:
        asyncio.run(shared.prepare({'a':config,'b':config},target,{'a':[],'b':[]}))
        asyncio.run(shared.prepare({'a':config},target,{'a':[]}))
        answer=asyncio.run(shared.adapter('b').classify(config,target,[]))
        assert answer.answers['decision'].label=='no'
        assert model.calls==3
    finally:shared.close()


def test_an_answer_cache_identity_names_the_complete_request_before_and_after_execution(tmp_path):
    class Model:
        model_identity='fake'
        async def classify_many(self,configs,target,training,**kwargs):
            return BatchedAnswers({name:{'decision':DecisionResult('yes',{'yes':.8,'no':.2})}
                for name in configs},'fake',None,1)
    config=ClassifierConfig(DecisionTask('main',('yes','no'),'Choose'));target=Item('one',{'text':'Text'})
    shared=SharedDecisions(tmp_path/'cache.sqlite',Model(),max_requests=3,observer=lambda _:None)
    try:
        adapter=shared.adapter('a')
        solo=adapter.cache_identity(config,target,[],now=None)
        asyncio.run(adapter.classify(config,target,[]))
        assert adapter.cache_identity(config,target,[],now=None)==solo
        asyncio.run(shared.prepare({'a':config,'b':config},target,{'a':[],'b':[]}))
        joint=adapter.cache_identity(config,target,[],now=None)
        assert joint!=solo
        asyncio.run(shared.prepare({'a':config,'b':ClassifierConfig(config.task,rubric='Changed sibling')},target,{'a':[],'b':[]}))
        assert adapter.cache_identity(config,target,[],now=None)!=joint
    finally:shared.close()


def test_the_flywheel_caches_joint_and_solo_answers_under_their_actual_context(tmp_path):
    from datetime import datetime,timezone
    from .flywheel import DecisionFlywheel
    from .optimizer_agent import OptimizerAgent
    class Model:
        model_identity='fake'
        calls=0
        async def classify_many(self,configs,target,training,**kwargs):
            self.calls+=1
            positive=len(configs)==2
            return BatchedAnswers({name:{'decision':DecisionResult('yes' if positive else 'no',
                {'yes':.8 if positive else .2,'no':.2 if positive else .8})} for name in configs},'fake',None,1)
    def no_optimizer(_):raise AssertionError('answer collection does not optimize')
    model=Model();config=ClassifierConfig(DecisionTask('main',('yes','no'),'Choose'));target=Item('one',{'text':'Text'})
    shared=SharedDecisions(tmp_path/'cache.sqlite',model,max_requests=3,observer=lambda _:None)
    wheel=DecisionFlywheel(tmp_path/'wheel.sqlite',config,shared.adapter('a'),OptimizerAgent(no_optimizer),max_requests=3)
    now=datetime(2026,1,1,tzinfo=timezone.utc)
    try:
        solo=asyncio.run(wheel._answers(config,target,[],now))
        asyncio.run(shared.prepare({'a':config,'b':config},target,{'a':[],'b':[]},now=now))
        joint=asyncio.run(wheel._answers(config,target,[],now))
        asyncio.run(shared.prepare({'a':config},target,{'a':[]},now=now))
        repeated=asyncio.run(wheel._answers(config,target,[],now))
        assert solo.answers['decision'].label==repeated.answers['decision'].label=='no'
        assert joint.answers['decision'].label=='yes'
        assert model.calls==2 and wheel.requests==2
    finally:wheel.close();shared.close()


def test_complete_batches_resume_from_cache_and_changes_require_another_call(tmp_path):
    class Model:
        model_identity='fake'
        calls=0
        async def classify_many(self,configs,target,training,**kwargs):
            self.calls+=1
            return BatchedAnswers({key:{'decision':DecisionResult('yes',{'yes':.8,'no':.2})} for key in configs},'fake',{'tokens':3},1)
    model=Model(); config=ClassifierConfig(DecisionTask('main',('yes','no'),'Choose'))
    target=Item('one',{'text':'Text'}); events=[]
    shared=SharedDecisions(tmp_path/'cache.sqlite',model,max_requests=2,observer=events.append)
    asyncio.run(shared.prepare({'a':config,'b':config},target,{'a':[],'b':[]}))
    shared.close()
    shared=SharedDecisions(tmp_path/'cache.sqlite',model,max_requests=2,observer=events.append)
    asyncio.run(shared.prepare({'a':config,'b':config},target,{'a':[],'b':[]}))
    assert model.calls==1 and shared.requests==1
    asyncio.run(shared.prepare({'a':ClassifierConfig(config.task,rubric='New'),'b':config},target,{'a':[],'b':[]}))
    assert model.calls==2
    with pytest.raises(Exception):
        asyncio.run(shared.prepare({'a':config},target,{'a':[]},options=CacheOptions('refresh')))
    assert model.calls==2
    shared.close()


def test_same_named_models_on_different_servers_do_not_reuse_joint_answers(tmp_path):
    from .adapters.kev import KevAdapter, KevConfiguration
    from .adapters.kev_test import FakeTransport, Response
    config = ClassifierConfig(DecisionTask('main', ('yes', 'no'), 'Choose'))
    target = Item('one', {'text': 'Text'})
    first = FakeTransport(Response(payload={'answers': {'q0': {
        'choice': 'yes', 'probabilities': {'yes': .8, 'no': .2}}}}))
    second = FakeTransport(Response(payload={'answers': {'q0': {
        'choice': 'no', 'probabilities': {'yes': .2, 'no': .8}}}}))
    cache = tmp_path / 'cache.sqlite'
    for endpoint, transport, label in [
        ('https://first.example', first, 'yes'),
        ('https://second.example', second, 'no'),
        ('https://first.example/', first, 'yes'),
    ]:
        model = KevAdapter(transport=transport, configuration=KevConfiguration(base_url=endpoint))
        shared = SharedDecisions(cache, model, max_requests=2, observer=lambda _: None)
        try:
            payload, _ = asyncio.run(shared.prepare({'a': config}, target, {'a': []}))
            assert payload['result']['answers']['a']['decision']['label'] == label
        finally:
            shared.close()
    assert first.calls == second.calls == 1

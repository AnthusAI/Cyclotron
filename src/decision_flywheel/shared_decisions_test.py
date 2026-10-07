import asyncio
import pytest
from .shared_decisions import SharedDecisions
from .classifier_config import ClassifierConfig
from .models import DecisionTask,Item,DecisionResult
from .batched_classification import BatchedAnswers
from .decision_cache import CacheOptions


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

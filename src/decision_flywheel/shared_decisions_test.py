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

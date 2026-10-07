"""Independent classifiers share transport but not rubric or feature identities."""
import asyncio
from types import SimpleNamespace
from .classifier_config import ClassifierConfig
from .models import DecisionTask, Item, LabeledItem
from .adapters.jev import JevAdapter
import pytest


def test_two_classifiers_and_their_features_use_one_request_with_scoped_context():
    yes = DecisionTask('library',('include','exclude'),'Include it?')
    topic = DecisionTask('topic',('science','sport','business'),'Choose its topic')
    configurations = {'library':ClassifierConfig(yes,rubric='Memory systems',example_ids=('example',),
        tasks=(DecisionTask('memory',('yes','no'),'About memory?'),)), 'topic':ClassifierConfig(topic,rubric='Choose the primary topic')}
    training = {'library':[LabeledItem(Item('example',{'text':'Memory paper'}),'include')], 'topic':[]}
    class Client:
        calls = 0
        def system_one(self, *, state, questions):
            self.calls+=1;self.state=state;self.questions=questions
            return SimpleNamespace(answers={key:{'choice':next(iter(q['criteria'])), 'probabilities':{label:1/len(q['criteria']) for label in q['criteria']}} for key,q in questions.items()},model='fake',usage={'input_tokens':10})
    client=Client(); events=[]
    result=asyncio.run(JevAdapter(client).classify_many(configurations,Item('target',{'text':'Target paper'}),training,event_sink=events.append))
    assert client.calls == 1 and len(client.questions)==3
    assert client.state['classifiers']['library']['rubric']=='Memory systems'
    assert client.state['classifiers']['topic']['examples']==[]
    assert set(result.answers)=={'library','topic'}
    assert set(result.answers['library'])=={'decision','memory'}
    assert result.usage=={'input_tokens':10}
    assert all(answer.usage is None for group in result.answers.values() for answer in group.values())
    assert events[0]['state']==client.state and events[0]['questions']==client.questions
    assert 'state.classifiers["library"].rubric' in next(iter(client.questions.values()))['instructions']


def test_oversized_batches_and_target_leakage_fail_before_a_model_call():
    class Client:
        def system_one(self,**kwargs): raise AssertionError('invalid batch must never call a model')
    task=DecisionTask('main',('yes','no'),'Choose')
    config=ClassifierConfig(task,rubric='x'*100)
    with pytest.raises(ValueError,match='budget'):
        asyncio.run(JevAdapter(Client()).classify_many({'a':config},Item('target',{'text':'Target'}),{'a':[]},max_request_bytes=10))
    from .batched_classification import batch_request
    request,_=batch_request({'a':ClassifierConfig(task,example_ids=('target',))},Item('target',{'text':'Target'}),
                           {'a':[LabeledItem(Item('target',{'text':'Target'}),'yes')]})
    assert request['state']['classifiers']['a']['examples']==[]

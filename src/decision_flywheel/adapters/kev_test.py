import asyncio

import pytest

from .kev import KevAdapter, KevConfiguration
from ..models import DecisionTask, Item, LabeledItem


TASK = DecisionTask("topic", ("yes", "no"), "Classify only the target.")
TARGET = Item("target", {"text": "target text"})


class Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self.payload = payload

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeTransport:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    async def post(self, url, *, json, timeout):
        self.calls += 1
        self.url, self.body, self.timeout = url, json, timeout
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_a_kev_adapter_posts_the_source_verified_system_one_schema_through_an_injected_transport():
    transport = FakeTransport(Response(payload={
        "model": "kev-4b@abc", "answers": {"topic": {"choice": "YES!", "confidence": .4, "probabilities": {"yes": .7, "no": .3}}},
        "usage": {"input_tokens": 9},
    }))
    adapter = KevAdapter(transport=transport, configuration=KevConfiguration(model="kev-4b", timeout_seconds=4))

    result = asyncio.run(adapter.decide(TASK, TARGET, [LabeledItem(Item("demo", {"text": "demo"}), "yes")]))

    assert transport.url == "http://127.0.0.1:8009/v1/systemone"
    assert transport.timeout == 4
    assert transport.body == {
        "state": {"labeled_examples": [{"text": "demo", "label": "yes"}], "target": {"text": "target text"}},
        "model": "kev-4b",
        "questions": {"topic": {"type": "choice", "instructions": "Classify only the target.", "criteria": {"yes": None, "no": None}}},
    }
    assert result.label == "yes"
    assert result.probabilities == {"yes": .7, "no": .3}
    assert result.confidence == .4
    assert result.model == "kev-4b@abc"


def test_a_kev_adapter_has_an_immutable_configured_model_identity_for_caches():
    adapter = KevAdapter(transport=FakeTransport(Response(payload={"answers": {"topic": {"choice": "yes"}}})),
                         configuration=KevConfiguration(model="kev-4b", revision="abc"))

    assert adapter.model_identity == "kev:kev-4b@abc"
    with pytest.raises(Exception):
        adapter.configuration.model = "other"


def test_kev_cache_identity_separates_servers_without_disclosing_endpoint_details():
    first = KevConfiguration(base_url="https://first.example/private-token")
    second = KevConfiguration(base_url="https://second.example/private-token")
    assert first.model_identity != second.model_identity
    assert first.model_identity == KevConfiguration(base_url=first.base_url + "/").model_identity
    assert "first.example" not in first.model_identity
    assert "private-token" not in first.model_identity
    assert KevConfiguration().model_identity == KevConfiguration(base_url="http://127.0.0.1:8009/").model_identity


@pytest.mark.parametrize("response, message", [
    (Response(status_code=503, payload={}), "status 503"),
    (Response(payload={"answers": {}}), "missing answer"),
    (Response(payload=ValueError("bad json")), "malformed JSON"),
])
def test_a_kev_adapter_rejects_endpoint_and_malformed_response_errors(response, message):
    transport = FakeTransport(response)

    with pytest.raises(ValueError, match=message):
        asyncio.run(KevAdapter(transport=transport).decide(TASK, TARGET, []))


def test_a_kev_adapter_checks_context_before_requesting_the_endpoint():
    transport = FakeTransport(Response(payload={}))

    with pytest.raises(ValueError, match="not one of"):
        asyncio.run(KevAdapter(transport=transport).decide(
            TASK, TARGET, [LabeledItem(Item("demo", {"text": "demo"}), "wrong")]
        ))

    assert not hasattr(transport, "url")


def test_a_kev_adapter_reports_a_transport_timeout_without_a_retry():
    with pytest.raises(ValueError, match="timed out"):
        asyncio.run(KevAdapter(transport=FakeTransport(TimeoutError())).decide(TASK, TARGET, []))


def test_kev_sends_all_cyclotron_questions_with_scoped_rubrics_examples_and_observable_wire_exchanges():
    from ..classifier_config import ClassifierConfig
    extra=DecisionTask('practical',('yes','no'),'Is it practical?')
    configs={'topic':ClassifierConfig(TASK,rubric='Prefer research',example_ids=('demo',),tasks=(extra,)),
             'quality':ClassifierConfig(TASK,rubric='Prefer evidence')}
    demo=LabeledItem(Item('demo',{'text':'Example paper'}),'yes',context={'human_feedback':'Useful research'})
    transport=FakeTransport(Response(payload={'model':'kev-4b','usage':{'input_tokens':12},'answers':{
        'q0':{'choice':'yes','probabilities':{'yes':.9,'no':.1}},
        'q1':{'choice':'no','probabilities':{'yes':.3,'no':.7}},
        'q2':{'choice':'no','probabilities':{'yes':.2,'no':.8}}}}))
    events=[]
    adapter=KevAdapter(transport=transport)
    result=asyncio.run(adapter.classify_many(configs,TARGET,{'topic':(demo,),'quality':()},event_sink=events.append))
    assert transport.body['state']['classifiers']['topic']['rubric']=='Prefer research'
    assert transport.body['state']['classifiers']['topic']['examples'][0]['label']=='yes'
    assert transport.body['state']['classifiers']['quality']['examples']==[]
    assert result.answers['topic']['practical'].probabilities=={'yes':.3,'no':.7}
    assert result.answers['quality']['decision'].label=='no'
    assert result.usage=={'input_tokens':12}
    assert events[0]['state']==transport.body['state']
    assert events[0]['questions']==transport.body['questions']
    assert events[0]['question_bindings']['q1']['question']=='practical'
    assert events[1]['answers']==transport.response.payload['answers']
    assert transport.calls==1


def test_kev_single_classifier_optimization_preserves_all_extra_question_features():
    from ..classifier_config import ClassifierConfig
    config=ClassifierConfig(TASK,tasks=(DecisionTask('factor',('yes','no'),'Factor?'),))
    transport=FakeTransport(Response(payload={'answers':{'q0':{'choice':'yes'},'q1':{'choice':'no'}}}))
    result=asyncio.run(KevAdapter(transport=transport).classify(config,TARGET,()))
    assert set(result.answers)=={'decision','factor'}
    assert result.answers['factor'].label=='no'


def test_kev_joint_context_validation_and_missing_answers_fail_without_hidden_retries():
    from ..classifier_config import ClassifierConfig
    config=ClassifierConfig(TASK,example_ids=('missing',))
    transport=FakeTransport(Response(payload={'answers':{}}))
    with pytest.raises(ValueError):
        asyncio.run(KevAdapter(transport=transport).classify_many({'topic':config},TARGET,
            {'topic':(LabeledItem(TARGET,'yes'),)}))
    assert not hasattr(transport,'body')
    config=ClassifierConfig(TASK)
    with pytest.raises(ValueError,match='missing answer'):
        asyncio.run(KevAdapter(transport=transport).classify_many({'topic':config},TARGET,{'topic':()}))


def test_kev_never_sends_a_target_as_its_own_demonstration():
    from ..classifier_config import ClassifierConfig
    transport=FakeTransport(Response(payload={'answers':{'q0':{'choice':'yes'}}}))
    asyncio.run(KevAdapter(transport=transport).classify_many(
        {'topic':ClassifierConfig(TASK,example_ids=('target',))},TARGET,{'topic':(LabeledItem(TARGET,'yes'),)}))
    assert transport.body['state']['classifiers']['topic']['examples']==[]

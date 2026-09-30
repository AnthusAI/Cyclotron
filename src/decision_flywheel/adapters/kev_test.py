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

    async def post(self, url, *, json, timeout):
        self.url, self.body, self.timeout = url, json, timeout
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_a_kev_adapter_posts_the_source_verified_system_one_schema_through_an_injected_transport():
    transport = FakeTransport(Response(payload={
        "model": "kev-4b@abc", "answers": {"topic": {"choice": "YES!", "probabilities": {"yes": .7, "no": .3}}},
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
    assert result.model == "kev-4b@abc"


def test_a_kev_adapter_has_an_immutable_configured_model_identity_for_caches():
    adapter = KevAdapter(transport=FakeTransport(Response(payload={"answers": {"topic": {"choice": "yes"}}})),
                         configuration=KevConfiguration(model="kev-4b", revision="abc"))

    assert adapter.model_identity == "kev:kev-4b@abc"
    with pytest.raises(Exception):
        adapter.configuration.model = "other"


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

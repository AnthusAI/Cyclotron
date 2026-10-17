import asyncio
import json

import pytest

from ..classifier_config import ClassifierConfig
from ..models import DecisionTask, Item, LabeledItem
from .microsoft_decision import MicrosoftDecisionAdapter, MicrosoftDecisionConfiguration

TASK = DecisionTask("topic", ("yes", "no"), "Classify only the target.")
TARGET = Item("target", {"text": "target text"})
ENDPOINT = "https://resource.services.ai.azure.com"
KEY = "sk-secret-key-value"


class Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code, self.payload = status_code, payload

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeTransport:
    def __init__(self, response):
        self.response, self.calls = response, 0

    async def post(self, url, *, json, headers, timeout):
        self.calls += 1
        self.url, self.body, self.headers, self.timeout = url, json, headers, timeout
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def adapter(response, **options):
    transport = FakeTransport(response)
    configuration = MicrosoftDecisionConfiguration("my-deployment", ENDPOINT, **options)
    return MicrosoftDecisionAdapter(configuration=configuration, api_key=KEY, transport=transport), transport


def ok(**answers):
    return Response(
        payload={
            "model": "microsoft-decision-1",
            "usage": {"input_tokens": 12, "note": "x"},
            "answers": answers or {"q0": {"choice": "yes", "probabilities": {"yes": 0.8, "no": 0.2}}},
        }
    )


def test_decide_posts_the_system_one_shape_with_an_api_key_header():
    a, t = adapter(
        Response(
            payload={
                "model": "microsoft-decision-1",
                "usage": {"input_tokens": 9},
                "answers": {"topic": {"choice": "YES!", "confidence": 0.4, "probabilities": {"yes": 0.7, "no": 0.3}}},
            }
        ),
        timeout_seconds=4,
    )
    result = asyncio.run(a.decide(TASK, TARGET, [LabeledItem(Item("demo", {"text": "demo"}), "yes")]))
    assert t.url == ENDPOINT + "/providers/microsoft/v1/systemone"
    assert t.headers == {"api-key": KEY} and t.timeout == 4
    assert t.body == {
        "model": "my-deployment",
        "state": {"labeled_examples": [{"text": "demo", "label": "yes"}], "target": {"text": "target text"}},
        "questions": {
            "topic": {"type": "choice", "instructions": "Classify only the target.", "criteria": {"yes": None, "no": None}}
        },
    }
    assert (result.label, result.probabilities, result.confidence, result.model) == (
        "yes",
        {"yes": 0.7, "no": 0.3},
        0.4,
        "microsoft-decision-1",
    )
    assert result.usage == {"input_tokens": 9} and result.latency_ms is not None


def test_a_trailing_slash_on_the_endpoint_does_not_change_the_url():
    a, t = adapter(ok(topic={"choice": "yes"}))
    a.configuration = MicrosoftDecisionConfiguration("d", ENDPOINT + "/")
    asyncio.run(a.decide(TASK, TARGET, []))
    assert t.url == ENDPOINT + "/providers/microsoft/v1/systemone"


CONFIG = ClassifierConfig(TASK, rubric="Be practical")


def test_criteria_placement_puts_the_rubric_in_the_first_label_criterion_and_not_in_state():
    a, t = adapter(ok())
    events = []
    asyncio.run(a.classify(CONFIG, TARGET, [], event_sink=events.append))
    question = t.body["questions"]["q0"]
    assert question["criteria"] == {"yes": "Be practical", "no": None}
    assert ".rubric" not in question["instructions"]
    assert "rubric" not in t.body["state"]["classifiers"]["classifier"]
    assert t.body["model"] == "my-deployment" and t.calls == 1
    assert events[0]["questions"] == t.body["questions"] and events[0]["state"] == t.body["state"]
    assert events[1]["kind"] == "decision-response" and events[1]["usage"] == {"input_tokens": 12}


def test_criteria_placement_honours_rubric_label_and_rejects_an_unknown_one():
    a, t = adapter(ok(), rubric_label="no")
    asyncio.run(a.classify(CONFIG, TARGET, []))
    assert t.body["questions"]["q0"]["criteria"] == {"yes": None, "no": "Be practical"}
    a, _ = adapter(ok(), rubric_label="maybe")
    with pytest.raises(ValueError, match="rubric_label"):
        asyncio.run(a.classify(CONFIG, TARGET, []))
    with pytest.raises(ValueError):
        MicrosoftDecisionConfiguration("d", ENDPOINT, rubric_label="")


def test_state_placement_keeps_the_rubric_in_state_and_criteria_empty():
    a, t = adapter(ok(), rubric_placement="state")
    asyncio.run(a.classify(CONFIG, TARGET, []))
    assert t.body["state"]["classifiers"]["classifier"]["rubric"] == "Be practical"
    assert t.body["questions"]["q0"]["criteria"] == {"yes": None, "no": None}
    assert "rubric" in t.body["questions"]["q0"]["instructions"]


def test_an_empty_rubric_and_extra_questions_are_the_same_under_both_placements():
    config = ClassifierConfig(TASK, tasks=(DecisionTask("el", ("a", "b"), "Element?"),))
    bodies = []
    for placement in ("criteria", "state"):
        a, t = adapter(ok(q0={"choice": "yes"}, q1={"choice": "a"}), rubric_placement=placement)
        result = asyncio.run(a.classify(config, TARGET, []))
        bodies.append(t.body["questions"])
        assert set(result.answers) == {"decision", "el"}
    assert [q["criteria"] for q in bodies[0].values()] == [{"yes": None, "no": None}, {"a": None, "b": None}]


def test_classify_many_asks_all_classifiers_in_one_request_and_validates_context_first():
    a, t = adapter(ok(q0={"choice": "yes"}, q1={"choice": "exclude"}))
    other = ClassifierConfig(DecisionTask("b", ("include", "exclude"), "B?"), rubric="Rubric B")
    result = asyncio.run(a.classify_many({"a": CONFIG, "b": other}, TARGET, {"a": (), "b": ()}))
    assert t.calls == 1
    assert [q["criteria"] for q in t.body["questions"].values()] == [
        {"yes": "Be practical", "no": None},
        {"include": "Rubric B", "exclude": None},
    ]
    assert result.answers["a"]["decision"].label == "yes" and result.usage == {"input_tokens": 12}
    a, t = adapter(ok())
    with pytest.raises(ValueError):
        asyncio.run(
            a.classify_many({"a": ClassifierConfig(TASK, example_ids=("missing",))}, TARGET, {"a": (LabeledItem(TARGET, "yes"),)})
        )
    assert t.calls == 0


def test_identity_with_and_without_revision_and_hashed_endpoint():
    plain = MicrosoftDecisionConfiguration("dep", ENDPOINT)
    pinned = MicrosoftDecisionConfiguration("dep", ENDPOINT, revision="r1")
    other = MicrosoftDecisionConfiguration("dep", "https://other.example/private-token")
    assert plain.model_identity.startswith("microsoft-decision:dep:endpoint-")
    assert pinned.model_identity.startswith("microsoft-decision:dep@r1:endpoint-")
    assert plain.model_identity != other.model_identity
    assert plain.model_identity == MicrosoftDecisionConfiguration("dep", ENDPOINT + "/").model_identity
    assert "resource" not in plain.model_identity and "private-token" not in other.model_identity
    assert MicrosoftDecisionAdapter(configuration=plain, api_key=KEY).model_identity == plain.model_identity
    with pytest.raises(Exception):
        plain.deployment = "x"


@pytest.mark.parametrize(
    "bad",
    [
        dict(deployment=""),
        dict(endpoint=" "),
        dict(endpoint="not a url"),
        dict(endpoint="ftp://x.example"),
        dict(timeout_seconds=0),
        dict(rubric_placement="elsewhere"),
    ],
)
def test_configuration_validation(bad):
    with pytest.raises(ValueError):
        MicrosoftDecisionConfiguration(**{"deployment": "d", "endpoint": ENDPOINT, **bad})


FULL = {"AZURE_FOUNDRY_API_KEY": KEY, "AZURE_FOUNDRY_ENDPOINT": ENDPOINT, "AZURE_FOUNDRY_DEPLOYMENT": "dep"}


def test_from_environment_reads_the_three_variables():
    a = MicrosoftDecisionAdapter.from_environment(environ=FULL, rubric_placement="state", revision="r2")
    assert (a.configuration.deployment, a.configuration.endpoint, a.configuration.rubric_placement, a.configuration.revision) == (
        "dep",
        ENDPOINT,
        "state",
        "r2",
    )
    assert (
        MicrosoftDecisionAdapter.from_environment(
            environ={k: v for k, v in FULL.items() if k != "AZURE_FOUNDRY_DEPLOYMENT"}, deployment="given"
        ).configuration.deployment
        == "given"
    )


@pytest.mark.parametrize("missing", sorted(FULL))
def test_from_environment_names_the_missing_variable_without_leaking_values(missing):
    with pytest.raises(ValueError, match=missing) as error:
        MicrosoftDecisionAdapter.from_environment(environ={k: v for k, v in FULL.items() if k != missing})
    assert KEY not in str(error.value)


@pytest.mark.parametrize(
    "response, message",
    [
        (Response(status_code=503, payload={}), "status 503"),
        (Response(status_code=401, payload={"error": KEY}), "status 401"),
        (Response(payload={"answers": {}}), "missing answer"),
        (Response(payload={}), "missing answers"),
        (Response(payload={"answers": {"q0": {"type": "choice"}}}), "missing answer"),
        (Response(payload=ValueError("bad json")), "malformed JSON"),
        (Response(payload=[1]), "malformed JSON"),
        (TimeoutError(), "timed out"),
        (ConnectionError(KEY), "request failed"),
        (Response(payload={"answers": {"q0": {"choice": "maybe"}}}), "not one of"),
    ],
)
def test_failures_are_clear_not_retried_and_never_contain_the_key(response, message):
    a, t = adapter(response)
    with pytest.raises(ValueError, match=message) as error:
        asyncio.run(a.classify(ClassifierConfig(TASK), TARGET, []))
    assert t.calls == 1 and KEY not in str(error.value) and KEY not in repr(error.value.__cause__)


def test_decide_rejects_a_missing_answer_and_bad_context_before_posting():
    a, t = adapter(Response(payload={"answers": {}}))
    with pytest.raises(ValueError, match="missing answer"):
        asyncio.run(a.decide(TASK, TARGET, []))
    a, t = adapter(Response(payload={}))
    with pytest.raises(ValueError, match="not one of"):
        asyncio.run(a.decide(TASK, TARGET, [LabeledItem(Item("d", {"text": "d"}), "wrong")]))
    assert t.calls == 0


def test_the_key_never_appears_in_events_bodies_or_the_adapter_repr():
    a, t = adapter(ok())
    events = []
    asyncio.run(a.classify(CONFIG, TARGET, [], event_sink=events.append))
    assert KEY not in json.dumps([events, t.body]) and KEY not in repr(a) and KEY not in repr(a.configuration)
    assert "resource.services" not in json.dumps(events)


@pytest.mark.parametrize(
    "endpoint",
    [ENDPOINT, ENDPOINT + "/", ENDPOINT + "/api/projects/my-project", ENDPOINT + "/api/projects/my-project/?api-version=1"],
)
def test_any_endpoint_form_is_normalized_to_the_resource_root(endpoint):
    transport = FakeTransport(ok(topic={"choice": "yes"}))
    a = MicrosoftDecisionAdapter(configuration=MicrosoftDecisionConfiguration("d", endpoint), api_key=KEY, transport=transport)
    asyncio.run(a.decide(TASK, TARGET, []))
    assert transport.url == ENDPOINT + "/providers/microsoft/v1/systemone"
    assert a.model_identity == MicrosoftDecisionConfiguration("d", ENDPOINT).model_identity
    assert "my-project" not in a.model_identity

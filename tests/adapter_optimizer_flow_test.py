"""Offline wiring specs: selected context reaches only injected adapters."""
import asyncio
from types import SimpleNamespace

from decision_flywheel.adapters.jev import JevAdapter
from decision_flywheel.adapters.kev import KevAdapter
from decision_flywheel.adapters.laya import LayaAdapter
from decision_flywheel.context import RandomBalanced
from decision_flywheel.models import DecisionTask, Item, LabeledItem
from decision_flywheel.optimizer import TrialSpec, search_context_policies


def test_a_litellm_optimizer_exposes_the_complete_human_context_and_returned_reply_in_the_normal_trace():
    from decision_flywheel.adapters.litellm_optimizer import LiteLLMOptimizer
    from decision_flywheel.optimizer_agent import FeedbackBriefing, OptimizerAgent
    requests=[];events=[]
    def complete(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(model='fake-returned',usage=None,choices=[SimpleNamespace(message=
            SimpleNamespace(content='{"rubric":"Keep knowledge-base management research"}',tool_calls=None))])
    briefing=FeedbackBriefing.build(TASK,[LabeledItem(Item('trusted',{'text':'An actual training abstract'}),'yes',
        context={'human_feedback':'I want papers about managing knowledge bases'})],
        current={'rubric':'','control_under_test':'rubric'},protected=[])
    agent=OptimizerAgent(LiteLLMOptimizer(complete,model='ollama/fake'),observer=events.append)
    proposal=agent.propose(briefing)
    assert proposal['rubric']=='Keep knowledge-base management research'
    request,response=events
    assert request['kind']=='optimizer-request' and response['kind']=='optimizer-response'
    assert request['messages']==requests[0]['messages']
    assert 'I want papers about managing knowledge bases' in request['messages'][1]['content']
    assert response['content']=='{"rubric":"Keep knowledge-base management research"}'
    assert response['model']=='fake-returned'


TASK = DecisionTask("topic", ("yes", "no"), "Classify only the target.")
CANDIDATES = [
    LabeledItem(Item(f"{label}-{number}", {"text": f"{label} demonstration {number}"}), label)
    for label in TASK.labels for number in range(2)
]
DEVELOPMENT = [
    LabeledItem(Item("dev-yes", {"text": "yes target"}), "yes"),
    LabeledItem(Item("dev-no", {"text": "no target"}), "no"),
]
TRIALS = (TrialSpec(RandomBalanced(seed=4), per_label=1),)


class FakeJevClient:
    def system_one(self, *, state, questions):
        self.requests = getattr(self, "requests", []) + [(state, questions)]
        label = "yes" if state["target"]["text"].startswith("yes") else "no"
        return SimpleNamespace(answers={"topic": {"choice": label}}, model="fake-jev", usage=None)


class FakeResponse:
    status_code = 200

    def __init__(self, label):
        self.label = label

    def json(self):
        return {"model": "fake-kev", "answers": {"topic": {"choice": self.label}}}


class FakeKevTransport:
    async def post(self, url, *, json, timeout):
        self.requests = getattr(self, "requests", []) + [(url, json, timeout)]
        label = "yes" if json["state"]["target"]["text"].startswith("yes") else "no"
        return FakeResponse(label)


class FakeLaya:
    def system_one(self, *, state, questions):
        self.requests = getattr(self, "requests", []) + [(state, questions)]
        label = "yes" if state["target"]["text"].startswith("yes") else "no"
        return {"answers": {"topic": {"choice": label}}}


def test_optimizer_reaches_an_injected_jev_client_without_keys_or_network(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    client = FakeJevClient()
    adapter = JevAdapter(client)

    result = asyncio.run(search_context_policies(
        TASK, CANDIDATES, DEVELOPMENT, adapter, TRIALS, max_model_calls=2,
        model_fingerprint=adapter.model_identity,
    ))

    assert result.winner.objective == 1.0
    assert len(client.requests) == 2
    assert all(request[0]["labeled_examples"] for request in client.requests)
    assert all(request[1] == {"topic": {"type": "choice", "instructions": "Classify only the target.",
                                               "criteria": {"yes": None, "no": None}}}
               for request in client.requests)


def test_optimizer_reaches_an_injected_kev_transport_without_network_or_keys(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    transport = FakeKevTransport()
    adapter = KevAdapter(transport=transport)

    result = asyncio.run(search_context_policies(
        TASK, CANDIDATES, DEVELOPMENT, adapter, TRIALS, max_model_calls=2,
        model_fingerprint=adapter.model_identity,
    ))

    assert result.winner.objective == 1.0
    assert len(transport.requests) == 2
    assert all(url.endswith("/v1/systemone") for url, _, _ in transport.requests)
    assert all(body["state"]["labeled_examples"] for _, body, _ in transport.requests)


def test_optimizer_experiments_with_laya_context_through_an_injected_local_model():
    model = FakeLaya()
    adapter = LayaAdapter(model)

    result = asyncio.run(search_context_policies(
        TASK, CANDIDATES, DEVELOPMENT, adapter, TRIALS, max_model_calls=2,
        model_fingerprint=adapter.model_identity,
    ))

    assert result.winner.objective == 1.0
    assert len(model.requests) == 2
    assert all(state['labeled_examples'] for state, _ in model.requests)
    assert all(state['target']['text'].endswith('target') for state, _ in model.requests)

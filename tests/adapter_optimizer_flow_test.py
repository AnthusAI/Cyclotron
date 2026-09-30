"""Offline wiring specs: selected context reaches only injected adapters."""
import asyncio
from types import SimpleNamespace

from decision_flywheel.adapters.jev import JevAdapter
from decision_flywheel.adapters.kev import KevAdapter
from decision_flywheel.adapters.laya import LayaAdapter
from decision_flywheel.context import RandomBalanced
from decision_flywheel.models import DecisionTask, Item, LabeledItem
from decision_flywheel.optimizer import TrialSpec, search_context_policies


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


class NeverCalledLaya:
    def system_one(self, **kwargs):
        raise AssertionError("few-shot Laya must be excluded before model inference")


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


def test_optimizer_records_laya_few_shot_exclusion_without_calling_a_local_model():
    model = NeverCalledLaya()
    adapter = LayaAdapter(model)

    result = asyncio.run(search_context_policies(
        TASK, CANDIDATES, DEVELOPMENT, adapter, TRIALS, max_model_calls=2,
        model_fingerprint=adapter.model_identity,
    ))

    assert result.winner is None
    assert result.trials[0].status == "incomplete"
    assert result.trials[0].failure_count == 1

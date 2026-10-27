"""The hosted worker rings the decision-ready doorbell for finished machine decision jobs only."""

import asyncio
import json

from decision_flywheel import account_sessions_test
from decision_flywheel.decision_events import DecisionEventNotifier
from decision_flywheel.decision_events_test import BUS, FakeEvents
from decision_flywheel.hosted_machine_publications_test import (
    client,
    decide,
    decisions_body,
    drain,
    fixture,
    new_item,
    revisions,
)
from decision_flywheel.provider_credentials import ProviderKeyUnavailable

identity = account_sessions_test.identity


def test_the_worker_rings_once_for_a_finished_shared_decision_and_never_for_a_single_publication(identity, monkeypatch):
    (bff, worker, model, store, config, account, *_, sent, targets, app), wrapped = fixture(identity, monkeypatch)
    events = FakeEvents()
    worker.decision_events = DecisionEventNotifier(events, BUS)

    async def go():
        async with client(wrapped, config, account) as http:
            await decide(http, worker, sent, new_item("hn-single"))
            assert events.calls == []
            posted = await http.post("/machine/decisions", json=decisions_body(revisions("alpha", "beta"), new_item()))
            await drain(worker, sent)
            (entry,) = events.calls[0]["Entries"]
            detail = json.loads(entry["Detail"])
            assert len(events.calls) == 1 and entry["EventBusName"] == BUS
            assert detail["jobId"] == posted.json()["jobId"] and detail["status"] == "complete"
            assert detail["requestId"] == "req-all-0001" and detail["publications"] == ["alpha", "beta"]
            assert detail["accountId"] == account and detail["itemId"] == "hn-1"

    asyncio.run(go())


def test_a_refused_shared_decision_rings_failed_and_a_broken_bus_does_not_change_the_receipt(identity, monkeypatch):
    (bff, worker, model, store, config, account, *_, sent, targets, app), wrapped = fixture(identity, monkeypatch)
    events = FakeEvents(error=RuntimeError("bus down"))
    worker.decision_events = DecisionEventNotifier(events, BUS, log=lambda line: None)

    def unavailable(config):
        raise ProviderKeyUnavailable()

    worker.model_factory = unavailable

    async def go():
        async with client(wrapped, config, account) as http:
            posted = await http.post("/machine/decisions", json=decisions_body(revisions("alpha"), new_item()))
            await drain(worker, sent)
            receipt = await http.get(f"/machine/decisions/{posted.json()['jobId']}")
            assert receipt.status_code == 503 and receipt.json()["code"] == "provider_key_unavailable"
            assert json.loads(events.calls[0]["Entries"][0]["Detail"])["status"] == "failed"

    asyncio.run(go())

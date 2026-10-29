"""Freeze an empty account's publication run without live-call authority."""

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest

from . import account_sessions_test
from .hosted_publications import PublicationTarget
from .hosted_publications_test import PUBS, accepted_example, priming_client, setup
from .publication_lab import CONTRACT, PublicationBinding

identity = account_sessions_test.identity


def request():
    return {
        "contractVersion": CONTRACT,
        "requestId": str(uuid4()),
        "name": "Synthetic frozen publication router",
        "config": {"cyclotron_id": "shared", "cyclotron_definition_revision": 1, "item_list_id": "sources"},
    }


def offline(identity, monkeypatch):
    result = setup(identity, monkeypatch, create_run=False)
    bff, worker, model, store, config, account, cookie, session, state, sent, targets, app = result
    bff.config = worker.config = replace(config, allow_live=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("data-only bootstrap constructed a provider")

    bff.model_factory = worker.model_factory = forbidden
    assert store.runs() == []
    return result


def test_the_bootstrap_is_also_served_under_api_with_the_same_identity_and_receipt(identity, monkeypatch):
    bff, worker, model, store, config, account, cookie, session, state, sent, targets, app = offline(identity, monkeypatch)
    body = request()

    async def run():
        async with priming_client(app, config, cookie, session, account) as http:
            created = await http.post("/api/publication-runs/bootstrap", json=body)
            assert created.status_code == 201, created.text
            receipt = created.json()
            assert receipt["createdBy"] == session.person_id
            again = await http.post("/publication-runs/bootstrap", json=body)
            assert again.status_code == 200 and again.json() == receipt
            assert len(store.runs()) == 1 and not sent and not model.calls

    asyncio.run(run())


def test_the_standing_runs_bootstrap_speaks_the_decision_contract_and_replays_the_same_run(identity, monkeypatch):
    bff, worker, model, store, config, account, cookie, session, state, sent, targets, app = offline(identity, monkeypatch)
    body = request()

    async def run():
        async with priming_client(app, config, cookie, session, account) as http:
            assert (await http.post("/api/standing-runs/bootstrap", json=body)).status_code == 400
            created = await http.post("/api/standing-runs/bootstrap", json={**body, "contractVersion": "cyclotron-decision/v1"})
            assert created.status_code == 201, created.text
            receipt = created.json()
            assert receipt["contractVersion"] == "cyclotron-decision/v1" and receipt["createdBy"] == session.person_id
            # The same request id on the publication path is the same frozen run, answered in its own contract.
            again = await http.post("/api/publication-runs/bootstrap", json=body)
            assert again.status_code == 200
            assert again.json() == {**receipt, "contractVersion": "cyclotron-publication/v1"}
            assert len(store.runs()) == 1 and not sent and not model.calls

    asyncio.run(run())


def test_a_run_frozen_under_either_bootstrap_key_is_recognised():
    from .hosted_publication_bootstrap import bootstrap_of

    record = {"requestId": "r", "fingerprint": "f", "createdBy": "p"}
    assert bootstrap_of({"publication_bootstrap": record}) == record
    assert bootstrap_of({"standing_bootstrap": record}) == record
    assert bootstrap_of({}) == {}


def test_empty_account_bootstraps_one_frozen_run_and_can_prime_with_live_execution_disabled(identity, monkeypatch):
    bff, worker, model, store, config, account, cookie, session, state, sent, targets, app = offline(identity, monkeypatch)
    body = request()

    async def run():
        async with priming_client(app, config, cookie, session, account) as http:
            created = await http.post("/publication-runs/bootstrap", json=body)
            assert created.status_code == 201, created.text
            receipt = created.json()
            assert receipt["accountId"] == account and receipt["createdBy"] == session.person_id
            frozen = store.run(receipt["runId"])
            assert frozen["config"]["cyclotron_definition_revision"] == 1
            assert {row["id"] for row in frozen["config"]["classifiers"]} == set(PUBS)
            assert frozen["config"]["item_revisions"] and len(store.items(receipt["runId"])) == 1
            assert not sent and not model.calls and not store.jobs(receipt["runId"])
            again = await http.post("/publication-runs/bootstrap", json=body)
            assert again.status_code == 200 and again.json() == receipt
            assert len(store.runs()) == 1
            assert len(store.events(receipt["runId"])) == 1
            assert store.events(receipt["runId"])[0]["payload"]["createdBy"] == session.person_id
            changed = await http.post("/publication-runs/bootstrap", json={**body, "name": "Different intent"})
            assert changed.status_code == 409 and len(store.runs()) == 1
            # Catalog advancement cannot silently repin an already created run.
            store.save_classifier(
                "delta", "New delta", {"question": "Changed?", "classes": [{"label": "include"}, {"label": "exclude"}]}
            )
            retry = await http.post("/publication-runs/bootstrap", json=body)
            assert retry.status_code == 200 and retry.json() == receipt
            assert store.run(receipt["runId"])["config"] == frozen["config"]
            targets.append(
                PublicationTarget(
                    account,
                    receipt["runId"],
                    tuple(PublicationBinding(p, (p,), "one") for p in PUBS),
                    model_identity=model.model_identity,
                )
            )
            store.save_item_list("priming-alpha", "Synthetic approved examples")
            await accepted_example(http)
            prime = await http.post(
                "/publications/alpha/operations",
                json={
                    "contractVersion": CONTRACT,
                    "operation": "prime",
                    "payload": {"bindingRevision": "one", "listId": "priming-alpha"},
                },
            )
            assert prime.status_code == 202, prime.text
            await worker.worker().process(sent.pop())
            applied = await http.get(f"/publications/alpha/commands/{prime.json()['jobId']}")
            assert applied.status_code == 200 and applied.json()["primedExamples"] == {"alpha": 1}
            assert not model.calls and not bff.config.allow_live

    asyncio.run(run())


@pytest.mark.parametrize(
    "case",
    [
        "viewer",
        "definition",
        "classifier",
        "frozen-config",
        "account-in-body",
        "csrf",
        "revoke-at-commit",
        "concurrent-catalog",
        "uncertain-commit",
    ],
)
def test_bootstrap_rejects_stale_or_foreign_evidence_and_handles_uncertain_retries(identity, monkeypatch, case):
    bff, worker, model, store, config, account, cookie, session, state, sent, targets, app = offline(identity, monkeypatch)
    body = request()
    if case == "viewer":
        state["role"] = "viewer"
    elif case == "definition":
        body["config"]["cyclotron_definition_revision"] = 99
    elif case == "classifier":
        store.save_classifier(
            "alpha", "New alpha", {"question": "Changed?", "classes": [{"label": "include"}, {"label": "exclude"}]}
        )
    elif case == "frozen-config":
        body["config"]["classifiers"] = []
    elif case == "account-in-body":
        body["accountId"] = str(uuid4())

    async def run():
        async with priming_client(app, config, cookie, session, account) as http:
            if case == "csrf":
                http.headers["X-CSRF-Token"] = "wrong"
            if case in ("revoke-at-commit", "concurrent-catalog", "uncertain-commit"):
                from .hosted_publications import _GuardedJournal
                from .storage_errors import StorageConflict, StorageUncertain

                commit = _GuardedJournal.commit
                triggered = []

                def intercept(journal, expected, value):
                    if value.get("schema") == "cyclotron-s3-console/v4" and not triggered:
                        triggered.append(True)
                        if case == "revoke-at-commit":
                            state["denied"] = True
                        elif case == "concurrent-catalog":
                            raise StorageConflict("synthetic concurrent catalog write")
                        else:
                            commit(journal, expected, value)
                            raise StorageUncertain("synthetic lost successful response")
                    return commit(journal, expected, value)

                monkeypatch.setattr(_GuardedJournal, "commit", intercept)
            response = await http.post("/publication-runs/bootstrap", json=body)
            assert response.status_code in (400, 403, 409, 503), response.text
            if case == "uncertain-commit":
                assert len(store.runs()) == 1
                retry = await http.post("/publication-runs/bootstrap", json=body)
                assert retry.status_code == 200 and retry.json()["runId"] == store.runs()[0]["id"]
                assert len(store.runs()) == 1
            else:
                assert store.runs() == []
            assert not model.calls and not sent

    asyncio.run(run())


def test_a_bootstrap_request_over_sixty_four_kilobytes_with_a_long_run_name_creates_the_run(identity, monkeypatch):
    bff, worker, model, store, config, account, cookie, session, state, sent, targets, app = offline(identity, monkeypatch)
    body = {**request(), "name": "Synthetic long-named router " + "n" * 100_000}

    async def run():
        async with priming_client(app, config, cookie, session, account) as http:
            created = await http.post("/publication-runs/bootstrap", json=body)
            assert created.status_code == 201, created.text
            assert len(store.runs()) == 1 and not sent and not model.calls

    asyncio.run(run())

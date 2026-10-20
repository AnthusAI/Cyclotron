"""Actual hosted API/queue/native SDK/S3 composition, with offline boundaries."""

import asyncio
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest

from . import account_sessions_test
from .batched_classification import BatchedAnswers
from .hosted_publications import PublicationTarget
from .hosted_runtime import HostedConfig, HostedRuntime
from .hosted_runtime_test import complete_environment
from .models import DecisionResult
from .optimizer_agent import DisabledOptimizer
from .publication_lab import HTTPPublicationClient, PublicationBinding, PublicationError
from .s3_journal_test import ConditionalS3
from .s3_sessions_test import Cipher

identity = account_sessions_test.identity
PUBS = ("alpha", "beta", "gamma", "delta")
ITEM = {"id": "source-revision-one", "values": {"text": "Explicitly synthetic shared source, not human feedback."}}


class OfflineModel:
    model_identity = "hosted-publication-offline-provider"

    def __init__(self):
        self.calls = []

    async def classify_many(self, configs, target, training, **kwargs):
        self.calls.append(tuple(configs))
        return BatchedAnswers(
            {key: {"decision": DecisionResult("include", {"include": 0.8, "exclude": 0.2})} for key in configs}, "offline", {}, 1
        )


def setup(identity, monkeypatch):
    verifier, token = identity
    person, account = str(uuid4()), str(uuid4())
    state, sent, targets = {"role": "owner", "denied": False}, [], []
    model, s3 = OfflineModel(), ConditionalS3()

    def central(method, path, human, body=None, key=None, **kwargs):
        verifier.access(human)
        if state["denied"]:
            from .prospectus_accounts import AccountAccessDenied

            raise AccountAccessDenied("revoked")
        if path == "/v1/identities/me":
            return {"person": {"id": person, "createdAt": "synthetic"}}
        if path == f"/v1/accounts/{account}/authorize":
            return {
                "allowed": True,
                "accountId": account,
                "personId": person,
                "product": "cyclotron",
                "role": state["role"],
                "operation": body["operation"],
                "revision": 1,
            }
        raise AssertionError("unexpected central operation")

    config = replace(HostedConfig.from_environment(complete_environment()), allow_live=True)
    kwargs = dict(
        s3=s3,
        cipher=Cipher(),
        central=central,
        verifier=verifier,
        send=sent.append,
        model_factory=lambda config: (model, DisabledOptimizer()),
        publication_targets=lambda: tuple(targets),
    )
    bff, worker = HostedRuntime(config, **kwargs), HostedRuntime(config, **kwargs)
    store = bff.console(account, initialize=True)
    for pub in PUBS:
        store.save_classifier(
            pub,
            pub,
            {
                "question": f"Synthetic relevance for {pub}?",
                "classes": [{"label": "include", "role": "positive"}, {"label": "exclude"}],
            },
        )
    store.save_cyclotron_definition("shared", "Synthetic shared router", [{"id": p, "revision": 1} for p in PUBS], {})
    store.save_item_list("sources", "Synthetic sources")
    store.upsert_list_items("sources", [{"id": ITEM["id"], "occurred_at": "2026-01-01", "values": ITEM["values"]}])
    run = bff.workspace(account).service.create_run(
        "Synthetic remote baseline", {"cyclotron_id": "shared", "item_list_id": "sources", "max_requests": 2}
    )
    targets.append(PublicationTarget(account, run["id"], tuple(PublicationBinding(p, (p,), "binding-one") for p in PUBS)))
    access = token()
    cookie, session = bff.sessions.create(access, verifier.access(access))
    session.person_id = person
    bff.sessions.save(session)
    app = bff.app()

    def forbidden(*args, **kwargs):
        raise AssertionError("hosted publication opened SQLite or local files")

    monkeypatch.setattr("sqlite3.connect", forbidden)
    monkeypatch.setattr("pathlib.Path.mkdir", forbidden)
    return bff, worker, model, store, config, account, cookie, session, state, sent, targets, app


def test_four_remote_projections_share_one_native_request_after_queue_completion(identity, monkeypatch):
    bff, worker, model, store, config, account, cookie, session, state, sent, targets, app = setup(identity, monkeypatch)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=config.public_origin,
            cookies={"__Host-cyclotron-session": cookie},
            headers={"Origin": config.public_origin, "X-CSRF-Token": session.csrf_token, "X-Cyclotron-Account": account},
        ) as http:
            decisions = []
            for pub in PUBS:
                response = await http.post(
                    f"/publications/{pub}/operations",
                    json={
                        "contractVersion": "cyclotron-publication/v1",
                        "operation": "decide",
                        "payload": {"bindingRevision": "binding-one", "item": ITEM},
                    },
                )
                assert response.status_code == 202, response.text
                pending = response.json()
                assert pending["status"] == "pending" and "decision" not in pending
                message = sent.pop(0)
                assert set(message) == {"accountId", "runId", "jobId"}
                await worker.worker().process(message)
                receipt = await http.get(f"/publications/{pub}/commands/{pending['jobId']}")
                assert receipt.status_code == 200, receipt.text
                value = receipt.json()
                assert value["accountId"] == account and value["publicationId"] == pub
                assert set(value["decision"]["classifiers"]) == {pub}
                decisions.append(value["decision"]["decisionId"])
            assert len(set(decisions)) == 1 and len(model.calls) == 1
            assert set(model.calls[0]) == set(PUBS)
            assert all(not store.item_labels("sources", ITEM["id"], 1) for _ in PUBS)

    asyncio.run(run())


def test_http_client_waits_for_the_same_durable_command_without_reposting(identity, monkeypatch):
    bff, worker, model, store, config, account, cookie, session, state, sent, targets, app = setup(identity, monkeypatch)
    requests = []
    transport = httpx.ASGITransport(app=app)

    async def handle(request):
        requests.append((request.method, request.url.path))
        if request.method == "GET" and sent:
            await worker.worker().process(sent.pop(0))
        return await transport.handle_async_request(request)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle),
            base_url=config.public_origin,
            cookies={"__Host-cyclotron-session": cookie},
            headers={"Origin": config.public_origin, "X-CSRF-Token": session.csrf_token},
        ) as http:
            result = await HTTPPublicationClient(http, account_id=account, poll_interval=0.001).command(
                "alpha", "decide", {"bindingRevision": "binding-one", "item": ITEM}
            )
            assert result["decision"]["classifiers"]["alpha"]

    asyncio.run(run())
    assert [method for method, path in requests] == ["POST", "GET"]
    assert len(model.calls) == 1


@pytest.mark.parametrize("case", ["viewer", "source", "binding", "legacy", "feedback"])
def test_invalid_or_unactivated_requests_cannot_enqueue_provider_work(identity, monkeypatch, case):
    from .prospectus_accounts import VerifiedAccountSession

    bff, worker, model, store, config, account, cookie, session, state, sent, targets, app = setup(identity, monkeypatch)

    async def run():
        if case == "legacy":
            with pytest.raises(ValueError, match="scoped prediction path"):
                await bff.workspace(account).service.submit_command(
                    VerifiedAccountSession(session.access_token, session.person_id, account, session_ref=session.session_ref),
                    targets[0].run_id,
                    "synthetic-legacy",
                    "prepare",
                    {},
                )
            return
        payload = {"bindingRevision": "binding-one", "item": ITEM}
        operation = "decide"
        if case == "viewer":
            state["role"] = "viewer"
        if case == "source":
            payload = {**payload, "item": {**ITEM, "values": {"text": "different revision"}}}
        if case == "binding":
            payload = {**payload, "bindingRevision": "stale"}
        if case == "feedback":
            operation = "review"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=config.public_origin,
            cookies={"__Host-cyclotron-session": cookie},
            headers={"Origin": config.public_origin, "X-CSRF-Token": session.csrf_token, "X-Cyclotron-Account": account},
        ) as http:
            response = await http.post(
                "/publications/alpha/operations",
                json={"contractVersion": "cyclotron-publication/v1", "operation": operation, "payload": payload},
            )
            assert response.status_code in (403, 409, 503), response.text

    asyncio.run(run())
    assert not sent and not model.calls


def test_pending_timeout_requires_reconciliation_and_never_reposts():
    from .publication_lab import CONTRACT

    job, requests = str(uuid4()), []

    def response(request):
        requests.append(request.method)
        return httpx.Response(
            202,
            json={
                "contractVersion": CONTRACT,
                "publicationId": "alpha",
                "bindingRevision": "one",
                "jobId": job,
                "status": "pending",
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(response), base_url="https://synthetic.test") as http:
            with pytest.raises(PublicationError) as error:
                await HTTPPublicationClient(http, poll_timeout=0.003, poll_interval=0.001).command(
                    "alpha", "decide", {"bindingRevision": "one"}
                )
            assert error.value.code == "reconciliation_required"

    asyncio.run(run())
    assert requests.count("POST") == 1 and "GET" in requests


@pytest.mark.parametrize("change", ["revoked", "binding"])
def test_changed_original_authority_or_binding_cannot_dispatch(identity, monkeypatch, change):
    bff, worker, model, store, config, account, cookie, session, state, sent, targets, app = setup(identity, monkeypatch)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=config.public_origin,
            cookies={"__Host-cyclotron-session": cookie},
            headers={"Origin": config.public_origin, "X-CSRF-Token": session.csrf_token, "X-Cyclotron-Account": account},
        ) as http:
            queued = await http.post(
                "/publications/alpha/operations",
                json={
                    "contractVersion": "cyclotron-publication/v1",
                    "operation": "decide",
                    "payload": {"bindingRevision": "binding-one", "item": ITEM},
                },
            )
            assert queued.status_code == 202
            if change == "revoked":
                state["denied"] = True
            else:
                targets[0] = replace(targets[0], bindings=tuple(PublicationBinding(p, (p,), "changed") for p in PUBS))
            with pytest.raises(Exception):
                await worker.worker().process(sent[0])
            assert not model.calls

    asyncio.run(run())

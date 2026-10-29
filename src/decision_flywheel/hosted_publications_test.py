"""Actual hosted API/queue/native SDK/S3 composition, with offline boundaries."""

import asyncio
import json
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest

from . import account_sessions_test
from .batched_classification import BatchedAnswers
from .hosted_publications import PublicationTarget, envelope, targets_from_environment
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


def setup(identity, monkeypatch, *, create_run=True, items=(ITEM,)):
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
    store.upsert_list_items(
        "sources", [{"id": item["id"], "occurred_at": "2026-01-01", "values": item["values"]} for item in items]
    )
    if create_run:
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


def test_a_hosted_run_decides_more_items_than_its_configured_request_budget_with_no_ceiling_refusal(identity, monkeypatch):
    items = [{"id": f"source-revision-{n}", "values": {"text": f"Synthetic source {n}."}} for n in range(4)]
    bff, worker, model, store, config, account, cookie, session, state, sent, targets, app = setup(
        identity, monkeypatch, items=items
    )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=config.public_origin,
            cookies={"__Host-cyclotron-session": cookie},
            headers={"Origin": config.public_origin, "X-CSRF-Token": session.csrf_token, "X-Cyclotron-Account": account},
        ) as http:
            for item in items:
                response = await http.post(
                    "/publications/alpha/operations",
                    json={
                        "contractVersion": "cyclotron-publication/v1",
                        "operation": "decide",
                        "payload": {"bindingRevision": "binding-one", "item": item},
                    },
                )
                assert response.status_code == 202, response.text
                await worker.worker().process(sent.pop(0))
                receipt = await http.get(f"/publications/alpha/commands/{response.json()['jobId']}")
                assert receipt.status_code == 200, receipt.text
            assert len(model.calls) == 4

    asyncio.run(run())


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


def test_publication_operations_and_receipts_are_also_served_under_api(identity, monkeypatch):
    bff, worker, model, store, config, account, cookie, session, state, sent, targets, app = setup(identity, monkeypatch)
    pub = PUBS[0]

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=config.public_origin,
            cookies={"__Host-cyclotron-session": cookie},
            headers={"Origin": config.public_origin, "X-CSRF-Token": session.csrf_token, "X-Cyclotron-Account": account},
        ) as http:
            response = await http.post(
                f"/api/publications/{pub}/operations",
                json={
                    "contractVersion": "cyclotron-publication/v1",
                    "operation": "decide",
                    "payload": {"bindingRevision": "binding-one", "item": ITEM},
                },
            )
            assert response.status_code == 202, response.text
            await worker.worker().process(sent.pop(0))
            receipt = await http.get(f"/api/publications/{pub}/commands/{response.json()['jobId']}")
            assert receipt.status_code == 200, receipt.text
            assert receipt.json()["publicationId"] == pub and receipt.json()["decision"]["classifiers"]

    asyncio.run(run())


def test_binding_operations_and_receipts_are_served_under_api_in_the_decision_contract(identity, monkeypatch):
    bff, worker, model, store, config, account, cookie, session, state, sent, targets, app = setup(identity, monkeypatch)
    pub = PUBS[0]

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=config.public_origin,
            cookies={"__Host-cyclotron-session": cookie},
            headers={"Origin": config.public_origin, "X-CSRF-Token": session.csrf_token, "X-Cyclotron-Account": account},
        ) as http:
            asked = {"operation": "decide", "payload": {"bindingRevision": "binding-one", "item": ITEM}}
            refused = await http.post(
                f"/api/bindings/{pub}/operations", json={"contractVersion": "cyclotron-publication/v1", **asked}
            )
            assert refused.status_code == 400
            response = await http.post(
                f"/api/bindings/{pub}/operations", json={"contractVersion": "cyclotron-decision/v1", **asked}
            )
            assert response.status_code == 202, response.text
            assert response.json()["contractVersion"] == "cyclotron-decision/v1" and response.json()["bindingId"] == pub
            await worker.worker().process(sent.pop(0))
            receipt = await http.get(f"/api/bindings/{pub}/commands/{response.json()['jobId']}")
            assert receipt.status_code == 200, receipt.text
            value = receipt.json()
            assert value["contractVersion"] == "cyclotron-decision/v1" and value["bindingId"] == pub
            assert "publicationId" not in value and value["decision"]["classifiers"]

    asyncio.run(run())


def test_fifth_publication_waits_for_criteria_without_joining_the_shared_model_request(identity, monkeypatch):
    bff, worker, model, store, config, account, cookie, session, state, sent, targets, app = setup(identity, monkeypatch)
    target = targets[0]
    targets[0] = replace(
        target,
        bindings=(*target.bindings, PublicationBinding("data-parade", ("data-parade",), "criteria-pending")),
        pending_outputs=("data-parade",),
    )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=config.public_origin,
            cookies={"__Host-cyclotron-session": cookie},
            headers={"Origin": config.public_origin, "X-CSRF-Token": session.csrf_token, "X-Cyclotron-Account": account},
        ) as http:
            status = await http.post(
                "/publications/data-parade/operations",
                json={"contractVersion": "cyclotron-publication/v1", "operation": "status", "payload": {}},
            )
            assert status.status_code == 200, status.text
            metadata = status.json()
            assert metadata["status"] == "pending_criteria"
            assert metadata["pendingOutputs"] == ["data-parade"] and metadata["outputs"] == []
            assert "decision" not in metadata and not sent and not model.calls
            refused = await http.post(
                "/publications/data-parade/operations",
                json={
                    "contractVersion": "cyclotron-publication/v1",
                    "operation": "decide",
                    "payload": {"bindingRevision": "criteria-pending", "item": ITEM},
                },
            )
            assert refused.status_code == 409 and not sent and not model.calls
            active = await http.post(
                "/publications/alpha/operations",
                json={
                    "contractVersion": "cyclotron-publication/v1",
                    "operation": "decide",
                    "payload": {"bindingRevision": "binding-one", "item": ITEM},
                },
            )
            assert active.status_code == 202, active.text
            await worker.worker().process(sent.pop())
            assert len(targets) == 1 and targets[0].run_id == target.run_id
            assert len(model.calls) == 1 and set(model.calls[0]) == set(PUBS)
            assert "data-parade" not in {row["id"] for row in store.run(target.run_id)["config"]["classifiers"]}
            assert not store.item_labels("sources", ITEM["id"], 1)

    asyncio.run(run())


def test_pending_configuration_declares_five_outputs_without_inventing_a_classifier():
    raw = [
        {
            "accountId": str(uuid4()),
            "runId": str(uuid4()),
            "bindings": [
                {"publicationId": pub, "outputs": [pub], "revision": "synthetic-declaration"}
                for pub in ("alpha", "beta", "gamma", "delta", "epsilon")
            ],
            "pendingOutputs": ["epsilon"],
        }
    ]
    targets = targets_from_environment({"CYCLOTRON_PUBLICATION_TARGETS": json.dumps(raw)})
    assert len(targets) == 1 and len(targets[0].bindings) == 5
    assert targets[0].pending_outputs == ("epsilon",)
    assert all(set(row) == {"publicationId", "outputs", "revision"} for row in raw[0]["bindings"])
    for pending in (["undeclared"], ["epsilon", "epsilon"], "epsilon"):
        with pytest.raises(ValueError, match="configuration"):
            targets_from_environment({"CYCLOTRON_PUBLICATION_TARGETS": json.dumps([{**raw[0], "pendingOutputs": pending}])})


def test_standing_runs_name_their_bindings_by_binding_id_and_read_the_same_as_the_publication_targets():
    run = {"accountId": str(uuid4()), "runId": str(uuid4()), "standing": True}
    standing = [{**run, "bindings": [{"bindingId": "alpha", "outputs": ["alpha"], "revision": "r1"}]}]
    legacy = [{**run, "bindings": [{"publicationId": "alpha", "outputs": ["alpha"], "revision": "r1"}]}]
    new = targets_from_environment({"CYCLOTRON_STANDING_RUNS": json.dumps(standing)})
    assert new == targets_from_environment({"CYCLOTRON_PUBLICATION_TARGETS": json.dumps(legacy)})
    assert new[0].bindings[0].binding_id == "alpha" and new[0].standing
    # Each variable takes its own key, and setting both is refused rather than quietly ignoring one.
    with pytest.raises(ValueError, match="configuration"):
        targets_from_environment({"CYCLOTRON_STANDING_RUNS": json.dumps(legacy)})
    with pytest.raises(ValueError, match="configuration"):
        targets_from_environment({"CYCLOTRON_PUBLICATION_TARGETS": json.dumps(standing)})
    with pytest.raises(ValueError, match="not both"):
        targets_from_environment(
            {"CYCLOTRON_STANDING_RUNS": json.dumps(standing), "CYCLOTRON_PUBLICATION_TARGETS": json.dumps(legacy)}
        )
    assert targets_from_environment({}) is None


def test_pending_criteria_cannot_be_silently_replaced_by_a_runnable_classifier(identity, monkeypatch):
    bff, worker, model, store, config, account, cookie, session, state, sent, targets, app = setup(identity, monkeypatch)
    target = replace(targets[0], pending_outputs=("alpha",))
    with pytest.raises(ValueError, match="runnable classifier"):
        envelope(target, target.bindings[0], store.run(target.run_id)["config"])
    assert not model.calls and not sent


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


async def accepted_example(http, *, list_id="priming-alpha", item_id="synthetic-human-example", output="alpha"):
    import base64

    async def mutation(action, payload):
        result = await http.post(
            "/graphql",
            params={"account": http.headers["X-Cyclotron-Account"]},
            json={
                "query": "mutation($action:String!,$payload:JSON!){primingCommand(action:$action,payload:$payload)}",
                "variables": {"action": action, "payload": payload},
            },
        )
        data = result.json()
        assert "errors" not in data and "data" in data, data
        return data["data"]["primingCommand"]

    row = {
        "id": item_id,
        "occurred_at": "2026-01-01",
        "values": {"text": "Synthetic human-approved context"},
        "labels": {output: {"label": "include", "rationale": "Synthetic human explanation"}},
    }
    draft = await mutation(
        "file",
        {
            "identifier": list_id,
            "list_id": list_id,
            "refs": [{"id": output, "revision": 1}],
            "kind": "json",
            "content": base64.b64encode(json.dumps([row]).encode()).decode(),
        },
    )
    assert draft["status"] == "preview"
    accepted = await mutation("accept", {"identifier": list_id, "version": draft["version"], "labels": None})
    assert accepted["status"] == "accepted"


def priming_client(app, config, cookie, session, account):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=config.public_origin,
        cookies={"__Host-cyclotron-session": cookie},
        headers={"Origin": config.public_origin, "X-CSRF-Token": session.csrf_token, "X-Cyclotron-Account": account},
    )


def test_accepted_human_priming_reopens_native_s3_engine_without_provider_authority(identity, monkeypatch):
    bff, worker, model, store, config, account, cookie, session, state, sent, targets, app = setup(identity, monkeypatch)
    session.account_id = account
    bff.sessions.save(session)
    targets[0] = replace(targets[0], model_identity=model.model_identity)
    bff.config = worker.config = replace(config, allow_live=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("pure priming constructed a provider or optimizer")

    bff.model_factory = worker.model_factory = forbidden
    for name in ("priming-alpha", "same-approved-context"):
        store.save_item_list(name, "Synthetic human priming")

    async def run():
        async with priming_client(app, config, cookie, session, account) as http:
            for name in ("priming-alpha", "same-approved-context"):
                await accepted_example(http, list_id=name)
                payload = {"bindingRevision": "binding-one", "listId": name}
                response = await http.post(
                    "/publications/alpha/operations",
                    json={"contractVersion": "cyclotron-publication/v1", "operation": "prime", "payload": payload},
                )
                assert response.status_code == 202, response.text
                job = response.json()
                message = sent.pop()
                assert await worker.worker().process(message) == {"status": "completed", "jobId": job["jobId"]}
                receipt = await http.get(f"/publications/alpha/commands/{job['jobId']}")
                assert receipt.status_code == 200, receipt.text
                result = receipt.json()
                assert result["primedExamples"] == {"alpha": 1}
                assert result["added"] == ({"alpha": ["synthetic-human-example"]} if name == "priming-alpha" else {})
                assert result["primingAdditions"] == [
                    {"addedBy": session.person_id, "examples": {"alpha": ["synthetic-human-example"]}}
                ]
                assert not model.calls and "decision" not in result
                duplicate = await http.post(
                    "/publications/alpha/operations",
                    json={"contractVersion": "cyclotron-publication/v1", "operation": "prime", "payload": payload},
                )
                assert duplicate.json()["jobId"] == job["jobId"]
                assert await worker.worker().process(sent.pop()) == {"status": "completed", "jobId": job["jobId"]}
                foreign = await http.get(f"/publications/beta/commands/{job['jobId']}")
                assert foreign.status_code == 404
            status = await http.post(
                "/publications/alpha/operations",
                json={"contractVersion": "cyclotron-publication/v1", "operation": "status", "payload": {}},
            )
            assert status.json()["classifiers"]["alpha"]["cyclotron"]["primedExamples"] == 1
            denied = await http.post(
                "/publications/alpha/operations",
                json={
                    "contractVersion": "cyclotron-publication/v1",
                    "operation": "decide",
                    "payload": {"bindingRevision": "binding-one", "item": ITEM},
                },
            )
            assert denied.status_code == 503 and not model.calls
            assert len(store.jobs(targets[0].run_id)) == 2
            # Reopen with a real adapter identity after pure-data priming; this
            # remains a synthetic provider, never a live paid request.
            bff.config = worker.config = config
            bff.model_factory = worker.model_factory = lambda config: (model, DisabledOptimizer())
            predicted = await http.post(
                "/publications/gamma/operations",
                json={
                    "contractVersion": "cyclotron-publication/v1",
                    "operation": "decide",
                    "payload": {"bindingRevision": "binding-one", "item": ITEM},
                },
            )
            assert predicted.status_code == 202, predicted.text
            await worker.worker().process(sent.pop())
            status = await http.post(
                "/publications/gamma/operations",
                json={"contractVersion": "cyclotron-publication/v1", "operation": "status", "payload": {}},
            )
            assert status.json()["classifiers"]["gamma"]["cyclotron"]["primedExamples"] == 0
            assert len(model.calls) == 1 and set(model.calls[0]) == set(PUBS)

    asyncio.run(run())


@pytest.mark.parametrize(
    "change", ["viewer", "revision", "binding", "overlap", "catalog-change", "revoked", "fence", "identity", "mid-write"]
)
def test_native_priming_refuses_stale_authority_scope_or_catalog_before_applying(identity, monkeypatch, change):
    from .prospectus_accounts import AccountAccessDenied

    bff, worker, model, store, config, account, cookie, session, state, sent, targets, app = setup(identity, monkeypatch)
    session.account_id = account
    bff.sessions.save(session)
    targets[0] = replace(targets[0], model_identity=model.model_identity)
    bff.config = worker.config = replace(config, allow_live=False)
    store.save_item_list("priming-alpha", "Synthetic human priming")

    async def run():
        async with priming_client(app, config, cookie, session, account) as http:
            await accepted_example(http, item_id=ITEM["id"] if change == "overlap" else "synthetic-human-example")
            if change == "viewer":
                state["role"] = "viewer"
            if change == "revision":
                store.save_classifier(
                    "alpha", "Changed", {"question": "Changed?", "classes": [{"label": "include"}, {"label": "exclude"}]}
                )
            if change == "identity":
                targets[0] = replace(targets[0], model_identity=None)
            response = await http.post(
                "/publications/alpha/operations",
                json={
                    "contractVersion": "cyclotron-publication/v1",
                    "operation": "prime",
                    "payload": {"bindingRevision": "stale" if change == "binding" else "binding-one", "listId": "priming-alpha"},
                },
            )
            if change in ("viewer", "revision", "binding", "overlap", "identity"):
                assert response.status_code in (403, 409, 503), response.text
                assert not sent
                return
            assert response.status_code == 202, response.text
            message = sent.pop()
            if change == "catalog-change":
                store.label_item("alpha", 1, "priming-alpha", "synthetic-human-example", 1, "exclude", "Corrected", "correction")
            elif change == "revoked":
                state["denied"] = True
            elif change == "fence":
                store.claim_command(owner=str(uuid4()), job_id=message["jobId"])
            elif change == "mid-write":
                from .hosted_publications import _GuardedJournal

                commit = _GuardedJournal.commit

                def revoke_before_priming_write(journal, expected, value):
                    if str(value.get("schema")).startswith("cyclotron-s3-embedded/"):
                        saved = value["data"]["meta"].get("priming_examples")
                        if saved and "synthetic-human-example" in str(saved):
                            state["denied"] = True
                    return commit(journal, expected, value)

                monkeypatch.setattr(_GuardedJournal, "commit", revoke_before_priming_write)
            with pytest.raises((ValueError, AccountAccessDenied, RuntimeError)):
                await worker.worker().process(message)
            assert not model.calls
            assert store.command_envelope(message["jobId"])[0]["status"] != "completed"

    asyncio.run(run())
    assert not model.calls


def test_each_output_carries_a_fingerprint_of_its_exact_criteria_question():
    from hashlib import sha256
    from types import SimpleNamespace

    from .hosted_publications import envelope

    def config(question):
        return {
            "classifiers": [
                {
                    "id": "alpha",
                    "config": {"question": question, "classes": [{"label": "include", "role": "positive"}, {"label": "exclude"}]},
                }
            ]
        }

    binding = SimpleNamespace(publication_id="alpha", revision="4", outputs=["alpha"])
    target = SimpleNamespace(account_id="account-a", bindings=[binding], pending_outputs=())
    (output,) = envelope(target, binding, config("Is this relevant?"))["outputs"]
    assert output["criteriaFingerprint"] == sha256(b"Is this relevant?").hexdigest()
    (changed,) = envelope(target, binding, config("Is this relevant to ML?"))["outputs"]
    assert changed["criteriaFingerprint"] != output["criteriaFingerprint"]


def test_a_publication_request_body_over_sixty_four_kilobytes_is_accepted_and_queued(identity, monkeypatch):
    bff, worker, model, store, config, account, cookie, session, state, sent, targets, app = setup(identity, monkeypatch)
    value = {
        "contractVersion": "cyclotron-publication/v1",
        "operation": "decide",
        "payload": {"bindingRevision": "binding-one", "item": ITEM},
    }
    content = json.dumps(value)[:-1] + " " * 200_000 + "}"

    async def run():
        async with priming_client(app, config, cookie, session, account) as http:
            response = await http.post("/publications/alpha/operations", content=content)
            assert response.status_code == 202, response.text
            assert len(sent) == 1

    asyncio.run(run())

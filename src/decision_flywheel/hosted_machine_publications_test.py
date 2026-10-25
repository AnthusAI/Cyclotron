"""Standing runs reached by a SigV4 service principal, over the real hosted API, queue, worker and S3 stores."""

import asyncio
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest

from . import account_sessions_test
from .command_refusal import CommandRefused
from .hosted_publications import PublicationTarget
from .hosted_publications_test import ITEM, PUBS, setup
from .machine_access import MachineActor, MachineDenied
from .provider_credentials import ProviderKeyUnavailable

ROLE = "arn:aws:iam::222222222222:role/synthetic-invoker"
CALLER = "arn:aws:sts::222222222222:assumed-role/synthetic-invoker/papyrus-monitor"
CONTRACT = "cyclotron-publication/v1"
identity = account_sessions_test.identity


class InjectIam:
    """Test stand-in for API Gateway: it alone supplies the verified IAM identity in the ASGI scope."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            arn = dict(scope["headers"]).get(b"x-test-verified-arn")
            if arn:
                scope["aws.event"] = {"requestContext": {"authorizer": {"iam": {"userArn": arn.decode()}}}}
        await self.app(scope, receive, send)


def standing(targets):
    targets[0] = replace(targets[0], standing=True)


def fixture(identity, monkeypatch):
    parts = setup(identity, monkeypatch)
    bff, worker, model, store, config, account, cookie, session, state, sent, targets, app = parts
    standing(targets)
    bff.machine_principals = worker.machine_principals = (ROLE,)
    return parts, InjectIam(app)


def client(app, config, account, arn=CALLER):
    headers = {"X-Cyclotron-Account": account}
    if arn:
        headers["X-Test-Verified-Arn"] = arn
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=config.public_origin, headers=headers)


def body(operation, payload, request_id=None):
    value = {"contractVersion": CONTRACT, "operation": operation, "payload": payload}
    if request_id:
        value["requestId"] = request_id
    return value


def new_item(name="hn-1", text=None):
    return {"id": name, "values": {"text": text or f"A synthetic streaming source named {name}."}}


async def decide(http, worker, sent, item, request_id=None, publication="alpha"):
    request_id = request_id or "req-" + uuid4().hex
    response = await http.post(
        f"/machine/publications/{publication}/operations",
        json=body("decide", {"bindingRevision": "binding-one", "item": item}, request_id),
    )
    assert response.status_code == 202, response.text
    job = response.json()["jobId"]
    while sent:
        await worker.worker().process(sent.pop(0))
    return job, await http.get(f"/machine/publications/{publication}/commands/{job}")


def test_a_new_streaming_item_is_registered_decided_once_and_shared_across_outputs(identity, monkeypatch):
    (bff, worker, model, store, config, account, *_, sent, targets, app), wrapped = fixture(identity, monkeypatch)
    run_id = targets[0].run_id

    async def go():
        async with client(wrapped, config, account) as http:
            job, receipt = await decide(http, worker, sent, new_item())
            assert receipt.status_code == 200, receipt.text
            first = receipt.json()
            assert first["source"]["id"] == "hn-1" and first["source"]["revision"] == 1
            assert set(first["decision"]["classifiers"]) == {"alpha"}
            assert len(model.calls) == 1 and set(model.calls[0]) == set(PUBS)
            # Every other publication output reuses the one native decision with no second provider call.
            other, receipt = await decide(http, worker, sent, new_item(), publication="beta")
            assert receipt.json()["decision"]["decisionId"] == first["decision"]["decisionId"]
            assert len(model.calls) == 1
            # An identical request id is the same durable job; a new request id for identical content is a cache hit.
            again = await http.post(
                "/machine/publications/alpha/operations",
                json=body("decide", {"bindingRevision": "binding-one", "item": new_item()}, "req-fixed-0001"),
            )
            assert again.status_code == 202
            while sent:
                await worker.worker().process(sent.pop(0))
            same = await http.post(
                "/machine/publications/alpha/operations",
                json=body("decide", {"bindingRevision": "binding-one", "item": new_item()}, "req-fixed-0001"),
            )
            assert same.status_code == 202 and same.json()["jobId"] == again.json()["jobId"]
            sent.clear()
            conflict = await http.post(
                "/machine/publications/alpha/operations",
                json=body("decide", {"bindingRevision": "binding-one", "item": new_item(text="other")}, "req-fixed-0001"),
            )
            assert conflict.status_code == 409 and conflict.json()["code"] == "request_conflict"
            fresh, receipt = await decide(http, worker, sent, new_item())
            assert fresh != job and receipt.json()["decision"]["decisionId"] == first["decision"]["decisionId"]
            assert len(model.calls) == 1

    asyncio.run(go())
    ids = [row["id"] for row in store.items(run_id)]
    assert ids == [ITEM["id"], "hn-1"]
    assert [r["revision"] for r in store.snapshot_items("sources") if r["id"] == "hn-1"] == [1]
    job = store.jobs(run_id)[0]
    actor = store.command_envelope(job["id"])[1]
    assert isinstance(actor, MachineActor) and actor.principal == ROLE and actor.session == "papyrus-monitor"


def test_same_id_with_different_values_is_a_new_immutable_revision_and_a_new_decision(identity, monkeypatch):
    (bff, worker, model, store, config, account, *_, sent, targets, app), wrapped = fixture(identity, monkeypatch)

    async def go():
        async with client(wrapped, config, account) as http:
            _, one = await decide(http, worker, sent, new_item(text="first version"))
            _, two = await decide(http, worker, sent, new_item(text="edited version"))
            assert one.json()["source"]["revision"] == 1 and two.json()["source"]["revision"] == 2
            assert one.json()["decision"]["decisionId"] != two.json()["decision"]["decisionId"]
            assert len(model.calls) == 2

    asyncio.run(go())
    assert [store.item_revision("sources", "hn-1", n)["values"]["text"] for n in (1, 2)] == ["first version", "edited version"]
    assert [row for row in store.items(targets[0].run_id) if row["id"] == "hn-1"][0]["revision"] == 2


def test_ids_that_exist_in_a_priming_list_are_refused_without_registration_or_a_request(identity, monkeypatch):
    (bff, worker, model, store, config, account, *_, sent, targets, app), wrapped = fixture(identity, monkeypatch)
    store.save_item_list("priming", "Synthetic priming")
    store.upsert_list_items("priming", [{"id": "primed-1", "occurred_at": "2026-01-01", "values": {"text": "labelled"}}])

    async def go():
        async with client(wrapped, config, account) as http:
            _, receipt = await decide(http, worker, sent, new_item("primed-1", "labelled"))
            assert receipt.status_code == 409 and receipt.json()["code"] == "source_refused"
            assert not model.calls
            _, receipt = await decide(http, worker, sent, new_item("hn-2"))
            assert receipt.status_code == 200

    asyncio.run(go())
    assert "primed-1" not in {row["id"] for row in store.items(targets[0].run_id)}
    assert len(model.calls) == 1


def test_standing_targets_process_more_than_the_former_daily_lifetime_and_item_ceilings(identity, monkeypatch):
    (bff, worker, model, store, config, account, *_, sent, targets, app), wrapped = fixture(identity, monkeypatch)

    async def go():
        async with client(wrapped, config, account) as http:
            for index in range(301):
                _, receipt = await decide(http, worker, sent, new_item(f"hn-{index}"))
                assert receipt.status_code == 200, receipt.text
            assert len(model.calls) == 301
            assert len(store.items(targets[0].run_id)) >= 301

    asyncio.run(go())


def test_a_missing_provider_key_refuses_before_registration_and_before_reserving_a_request(identity, monkeypatch):
    (bff, worker, model, store, config, account, *_, sent, targets, app), wrapped = fixture(identity, monkeypatch)
    available = {"key": False}

    def factory(config):
        if not available["key"]:
            raise ProviderKeyUnavailable()
        return model, __import__("decision_flywheel.optimizer_agent", fromlist=["x"]).DisabledOptimizer()

    worker.model_factory = factory

    async def go():
        async with client(wrapped, config, account) as http:
            _, receipt = await decide(http, worker, sent, new_item("hn-1"))
            assert receipt.status_code == 503 and receipt.json()["code"] == "provider_key_unavailable"
            assert "hn-1" not in {row["id"] for row in store.items(targets[0].run_id)} and not model.calls
            available["key"] = True
            # A missing key does not register the item or consume an attempt.
            _, receipt = await decide(http, worker, sent, new_item("hn-1"))
            assert receipt.status_code == 200 and len(model.calls) == 1

    asyncio.run(go())


@pytest.mark.parametrize(
    "arn",
    [
        None,
        "arn:aws:sts::222222222222:assumed-role/other-role/papyrus-monitor",
        "arn:aws:sts::333333333333:assumed-role/synthetic-invoker/papyrus-monitor",
        "arn:aws:iam::222222222222:user/synthetic-invoker",
        "arn:aws:sts::222222222222:assumed-role/synthetic-invoker*/x",
    ],
)
def test_unlisted_or_unsigned_callers_cannot_reach_any_machine_route(identity, monkeypatch, arn):
    (bff, worker, model, store, config, account, *_, sent, targets, app), wrapped = fixture(identity, monkeypatch)

    async def go():
        async with client(wrapped, config, account, arn) as http:
            http.headers["X-Amzn-Iam-User-Arn"] = CALLER  # a spoofed header is never identity
            for call in (
                http.post("/machine/publications/alpha/operations", json=body("status", {})),
                http.post(
                    "/machine/publications/alpha/operations",
                    json=body("decide", {"bindingRevision": "binding-one", "item": ITEM}, "req-abcdefgh"),
                ),
                http.get(f"/machine/publications/alpha/commands/{uuid4()}"),
            ):
                assert (await call).status_code == 403

    asyncio.run(go())
    assert not sent and not model.calls


def test_target_selection_is_closed_account_pinned_standing_only_and_never_caller_chosen(identity, monkeypatch):
    (bff, worker, model, store, config, account, *_, sent, targets, app), wrapped = fixture(identity, monkeypatch)

    async def go():
        async with client(wrapped, config, str(uuid4())) as http:  # another account pin
            assert (await http.post("/machine/publications/alpha/operations", json=body("status", {}))).status_code == 403
        async with client(wrapped, config, account) as http:
            assert (await http.post("/machine/publications/unknown/operations", json=body("status", {}))).status_code == 403
            for extra in ({"accountId": str(uuid4())}, {"runId": str(uuid4())}):
                response = await http.post(
                    "/machine/publications/alpha/operations",
                    json={**body("decide", {"bindingRevision": "binding-one", "item": ITEM}, "req-abcdefgh"), **extra},
                )
                assert response.status_code == 400
            for operation in ("prime", "review", "replay", "steer", "bootstrap"):
                response = await http.post("/machine/publications/alpha/operations", json=body(operation, {}, "req-abcdefgh"))
                assert response.status_code == 400
            assert (
                await http.post(
                    "/machine/publications/alpha/operations",
                    json=body("decide", {"bindingRevision": "stale", "item": ITEM}, "req-abcdefgh"),
                )
            ).status_code == 409
        targets[0] = replace(targets[0], standing=False)
        async with client(wrapped, config, account) as http:
            assert (await http.post("/machine/publications/alpha/operations", json=body("status", {}))).status_code == 403

    asyncio.run(go())
    assert not sent and not model.calls


def test_authority_is_rechecked_by_the_worker_so_revocation_stops_queued_work(identity, monkeypatch):
    (bff, worker, model, store, config, account, *_, sent, targets, app), wrapped = fixture(identity, monkeypatch)

    async def go():
        async with client(wrapped, config, account) as http:
            response = await http.post(
                "/machine/publications/alpha/operations",
                json=body("decide", {"bindingRevision": "binding-one", "item": new_item()}, "req-abcdefgh"),
            )
            assert response.status_code == 202
            worker.machine_principals = ()
            with pytest.raises(MachineDenied):
                await worker.worker().process(sent.pop(0))

    asyncio.run(go())
    assert not model.calls and "hn-1" not in {row["id"] for row in store.items(targets[0].run_id)}


def test_live_disabled_refuses_decide_but_status_and_feedback_remain_available(identity, monkeypatch):
    (bff, worker, model, store, config, account, *_, sent, targets, app), wrapped = fixture(identity, monkeypatch)
    bff.config = replace(bff.config, allow_live=False)

    async def go():
        async with client(wrapped, config, account) as http:
            response = await http.post(
                "/machine/publications/alpha/operations",
                json=body("decide", {"bindingRevision": "binding-one", "item": ITEM}, "req-abcdefgh"),
            )
            assert response.status_code == 503

    asyncio.run(go())
    assert not sent and not model.calls


def feedback_payload(**changes):
    return {
        "bindingRevision": "binding-one",
        "itemId": "hn-1",
        "itemRevision": 1,
        "output": "alpha",
        "label": "include",
        "comment": "Editor kept this one.",
        "editor": {"site": "anth-us", "subject": "editor-17"},
    } | changes


def test_feedback_records_one_idempotent_human_label_with_provenance_and_triggers_no_learning(identity, monkeypatch):
    (bff, worker, model, store, config, account, *_, sent, targets, app), wrapped = fixture(identity, monkeypatch)
    run_id = targets[0].run_id

    async def go():
        async with client(wrapped, config, account) as http:
            await decide(http, worker, sent, new_item())
            calls = len(model.calls)
            url = "/machine/publications/alpha/operations"
            first = await http.post(url, json=body("feedback", feedback_payload(), "fb-req-0001"))
            assert first.status_code == 200, first.text
            value = first.json()
            assert value["status"] == "recorded" and value["recordedAs"] == "human-label"
            assert value["asserted"] == {"machine": ROLE, "editor": {"site": "anth-us", "subject": "editor-17"}}
            repeat = await http.post(url, json=body("feedback", feedback_payload(), "fb-req-0001"))
            assert repeat.status_code == 200
            assert (
                await http.post(url, json=body("feedback", feedback_payload(label="exclude"), "fb-req-0001"))
            ).status_code == 409
            # Scoped to this binding's outputs and to submitted sources, with valid labels only.
            assert (
                await http.post(url, json=body("feedback", feedback_payload(output="beta"), "fb-req-0002"))
            ).status_code == 403
            assert (
                await http.post(url, json=body("feedback", feedback_payload(itemId="never-sent"), "fb-req-0003"))
            ).status_code == 409
            assert (
                await http.post(url, json=body("feedback", feedback_payload(label="maybe"), "fb-req-0004"))
            ).status_code == 409
            assert (
                await http.post(url, json=body("feedback", feedback_payload(itemRevision=9), "fb-req-0005"))
            ).status_code == 409
            assert (
                await http.post(url, json=body("feedback", feedback_payload(editor={"site": "x"}), "fb-req-0006"))
            ).status_code == 400
            assert (
                await http.post(url, json=body("feedback", {**feedback_payload(), "accountId": account}, "fb-req-0007"))
            ).status_code == 400
            assert len(model.calls) == calls and not sent

    asyncio.run(go())
    labels = store.item_labels("sources", "hn-1", 1)
    assert [(row["classifier_id"], row["label"], row["comment"]) for row in labels] == [
        ("alpha", "include", "Editor kept this one.")
    ]
    events = [e["payload"] for e in store.all_events(run_id) if e["payload"]["kind"] == "publication-machine-feedback"]
    assert len(events) == 1 and events[0]["machine"]["principal"] == ROLE and events[0]["editor"]["asserted"] is True


def test_status_serves_the_last_completed_snapshot_for_machine_callers(identity, monkeypatch):
    (bff, worker, model, store, config, account, *_, sent, targets, app), wrapped = fixture(identity, monkeypatch)

    async def go():
        async with client(wrapped, config, account) as http:
            url = "/machine/publications/alpha/operations"
            assert (await http.post(url, json=body("status", {}))).status_code == 503
            await decide(http, worker, sent, new_item())
            response = await http.post(url, json=body("status", {}))
            assert response.status_code == 200 and response.json()["publicationId"] == "alpha"

    asyncio.run(go())


def test_a_machine_actor_cannot_be_forged_for_another_target_or_account():
    with pytest.raises(ValueError):
        MachineActor("arn:aws:iam::222222222222:role/*", "papyrus-monitor", str(uuid4()), str(uuid4()), "b")
    with pytest.raises(ValueError):
        MachineActor(ROLE, "papyrus-monitor", str(uuid4()), str(uuid4()), "b", operation="console:read")


def test_standing_targets_need_no_arbitrary_quota_configuration():
    from .publication_lab import PublicationBinding

    bindings = (PublicationBinding("alpha", ("alpha",), "one"),)
    a, r = str(uuid4()), str(uuid4())
    assert PublicationTarget(a, r, bindings, standing=True).standing is True
    assert PublicationTarget(a, r, bindings).standing is False


def test_refusals_are_public_codes_only():
    assert CommandRefused("x", "y").code == "x"


def test_an_item_whose_earlier_attempt_failed_is_refused_with_a_receipt_instead_of_wedging_the_run(identity, monkeypatch):
    (bff, worker, model, store, config, account, *_, sent, targets, app), wrapped = fixture(identity, monkeypatch)
    run_id = targets[0].run_id
    real = model.classify_many
    state = {"fail": True}

    async def flaky(*args, **kwargs):
        if state["fail"]:
            raise RuntimeError("synthetic provider failure after dispatch")
        return await real(*args, **kwargs)

    model.classify_many = flaky

    async def go():
        async with client(wrapped, config, account) as http:
            first = await http.post(
                "/machine/publications/alpha/operations",
                json=body("decide", {"bindingRevision": "binding-one", "item": new_item("hn-1")}, "req-first-0001"),
            )
            assert first.status_code == 202
            # A failure after dispatch stays fail-closed: the claimed job is left running for an operator.
            with pytest.raises(RuntimeError):
                await worker.worker().process(sent.pop(0))
            state["fail"] = False
            stuck = next(j for j in store.jobs(run_id) if j["status"] == "running")
            claim = store.command_recovery(stuck["id"])
            store.reconcile_command(
                stuck["id"],
                owner=claim["owner"],
                fence=claim["fence"],
                status="failed",
                reason="synthetic failure, no prediction",
                operator="test",
                apply=True,
            )
            # The same item again hits the failed shared request: a recorded refusal, not a running job.
            job, receipt = await decide(http, worker, sent, new_item("hn-1"))
            assert receipt.status_code == 409 and receipt.json()["code"] == "source_refused"
            assert "operator retry" in receipt.json()["message"]
            assert not any(j["status"] == "running" for j in store.jobs(run_id))
            # The run is free: another item is decided normally.
            _, receipt = await decide(http, worker, sent, new_item("hn-2"))
            assert receipt.status_code == 200

    asyncio.run(go())

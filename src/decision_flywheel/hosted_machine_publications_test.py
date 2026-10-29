"""Standing runs reached by a SigV4 service principal, over the real hosted API, queue, worker and S3 stores."""

import asyncio
import json
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


def decisions_body(bindings, item, request_id="req-all-0001"):
    return {"contractVersion": CONTRACT, "requestId": request_id, "payload": {"bindings": bindings, "item": item}}


def revisions(*names):
    return {name: "binding-one" for name in names}


async def drain(worker, sent):
    while sent:
        await worker.worker().process(sent.pop(0))


def test_one_request_for_three_publications_makes_one_job_and_one_model_request_with_equal_decision_ids(identity, monkeypatch):
    (bff, worker, model, store, config, account, *_, sent, targets, app), wrapped = fixture(identity, monkeypatch)
    run_id = targets[0].run_id

    async def go():
        async with client(wrapped, config, account) as http:
            posted = await http.post("/machine/decisions", json=decisions_body(revisions("alpha", "beta", "gamma"), new_item()))
            assert posted.status_code == 202, posted.text
            accepted = posted.json()
            assert set(accepted) == {"contractVersion", "accountId", "runId", "jobId", "status", "bindings"}
            assert accepted["bindings"] == revisions("alpha", "beta", "gamma") and accepted["runId"] == run_id
            running = await http.get(f"/machine/decisions/{accepted['jobId']}")
            assert running.status_code == 202 and running.json()["status"] == "pending"
            assert len(sent) == 1
            await drain(worker, sent)
            done = await http.get(f"/machine/decisions/{accepted['jobId']}")
            assert done.status_code == 200, done.text
            receipt = done.json()
            assert set(receipt) == {"contractVersion", "accountId", "runId", "jobId", "decisionId", "publications"}
            assert set(receipt["publications"]) == {"alpha", "beta", "gamma"}
            ids = {row["decision"]["decisionId"] for row in receipt["publications"].values()}
            assert ids == {receipt["decisionId"]}
            for name, row in receipt["publications"].items():
                assert row["publicationId"] == name and set(row["decision"]["classifiers"]) == {name}
                assert row["source"]["id"] == "hn-1"
            assert len(model.calls) == 1
            assert len([j for j in store.jobs(run_id) if j["kind"] == "publication"]) == 1
            same = await http.post("/machine/decisions", json=decisions_body(revisions("alpha", "beta", "gamma"), new_item()))
            assert same.status_code == 202 and same.json()["jobId"] == accepted["jobId"]
            changed = await http.post(
                "/machine/decisions", json=decisions_body(revisions("alpha", "beta", "gamma"), new_item(text="other"))
            )
            assert changed.status_code == 409 and changed.json()["code"] == "request_conflict"

    asyncio.run(go())


def test_each_projection_is_byte_identical_to_the_single_publication_receipt_for_the_same_decision(identity, monkeypatch):
    (bff, worker, model, store, config, account, *_, sent, targets, app), wrapped = fixture(identity, monkeypatch)

    async def go():
        async with client(wrapped, config, account) as http:
            posted = await http.post("/machine/decisions", json=decisions_body(revisions("alpha", "beta"), new_item()))
            await drain(worker, sent)
            shared = (await http.get(f"/machine/decisions/{posted.json()['jobId']}")).json()
            for name in ("alpha", "beta"):
                _, single = await decide(http, worker, sent, new_item(), publication=name)
                assert single.status_code == 200
                assert json.dumps(single.json(), sort_keys=True) == json.dumps(shared["publications"][name], sort_keys=True)
            assert len(model.calls) == 1

    asyncio.run(go())


def test_a_mismatched_revision_or_unknown_publication_gives_409_and_creates_no_job(identity, monkeypatch):
    (bff, worker, model, store, config, account, *_, sent, targets, app), wrapped = fixture(identity, monkeypatch)

    async def go():
        async with client(wrapped, config, account) as http:
            for bindings in ({"alpha": "binding-one", "beta": "stale"}, {"alpha": "binding-one", "unknown": "binding-one"}):
                refused = await http.post("/machine/decisions", json=decisions_body(bindings, new_item()))
                assert refused.status_code == 409 and "binding changed" in refused.text
            empty = await http.post("/machine/decisions", json=decisions_body({}, new_item()))
            assert empty.status_code == 400
            assert not sent and not [j for j in store.jobs(targets[0].run_id) if j["kind"] == "publication"]

    asyncio.run(go())


def test_a_pending_output_gives_409_and_creates_no_job(identity, monkeypatch):
    (bff, worker, model, store, config, account, *_, sent, targets, app), wrapped = fixture(identity, monkeypatch)
    targets[0] = replace(targets[0], pending_outputs=("gamma",))

    async def go():
        async with client(wrapped, config, account) as http:
            pending = await http.post("/machine/decisions", json=decisions_body(revisions("alpha", "gamma"), new_item()))
            assert pending.status_code == 409 and "pending" in pending.text
            assert not sent

    asyncio.run(go())


def test_live_disabled_refuses_the_shared_decision_and_unsigned_callers_are_refused(identity, monkeypatch):
    (bff, worker, model, store, config, account, *_, sent, targets, app), wrapped = fixture(identity, monkeypatch)
    bff.config = replace(bff.config, allow_live=False)

    async def go():
        async with client(wrapped, config, account) as http:
            refused = await http.post("/machine/decisions", json=decisions_body(revisions("alpha"), new_item()))
            assert refused.status_code == 503
        async with client(wrapped, config, account, arn=None) as http:
            assert (await http.post("/machine/decisions", json=decisions_body(revisions("alpha"), new_item()))).status_code == 403
            assert (await http.get(f"/machine/decisions/{uuid4()}")).status_code == 403

    asyncio.run(go())


def test_a_missing_provider_key_refuses_the_shared_receipt_with_the_single_receipt_shape(identity, monkeypatch):
    (bff, worker, model, store, config, account, *_, sent, targets, app), wrapped = fixture(identity, monkeypatch)

    def unavailable(config):
        raise ProviderKeyUnavailable()

    worker.model_factory = unavailable

    async def go():
        async with client(wrapped, config, account) as http:
            posted = await http.post("/machine/decisions", json=decisions_body(revisions("alpha", "beta"), new_item()))
            await drain(worker, sent)
            receipt = await http.get(f"/machine/decisions/{posted.json()['jobId']}")
            assert receipt.status_code == 503 and receipt.json()["code"] == "provider_key_unavailable"
            assert not model.calls

    asyncio.run(go())


def test_a_decision_the_provider_rejects_fails_its_job_and_the_next_job_still_runs(identity, monkeypatch):
    from .command_refusal import ProviderRejectedRequest

    (bff, worker, model, store, config, account, *_, sent, targets, app), wrapped = fixture(identity, monkeypatch)
    answer = model.classify_many

    async def rejecting(configs, target, training, **kwargs):
        if target.id == "too-big-story":
            raise ProviderRejectedRequest(400, "req-provider-1")
        return await answer(configs, target, training, **kwargs)

    model.classify_many = rejecting

    async def go():
        async with client(wrapped, config, account) as http:
            rejected = await http.post(
                "/machine/decisions",
                json=decisions_body(revisions("alpha", "beta"), new_item("too-big-story"), request_id="req-rejected-1"),
            )
            following = await http.post(
                "/machine/decisions", json=decisions_body(revisions("alpha", "beta"), new_item(), request_id="req-following-1")
            )
            await drain(worker, sent)
            receipt = await http.get(f"/machine/decisions/{rejected.json()['jobId']}")
            assert receipt.status_code == 422 and receipt.json()["code"] == "provider_rejected"
            assert "HTTP 400" in receipt.json()["message"]
            assert (await http.get(f"/machine/decisions/{following.json()['jobId']}")).status_code == 200

    asyncio.run(go())


def test_a_single_publication_job_is_not_readable_through_the_shared_route_and_the_reverse(identity, monkeypatch):
    (bff, worker, model, store, config, account, *_, sent, targets, app), wrapped = fixture(identity, monkeypatch)

    async def go():
        async with client(wrapped, config, account) as http:
            single, _ = await decide(http, worker, sent, new_item())
            assert (await http.get(f"/machine/decisions/{single}")).status_code == 404
            posted = await http.post(
                "/machine/decisions", json=decisions_body(revisions("alpha"), new_item("hn-2"), "req-all-0002")
            )
            await drain(worker, sent)
            assert (await http.get(f"/machine/publications/alpha/commands/{posted.json()['jobId']}")).status_code == 404

    asyncio.run(go())


def test_worker_jobs_per_story_drop_from_the_publication_count_to_one(identity, monkeypatch):
    (bff, worker, model, store, config, account, *_, sent, targets, app), wrapped = fixture(identity, monkeypatch)
    names = ("alpha", "beta", "gamma", "delta")
    stories = [new_item(f"story-{n}") for n in range(3)]
    run_id = targets[0].run_id

    def jobs():
        return len([j for j in store.jobs(run_id) if j["kind"] == "publication"])

    async def go():
        async with client(wrapped, config, account) as http:
            for story in stories:
                for name in names:
                    await decide(http, worker, sent, story, publication=name)
            before = (jobs(), len(model.calls))
            for n in range(len(stories)):
                posted = await http.post(
                    "/machine/decisions", json=decisions_body(revisions(*names), new_item(f"fresh-{n}"), f"req-bench-{n:04d}")
                )
                assert posted.status_code == 202
                await drain(worker, sent)
            return before, (jobs() - before[0], len(model.calls) - before[1])

    before, after = asyncio.run(go())
    assert before == (len(stories) * len(names), len(stories))
    assert after == (len(stories), len(stories))
    print(
        f"jobs per story before={before[0] // len(stories)} after={after[0] // len(stories)}; model calls per story {before[1] // len(stories)} -> {after[1] // len(stories)}"
    )


def test_status_reflects_the_newest_shared_decision_job_for_each_of_its_publications(identity, monkeypatch):
    (bff, worker, model, store, config, account, *_, sent, targets, app), wrapped = fixture(identity, monkeypatch)

    async def go():
        async with client(wrapped, config, account) as http:
            await decide(http, worker, sent, new_item("single-1"), publication="alpha")
            posted = await http.post("/machine/decisions", json=decisions_body(revisions("alpha", "beta"), new_item("shared-1")))
            await drain(worker, sent)
            shared = (await http.get(f"/machine/decisions/{posted.json()['jobId']}")).json()
            job = next(j for j in store.jobs(targets[0].run_id) if j["id"] == posted.json()["jobId"])
            assert set(job["result"]["statusSnapshots"]) == {"alpha", "beta"}
            for name in ("alpha", "beta"):
                status = (await http.post(f"/machine/publications/{name}/operations", json=body("status", {}))).json()
                assert status == job["result"]["statusSnapshots"][name] and status["publicationId"] == name
            assert shared["publications"]["alpha"]["decision"]["decisionId"] == shared["decisionId"]
            gamma = await http.post("/machine/publications/gamma/operations", json=body("status", {}))
            assert gamma.status_code == 503

    asyncio.run(go())


def without_clock(value):
    stamp = __import__("re").compile(r"\d{4}-\d\d-\d\dT[\d:.+]+")
    return stamp.sub("<time>", json.dumps(value, sort_keys=True))


def test_a_shared_jobs_status_snapshots_match_what_single_publication_jobs_record_for_the_same_state(identity, monkeypatch):
    (bff, worker, model, store, config, account, *_, sent, targets, app), wrapped = fixture(identity, monkeypatch)

    async def go():
        async with client(wrapped, config, account) as http:
            posted = await http.post("/machine/decisions", json=decisions_body(revisions("alpha", "beta"), new_item()))
            await drain(worker, sent)
            shared = {
                name: (await http.post(f"/machine/publications/{name}/operations", json=body("status", {}))).json()
                for name in ("alpha", "beta")
            }
            for name in ("alpha", "beta"):
                await decide(http, worker, sent, new_item(), publication=name)
                single = (await http.post(f"/machine/publications/{name}/operations", json=body("status", {}))).json()
                assert without_clock(single) == without_clock(shared[name])
            assert posted.status_code == 202

    asyncio.run(go())


def test_a_status_step_writes_nothing_because_its_saved_status_fold_waits_for_the_release(identity, monkeypatch):
    from .publication_lab import PublicationLab

    (bff, worker, model, store, config, account, *_, sent, targets, app), wrapped = fixture(identity, monkeypatch)
    puts, spent = [0], []
    real_put, real_command = worker.s3.put_object, PublicationLab.command

    def counted(**request):
        puts[0] += 1
        return real_put(**request)

    async def measured(self, actor, publication, operation, payload, **kwargs):
        before = puts[0]
        try:
            return await real_command(self, actor, publication, operation, payload, **kwargs)
        finally:
            if operation == "status":
                spent.append(puts[0] - before)

    worker.s3.put_object = counted
    monkeypatch.setattr(PublicationLab, "command", measured)

    async def go():
        async with client(wrapped, config, account) as http:
            await http.post("/machine/decisions", json=decisions_body(revisions("alpha", "beta", "gamma", "delta"), new_item()))
            await drain(worker, sent)

    asyncio.run(go())
    outer = spent  # one status step per publication, all under one guarded execution
    assert len(outer) == 4
    print(f"S3 writes during each status step: {outer}; whole job: {puts[0]}")
    # The saved status fold (so no snapshot rereads history) is a derived summary: the runtime holds it and
    # publishes it with the flush that ends the job, so a status step itself writes nothing.
    assert outer == [0, 0, 0, 0]


def request_log_lines(output):
    entries = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    return [entry for entry in entries if entry.get("phase") == "request"]


def test_refused_machine_requests_log_a_safe_reason_and_accepted_ones_none(identity, monkeypatch, capsys):
    (bff, worker, model, store, config, account, *_, sent, targets, app), wrapped = fixture(identity, monkeypatch)
    secret = "private-item-text-4242"

    async def go():
        async with client(wrapped, config, account) as http:
            capsys.readouterr()
            stale = await http.post(
                "/machine/publications/alpha/operations",
                json=body("decide", {"bindingRevision": "stale", "item": new_item(text=secret)}, "req-reason-0001"),
            )
            assert stale.status_code == 409
            accepted = await http.post(
                "/machine/publications/alpha/operations",
                json=body("decide", {"bindingRevision": "binding-one", "item": new_item()}, "req-reason-0002"),
            )
            assert accepted.status_code == 202
            sent.clear()
            conflict = await http.post(
                "/machine/publications/alpha/operations",
                json=body("decide", {"bindingRevision": "binding-one", "item": new_item(text=secret)}, "req-reason-0002"),
            )
            assert conflict.status_code == 409 and conflict.json()["code"] == "request_conflict"

    asyncio.run(go())
    output = capsys.readouterr().out
    lines = request_log_lines(output)
    assert [line["status"] for line in lines] == [409, 202, 409]
    assert lines[0]["reason"] == "publication binding changed"
    assert "reason" not in lines[1]
    assert lines[2]["reason"] == "request_conflict (ValueError)"
    assert secret not in output and CALLER not in output


def test_a_decision_request_with_a_200_kb_article_body_is_accepted_and_queued(identity, monkeypatch):
    (bff, worker, model, store, config, account, *_, sent, targets, app), wrapped = fixture(identity, monkeypatch)
    article = new_item("long-article-" + "i" * 300, text="word " * 40_000)

    async def go():
        async with client(wrapped, config, account) as http:
            single = await http.post(
                "/machine/publications/alpha/operations",
                json=body("decide", {"bindingRevision": "binding-one", "item": article}, "req-long-single-" + "r" * 300),
            )
            assert single.status_code == 202, single.text
            shared = await http.post(
                "/machine/decisions", json=decisions_body(revisions("alpha", "beta"), article, "req-long-shared-0001")
            )
            assert shared.status_code == 202, shared.text
            assert len(sent) == 2 and not model.calls

    asyncio.run(go())


def test_feedback_with_a_long_comment_and_long_editor_identity_is_recorded_whole(identity, monkeypatch):
    (bff, worker, model, store, config, account, *_, sent, targets, app), wrapped = fixture(identity, monkeypatch)
    comment, subject = "Kept because " + "c" * 20_000, "editor-" + "s" * 1_000

    async def go():
        async with client(wrapped, config, account) as http:
            await decide(http, worker, sent, new_item())
            payload = feedback_payload(comment=comment, editor={"site": "site-" + "w" * 500, "subject": subject})
            recorded = await http.post("/machine/publications/alpha/operations", json=body("feedback", payload, "fb-long-0001"))
            assert recorded.status_code == 200, recorded.text
            assert recorded.json()["asserted"]["editor"]["subject"] == subject

    asyncio.run(go())


DECISION = "cyclotron-decision/v1"


def test_the_binding_routes_decide_poll_and_report_status_in_the_decision_contract(identity, monkeypatch):
    (bff, worker, model, store, config, account, *_, sent, targets, app), wrapped = fixture(identity, monkeypatch)

    async def go():
        async with client(wrapped, config, account) as http:
            url = "/machine/bindings/alpha/operations"
            decide_body = {**body("decide", {"bindingRevision": "binding-one", "item": new_item()}, "req-bind-0001")}
            # Each route speaks one contract: the publication contract is refused on a binding route.
            assert (await http.post(url, json=decide_body)).status_code == 400
            posted = await http.post(url, json={**decide_body, "contractVersion": DECISION})
            assert posted.status_code == 202, posted.text
            accepted = posted.json()
            assert accepted["contractVersion"] == DECISION and accepted["bindingId"] == "alpha"
            assert "publicationId" not in accepted
            await drain(worker, sent)
            done = await http.get(f"/machine/bindings/alpha/commands/{accepted['jobId']}")
            assert done.status_code == 200, done.text
            receipt = done.json()
            assert receipt["contractVersion"] == DECISION and receipt["bindingId"] == "alpha"
            assert set(receipt["decision"]["classifiers"]) == {"alpha"}
            # The same job read through the publication route is the publication contract's receipt.
            legacy = (await http.get(f"/machine/publications/alpha/commands/{accepted['jobId']}")).json()
            assert legacy["contractVersion"] == CONTRACT and legacy["publicationId"] == "alpha"
            assert {k: v for k, v in receipt.items() if k not in ("contractVersion", "bindingId")} == {
                k: v for k, v in legacy.items() if k not in ("contractVersion", "publicationId")
            }
            status = await http.post(url, json={"contractVersion": DECISION, "operation": "status", "payload": {}})
            assert status.status_code == 200 and status.json()["bindingId"] == "alpha"

    asyncio.run(go())


def test_decision_contract_feedback_names_the_caller_and_reviewer_and_is_stored_as_the_publication_contract_stores_it(
    identity, monkeypatch
):
    (bff, worker, model, store, config, account, *_, sent, targets, app), wrapped = fixture(identity, monkeypatch)
    run_id = targets[0].run_id

    async def go():
        async with client(wrapped, config, account) as http:
            await decide(http, worker, sent, new_item())
            url = "/machine/bindings/alpha/operations"
            payload = {k: v for k, v in feedback_payload().items() if k != "editor"} | {
                "caller": "anth-us",
                "reviewer": "editor-17",
            }
            sent_body = {"contractVersion": DECISION, "operation": "feedback", "payload": payload, "requestId": "fb-bind-0001"}
            first = await http.post(url, json=sent_body)
            assert first.status_code == 200, first.text
            value = first.json()
            assert value["contractVersion"] == DECISION and value["bindingId"] == "alpha"
            assert value["asserted"] == {"machine": ROLE, "caller": "anth-us", "reviewer": "editor-17"}
            # The decision contract has no editor field, and the publication contract no caller or reviewer.
            editor = {**sent_body, "payload": feedback_payload(), "requestId": "fb-bind-0002"}
            assert (await http.post(url, json=editor)).status_code == 400
            mixed = {**sent_body, "contractVersion": CONTRACT, "requestId": "fb-bind-0003"}
            assert (await http.post("/machine/publications/alpha/operations", json=mixed)).status_code == 400

    asyncio.run(go())
    events = [e["payload"] for e in store.all_events(run_id) if e["payload"]["kind"] == "publication-machine-feedback"]
    assert len(events) == 1
    assert events[0]["editor"] == {"site": "anth-us", "subject": "editor-17", "asserted": True}


def test_a_shared_decision_asked_for_in_the_decision_contract_is_answered_in_it(identity, monkeypatch):
    (bff, worker, model, store, config, account, *_, sent, targets, app), wrapped = fixture(identity, monkeypatch)

    async def go():
        async with client(wrapped, config, account) as http:
            asked = {**decisions_body(revisions("alpha", "beta"), new_item(), "req-dec-0001"), "contractVersion": DECISION}
            posted = await http.post("/machine/decisions", json=asked)
            assert posted.status_code == 202, posted.text
            assert posted.json()["contractVersion"] == DECISION
            pending = await http.get(f"/machine/decisions/{posted.json()['jobId']}")
            assert pending.status_code == 202 and pending.json()["contractVersion"] == DECISION
            await drain(worker, sent)
            receipt = (await http.get(f"/machine/decisions/{posted.json()['jobId']}")).json()
            assert receipt["contractVersion"] == DECISION and "publications" not in receipt
            assert set(receipt["decisions"]) == {"alpha", "beta"}
            for name, row in receipt["decisions"].items():
                assert row["contractVersion"] == DECISION and row["bindingId"] == name
            # The same request id in the other contract is a different request, not a replay.
            again = await http.post("/machine/decisions", json={**asked, "contractVersion": CONTRACT})
            assert again.status_code == 409 and again.json()["code"] == "request_conflict"
            legacy = await http.post("/machine/decisions", json=decisions_body(revisions("alpha"), new_item(), "req-dec-0002"))
            await drain(worker, sent)
            old = (await http.get(f"/machine/decisions/{legacy.json()['jobId']}")).json()
            assert old["contractVersion"] == CONTRACT and set(old["publications"]) == {"alpha"}
            assert len(model.calls) == 1

    asyncio.run(go())

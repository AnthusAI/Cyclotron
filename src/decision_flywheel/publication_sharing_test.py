"""A consuming tenant receives only explicitly delegated inference results."""

import asyncio
import copy
import json
import threading
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from .publication_sharing import (
    DeliveryRequest,
    EngineBinding,
    GrantSpec,
    InferenceRequest,
    ResolvedDelivery,
    SharingConflict,
    SharingDenied,
    SharingReconciliationRequired,
    SharingService,
    SharingUnavailable,
    SourceReference,
    VerifiedPrincipal,
)


class AtomicTestStore:
    """Synthetic transactional port, deliberately not a production adapter."""

    def __init__(self):
        self.grants, self.requests, self.sources, self.events = {}, {}, {}, []
        self.lock = threading.RLock()
        self.fail_audit = None
        self.lose_commit_ack = None

    @contextmanager
    def transaction(self):
        with self.lock:
            before = copy.deepcopy((self.grants, self.requests, self.sources, self.events))
            try:
                yield self
            except BaseException:
                self.grants, self.requests, self.sources, self.events = before
                raise
            if self.lose_commit_ack and self.events[-1]["kind"] == self.lose_commit_ack:
                self.lose_commit_ack = None
                raise OSError("commit acknowledgement lost")

    def load_grant(self, identifier):
        return self.grants.get(identifier)

    def save_grant(self, grant):
        self.grants[grant.spec.grant_id] = grant

    def load_request(self, key):
        return self.requests.get(key)

    def save_request(self, record):
        self.requests[record.key] = record

    def load_source(self, key):
        return self.sources.get(key)

    def save_source(self, key, content_hash):
        self.sources[key] = content_hash

    def append_audit(self, event):
        if event["kind"] == self.fail_audit:
            raise OSError("audit sink unavailable")
        self.events.append(copy.deepcopy(event))


class Harness:
    def __init__(self):
        self.store = AtomicTestStore()
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.principal = VerifiedPrincipal("owner", "owner-person")
        self.owner_allowed = True
        self.use_allowed = True
        self.external_outage = False
        self.calls = []
        self.binding_reads = 0
        self.owner_checks = []
        self.use_checks = []
        self.lock = asyncio.Lock()
        self.waiting = asyncio.Event()
        self.after_inference = None
        self.after_delivery = None
        self.deliveries = []
        self.delivery_allowed = True
        self.source_allowed = True
        self.source_checks = []
        self.provider_error = False
        self.raw_result = {
            "decisionId": "central-decision",
            "providerRequests": 1,
            "outputs": {
                "consumer-output": {"label": "match", "confidence": 0.8, "version": 1, "rubric": "private rubric"},
                "sibling-output": {"label": "private sibling result", "confidence": 0.9, "version": 2},
            },
            "source": "private owner source",
            "feedback": "private feedback",
            "labels": ["private training label"],
        }
        self.spec = GrantSpec(
            grant_id="grant-one",
            owner_account_id="owner",
            consumer_account_id="consumer",
            publication_id="consumer-publication",
            engine_id="shared-central",
            binding_revision="binding-one",
            execution_outputs=("consumer-output", "sibling-output"),
            visible_outputs=("consumer-output",),
            expires_at=self.now + timedelta(hours=1),
            max_requests=3,
            billing_account_id="owner",
            allowed_operations=("infer", "deliver"),
        )
        self.binding = EngineBinding("owner", "shared-central", "binding-one", self.spec.execution_outputs)
        self.service = SharingService(
            self.store,
            resolve_principal=self.resolve_principal,
            authorize_owner=self.authorize_owner,
            authorize_use=self.authorize_use,
            resolve_binding=self.resolve_binding,
            infer=self.infer,
            resolve_delivery=self.resolve_delivery,
            validate_source=self.validate_source,
            clock=lambda: self.now,
        )

    def resolve_principal(self):
        if self.external_outage:
            raise OSError("identity service down")
        return self.principal

    def authorize_owner(self, principal, spec, operation):
        self.owner_checks.append((principal, spec, operation))
        if not self.owner_allowed:
            raise SharingDenied("owner delegation refused")

    def authorize_use(self, principal, grant, operation):
        self.use_checks.append((principal, grant.revision, operation))
        if not self.use_allowed:
            raise SharingDenied("live owner grant refused")

    def resolve_binding(self, spec):
        self.binding_reads += 1
        return self.binding

    def validate_source(self, principal, grant, execution):
        self.source_checks.append((principal, grant, execution))
        if not self.source_allowed:
            raise SharingDenied("source is not accessible or does not match the canonical revision")
        return SourceReference(
            execution.source_account_id,
            execution.source_publication_id,
            execution.source_list_id,
            execution.source_id,
            execution.source_revision,
            execution.content_hash,
        )

    async def infer(self, execution, guard):
        self.waiting.set()
        async with self.lock:
            await guard()  # Actual engine adapter must check after its ownership lock.
            self.calls.append(execution)
            if self.provider_error:
                raise TimeoutError("provider may have completed")
            if self.after_inference:
                self.after_inference()
            return copy.deepcopy(self.raw_result)

    def delivery_request(self, **changes):
        return DeliveryRequest(
            **{
                "grant_id": "grant-one",
                "grant_revision": 1,
                "request_id": "delivery-request",
                "consumer_account_id": "consumer",
                "publication_id": "consumer-publication",
                "delivery_id": "addressed-delivery",
                **changes,
            }
        )

    def resolve_delivery(self, grant, principal, delivery_id):
        self.deliveries.append((grant, principal, delivery_id))
        if not self.delivery_allowed or delivery_id != "addressed-delivery":
            raise SharingDenied("no addressed delivery")
        result = ResolvedDelivery(
            delivery_id=delivery_id,
            grant_id=grant.spec.grant_id,
            grant_revision=grant.revision,
            owner_account_id="owner",
            consumer_account_id="consumer",
            publication_id="consumer-publication",
            recipient_id="consumer-person",
            engine_id="shared-central",
            binding_revision="binding-one",
            execution_outputs=self.spec.execution_outputs,
            central_receipt_id="original-central-receipt",
            billing_account_id="owner",
            source=SourceReference("origin-owner", "discovery-publication", "central-sources", "discovery-one", 1, "a" * 64),
            result=copy.deepcopy(self.raw_result),
        )
        if self.after_delivery:
            return self.after_delivery(result)
        return result

    async def issue(self):
        result = await self.service.issue(self.spec)
        self.principal = VerifiedPrincipal("consumer", "consumer-person")
        return result

    def request(self, **changes):
        return InferenceRequest(
            **{
                "grant_id": self.spec.grant_id,
                "grant_revision": 1,
                "request_id": "request-one",
                "consumer_account_id": "consumer",
                "publication_id": "consumer-publication",
                "source_list_id": "consumer-sources",
                "source_id": "source-one",
                "source_revision": 1,
                "values": {"text": "consumer-owned source text"},
                **changes,
            }
        )


def run(coroutine):
    return asyncio.run(coroutine)


def test_an_owner_explicitly_delegates_inference_without_granting_account_membership():
    async def scenario():
        h = Harness()
        grant = await h.issue()
        receipt = await h.service.decide(h.request())
        assert grant.revision == 1 and grant.status == "active"
        assert h.owner_checks[0][2] == "issue"
        assert h.calls[0].owner_account_id == "owner"
        assert h.calls[0].consumer_account_id == "consumer"
        assert h.calls[0].engine_id == "shared-central"
        assert h.calls[0].execution_outputs == ("consumer-output", "sibling-output")
        assert h.calls[0].visible_outputs == ("consumer-output",)
        assert receipt["ownerAccountId"] == "owner" and receipt["consumerAccountId"] == "consumer"
        assert receipt["publicationId"] == "consumer-publication"
        assert receipt["source"]["accountId"] == "consumer"
        assert receipt["source"]["publicationId"] == "consumer-publication"
        assert receipt["source"]["listId"] == "consumer-sources"
        assert receipt["source"]["id"] == "source-one" and receipt["source"]["revision"] == 1
        assert receipt["billing"] == {"accountId": "owner", "policy": "owner-pays", "reservedRequests": 1, "providerRequests": 1}
        assert set(receipt["outputs"]) == {"consumer-output"}
        assert receipt["outputs"]["consumer-output"] == {"label": "match", "confidence": 0.8, "version": 1}
        assert "private" not in json.dumps(receipt)
        assert "source text" not in json.dumps(h.store.events)
        assert "match" not in json.dumps(h.store.events)
        assert h.store.grants["grant-one"].reserved_requests == 1
        assert [event["kind"] for event in h.store.events] == ["issued", "reserved", "completed", "disclosed"]
        assert len(h.use_checks) >= 3

    run(scenario())


@pytest.mark.parametrize("change", ["not-owner", "no-owner-permission", "boolean-owner-permission", "identity-outage"])
def test_grant_fields_cannot_supply_owner_authority(change):
    async def scenario():
        h = Harness()
        if change == "not-owner":
            h.principal = VerifiedPrincipal("consumer", "consumer-person")
        elif change == "no-owner-permission":
            h.owner_allowed = False
        elif change == "boolean-owner-permission":
            h.service.authorize_owner = lambda *args: True
        else:
            h.external_outage = True
        with pytest.raises((SharingDenied, SharingUnavailable)):
            await h.service.issue(h.spec)
        assert not h.store.grants and not h.store.events and not h.calls

    run(scenario())


@pytest.mark.parametrize(
    "changes",
    [
        {"billing_account_id": "consumer"},
        {"consumer_account_id": "owner"},
        {"visible_outputs": ("other",)},
        {"visible_outputs": ("consumer-output", "consumer-output")},
        {"execution_outputs": ()},
        {"execution_outputs": "*"},
        {"execution_outputs": ("*",)},
        {"max_requests": True},
        {"max_requests": 0},
        {"expires_at": datetime(2026, 1, 2)},
        {"allowed_operations": ()},
        {"allowed_operations": ("admin",)},
        {"allowed_operations": ("infer", "infer")},
        {"allowed_operations": "infer"},
    ],
)
def test_grants_require_exact_scopes_and_only_owner_pays_billing(changes):
    with pytest.raises(ValueError):
        replace(Harness().spec, **changes)


def test_duplicate_or_expired_grants_cannot_replace_existing_owner_permission():
    async def scenario():
        h = Harness()
        await h.service.issue(h.spec)
        with pytest.raises(SharingConflict):
            await h.service.issue(replace(h.spec, visible_outputs=h.spec.execution_outputs))
        with pytest.raises(SharingDenied):
            await h.service.issue(replace(h.spec, grant_id="expired", expires_at=h.now))
        assert len(h.store.grants) == 1 and len(h.store.events) == 1

    run(scenario())


@pytest.mark.parametrize(
    "change",
    ["account", "person", "publication", "grant", "revision", "use-refused", "boolean-use", "identity-outage", "unverified"],
)
def test_inference_requires_fresh_consumer_identity_and_explicit_owner_permission(change):
    async def scenario():
        h = Harness()
        await h.issue()
        request = h.request()
        if change == "account":
            h.principal = VerifiedPrincipal("intruder", "consumer-person")
        elif change == "person":
            h.principal = VerifiedPrincipal("consumer", "intruder-person")
            h.use_allowed = False
        elif change == "publication":
            request = h.request(publication_id="other-publication")
        elif change == "grant":
            request = h.request(grant_id="missing")
        elif change == "revision":
            request = h.request(grant_revision=2)
        elif change == "use-refused":
            h.use_allowed = False
        elif change == "boolean-use":
            h.service.authorize_use = lambda *args: True
        elif change == "identity-outage":
            h.external_outage = True
        else:
            h.principal = {"account_id": "consumer", "subject_id": "consumer-person"}
        with pytest.raises((SharingDenied, SharingUnavailable)):
            await h.service.decide(request)
        assert not h.calls and not h.store.requests
        assert h.binding_reads == 1  # Issuance only; denied use never opens owner state.

    run(scenario())


@pytest.mark.parametrize("change", ["owner", "engine", "revision", "outputs"])
def test_execution_binding_must_match_the_complete_authorized_engine_scope(change):
    async def scenario():
        h = Harness()
        await h.issue()
        h.binding = replace(
            h.binding,
            **{
                "owner": {"owner_account_id": "other-owner"},
                "engine": {"engine_id": "other-engine"},
                "revision": {"revision": "binding-two"},
                "outputs": {"outputs": ("consumer-output", "sibling-output", "new-private-output")},
            }[change],
        )
        with pytest.raises(SharingDenied):
            await h.service.decide(h.request())
        assert not h.calls and not h.store.requests

    run(scenario())


@pytest.mark.parametrize(
    "change", ["revoked", "expired", "revision", "person", "binding", "external-grant", "outage", "source-revoked"]
)
def test_a_waiting_request_rechecks_authority_after_engine_ownership_before_dispatch(change):
    async def scenario():
        h = Harness()
        await h.issue()
        await h.lock.acquire()
        task = asyncio.create_task(h.service.decide(h.request()))
        await h.waiting.wait()
        if change == "revoked":
            h.principal = VerifiedPrincipal("owner", "owner-person")
            await h.service.revoke("grant-one", expected_revision=1)
            h.principal = VerifiedPrincipal("consumer", "consumer-person")
        elif change == "expired":
            h.now += timedelta(hours=2)
        elif change == "revision":
            h.store.grants["grant-one"] = replace(h.store.grants["grant-one"], revision=2)
        elif change == "person":
            h.principal = VerifiedPrincipal("consumer", "changed-person")
        elif change == "binding":
            h.binding = replace(h.binding, revision="changed-binding")
        elif change == "external-grant":
            h.use_allowed = False
        elif change == "source-revoked":
            h.source_allowed = False
        else:
            h.external_outage = True
        h.lock.release()
        with pytest.raises((SharingDenied, SharingUnavailable)):
            await task
        assert not h.calls
        assert next(iter(h.store.requests.values())).status == "reserved"

    run(scenario())


@pytest.mark.parametrize("change", ["grant", "person", "expiry", "binding", "source"])
def test_authority_changes_after_prediction_prevent_disclosure_and_do_not_repeat_execution(change):
    async def scenario():
        h = Harness()
        await h.issue()

        def changed():
            if change == "grant":
                h.use_allowed = False
            elif change == "person":
                h.principal = VerifiedPrincipal("consumer", "other-person")
            elif change == "expiry":
                h.now += timedelta(hours=2)
            elif change == "source":
                h.source_allowed = False
            else:
                h.binding = replace(h.binding, revision="later")

        h.after_inference = changed
        with pytest.raises((SharingDenied, SharingUnavailable)):
            await h.service.decide(h.request())
        assert len(h.calls) == 1
        assert next(iter(h.store.requests.values())).status == "reserved"
        h.principal = VerifiedPrincipal("consumer", "consumer-person")
        h.use_allowed, h.now = True, datetime(2026, 1, 1, tzinfo=timezone.utc)
        h.source_allowed = True
        h.binding = replace(h.binding, revision="binding-one")
        with pytest.raises(SharingReconciliationRequired):
            await h.service.decide(h.request())
        assert len(h.calls) == 1

    run(scenario())


def test_completed_retries_are_private_and_idempotent_and_still_need_live_authorization():
    async def scenario():
        h = Harness()
        await h.issue()
        original = await h.service.decide(h.request())
        retry = await h.service.decide(h.request())
        assert retry == original and len(h.calls) == 1
        retry["outputs"]["consumer-output"]["label"] = "mutated client copy"
        assert await h.service.decide(h.request()) == original
        with pytest.raises(SharingConflict):
            await h.service.decide(h.request(values={"text": "changed retry"}))
        h.use_allowed = False
        with pytest.raises(SharingDenied):
            await h.service.decide(h.request())
        assert len(h.calls) == 1 and h.store.grants["grant-one"].reserved_requests == 1

    run(scenario())


def test_sources_and_receipts_are_separate_for_each_consumer_and_publication():
    async def scenario():
        h = Harness()
        await h.issue()
        first = await h.service.decide(h.request())
        h.principal = VerifiedPrincipal("owner", "owner-person")
        other = replace(h.spec, grant_id="grant-two", consumer_account_id="other-consumer", publication_id="other-publication")
        await h.service.issue(other)
        h.principal = VerifiedPrincipal("other-consumer", "consumer-person")
        second = await h.service.decide(
            h.request(grant_id="grant-two", consumer_account_id="other-consumer", publication_id="other-publication")
        )
        assert first["receiptId"] != second["receiptId"]
        assert h.calls[0].item_id != h.calls[1].item_id
        assert first["source"]["contentHash"] == second["source"]["contentHash"]
        assert len(h.store.sources) == 2
        h.principal = VerifiedPrincipal("consumer", "different-person")
        third = await h.service.decide(h.request())
        assert third["receiptId"] != first["receiptId"]  # No other person's receipt is reused.

    run(scenario())


def test_a_source_revision_cannot_change_under_a_new_request_identifier():
    async def scenario():
        h = Harness()
        await h.issue()
        await h.service.decide(h.request())
        with pytest.raises(SharingConflict):
            await h.service.decide(h.request(request_id="request-two", values={"text": "changed source"}))
        assert len(h.calls) == 1 and h.store.grants["grant-one"].reserved_requests == 1

    run(scenario())


def test_concurrent_reservations_cannot_overspend_or_repeat_one_request():
    async def scenario():
        h = Harness()
        h.spec = replace(h.spec, max_requests=1)
        await h.issue()
        await h.lock.acquire()
        first = asyncio.create_task(h.service.decide(h.request()))
        await h.waiting.wait()
        with pytest.raises(SharingReconciliationRequired):
            await h.service.decide(h.request())
        with pytest.raises(SharingDenied):
            await h.service.decide(h.request(request_id="request-two"))
        h.lock.release()
        await first
        assert len(h.calls) == 1 and h.store.grants["grant-one"].reserved_requests == 1

    run(scenario())


@pytest.mark.parametrize("kind", ["issued", "reserved", "completed", "disclosed"])
def test_audit_failure_prevents_the_corresponding_mutation_or_disclosure(kind):
    async def scenario():
        h = Harness()
        h.store.fail_audit = kind
        if kind == "issued":
            with pytest.raises(SharingUnavailable):
                await h.issue()
            assert not h.store.grants
            return
        await h.issue()
        with pytest.raises(SharingUnavailable):
            await h.service.decide(h.request())
        if kind == "reserved":
            assert not h.calls and not h.store.requests and not h.store.sources
            assert h.store.grants["grant-one"].reserved_requests == 0
        else:
            assert len(h.calls) == 1
            record = next(iter(h.store.requests.values()))
            assert record.status == ("reserved" if kind == "completed" else "completed")
            h.store.fail_audit = None
            if kind == "completed":
                with pytest.raises(SharingReconciliationRequired):
                    await h.service.decide(h.request())
            else:
                await h.service.decide(h.request())
            assert len(h.calls) == 1

    run(scenario())


@pytest.mark.parametrize("kind", ["reserved", "completed"])
def test_lost_store_acknowledgements_are_reconciled_without_repeating_provider_work(kind):
    async def scenario():
        h = Harness()
        await h.issue()
        h.store.lose_commit_ack = kind
        with pytest.raises(SharingUnavailable):
            await h.service.decide(h.request())
        calls = len(h.calls)
        if kind == "reserved":
            with pytest.raises(SharingReconciliationRequired):
                await h.service.decide(h.request())
        else:
            receipt = await h.service.decide(h.request())
            assert receipt["decisionId"] == "central-decision"
        assert len(h.calls) == calls

    run(scenario())


def test_uncertain_provider_outcomes_keep_the_reserved_budget_and_require_reconciliation():
    async def scenario():
        h = Harness()
        await h.issue()
        h.provider_error = True
        with pytest.raises(SharingReconciliationRequired):
            await h.service.decide(h.request())
        with pytest.raises(SharingReconciliationRequired):
            await h.service.decide(h.request())
        assert len(h.calls) == 1 and h.store.grants["grant-one"].reserved_requests == 1

    run(scenario())


@pytest.mark.parametrize(
    "change",
    ["missing-output", "invalid-confidence", "invalid-provider-count", "missing-guard", "boolean-version", "string-version"],
)
def test_unverifiable_engine_results_are_not_disclosed_or_automatically_retried(change):
    async def scenario():
        h = Harness()
        await h.issue()
        if change == "missing-output":
            del h.raw_result["outputs"]["consumer-output"]
        elif change == "invalid-confidence":
            h.raw_result["outputs"]["consumer-output"]["confidence"] = float("nan")
        elif change == "invalid-provider-count":
            h.raw_result["providerRequests"] = -1
        elif change == "boolean-version":
            h.raw_result["outputs"]["consumer-output"]["version"] = True
        elif change == "string-version":
            h.raw_result["outputs"]["consumer-output"]["version"] = "v1"
        else:

            async def unguarded(*args):
                return h.raw_result

            h.service.infer = unguarded
        with pytest.raises(SharingReconciliationRequired):
            await h.service.decide(h.request())
        assert not any(event["kind"] == "disclosed" for event in h.store.events)
        with pytest.raises(SharingReconciliationRequired):
            await h.service.decide(h.request())

    run(scenario())


def test_revocation_is_owner_authorized_revisioned_and_audited():
    async def scenario():
        h = Harness()
        await h.issue()
        with pytest.raises(SharingDenied):
            await h.service.revoke("grant-one", expected_revision=1)
        h.principal = VerifiedPrincipal("owner", "owner-person")
        h.store.fail_audit = "revoked"
        with pytest.raises(SharingUnavailable):
            await h.service.revoke("grant-one", expected_revision=1)
        assert h.store.grants["grant-one"].status == "active"
        h.store.fail_audit = None
        revoked = await h.service.revoke("grant-one", expected_revision=1)
        assert revoked.status == "revoked" and revoked.revision == 2
        with pytest.raises(SharingConflict):
            await h.service.revoke("grant-one", expected_revision=1)
        h.principal = VerifiedPrincipal("consumer", "consumer-person")
        with pytest.raises(SharingDenied):
            await h.service.decide(h.request())
        assert not h.calls

    run(scenario())


@pytest.mark.parametrize("values", [{"text": "x" * 70_000}, {"x": float("inf")}, ["not", "an", "object"]])
def test_invalid_or_oversized_source_values_never_reserve_work(values):
    async def scenario():
        h = Harness()
        await h.issue()
        with pytest.raises(ValueError):
            await h.service.decide(h.request(values=values))
        assert not h.calls and not h.store.requests

    run(scenario())


def test_an_addressed_existing_central_decision_is_delivered_without_another_inference():
    async def scenario():
        h = Harness()
        h.spec = replace(h.spec, allowed_operations=("deliver",))
        await h.issue()
        receipt = await h.service.deliver(h.delivery_request())
        assert not h.calls
        assert receipt["decisionId"] == "central-decision"
        assert receipt["delivery"] == {"id": "addressed-delivery", "centralReceiptId": "original-central-receipt"}
        assert receipt["consumerAccountId"] == "consumer"
        assert receipt["source"] == {
            "accountId": "origin-owner",
            "publicationId": "discovery-publication",
            "listId": "central-sources",
            "id": "discovery-one",
            "revision": 1,
            "contentHash": "a" * 64,
        }
        assert receipt["billing"] == {
            "accountId": "owner",
            "policy": "owner-pays",
            "reservedRequests": 1,
            "providerRequests": 0,
            "origin": {"receiptId": "original-central-receipt", "providerRequests": 1},
        }
        assert set(receipt["outputs"]) == {"consumer-output"}
        assert "private" not in json.dumps(receipt)
        assert all(operation == "deliver" for _, _, operation in h.use_checks)
        assert [e["kind"] for e in h.store.events] == ["issued", "delivery_reserved", "delivered", "disclosed"]
        retry = await h.service.deliver(h.delivery_request())
        assert retry == receipt and not h.calls
        assert h.store.grants["grant-one"].reserved_requests == 1

    run(scenario())


@pytest.mark.parametrize("allowed", [("infer",), ("deliver",)])
def test_delivery_permission_and_paid_inference_permission_are_separate(allowed):
    async def scenario():
        h = Harness()
        h.spec = replace(h.spec, allowed_operations=allowed)
        await h.issue()
        with pytest.raises(SharingDenied):
            if allowed == ("deliver",):
                await h.service.decide(h.request())
            else:
                await h.service.deliver(h.delivery_request())
        assert not h.calls and not h.deliveries and not h.store.requests

    run(scenario())


def test_fresh_actor_authority_receives_the_exact_requested_operation():
    async def scenario():
        h = Harness()
        await h.issue()

        def only_delivery(principal, grant, operation):
            if operation != "deliver":
                raise SharingDenied("actor may receive only")

        h.service.authorize_use = only_delivery
        with pytest.raises(SharingDenied):
            await h.service.decide(h.request())
        await h.service.deliver(h.delivery_request())
        assert not h.calls

    run(scenario())


@pytest.mark.parametrize(
    "field,value",
    [
        ("delivery_id", "wrong-delivery"),
        ("grant_id", "wrong-grant"),
        ("grant_revision", 2),
        ("owner_account_id", "wrong-owner"),
        ("consumer_account_id", "wrong-consumer"),
        ("publication_id", "wrong-publication"),
        ("recipient_id", "another-person"),
        ("engine_id", "wrong-engine"),
        ("binding_revision", "wrong-binding"),
        ("execution_outputs", ("consumer-output",)),
        ("billing_account_id", "consumer"),
    ],
)
def test_a_resolved_delivery_must_be_addressed_to_the_exact_grant_recipient_and_binding(field, value):
    async def scenario():
        h = Harness()
        await h.issue()
        h.after_delivery = lambda delivery: replace(delivery, **{field: value})
        with pytest.raises(SharingDenied):
            await h.service.deliver(h.delivery_request())
        assert not h.calls and not any(e["kind"] == "disclosed" for e in h.store.events)

    run(scenario())


def test_guessing_an_owner_receipt_or_delivery_identity_confers_no_access():
    async def scenario():
        h = Harness()
        await h.issue()
        with pytest.raises(SharingDenied):
            await h.service.deliver(h.delivery_request(delivery_id="guessed-owner-receipt"))
        assert not h.calls
        assert len(h.deliveries) == 1
        assert h.deliveries[0][1] == VerifiedPrincipal("consumer", "consumer-person")

    run(scenario())


@pytest.mark.parametrize("change", ["delivery-revoked", "grant-revoked", "source-changed", "decision-changed", "output-changed"])
def test_completed_delivery_retries_reverify_addressing_authority_and_original_provenance(change):
    async def scenario():
        h = Harness()
        await h.issue()
        await h.service.deliver(h.delivery_request())
        if change == "delivery-revoked":
            h.delivery_allowed = False
        elif change == "grant-revoked":
            h.use_allowed = False
        elif change == "source-changed":
            h.after_delivery = lambda d: replace(d, source=replace(d.source, revision=2))
        elif change == "decision-changed":
            h.raw_result["decisionId"] = "different-decision"
        else:
            h.raw_result["outputs"]["consumer-output"]["label"] = "different"
        with pytest.raises((SharingDenied, SharingConflict)):
            await h.service.deliver(h.delivery_request())
        assert not h.calls
        assert len([e for e in h.store.events if e["kind"] == "disclosed"]) == 1

    run(scenario())


def test_revocation_during_addressed_delivery_resolution_prevents_disclosure():
    async def scenario():
        h = Harness()
        await h.issue()

        def revoke(delivery):
            h.use_allowed = False
            return delivery

        h.after_delivery = revoke
        with pytest.raises(SharingDenied):
            await h.service.deliver(h.delivery_request())
        assert not h.calls and not any(e["kind"] == "disclosed" for e in h.store.events)

    run(scenario())


@pytest.mark.parametrize("kind", ["delivery_reserved", "delivered", "disclosed"])
def test_addressed_delivery_also_fails_closed_when_required_audit_cannot_commit(kind):
    async def scenario():
        h = Harness()
        await h.issue()
        h.store.fail_audit = kind
        with pytest.raises(SharingUnavailable):
            await h.service.deliver(h.delivery_request())
        assert not h.calls and not any(e["kind"] == "disclosed" for e in h.store.events)
        if kind == "delivery_reserved":
            assert not h.deliveries
            assert h.store.grants["grant-one"].reserved_requests == 0

    run(scenario())


def test_one_native_central_decision_keeps_its_integer_version_when_delivered_to_another_account(tmp_path):
    from .embedded_cyclotron import ClassifierSpec, Cyclotron, CyclotronDefinition
    from .models import Item
    from .publication_lab_test import OfflineModel

    async def scenario():
        h = Harness()
        model = OfflineModel()
        definition = CyclotronDefinition(
            "shared-central",
            tuple(
                ClassifierSpec(output, ("accept", "reject"), "Synthetic classification?") for output in h.spec.execution_outputs
            ),
            seed="synthetic-sharing",
        )
        with Cyclotron.open(tmp_path / "central", definition, model) as engine:
            native = await engine.decide(Item("original-owner-discovery", {"text": "synthetic original discovery"}))
            h.raw_result = {
                "decisionId": native.decision_id,
                "providerRequests": model.calls,
                "outputs": native.to_json()["classifiers"],
            }
            h.spec = replace(h.spec, allowed_operations=("deliver",))
            await h.issue()
            receipt = await h.service.deliver(h.delivery_request())
            assert receipt["decisionId"] == native.decision_id
            assert receipt["outputs"]["consumer-output"]["version"] == 1
            assert len(native.results) == 2 and len(receipt["outputs"]) == 1
            assert model.calls == 1 and not h.calls
            assert receipt["billing"]["providerRequests"] == 0
            assert receipt["billing"]["origin"]["providerRequests"] == 1

    run(scenario())


def test_central_discovery_without_an_origin_publication_preserves_an_explicit_null():
    async def scenario():
        h = Harness()
        await h.issue()
        h.after_delivery = lambda d: replace(d, source=replace(d.source, publication_id=None))
        receipt = await h.service.deliver(h.delivery_request())
        assert receipt["source"]["publicationId"] is None
        assert receipt["source"]["accountId"] == "origin-owner"
        assert receipt["publicationId"] == "consumer-publication"

    run(scenario())


def test_revocation_preserves_concurrent_reservations_without_waiting_for_a_quiet_account():
    async def scenario():
        h = Harness()
        await h.issue()
        consumer = SharingService(
            h.store,
            resolve_principal=lambda: VerifiedPrincipal("consumer", "consumer-person"),
            authorize_owner=h.authorize_owner,
            authorize_use=h.authorize_use,
            resolve_binding=h.resolve_binding,
            infer=h.infer,
            resolve_delivery=h.resolve_delivery,
            validate_source=h.validate_source,
            clock=lambda: h.now,
        )
        h.principal = VerifiedPrincipal("owner", "owner-person")
        performed = False

        async def authorize_owner(principal, spec, operation):
            nonlocal performed
            if operation == "revoke" and not performed:
                performed = True
                await consumer.decide(h.request())

        h.service.authorize_owner = authorize_owner
        revoked = await h.service.revoke("grant-one", expected_revision=1)
        assert revoked.status == "revoked" and revoked.reserved_requests == 1
        assert len(h.calls) == 1
        with pytest.raises(SharingDenied):
            await consumer.decide(h.request())

    run(scenario())


def test_equal_source_identifiers_in_different_catalogs_never_alias():
    async def scenario():
        h = Harness()
        await h.issue()
        first = await h.service.decide(h.request())
        second = await h.service.decide(
            h.request(
                request_id="another-request",
                source_list_id="other-sources",
                values={"text": "a different source"},
            )
        )
        assert first["source"]["id"] == second["source"]["id"]
        assert first["source"]["revision"] == second["source"]["revision"] == 1
        assert first["source"]["listId"] != second["source"]["listId"]
        assert h.calls[0].item_id != h.calls[1].item_id
        assert len(h.store.sources) == 2

    run(scenario())


@pytest.mark.parametrize("revision", [True, 0, "1", "revision-one"])
def test_source_revisions_preserve_native_positive_integers(revision):
    with pytest.raises(ValueError):
        Harness().request(source_revision=revision)
    with pytest.raises(ValueError):
        SourceReference("origin-owner", None, "sources", "source-one", revision, "a" * 64)


@pytest.mark.parametrize("change", ["refused", "outage", "boolean", "account", "list", "item", "revision"])
def test_source_validation_prevents_unverified_data_from_poisoning_a_catalog_namespace(change):
    async def scenario():
        h = Harness()
        await h.issue()

        def validate(principal, grant, execution):
            if change == "refused":
                raise SharingDenied("source mismatch")
            if change == "outage":
                raise OSError("catalog unavailable")
            if change == "boolean":
                return True
            original = h.validate_source(principal, grant, execution)
            return replace(
                original,
                **{
                    "account": {"account_id": "other-account"},
                    "list": {"list_id": "other-list"},
                    "item": {"source_id": "other-item"},
                    "revision": {"revision": 2},
                }[change],
            )

        h.service.validate_source = validate
        with pytest.raises((SharingDenied, SharingUnavailable)):
            await h.service.decide(h.request())
        assert not h.calls and not h.store.sources and not h.store.requests
        assert h.store.grants["grant-one"].reserved_requests == 0
        h.service.validate_source = h.validate_source
        await h.service.decide(h.request())
        assert len(h.calls) == 1

    run(scenario())


def test_inference_preserves_the_verified_native_source_hash_and_optional_origin():
    async def scenario():
        h = Harness()
        await h.issue()

        def native_reference(principal, grant, execution):
            return replace(h.validate_source(principal, grant, execution), publication_id=None, content_hash="b" * 64)

        h.service.validate_source = native_reference
        receipt = await h.service.decide(h.request())
        assert receipt["source"]["contentHash"] == "b" * 64
        assert receipt["source"]["publicationId"] is None
        assert h.calls[0].values == h.request().values
        h.source_allowed = False
        with pytest.raises(SharingDenied):
            await h.service.decide(h.request())
        assert len(h.calls) == 1

    run(scenario())


def test_addressed_central_delivery_never_uses_consumer_inference_source_validation():
    async def scenario():
        h = Harness()
        await h.issue()
        h.source_allowed = False
        await h.service.deliver(h.delivery_request())
        assert not h.source_checks and not h.calls

    run(scenario())


@pytest.mark.parametrize("phase", ["dispatch", "completion", "disclosure"])
@pytest.mark.parametrize("change", ["grant", "principal", "binding"])
def test_authority_is_rechecked_after_waiting_for_source_validation(phase, change):
    async def scenario():
        h = Harness()
        await h.issue()
        trigger = {"dispatch": 2, "completion": 3, "disclosure": 4}[phase]

        async def validation(principal, grant, execution):
            source = h.validate_source(principal, grant, execution)
            await asyncio.sleep(0)
            if len(h.source_checks) == trigger:
                if change == "grant":
                    h.use_allowed = False
                elif change == "principal":
                    h.principal = VerifiedPrincipal("consumer", "different-person")
                else:
                    h.binding = replace(h.binding, revision="changed-binding")
            return source

        h.service.validate_source = validation
        with pytest.raises(SharingDenied):
            await h.service.decide(h.request())
        assert len(h.calls) == (0 if phase == "dispatch" else 1)
        assert not any(event["kind"] == "disclosed" for event in h.store.events)
        record = next(iter(h.store.requests.values()))
        assert record.status == ("completed" if phase == "disclosure" else "reserved")

    run(scenario())

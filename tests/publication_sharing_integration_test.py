"""Hosted sharing composes canonical storage, current accounts and the native engine."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from decision_flywheel.batch_storage import StorageScope
from decision_flywheel.embedded_cyclotron import ClassifierSpec, Cyclotron, CyclotronDefinition
from decision_flywheel.models import Item
from decision_flywheel.prospectus_accounts import AccountsClient, VerifiedAccountSession
from decision_flywheel.publication_lab_test import OfflineModel
from decision_flywheel.publication_records import digest
from decision_flywheel.publication_sharing import (
    DeliveryRequest,
    EngineBinding,
    GrantSpec,
    InferenceRequest,
    ResolvedDelivery,
    SharingDenied,
    SharingReconciliationRequired,
    SharingService,
    SharingUnavailable,
    SourceReference,
)
from decision_flywheel.publication_sharing_authorization import ProspectusSharingAuthorization
from decision_flywheel.publication_sharing_storage import JournalSharingStore, sharing_journal_name
from decision_flywheel.s3_journal import S3Journal
from decision_flywheel.s3_journal_test import ConditionalS3
from decision_flywheel.storage_payloads import S3Payloads

OWNER = "11111111-1111-4111-8111-111111111111"
CONSUMER = "22222222-2222-4222-8222-222222222222"
OWNER_PERSON = "33333333-3333-4333-8333-333333333333"
CONSUMER_PERSON = "44444444-4444-4444-8444-444444444444"
ENGINE = "synthetic-central"
PUBLICATION = "synthetic-consumer-publication"
OUTPUTS = ("intel", "private-sibling")
NOW = datetime(2026, 10, 20, tzinfo=timezone.utc)
DEFINITION = CyclotronDefinition(
    ENGINE,
    tuple(ClassifierSpec(output, ("accept", "reject"), "Synthetic match?") for output in OUTPUTS),
    seed="synthetic-sharing-integration",
)


class AccountTransport:
    """The consumer has no owner-account membership; central responses stay fresh."""

    def __init__(self):
        self.calls = []
        self.records = {
            "synthetic-owner-token": [OWNER, OWNER_PERSON, "owner", True],
            "synthetic-consumer-token": [CONSUMER, CONSUMER_PERSON, "viewer", True],
        }

    def __call__(self, method, path, token, body):
        assert method == "POST" and path.endswith("/authorize")
        account = path.split("/")[3]
        operation = body["operation"]
        self.calls.append((token, account, operation))
        known_account, person, role, allowed = self.records[token]
        return {
            "allowed": allowed and account == known_account and (operation == "console:read" or role != "viewer"),
            "accountId": account,
            "personId": person,
            "product": "cyclotron",
            "operation": operation,
            "revision": 1,
            "role": role,
        }


class HostedSharing:
    def __init__(self, engine, model):
        self.engine, self.model = engine, model
        self.client = ConditionalS3()
        self.transport = AccountTransport()
        self.sessions = {
            "owner": VerifiedAccountSession("synthetic-owner-token", OWNER_PERSON, OWNER, "a" * 64),
            "consumer": VerifiedAccountSession("synthetic-consumer-token", CONSUMER_PERSON, CONSUMER, "b" * 64),
        }
        self.owner_scope = True
        self.consumer_scope = True
        self.source_permission = True
        self.before_engine = None
        self.after_engine = None
        self.delivered_result = None
        self.source_checks = 0
        self.source_snapshot = {
            "id": "synthetic-item",
            "values": {"text": "Synthetic source with no real private content"},
            "occurred_at": NOW.isoformat(),
        }
        self.spec = GrantSpec(
            grant_id="synthetic-grant",
            owner_account_id=OWNER,
            consumer_account_id=CONSUMER,
            publication_id=PUBLICATION,
            engine_id=ENGINE,
            binding_revision="synthetic-binding-1",
            execution_outputs=OUTPUTS,
            visible_outputs=("intel",),
            expires_at=NOW + timedelta(hours=1),
            max_requests=3,
            billing_account_id=OWNER,
            allowed_operations=("infer", "deliver"),
        )
        self.binding = EngineBinding(OWNER, ENGINE, self.spec.binding_revision, OUTPUTS)
        self.store = self.new_store().initialize()
        self.authorization = ProspectusSharingAuthorization(
            AccountsClient(self.transport),
            resolve_session=self.resolve_session,
            authorize_owner_scope=self.check_owner_scope,
            authorize_consumer_scope=self.check_consumer_scope,
        )

    def new_store(self):
        scope = StorageScope(OWNER, "synthetic-central-workspace")
        payloads = S3Payloads(self.client, "synthetic-bucket", "111111111111", scope, max_bytes=1_000_000)
        journal = S3Journal(payloads, "publication-sharing", sharing_journal_name(ENGINE), max_bytes=1_000_000, max_commits=1_000)
        return JournalSharingStore(journal, scope=scope, engine_id=ENGINE, max_records=1_000, max_bytes=1_000_000)

    async def resolve_session(self, connection):
        return self.sessions.get(connection)

    def check_owner_scope(self, principal, spec, operation):
        if not self.owner_scope or principal.account_id != OWNER or spec != self.spec or operation not in ("issue", "revoke"):
            raise SharingDenied("synthetic exact owner scope refused")

    def check_consumer_scope(self, principal, grant, operation):
        if (
            not self.consumer_scope
            or principal.account_id != CONSUMER
            or grant.spec != self.spec
            or operation not in self.spec.allowed_operations
        ):
            raise SharingDenied("synthetic publication operation refused")

    def validate_source(self, principal, grant, execution):
        self.source_checks += 1
        if (
            not self.source_permission
            or principal.account_id != CONSUMER
            or grant.spec != self.spec
            or execution.source_list_id != "synthetic-consumer-catalog"
            or execution.source_id != self.source_snapshot["id"]
            or execution.source_revision != 1
            or execution.values != self.source_snapshot["values"]
        ):
            raise SharingDenied("synthetic catalog access refused")
        return SourceReference(
            CONSUMER, PUBLICATION, execution.source_list_id, execution.source_id, 1, digest(self.source_snapshot)
        )

    async def infer(self, execution, guard):
        if self.before_engine:
            self.before_engine()
        before = self.model.calls
        decision = await self.engine.decide(Item(execution.item_id, execution.values), execution_guard=guard)
        if self.after_engine:
            self.after_engine()
        return {
            "decisionId": decision.decision_id,
            "providerRequests": self.model.calls - before,
            "outputs": decision.to_json()["classifiers"],
        }

    def resolve_delivery(self, grant, principal, delivery_id):
        assert grant.spec == self.spec and principal.account_id == CONSUMER
        if delivery_id != "synthetic-addressed-delivery" or self.delivered_result is None:
            raise SharingDenied("synthetic delivery unavailable")
        return ResolvedDelivery(
            delivery_id=delivery_id,
            grant_id=grant.spec.grant_id,
            grant_revision=grant.revision,
            owner_account_id=OWNER,
            consumer_account_id=CONSUMER,
            publication_id=PUBLICATION,
            recipient_id=CONSUMER_PERSON,
            engine_id=ENGINE,
            binding_revision=grant.spec.binding_revision,
            execution_outputs=OUTPUTS,
            central_receipt_id="synthetic-original-receipt",
            billing_account_id=OWNER,
            source=SourceReference(
                OWNER, None, "synthetic-owner-catalog", self.source_snapshot["id"], 1, digest(self.source_snapshot)
            ),
            result=self.delivered_result,
        )

    async def service(self, actor):
        request = await self.authorization.for_request(actor)
        return SharingService(
            self.store,
            resolve_principal=request.resolve_principal,
            authorize_owner=request.authorize_owner,
            authorize_use=request.authorize_use,
            resolve_binding=lambda spec: self.binding,
            infer=self.infer,
            resolve_delivery=self.resolve_delivery,
            validate_source=self.validate_source,
            clock=lambda: NOW,
        )

    def request(self):
        return InferenceRequest(
            self.spec.grant_id,
            1,
            "synthetic-request",
            CONSUMER,
            PUBLICATION,
            "synthetic-consumer-catalog",
            self.source_snapshot["id"],
            1,
            self.source_snapshot["values"],
        )

    def reserved(self):
        with self.store.transaction() as tx:
            return tx.load_grant(self.spec.grant_id).reserved_requests


def test_native_inference_survives_host_restart_without_repeated_provider_work(tmp_path):
    async def scenario():
        model = OfflineModel()
        with Cyclotron.open(tmp_path / "native", DEFINITION, model) as engine:
            host = HostedSharing(engine, model)
            await (await host.service("owner")).issue(host.spec)
            receipt = await (await host.service("consumer")).decide(host.request())
            host.store = host.new_store().open()
            assert await (await host.service("consumer")).decide(host.request()) == receipt
            assert model.calls == 1 and host.reserved() == 1
            assert receipt["source"]["contentHash"] == digest(host.source_snapshot)
            assert receipt["source"]["accountId"] == CONSUMER
            assert set(receipt["outputs"]) == {"intel"}
            assert receipt["billing"]["accountId"] == OWNER
            assert all(
                account == CONSUMER and operation == "console:read"
                for token, account, operation in host.transport.calls
                if token == "synthetic-consumer-token"
            )

    asyncio.run(scenario())


def test_existing_decision_delivery_restarts_and_revokes_without_owner_membership(tmp_path):
    async def scenario():
        model = OfflineModel()
        with Cyclotron.open(tmp_path / "native", DEFINITION, model) as engine:
            host = HostedSharing(engine, model)
            await (await host.service("owner")).issue(host.spec)
            decision = await engine.decide(Item(host.source_snapshot["id"], host.source_snapshot["values"]))
            host.delivered_result = {
                "decisionId": decision.decision_id,
                "providerRequests": 1,
                "outputs": decision.to_json()["classifiers"],
            }
            request = DeliveryRequest(
                host.spec.grant_id, 1, "delivery-request", CONSUMER, PUBLICATION, "synthetic-addressed-delivery"
            )
            receipt = await (await host.service("consumer")).deliver(request)
            host.store = host.new_store().open()
            assert await (await host.service("consumer")).deliver(request) == receipt
            assert model.calls == 1 and host.source_checks == 0 and host.reserved() == 1
            assert receipt["source"]["accountId"] == OWNER and receipt["source"]["publicationId"] is None
            assert receipt["billing"]["providerRequests"] == 0
            assert receipt["billing"]["origin"]["providerRequests"] == 1
            assert set(receipt["outputs"]) == {"intel"}
            assert all(account == CONSUMER for token, account, _ in host.transport.calls if token == "synthetic-consumer-token")
            await (await host.service("owner")).revoke(host.spec.grant_id, expected_revision=1)
            host.store = host.new_store().open()
            with pytest.raises(SharingDenied):
                await (await host.service("consumer")).deliver(request)
            assert model.calls == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("role", ["admin", "member", "viewer"])
def test_console_membership_cannot_issue_an_owner_grant(tmp_path, role):
    async def scenario():
        model = OfflineModel()
        with Cyclotron.open(tmp_path / "native", DEFINITION, model) as engine:
            host = HostedSharing(engine, model)
            host.transport.records["synthetic-owner-token"][2] = role
            with pytest.raises(SharingDenied):
                await (await host.service("owner")).issue(host.spec)
            with host.store.transaction() as tx:
                assert tx.load_grant(host.spec.grant_id) is None
            assert model.calls == 0

    asyncio.run(scenario())


def test_membership_revoked_after_reservation_stops_native_dispatch(tmp_path):
    async def scenario():
        model = OfflineModel()
        with Cyclotron.open(tmp_path / "native", DEFINITION, model) as engine:
            host = HostedSharing(engine, model)
            await (await host.service("owner")).issue(host.spec)
            host.before_engine = lambda: host.transport.records["synthetic-consumer-token"].__setitem__(3, False)
            with pytest.raises(SharingDenied):
                await (await host.service("consumer")).decide(host.request())
            assert model.calls == 0 and host.reserved() == 1
            host.transport.records["synthetic-consumer-token"][3] = True
            host.store = host.new_store().open()
            with pytest.raises(SharingReconciliationRequired):
                await (await host.service("consumer")).decide(host.request())
            assert model.calls == 0

    asyncio.run(scenario())


def test_lost_reservation_acknowledgement_never_authorizes_native_dispatch(tmp_path):
    async def scenario():
        model = OfflineModel()
        with Cyclotron.open(tmp_path / "native", DEFINITION, model) as engine:
            host = HostedSharing(engine, model)
            await (await host.service("owner")).issue(host.spec)
            host.client.lose = lambda key: key == host.store.journal.head_key
            with pytest.raises(SharingUnavailable):
                await (await host.service("consumer")).decide(host.request())
            assert model.calls == 0
            host.store = host.new_store().open()
            assert host.reserved() == 1
            with pytest.raises(SharingReconciliationRequired):
                await (await host.service("consumer")).decide(host.request())
            assert model.calls == 0

    asyncio.run(scenario())


def test_completed_receipt_after_lost_acknowledgement_requires_fresh_access_not_another_call(tmp_path):
    async def scenario():
        model = OfflineModel()
        with Cyclotron.open(tmp_path / "native", DEFINITION, model) as engine:
            host = HostedSharing(engine, model)
            await (await host.service("owner")).issue(host.spec)
            host.after_engine = lambda: setattr(host.client, "lose", lambda key: key == host.store.journal.head_key)
            with pytest.raises(SharingUnavailable):
                await (await host.service("consumer")).decide(host.request())
            assert model.calls == 1
            host.store = host.new_store().open()
            host.consumer_scope = False
            with pytest.raises(SharingDenied):
                await (await host.service("consumer")).decide(host.request())
            host.consumer_scope = True
            receipt = await (await host.service("consumer")).decide(host.request())
            assert receipt["billing"]["providerRequests"] == 1
            assert model.calls == 1 and host.reserved() == 1

    asyncio.run(scenario())

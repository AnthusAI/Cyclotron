"""Explicit owner-granted inference across distinct SaaS accounts.

This additive application boundary does not install routes, issue credentials,
open an engine, or confer console/account administration. Its trusted ports must
be supplied by a host; no production persistence or identity adapter is provided.

``SharingStore.transaction`` must be serializable across all writers, durably
commit on successful exit, and atomically roll back every mutation on failure.
Grant/quota, immutable source identity, request reservation and audit writes share
one transaction. Commit acknowledgement loss may have an uncertain outcome: this
module never retries it. A reserved request remains consumed until an operator
reconciles it; there is deliberately no automatic reclaim or retry API.

Identity and owner-grant authorization callbacks must revalidate the current
external authority on every call. Bind them to an immutable authenticated request
context, never a shared mutable "selected account" variable. They are trusted
host functions, never request fields. Authorization callbacks return None or raise;
booleans grant no access. authorize_use receives the exact infer/deliver operation.
The inference port must invoke its supplied guard AFTER obtaining actual engine
ownership, before protected state reads/provider dispatch, and at each later
effect boundary. It must retain existing provider-budget and storage-fence guards.
The explicit execution scope may include private sibling outputs; the projection
scope does not. Grant max_requests counts logical reservations, not provider calls
or money. Only owner-pays attribution is supported; no payment is initiated here.

resolve_delivery must resolve an immutable, explicitly addressed delivery by its
opaque ID, pinned to grant, principal and consumer publication. It must not expose
general owner history lookup. The infer/resolve_delivery adapters validate native
prediction integrity, classifier vocabulary and versions; shape filtering alone
cannot sanitize arbitrary provider text disguised as a classification label.
Delivery uses the original central decision without invoking inference. Its origin
usage records prior attribution; current providerRequests is zero, not a new charge.

validate_source(principal, grant, execution) must verify source access and compare
the frozen values to the canonical catalog revision, returning its SourceReference
with the native hash. It runs before reservation, at guarded inference dispatch,
and before result disclosure. A caller cannot reserve an invented source identity
or replace the native hash with a hash of its own unverified values.

Receipts are private data, not bearer capabilities. A transport must authenticate
them and receiving hosts must freshly validate audience, grant and source scope.
Revocation cannot cancel an already dispatched provider call.
"""

import inspect
import json
import math
import re
import secrets
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from hashlib import sha256
from typing import Protocol
from uuid import uuid4

CONTRACT = "cyclotron-sharing/v1"
MAX_SOURCE_BYTES = 65_536


class SharingError(RuntimeError):
    pass


class SharingDenied(SharingError):
    pass


class SharingConflict(SharingError):
    pass


class SharingUnavailable(SharingError):
    pass


class SharingReconciliationRequired(SharingError):
    pass


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", value):
        raise ValueError("an exact bounded identity is required")
    return value


def _positive(value):
    if type(value) is not int or value < 1:
        raise ValueError("a positive integer is required")
    return value


def _time(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("an explicit timezone is required")
    return value.astimezone(timezone.utc)


def _outputs(value):
    if not isinstance(value, (tuple, list)) or not value or len(value) > 100 or len(set(value)) != len(value):
        raise ValueError("distinct explicit output identities are required")
    return tuple(_identifier(output) for output in value)


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class VerifiedPrincipal:
    """Returned only by a fresh host verifier, never deserialized from a body."""

    account_id: str
    subject_id: str

    def __post_init__(self):
        _identifier(self.account_id)
        _identifier(self.subject_id)


@dataclass(frozen=True)
class GrantSpec:
    grant_id: str
    owner_account_id: str
    consumer_account_id: str
    publication_id: str
    engine_id: str
    binding_revision: str
    execution_outputs: tuple[str, ...]
    visible_outputs: tuple[str, ...]
    expires_at: datetime
    max_requests: int
    billing_account_id: str
    allowed_operations: tuple[str, ...]

    def __post_init__(self):
        for value in (
            self.grant_id,
            self.owner_account_id,
            self.consumer_account_id,
            self.publication_id,
            self.engine_id,
            self.binding_revision,
            self.billing_account_id,
        ):
            _identifier(value)
        object.__setattr__(self, "execution_outputs", _outputs(self.execution_outputs))
        object.__setattr__(self, "visible_outputs", _outputs(self.visible_outputs))
        object.__setattr__(self, "expires_at", _time(self.expires_at))
        _positive(self.max_requests)
        if (
            not isinstance(self.allowed_operations, (tuple, list))
            or not self.allowed_operations
            or len(set(self.allowed_operations)) != len(self.allowed_operations)
            or not set(self.allowed_operations) <= {"infer", "deliver"}
        ):
            raise ValueError("explicit inference and/or addressed-delivery operations are required")
        object.__setattr__(self, "allowed_operations", tuple(self.allowed_operations))
        if self.owner_account_id == self.consumer_account_id:
            raise ValueError("a sharing grant requires distinct SaaS accounts")
        if self.billing_account_id != self.owner_account_id:
            raise ValueError("only explicit owner-pays attribution is supported")
        if not set(self.visible_outputs) <= set(self.execution_outputs):
            raise ValueError("visible outputs must be within the exact execution scope")


@dataclass(frozen=True)
class SharingGrant:
    spec: GrantSpec
    issued_by: str
    revision: int = 1
    status: str = "active"
    reserved_requests: int = 0

    def __post_init__(self):
        if type(self.spec) is not GrantSpec:
            raise ValueError("validated grant specification required")
        _identifier(self.issued_by)
        _positive(self.revision)
        if self.status not in ("active", "revoked"):
            raise ValueError("invalid grant state")
        if type(self.reserved_requests) is not int or not 0 <= self.reserved_requests <= self.spec.max_requests:
            raise ValueError("invalid reservation count")


@dataclass(frozen=True)
class EngineBinding:
    owner_account_id: str
    engine_id: str
    revision: str
    outputs: tuple[str, ...]

    def __post_init__(self):
        for value in (self.owner_account_id, self.engine_id, self.revision):
            _identifier(value)
        object.__setattr__(self, "outputs", _outputs(self.outputs))


@dataclass(frozen=True)
class InferenceRequest:
    grant_id: str
    grant_revision: int
    request_id: str
    consumer_account_id: str
    publication_id: str
    source_list_id: str
    source_id: str
    source_revision: int
    values: dict = field(repr=False)

    def __post_init__(self):
        for value in (
            self.grant_id,
            self.request_id,
            self.consumer_account_id,
            self.publication_id,
            self.source_list_id,
            self.source_id,
        ):
            _identifier(value)
        _positive(self.grant_revision)
        _positive(self.source_revision)


@dataclass(frozen=True)
class DeliveryRequest:
    grant_id: str
    grant_revision: int
    request_id: str
    consumer_account_id: str
    publication_id: str
    delivery_id: str

    def __post_init__(self):
        for value in (self.grant_id, self.request_id, self.consumer_account_id, self.publication_id, self.delivery_id):
            _identifier(value)
        _positive(self.grant_revision)


@dataclass(frozen=True)
class SourceReference:
    account_id: str
    publication_id: str | None
    list_id: str
    source_id: str
    revision: int
    content_hash: str

    def __post_init__(self):
        for value in (self.account_id, self.list_id, self.source_id):
            _identifier(value)
        _positive(self.revision)
        if self.publication_id is not None:
            _identifier(self.publication_id)
        if not isinstance(self.content_hash, str) or not re.fullmatch("[0-9a-f]{64}", self.content_hash):
            raise ValueError("an immutable source content hash is required")

    def to_json(self):
        return {
            "accountId": self.account_id,
            "publicationId": self.publication_id,
            "listId": self.list_id,
            "id": self.source_id,
            "revision": self.revision,
            "contentHash": self.content_hash,
        }


@dataclass(frozen=True)
class ResolvedDelivery:
    """Trusted resolver output; never accepted from a consumer request body."""

    delivery_id: str
    grant_id: str
    grant_revision: int
    owner_account_id: str
    consumer_account_id: str
    publication_id: str
    recipient_id: str
    engine_id: str
    binding_revision: str
    execution_outputs: tuple[str, ...]
    central_receipt_id: str
    billing_account_id: str
    source: SourceReference
    result: dict = field(repr=False)

    def __post_init__(self):
        for value in (
            self.delivery_id,
            self.grant_id,
            self.owner_account_id,
            self.consumer_account_id,
            self.publication_id,
            self.recipient_id,
            self.engine_id,
            self.binding_revision,
            self.central_receipt_id,
            self.billing_account_id,
        ):
            _identifier(value)
        _positive(self.grant_revision)
        object.__setattr__(self, "execution_outputs", _outputs(self.execution_outputs))
        if type(self.source) is not SourceReference or not isinstance(self.result, dict):
            raise ValueError("verified immutable source and native result are required")


@dataclass(frozen=True)
class Execution:
    """Trusted engine input; ``values`` returns a fresh copy of frozen caller data."""

    owner_account_id: str
    consumer_account_id: str
    publication_id: str
    engine_id: str
    binding_revision: str
    execution_outputs: tuple[str, ...]
    visible_outputs: tuple[str, ...]
    grant_id: str
    grant_revision: int
    request_id: str
    requester_id: str
    billing_account_id: str
    source_account_id: str
    source_publication_id: str | None
    source_list_id: str
    source_id: str
    source_revision: int
    content_hash: str
    values_json: str = field(repr=False)

    @property
    def source_key(self):
        return self.source_account_id, self.source_list_id, self.source_id, self.source_revision

    @property
    def item_id(self):
        return "shared-" + sha256(_encoded(self.source_key).encode()).hexdigest()

    @property
    def values(self):
        return json.loads(self.values_json)

    @property
    def source(self):
        return SourceReference(
            self.source_account_id,
            self.source_publication_id,
            self.source_list_id,
            self.source_id,
            self.source_revision,
            self.content_hash,
        ).to_json()


@dataclass(frozen=True)
class RequestRecord:
    key: tuple[str, str, str, str]
    fingerprint: str
    grant_id: str
    grant_revision: int
    claim: str = field(repr=False)
    status: str = "reserved"
    receipt_json: str | None = field(default=None, repr=False)


class SharingTransaction(Protocol):
    """All methods operate in one durable, serializable, rollback-safe transaction."""

    def load_grant(self, identifier: str) -> SharingGrant | None: ...
    def save_grant(self, grant: SharingGrant) -> None: ...
    def load_request(self, key: tuple[str, str, str, str]) -> RequestRecord | None: ...
    def save_request(self, record: RequestRecord) -> None: ...
    def load_source(self, key: tuple[str, str, str, int]) -> str | None: ...
    def save_source(self, key: tuple[str, str, str, int], content_hash: str) -> None: ...
    def append_audit(self, event: dict) -> None: ...


class SharingStore(Protocol):
    def transaction(self) -> AbstractContextManager[SharingTransaction]: ...


class SharingService:
    def __init__(
        self,
        store: SharingStore,
        *,
        resolve_principal,
        authorize_owner,
        authorize_use,
        resolve_binding,
        infer,
        resolve_delivery,
        validate_source,
        clock,
    ):
        for callback in (
            resolve_principal,
            authorize_owner,
            authorize_use,
            resolve_binding,
            infer,
            resolve_delivery,
            validate_source,
            clock,
        ):
            if not callable(callback):
                raise TypeError("explicit trusted sharing ports are required")
        self.store, self.resolve_principal, self.authorize_owner = store, resolve_principal, authorize_owner
        self.authorize_use, self.resolve_binding, self.infer, self.clock = authorize_use, resolve_binding, infer, clock
        self.resolve_delivery = resolve_delivery
        self.validate_source = validate_source

    @contextmanager
    def _transaction(self):
        try:
            with self.store.transaction() as transaction:
                yield transaction
        except SharingError:
            raise
        except Exception:
            raise SharingUnavailable("sharing transaction outcome is unavailable; do not retry blindly") from None

    async def _call(self, callback, *args):
        try:
            value = callback(*args)
            return await value if inspect.isawaitable(value) else value
        except SharingError:
            raise
        except Exception:
            raise SharingUnavailable("fresh sharing authorization is unavailable") from None

    async def _principal(self, expected=None):
        principal = await self._call(self.resolve_principal)
        if type(principal) is not VerifiedPrincipal or expected is not None and principal != expected:
            raise SharingDenied("verified sharing principal changed or is unavailable")
        return principal

    async def _permission(self, callback, *args):
        if await self._call(callback, *args) is not None:
            raise SharingDenied("explicit sharing permission was not established")

    async def _binding(self, spec):
        binding = await self._call(self.resolve_binding, spec)
        expected = EngineBinding(spec.owner_account_id, spec.engine_id, spec.binding_revision, spec.execution_outputs)
        if type(binding) is not EngineBinding or binding != expected:
            raise SharingDenied("owner engine execution binding changed")

    def _now(self):
        try:
            return _time(self.clock())
        except Exception:
            raise SharingUnavailable("sharing time authority is unavailable") from None

    def _audit(self, kind, grant, actor, *, execution=None, receipt=None):
        spec = grant.spec
        event = {
            "kind": kind,
            "at": self._now().isoformat(),
            "grantId": spec.grant_id,
            "grantRevision": grant.revision,
            "ownerAccountId": spec.owner_account_id,
            "consumerAccountId": spec.consumer_account_id,
            "publicationId": spec.publication_id,
            "engineId": spec.engine_id,
            "bindingRevision": spec.binding_revision,
            "actorAccountId": actor.account_id,
            "actorId": actor.subject_id,
            "billingAccountId": spec.billing_account_id,
            "billingPolicy": "owner-pays",
        }
        if execution:
            event.update(requestId=execution.request_id, source=execution.source)
        if receipt:
            event.update(
                receiptId=receipt["receiptId"],
                decisionId=receipt["decisionId"],
                providerRequests=receipt["billing"]["providerRequests"],
            )
            if "delivery" in receipt:
                event.update(delivery=receipt["delivery"], originUsage=receipt["billing"]["origin"])
        return event

    async def issue(self, spec):
        if type(spec) is not GrantSpec:
            raise ValueError("validated grant specification required")
        principal = await self._principal()
        if principal.account_id != spec.owner_account_id:
            raise SharingDenied("the owning account must authorize its grant")
        await self._permission(self.authorize_owner, principal, spec, "issue")
        await self._binding(spec)
        await self._principal(principal)
        await self._permission(self.authorize_owner, principal, spec, "issue")
        with self._transaction() as tx:
            if tx.load_grant(spec.grant_id) is not None:
                raise SharingConflict("grant identity already exists")
            if spec.expires_at <= self._now():
                raise SharingDenied("grant must expire in the future")
            grant = SharingGrant(spec, principal.subject_id)
            tx.save_grant(grant)
            tx.append_audit(self._audit("issued", grant, principal))
        return grant

    async def revoke(self, grant_id, *, expected_revision):
        _identifier(grant_id)
        _positive(expected_revision)
        principal = await self._principal()
        with self._transaction() as tx:
            grant = tx.load_grant(grant_id)
        if type(grant) is not SharingGrant or principal.account_id != grant.spec.owner_account_id:
            raise SharingDenied("owner grant authority required")
        await self._permission(self.authorize_owner, principal, grant.spec, "revoke")
        await self._principal(principal)
        await self._permission(self.authorize_owner, principal, grant.spec, "revoke")
        with self._transaction() as tx:
            current = tx.load_grant(grant_id)
            if type(current) is not SharingGrant or (current.spec, current.revision, current.status, current.issued_by) != (
                grant.spec,
                expected_revision,
                "active",
                grant.issued_by,
            ):
                raise SharingConflict("grant revision changed")
            revoked = replace(current, status="revoked", revision=current.revision + 1)
            tx.save_grant(revoked)
            tx.append_audit(self._audit("revoked", revoked, principal))
        return revoked

    def _grant(self, tx, request, expected=None, *, operation="infer"):
        grant = tx.load_grant(request.grant_id)
        if (
            type(grant) is not SharingGrant
            or grant.status != "active"
            or grant.spec.expires_at <= self._now()
            or grant.revision != request.grant_revision
            or grant.spec.consumer_account_id != request.consumer_account_id
            or grant.spec.publication_id != request.publication_id
            or operation not in grant.spec.allowed_operations
            or expected is not None
            and (grant.spec, grant.revision) != (expected.spec, expected.revision)
        ):
            raise SharingDenied("active owner grant does not match this request")
        return grant

    async def _fresh(self, request, principal, expected=None, *, operation="infer"):
        await self._principal(principal)
        with self._transaction() as tx:
            grant = self._grant(tx, request, expected, operation=operation)
        await self._permission(self.authorize_use, principal, grant, operation)
        await self._binding(grant.spec)
        with self._transaction() as tx:
            return self._grant(tx, request, grant, operation=operation)

    async def _source(self, execution, principal, grant, *, pinned=False):
        source = await self._call(self.validate_source, principal, grant, execution)
        if (
            type(source) is not SourceReference
            or (source.account_id, source.list_id, source.source_id, source.revision) != execution.source_key
        ):
            raise SharingDenied("verified source scope does not match the inference request")
        if pinned and source.to_json() != execution.source:
            raise SharingDenied("verified source revision or provenance changed")
        return replace(execution, source_publication_id=source.publication_id, content_hash=source.content_hash)

    @staticmethod
    def _record(tx, record):
        current = tx.load_request(record.key)
        if current != record:
            raise SharingReconciliationRequired("request ownership or outcome changed")
        return current

    async def decide(self, request):
        if type(request) is not InferenceRequest or not isinstance(request.values, dict):
            raise ValueError("a structured inference request is required")
        values = _encoded(request.values)
        if len(values.encode()) > MAX_SOURCE_BYTES:
            raise ValueError("source exceeds sharing request limit")
        principal = await self._principal()
        if principal.account_id != request.consumer_account_id:
            raise SharingDenied("consumer account pin does not match verified identity")
        grant = await self._fresh(request, principal)
        spec = grant.spec
        execution = Execution(
            spec.owner_account_id,
            principal.account_id,
            spec.publication_id,
            spec.engine_id,
            spec.binding_revision,
            spec.execution_outputs,
            spec.visible_outputs,
            spec.grant_id,
            grant.revision,
            request.request_id,
            principal.subject_id,
            spec.billing_account_id,
            principal.account_id,
            spec.publication_id,
            request.source_list_id,
            request.source_id,
            request.source_revision,
            sha256(values.encode()).hexdigest(),
            values,
        )
        execution = await self._source(execution, principal, grant)
        await self._fresh(request, principal, grant)
        key = principal.account_id, spec.publication_id, principal.subject_id, request.request_id
        fingerprint = sha256(
            _encoded(
                [
                    "infer",
                    spec.grant_id,
                    grant.revision,
                    spec.engine_id,
                    spec.binding_revision,
                    execution.source,
                    sha256(values.encode()).hexdigest(),
                ]
            ).encode()
        ).hexdigest()
        with self._transaction() as tx:
            current = self._grant(tx, request, grant)
            record = tx.load_request(key)
            if record is not None:
                if type(record) is not RequestRecord or record.fingerprint != fingerprint:
                    raise SharingConflict("request identity is already bound to different input")
                if record.status != "completed" or record.receipt_json is None:
                    raise SharingReconciliationRequired("reserved inference needs reconciliation, not another dispatch")
            else:
                if current.reserved_requests >= spec.max_requests:
                    raise SharingDenied("owner inference reservation quota exhausted")
                known = tx.load_source(execution.source_key)
                if known is not None and known != execution.content_hash:
                    raise SharingConflict("source revision is already bound to different content")
                record = RequestRecord(key, fingerprint, spec.grant_id, grant.revision, secrets.token_hex(32))
                tx.save_source(execution.source_key, execution.content_hash)
                tx.save_grant(replace(current, reserved_requests=current.reserved_requests + 1))
                tx.save_request(record)
                tx.append_audit(self._audit("reserved", current, principal, execution=execution))
        if record.status == "completed":
            return await self._disclose(request, principal, grant, execution, record)

        guard_calls = 0

        async def guard():
            nonlocal guard_calls
            await self._fresh(request, principal, grant)
            await self._source(execution, principal, grant, pinned=True)
            await self._fresh(request, principal, grant)
            with self._transaction() as tx:
                self._grant(tx, request, grant)
                self._record(tx, record)
            guard_calls += 1

        try:
            raw = self.infer(execution, guard)
            raw = await raw if inspect.isawaitable(raw) else raw
            if not guard_calls:
                raise ValueError("engine did not use the execution guard")
            receipt = self._receipt(raw, execution)
        except SharingError:
            raise
        except Exception:
            raise SharingReconciliationRequired("inference outcome requires reconciliation") from None
        await self._fresh(request, principal, grant)
        await self._source(execution, principal, grant, pinned=True)
        await self._fresh(request, principal, grant)
        with self._transaction() as tx:
            current = self._grant(tx, request, grant)
            self._record(tx, record)
            completed = replace(record, status="completed", receipt_json=_encoded(receipt))
            tx.save_request(completed)
            tx.append_audit(self._audit("completed", current, principal, execution=execution, receipt=receipt))
        return await self._disclose(request, principal, grant, execution, completed)

    async def deliver(self, request):
        """Project one existing, explicitly addressed central decision; no inference."""
        if type(request) is not DeliveryRequest:
            raise ValueError("an addressed delivery request is required")
        principal = await self._principal()
        if principal.account_id != request.consumer_account_id:
            raise SharingDenied("consumer account pin does not match verified identity")
        grant = await self._fresh(request, principal, operation="deliver")
        spec = grant.spec
        key = principal.account_id, spec.publication_id, principal.subject_id, request.request_id
        fingerprint = sha256(
            _encoded(
                [
                    "deliver",
                    spec.grant_id,
                    grant.revision,
                    spec.engine_id,
                    spec.binding_revision,
                    request.delivery_id,
                ]
            ).encode()
        ).hexdigest()
        with self._transaction() as tx:
            current = self._grant(tx, request, grant, operation="deliver")
            record = tx.load_request(key)
            if record is not None:
                if type(record) is not RequestRecord or record.fingerprint != fingerprint:
                    raise SharingConflict("request identity is already bound to different input")
                if record.status != "completed" or record.receipt_json is None:
                    raise SharingReconciliationRequired("reserved delivery needs reconciliation")
            else:
                if current.reserved_requests >= spec.max_requests:
                    raise SharingDenied("owner service reservation quota exhausted")
                record = RequestRecord(key, fingerprint, spec.grant_id, grant.revision, secrets.token_hex(32))
                tx.save_grant(replace(current, reserved_requests=current.reserved_requests + 1))
                tx.save_request(record)
                event = self._audit("delivery_reserved", current, principal)
                event.update(requestId=request.request_id, deliveryId=request.delivery_id)
                tx.append_audit(event)
        if record.status != "completed":
            execution, receipt = await self._resolved_delivery(request, principal, grant)
            await self._fresh(request, principal, grant, operation="deliver")
            with self._transaction() as tx:
                current = self._grant(tx, request, grant, operation="deliver")
                self._record(tx, record)
                record = replace(record, status="completed", receipt_json=_encoded(receipt))
                tx.save_request(record)
                tx.append_audit(self._audit("delivered", current, principal, execution=execution, receipt=receipt))
        # Re-resolve the addressed delivery even for completed requests. An earlier
        # receipt is not authority after delivery entitlement is withdrawn.
        execution, candidate = await self._resolved_delivery(request, principal, grant)
        saved = json.loads(record.receipt_json)
        candidate["receiptId"] = saved["receiptId"]
        if candidate != saved:
            raise SharingConflict("original central delivery provenance changed")
        await self._fresh(request, principal, grant, operation="deliver")
        with self._transaction() as tx:
            current = self._grant(tx, request, grant, operation="deliver")
            self._record(tx, record)
            tx.append_audit(self._audit("disclosed", current, principal, execution=execution, receipt=saved))
        return saved

    async def _resolved_delivery(self, request, principal, grant):
        await self._fresh(request, principal, grant, operation="deliver")
        delivery = await self._call(self.resolve_delivery, grant, principal, request.delivery_id)
        spec = grant.spec
        if type(delivery) is not ResolvedDelivery or (
            delivery.delivery_id,
            delivery.grant_id,
            delivery.grant_revision,
            delivery.owner_account_id,
            delivery.consumer_account_id,
            delivery.publication_id,
            delivery.recipient_id,
            delivery.engine_id,
            delivery.binding_revision,
            delivery.execution_outputs,
            delivery.billing_account_id,
        ) != (
            request.delivery_id,
            spec.grant_id,
            grant.revision,
            spec.owner_account_id,
            principal.account_id,
            spec.publication_id,
            principal.subject_id,
            spec.engine_id,
            spec.binding_revision,
            spec.execution_outputs,
            spec.billing_account_id,
        ):
            raise SharingDenied("delivery is not addressed to this grant, consumer and publication")
        source = delivery.source
        execution = Execution(
            spec.owner_account_id,
            principal.account_id,
            spec.publication_id,
            spec.engine_id,
            spec.binding_revision,
            spec.execution_outputs,
            spec.visible_outputs,
            spec.grant_id,
            grant.revision,
            request.request_id,
            principal.subject_id,
            spec.billing_account_id,
            source.account_id,
            source.publication_id,
            source.list_id,
            source.source_id,
            source.revision,
            source.content_hash,
            "{}",
        )
        try:
            receipt = self._receipt(delivery.result, execution)
        except Exception:
            raise SharingReconciliationRequired("native delivery result could not be verified") from None
        original_requests = receipt["billing"]["providerRequests"]
        receipt["delivery"] = {"id": delivery.delivery_id, "centralReceiptId": delivery.central_receipt_id}
        receipt["billing"].update(
            providerRequests=0,
            origin={"receiptId": delivery.central_receipt_id, "providerRequests": original_requests},
        )
        return execution, receipt

    async def _disclose(self, request, principal, grant, execution, record):
        await self._fresh(request, principal, grant)
        await self._source(execution, principal, grant, pinned=True)
        await self._fresh(request, principal, grant)
        with self._transaction() as tx:
            current = self._grant(tx, request, grant)
            self._record(tx, record)
            receipt = json.loads(record.receipt_json)
            tx.append_audit(self._audit("disclosed", current, principal, execution=execution, receipt=receipt))
        return receipt

    @staticmethod
    def _receipt(raw, execution):
        if not isinstance(raw, dict) or not isinstance(raw.get("outputs"), dict):
            raise ValueError("structured engine result required")
        decision_id = _identifier(raw.get("decisionId"))
        count = raw.get("providerRequests")
        if type(count) is not int or count < 0:
            raise ValueError("measured provider request attribution required")
        outputs = {}
        for output in execution.visible_outputs:
            value = raw["outputs"].get(output)
            if not isinstance(value, dict):
                raise ValueError("authorized output result missing")
            label, version, confidence = value.get("label"), value.get("version"), value.get("confidence")
            if not isinstance(label, str) or not label.strip() or len(label) > 200:
                raise ValueError("bounded classification label required")
            _positive(version)
            if confidence is not None and (
                type(confidence) not in (float, int) or not math.isfinite(confidence) or not 0 <= confidence <= 1
            ):
                raise ValueError("invalid classification confidence")
            outputs[output] = {"label": label, "confidence": confidence, "version": version}
        return {
            "contractVersion": CONTRACT,
            "receiptId": str(uuid4()),
            "grantId": execution.grant_id,
            "grantRevision": execution.grant_revision,
            "ownerAccountId": execution.owner_account_id,
            "consumerAccountId": execution.consumer_account_id,
            "publicationId": execution.publication_id,
            "engineId": execution.engine_id,
            "bindingRevision": execution.binding_revision,
            "requestId": execution.request_id,
            "decisionId": decision_id,
            "source": execution.source,
            "outputs": outputs,
            "billing": {
                "accountId": execution.billing_account_id,
                "policy": "owner-pays",
                "reservedRequests": 1,
                "providerRequests": count,
            },
        }

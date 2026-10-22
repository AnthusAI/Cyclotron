"""Bounded, owner/engine-scoped sharing authority on the existing canonical journal.

The host injects an already scoped S3Journal (or LocalFileJournal for local use).
Construction performs no I/O. Explicit initialize/open precedes use; a missing
head never creates a replacement ledger. Each transaction reads fresh authority,
copies it, validates the whole result, and publishes once with compare-and-swap.
No yielded body, unknown write, or failed dispatch claim is automatically retried.

One document contains grants, reservations, source pins and append-only audit.
There is no account-wide discovery or public audit endpoint. Receipts and claims
remain private. Byte/record/commit budgets retain headroom to revoke every active
grant. Reaching the ordinary admission limit never prunes evidence or starts a
new budget. Storage unavailability can still prevent revocation.

A lost write acknowledgement permanently poisons this handle, including cleanup.
A fresh instance may inspect the committed ledger, but a reserved request remains
reserved: observing it never grants a new inference dispatch.
"""

import math
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, fields
from datetime import datetime
from hashlib import sha256

from .batch_storage import StorageScope
from .journal import Journal, commit_with_contract, decode, encode
from .publication_sharing import (
    CONTRACT,
    GrantSpec,
    RequestRecord,
    SharingConflict,
    SharingGrant,
    SharingUnavailable,
    SourceReference,
)
from .storage_errors import StorageConflict, StorageUncertain

SCHEMA = "cyclotron-publication-sharing-ledger/v1"
DOMAIN = "publication-sharing"
# A revoke event has fewer than 16 bounded identity fields (<=200 bytes each),
# a <=40-byte timestamp, fixed field names and small ordinals. 8192 also covers
# the grant's active->revoked/revision growth. Reserve separately for EVERY grant.
REVOCATION_BYTES = 8192
# The canonical journal's bounded namespace, UUIDs, parent digest and ordinals.
JOURNAL_ENVELOPE_BYTES = 1024
BASE_AUDIT = {
    "kind",
    "at",
    "grantId",
    "grantRevision",
    "ownerAccountId",
    "consumerAccountId",
    "publicationId",
    "engineId",
    "bindingRevision",
    "actorAccountId",
    "actorId",
    "billingAccountId",
    "billingPolicy",
}
RECEIPT_FIELDS = {
    "contractVersion",
    "receiptId",
    "grantId",
    "grantRevision",
    "ownerAccountId",
    "consumerAccountId",
    "publicationId",
    "engineId",
    "bindingRevision",
    "requestId",
    "decisionId",
    "source",
    "outputs",
    "billing",
}


def _text(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", value):
        raise ValueError("bounded exact identity required")
    return value


def _hash(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError("complete SHA256 required")
    return value


def _integer(value, minimum=1):
    if type(value) is not int or not minimum <= value < 2**63:
        raise ValueError("bounded integer required")
    return value


def _object(value, names):
    if not isinstance(value, dict) or set(value) != set(names):
        raise ValueError("complete canonical record required")
    return value


def _copy(value):
    return decode(encode(value))


def _source(value):
    _object(value, {"accountId", "publicationId", "listId", "id", "revision", "contentHash"})
    return SourceReference(
        value["accountId"], value["publicationId"], value["listId"], value["id"], value["revision"], value["contentHash"]
    )


def _source_key(value):
    return value.account_id, value.list_id, value.source_id, value.revision


def sharing_journal_name(engine_id):
    """Stable engine namespace; raw engine identities never become storage paths."""
    return sha256(_text(engine_id).encode()).hexdigest()


def _grant_json(grant):
    value = asdict(grant)
    value["spec"]["expires_at"] = grant.spec.expires_at.isoformat()
    return _copy(value)


def _grant(value):
    _object(value, {f.name for f in fields(SharingGrant)})
    spec = dict(_object(value["spec"], {f.name for f in fields(GrantSpec)}))
    if not isinstance(spec["expires_at"], str) or len(spec["expires_at"]) > 40:
        raise ValueError("canonical grant expiry required")
    spec["expires_at"] = datetime.fromisoformat(spec["expires_at"])
    result = SharingGrant(**{**value, "spec": GrantSpec(**spec)})
    if result.revision != (1 if result.status == "active" else 2):
        raise ValueError("only initial active and terminal revoked revisions are supported")
    return result


def _spec_hash(grant):
    return sha256(encode(_grant_json(grant)["spec"])).hexdigest()


def _request(value):
    _object(value, {f.name for f in fields(RequestRecord)})
    key = value["key"]
    if not isinstance(key, list) or len(key) != 4:
        raise ValueError("complete consumer/publication/actor/request key required")
    for part in key:
        _text(part)
    _hash(value["fingerprint"])
    _hash(value["claim"])
    _text(value["grant_id"])
    if value["grant_revision"] != 1 or type(value["grant_revision"]) is not int:
        raise ValueError("request must retain original active grant revision")
    if value["status"] not in ("reserved", "completed"):
        raise ValueError("invalid sharing request state")
    if (value["receipt_json"] is None) != (value["status"] == "reserved"):
        raise ValueError("complete receipt required only for completed requests")
    if value["receipt_json"] is not None and not isinstance(value["receipt_json"], str):
        raise ValueError("encoded private receipt required")
    return RequestRecord(**{**value, "key": tuple(key)})


def _receipt(record, grant, reservation):
    value = decode(record.receipt_json)
    delivered = reservation["kind"] == "delivery_reserved"
    _object(value, RECEIPT_FIELDS | ({"delivery"} if delivered else set()))
    spec = grant.spec
    pins = {
        "contractVersion": CONTRACT,
        "grantId": spec.grant_id,
        "grantRevision": record.grant_revision,
        "ownerAccountId": spec.owner_account_id,
        "consumerAccountId": record.key[0],
        "publicationId": record.key[1],
        "engineId": spec.engine_id,
        "bindingRevision": spec.binding_revision,
        "requestId": record.key[3],
    }
    if any(value[k] != v for k, v in pins.items()) or type(value["grantRevision"]) is not int:
        raise ValueError("private receipt scope differs from reserved request")
    for key in ("receiptId", "decisionId"):
        _text(value[key])
    source = _source(value["source"])
    _object(value["outputs"], spec.visible_outputs)
    for output in value["outputs"].values():
        _object(output, {"label", "confidence", "version"})
        if not isinstance(output["label"], str) or not output["label"].strip() or len(output["label"]) > 200:
            raise ValueError("bounded prediction label required")
        _integer(output["version"])
        confidence = output["confidence"]
        if confidence is not None and (
            type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1
        ):
            raise ValueError("valid prediction confidence required")
    billing = _object(
        value["billing"],
        {"accountId", "policy", "reservedRequests", "providerRequests"} | ({"origin"} if delivered else set()),
    )
    if (
        billing["accountId"] != spec.billing_account_id
        or billing["policy"] != "owner-pays"
        or type(billing["reservedRequests"]) is not int
        or billing["reservedRequests"] != 1
    ):
        raise ValueError("owner billing attribution required")
    _integer(billing["providerRequests"], 0)
    if delivered:
        delivery = _object(value["delivery"], {"id", "centralReceiptId"})
        origin = _object(billing["origin"], {"receiptId", "providerRequests"})
        if (
            delivery["id"] != reservation["deliveryId"]
            or delivery["centralReceiptId"] != origin["receiptId"]
            or billing["providerRequests"] != 0
        ):
            raise ValueError("addressed delivery retains original billing attribution")
        _text(origin["receiptId"])
        _integer(origin["providerRequests"], 0)
    elif source.to_json() != reservation["source"]:
        raise ValueError("inference source changed after reservation")
    return value


class JournalSharingStore:
    def __init__(self, journal, *, scope, engine_id, max_records, max_bytes):
        if not isinstance(journal, Journal) or type(scope) is not StorageScope:
            raise ValueError("canonical journal and fixed owner storage scope required")
        expected = f"{scope.key}/{DOMAIN}/{sharing_journal_name(engine_id)}"
        if (
            not isinstance(journal.boundary, tuple)
            or not journal.boundary
            or journal.boundary[0] != scope
            or journal.namespace != expected
        ):
            raise ValueError("journal must be pinned to the owning account and exact engine")
        _integer(max_records)
        _integer(max_bytes)
        _integer(getattr(journal, "max_commits", None))
        if max_bytes > journal.max_bytes:
            raise ValueError("sharing bytes cannot exceed the canonical journal budget")
        self.journal, self.scope, self.engine_id = journal, scope, engine_id
        self.max_records, self.max_bytes = max_records, max_bytes
        self._opened = self._uncertain = False
        self._active = ContextVar("sharing_transaction", default=False)

    def _available(self):
        if self._uncertain:
            raise SharingUnavailable("uncertain sharing handle; construct a fresh handle for explicit reconciliation")

    def _empty(self):
        return {
            "schema": SCHEMA,
            "ownerAccountId": self.scope.account,
            "engineId": self.engine_id,
            "grants": [],
            "requests": [],
            "sources": [],
            "audit": [],
        }

    def _poison(self):
        self._uncertain = True

    def _read(self):
        self._available()
        try:
            record = self.journal.read()
            self._validate(record.value, record.revision)
            return record
        except Exception:
            raise SharingUnavailable("canonical sharing authority is unavailable") from None

    def initialize(self):
        self._available()
        try:
            empty = self._empty()
            self._validate(empty, 1)
            record = self.journal.initialize(empty)
            self._validate(record.value, record.revision)
        except StorageUncertain:
            self._poison()
            raise SharingUnavailable("sharing initialization outcome requires reconciliation") from None
        except Exception:
            raise SharingUnavailable("sharing initialization unavailable; never reset an existing ledger") from None
        self._opened = True
        return self

    def open(self):
        self._read()
        self._opened = True
        return self

    @contextmanager
    def transaction(self):
        self._available()
        if not self._opened:
            raise SharingUnavailable("explicit sharing ledger initialize or open required")
        if self._active.get():
            raise SharingConflict("nested sharing transactions are refused")
        record = self._read()
        tx = _Transaction(_copy(record.value))
        token = self._active.set(True)
        try:
            yield tx
            tx._check()
            if encode(tx._value) != encode(record.value):
                self._transitions(record.value, tx._value)
                self._validate(tx._value, record.revision + 1)
                try:
                    commit_with_contract(self.journal, record, tx._value, invalidate=self._poison)
                except StorageConflict:
                    raise SharingConflict("sharing authority changed; transaction was not committed") from None
                except Exception:
                    self._poison()
                    raise SharingUnavailable("sharing commit outcome requires reconciliation") from None
        except (SharingConflict, SharingUnavailable):
            raise
        finally:
            tx._closed = True
            tx._value = None
            self._active.reset(token)

    def _validate(self, value, revision):
        _object(value, self._empty())
        if any(value[k] != self._empty()[k] for k in ("schema", "ownerAccountId", "engineId")):
            raise ValueError("canonical sharing owner or engine differs")
        for name in ("grants", "requests", "sources", "audit"):
            if not isinstance(value[name], list):
                raise ValueError("complete ordered sharing records required")
        grants = {}
        for raw in value["grants"]:
            grant = _grant(raw)
            if grant.spec.owner_account_id != self.scope.account or grant.spec.engine_id != self.engine_id:
                raise ValueError("grant belongs to a different owner or engine")
            if grant.spec.grant_id in grants:
                raise ValueError("duplicate grant")
            grants[grant.spec.grant_id] = grant
        requests = {}
        for raw in value["requests"]:
            record = _request(raw)
            grant = grants.get(record.grant_id)
            if grant is None or record.key[:2] != (grant.spec.consumer_account_id, grant.spec.publication_id):
                raise ValueError("request does not belong to its grant consumer and publication")
            if record.key in requests:
                raise ValueError("duplicate request identity")
            requests[record.key] = record
        sources = {}
        for raw in value["sources"]:
            _object(raw, {"key", "contentHash"})
            key = raw["key"]
            if not isinstance(key, list) or len(key) != 4:
                raise ValueError("complete source key required")
            source = SourceReference(key[0], None, key[1], key[2], key[3], raw["contentHash"])
            if _source_key(source) in sources:
                raise ValueError("duplicate source identity")
            sources[_source_key(source)] = source.content_hash
        self._audits(value["audit"], grants, requests, sources)
        active = sum(g.status == "active" for g in grants.values())
        count = sum(len(value[k]) for k in ("grants", "requests", "sources", "audit"))
        if (
            count + active > self.max_records
            or len(encode(value)) + active * REVOCATION_BYTES + JOURNAL_ENVELOPE_BYTES > self.max_bytes
            or revision + active > self.journal.max_commits
        ):
            raise SharingUnavailable("sharing capacity exhausted; reserved owner revocation headroom is protected")

    def _audits(self, audit, grants, requests, sources):
        issued, revoked, reserved, completed, seen_sources = set(), set(), {}, set(), {}
        for ordinal, row in enumerate(audit, 1):
            _object(row, {"sequence", "grantSpecDigest", "event"})
            if type(row["sequence"]) is not int or row["sequence"] != ordinal:
                raise ValueError("audit sequence cannot be skipped or rewritten")
            event = row["event"]
            if not isinstance(event, dict):
                raise ValueError("audit event required")
            grant = grants.get(event.get("grantId"))
            if grant is None or row["grantSpecDigest"] != _spec_hash(grant):
                raise ValueError("audit does not match the immutable grant specification")
            spec = grant.spec
            kind = event.get("kind")
            extra = set()
            if kind == "reserved":
                extra = {"requestId", "source"}
            elif kind == "delivery_reserved":
                extra = {"requestId", "deliveryId"}
            elif kind in ("completed", "delivered", "disclosed"):
                extra = {"requestId", "source", "receiptId", "decisionId", "providerRequests"}
                if "delivery" in event or "originUsage" in event:
                    extra |= {"delivery", "originUsage"}
            elif kind not in ("issued", "revoked"):
                raise ValueError("unknown sharing audit event")
            _object(event, BASE_AUDIT | extra)
            at = event["at"]
            if not isinstance(at, str) or len(at) > 40 or datetime.fromisoformat(at).utcoffset() is None:
                raise ValueError("bounded aware audit timestamp required")
            pins = {
                "ownerAccountId": spec.owner_account_id,
                "consumerAccountId": spec.consumer_account_id,
                "publicationId": spec.publication_id,
                "engineId": spec.engine_id,
                "bindingRevision": spec.binding_revision,
                "billingAccountId": spec.billing_account_id,
                "billingPolicy": "owner-pays",
                "actorAccountId": spec.owner_account_id if kind in ("issued", "revoked") else spec.consumer_account_id,
            }
            if (
                any(event[k] != v for k, v in pins.items())
                or type(event["grantRevision"]) is not int
                or event["grantRevision"] != (2 if kind == "revoked" else 1)
                or spec.grant_id in revoked
            ):
                raise ValueError("audit scope or lifecycle differs")
            _text(event["actorId"])
            if kind == "issued":
                if spec.grant_id in issued or event["actorId"] != grant.issued_by:
                    raise ValueError("grant issuance audit differs")
                issued.add(spec.grant_id)
                continue
            if spec.grant_id not in issued:
                raise ValueError("grant must be issued before use")
            if kind == "revoked":
                if grant.status != "revoked":
                    raise ValueError("revocation audit requires terminal grant")
                revoked.add(spec.grant_id)
                continue
            key = spec.consumer_account_id, spec.publication_id, event["actorId"], _text(event["requestId"])
            request = requests.get(key)
            if request is None or request.grant_id != spec.grant_id:
                raise ValueError("audit request identity differs")
            if kind in ("reserved", "delivery_reserved"):
                operation = "infer" if kind == "reserved" else "deliver"
                if key in reserved or operation not in spec.allowed_operations:
                    raise ValueError("request reservation requires permitted operation exactly once")
                if kind == "reserved":
                    source = _source(event["source"])
                    if source.account_id != spec.consumer_account_id or sources.get(_source_key(source)) != source.content_hash:
                        raise ValueError("reservation source differs from canonical pin")
                    seen_sources[_source_key(source)] = source.content_hash
                else:
                    _text(event["deliveryId"])
                reserved[key] = event
                continue
            if key not in reserved or request.status != "completed":
                raise ValueError("receipt audit requires completed reserved request")
            receipt = _receipt(request, grant, reserved[key])
            if kind in ("completed", "delivered"):
                expected = "completed" if reserved[key]["kind"] == "reserved" else "delivered"
                if kind != expected or key in completed:
                    raise ValueError("request completion occurs exactly once")
                completed.add(key)
            elif key not in completed:
                raise ValueError("receipt must be completed before disclosure")
            receipt_pins = {
                "source": receipt["source"],
                "receiptId": receipt["receiptId"],
                "decisionId": receipt["decisionId"],
                "providerRequests": receipt["billing"]["providerRequests"],
            }
            if "delivery" in receipt:
                receipt_pins.update(delivery=receipt["delivery"], originUsage=receipt["billing"]["origin"])
            if set(event) != BASE_AUDIT | {"requestId"} | set(receipt_pins) or any(
                encode(event[k]) != encode(v) for k, v in receipt_pins.items()
            ):
                raise ValueError("receipt audit must preserve exact private result attribution")
        if issued != set(grants) or revoked != {k for k, g in grants.items() if g.status == "revoked"}:
            raise ValueError("every grant lifecycle transition requires durable audit")
        if set(reserved) != set(requests) or completed != {k for k, r in requests.items() if r.status == "completed"}:
            raise ValueError("every request lifecycle transition requires durable audit")
        if seen_sources != sources:
            raise ValueError("source identities require reservation evidence")
        for identifier, grant in grants.items():
            if sum(r.grant_id == identifier for r in requests.values()) != grant.reserved_requests:
                raise ValueError("request reservations and consumed allowance must agree")

    @staticmethod
    def _transitions(before, after):
        if encode(after["audit"][: len(before["audit"])]) != encode(before["audit"]):
            raise SharingConflict("existing audit is immutable")
        for name, identity in (("sources", lambda r: tuple(r["key"])), ("requests", lambda r: tuple(r["key"]))):
            current = {identity(r): r for r in after[name]}
            for prior in before[name]:
                changed = current.get(identity(prior))
                if changed is None:
                    raise SharingConflict("existing authority cannot be deleted")
                if name == "sources" or prior["status"] == "completed":
                    if encode(changed) != encode(prior):
                        raise SharingConflict("source pins and completed receipts are immutable")
                elif encode({k: v for k, v in changed.items() if k not in ("status", "receipt_json")}) != encode(
                    {k: v for k, v in prior.items() if k not in ("status", "receipt_json")}
                ):
                    raise SharingConflict("reserved request identity cannot change")
        old_requests = {tuple(r["key"]) for r in before["requests"]}
        if any(tuple(r["key"]) not in old_requests and r["status"] != "reserved" for r in after["requests"]):
            raise SharingConflict("new requests must first reserve durably")
        current = {g["spec"]["grant_id"]: g for g in after["grants"]}
        for prior in before["grants"]:
            changed = current.get(prior["spec"]["grant_id"])
            if changed is None or encode(changed["spec"]) != encode(prior["spec"]) or changed["issued_by"] != prior["issued_by"]:
                raise SharingConflict("grant scope and issuer are immutable")
            if (
                changed["reserved_requests"] < prior["reserved_requests"]
                or prior["status"] == "revoked"
                and encode(changed) != encode(prior)
                or changed["status"] == "revoked"
                and changed["reserved_requests"] != prior["reserved_requests"]
            ):
                raise SharingConflict("grant allowance and revocation cannot be undone")
        old_grants = {g["spec"]["grant_id"] for g in before["grants"]}
        if any(g["spec"]["grant_id"] not in old_grants and g["status"] != "active" for g in after["grants"]):
            raise SharingConflict("new grants must first be issued")


class _Transaction:
    def __init__(self, value):
        self._value, self._closed = value, False

    def _check(self):
        if self._closed:
            raise SharingUnavailable("sharing transaction has ended")

    def _row(self, name, key, get_key):
        self._check()
        return next((row for row in self._value[name] if get_key(row) == key), None)

    def _save(self, name, row, get_key):
        self._check()
        key = get_key(row)
        rows = self._value[name]
        prior = self._row(name, key, get_key)
        if prior is not None:
            rows.remove(prior)
        rows.append(_copy(row))

    def load_grant(self, identifier):
        row = self._row("grants", identifier, lambda r: r["spec"]["grant_id"])
        return _grant(row) if row is not None else None

    def save_grant(self, grant):
        if type(grant) is not SharingGrant:
            raise ValueError("validated sharing grant required")
        self._save("grants", _grant_json(grant), lambda r: r["spec"]["grant_id"])

    def load_request(self, key):
        row = self._row("requests", key, lambda r: tuple(r["key"]))
        return _request(row) if row is not None else None

    def save_request(self, record):
        if type(record) is not RequestRecord:
            raise ValueError("sharing request record required")
        self._save("requests", asdict(record), lambda r: tuple(r["key"]))

    def load_source(self, key):
        row = self._row("sources", key, lambda r: tuple(r["key"]))
        return row["contentHash"] if row is not None else None

    def save_source(self, key, content_hash):
        self._save("sources", {"key": list(key), "contentHash": content_hash}, lambda r: tuple(r["key"]))

    def append_audit(self, event):
        self._check()
        event = _copy(event)
        grant = self.load_grant(event.get("grantId"))
        if grant is None:
            raise ValueError("audit requires its current grant")
        self._value["audit"].append(
            {"sequence": len(self._value["audit"]) + 1, "grantSpecDigest": _spec_hash(grant), "event": event}
        )

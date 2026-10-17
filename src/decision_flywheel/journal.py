"""Compare-and-swap journal contract shared by every document storage adapter.

A journal holds one JSON value per namespace behind an immutable anchor and a
conditionally replaced head. The document adapters only need initialize, read,
commit and exists, so any store that offers a conditional write can back them.
Uncertain writes always raise; observing a claim later never grants dispatch
permission.
"""

import json
import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from uuid import UUID, uuid4

from .storage_errors import StorageConflict, StorageLimit, StorageUncertain
from .storage_payloads import PayloadRef


class JournalConflict(StorageConflict):
    pass


class JournalUnavailable(RuntimeError):
    pass


class JournalUncertain(StorageUncertain):
    pass


class JournalLimit(StorageLimit):
    pass


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def decode(content):
    def invalid(value):
        raise ValueError("non-JSON value")

    return json.loads(content, parse_constant=invalid)


def uuid(value):
    if not isinstance(value, str) or str(UUID(value)) != value or UUID(value).version != 4:
        raise ValueError("UUIDv4 required")
    return value


@dataclass(frozen=True)
class JournalRecord:
    namespace: str
    anchor: str
    revision: int
    commit_id: str
    etag: str
    reference: PayloadRef
    value: object


@runtime_checkable
class Journal(Protocol):
    namespace: str
    max_bytes: int
    boundary: object

    def exists(self) -> bool: ...
    def initialize(self, value) -> JournalRecord: ...
    def read(self) -> JournalRecord: ...
    def commit(self, expected, value) -> JournalRecord: ...


class CanonicalJournal:
    """Anchor, head and immutable transaction logic over a conditional-write store.

    Subclasses supply only the two coordination primitives, _get and _put, and
    the payload adapter that stores immutable transactions.
    """

    def __init__(self, payloads, domain, name, *, max_bytes, max_commits, create_only_initialization=False):
        if any(not isinstance(v, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", v) for v in (domain, name)):
            raise ValueError("fixed domain and run namespace required")
        if (
            type(max_bytes) is not int
            or not 1 <= max_bytes <= payloads.max_bytes
            or type(max_commits) is not int
            or not 1 <= max_commits < 2**63
        ):
            raise ValueError("explicit positive journal byte and commit budgets required")
        if type(create_only_initialization) is not bool:
            raise ValueError("explicit creation mode required")
        self.create_only_initialization = create_only_initialization
        self.payloads = payloads
        self.max_bytes = max_bytes
        self.max_commits = max_commits
        self.namespace = f"{payloads.scope.key}/{domain}/{name}"
        prefix = f"{payloads.scope.key}/coordination/{domain}/{name}"
        self.head_key = prefix + "/head.json"
        self.anchor_key = prefix + "/anchor.json"
        self.known_anchor = None

    def _get(self, key, *, missing=False, creation_probe=False):
        raise NotImplementedError

    def _put(self, key, value, etag=None):
        raise NotImplementedError

    def _checked_anchor(self, value):
        try:
            if (
                set(value) != {"schema", "namespace", "anchor"}
                or value["schema"] != "cyclotron-s3-anchor/v1"
                or value["namespace"] != self.namespace
            ):
                raise ValueError()
            return uuid(value["anchor"])
        except Exception:
            raise JournalUnavailable("invalid namespace anchor") from None

    def _anchor(self):
        # The anchor is immutable once created, so one verified read serves the handle.
        if self.known_anchor is None:
            self.known_anchor = self._checked_anchor(self._get(self.anchor_key)[0])
        return self.known_anchor

    def _head(self, anchor, revision, commit_id, reference):
        return {
            "schema": "cyclotron-s3-head/v1",
            "namespace": self.namespace,
            "anchor": anchor,
            "revision": revision,
            "commit": commit_id,
            "state": {"digest": reference.digest, "length": reference.length},
        }

    def _publish(self, anchor, revision, value, parent):
        commit_id = str(uuid4())
        transaction = {
            "schema": "cyclotron-s3-transaction/v1",
            "namespace": self.namespace,
            "anchor": anchor,
            "revision": revision,
            "commit": commit_id,
            "parent": parent,
            "value": value,
        }
        content = encode(transaction)
        if len(content) > self.max_bytes or revision > self.max_commits:
            raise JournalLimit("canonical journal budget exceeded; no truncation")
        try:
            reference = self.payloads.put(content)
        except Exception:
            raise JournalUncertain("immutable transaction publication uncertain; head not advanced") from None
        return self._head(anchor, revision, commit_id, reference), reference, commit_id

    def exists(self):
        if self._get(self.anchor_key, missing=True) is not None:
            return True
        if self._get(self.head_key, missing=True) is not None:
            raise JournalUnavailable("head exists without namespace anchor; operator reconciliation required")
        return False

    def initialize(self, value):
        # Explicit new namespace only. The immutable anchor makes a missing head
        # after a crash/deletion unavailable rather than silently resetting money.
        # A no-ListBucket principal cannot distinguish missing from forbidden GET.
        # Only explicit initialization may probe this ambiguity: successful
        # create-only anchor AND head receipts establish creation, never the GET.
        existing = self._get(self.anchor_key, missing=True, creation_probe=self.create_only_initialization)
        if existing is not None:
            self.known_anchor = self._checked_anchor(existing[0])
            return self.read()
        if self._get(self.head_key, missing=True, creation_probe=self.create_only_initialization) is not None:
            raise JournalUnavailable("head exists without namespace anchor; operator reconciliation required")
        # Enforce size before creating a namespace that would need reconciliation.
        if len(encode(value)) + 1024 > self.max_bytes:
            raise JournalLimit("canonical journal byte budget exceeded")
        anchor = str(uuid4())
        try:
            self._put(self.anchor_key, {"schema": "cyclotron-s3-anchor/v1", "namespace": self.namespace, "anchor": anchor})
        except JournalConflict:
            return self.read()
        self.known_anchor = anchor
        head, reference, commit_id = self._publish(anchor, 1, value, None)
        etag = self._put(self.head_key, head)
        return JournalRecord(self.namespace, anchor, 1, commit_id, etag, reference, decode(encode(value)))

    def read(self):
        anchor = self._anchor()
        head, etag = self._get(self.head_key)
        try:
            if (
                set(head) != {"schema", "namespace", "anchor", "revision", "commit", "state"}
                or head["schema"] != "cyclotron-s3-head/v1"
                or head["namespace"] != self.namespace
                or head["anchor"] != anchor
                or type(head["revision"]) is not int
                or not 1 <= head["revision"] <= self.max_commits
            ):
                raise ValueError()
            uuid(head["commit"])
            if set(head["state"]) != {"digest", "length"}:
                raise ValueError()
            ref = PayloadRef(self.payloads.scope, head["state"]["digest"], head["state"]["length"])
            if ref.length > self.max_bytes:
                raise ValueError()
            transaction = decode(self.payloads.get(ref))
            if (
                set(transaction) != {"schema", "namespace", "anchor", "revision", "commit", "parent", "value"}
                or transaction["schema"] != "cyclotron-s3-transaction/v1"
                or any(transaction[k] != head[k] for k in ("namespace", "anchor", "revision", "commit"))
            ):
                raise ValueError()
            parent = transaction["parent"]
            if head["revision"] == 1:
                if parent is not None:
                    raise ValueError()
            else:
                if not isinstance(parent, dict) or set(parent) != {"digest", "length"}:
                    raise ValueError()
                PayloadRef(self.payloads.scope, parent["digest"], parent["length"])
            return JournalRecord(self.namespace, anchor, head["revision"], head["commit"], etag, ref, transaction["value"])
        except Exception:
            raise JournalUnavailable("invalid or missing canonical transaction; never use an index as fallback") from None

    def commit(self, expected, value):
        if (
            not isinstance(expected, JournalRecord)
            or expected.namespace != self.namespace
            or expected.reference.scope != self.payloads.scope
        ):
            raise PermissionError("journal transaction scope refused")
        if self._anchor() != expected.anchor:
            raise JournalConflict("namespace anchor changed")
        head, reference, commit_id = self._publish(
            expected.anchor,
            expected.revision + 1,
            value,
            {"digest": expected.reference.digest, "length": expected.reference.length},
        )
        etag = self._put(self.head_key, head, expected.etag)
        return JournalRecord(
            self.namespace, expected.anchor, expected.revision + 1, commit_id, etag, reference, decode(encode(value))
        )


def commit_with_contract(journal, expected, value, *, invalidate=None, invalidate_on_conflict=False):
    """The one commit path for every journal-backed adapter.

    Conflict, uncertain and limit outcomes always surface as the shared storage
    exception family. The caller's invalidate hook runs before an uncertain
    outcome (and optionally a conflict) propagates, so no handle keeps serving
    memory that may be stale. Retrying a conflict stays the caller's decision.
    """
    try:
        return journal.commit(expected, value)
    except StorageUncertain:
        if invalidate is not None:
            invalidate()
        raise
    except StorageConflict:
        if invalidate is not None and invalidate_on_conflict:
            invalidate()
        raise

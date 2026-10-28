"""S3-authoritative shared batches, atomic paid budget and fenced dispatch claims.

The paid-request ledger reserves a batched model request before dispatch and records its reply after,
so a retry never pays twice. No SQLite connection or DynamoDB authority is used.

Layout (``cyclotron-shared-batches/v2``). What one decision writes does not depend on how many
decisions came before it:

* The control journal's head holds only small counters (``attempts``, ``superseded``), the
  generations still in flight (``held``: pending requests, plus a settled one only while it could not
  yet be written out) and record-lake coverage. It never lists finished requests.
* A finished generation of a request is one create-only record found by key,
  ``records/shared-batches/batches-<ledger>/batch-<fingerprint>-<generation>.json``. An idempotent retry is
  a GET by key, never a scan. A request's generations are numbered 1, 2, ... and the current one is the
  highest that exists; a reader walks only that one request's own generations.
* Each operation owner gets ``owner-<owner>.json`` naming its request (a hint, verified against the
  request's generations), so an owner reused for a different request is still refused.
* Every attempt is also a record in the ``attempts-<ledger>`` lake (ordered, covered by the head), for
  export and audit without list permission.

Commits per paid request: two. ``reserve(..., claim=True)`` reserves and takes the dispatch claim in one
commit (``claim`` then confirms it without a write); ``settle`` records the reply in a second. Plain
``reserve`` followed by ``claim`` still works and still takes two commits, exactly as before.

Readers trust the head first: a generation the head holds wins over any record with its key, and a
record for a generation the head no longer holds is authoritative. Settling writes the record before
the head drops the generation, so a crash between the two leaves the request pending and claimed
(never payable again), and the same owner repeating the identical settlement adopts the record.

Raw provider replies. With a ``replies`` record lake a completed request keeps its raw reply as a
write-once lake record and the batch record keeps the small note (sequence, SHA-256, length), verified
on every read. If writing the reply record fails, the reply goes inline in the batch record; if the
batch record cannot be written either, the settled generation stays in the head rather than being
dropped, because the provider has already been paid.

Old ledgers (``cyclotron-shared-batches/v1``: every row in the head) still read exactly as before. The
first write migrates one in a single commit: every attempt, owner and finished generation is written as
a record (repeating a record is harmless), then the head is replaced by the small v2 head. The portable
``export`` snapshot keeps the v1 shape, so SQLite and S3 ledgers still compare equal.
"""

import json
import logging
import os
import re
import tempfile
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256

from .batch_storage import (
    BatchConflict,
    BatchInProgress,
    BatchRecord,
    BatchUncertain,
    StorageScope,
    is_reply_reference,
    validate_request,
    validate_snapshot,
)
from .decision_cache import CacheMiss
from .journal import CanonicalJournal, Journal, JournalConflict, JournalUncertain, decode, encode
from .record_lake import (
    RECORDS_KEY,
    Coverage,
    FileRecordLake,
    RecordConflict,
    RecordCorrupt,
    RecordLake,
    RecordLimit,
    RecordUnavailable,
    RecordUncertain,
    S3RecordLake,
    commit_with_records,
    coverage_in,
)
from .s3_transient import cause_name, read_with_one_retry
from .storage_payloads import FilePayloads, S3Payloads

log = logging.getLogger(__name__)

V1 = "cyclotron-shared-batches/v1"
V2 = "cyclotron-shared-batches/v2"
# The portable snapshot, and the old whole-ledger head layout.
EMPTY = {"schema": V1, "batches": [], "attempts": [], "history": []}
EMPTY_V2 = {"schema": V2, "attempts": 0, "superseded": 0, "held": []}
HEAD_FIELDS = set(EMPTY_V2)
ABANDONED = '{"learning_abandoned":true}'
STATUSES = ("pending", "complete", "failed", "interrupted")
GENERATION_FIELDS = {"fingerprint", "generation", "owner", "ordinal", "supersedes", "status", "payload", "claimed", "revision"}
ATTEMPT_FIELDS = ("ordinal", "fingerprint", "owner", "generation", "supersedes")
RECORD_ERRORS = (RecordUncertain, RecordLimit, RecordConflict, RecordUnavailable)


# --- create-only records found by key ------------------------------------------------------------------

KEYED_SCHEMA = "cyclotron-keyed-record/v1"
_KEY = re.compile(r"[a-zA-Z0-9_-]+")
_EXISTS = ("PreconditionFailed", "ConditionalRequestConflict", "412", "409")
_DENIED = ("AccessDenied", "403")
SENTINEL = "readable"


class KeyedRecords:
    """Write-once records named by a key derived from their identity. A back end supplies ``_create`` and ``_fetch``.

    ``put`` creates a record and returns the value stored under its key: its own, or the different one
    that was there first (nothing is ever overwritten). ``get`` returns None for a key nobody wrote.
    """

    def __init__(self, scope, domain, kind):
        if not isinstance(scope, StorageScope) or any(not isinstance(v, str) or not _KEY.fullmatch(v) for v in (domain, kind)):
            raise ValueError("fixed scope, domain and kind required")
        self.scope, self.domain, self.kind = scope, domain, kind
        self.name = f"{domain}/{kind}"
        self.namespace = f"{scope.key}/records/{domain}/{kind}"

    def _create(self, key, content):
        """Create-only write. True if created, False if the key already exists."""
        raise NotImplementedError

    def _fetch(self, key):
        """The stored bytes, or None if nothing was written under the key."""
        raise NotImplementedError

    def _encode(self, key, value):
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            raise ValueError("record key required")
        return encode({"schema": KEYED_SCHEMA, "namespace": self.namespace, "key": key, "value": value})

    def _decode(self, key, content):
        try:
            envelope = decode(content)
            if (
                set(envelope) != {"schema", "namespace", "key", "value"}
                or envelope["schema"] != KEYED_SCHEMA
                or envelope["namespace"] != self.namespace
                or envelope["key"] != key
            ):
                raise ValueError()
            return envelope["value"]
        except Exception:
            raise RecordCorrupt("keyed record envelope does not match its key") from None

    def put(self, key, value):
        content = self._encode(key, value)
        if self._create(key, content):
            return decode(content)["value"]
        existing = self._fetch(key)
        if existing is None:
            raise RecordUnavailable("a record that refused creation could not be read")
        return self._decode(key, existing)

    def get(self, key):
        content = self._fetch(key)
        return None if content is None else self._decode(key, content)


class MemoryKeyedRecords(KeyedRecords):
    """In-process records for tests. Pass the same ``store`` dict to model reopening."""

    def __init__(self, scope, domain, kind, *, store=None):
        super().__init__(scope, domain, kind)
        self.store = {} if store is None else store

    def _create(self, key, content):
        return self.store.setdefault((self.namespace, key), content) is content

    def _fetch(self, key):
        return self.store.get((self.namespace, key))


class FileKeyedRecords(KeyedRecords):
    """Trusted local directory, byte-identical in layout to the S3 keys under the payload root."""

    def __init__(self, payloads, domain, kind):
        if not isinstance(payloads, FilePayloads):
            raise ValueError("injected scoped file payload adapter required")
        super().__init__(payloads.scope, domain, kind)
        self.directory = payloads.root / self.namespace

    def _path(self, key):
        return self.directory / f"{key}.json"

    def _create(self, key, content):
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self.directory, prefix="tmp-", delete=False) as temporary:
                name = temporary.name
                try:
                    temporary.write(content)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                    os.link(name, self._path(key))
                except FileExistsError:
                    return False
                finally:
                    os.unlink(name)
            return True
        except OSError:
            raise RecordUncertain("file record write outcome uncertain; repeat the identical record") from None

    def _fetch(self, key):
        try:
            with open(self._path(key), "rb") as stream:
                return stream.read()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise RecordUnavailable("record unavailable", cause_name(error)) from None


class S3KeyedRecords(KeyedRecords):
    """Create-only S3 objects at ``<account>/<workspace>/records/<domain>/<kind>/<key>.json``.

    A principal without list permission gets AccessDenied, not NoSuchKey, for a key nobody wrote. Such a
    denial counts as "not written" only once this handle has proved it can read this very prefix, by
    reading a sentinel record it creates there. A real permission failure therefore fails closed.
    """

    def __init__(self, payloads, domain, kind):
        if not isinstance(payloads, S3Payloads):
            raise ValueError("injected scoped S3 payload adapter required")
        meta = getattr(payloads.client, "meta", None)
        if meta is not None and getattr(meta, "config", None) is not None:
            if (meta.config.retries or {}).get("total_max_attempts") != 1:
                raise ValueError("conditional S3 client requires total_max_attempts=1")
        super().__init__(payloads.scope, domain, kind)
        self.client, self.bucket, self.owner = payloads.client, payloads.bucket, payloads.owner
        self.readable = False

    def _key(self, key):
        return f"{self.namespace}/{key}.json"

    def _create(self, key, content):
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=self._key(key),
                Body=content,
                ExpectedBucketOwner=self.owner,
                IfNoneMatch="*",
                ContentType="application/json",
            )
            return True
        except Exception as error:
            if getattr(error, "response", {}).get("Error", {}).get("Code") in _EXISTS:
                return False
            raise RecordUncertain("S3 record write outcome uncertain; repeat the identical record") from None

    def _get(self, key):
        stream = None
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self._key(key), ExpectedBucketOwner=self.owner)
            stream = response["Body"]
            length = response["ContentLength"]
            if type(length) is not int or length < 1:
                raise ValueError("invalid record object")
            content = stream.read(length + 1)
            if len(content) != length:
                raise ValueError("incomplete record object")
            return content
        finally:
            if stream is not None:
                stream.close()

    def _read(self, key):
        """(bytes or None, error code or None)."""
        try:
            return read_with_one_retry(lambda: self._get(key), "record"), None
        except Exception as error:
            code = getattr(error, "response", {}).get("Error", {}).get("Code")
            if code == "NoSuchKey" or code in _DENIED:
                return None, code
            log.warning("keyed record unavailable: %s", cause_name(error))
            raise RecordUnavailable("record unavailable", cause_name(error)) from None

    def _prove_readable(self):
        if not self.readable:
            content = self._encode(SENTINEL, {"purpose": "proves this prefix is readable"})
            self._create(SENTINEL, content)
            found, _ = self._read(SENTINEL)
            if found != content:
                raise RecordUnavailable("keyed records are not readable; never treat a denial as a missing record")
            self.readable = True

    def _fetch(self, key):
        content, code = self._read(key)
        if code in _DENIED:
            self._prove_readable()
        return content


def _ledger_name(journal):
    domain, name = journal.namespace.rsplit("/", 2)[-2:]
    return name if domain == "shared-batches" else f"{domain}-{name}"


def batch_records_for(journal):
    """The keyed batch records that sit beside a ledger journal, on the same store, or None for an unknown store."""
    payloads, kind = getattr(journal, "payloads", None), "batches-" + _ledger_name(journal)
    if isinstance(payloads, S3Payloads):
        return S3KeyedRecords(payloads, "shared-batches", kind)
    if isinstance(payloads, FilePayloads):
        return FileKeyedRecords(payloads, "shared-batches", kind)
    return None


def attempts_lake_for(journal):
    """The ordered attempt lake beside a ledger journal, or None for an unknown store."""
    payloads, kind = getattr(journal, "payloads", None), "attempts-" + _ledger_name(journal)
    if isinstance(payloads, S3Payloads):
        return S3RecordLake(payloads, "shared-batches", kind)
    if isinstance(payloads, FilePayloads):
        return FileRecordLake(payloads, "shared-batches", kind)
    return None


def reply_lake_for(journal, ledger):
    """The reply lake that sits beside a ledger journal, on the same store, or None for an unknown store.

    One lake per ledger (``replies-<ledger>``) so ledgers that share a scope never share a sequence.
    """
    payloads, kind = getattr(journal, "payloads", None), f"replies-{ledger}"
    if isinstance(payloads, S3Payloads):
        return S3RecordLake(payloads, "shared-batches", kind)
    if isinstance(payloads, FilePayloads):
        return FileRecordLake(payloads, "shared-batches", kind)
    return None


def reply_reference(seq, payload):
    content = payload.encode()
    return {"seq": seq, "sha256": sha256(content).hexdigest(), "length": len(content)}


def reply_value(fingerprint, owner, generation, payload):
    return {"fingerprint": fingerprint, "owner": owner, "generation": generation, "payload": payload}


def same_payload(held, payload):
    """Whether a row's payload slot (embedded text or a reply reference) holds exactly ``payload``."""
    if is_reply_reference(held):
        return payload is not None and reply_reference(held["seq"], payload) == held
    return held == payload


def payload_digest(held):
    """(sha256, length) of a payload slot's text, whichever way it is held, or None for no payload."""
    if held is None:
        return None
    if is_reply_reference(held):
        return held["sha256"], held["length"]
    content = held.encode()
    return sha256(content).hexdigest(), len(content)


def batch_key(fingerprint, generation):
    return f"batch-{fingerprint}-{generation}"


def owner_key(owner):
    return f"owner-{owner}"


# --- generations: one attempt at one request ------------------------------------------------------------


def generation(fingerprint, number, owner, ordinal, supersedes, status, payload, claimed, revision):
    return {
        "fingerprint": fingerprint,
        "generation": number,
        "owner": owner,
        "ordinal": ordinal,
        "supersedes": supersedes,
        "status": status,
        "payload": payload,
        "claimed": claimed,
        "revision": revision,
    }


def attempt_value(gen):
    return {name: gen[name] for name in ATTEMPT_FIELDS}


def check_generation(gen):
    """Validate one generation. ``claimed``/``revision`` are None only for history migrated from v1."""
    if not isinstance(gen, dict) or set(gen) != GENERATION_FIELDS:
        raise ValueError("invalid batch generation")
    validate_request(gen["fingerprint"], gen["owner"])
    number, ordinal, supersedes, status, payload = (gen[k] for k in ("generation", "ordinal", "supersedes", "status", "payload"))
    if (
        type(number) is not int
        or number < 1
        or type(ordinal) is not int
        or ordinal < 1
        or (supersedes is None) != (number == 1)
        or (supersedes is not None and (type(supersedes) is not int or supersedes < 1))
        or status not in STATUSES
        or gen["claimed"] not in (True, False, None)
        or (gen["revision"] is not None and (type(gen["revision"]) is not int or gen["revision"] < 1))
    ):
        raise ValueError("invalid batch generation")
    if status == "pending" and (payload is not None or gen["claimed"] is None or gen["revision"] is None):
        raise ValueError("a pending generation has no payload and a claim and revision")
    if is_reply_reference(payload):
        if status != "complete":
            raise ValueError("only a complete batch may cite a reply record")
    elif payload is not None:
        if not isinstance(payload, str):
            raise ValueError("payload text required")
        json.loads(payload)
    if status == "complete" and payload is None:
        raise ValueError("complete payload required")
    return gen


def view_of_snapshot(snapshot):
    """A v1 ledger (or portable snapshot) as a v2 head that still holds every generation."""
    batches, attempts, history = snapshot["batches"], snapshot["attempts"], snapshot["history"]
    counts, ordinals = {}, {}
    for ordinal, fingerprint, _owner in attempts:
        counts[fingerprint] = counts.get(fingerprint, 0) + 1
        ordinals[fingerprint, counts[fingerprint]] = ordinal
    history_ids = {(row[1], row[2]): row[0] for row in history}
    held = [
        generation(fp, g, owner, ordinals[fp, g], history_ids.get((fp, g - 1)), status, payload, claimed, revision)
        for fp, status, payload, g, owner, claimed, revision in batches
    ]
    held += [
        generation(fp, g, owner, ordinals[fp, g], history_ids.get((fp, g - 1)), status, payload, None, None)
        for _id, fp, g, status, payload, owner in history
    ]
    held.sort(key=lambda gen: gen["ordinal"])
    view = {"schema": V2, "attempts": len(attempts), "superseded": max(history_ids.values(), default=0), "held": held}
    if RECORDS_KEY in snapshot:
        view[RECORDS_KEY] = snapshot[RECORDS_KEY]
    return view


def snapshot_of(gens):
    """The portable v1 snapshot of every generation (payloads as given)."""
    by_request = {}
    for gen in gens:
        by_request.setdefault(gen["fingerprint"], {})[gen["generation"]] = gen
    batches, history = [], []
    for fingerprint in sorted(by_request):
        chain = by_request[fingerprint]
        top = max(chain)
        if sorted(chain) != list(range(1, top + 1)):
            raise RecordUnavailable("a request generation is missing; never use an index as fallback")
        current = chain[top]
        batches.append(
            [fingerprint, current["status"], current["payload"], top, current["owner"], current["claimed"], current["revision"]]
        )
        for number in range(1, top):
            old = chain[number]
            history.append([chain[number + 1]["supersedes"], fingerprint, number, old["status"], old["payload"], old["owner"]])
    attempts = [[g["ordinal"], g["fingerprint"], g["owner"]] for g in sorted(gens, key=lambda g: g["ordinal"])]
    history.sort(key=lambda row: row[0])
    return {"schema": V1, "batches": batches, "attempts": attempts, "history": history}


@dataclass(frozen=True)
class RepliesConverted:
    originals: list  # (fingerprint, owner, generation, text) in the order they were written
    refs: list  # RecordRef of each reply record written
    before: object  # JournalRecord before the commit
    after: object  # JournalRecord the commit produced


@dataclass(frozen=True)
class _Change:
    """One planned commit: the head's new held generations and counters, plus the records it writes first."""

    held: list
    attempts: int
    superseded: int
    attempt: dict | None = None  # a new attempt: its lake record, and its owner's hint
    reply: tuple | None = None  # (fingerprint, generation, text) to write to the reply lake and cite


class _View:
    """The head as of one commit, indexed for lookups. Keyed reads made under it are cached with it."""

    def __init__(self, record, value, legacy):
        self.record, self.value, self.legacy = record, value, legacy
        self.held = {}
        self.owners = {}
        for gen in value["held"]:
            self.held.setdefault(gen["fingerprint"], {})[gen["generation"]] = gen
            self.owners[gen["owner"]] = gen
        self.lookups = {}


class S3BatchStorage:
    def __init__(self, journal, *, max_conflicts=8, replies=None, records=None, attempts=None):
        if not isinstance(journal, Journal):
            raise ValueError("canonical journal required")
        scope = journal.boundary[0]
        if replies is not None and (not isinstance(replies, RecordLake) or replies.scope != scope):
            raise ValueError("reply record lake must belong to the journal's scope")
        records = batch_records_for(journal) if records is None else records
        attempts = attempts_lake_for(journal) if attempts is None else attempts
        if not isinstance(records, KeyedRecords) or records.scope != scope:
            raise ValueError("keyed batch records in the journal's scope required")
        if not isinstance(attempts, RecordLake) or attempts.scope != scope:
            raise ValueError("an attempt record lake in the journal's scope required")
        if type(max_conflicts) is not int or max_conflicts < 1:
            raise ValueError("a positive contention retry count required")
        self.replies, self.records, self.attempts = replies, records, attempts
        self.journal = journal
        self.max_conflicts = max_conflicts
        self._view = None
        self._claims = {}  # (fingerprint, owner) -> generation this handle reserved and claimed in one commit
        self._written = {}  # batch records written during the current write, for its own conflict retries

    # --- reading the head ---------------------------------------------------------------------------------

    def _check_v1(self, value):
        if not isinstance(value, dict) or set(value) not in (set(EMPTY), set(EMPTY) | {RECORDS_KEY}):
            raise ValueError("complete shared batch state required")
        validate_snapshot(value, replies=True)
        cited = [r[2] for r in value["batches"] if is_reply_reference(r[2])]
        cited += [r[4] for r in value["history"] if is_reply_reference(r[4])]
        self._check_cited(value, cited)
        return value

    def _check_cited(self, value, cited):
        if cited:
            if self.replies is None:
                raise ValueError("this ledger cites reply records; a reply record lake is required")
            coverage = coverage_in(value, self.replies)
            if not all(coverage.covers(ref["seq"]) for ref in cited) or len({ref["seq"] for ref in cited}) != len(cited):
                raise ValueError("reply references must name distinct covered records")

    def _check_v2(self, value):
        if not isinstance(value, dict) or set(value) not in (HEAD_FIELDS, HEAD_FIELDS | {RECORDS_KEY}) or value["schema"] != V2:
            raise ValueError("complete shared batch state required")
        if any(type(value[k]) is not int or value[k] < 0 for k in ("attempts", "superseded")) or not isinstance(
            value["held"], list
        ):
            raise ValueError("invalid shared batch counters")
        seen, top = set(), {}
        for gen in value["held"]:
            check_generation(gen)
            identity = (gen["fingerprint"], gen["generation"])
            if identity in seen or gen["ordinal"] > value["attempts"] or (gen["supersedes"] or 0) > value["superseded"]:
                raise ValueError("invalid held generation")
            seen.add(identity)
            top[gen["fingerprint"]] = max(top.get(gen["fingerprint"], 0), gen["generation"])
        if any(gen["status"] == "pending" and gen["generation"] != top[gen["fingerprint"]] for gen in value["held"]):
            raise ValueError("only a request's latest generation may be pending")
        self._check_cited(value, [g["payload"] for g in value["held"] if is_reply_reference(g["payload"])])
        return value

    def _check(self, value):
        if isinstance(value, dict) and value.get("schema") == V2:
            return self._check_v2(value)
        return self._check_v1(value)

    def initialize(self):
        record = self.journal.initialize(deepcopy(EMPTY_V2))
        self._check(record.value)

    def _read(self):
        known = self._view.record if self._view is not None else None
        if known is not None and isinstance(self.journal, CanonicalJournal):
            return self.journal.read(known=known)
        return self.journal.read()

    def _state(self):
        record = self._read()
        current = self._view
        if current is None or (current.record.anchor, current.record.revision, current.record.commit_id) != (
            record.anchor,
            record.revision,
            record.commit_id,
        ):
            value = self._check(record.value)
            legacy = value.get("schema") != V2
            current = _View(record, view_of_snapshot(value) if legacy else value, legacy)
        else:
            current = _View.__new__(_View)
            current.__dict__.update(self._view.__dict__, record=record)
        self._view = current
        return current

    # --- finding generations by key ------------------------------------------------------------------------

    def _keyed(self, view, fingerprint, number):
        key = batch_key(fingerprint, number)
        if key not in view.lookups:
            value = self.records.get(key)
            if value is not None:
                check_generation(value)
                if (value["fingerprint"], value["generation"]) != (fingerprint, number) or value["status"] == "pending":
                    raise RecordCorrupt("batch record does not match its key")
            view.lookups[key] = value
        return view.lookups[key]

    def _chain(self, view, fingerprint, *, every=False):
        """A request's generations in order: enough to know the current one, or ``every`` one of them."""
        held = view.held.get(fingerprint, {})
        found = dict(held)
        top = max(held, default=0)
        if not view.legacy:
            if every:
                for number in range(1, top):
                    if number not in found:
                        gen = self._keyed(view, fingerprint, number)
                        if gen is None:
                            raise RecordUnavailable("a request generation is missing; never use an index as fallback")
                        found[number] = gen
            if not (top and held[top]["status"] == "pending"):
                number = top + 1
                while (gen := self._keyed(view, fingerprint, number)) is not None:
                    found[number] = gen
                    number += 1
        return [found[number] for number in sorted(found)]

    def _current(self, view, fingerprint):
        chain = self._chain(view, fingerprint)
        return chain[-1] if chain else None

    def _held_pending(self, view, fingerprint):
        held = view.held.get(fingerprint, {})
        gen = held[max(held)] if held else None
        return gen if gen is not None and gen["status"] == "pending" else None

    def _owner_attempt(self, view, owner):
        """The generation this owner reserved, or None. The owner record is a hint, verified by the request's chain."""
        if owner in view.owners or view.legacy:
            return view.owners.get(owner)
        key = owner_key(owner)
        if key not in view.lookups:
            view.lookups[key] = self.records.get(key)
        hint = view.lookups[key]
        if not isinstance(hint, dict) or set(hint) != {"fingerprint"}:
            return None
        return next((g for g in self._chain(view, hint["fingerprint"], every=True) if g["owner"] == owner), None)

    def _reply(self, gen):
        """The raw reply a reference names, read from the lake and checked against the reference."""
        reference = gen["payload"]
        if self.replies is None:
            raise ValueError("this ledger cites reply records; a reply record lake is required")
        value = self.replies.read(reference["seq"])
        try:
            if (
                set(value) != {"fingerprint", "owner", "generation", "payload"}
                or (value["fingerprint"], value["owner"], value["generation"])
                != (gen["fingerprint"], gen["owner"], gen["generation"])
                or reply_reference(reference["seq"], value["payload"]) != reference
            ):
                raise ValueError()
        except Exception:
            raise RecordCorrupt("reply record does not match its reference") from None
        return value["payload"]

    def _text(self, gen):
        return self._reply(gen) if is_reply_reference(gen["payload"]) else gen["payload"]

    def _batch(self, gen, *, payload=True, claimed=None):
        if gen is None:
            return None
        text = self._text(gen) if payload else (None if is_reply_reference(gen["payload"]) else gen["payload"])
        return BatchRecord(
            gen["fingerprint"],
            gen["status"],
            text,
            gen["generation"],
            gen["owner"],
            gen["claimed"] if claimed is None else claimed,
        )

    # --- writing --------------------------------------------------------------------------------------------

    def _externalize(self, held):
        """Write each settled generation as its record; return the generations the head must still hold."""
        kept = []
        for gen in held:
            if gen["status"] != "pending":
                try:
                    stored = self.records.put(batch_key(gen["fingerprint"], gen["generation"]), gen)
                    if stored == gen or self._same_outcome(stored, gen):
                        self._written[gen["fingerprint"], gen["generation"]] = stored
                        continue
                    log.warning("a different record holds this generation; the head keeps it")
                except RECORD_ERRORS as error:
                    log.warning("batch record not written (%s); the head keeps it", type(error).__name__)
            kept.append(gen)
        return kept

    @staticmethod
    def _same_outcome(stored, gen):
        """A record already there for the same attempt and outcome (an earlier, interrupted write of this one)."""
        try:
            check_generation(stored)
        except Exception:
            return False
        fixed = ("fingerprint", "generation", "owner", "ordinal", "supersedes", "status")
        return all(stored[k] == gen[k] for k in fixed) and payload_digest(stored["payload"]) == payload_digest(gen["payload"])

    def _hint_owner(self, gen):
        # Repeating the identical hint is harmless; a stale hint left by an abandoned reservation is verified away.
        self.records.put(owner_key(gen["owner"]), {"fingerprint": gen["fingerprint"]})

    def _commit(self, view, change):
        writes = []
        if change.attempt is not None:
            writes.append((self.attempts, [attempt_value(change.attempt)]))
        if change.reply is not None:
            writes.append((self.replies, [reply_value(change.reply[0], change.reply[1], change.reply[2], change.reply[3])]))

        def apply(state, refs):
            held = list(change.held)
            if change.reply is not None:
                fingerprint, _owner, number, text = change.reply
                cited = reply_reference(refs[self.replies.name][0].seq, text)
                held = [
                    {**g, "payload": cited} if (g["fingerprint"], g["generation"]) == (fingerprint, number) else g for g in held
                ]
            if change.attempt is not None:
                self._hint_owner(change.attempt)
            records = state.get(RECORDS_KEY)
            state.clear()
            state.update(schema=V2, attempts=change.attempts, superseded=change.superseded, held=self._externalize(held))
            if records:
                state[RECORDS_KEY] = records
            self._check_v2(state)

        commit_with_records(self.journal, view.record, writes, apply)

    def _migrate(self, view):
        """One commit: every attempt, owner and finished generation becomes a record; the head becomes v2."""
        gens = sorted(view.value["held"], key=lambda g: g["ordinal"])

        def apply(state, refs):
            for gen in gens:
                self._hint_owner(gen)
            records = state.get(RECORDS_KEY)
            state.clear()
            state.update(
                schema=V2,
                attempts=view.value["attempts"],
                superseded=view.value["superseded"],
                held=self._externalize(gens),
            )
            if records:
                state[RECORDS_KEY] = records
            self._check_v2(state)

        writes = [(self.attempts, [attempt_value(g) for g in gens])] if gens else []
        commit_with_records(self.journal, view.record, writes, apply)

    def _write(self, plan, *, records_uncertain=True):
        """Run ``plan(view) -> (result, change)``; commit the change, migrating an old ledger first.

        ``records_uncertain=False`` lets an uncertain record write propagate as itself, for a caller that
        has a fallback for it.

        Only pure domain changes are retried after an explicit CAS conflict. Transport ambiguity
        propagates; a successful dispatch claim is NEVER inferred from rereading after an uncertain PUT.
        """
        self._written = {}
        for _ in range(self.max_conflicts):
            view = self._state()
            result, change = plan(view)
            if change is None:
                return result, False
            try:
                if view.legacy:
                    self._migrate(view)
                    continue
                self._commit(view, change)
            except JournalConflict:
                continue
            except JournalUncertain:
                raise BatchUncertain("S3 transaction uncertain; reconcile before provider dispatch") from None
            except RecordUncertain:
                if not records_uncertain:
                    raise
                raise BatchUncertain("S3 record write uncertain; nothing reserved, reconcile before dispatch") from None
            return result, True
        raise BatchConflict("S3 run contention limit reached; retry operation with its same owner")

    @staticmethod
    def _replace(view, gen):
        identity = (gen["fingerprint"], gen["generation"])
        return [gen if (g["fingerprint"], g["generation"]) == identity else g for g in view.value["held"]]

    def _same(self, view, **changes):
        return _Change(changes.pop("held"), view.value["attempts"], view.value["superseded"], **changes)

    # --- the ledger ----------------------------------------------------------------------------------------

    def get(self, fingerprint, *, payload=True):
        """``payload=False`` skips the lake read; a reply held in the lake then shows as None."""
        view = self._state()
        return self._batch(self._current(view, fingerprint), payload=payload)

    def requests(self):
        return self._state().value["attempts"]

    def reserve(self, fingerprint, maximum, owner, *, policy="reuse", retry_failed=False, claim=False):
        """Reserve one paid attempt. ``claim=True`` also takes the dispatch claim in the same commit."""
        validate_request(fingerprint, owner)
        if (
            (maximum is not None and (type(maximum) is not int or maximum < 0))
            or policy not in ("reuse", "cache_only", "refresh")
            or type(claim) is not bool
        ):
            raise ValueError("valid request ceiling or None and policy required")

        def plan(view):
            row = self._current(view, fingerprint)
            if row and row["owner"] == owner:
                return self._batch(row), None
            prior = self._owner_attempt(view, owner)
            if prior and prior["fingerprint"] != fingerprint:
                raise BatchConflict("operation belongs to a different request")
            if prior:
                return self._batch(prior, claimed=True), None
            if row and row["status"] == "pending":
                raise BatchInProgress("shared request in progress; explicit reconciliation required")
            if policy == "cache_only" and (not row or row["status"] != "complete"):
                raise CacheMiss("complete batch not cached")
            if row and row["status"] == "complete" and policy != "refresh":
                return self._batch(row), None
            if (
                row
                and row["status"] != "complete"
                and not retry_failed
                and not (row["status"] == "interrupted" and row["payload"] == ABANDONED)
            ):
                raise RuntimeError("explicit retry required for interrupted batch")
            attempts, superseded = view.value["attempts"], view.value["superseded"]
            if maximum is not None and attempts >= maximum:
                raise RuntimeError("run decision request ceiling reached")
            if row:
                superseded += 1
                number, revision, supersedes = row["generation"] + 1, (row["revision"] or 0) + 1, superseded
            else:
                number, revision, supersedes = 1, 1, None
            fresh = generation(
                fingerprint, number, owner, attempts + 1, supersedes, "pending", None, claim, revision + (1 if claim else 0)
            )
            change = _Change([*view.value["held"], fresh], attempts + 1, superseded, attempt=fresh)
            return self._batch(fresh), change

        result, created = self._write(plan)
        if created and claim:
            self._claims[fingerprint, owner] = result.generation
        return result

    def claim(self, fingerprint, owner):
        validate_request(fingerprint, owner)
        if self._claims.pop((fingerprint, owner), None) is not None:
            return True  # claimed by this handle's own reservation commit

        def plan(view):
            row = self._held_pending(view, fingerprint)
            if not row or row["owner"] != owner or row["claimed"]:
                return False, None
            return True, self._same(view, held=self._replace(view, {**row, "claimed": True, "revision": row["revision"] + 1}))

        return self._write(plan)[0]

    def _plan_settle(self, view, fingerprint, owner, status, payload, lake):
        row = self._current(view, fingerprint)
        if row and row["owner"] == owner and row["status"] == status and same_payload(row["payload"], payload):
            return self._batch(row), None
        if not row or row["owner"] != owner or row["status"] != "pending":
            raise BatchConflict("stale shared request owner")
        if status == "complete" and not row["claimed"]:
            raise BatchConflict("completion requires dispatch claim")
        settled = {**row, "status": status, "payload": payload, "revision": row["revision"] + 1}
        written = self._written.get((fingerprint, row["generation"]))
        if written is not None and self._same_outcome(written, settled):
            # A try of this very settlement wrote its record, then lost the head race: reuse it as is.
            settled, lake = written, False
        reply = (fingerprint, owner, row["generation"], payload) if lake else None
        result = BatchRecord(fingerprint, status, payload, row["generation"], owner, row["claimed"])
        return result, self._same(view, held=self._replace(view, settled), reply=reply)

    def settle(self, fingerprint, owner, status, payload=None):
        validate_request(fingerprint, owner)
        if status not in ("complete", "failed", "interrupted") or status == "complete" and payload is None:
            raise ValueError("valid settlement required")
        if payload is not None:
            json.loads(payload)
        if self.replies is not None and status == "complete":
            try:
                plan = lambda view: self._plan_settle(view, fingerprint, owner, status, payload, True)  # noqa: E731
                return self._write(plan, records_uncertain=False)[0]
            except RECORD_ERRORS as error:
                # Only reply-lake failures land here (journal outcomes are other exceptions). The provider has
                # been paid, so keep the reply inline instead of dropping it.
                log.warning("reply record not written (%s); keeping the reply inline", type(error).__name__)
        return self._write(lambda view: self._plan_settle(view, fingerprint, owner, status, payload, False))[0]

    def operation_mark(self):
        return self._state().value["attempts"]

    def pending_operations(self):
        view = self._state()
        pending = [g for g in view.value["held"] if g["status"] == "pending"]
        return [(g["ordinal"], g["fingerprint"], g["owner"]) for g in sorted(pending, key=lambda g: g["ordinal"])]

    def abandon_learning(self, fingerprint, owner):
        validate_request(fingerprint, owner)

        def plan(view):
            row = self._held_pending(view, fingerprint)
            if not row or row["owner"] != owner:
                raise BatchConflict("stale shared request owner")
            if row["claimed"]:
                raise BatchUncertain("claimed cloud request requires explicit provider reconciliation; never automatically repay")
            settled = {**row, "status": "interrupted", "payload": ABANDONED, "revision": row["revision"] + 1}
            return self._batch(settled), self._same(view, held=self._replace(view, settled))

        return self._write(plan)[0]

    # --- audit: every generation, read by covered sequence and by key ---------------------------------------

    def _every_generation(self, view):
        if view.legacy:
            return list(view.value["held"])
        gens = []
        for _seq, attempt in self.attempts.read_covered(coverage_in(view.value, self.attempts)):
            fingerprint, number = attempt["fingerprint"], attempt["generation"]
            gen = view.held.get(fingerprint, {}).get(number) or self._keyed(view, fingerprint, number)
            if gen is None or attempt_value(gen) != attempt:
                raise RecordUnavailable("an attempt's generation is missing; never use an index as fallback")
            gens.append(gen)
        if len(gens) != view.value["attempts"] or sorted(g["ordinal"] for g in gens) != list(range(1, len(gens) + 1)):
            raise RecordCorrupt("attempt records do not match the ledger's attempt count")
        return gens

    def page(self, *, after=None, limit=100):
        if type(limit) is not int or limit < 1 or after is not None and not isinstance(after, str):
            raise ValueError("positive page size required")
        view = self._state()
        snapshot = snapshot_of(self._every_generation(view))
        current = {row[0]: row[3] for row in snapshot["batches"]}
        keys = [key for key in sorted(current) if key > (after or "")][: limit + 1]
        rows = [self._batch(self._current(view, key)) for key in keys[:limit]]
        return rows, keys[limit - 1] if len(keys) > limit else None

    def export(self):
        """The portable snapshot, with every raw reply back inline, whichever way it is stored."""
        view = self._state()
        gens = [{**g, "payload": self._text(g)} for g in self._every_generation(view)]
        snapshot = snapshot_of(gens)
        validate_snapshot(snapshot)
        return snapshot

    def restore(self, snapshot):
        snapshot = deepcopy(snapshot)
        validate_snapshot(snapshot)
        restored = view_of_snapshot(snapshot)
        for _ in range(self.max_conflicts):
            view = self._state()
            if view.value["attempts"] or view.value["held"]:
                raise BatchConflict("restore requires empty destination")
            target = _View(view.record, restored, True)
            try:
                self._migrate(target)
                return None
            except JournalConflict:
                continue
            except (JournalUncertain, RecordUncertain):
                raise BatchUncertain("S3 transaction uncertain; reconcile before provider dispatch") from None
        raise BatchConflict("S3 run contention limit reached; retry")

    # --- the one-time conversion of v1 ledgers whose replies are still embedded (record_lake_conversion) --

    @staticmethod
    def _embedded_slots(state):
        """Complete rows whose reply text is still inside a v1 ledger, in conversion order: batches, then history."""
        if state.get("schema") == V2:
            return []
        slots = [(r, 2, r[0], r[4], r[3]) for r in state["batches"] if r[1] == "complete" and isinstance(r[2], str)]
        slots += [(r, 4, r[1], r[5], r[2]) for r in state["history"] if r[3] == "complete" and isinstance(r[4], str)]
        return slots

    def audit_replies(self):
        """Read-only: the head record and the embedded replies as ``(fingerprint, owner, generation, text)``."""
        record = self.journal.read()
        self._check(record.value)
        return record, [(f, o, g, row[index]) for row, index, f, o, g in self._embedded_slots(record.value)]

    def preview_replies_conversion(self, record):
        """(the ledger as it would stand after the conversion, bytes of the records it would write). Writes nothing."""
        slots = self._embedded_slots(record.value)
        state = deepcopy(record.value)
        coverage = coverage_in(state, self.replies)
        seqs = range(coverage.last + 1, coverage.last + 1 + len(slots))
        for (row, index, *_), seq in zip(self._embedded_slots(state), seqs):
            row[index] = reply_reference(seq, row[index])
        state[RECORDS_KEY] = {
            **state.get(RECORDS_KEY, {}),
            self.replies.name: Coverage(coverage.last + len(slots), coverage.skipped).to_json(),
        }
        size = sum(
            self.replies.encoded_size(seq, reply_value(f, o, g, row[index])) for (row, index, f, o, g), seq in zip(slots, seqs)
        )
        return state, size

    def convert_embedded_replies(self, *, check=None):
        """Move every reply still embedded in a v1 ledger into the reply lake, in one commit.

        Reply records are written first, then ONE commit swaps each embedded text for its
        ``{seq, sha256, length}`` note and stores the coverage. Reservation, claims, attempts and ordering
        are untouched. Returns ``RepliesConverted`` or None when nothing is embedded (idempotent; a v2
        ledger has nothing embedded).
        """
        if self.replies is None:
            raise ValueError("a reply record lake is required to convert embedded replies")
        for _ in range(self.max_conflicts):
            record = self.journal.read()
            self._check(record.value)
            slots = self._embedded_slots(record.value)
            if not slots:
                return None
            originals = [(f, o, g, row[index]) for row, index, f, o, g in slots]

            def apply(state, refs):
                cited = iter(refs[self.replies.name])
                for row, index, *_ in self._embedded_slots(state):
                    row[index] = reply_reference(next(cited).seq, row[index])
                self._check_v1(state)

            try:
                committed, refs = commit_with_records(
                    self.journal,
                    record,
                    [(self.replies, [reply_value(*original) for original in originals])],
                    apply,
                    check=check,
                )
            except JournalConflict:
                continue
            except JournalUncertain:
                raise BatchUncertain("S3 transaction uncertain; reconcile before provider dispatch") from None
            return RepliesConverted(originals, refs[self.replies.name], record, committed)
        raise BatchConflict("S3 run contention limit reached; retry")

    def close(self):
        pass  # Injected SDK clients belong to the host.

"""Named core runtime repositories over a canonical S3 transaction.

An explicit owner holds this classifier until release. There is no timeout-based
steal: an uncertain/crashed holder needs reconciliation. Every read and CAS
mutation checks the same owner/fence stored with its data, never a DDB lease.

Layout (``cyclotron-s3-runtime/v2``). A commit rewrites only a small head, so what one decision writes does
not depend on how many came before it:

- The head keeps the owner and fence, counters, the answer reservations still pending, the events lake
  coverage, and a reference for everything else.
- Answers, answer history, each item's unfinished cycles and other per-key state are one keyed document per
  key (``keyed_records.py``), found with one GET. An entry counts only once the head covers the commit's
  chunk record, so a commit whose head never moved leaves nothing visible.
- Every other domain and every state value is one content-addressed payload, rewritten only when it
  changes. A commit that leaves it alone carries its reference.
- The events a commit publishes are ONE lake record (a chunk) holding them in order, not one per event.

A ``v1`` head (the whole runtime inline, one lake record per event) is read as it is and converted on its
first write: its rows move into keyed documents and its earlier event records stay where they are, ahead of the
chunks, with the same ids.
"""

import bisect
import json
import time
import weakref
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256

from .active_labels import (
    ACTIVE_LABELS_STATE_KEY,
    FEEDBACK_EVENT_KIND,
    dump_active_labels,
    load_active_labels,
    rebuild_active_labels,
    record_feedback_event,
)
from .cycles import CYCLE_EVENT_KINDS, note_cycle_event, unfinished_cycles_from
from .journal import CanonicalJournal, Journal, commit_with_contract, decode, encode, uuid
from .keyed_records import keyed_documents_for, resolve
from .learning_storage import ARTIFACTS, artifact_table
from .record_lake import (
    RECORDS_KEY,
    Coverage,
    FileRecordLake,
    RecordCorrupt,
    Records,
    S3RecordLake,
    commit_with_records,
    coverage_in,
)
from .storage_errors import StorageConflict
from .storage_payloads import FilePayloads, PayloadRef, S3Payloads
from .value_records import REF as VALUES_REF
from .value_records import canonical


class RuntimeOwnershipError(RuntimeError):
    pass


DICT_FIELDS = ("state", "answers", "rounds", "features", "controls", "causes")
LIST_FIELDS = (
    "events",
    "answer_history",
    "evaluations",
    "verdicts",
    "elements",
    "shadow_set",
    "shadow_scored",
    "shadow_looks",
    "shadow_incumbents",
)


EVENTS_DOMAIN = "runtime"
# The highest cycle number seen, kept in the small control state so numbering a cycle never scans history.
CYCLE_MARK_KEY = "cycle-number-high-water"
# Where a v1 head kept every item's unfinished cycles: {item_id: [[cycle_id, cycle_number, reason, failed], ...]}.
# v2 keeps each item's rows in its own ``cycles`` keyed document instead.
CYCLE_INDEX_KEY = "unfinished-cycles"
PAGE = 100  # records fetched together; one page is also the most a reader holds beyond what it was asked for
READ_WORKERS = 8
CACHE_BYTES = 32_000_000  # records are immutable, so a handle may keep what it has read; bounded, never evicted

SCHEMA_V1 = "cyclotron-s3-runtime/v1"
SCHEMA = "cyclotron-s3-runtime/v2"
ANSWERS, HISTORY, CYCLES = "answers", "answer-history", "cycles"
RESERVED_MAPS = (ANSWERS, HISTORY, CYCLES)
# Domains held in the head itself: the pending answer reservations (settled answers are keyed documents), the
# events embedded by the oldest format, and the answer history a v1 head held (v2 keeps it by key).
INLINE_DOMAINS = ("answers", "events", "answer_history")
REF_DOMAINS = tuple(k for k in (*DICT_FIELDS, *LIST_FIELDS, "artifacts") if k not in (*INLINE_DOMAINS, "state"))
INLINE_STATE = (CYCLE_MARK_KEY,)  # the storage's own one-number summary stays in the head
COUNTERS = {"answer_history": 0, "answer_mark": 0, "cycles_indexed": False}


def events_kind(journal):
    """One scope holds many runtimes (one per classifier), so each runtime journal gets its own lake kind."""
    return "events-" + journal.namespace.rsplit("/", 1)[-1]


def runtime_events_lake(journal):
    """The events lake of one runtime journal, on its payload adapter (and so its scope, bucket and owner)."""
    payloads, kind = journal.payloads, events_kind(journal)
    if isinstance(payloads, S3Payloads):
        return S3RecordLake(payloads, EVENTS_DOMAIN, kind)
    if isinstance(payloads, FilePayloads):
        return FileRecordLake(payloads, EVENTS_DOMAIN, kind)
    raise ValueError("an S3 or file payload adapter is required for the runtime events lake")


# Content-addressed payloads known to exist, per payload store, shared by every handle in the process: the
# objects are immutable and never deleted, so a digest once stored or read is never written again (six runtimes
# deciding the same article write its values once). Keyed weakly by the injected client.
_STORED_BY_CLIENT = weakref.WeakKeyDictionary()
_STORED_BY_ROOT = {}


def _stored_digests(payloads):
    if isinstance(payloads, S3Payloads):
        try:
            per_client = _STORED_BY_CLIENT.setdefault(payloads.client, {})
        except TypeError:
            return set()  # a client that cannot be weakly referenced shares nothing
        return per_client.setdefault((payloads.bucket, payloads.scope.key), set())
    if isinstance(payloads, FilePayloads):
        return _STORED_BY_ROOT.setdefault((str(payloads.root), payloads.scope.key), set())
    return set()


def ref_of(content):
    """``[digest, length]`` of content-addressed bytes, the form a head stores."""
    return [sha256(content).hexdigest(), len(content)]


def _empty_data():
    return {**{k: {} for k in DICT_FIELDS}, **{k: [] for k in LIST_FIELDS}, "artifacts": {k: {} for k in ARTIFACTS}}


EMPTY_DATA = _empty_data()


def empty():
    return {
        "schema": SCHEMA,
        "owner": None,
        "fence": 0,
        "ordinal": 0,
        "data": _empty_data(),
        "refs": {},
        "maps": {},
        "counters": dict(COUNTERS),
    }


def json_text(value):
    if not isinstance(value, str):
        raise ValueError("raw JSON text required")
    decode(value)
    return value


def _text(value):
    return encode(value).decode()


def _is_ref(value):
    return isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and type(value[1]) is int


@dataclass(frozen=True)
class _Head:
    """A committed journal record and the working form of its value."""

    record: object
    value: dict


class S3RuntimeStores:
    requires_durable_optimizer = True

    def __init__(self, journal, *, max_conflicts=8, cache_owned=False, coalesce_events=None, events_lake=None):
        if not isinstance(journal, Journal) or type(max_conflicts) is not int or max_conflicts < 1:
            raise ValueError("canonical journal and a positive conflict retry count required")
        if events_lake is not None and (
            not isinstance(events_lake, Records)
            or events_lake.name != f"{EVENTS_DOMAIN}/{events_kind(journal)}"
            or events_lake.scope != journal.boundary[0]
        ):
            raise ValueError("the runtime events lake must be this journal's own, in its own scope")
        self._lake_name = f"{EVENTS_DOMAIN}/{events_kind(journal)}"
        # With a lake, events are write-once records covered by the control state, not rows of the snapshot.
        # Events already embedded in an existing snapshot stay readable as a legacy prefix before the lake's.
        self.events_lake = events_lake
        self._record_cache = {}
        self._record_cache_bytes = 0
        self._held = []  # events appended but not yet published, oldest first (coalescing only)
        self._staged = None  # the events the commit being built will write
        self.journal = journal
        # One document per key beside the journal. Its entries count only under the head's lake coverage, so
        # keyed state needs the events lake; a handle without one keeps keyed state inline in the head.
        self.keyed_documents = keyed_documents_for(journal) if events_lake is not None else None
        self._documents = {}  # (kind, key) -> Document or None, as last read or written by this handle
        # Keyed changes written ahead of the commit that will cover them (a conversion's rows), in the background
        # so many runtimes convert at once. Each is (future of its chunk sequence, its changes); one worker
        # claims their sequences in order.
        self._prepared = []
        self._prepared_values = {}  # (kind, key) -> text, visible to this handle until the covering commit
        self._stored = _stored_digests(journal.payloads)
        self._pending_blobs = {}  # item values referenced by held events, written with the commit that publishes them
        self._background = None
        self._blobs = {}  # digest -> bytes of content-addressed values read or written; they never change
        self._seen = None  # the last head read, so an unchanged head is not decoded again
        self.max_conflicts = max_conflicts
        self.authority = None
        self.poisoned = False
        self.cache_owned = cache_owned
        # A trace event, an answer's completion and a derived summary are not grants. An owning handle may hold
        # them in memory and publish them with the next durable change (an answer reservation, a state write)
        # or at release, in the same atomic commit. The reservation that grants a paid call is never deferred.
        # Off unless the handle owns a cache.
        self.coalesce_events = cache_owned if coalesce_events is None else bool(coalesce_events) and cache_owned
        self._cached = None
        self._working = None
        self.runtime_storage = _State(self)
        self.runtime_cache = _Cache(self)
        self.learning_storage = _Learning(self)
        self.feature_storage = _Payloads(self, "features")
        self.control_storage = _Payloads(self, "controls")
        self.hypothesis_storage = _Hypothesis(self)
        self.shadow_storage = _Shadow(self)
        self.pending_storage = _Pending(self)

    # Shapes ------------------------------------------------------------------
    def _check_common(self, value, keys):
        if not isinstance(value, dict) or set(value) - {RECORDS_KEY, "chunks"} != keys:
            raise ValueError("invalid canonical runtime")
        if RECORDS_KEY in value:
            held = value[RECORDS_KEY]
            if not isinstance(held, dict) or set(held) - {self._lake_name}:
                raise ValueError("invalid record coverage")
            for coverage in held.values():
                Coverage.from_json(coverage)
        if type(value["fence"]) is not int or value["fence"] < 0 or type(value["ordinal"]) is not int or value["ordinal"] < 0:
            raise ValueError("invalid runtime epoch")
        if value["owner"] is not None:
            uuid(value["owner"])
        if "chunks" in value:
            chunks = value["chunks"]
            if (
                not isinstance(chunks, dict)
                or set(chunks) != {"from", "last"}
                or type(chunks["from"]) is not int
                or chunks["from"] < 1
                or type(chunks["last"]) is not int
                or chunks["last"] < 0
            ):
                raise ValueError("invalid event chunk index")
        data = value["data"]
        if not isinstance(data, dict) or set(data) != set(EMPTY_DATA):
            raise ValueError("invalid runtime domains")
        if any(not isinstance(data[k], dict) for k in DICT_FIELDS) or any(not isinstance(data[k], list) for k in LIST_FIELDS):
            raise ValueError("invalid runtime records")
        if (
            not isinstance(data["artifacts"], dict)
            or set(data["artifacts"]) != set(ARTIFACTS)
            or any(not isinstance(v, dict) for v in data["artifacts"].values())
        ):
            raise ValueError("invalid learning kinds")

    def _check_stored(self, value):
        """A head as the journal holds it: v1 (everything inline) or v2 (references)."""
        schema = value.get("schema") if isinstance(value, dict) else None
        if schema == SCHEMA_V1:
            self._check_common(value, {"schema", "owner", "fence", "ordinal", "data"})
            if "chunks" in value:
                raise ValueError("invalid canonical runtime")
            return
        if schema != SCHEMA:
            raise ValueError("invalid canonical runtime")
        self._check_common(value, {"schema", "owner", "fence", "ordinal", "data", "refs", "maps", "counters"})
        refs, maps, counters = value["refs"], value["maps"], value["counters"]
        if (
            not isinstance(refs, dict)
            or set(refs) - set(REF_DOMAINS)
            or any(not _is_ref(r) or value["data"][d] != EMPTY_DATA[d] for d, r in refs.items())
        ):
            raise ValueError("invalid runtime references")
        if any(not isinstance(v, str) and not _is_ref(v) for v in value["data"]["state"].values()):
            raise ValueError("invalid runtime state")
        if value["data"]["answer_history"] or any(r[0] != "pending" for r in value["data"]["answers"].values()):
            raise ValueError("completed answers and answer history live by key")
        if not isinstance(maps, dict) or any(
            not isinstance(rows, dict) or any(not isinstance(v, str) for v in rows.values()) for rows in maps.values()
        ):
            raise ValueError("invalid inline keyed state")
        if not isinstance(counters, dict) or set(counters) != set(COUNTERS) or type(counters["cycles_indexed"]) is not bool:
            raise ValueError("invalid runtime counters")
        if any(type(counters[k]) is not int or counters[k] < 0 for k in ("answer_history", "answer_mark")):
            raise ValueError("invalid runtime counters")

    def _validate(self, value):
        """A working value: every domain resolved, keyed state as inline rows plus the changes not yet written."""
        if not isinstance(value, dict) or value.get("schema") != SCHEMA:
            raise ValueError("invalid canonical runtime")
        self._check_common(value, {"schema", "owner", "fence", "ordinal", "data", "maps", "counters"})
        if any(not isinstance(v, str) for v in value["data"]["state"].values()):
            raise ValueError("invalid runtime state")
        maps = value["maps"]
        if not isinstance(maps, dict) or any(
            not isinstance(m, dict)
            or set(m) != {"inline", "changes"}
            or not isinstance(m["inline"], dict)
            or not isinstance(m["changes"], dict)
            for m in maps.values()
        ):
            raise ValueError("invalid runtime keyed state")
        if not isinstance(value["counters"], dict) or set(value["counters"]) != set(COUNTERS):
            raise ValueError("invalid runtime counters")

    # Heads -------------------------------------------------------------------
    def _get_blobs(self, refs):
        """Bytes of content-addressed values, fetched together; each is read once per handle."""
        missing = list({r[0]: r for r in refs if r[0] not in self._blobs}.values())
        get = self.journal.payloads.get
        scope = self.journal.payloads.scope
        if len(missing) > 1:
            with ThreadPoolExecutor(self._lanes(len(missing))) as pool:
                fetched = list(pool.map(lambda r: get(PayloadRef(scope, r[0], r[1])), missing))
        else:
            fetched = [get(PayloadRef(scope, r[0], r[1])) for r in missing]
        for ref, content in zip(missing, fetched):
            self._blobs[ref[0]] = content

    def _materialize(self, stored):
        """The working form of a stored head. A v1 head is converted here; its first write stores the result."""
        self._check_stored(stored)
        value = {k: stored[k] for k in ("owner", "fence", "ordinal")}
        value["schema"] = SCHEMA
        for key in (RECORDS_KEY, "chunks"):
            if key in stored:
                value[key] = deepcopy(stored[key])
        data = deepcopy(stored["data"])
        if stored["schema"] == SCHEMA_V1:
            maps, counters = {}, dict(COUNTERS)
            answers = data["answers"]
            data["answers"] = {k: r for k, r in answers.items() if r[0] == "pending"}
            done = {k: _text(r) for k, r in answers.items() if r[0] != "pending"}
            if done:
                maps[ANSWERS] = {"inline": {}, "changes": done}
            counters["answer_mark"] = max((r[2] for r in answers.values()), default=0)
            history, data["answer_history"] = data["answer_history"], []
            if history:
                maps[HISTORY] = {"inline": {}, "changes": {str(r[0]): _text(r) for r in history}}
            counters["answer_history"] = max((r[0] for r in history), default=0)
            if self.events_lake is not None and CYCLE_INDEX_KEY in data["state"]:
                index = json.loads(data["state"].pop(CYCLE_INDEX_KEY))
                if index:
                    maps[CYCLES] = {"inline": {}, "changes": {item: _text(rows) for item, rows in index.items()}}
                counters["cycles_indexed"] = True
        else:
            refs = list(stored["refs"].values()) + [v for v in data["state"].values() if _is_ref(v)]
            self._get_blobs(refs)
            for domain, ref in stored["refs"].items():
                data[domain] = decode(self._blobs[ref[0]])
            for key, held in data["state"].items():
                if _is_ref(held):
                    data["state"][key] = self._blobs[held[0]].decode()
            maps = {name: {"inline": dict(rows), "changes": {}} for name, rows in stored["maps"].items()}
            counters = dict(stored["counters"])
        value["data"], value["maps"], value["counters"] = data, maps, counters
        self._validate(value)
        return value

    def _head(self, record):
        seen = self._seen
        if seen is not None and (seen.record.revision, seen.record.commit_id) == (record.revision, record.commit_id):
            head = _Head(record, seen.value)
        else:
            # A head this handle did not just commit may come with keyed documents it has not read.
            self._documents = {}
            head = _Head(record, self._materialize(record.value))
        self._seen = head
        return head

    def _read_record(self, known=None):
        if known is not None and isinstance(self.journal, CanonicalJournal):
            return self.journal.read(known=known)
        return self.journal.read()

    def _fresh(self):
        return self._head(self._read_record(self._seen.record if self._seen is not None else None))

    def _store(self, value, head):
        """(the head to commit for ``value``, {digest: bytes} it references that must exist first)."""
        prior, prior_value = head.record.value, head.value
        prior_refs = prior.get("refs", {}) if prior["schema"] == SCHEMA else {}
        blobs, data, refs = {}, {}, {}

        def blob(content):
            ref = ref_of(content)
            blobs[ref[0]] = content
            return ref

        for domain, held in value["data"].items():
            if domain == "state":
                state = {}
                for key, text in held.items():
                    old = prior["data"]["state"].get(key)
                    if key in INLINE_STATE:
                        state[key] = text
                    elif _is_ref(old) and prior_value["data"]["state"].get(key) == text:
                        state[key] = old
                    else:
                        state[key] = blob(text.encode())
                data["state"] = state
            elif domain in REF_DOMAINS and held != EMPTY_DATA[domain]:
                if domain in prior_refs and prior_value["data"][domain] == held:
                    refs[domain] = prior_refs[domain]
                else:
                    refs[domain] = blob(encode(held))
                data[domain] = deepcopy(EMPTY_DATA[domain])
            else:
                data[domain] = deepcopy(held)
        maps = {}
        for name, held in value["maps"].items():
            rows = dict(held["inline"])
            for key, text in held["changes"].items():
                # With the lake the change goes to the key's document, so the head must not keep an older copy.
                if text is None or self.keyed_documents is not None:
                    rows.pop(key, None)
                else:
                    rows[key] = text
            if rows:
                maps[name] = rows
        stored = {k: value[k] for k in ("schema", "owner", "fence", "ordinal")}
        for key in (RECORDS_KEY, "chunks"):
            if key in value:
                stored[key] = deepcopy(value[key])
        stored.update(data=data, refs=refs, maps=maps, counters=dict(value["counters"]))
        return stored, blobs

    def _publish(self, blobs):
        """Write the content-addressed values a head will name, together, before the head moves."""
        contents = [content for digest, content in blobs.items() if digest not in self._blobs and digest not in self._stored]
        put = self.journal.payloads.put
        if len(contents) > 1:
            with ThreadPoolExecutor(self._lanes(len(contents))) as pool:
                list(pool.map(put, contents))
        elif contents:
            put(contents[0])
        self._blobs.update(blobs)
        self._stored.update(blobs)

    def _settled(self, value, committed):
        """The working value of a head just committed from ``value``: keyed changes written, nothing pending."""
        stored = committed.value
        for name in value["maps"]:
            value["maps"][name] = {"inline": dict(stored["maps"].get(name, {})), "changes": {}}
        for key in (RECORDS_KEY, "chunks"):
            if key in stored:
                value[key] = deepcopy(stored[key])
        self._validate(value)
        head = _Head(committed, value)
        self._seen = head
        return head

    # Ownership ---------------------------------------------------------------
    def initialize(self):
        self._head(self.journal.initialize(empty()))  # validated, and remembered so acquiring reads only the head

    def _owned(self, value):
        if self.authority is None or (value["owner"], value["fence"]) != self.authority:
            raise RuntimeOwnershipError("runtime execution ownership unavailable; no timeout takeover")

    def _holding(self):
        return (self._working is not None or bool(self._held)) and self._cached is not None

    def flush(self):
        """Publish any held changes now. A no-op when nothing is held."""
        if self._working is not None or self._held:
            self._change(lambda value: None)

    def refresh(self):
        """Drop what this handle read and check it still owns the runtime.

        Held changes stay held while the head is exactly the one they were made on (one head GET proves it),
        so a guarded call does not cost a commit. A head that moved under held changes poisons the handle.
        """
        if self.poisoned:
            raise RuntimeOwnershipError("uncertain runtime write; reopen/reconcile before continuing")
        if self._holding():
            cached = self._cached
            record = self._read_record(cached.record)
            if (record.revision, record.commit_id) != (cached.record.revision, cached.record.commit_id):
                self._poison()
                raise RuntimeOwnershipError("runtime head moved while changes were held; reopen/reconcile")
            self._owned(cached.value)
            self._cached = _Head(record, cached.value)
            return
        self._cached = None
        head = self._fresh()
        self._owned(head.value)
        self._cached = head

    def _read_value(self):
        if self.poisoned:
            raise RuntimeOwnershipError("uncertain runtime write; reopen/reconcile before continuing")
        head = self._cached if self.cache_owned and self._cached is not None else self._fresh()
        value = self._working if self._working is not None else head.value
        self._validate(value)
        self._owned(value)
        return value

    def _read(self):
        return self._read_value()["data"]

    def _commit(self, head, value, staged):
        stored, blobs = self._store(value, head)
        self._publish({**blobs, **self._pending_blobs})
        keyed = (
            [(name, key, text) for name, held in value["maps"].items() for key, text in held["changes"].items()]
            if self.keyed_documents is not None
            else []
        )
        claimed, unwritten = self._take_prepared()
        keyed += unwritten  # a preparation that failed is written again, by this commit
        if not staged and not keyed and not claimed:
            return self._settled(value, commit_with_contract(self.journal, head.record, stored, invalidate=self._poison))
        chunks = value.get("chunks")
        first = _Events(self, value).last_id + 1
        before = coverage_in(value, self.events_lake)
        # Prepared chunks were written above the head's coverage; whatever lies between them (another attempt's
        # abandoned record) is skipped, as an append would skip it.
        top = max([before.last, *claimed])
        coverage = Coverage(
            top, tuple(sorted({*before.skipped, *(n for n in range(before.last + 1, top + 1) if n not in claimed)}))
        )
        seqs = list(claimed)
        if staged or keyed:
            # The chunk names the keyed changes it stamps (their digest). A retry of the identical commit adopts
            # the identical record at the same sequence; an attempt with other changes can never reuse an
            # abandoned attempt's sequence, so entries stamped with it never become covered.
            changes = sha256(encode(sorted([name, key, text] for name, key, text in keyed))).hexdigest()
            chunk = {"first": first, "events": list(staged), "commit": changes}
            # The chunk first (harmless if abandoned), then each key's document gains an entry stamped with its
            # sequence, and only then the head moves: they count only together with the change they belong to.
            coverage, refs = self.events_lake.append(coverage, [chunk])
            seqs.append(refs[0].seq)
            self._store_keyed(keyed, before, refs[0].seq)
            self._remember(refs[0].seq, chunk)
        stored[RECORDS_KEY] = {**stored.get(RECORDS_KEY, {}), self._lake_name: coverage.to_json()}
        stored["chunks"] = {"from": chunks["from"] if chunks else min(seqs), "last": first + len(staged) - 1}
        try:
            committed = commit_with_contract(self.journal, head.record, stored, invalidate=self._poison)
        except StorageConflict:
            if claimed:
                # Prepared rows are stamped for this head only; they cannot be carried onto a moved one.
                self._poison()
                raise RuntimeOwnershipError("runtime head moved during a conversion; reopen/reconcile") from None
            raise
        self._prepared_values = {}
        self._pending_blobs = {}
        return self._settled(value, committed)

    def _change(self, fn, *, ownership=True, recovery=False, defer=False, start=None):
        """``start``: a head to try first instead of reading one; its conditional write proves it current."""
        if self.poisoned and not recovery:
            raise RuntimeOwnershipError("uncertain runtime write; reopen/reconcile before continuing")
        for _ in range(self.max_conflicts):
            held = self._holding()
            head = (
                start
                if start is not None
                else self._cached
                if held or (self.cache_owned and self._cached is not None and ownership and not recovery)
                else self._fresh()
            )
            start = None
            base = self._working if self._working is not None and held else head.value
            self._validate(base)
            if ownership:
                self._owned(base)
            value = deepcopy(base)
            self._staged = list(self._held)
            try:
                result = fn(value)
                staged = self._staged
            finally:
                self._staged = None
            self._validate(value)
            if defer and self.coalesce_events and ownership and not recovery:
                if value != head.value:
                    self._working = value
                self._held = staged
                if self._cached is None:
                    self._cached = head
                return result
            if value == head.value and not staged:
                self._working = None
                self._held = []
                return result
            try:
                self._cached = self._commit(head, value, staged)
                self._working = None
                self._held = []
            except StorageConflict:
                self._cached = None
                if held:
                    # Changes held in memory cannot be replayed onto a head someone else moved.
                    self._poison()
                    raise RuntimeOwnershipError("runtime head moved while events were held; reopen/reconcile") from None
                continue
            return result
        raise RuntimeOwnershipError("runtime contention limit reached")

    def _poison(self):
        self.poisoned = True
        self._cached = None
        self._working = None
        self._held = []
        self._prepared, self._prepared_values = [], {}
        self._pending_blobs = {}

    def change(self, fn, *, defer=False):
        return self._change(lambda value: fn(value["data"], value), defer=defer)

    def acquire(self, owner, *, resume=False):
        """Take ownership. ``resume=True`` also re-takes a runtime this very ``owner`` id was left holding by a
        run that stopped without releasing (the fence still moves, so the older run is fenced out). It never
        takes over a different owner."""
        uuid(owner)
        if self.authority is not None:
            raise RuntimeOwnershipError("handle already owns execution")

        def apply(value):
            if value["owner"] is not None and not (resume and value["owner"] == owner):
                raise RuntimeOwnershipError("runtime already owned; reconcile crashed owner explicitly")
            value["owner"] = owner
            value["fence"] += 1
            return owner, value["fence"]

        # The head initialize() just read and validated is tried first: the acquiring commit is conditional on
        # it, so a head that moved since fails that write and the next attempt reads the live one. It is used
        # only when it shows the runtime free to take, so a stale "owned" can never refuse a free runtime.
        seen = self._seen if not self.poisoned else None
        if seen is not None and (
            seen.record.value.get("schema") != SCHEMA
            or not (seen.value["owner"] is None or (resume and seen.value["owner"] == owner))
        ):
            seen = None
        if self.keyed_documents is not None and not self.poisoned and seen is None:
            record = self.journal.read()
            if isinstance(record.value, dict) and record.value.get("schema") == SCHEMA_V1:
                # Take an old-layout runtime without converting it yet: the head stays v1 (every row still in
                # it), and its rows are written to keyed documents in the background while the caller goes on,
                # so several runtimes convert at once. The next commit covers them and stores the v2 head.
                self._check_stored(record.value)
                value = deepcopy(record.value)
                result = apply(value)
                committed = commit_with_contract(self.journal, record, value, invalidate=self._poison)
                self.authority = result
                head = self._head(committed)
                self._cached = head
                self._prepare_from(head.value)
                return
        # Set local permission ONLY after confirmed CAS success.
        self.authority = self._change(apply, ownership=False, start=seen)

    def close(self):
        if self.authority is None:
            return

        def release(value):
            value["owner"] = None

        try:
            self._change(release, recovery=True)
        finally:
            if self._background is not None:
                self._background.shutdown(wait=True)
                self._background = None
            self.authority = None
            self._cached = None
            self._working = None
            self._held = []

    # Keyed state ---------------------------------------------------------------
    def _document(self, name, key):
        if (name, key) not in self._documents:
            self._documents[(name, key)] = self.keyed_documents.load(name, key)
        return self._documents[(name, key)]

    def _lanes(self, count):
        """How many requests to run at once: all of them, up to the connections the injected client keeps."""
        config = getattr(getattr(self.journal.payloads, "client", None), "meta", None)
        pool = getattr(getattr(config, "config", None), "max_pool_connections", None)
        return max(1, min(count, pool)) if type(pool) is int and pool > 0 else max(1, count)

    def _store_keyed(self, changes, coverage, at):
        """Write each change's document entry, all at once. A document not read yet is written create-only
        (no GET first); one that turns out to exist is read and merged by the conditional write itself."""
        write = (
            lambda change: self.keyed_documents.store(  # noqa: E731
                change[0], change[1], self._documents.get(change[:2]), coverage, at, change[2]
            )
        )
        if len(changes) > 1:
            with ThreadPoolExecutor(self._lanes(len(changes))) as pool:
                written = list(pool.map(write, changes))
        else:
            written = [write(change) for change in changes]
        for (name, key, _), document in zip(changes, written):
            self._documents[(name, key)] = document

    def _prepare(self, changes, value):
        """Write keyed changes ahead of the commit that will cover them, in the background.

        A chunk record with no events claims a sequence above the head's coverage (and above any preparation
        before it), then every change's document gains an entry stamped with it. None of it counts until the
        next commit of this handle covers that sequence; until then this handle reads the changes from memory.
        """
        if not changes:
            return
        for name, key, text in changes:
            self._prepared_values[(name, key)] = text
        before = coverage_in(value, self.events_lake)
        first = _Events(self, value).stored_last + 1
        digest = sha256(encode(sorted([name, key, text] for name, key, text in changes))).hexdigest()
        earlier = self._prepared[-1][0] if self._prepared else None

        def run():
            floor = earlier.result() if earlier is not None else before.last
            claim = Coverage(max(floor, before.last), tuple(n for n in before.skipped if n <= max(floor, before.last)))
            _, refs = self.events_lake.append(claim, [{"first": first, "events": [], "commit": digest}])
            self._store_keyed(changes, before, refs[0].seq)
            return refs[0].seq

        if self._background is None:
            self._background = ThreadPoolExecutor(1)
        self._prepared.append((self._background.submit(run), list(changes)))

    def _prepare_from(self, value):
        """Move a converted head's pending keyed rows out of ``value`` into a background preparation."""
        changes = [(name, key, text) for name, held in value["maps"].items() for key, text in held["changes"].items()]
        for held in value["maps"].values():
            held["changes"] = {}
        self._prepare(changes, value)

    def _take_prepared(self):
        """(sequences of the preparations that landed, changes of those that did not), waiting for all of them."""
        claimed, unwritten = [], []
        for future, changes in self._prepared:
            try:
                claimed.append(future.result())
            except Exception:
                unwritten += changes
        self._prepared = []
        return claimed, unwritten

    def _map_get(self, value, name, key):
        held = value["maps"].get(name)
        if held is not None and key in held["changes"]:
            return held["changes"][key]
        if (name, key) in self._prepared_values:
            return self._prepared_values[(name, key)]
        if self.keyed_documents is not None:
            found = resolve(self._document(name, key), coverage_in(value, self.events_lake))
            if found is not None:
                return found
        return held["inline"].get(key) if held is not None else None

    def _map_put(self, value, name, key, text):
        if self._map_get(value, name, key) == text:
            return
        value["maps"].setdefault(name, {"inline": {}, "changes": {}})["changes"][key] = text

    # Event records -----------------------------------------------------------
    def _remember(self, seq, payload):
        # Covered records never change, so a copy kept here can never go stale.
        size = len(payload) if isinstance(payload, str) else sum(map(len, payload["events"]))
        if seq not in self._record_cache and self._record_cache_bytes + size <= CACHE_BYTES:
            self._record_cache[seq] = payload
            self._record_cache_bytes += size

    @staticmethod
    def _single(payload):
        if not isinstance(payload, str):
            raise RecordCorrupt("runtime event record is not raw JSON text")

    @staticmethod
    def _chunk(payload):
        if (
            not isinstance(payload, dict)
            or set(payload) != {"first", "events", "commit"}
            or type(payload["first"]) is not int
            or not isinstance(payload["events"], list)
            or not isinstance(payload["commit"], str)
            or any(not isinstance(p, str) for p in payload["events"])
        ):
            raise RecordCorrupt("runtime event chunk is not a list of raw JSON texts")

    def _records(self, seqs, check=None):
        """Payloads of covered records, in the order asked. Fetched together, never skipped when missing."""
        check = check or self._single
        missing = [seq for seq in seqs if seq not in self._record_cache]
        got = {seq: self._record_cache[seq] for seq in seqs if seq in self._record_cache}
        started = time.monotonic()
        if len(missing) > 1:
            with ThreadPoolExecutor(min(READ_WORKERS, len(missing))) as pool:
                fetched = list(pool.map(self.events_lake.read, missing))
        else:
            fetched = [self.events_lake.read(seq) for seq in missing]
        for seq, payload in zip(missing, fetched):
            check(payload)
            self._remember(seq, payload)
            got[seq] = payload
        if missing:
            # Event history should be read only by audits and tools; this line makes any other read visible.
            print(
                json.dumps(
                    {
                        "event": "runtime_history_read",
                        "records": len(missing),
                        "durationMs": round((time.monotonic() - started) * 1000),
                    }
                )
            )
        for seq in seqs:
            check(got[seq])
        return [got[seq] for seq in seqs]

    def _externalized(self, payload, event):
        """The payload to store for ``event``: a ``cycle-started`` item's values become a ``valuesRef``.

        The values are one content-addressed payload (``value_records.py``) in this runtime's own store, written
        with the commit that publishes the event and not at all when that store already holds them.
        """
        item = event.get("item")
        if event.get("kind") != "cycle-started" or not isinstance(item, dict) or not isinstance(item.get("values"), dict):
            return payload
        content = canonical(item["values"])
        ref = ref_of(content)
        if ref[0] not in self._blobs:
            self._pending_blobs[ref[0]] = content
        rest = {key: held for key, held in item.items() if key != "values"}
        return _text({**event, "item": {**rest, VALUES_REF: {"digest": ref[0], "length": ref[1]}}})

    def _inflated(self, payload):
        """The event exactly as it was emitted: a referenced item's values read back (verified) in place."""
        if VALUES_REF not in payload:
            return payload
        event = json.loads(payload)
        item = event.get("item")
        if event.get("kind") != "cycle-started" or not isinstance(item, dict) or VALUES_REF not in item:
            return payload
        ref = item[VALUES_REF]
        if not isinstance(ref, dict) or set(ref) != {"digest", "length"}:
            raise RecordCorrupt("invalid item values reference in a runtime event")
        digest = ref["digest"]
        # Values still waiting for their commit are read from memory; ``_blobs`` holds only stored ones.
        content = self._pending_blobs.get(digest) or self._blobs.get(digest)
        if content is None:
            content = self.journal.payloads.get(PayloadRef(self.journal.payloads.scope, digest, ref["length"]))
            self._blobs[digest] = content
        rest = {key: held for key, held in item.items() if key != VALUES_REF}
        return _text({**event, "item": {**rest, "values": decode(content)}})

    def _events(self, value, extra=None):
        return _Events(self, value, self._held if extra is None else extra)

    def audit_events(self):
        """Read-only: the head record and the whole event log as it stands, whoever owns the runtime.

        Returns ``EventAudit``: ``embedded`` are the rows still inside the document, ``stored`` the rows
        read from the lake (each ``(id, payload)``), and the lake coverage. For audit and conversion only.
        """
        record = self.journal.read()
        self._check_stored(record.value)
        view = _Events(self, record.value)
        rows = list(view.rows())
        embedded = len(view.legacy)
        return EventAudit(record, rows[:embedded], rows[embedded:], view.coverage)

    def preview_events_conversion(self, audit):
        """(the document as it would stand after the conversion, bytes of the records it would write).

        Nothing is written. Exact unless a sequence turns out to be occupied when the pages are created.
        """
        rows = audit.embedded + audit.stored
        mark = None if CYCLE_MARK_KEY in audit.record.value["data"]["state"] else json.dumps(_highest_cycle(rows))
        state = deepcopy(audit.record.value)
        state[RECORDS_KEY] = {
            **state.get(RECORDS_KEY, {}),
            self._lake_name: Coverage(audit.coverage.last + len(rows), audit.coverage.skipped).to_json(),
        }
        events_moved(state, self._lake_name, audit.coverage, Coverage.from_json(state[RECORDS_KEY][self._lake_name]), mark)
        size = sum(self.events_lake.encoded_size(audit.coverage.last + n, p) for n, (_, p) in enumerate(rows, 1))
        return state, size

    def convert_embedded_events(self, *, check=None):
        """Move the events embedded in the document into the lake, in one commit. Needs ownership.

        The embedded events (ids 1..n) become the first lake records, followed by any lake records that were
        written meanwhile (copied again after them, their old sequences marked skipped), so ids and order are
        exactly what readers saw. The pages are written first; then ONE commit empties the embedded list and
        stores the new coverage. Returns ``EventsConverted``, or None when nothing is embedded (idempotent).
        Anything wrong refuses before a page is written. Chunks written since are copied as single records
        too, so afterwards every covered record holds one event and the next commit starts a new chunk run.
        """
        if self.events_lake is None:
            raise ValueError("a runtime events lake is required to convert embedded events")
        for _ in range(self.max_conflicts):
            if self.poisoned:
                raise RuntimeOwnershipError("uncertain runtime write; reopen/reconcile before continuing")
            audit = self.audit_events()
            self._owned(audit.record.value)
            if not audit.embedded:
                return None
            if [i for i, _ in audit.embedded] != list(range(1, len(audit.embedded) + 1)):
                raise ValueError("embedded event ids are not 1..n; refusing to renumber them")
            rows = audit.embedded + audit.stored
            history = [payload for _, payload in rows]
            before = audit.record.value
            mark = None if CYCLE_MARK_KEY in before["data"]["state"] else json.dumps(_highest_cycle(rows))

            def settle(state, refs, before=audit.coverage, mark=mark):
                grown = Coverage.from_json(state[RECORDS_KEY][self._lake_name])
                events_moved(state, self._lake_name, before, grown, mark)
                self._check_stored(state)

            try:
                committed, refs = commit_with_records(
                    self.journal, audit.record, [(self.events_lake, history)], settle, invalidate=self._poison, check=check
                )
            except StorageConflict:
                continue
            self._cached = None
            return EventsConverted(
                moved=len(audit.embedded),
                rows=rows,
                refs=refs[self._lake_name],
                before=audit.record,
                after=committed,
                coverage=Coverage.from_json(committed.value[RECORDS_KEY][self._lake_name]),
            )
        raise RuntimeOwnershipError("runtime contention limit reached")

    @staticmethod
    def ordinal(value):
        value["ordinal"] += 1
        return value["ordinal"]


def events_moved(state, lake_name, before, grown, mark):
    """Edit ``state`` in place: no embedded events, the lake covering ``grown``, and the sequences that held the
    lake's earlier records marked skipped (they were copied again, after the embedded events). Every covered
    record then holds one event, so the head names no chunk run until the next commit starts one."""
    state["data"]["events"] = []
    state.pop("chunks", None)
    state[RECORDS_KEY][lake_name] = Coverage(grown.last, tuple(sorted(set(grown.skipped) | set(before.sequences())))).to_json()
    if mark is not None:
        state["data"]["state"][CYCLE_MARK_KEY] = mark


@dataclass(frozen=True)
class EventAudit:
    record: object
    embedded: list
    stored: list
    coverage: Coverage


@dataclass(frozen=True)
class EventsConverted:
    moved: int  # embedded events that went into the lake
    rows: list  # the whole log, (id, payload), as it was before and must be after
    refs: list  # RecordRef of every page written
    before: object  # JournalRecord before the commit
    after: object  # JournalRecord the commit produced
    coverage: Coverage


def _covered(coverage, low, high):
    """How many sequences in ``low..high`` the coverage vouches for."""
    if high < low:
        return 0
    skipped = coverage.skipped
    return high - low + 1 - (bisect.bisect_right(skipped, high) - bisect.bisect_left(skipped, low))


class _Events:
    """One snapshot of the ordered event log: embedded legacy rows, then covered lake records, then held events.

    An event's id is its position. Legacy rows keep the ids they have. Lake records below the first chunk hold
    one event each (the v1 layout): the n-th covered one is ``base + n``, whatever sequences were skipped. From
    ``chunks.from`` on, each covered record is a chunk naming the id of its first event, so ids stay dense and
    ordered. A reader finds the chunk holding an id by searching back from the newest, which reads a handful of
    recent chunks for a recent id and a logarithmic number for an old one; it never loads the rest of the log.
    """

    def __init__(self, store, value, held=()):
        self.store, self.legacy, self.held = store, value["data"]["events"], tuple(held)
        lake = store.events_lake
        self.coverage = coverage_in(value, lake) if lake is not None else Coverage()
        if lake is None and (
            "chunks" in value or (RECORDS_KEY in value and value[RECORDS_KEY].get(store._lake_name, {}).get("last"))
        ):
            raise RuntimeOwnershipError("runtime events live in the record lake, which this handle was not given")
        self.base = self.legacy[-1][0] if self.legacy else 0
        chunks = value.get("chunks")
        self.start = chunks["from"] if chunks else None
        self.count = _covered(self.coverage, 1, self.start - 1 if chunks else self.coverage.last)
        self.before = self.base + self.count
        self.chunks = _covered(self.coverage, self.start, self.coverage.last) if chunks else 0
        self.stored_last = chunks["last"] if chunks else self.before
        self.last_id = self.stored_last + len(self.held)

    def _seq(self, position):
        """The sequence holding the ``position``-th covered record (0 for none)."""
        seq = position
        for skipped in self.coverage.skipped:
            if skipped > seq:
                break
            seq += 1
        return seq

    def _page(self, after_seq, wanted):
        seqs = self.coverage.sequences(after_seq, wanted)
        return seqs, self.store._records(seqs)

    def _chunk_at(self, index):
        """The ``index``-th covered chunk (0-based)."""
        seq = self.start + index
        for skipped in self.coverage.skipped:
            if skipped < self.start:
                continue
            if skipped > seq:
                break
            seq += 1
        return self.store._records([seq], self.store._chunk)[0]

    def _locate(self, identifier):
        """The index of the chunk holding ``identifier`` (before < identifier <= stored_last)."""
        if identifier == self.before + 1 and self._chunk_at(0)["events"]:
            return 0  # the first chunk starts right after the single-event records (unless it holds none)
        first = lambda index: self._chunk_at(index)["first"]  # noqa: E731
        upper = self.chunks - 1
        if first(upper) <= identifier:
            return upper
        step = 1
        while True:
            probe = upper - step
            if probe <= 0:
                low = 0
                break
            if first(probe) <= identifier:
                low = probe
                break
            upper, step = probe, step * 2
        while upper - low > 1:
            middle = (low + upper) // 2
            if first(middle) <= identifier:
                low = middle
            else:
                upper = middle
        return low

    def rows(self, after_id=0, limit=None):
        """(id, payload) above ``after_id`` in order, each event as it was emitted (item values read back)."""
        inflated = self.store._inflated
        return ((identifier, inflated(payload)) for identifier, payload in self._rows(after_id, limit))

    def tail(self, limit):
        """The last ``limit`` rows, oldest first, each event as it was emitted."""
        return [(identifier, self.store._inflated(payload)) for identifier, payload in self._tail(limit)]

    def _rows(self, after_id=0, limit=None):
        """(id, payload) above ``after_id`` in order; at most ``limit`` of them, and no more records than that read."""
        remaining = limit
        for identifier, payload in self.legacy:
            if identifier > after_id and remaining != 0:
                yield identifier, payload
                remaining = remaining if remaining is None else remaining - 1
        position = max(after_id - self.base, 0)
        seq = self._seq(position) if position < self.count else 0
        while position < self.count and remaining != 0:
            wanted = min(PAGE, self.count - position, remaining if remaining is not None else PAGE)
            seqs, payloads = self._page(seq, wanted)
            for payload in payloads:
                position += 1
                yield self.base + position, payload
            seq = seqs[-1]
            remaining = remaining if remaining is None else remaining - len(payloads)
        if self.chunks and remaining != 0 and self.stored_last > max(after_id, self.before):
            index = self._locate(max(after_id, self.before) + 1)
            while index < self.chunks and remaining != 0:
                chunk = self._chunk_at(index)
                for offset, payload in enumerate(chunk["events"]):
                    identifier = chunk["first"] + offset
                    if identifier > after_id and remaining != 0:
                        yield identifier, payload
                        remaining = remaining if remaining is None else remaining - 1
                index += 1
        for offset, payload in enumerate(self.held, 1):
            identifier = self.stored_last + offset
            if identifier > after_id and remaining != 0:
                yield identifier, payload
                remaining = remaining if remaining is None else remaining - 1

    def _tail(self, limit):
        """The last ``limit`` rows, oldest first, reading only the records they need."""
        need = limit
        held = self.held[-need:] if need > 0 else ()
        rows = [(self.last_id - len(held) + i + 1, payload) for i, payload in enumerate(held)]
        need -= len(held)
        index = self.chunks - 1
        while need > 0 and index >= 0:
            chunk = self._chunk_at(index)
            events = chunk["events"]
            taken = events[-need:]
            start = chunk["first"] + len(events) - len(taken)
            rows[:0] = [(start + i, payload) for i, payload in enumerate(taken)]
            need -= len(taken)
            index -= 1
        if need > 0 and self.count:
            take = min(need, self.count)
            seqs, payloads = self._page(self._seq(self.count - take), take)
            rows[:0] = [(self.base + self.count - take + i + 1, payload) for i, payload in enumerate(payloads)]
            need -= take
        if need > 0:
            rows[:0] = self.legacy[-need:]
        return rows


def _active_by_item(data, events):
    saved = data["state"].get(ACTIVE_LABELS_STATE_KEY)
    return load_active_labels(saved) if saved is not None else rebuild_active_labels(events.rows())


class _View:
    def __init__(self, store):
        self.store = store

    def read(self):
        return self.store._read()

    def change(self, fn, *, defer=False):
        return self.store.change(fn, defer=defer)

    def ensure(self):
        self.read()


class _State(_View):
    def close(self):
        self.store.close()

    def state(self, key):
        return self.read()["state"].get(key)

    def bind_contract(self, expected):
        json_text(expected)

        def apply(data, value):
            old = data["state"].get("contract")
            if old is not None and old != expected:
                raise ValueError("runtime task/model contract does not match the saved study")
            data["state"].setdefault("contract", expected)

        self.change(apply)

    def put_state(self, key, payload):
        json_text(payload)
        self.change(lambda data, value: data["state"].__setitem__(key, payload))

    def keyed(self, name, key):
        """One keyed value this storage keeps for derived summaries, or None."""
        store = self.store
        return store._map_get(store._read_value(), name, key)

    def save_derived(self, states, maps=None, *, prepare=False):
        """Save a derived summary: state values and keyed changes (``None`` removes a key), together.

        A summary can always be rebuilt from the events it was folded from, and its cursor is saved with it,
        so an owning handle holds it and publishes it with the next durable change or at release. With
        ``prepare`` (a summary converted from an older layout: many keys at once), the keyed changes are written
        in the background straight away, still counting only once that commit covers them.
        """
        maps = maps or {}
        for payload in states.values():
            json_text(payload)
        for name, changes in maps.items():
            if not isinstance(name, str) or name in RESERVED_MAPS or not name or not all(c.isalnum() or c == "-" for c in name):
                raise ValueError("derived keyed state needs its own plain name")
            for text in changes.values():
                if text is not None:
                    json_text(text)
        store = self.store

        def apply(data, value):
            data["state"].update(states)
            if prepare and store.keyed_documents is not None:
                store._prepare([(name, key, text) for name, changes in maps.items() for key, text in changes.items()], value)
                return
            for name, changes in maps.items():
                for key, text in changes.items():
                    store._map_put(value, name, key, text)

        self.change(apply, defer=True)

    def append_event(self, payload):
        json_text(payload)
        store = self.store

        def apply(data, value):
            if store.events_lake is None:
                events = _Events(store, value)
                identifier = events.last_id + 1
                data["events"].append([identifier, payload])
            else:
                # The event joins the chunk the next commit writes; nothing lands in the head but control state.
                # An item's values go by reference, so the chunk does not grow with the article's text.
                store._staged.append(store._externalized(payload, json.loads(payload)))
                events = _Events(store, value, store._staged)
                identifier = events.last_id
            event = json.loads(payload)
            if event.get("kind") == FEEDBACK_EVENT_KIND:
                active_by_item = _active_by_item(data, events)
                record_feedback_event(active_by_item, identifier, event)
                data["state"][ACTIVE_LABELS_STATE_KEY] = dump_active_labels(active_by_item)
            if store.events_lake is not None:
                _note_cycle(data, events, event)
                _note_unfinished(store, value, events, event)
            return identifier

        return self.change(apply, defer=True)

    def active_labels(self):
        def materialize(data, value):
            if ACTIVE_LABELS_STATE_KEY not in data["state"]:
                data["state"][ACTIVE_LABELS_STATE_KEY] = dump_active_labels(_active_by_item(data, self.store._events(value)))
            return data["state"][ACTIVE_LABELS_STATE_KEY]

        saved = self.read()["state"].get(ACTIVE_LABELS_STATE_KEY)
        if saved is None:
            saved = self.change(materialize)
        return tuple(load_active_labels(saved).values())

    def _view(self):
        return self.store._events(self.store._read_value())

    def history(self, limit=100):
        view = self._view()
        rows = list(view.rows()) if limit < 0 else view.tail(limit) if limit else []
        return tuple({**json.loads(payload), "event_id": identifier} for identifier, payload in rows)

    def trace_events(self, *, after_event_id=0, limit=100):
        if type(after_event_id) is not int or after_event_id < 0 or type(limit) is not int or limit < 1:
            raise ValueError("a non-negative trace cursor and positive page size required")
        rows = list(self._view().rows(after_event_id, limit))
        return {
            "events": [{**json.loads(payload), "event_id": identifier} for identifier, payload in rows],
            "cursor": rows[-1][0] if rows else after_event_id,
        }

    @staticmethod
    def _kinds(kinds):
        if not isinstance(kinds, tuple) or not kinds or any(not isinstance(k, str) for k in kinds):
            raise ValueError("explicit event kinds required")

    def event_payloads(self, kinds):
        self._kinds(kinds)
        view = self._view()  # the snapshot is fixed now; records are then read lazily, a page at a time
        return (payload for _, payload in view.rows() if json.loads(payload).get("kind") in kinds)

    def events_of(self, kinds):
        self._kinds(kinds)
        return tuple(
            {**json.loads(payload), "event_id": identifier}
            for identifier, payload in self._view().rows()
            if json.loads(payload).get("kind") in kinds
        )

    def unfinished_cycles(self, item_id):
        store = self.store
        value = store._read_value()
        if store.events_lake is not None and value["counters"]["cycles_indexed"]:
            saved = store._map_get(value, CYCLES, item_id)
            return tuple(tuple(row) for row in json.loads(saved)) if saved is not None else ()
        return unfinished_cycles_from(self.events_of(CYCLE_EVENT_KINDS), item_id)

    def next_cycle_number(self):
        mark = self.store._read()["state"].get(CYCLE_MARK_KEY) if self.store.events_lake is not None else None
        if mark is not None:
            return json.loads(mark) + 1
        return _highest_cycle(self._view().rows()) + 1


def _highest_cycle(rows):
    return max((json.loads(payload).get("cycle_number", 0) or 0 for _, payload in rows), default=0)


def _note_cycle(data, events, event):
    """Keep the highest cycle number in control state; the first time, from every event so far."""
    saved = data["state"].get(CYCLE_MARK_KEY)
    mark = _highest_cycle(events.rows()) if saved is None else max(json.loads(saved), event.get("cycle_number", 0) or 0)
    if saved is None or mark != json.loads(saved):
        data["state"][CYCLE_MARK_KEY] = json.dumps(mark)


def _note_unfinished(store, value, events, event):
    """Keep each item's unfinished cycles in its ``cycles`` document; the first time, from every event so far."""
    counters = value["counters"]
    if not counters["cycles_indexed"]:
        index = {}
        for _, payload in events.rows():
            note_cycle_event(index, json.loads(payload))
        for item_id, rows in index.items():
            store._map_put(value, CYCLES, item_id, _text(rows))
        counters["cycles_indexed"] = True
        return
    item_id = event.get("cycle_item_id")
    if event.get("kind") not in CYCLE_EVENT_KINDS or item_id is None:
        return
    saved = store._map_get(value, CYCLES, item_id)
    index = note_cycle_event({item_id: json.loads(saved)} if saved is not None else {}, event)
    store._map_put(value, CYCLES, item_id, _text(index[item_id]) if item_id in index else None)


def _answer(store, value, key):
    """An answer row: a pending reservation in the head, or a settled row in its ``answers`` document."""
    row = value["data"]["answers"].get(key)
    if row is not None:
        return row
    saved = store._map_get(value, ANSWERS, key)
    return json.loads(saved) if saved is not None else None


def _put_answer(store, value, key, row):
    """Place an answer row where its status belongs; ``None`` removes it."""
    if row is not None and row[0] == "pending":
        value["data"]["answers"][key] = row
        store._map_put(value, ANSWERS, key, None)
    else:
        value["data"]["answers"].pop(key, None)
        store._map_put(value, ANSWERS, key, _text(row) if row is not None else None)


def _answer_ordinal(store, value):
    ordinal = store.ordinal(value)
    value["counters"]["answer_mark"] = max(value["counters"]["answer_mark"], ordinal)
    return ordinal


class _Cache(_View):
    def answer(self, key):
        row = _answer(self.store, self.store._read_value(), key)
        return tuple(row[:2]) if row else None

    def begin_answer(self, key, previous):
        store = self.store

        def apply(data, value):
            if previous:
                number = value["counters"]["answer_history"] + 1
                store._map_put(value, HISTORY, str(number), _text([number, key, *previous]))
                value["counters"]["answer_history"] = number
            _put_answer(store, value, key, ["pending", None, _answer_ordinal(store, value)])

        # The reservation grants a paid call, so it is durable before the call: never deferred.
        self.change(apply)

    def fail_answer(self, key):
        store = self.store

        def apply(data, value):
            row = _answer(store, value, key)
            if row is not None:
                _put_answer(store, value, key, ["failed", *row[1:]])

        self.change(apply, defer=True)

    def complete_answer(self, key, payload, *, previous, current_key=None):
        json_text(payload)
        store = self.store

        def apply(data, value):
            target = current_key or key
            if target != key:
                row = _answer(store, value, key)
                if previous and row is not None:
                    _put_answer(store, value, key, [*previous, *row[2:]])
                else:
                    _put_answer(store, value, key, None)
                _put_answer(store, value, target, ["pending", None, _answer_ordinal(store, value)])
            row = _answer(store, value, target)
            if row is not None:
                _put_answer(store, value, target, ["complete", payload, *row[2:]])

        # Recording an outcome grants nothing: the reservation is already durable, so an interrupted run
        # leaves it pending (needing explicit retry), exactly as a crash during the call would.
        self.change(apply, defer=True)

    def round(self, key):
        row = self.read()["rounds"].get(key)
        return tuple(row[:2]) if row else None

    def reset_pending_round(self, key):
        def apply(data, value):
            row = data["rounds"].get(key)
            if row and row[0] == "pending":
                del data["rounds"][key]

        self.change(apply)

    def begin_round(self, key):
        def apply(data, value):
            if key in data["rounds"]:
                raise ValueError("round already exists")
            data["rounds"][key] = ["pending", None, self.store.ordinal(value)]

        self.change(apply)

    def complete_round(self, key, payload):
        json_text(payload)

        def apply(data, value):
            if key in data["rounds"]:
                data["rounds"][key][:2] = ["complete", payload]

        self.change(apply)

    def save_completed_round(self, key, payload):
        json_text(payload)
        self.change(lambda data, value: data["rounds"].__setitem__(key, ["complete", payload, self.store.ordinal(value)]))


class _Learning(_View):
    def ensure(self, kind):
        artifact_table(kind)
        self.read()

    def artifact(self, kind, key):
        artifact_table(kind)
        row = self.read()["artifacts"][kind].get(key)
        return tuple(row[:2]) if row else None

    def completed(self, kind):
        artifact_table(kind)
        rows = self.read()["artifacts"][kind]
        return [(k, r[1]) for k, r in sorted(rows.items(), key=lambda p: p[1][2], reverse=True) if r[0] == "complete"]

    def count(self, kind):
        artifact_table(kind)
        return len(self.read()["artifacts"][kind])

    def begin(self, kind, key, payload):
        artifact_table(kind)
        json_text(payload)
        self.change(lambda data, value: data["artifacts"][kind].__setitem__(key, ["pending", payload, self.store.ordinal(value)]))

    def update_payload(self, kind, key, payload):
        artifact_table(kind)
        json_text(payload)

        def apply(data, value):
            if key in data["artifacts"][kind]:
                data["artifacts"][kind][key][1] = payload

        self.change(apply)

    def complete(self, kind, key, payload, *, aliases=(), clear_pending_key=None):
        artifact_table(kind)
        json_text(payload)

        def apply(data, value):
            rows = data["artifacts"][kind]
            if clear_pending_key:
                old = json.loads(rows[clear_pending_key][1])
                old["pending_evaluation"] = False
                rows[clear_pending_key][1] = json.dumps(
                    old, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
                )
            if key in rows:
                rows[key][:2] = ["complete", payload]
            for alias in aliases:
                rows[alias] = ["complete", payload, self.store.ordinal(value)]

        self.change(apply)


class _Payloads(_View):
    def __init__(self, store, domain):
        super().__init__(store)
        self.domain = domain

    def payloads(self):
        return tuple(value for key, value in sorted(self.read()[self.domain].items()))

    def payload(self, key):
        return self.read()[self.domain].get(key)

    def save(self, key, payload):
        json_text(payload)
        self.change(lambda data, value: data[self.domain].__setitem__(key, payload))


class _Pending(_View):
    DOMAINS = {"runtime_rounds": "rounds", "runtime_answers": "answers", **{v[0]: k for k, v in ARTIFACTS.items()}}

    def _rows(self, data, kind):
        domain = self.DOMAINS[kind]
        return data[domain] if domain in ("rounds", "answers") else data["artifacts"][domain]

    def marks(self):
        value = self.store._read_value()
        data = value["data"]
        marks = {kind: max((r[2] for r in self._rows(data, kind).values()), default=0) for kind in self.DOMAINS}
        # Settled answers live by key; the head keeps the highest ordinal any answer row was given.
        # Ordinals only grow, so "a row newer than this mark" means the same as against the rows themselves.
        marks["runtime_answers"] = max(marks["runtime_answers"], value["counters"]["answer_mark"])
        return marks

    def pending(self):
        data = self.read()
        return [
            (kind, row[2], key) for kind in self.DOMAINS for key, row in self._rows(data, kind).items() if row[0] == "pending"
        ]

    def discard(self, kind, ordinal, key):
        if kind not in self.DOMAINS:
            raise ValueError("unknown pending artifact")

        def apply(data, value):
            rows = self._rows(data, kind)
            row = rows.get(key)
            if row and row[0] == "pending" and row[2] == ordinal:
                del rows[key]
                return True
            return False

        return self.change(apply)


class _Hypothesis(_View):
    def ensure_diagnosis(self):
        self.read()

    def record_evaluations(self, rows):
        def apply(data, value):
            for row in rows:
                data["evaluations"] = [r for r in data["evaluations"] if (r[1], r[2], r[7]) != (row[0], row[1], row[6])]
                data["evaluations"].append([self.store.ordinal(value), *row])

        self.change(apply)

    def append_verdict(self, row):
        self.change(lambda data, value: data["verdicts"].append(list(row)))

    def verdicts(self, idea_id):
        return [tuple(r[1:]) for r in self.read()["verdicts"] if r[0] == idea_id]

    def _evaluations(self, idea_id, context_version=None):
        return [r for r in self.read()["evaluations"] if r[1] == idea_id and (context_version is None or r[7] == context_version)]

    def latest(self, idea_id, context_version=None):
        return [(r[2], r[3]) for r in sorted(self._evaluations(idea_id, context_version), key=lambda r: (r[8], r[0]))]

    def marks(self, idea_id, since=None, *, strict=False):
        return [r[3] for r in self._evaluations(idea_id) if since is None or (r[8] > since if strict else r[8] >= since)]

    def verdict_idea_ids(self):
        return sorted({r[0] for r in self.read()["verdicts"]})

    def idea_ids(self, context_version=None):
        return sorted({r[1] for r in self.read()["evaluations"] if context_version is None or r[7] == context_version})

    def idea_payload(self, idea_id):
        return self.store.control_storage.payload(idea_id)

    def idea_payloads(self):
        return self.store.control_storage.payloads()

    def save_cause(self, row):
        self.change(lambda data, value: data["causes"].__setitem__(row[0], list(row[1:])))

    def causes(self, round_number=None):
        return [
            r[1]
            for key, r in sorted(self.read()["causes"].items(), key=lambda p: (p[1][0], p[0]))
            if round_number is None or r[0] == round_number
        ]

    def save_element(self, row):
        def apply(data, value):
            data["elements"] = [r for r in data["elements"] if (r[1], r[2]) != (row[0], row[1])]
            data["elements"].append([self.store.ordinal(value), *row])

        self.change(apply)

    def elements(self):
        return [tuple(r[1:13]) for r in sorted(self.read()["elements"], key=lambda r: (r[2], r[0]))]

    def wrong(self, idea_id, context_version=None, *, recent=None):
        rows = sorted(
            (r for r in self._evaluations(idea_id, context_version) if r[3] == 0),
            key=lambda r: (r[8], r[0]),
            reverse=recent is not None,
        )
        return [(r[2], r[4], r[5]) for r in (rows if recent is None else rows[:recent])]


class _Shadow(_View):
    def calls(self):
        return len(self.read()["shadow_scored"])

    def active(self):
        return [tuple(r[:3]) for r in sorted(self.read()["shadow_set"], key=lambda r: (r[2], r[0])) if r[3] == "active"]

    def known(self):
        return {r[0] for r in self.read()["shadow_set"]}

    def statuses(self):
        return {r[0]: r[3] for r in self.read()["shadow_set"]}

    def set_status(self, idea_id, status):
        def apply(data, value):
            for row in data["shadow_set"]:
                if row[0] == idea_id:
                    row[3] = status

        self.change(apply)

    def _append(self, domain, row, identity, ignore=False):
        def apply(data, value):
            if any(tuple(r[:identity]) == tuple(row[:identity]) for r in data[domain]):
                if ignore:
                    return
                raise ValueError("duplicate forward-evaluation identity")
            data[domain].append(list(row))

        self.change(apply)

    def join(self, idea_id, control, seq):
        self._append("shadow_set", (idea_id, control, seq, "active"), 1)

    def scored(self, idea_id, item_id, seq):
        self._append("shadow_scored", (idea_id, item_id, seq), 2, True)

    def incumbent(self, incumbent_id, rubric, seq):
        self._append("shadow_incumbents", (incumbent_id, rubric, seq), 1, True)

    def incumbent_ids(self):
        return {r[0] for r in self.read()["shadow_incumbents"]}

    def looks_taken(self, idea_id, incumbent):
        return sum(r[0] == idea_id and r[1] == incumbent for r in self.read()["shadow_looks"])

    def look(self, row):
        self._append("shadow_looks", row, 3)

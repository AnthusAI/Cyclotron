"""Named core runtime repositories over a canonical S3 transaction.

An explicit owner holds this classifier until release. There is no timeout-based
steal: an uncertain/crashed holder needs reconciliation. Every read and CAS
mutation checks the same owner/fence stored with its data, never a DDB lease.
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass

from .active_labels import (
    ACTIVE_LABELS_STATE_KEY,
    FEEDBACK_EVENT_KIND,
    dump_active_labels,
    load_active_labels,
    rebuild_active_labels,
    record_feedback_event,
)
from .cycles import CYCLE_EVENT_KINDS, note_cycle_event, unfinished_cycles_from
from .journal import Journal, commit_with_contract, decode, uuid
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
from .storage_payloads import FilePayloads, S3Payloads


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
CYCLE_INDEX_KEY = "unfinished-cycles"  # {item_id: [[cycle_id, cycle_number, reason, failed], ...]}
PAGE = 100  # records fetched together; one page is also the most a reader holds beyond what it was asked for
READ_WORKERS = 8
CACHE_BYTES = 32_000_000  # records are immutable, so a handle may keep what it has read; bounded, never evicted


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


def empty():
    return {
        "schema": "cyclotron-s3-runtime/v1",
        "owner": None,
        "fence": 0,
        "ordinal": 0,
        "data": {**{k: {} for k in DICT_FIELDS}, **{k: [] for k in LIST_FIELDS}, "artifacts": {k: {} for k in ARTIFACTS}},
    }


def json_text(value):
    if not isinstance(value, str):
        raise ValueError("raw JSON text required")
    decode(value)
    return value


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
        self.max_conflicts = max_conflicts
        self.authority = None
        self.poisoned = False
        self.cache_owned = cache_owned
        # Every commit rewrites the whole runtime snapshot, and a trace event is not a decision. An owning
        # handle may therefore hold appended events in memory and publish them with the next durable change
        # (an answer reservation or completion, a state write) or at release, in the same atomic commit.
        # Nothing that grants or records a paid action is ever deferred. Off unless the handle owns a cache.
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

    def _validate(self, value):
        if (
            not isinstance(value, dict)
            or set(value) - {RECORDS_KEY} != set(empty())
            or value["schema"] != "cyclotron-s3-runtime/v1"
        ):
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
        data = value["data"]
        if not isinstance(data, dict) or set(data) != set(empty()["data"]):
            raise ValueError("invalid runtime domains")
        if any(not isinstance(data[k], dict) for k in DICT_FIELDS) or any(not isinstance(data[k], list) for k in LIST_FIELDS):
            raise ValueError("invalid runtime records")
        if (
            not isinstance(data["artifacts"], dict)
            or set(data["artifacts"]) != set(ARTIFACTS)
            or any(not isinstance(v, dict) for v in data["artifacts"].values())
        ):
            raise ValueError("invalid learning kinds")

    def initialize(self):
        self._validate(self.journal.initialize(empty()).value)

    def _owned(self, value):
        if self.authority is None or (value["owner"], value["fence"]) != self.authority:
            raise RuntimeOwnershipError("runtime execution ownership unavailable; no timeout takeover")

    def flush(self):
        """Publish any held events now. A no-op when nothing is held."""
        if self._working is not None or self._held:
            self._change(lambda value: None)

    def refresh(self):
        self.flush()
        self._cached = None
        if self.poisoned:
            raise RuntimeOwnershipError("uncertain runtime write; reopen/reconcile before continuing")
        record = self.journal.read()
        self._validate(record.value)
        self._owned(record.value)
        self._cached = record

    def _read_value(self):
        if self.poisoned:
            raise RuntimeOwnershipError("uncertain runtime write; reopen/reconcile before continuing")
        current = self._cached if self.cache_owned and self._cached is not None else self.journal.read()
        value = self._working if self._working is not None else current.value
        self._validate(value)
        self._owned(value)
        return value

    def _read(self):
        return self._read_value()["data"]

    def _commit(self, record, value, staged):
        if not staged:
            return commit_with_contract(self.journal, record, value, invalidate=self._poison)

        def settle(state, refs):
            # The new coverage is already in ``state``; everything else is the change the caller built.
            coverage = state[RECORDS_KEY]
            state.clear()
            state.update(value)
            state[RECORDS_KEY] = coverage

        # Write the event records first (harmless if abandoned), then flip the head once: the records count
        # only together with the control change they belong to.
        committed, refs = commit_with_records(self.journal, record, [(self.events_lake, staged)], settle, invalidate=self._poison)
        for ref, payload in zip(refs[self._lake_name], staged):
            self._remember(ref.seq, payload)
        return committed

    def _change(self, fn, *, ownership=True, recovery=False, defer=False):
        if self.poisoned and not recovery:
            raise RuntimeOwnershipError("uncertain runtime write; reopen/reconcile before continuing")
        for _ in range(self.max_conflicts):
            held = (self._working is not None or bool(self._held)) and self._cached is not None
            record = (
                self._cached
                if held or (self.cache_owned and self._cached is not None and ownership and not recovery)
                else self.journal.read()
            )
            base = self._working if self._working is not None and held else record.value
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
                if value != record.value:
                    self._working = value
                self._held = staged
                return result
            if value == record.value and not staged:
                self._working = None
                self._held = []
                return result
            try:
                self._cached = self._commit(record, value, staged)
                self._working = None
                self._held = []
            except StorageConflict:
                self._cached = None
                if held:
                    # Events held in memory cannot be replayed onto a head someone else moved.
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

        # Set local permission ONLY after confirmed CAS success.
        self.authority = self._change(apply, ownership=False)

    def close(self):
        if self.authority is None:
            return

        def release(value):
            value["owner"] = None

        try:
            self._change(release, recovery=True)
        finally:
            self.authority = None
            self._cached = None
            self._working = None
            self._held = []

    def _remember(self, seq, payload):
        # Covered records never change, so a copy kept here can never go stale.
        if seq not in self._record_cache and self._record_cache_bytes + len(payload) <= CACHE_BYTES:
            self._record_cache[seq] = payload
            self._record_cache_bytes += len(payload)

    def _records(self, seqs):
        """Payloads of covered records, in the order asked. Fetched together, never skipped when missing."""
        missing = [seq for seq in seqs if seq not in self._record_cache]
        got = {seq: self._record_cache[seq] for seq in seqs if seq in self._record_cache}
        started = time.monotonic()
        if len(missing) > 1:
            with ThreadPoolExecutor(min(READ_WORKERS, len(missing))) as pool:
                fetched = list(pool.map(self.events_lake.read, missing))
        else:
            fetched = [self.events_lake.read(seq) for seq in missing]
        for seq, payload in zip(missing, fetched):
            if not isinstance(payload, str):
                raise RecordCorrupt("runtime event record is not raw JSON text")
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
        return [got[seq] for seq in seqs]

    def _events(self, value, extra=None):
        return _Events(self, value, self._held if extra is None else extra)

    def audit_events(self):
        """Read-only: the head record and the whole event log as it stands, whoever owns the runtime.

        Returns ``EventAudit``: ``embedded`` are the rows still inside the document, ``stored`` the rows
        read from the lake (each ``(id, payload)``), and the lake coverage. For audit and conversion only.
        """
        record = self.journal.read()
        self._validate(record.value)
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
        Anything wrong refuses before a page is written.
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
                self._validate(state)

            try:
                committed, refs = commit_with_records(
                    self.journal, audit.record, [(self.events_lake, history)], settle, invalidate=self._poison, check=check
                )
            except StorageConflict:
                continue
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
    lake's earlier records marked skipped (they were copied again, after the embedded events)."""
    state["data"]["events"] = []
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


class _Events:
    """One snapshot of the ordered event log: embedded legacy rows, then covered lake records, then held events.

    An event's id is its position. Legacy rows keep the ids they have; the lake's n-th covered record is
    ``base + n`` where ``base`` is the last legacy id, so ids stay dense and ordered whatever sequences were
    skipped. Lake records are fetched a page at a time, so a page of the log never loads the rest of it.
    """

    def __init__(self, store, value, held=()):
        self.store, self.legacy, self.held = store, value["data"]["events"], tuple(held)
        self.coverage = coverage_in(value, store.events_lake) if store.events_lake is not None else Coverage()
        if store.events_lake is None and RECORDS_KEY in value and value[RECORDS_KEY].get(store._lake_name, {}).get("last"):
            raise RuntimeOwnershipError("runtime events live in the record lake, which this handle was not given")
        self.base = self.legacy[-1][0] if self.legacy else 0
        self.count = self.coverage.last - len(self.coverage.skipped)
        self.last_id = self.base + self.count + len(self.held)

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

    def rows(self, after_id=0, limit=None):
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
        for offset, payload in enumerate(self.held, 1):
            identifier = self.base + self.count + offset
            if identifier > after_id and remaining != 0:
                yield identifier, payload
                remaining = remaining if remaining is None else remaining - 1

    def tail(self, limit):
        """The last ``limit`` rows, oldest first, reading only the records they need."""
        need = limit
        held = self.held[-need:] if need > 0 else ()
        rows = [(self.last_id - len(held) + i + 1, payload) for i, payload in enumerate(held)]
        need -= len(held)
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

    def append_event(self, payload):
        json_text(payload)
        store = self.store

        def apply(data, value):
            if store.events_lake is None:
                events = _Events(store, value)
                identifier = events.last_id + 1
                data["events"].append([identifier, payload])
            else:
                # The record joins the commit being built; nothing lands in the snapshot but control state.
                store._staged.append(payload)
                events = _Events(store, value, store._staged)
                identifier = events.last_id
            event = json.loads(payload)
            if event.get("kind") == FEEDBACK_EVENT_KIND:
                active_by_item = _active_by_item(data, events)
                record_feedback_event(active_by_item, identifier, event)
                data["state"][ACTIVE_LABELS_STATE_KEY] = dump_active_labels(active_by_item)
            if store.events_lake is not None:
                _note_cycle(data, events, event)
                _note_unfinished(data, events, event)
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
        saved = self.store._read()["state"].get(CYCLE_INDEX_KEY) if self.store.events_lake is not None else None
        if saved is not None:
            return tuple(tuple(row) for row in json.loads(saved).get(item_id, ()))
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


def _note_unfinished(data, events, event):
    """Keep each item's unfinished cycles in control state; the first time, from every event so far."""
    saved = data["state"].get(CYCLE_INDEX_KEY)
    if saved is None:
        index = {}
        for _, payload in events.rows():
            note_cycle_event(index, json.loads(payload))
    elif event.get("kind") in CYCLE_EVENT_KINDS:
        index = note_cycle_event(json.loads(saved), event)
    else:
        return
    data["state"][CYCLE_INDEX_KEY] = json.dumps(index, sort_keys=True, separators=(",", ":"))


class _Cache(_View):
    def answer(self, key):
        row = self.read()["answers"].get(key)
        return tuple(row[:2]) if row else None

    def begin_answer(self, key, previous):
        def apply(data, value):
            if previous:
                data["answer_history"].append([len(data["answer_history"]) + 1, key, *previous])
            data["answers"][key] = ["pending", None, self.store.ordinal(value)]

        self.change(apply)

    def fail_answer(self, key):
        def apply(data, value):
            if key in data["answers"]:
                data["answers"][key][0] = "failed"

        self.change(apply)

    def complete_answer(self, key, payload, *, previous, current_key=None):
        json_text(payload)

        def apply(data, value):
            target = current_key or key
            if target != key:
                if previous and key in data["answers"]:
                    data["answers"][key][:2] = previous
                else:
                    data["answers"].pop(key, None)
                data["answers"][target] = ["pending", None, self.store.ordinal(value)]
            if target in data["answers"]:
                data["answers"][target][:2] = ["complete", payload]

        self.change(apply)

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
        data = self.read()
        return {kind: max((r[2] for r in self._rows(data, kind).values()), default=0) for kind in self.DOMAINS}

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

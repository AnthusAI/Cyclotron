"""S3 embedded root and complete core composition, with no local-file fallback.

Root source-cursor/derived-record transactions publish one CAS. A failed/unknown
commit poisons that handle rather than continuing with potentially stale memory.
All execution ownership is explicit, fenced, and has no time-based takeover.

Layout (``cyclotron-s3-embedded/v2``). A commit costs the same however many decisions came before it:

* The control state (the journal head's value) holds only small mutable state: owner and fence, root
  metadata, reviews, the learning queue, the latest decision id and the coverage of the record lakes.
* Immutable facts are create-only records in ordered lakes beside it (record_lake.py), found by position:
  ``decisions`` (identity, item, fingerprint and payload), ``events`` (the subscribe log; an event's id is
  its position), ``usage`` (one row with the shares recorded with it) and ``items`` (the order in which item
  identities first arrived). Item values stay content-addressed payloads.
* What must be found by key is one keyed document per key (keyed_records.py): ``decision/<id>`` (its
  position, item, fingerprint, state and the item's previous decision) and ``item/<id>`` (values
  reference, fingerprint, latest and latest live decision). A commit that changes keyed documents also
  writes one ``commits`` record, and a keyed entry counts only when the head covers that record.

A commit writes its new records and keyed entries first (invisible until covered), then moves the head once.

Coalescing. An owning handle (``cache_owned``) may hold several transactions in memory inside
``coalesced()`` and publish them in one commit when the scope ends or when any transaction outside the scope
commits. Each held transaction stays all-or-nothing in memory.

A ``cyclotron-s3-embedded/v1`` journal (everything inside the head) is read as before and converted to this
layout by the first write, ``acquire``.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from hashlib import sha256
from uuid import uuid4

from .concurrent_calls import call_concurrently
from .journal import CanonicalJournal, Journal, commit_with_contract, uuid
from .keyed_records import keyed_documents_for, resolve
from .record_lake import RECORDS_KEY, Coverage, FileRecordLake, RecordConflict, S3RecordLake, coverage_in, with_coverage
from .s3_batch_storage import S3BatchStorage, reply_lake_for
from .s3_runtime_storage import RuntimeOwnershipError, S3RuntimeStores, runtime_events_lake
from .storage_errors import StorageMissing
from .storage_payloads import FilePayloads, PayloadRef, S3Payloads

SCHEMA = "cyclotron-s3-embedded/v2"
LEGACY_SCHEMA = "cyclotron-s3-embedded/v1"
RECORDS_DOMAIN = "workspace"
LAKES = ("decisions", "events", "usage", "items", "commits")
SUPERSEDED = "superseded"
READ_WORKERS = 8  # concurrent GETs for whole-history reads and parallel writes; not a limit on how many


def empty():
    return {
        "schema": SCHEMA,
        "owner": None,
        "fence": 0,
        RECORDS_KEY: {},
        "data": {"meta": {}, "reviews": [], "learning": [], "latest": None, "late_shares": []},
    }


def legacy_empty():
    """The v1 control state, where every record lived inside the head. Read and converted, never written."""
    return {
        "schema": LEGACY_SCHEMA,
        "owner": None,
        "fence": 0,
        "data": {
            "meta": {},
            "items": {},
            "decisions": [],
            "reviews": [],
            "events": [],
            "learning": [],
            "usage": [],
            "shares": [],
        },
    }


def lake_kind(journal, lake):
    """Each root journal gets its own lake kinds, named after the journal, like the runtime events lake."""
    return f"{lake}-" + journal.namespace.rsplit("/", 1)[-1]


def root_lakes(journal):
    payloads = journal.payloads
    if isinstance(payloads, S3Payloads):
        make = S3RecordLake
    elif isinstance(payloads, FilePayloads):
        make = FileRecordLake
    else:
        raise ValueError("an S3 or file payload adapter is required for the root record lakes")
    return {lake: make(payloads, RECORDS_DOMAIN, lake_kind(journal, lake)) for lake in LAKES}


def _seq(coverage, position):
    """The sequence holding the ``position``-th covered record."""
    seq = position
    for skipped in coverage.skipped:
        if skipped > seq:
            break
        seq += 1
    return seq


def _count(coverage):
    return coverage.last - len(coverage.skipped)


def _parallel(fn, values):
    values = list(values)
    if len(values) < 2:
        return [fn(value) for value in values]
    with ThreadPoolExecutor(min(READ_WORKERS, len(values))) as pool:
        return list(pool.map(fn, values))


def _item_document():
    return {"values": None, "fingerprint": None, "latest": None, "live": None, "walk": False}


class _Pending:
    """A control state being built plus the records and keyed values it will publish."""

    __slots__ = ("head", "appends", "keys")

    def __init__(self, head, appends=None, keys=None):
        self.head, self.appends, self.keys = head, appends or {}, keys or {}

    def copy(self):
        return _Pending(deepcopy(self.head), deepcopy(self.appends), deepcopy(self.keys))

    def append(self, lake, value):
        self.appends.setdefault(lake, []).append(value)

    def changed(self, base):
        return bool(self.keys) or any(self.appends.values()) or self.head != base


class S3EmbeddedStorage:
    def __init__(self, journal, *, cache_owned=False):
        if not isinstance(journal, Journal):
            raise ValueError("scoped journal required")
        self.journal = journal
        self.lakes = root_lakes(journal)
        self.keyed = keyed_documents_for(journal)
        self.authority = None
        self.poisoned = False
        self.cache_owned = cache_owned
        self._cached = None
        self._held = None  # transactions held by coalesced(), not yet published
        self._records = {}  # (lake, seq) -> value; covered records never change
        self._documents = {}  # (kind, key) -> Document or None (absent), as last read or written by this handle
        self._transaction = ContextVar("s3_embedded_transaction", default=None)
        self._coalescing = ContextVar("s3_embedded_coalescing", default=False)

    # Validation ------------------------------------------------------------
    def _validate(self, value):
        if not isinstance(value, dict) or set(value) != set(empty()) or value["schema"] != SCHEMA:
            raise ValueError("invalid canonical embedded root")
        self._validate_epoch(value)
        held = value[RECORDS_KEY]
        if not isinstance(held, dict) or set(held) - {lake.name for lake in self.lakes.values()}:
            raise ValueError("invalid record coverage")
        for coverage in held.values():
            Coverage.from_json(coverage)
        data = value["data"]
        if not isinstance(data, dict) or set(data) != set(empty()["data"]):
            raise ValueError("complete embedded domains required")
        if (
            not isinstance(data["meta"], dict)
            or any(not isinstance(data[k], list) for k in ("reviews", "learning", "late_shares"))
            or not (data["latest"] is None or isinstance(data["latest"], str))
            or any(type(n) is not int for n in data["late_shares"])
        ):
            raise ValueError("invalid embedded records")

    @staticmethod
    def _validate_epoch(value):
        if type(value["fence"]) is not int or value["fence"] < 0:
            raise ValueError("invalid root execution fence")
        if value["owner"] is not None:
            uuid(value["owner"])

    def _validate_legacy(self, value):
        if not isinstance(value, dict) or set(value) != set(legacy_empty()) or value["schema"] != LEGACY_SCHEMA:
            raise ValueError("invalid canonical embedded root")
        self._validate_epoch(value)
        data = value["data"]
        if not isinstance(data, dict) or set(data) != set(legacy_empty()["data"]):
            raise ValueError("complete embedded domains required")
        if any(not isinstance(data[k], dict if k in ("meta", "items") else list) for k in data):
            raise ValueError("invalid embedded records")

    def _validate_any(self, value):
        """Either layout; true when it is the v1 layout that the next acquire converts."""
        if isinstance(value, dict) and value.get("schema") == LEGACY_SCHEMA:
            self._validate_legacy(value)
            return True
        self._validate(value)
        return False

    # Ownership -------------------------------------------------------------
    def initialize(self):
        self._validate_any(self.journal.initialize(empty()).value)

    def _owned(self, value):
        if self.authority is None or (value["owner"], value["fence"]) != self.authority:
            raise RuntimeOwnershipError("embedded execution ownership unavailable")

    def acquire(self, owner, *, resume=False):
        """Take ownership. ``resume=True`` re-takes a root this same ``owner`` id left held (the fence moves).

        Taking ownership of a v1 root converts it: its records move into the lakes and keyed documents and
        the head becomes the small v2 control state, in this one commit.
        """
        uuid(owner)
        record = self.journal.read()
        legacy = self._validate_any(record.value)
        held = record.value["owner"]
        if self.authority is not None or (held is not None and not (resume and held == owner)):
            raise RuntimeOwnershipError("embedded root already owned; no timeout takeover")
        if legacy:
            pending = self._converted(record.value)
            pending.head["owner"] = owner
            pending.head["fence"] = record.value["fence"] + 1
            self._cached = self._publish(record, pending, invalidate=None)
        else:
            value = deepcopy(record.value)
            value["owner"] = owner
            value["fence"] += 1
            self._cached = self.journal.commit(record, value)
        self.authority = (owner, self._cached.value["fence"])

    def release(self):
        if self.authority is None:
            return
        try:
            self.flush()
            record = self.journal.read()
            self._validate(record.value)
            self._owned(record.value)
            value = deepcopy(record.value)
            value["owner"] = None
            self.journal.commit(record, value)
        finally:
            self.authority = None
            self._cached = None
            self._held = None
            self._documents = {}

    def close(self):
        pass  # The enclosing execution lease releases ownership.

    def _record(self):
        if self.poisoned:
            raise RuntimeOwnershipError("uncertain root transaction; reopen/reconcile before continuing")
        record = self._cached if self.cache_owned and self._cached is not None else self.journal.read()
        self._validate(record.value)
        self._owned(record.value)
        return record

    def refresh(self):
        self.flush()
        self.reread()

    def reread(self):
        """Read the head again and check this handle still owns it; nothing held is published.

        The record this handle last committed or read is offered as known, so an unchanged head costs only
        its GET (the transaction it names is immutable and already in hand).
        """
        known, self._cached = self._cached, None
        if self.poisoned:
            raise RuntimeOwnershipError("uncertain root transaction; reopen/reconcile before continuing")
        if known is not None and isinstance(self.journal, CanonicalJournal):
            record = self.journal.read(known=known)
        else:
            record = self.journal.read()
        self._validate(record.value)
        self._owned(record.value)
        self._cached = record

    def _poison(self):
        self.poisoned = True
        self._cached = None
        self._held = None
        self._documents = {}

    # Transactions ------------------------------------------------------------
    def _view(self):
        """The state reads see: the open transaction's, else what is held, else the committed state."""
        pending = self._transaction.get()
        if pending is not None:
            return pending
        record = self._record()
        return self._held if self._held is not None else _Pending(record.value)

    @contextmanager
    def transaction(self):
        if self._transaction.get() is not None:
            raise RuntimeError("nested embedded transaction")
        record = self._record()
        base = self._held
        pending = base.copy() if base is not None else _Pending(deepcopy(record.value))
        token = self._transaction.set(pending)
        try:
            yield self
            self._validate(pending.head)
            if self.cache_owned and self._coalescing.get():
                self._held = pending if pending.changed(record.value) else None
            elif pending.changed(record.value):
                self._cached = self._publish(record, pending, invalidate=self._poison)
                self._held = None
            else:
                self._held = None
        finally:
            self._transaction.reset(token)

    @contextmanager
    def coalesced(self):
        """Hold this task's transactions in memory and publish them in one commit when the scope ends.

        Only an owning handle coalesces; elsewhere every transaction commits as usual. A transaction made by
        another task meanwhile publishes everything held together with itself. Leaving the scope, even by an
        exception, publishes what is held, exactly as the separate commits would have.
        """
        if not self.cache_owned or self._coalescing.get():
            yield self
            return
        token = self._coalescing.set(True)
        try:
            yield self
        finally:
            self._coalescing.reset(token)
            self.flush()

    def flush(self):
        """Publish held transactions now. A no-op when nothing is held."""
        if self._held is None:
            return
        record = self._record()
        self._cached = self._publish(record, self._held, invalidate=self._poison)
        self._held = None

    def _write(self, fn):
        pending = self._transaction.get()
        if pending is not None:
            return fn(pending)
        with self.transaction():
            return fn(self._transaction.get())

    def _append(self, before, appends):
        """Write every new record of every lake concurrently; return each lake's (coverage, refs).

        Each record goes to the next free sequence after the lake's coverage, all at once. A repeated identical
        record (a conversion or commit redone after its head never moved) is accepted where it stands. Only if a
        different record holds one of those sequences does that lake fall back to ``RecordLake.append``, which
        skips it. Nothing written here is visible until the head commit stores the returned coverage.
        """
        starts = {lake: coverage_in(before, self.lakes[lake]) for lake in appends}
        tasks = [(lake, starts[lake].last + offset, value) for lake in appends for offset, value in enumerate(appends[lake], 1)]

        def put(task):
            lake, seq, value = task
            try:
                return self.lakes[lake].put(seq, value)
            except RecordConflict:
                return None

        refs = dict(zip(((lake, seq) for lake, seq, _ in tasks), _parallel(put, tasks)))
        result = {}
        for lake, values in appends.items():
            mine = [refs[(lake, starts[lake].last + offset)] for offset in range(1, len(values) + 1)]
            if all(ref is not None for ref in mine):
                result[lake] = (Coverage(starts[lake].last + len(values), starts[lake].skipped), mine)
            else:
                result[lake] = self.lakes[lake].append(starts[lake], values)
        return result

    def _publish(self, record, pending, *, invalidate):
        """Records, then keyed entries, then one head commit. Nothing is visible before the head moves."""
        before = record.value
        appends = {lake: list(values) for lake, values in pending.appends.items() if values}
        if pending.keys:
            stamp = {"nonce": str(uuid4()), "keys": sorted(f"{kind}/{key}" for kind, key in pending.keys)}
            appends["commits"] = appends.get("commits", []) + [stamp]

        state = deepcopy(pending.head)
        state[RECORDS_KEY] = deepcopy(before.get(RECORDS_KEY) or {})
        written = {}
        for lake, (coverage, refs) in self._append(before, appends).items():
            state = with_coverage(state, self.lakes[lake], coverage)
            written[lake] = refs
        documents = {}
        if pending.keys:
            at = written["commits"][-1].seq
            commits = coverage_in(before, self.lakes["commits"])

            def store(item):
                (kind, key), value = item
                document = self._documents.get((kind, key))
                return (kind, key), self.keyed.store(kind, key, document, commits, at, value)

            try:
                documents = dict(_parallel(store, pending.keys.items()))
            except BaseException:
                for name in pending.keys:
                    self._documents.pop(name, None)
                raise
        committed = commit_with_contract(self.journal, record, state, invalidate=invalidate, invalidate_on_conflict=True)
        for lake, refs in written.items():
            for ref, value in zip(refs, appends[lake]):
                self._records[(lake, ref.seq)] = deepcopy(value)
        self._documents.update(documents)
        return committed

    # Reading records and keyed documents ------------------------------------
    def _coverage(self, pending, lake):
        return coverage_in(pending.head, self.lakes[lake])

    def _count(self, pending, lake):
        return _count(self._coverage(pending, lake)) + len(pending.appends.get(lake, ()))

    def _record_value(self, lake, seq):
        key = (lake, seq)
        if key not in self._records:
            self._records[key] = self.lakes[lake].read(seq)
        return self._records[key]

    def _positions(self, pending, lake, start, stop):
        """Values at positions ``start..stop`` (inclusive, 1-based) of a lake, committed or pending."""
        coverage = self._coverage(pending, lake)
        committed = _count(coverage)
        staged = pending.appends.get(lake, [])
        seqs = [_seq(coverage, position) for position in range(max(start, 1), min(stop, committed) + 1)]
        values = _parallel(lambda seq: self._record_value(lake, seq), seqs)
        first = max(start, committed + 1)
        return values + staged[first - committed - 1 : max(stop - committed, 0)]

    def _at(self, pending, lake, position):
        found = self._positions(pending, lake, position, position)
        return found[0] if found else None

    def _all(self, pending, lake):
        return self._positions(pending, lake, 1, self._count(pending, lake))

    def _document(self, kind, key):
        name = (kind, key)
        if name not in self._documents:
            self._documents[name] = self.keyed.load(kind, key)
        return self._documents[name]

    def _value(self, pending, kind, key):
        if (kind, key) in pending.keys:
            return pending.keys[(kind, key)]
        return resolve(self._document(kind, key), self._coverage(pending, "commits"))

    def _values(self, pending, kind, keys):
        return _parallel(lambda key: self._value(pending, kind, key), keys)

    def _item(self, pending, item_id):
        return {**_item_document(), **(self._value(pending, "item", item_id) or {})}

    # Conversion from v1 ---------------------------------------------------------
    def _converted(self, value):
        """The v1 state as a v2 control state plus every record and keyed document it implies."""
        data = value["data"]
        for name in ("events", "usage"):
            if [row[0] for row in data[name]] != list(range(1, len(data[name]) + 1)):
                raise ValueError(f"v1 root {name} ids are not 1..n; refusing to renumber them")
        head = empty()
        head["data"].update(meta=data["meta"], reviews=data["reviews"], learning=data["learning"])
        pending = _Pending(head)
        items = {}

        def reference(held):
            # Values held inline become content-addressed payloads (create-only, harmless if the head never moves).
            if isinstance(held, dict):
                return held
            stored = self.journal.payloads.put(held.encode())
            return {"digest": stored.digest, "length": stored.length}

        refs = _parallel(reference, [held for held, _ in data["items"].values()])
        for (item_id, (_, fingerprint)), held in zip(data["items"].items(), refs):
            pending.append("items", item_id)
            items[item_id] = {**_item_document(), "values": held, "fingerprint": fingerprint}
        for position, (identifier, item_id, fingerprint, state, payload) in enumerate(data["decisions"], start=1):
            pending.append("decisions", {"id": identifier, "item": item_id, "fingerprint": fingerprint, "payload": payload})
            item = items.setdefault(item_id, _item_document())
            previous, item["latest"] = item["latest"], identifier
            if state != SUPERSEDED:
                item["live"], item["walk"] = identifier, False
            pending.keys[("decision", identifier)] = {
                "n": position,
                "item": item_id,
                "fingerprint": fingerprint,
                "state": state,
                "previous": previous,
            }
            head["data"]["latest"] = identifier
        for item_id, document in items.items():
            pending.keys[("item", item_id)] = document
        for _, payload in data["events"]:
            pending.append("events", payload)
        shares, late = {}, {}
        for usage_id, classifier, share in data["shares"]:
            if 1 <= usage_id <= len(data["usage"]):
                shares.setdefault(usage_id, []).append([classifier, share])
            else:
                late.setdefault(usage_id, {})[classifier] = share
        for row in data["usage"]:
            pending.append("usage", {"row": row[1:], "shares": shares.get(row[0], [])})
        for usage_id, held in late.items():
            pending.keys[("usage", str(usage_id))] = {"shares": held}
        head["data"]["late_shares"] = sorted(late)
        return pending

    # Metadata --------------------------------------------------------------------
    def meta(self, key):
        data = self._view().head["data"]["meta"]
        return (data[key],) if key in data else None

    def insert_meta(self, key, payload, *, ignore=False):
        def apply(pending):
            meta = pending.head["data"]["meta"]
            if key in meta:
                if ignore:
                    return
                raise ValueError("duplicate root metadata")
            meta[key] = payload

        self._write(apply)

    def put_meta(self, key, payload):
        self._write(lambda pending: pending.head["data"]["meta"].__setitem__(key, payload))

    # Items -------------------------------------------------------------------------
    def put_item(self, identifier, payload, fingerprint):
        # The values are one content-addressed record; the item's keyed document keeps only its reference.
        stored = self.journal.payloads.put(payload.encode())
        ref = {"digest": stored.digest, "length": stored.length}

        def apply(pending):
            item = self._item(pending, identifier)
            if item["values"] is None:
                pending.append("items", identifier)
            item.update(values=ref, fingerprint=fingerprint)
            pending.keys[("item", identifier)] = item

        self._write(apply)

    def _item_text(self, ref):
        return self.journal.payloads.get(PayloadRef(self.journal.payloads.scope, ref["digest"], ref["length"])).decode()

    def items(self):
        pending = self._view()
        identifiers = self._all(pending, "items")
        documents = self._values(pending, "item", identifiers)
        texts = _parallel(lambda document: self._item_text(document["values"]), documents)
        return list(zip(identifiers, texts))

    def item_values(self, identifier):
        item = self._value(self._view(), "item", identifier)
        return self._item_text(item["values"]) if item and item.get("values") else None

    # Decisions ---------------------------------------------------------------------
    def insert_decision(self, row):
        identifier, item_id, fingerprint, state, payload = row

        def apply(pending):
            if self._value(pending, "decision", identifier) is not None:
                raise ValueError("duplicate decision identity")
            position = self._count(pending, "decisions") + 1
            pending.append("decisions", {"id": identifier, "item": item_id, "fingerprint": fingerprint, "payload": payload})
            item = self._item(pending, item_id)
            previous, item["latest"] = item["latest"], identifier
            if state != SUPERSEDED:
                item["live"], item["walk"] = identifier, False
            pending.keys[("item", item_id)] = item
            pending.keys[("decision", identifier)] = {
                "n": position,
                "item": item_id,
                "fingerprint": fingerprint,
                "state": state,
                "previous": previous,
            }
            pending.head["data"]["latest"] = identifier

        self._write(apply)

    def set_decision_state(self, identifier, state, *, unless_superseded=False):
        def apply(pending):
            decision = self._value(pending, "decision", identifier)
            if decision is None or (unless_superseded and decision["state"] == SUPERSEDED) or decision["state"] == state:
                return
            pending.keys[("decision", identifier)] = {**decision, "state": state}
            item = self._item(pending, decision["item"])
            if state == SUPERSEDED:
                if item["live"] != identifier:
                    return  # an older decision, or one already behind the item's live pointer
                # The newest live decision is now at or before the previous one; found when it is asked for.
                item["live"], item["walk"] = decision["previous"], decision["previous"] is not None
            elif item["live"] is None:
                item["live"], item["walk"] = identifier, False
            else:
                live = self._value(pending, "decision", item["live"])
                # Walking: the walk starts at the live pointer. Otherwise it is the newest live decision.
                if not (decision["n"] >= live["n"] if item["walk"] else decision["n"] > live["n"]):
                    return  # older than the newest live one, which stays (or is still found by walking)
                item["live"], item["walk"] = identifier, False
            pending.keys[("item", decision["item"])] = item

        self._write(apply)

    def decision_count(self):
        return self._count(self._view(), "decisions")

    def _decision_records(self):
        return self._all(self._view(), "decisions")

    def decision_rows(self):
        pending = self._view()
        records = self._all(pending, "decisions")
        states = self._values(pending, "decision", [r["id"] for r in records])
        return [(r["id"], state["state"], r["payload"]) for r, state in zip(records, states)]

    def decision_ids(self):
        return [(r["id"],) for r in self._decision_records()]

    def decision_items(self):
        return [(r["id"], r["item"]) for r in self._decision_records()]

    def decision_payloads(self):
        return [(r["payload"],) for r in self._decision_records()]

    def window_decisions(self, limit, offset):
        records = self._positions(self._view(), "decisions", offset + 1, offset + limit)
        return [(r["id"], r["payload"]) for r in records]

    def decision(self, identifier):
        pending = self._view()
        decision = self._value(pending, "decision", identifier)
        return None if decision is None else (self._at(pending, "decisions", decision["n"])["payload"],)

    def decision_fingerprint(self, identifier):
        decision = self._value(self._view(), "decision", identifier)
        if decision is None:
            raise StorageMissing(f"unknown decision {identifier}")
        return decision["fingerprint"]

    def latest_decision(self, item_id):
        pending = self._view()
        item = self._value(pending, "item", item_id)
        current = item["live"] if item else None
        while current is not None:
            decision = self._value(pending, "decision", current)
            if decision["state"] != SUPERSEDED:
                return (decision["state"], self._at(pending, "decisions", decision["n"])["payload"])
            if not item["walk"]:
                break
            current = decision["previous"]
        return None

    def latest_id(self, item_id=None):
        pending = self._view()
        if item_id is None:
            latest = pending.head["data"]["latest"]
        else:
            item = self._value(pending, "item", item_id)
            latest = item["latest"] if item else None
        return None if latest is None else (latest,)

    # Reviews (small mutable state in the head) ---------------------------------------
    def _reviews(self):
        return self._view().head["data"]["reviews"]

    def insert_review(self, identifier, decision_id, classifier, payload):
        def apply(pending):
            reviews = pending.head["data"]["reviews"]
            if any(r[1] == identifier for r in reviews):
                raise ValueError("duplicate review identity")
            reviews.append([len(reviews) + 1, identifier, decision_id, classifier, payload])

        self._write(apply)

    def review(self, identifier):
        return next(((r[4],) for r in self._reviews() if r[1] == identifier), None)

    def reviews(self, decision_id, classifier):
        return [(r[4],) for r in self._reviews() if (r[2], r[3]) == (decision_id, classifier)]

    def last_review(self, decision_id, classifier):
        return next(((r[4],) for r in reversed(self._reviews()) if (r[2], r[3]) == (decision_id, classifier)), None)

    def review_count(self, classifier):
        return sum(r[3] == classifier for r in self._reviews())

    # Events --------------------------------------------------------------------------
    def append_event(self, payload):
        def apply(pending):
            identifier = self._count(pending, "events") + 1
            pending.append("events", payload)
            return identifier

        return self._write(apply)

    def events(self, after, limit):
        pending = self._view()
        payloads = self._positions(pending, "events", after + 1, min(after + limit, self._count(pending, "events")))
        return [(after + offset, payload) for offset, payload in enumerate(payloads, start=1)]

    # Usage ---------------------------------------------------------------------------
    def insert_usage(self, decision_id, model, prompt, cached, output, source):
        def apply(pending):
            identifier = self._count(pending, "usage") + 1
            pending.append("usage", {"row": [decision_id, model, prompt, cached, output, source], "shares": []})
            return identifier

        return self._write(apply)

    def insert_usage_share(self, usage_id, classifier, share):
        def apply(pending):
            committed = _count(self._coverage(pending, "usage"))
            if committed < usage_id <= self._count(pending, "usage"):
                # Recorded with its usage row, which is still being written.
                row = pending.appends["usage"][usage_id - committed - 1]
                if any(name == classifier for name, _ in row["shares"]):
                    raise ValueError("duplicate usage share")
                row["shares"].append([classifier, share])
                return
            # A share added after its usage row was written lives in that row's keyed document.
            recorded = self._at(pending, "usage", usage_id)["shares"] if 1 <= usage_id <= committed else []
            late = self._value(pending, "usage", str(usage_id)) if usage_id in pending.head["data"]["late_shares"] else None
            held = dict((late or {}).get("shares", {}))
            if classifier in held or any(name == classifier for name, _ in recorded):
                raise ValueError("duplicate usage share")
            held[classifier] = share
            pending.keys[("usage", str(usage_id))] = {"shares": held}
            pending.head["data"]["late_shares"] = sorted(set(pending.head["data"]["late_shares"]) | {usage_id})

        self._write(apply)

    def _usage(self):
        return self._all(self._view(), "usage")

    def usage_counts(self):
        return [tuple(r["row"][:5]) for r in self._usage()]

    def usage_shares(self):
        pending = self._view()
        rows = [(n, name, share) for n, r in enumerate(self._all(pending, "usage"), start=1) for name, share in r["shares"]]
        late = pending.head["data"]["late_shares"]
        for usage_id, document in zip(late, self._values(pending, "usage", [str(n) for n in late])):
            rows.extend((usage_id, name, share) for name, share in document["shares"].items())
        return rows

    def usage_by_output(self):
        return [(n, *r["row"][1:]) for n, r in enumerate(self._usage(), start=1)]

    # Learning queue (small mutable state in the head) ----------------------------------
    def _learning(self):
        return self._view().head["data"]["learning"]

    def enqueue_learning(self, classifier, since_count, at):
        def apply(pending):
            learning = pending.head["data"]["learning"]
            if any(r[1] == classifier for r in learning):
                return
            learning.append([max((r[0] for r in learning), default=0) + 1, classifier, since_count, at, 0])

        self._write(apply)

    def pending_learning(self):
        return [r[1] for r in self._learning()]

    def next_learning(self):
        return next((tuple(r[:3]) for r in self._learning()), None)

    def learning_rows(self):
        return [(r[0], r[1], r[2], r[4]) for r in self._learning()]

    def supersede_learning(self, seq):
        def apply(pending):
            for row in pending.head["data"]["learning"]:
                if row[0] == seq:
                    row[4] += 1

        self._write(apply)

    def requeue_learning(self, seq, covered):
        def apply(pending):
            for row in pending.head["data"]["learning"]:
                if row[0] == seq:
                    row[2] = covered
                    row[4] = 0

        self._write(apply)

    def delete_learning(self, seq):
        def apply(pending):
            data = pending.head["data"]
            data["learning"] = [r for r in data["learning"] if r[0] != seq]

        self._write(apply)


class S3CyclotronStores:
    """Explicit composition seam, not hosted deployment or an account directory.

    Host binds all journals to an already-authorized scope and absolute shared
    request ceiling. No FileLease, local directory, SQLite or DDB fallback exists.
    """

    requires_durable_optimizer = True

    def __init__(self, root_journal, *, journal_for):
        if not callable(journal_for):
            raise ValueError("fixed scoped journal factory required")
        self.root_storage = S3EmbeddedStorage(root_journal, cache_owned=True)
        self.journal_for = journal_for
        self.owner = str(uuid4())
        self.lease = self
        self.batch_storage = None
        self.runtimes = {}
        self.prepared = {}  # Acquired together by open_classifier_runtimes, not yet taken by an output.

    def _journal(self, domain, identifier):
        root = self.root_storage.journal
        name = sha256((root.namespace + "\0" + identifier).encode()).hexdigest()
        result = self.journal_for(domain, name)
        if not isinstance(result, Journal) or result.boundary != root.boundary:
            raise PermissionError("cloud composition journal scope mismatch")
        return result

    def acquire(self):
        self.root_storage.initialize()
        self.root_storage.acquire(self.owner)
        try:
            ledger = self._journal("shared", "ledger")
            # Raw provider replies are write-once records beside the ledger, not rows inside it.
            self.batch_storage = S3BatchStorage(ledger, replies=reply_lake_for(ledger, "ledger"))
            self.batch_storage.initialize()
        except BaseException:
            self.root_storage.release()
            raise

    def _open_runtime(self, identifier):
        journal = self._journal("runtime", identifier)
        runtime = S3RuntimeStores(journal, cache_owned=True, events_lake=runtime_events_lake(journal))
        runtime.initialize()
        runtime.acquire(self.owner)
        return runtime

    def open_classifier_runtimes(self, identifiers):
        """Initialize and acquire every named output's runtime at once, ready for ``runtime_for_classifier``.

        All or nothing: when any one cannot be acquired, the ones that were are released again and the first
        failure in the given order is raised.
        """
        identifiers = list(identifiers)
        if len(set(identifiers)) != len(identifiers) or set(identifiers) & (set(self.runtimes) | set(self.prepared)):
            raise ValueError("classifier runtime already opened")
        opened = {}

        def open_one(identifier):
            opened[identifier] = self._open_runtime(identifier)

        try:
            call_concurrently([lambda identifier=identifier: open_one(identifier) for identifier in identifiers])
        except BaseException:
            try:
                call_concurrently([runtime.close for runtime in opened.values()])
            except BaseException:  # noqa: BLE001 - the acquire failure is the one to report
                pass
            raise
        self.prepared.update({identifier: opened[identifier] for identifier in identifiers})

    def runtime_for_classifier(self, identifier):
        if identifier in self.runtimes:
            raise ValueError("classifier runtime already opened")
        runtime = self.prepared.pop(identifier) if identifier in self.prepared else self._open_runtime(identifier)
        self.runtimes[identifier] = runtime
        return runtime

    def release_classifier_runtime(self, identifier):
        runtime = self.runtimes.pop(identifier, None)
        if runtime is not None:
            runtime.close()

    def execution_boundary(self):
        # Called only after the public per-call human/tenant guard succeeds.
        # Native ownership excludes other writers; CAS still fences every write. The heads are still read
        # here, so a fence an operator moved is refused before this call reads state or pays a provider
        # (a stale handle's own write would fail only after the paid call). They are independent GETs of
        # immutable-backed heads, so they go out together: one round trip, however many runtimes are open.
        # The shared ledger has no owner; its reads share one head for this call, its writes still read the
        # live head first.
        self.root_storage.flush()
        call_concurrently([self.root_storage.reread, *(runtime.refresh for runtime in self.runtimes.values())])
        if self.batch_storage is not None:
            self.batch_storage.pin_reads()

    def flush(self):
        """Publish what the outputs and the root hold in memory, the outputs at once. Ownership is kept."""
        call_concurrently([runtime.flush for runtime in self.runtimes.values()])
        self.root_storage.flush()

    @staticmethod
    def run_concurrently(calls):
        """An engine's independent per-output calls over these stores, at once (see ``concurrent_calls``)."""
        return call_concurrently(calls)

    def release(self):
        errors = []
        runtimes = [*self.runtimes.values(), *self.prepared.values()]
        self.runtimes, self.prepared = {}, {}
        try:
            call_concurrently([runtime.close for runtime in runtimes])
        except BaseException as error:
            errors.append(error)
        try:
            self.root_storage.release()
        except BaseException as error:
            errors.append(error)
        if errors:
            raise errors[0]

"""S3-authoritative shared batches, atomic paid budget and fenced dispatch claims.

One explicitly bounded run ledger; immutable transaction values retain exact text
and all charged history. No SQLite connection or DynamoDB authority is used.

Raw provider replies. With a ``replies`` record lake, a completed request keeps its raw reply as a
write-once record and the journal keeps only a small note (sequence, SHA-256, length). Reservation,
the paid ceiling and the dispatch claim stay in the control state, exactly as before, and the
reply is written FIRST, then ONE commit flips the request to complete (``commit_with_records``).
Crash analysis, by the point where the process stops:

1. After reserve, before claim: one attempt is charged, nothing dispatched. Same as without a lake.
2. After claim, before or during the provider call: claimed and pending. Never dispatched again.
3. Provider answered, reply not yet written: the reply is lost, the claim stays. Same as without a lake.
4. Reply record written, control state not yet flipped: the record is an orphan nobody can read. The
   request is still pending and claimed, so it is never paid for again, and usage is unchanged (usage
   is the attempt count, which only ``reserve`` changes). The same owner repeating the identical
   settlement adopts the orphan's sequence and flips the state.
5. Flip commit outcome unknown (lost response): ``BatchUncertain``. Repeating the settlement either
   finds the flip landed (identical reply, returned as is) or finds it did not and flips it.
6. Flip landed: the request is complete; cache hits read the record by reference.

If writing the reply record itself fails (no records permission, record over budget), the reply is
embedded in the journal as before rather than dropped, because the provider has already been paid.
Replies embedded by earlier versions keep working; a later conversion moves them into the lake.
"""

import json
import logging
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256

from .batch_storage import (
    BatchConflict,
    BatchInProgress,
    BatchRecord,
    BatchUncertain,
    is_reply_reference,
    validate_request,
    validate_snapshot,
)
from .decision_cache import CacheMiss
from .journal import Journal, JournalConflict, JournalUncertain
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
from .storage_payloads import FilePayloads, S3Payloads

log = logging.getLogger(__name__)

EMPTY = {"schema": "cyclotron-shared-batches/v1", "batches": [], "attempts": [], "history": []}


REPLY_RECORD_BYTES = 4 * 1024 * 1024


def reply_lake_for(journal, ledger, *, max_record_bytes=REPLY_RECORD_BYTES):
    """The reply lake that sits beside a ledger journal, on the same store, or None for an unknown store.

    One lake per ledger (``replies-<ledger>``) so ledgers that share a scope never share a sequence.
    """
    payloads, kind = getattr(journal, "payloads", None), f"replies-{ledger}"
    if isinstance(payloads, S3Payloads):
        return S3RecordLake(payloads, "shared-batches", kind, max_record_bytes=max_record_bytes)
    if isinstance(payloads, FilePayloads):
        return FileRecordLake(payloads, "shared-batches", kind, max_record_bytes=max_record_bytes)
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


@dataclass(frozen=True)
class RepliesConverted:
    originals: list  # (fingerprint, owner, generation, text) in the order they were written
    refs: list  # RecordRef of each reply record written
    before: object  # JournalRecord before the commit
    after: object  # JournalRecord the commit produced


class S3BatchStorage:
    def __init__(self, journal, *, max_records, max_conflicts=8, replies=None):
        if not isinstance(journal, Journal):
            raise ValueError("canonical journal required")
        if replies is not None and (not isinstance(replies, RecordLake) or replies.scope != journal.boundary[0]):
            raise ValueError("reply record lake must belong to the journal's scope")
        self.replies = replies
        if type(max_records) is not int or max_records < 1 or type(max_conflicts) is not int or not 1 <= max_conflicts <= 100:
            raise ValueError("explicit record budget and bounded contention retries required")
        self.journal = journal
        self.max_records = max_records
        self.max_conflicts = max_conflicts

    def _check(self, value, max_records=None):
        if not isinstance(value, dict) or set(value) not in (set(EMPTY), set(EMPTY) | {RECORDS_KEY}):
            raise ValueError("complete shared batch state required")
        validate_snapshot(value, replies=True)
        cited = [r[2] for r in value["batches"] if is_reply_reference(r[2])]
        cited += [r[4] for r in value["history"] if is_reply_reference(r[4])]
        if cited:
            if self.replies is None:
                raise ValueError("this ledger cites reply records; a reply record lake is required")
            coverage = coverage_in(value, self.replies)
            if not all(coverage.covers(ref["seq"]) for ref in cited) or len({ref["seq"] for ref in cited}) != len(cited):
                raise ValueError("reply references must name distinct covered records")
        if sum(len(value[k]) for k in ("batches", "attempts", "history")) > (
            self.max_records if max_records is None else min(max_records, self.max_records)
        ):
            raise ValueError("shared batch record budget exceeded; no history truncation")
        return value

    def initialize(self):
        self._check(self.journal.initialize(deepcopy(EMPTY)).value)

    def _read(self):
        record = self.journal.read()
        self._check(record.value)
        return record

    def _mutate(self, change):
        # Only pure domain changes are retried after an explicit CAS conflict.
        # Transport ambiguity propagates; a successful dispatch claim is NEVER
        # inferred from rereading a claimed row after an uncertain PUT.
        for _ in range(self.max_conflicts):
            expected = self._read()
            state = deepcopy(expected.value)
            changed, result = change(state)
            if not changed:
                return result
            self._check(state)
            try:
                self.journal.commit(expected, state)
            except JournalConflict:
                continue
            except JournalUncertain:
                raise BatchUncertain("S3 transaction uncertain; reconcile before provider dispatch") from None
            return result
        raise BatchConflict("S3 run contention limit reached; retry operation with its same owner")

    @staticmethod
    def _row(state, fingerprint):
        return next((r for r in state["batches"] if r[0] == fingerprint), None)

    def _reply(self, state, fingerprint, owner, generation, reference):
        """The raw reply a reference names, read from the lake and checked against the reference."""
        if self.replies is None or not coverage_in(state, self.replies).covers(reference["seq"]):
            raise RecordCorrupt("reply reference is not covered by the control state")
        value = self.replies.read(reference["seq"])
        try:
            if (
                set(value) != {"fingerprint", "owner", "generation", "payload"}
                or (value["fingerprint"], value["owner"], value["generation"]) != (fingerprint, owner, generation)
                or reply_reference(reference["seq"], value["payload"]) != reference
            ):
                raise ValueError()
        except Exception:
            raise RecordCorrupt("reply record does not match its reference") from None
        return value["payload"]

    def _record(self, state, row, *, payload=True):
        if not row:
            return None
        text = row[2]
        if is_reply_reference(text):
            text = self._reply(state, row[0], row[4], row[3], text) if payload else None
        return BatchRecord(row[0], row[1], text, row[3], row[4], row[5])

    def get(self, fingerprint, *, payload=True):
        """``payload=False`` skips the lake read; a reply held in the lake then shows as None."""
        state = self._read().value
        return self._record(state, self._row(state, fingerprint), payload=payload)

    def requests(self):
        return len(self._read().value["attempts"])

    def reserve(self, fingerprint, maximum, owner, *, policy="reuse", retry_failed=False):
        validate_request(fingerprint, owner)
        if (maximum is not None and (type(maximum) is not int or maximum < 0)) or policy not in (
            "reuse",
            "cache_only",
            "refresh",
        ):
            raise ValueError("valid request ceiling or None and policy required")

        def change(state):
            row = self._row(state, fingerprint)
            prior = next((r for r in state["attempts"] if r[2] == owner), None)
            if prior and prior[1] != fingerprint:
                raise BatchConflict("operation belongs to a different request")
            if row and row[4] == owner:
                return False, self._record(state, row)
            if prior:
                old = next((r for r in state["history"] if r[1] == fingerprint and r[5] == owner), None)
                if old:
                    text = old[4]
                    if is_reply_reference(text):
                        text = self._reply(state, fingerprint, owner, old[2], text)
                    return False, BatchRecord(fingerprint, old[3], text, old[2], owner, True)
                raise BatchConflict("operation result requires reconciliation")
            if row and row[1] == "pending":
                raise BatchInProgress("shared request in progress; explicit reconciliation required")
            if policy == "cache_only" and (not row or row[1] != "complete"):
                raise CacheMiss("complete batch not cached")
            if row and row[1] == "complete" and policy != "refresh":
                return False, self._record(state, row)
            if (
                row
                and row[1] != "complete"
                and not retry_failed
                and not (row[1] == "interrupted" and row[2] == '{"learning_abandoned":true}')
            ):
                raise RuntimeError("explicit retry required for interrupted batch")
            if maximum is not None and len(state["attempts"]) >= maximum:
                raise RuntimeError("run decision request ceiling reached")
            if row:
                state["history"].append([len(state["history"]) + 1, fingerprint, row[3], row[1], row[2], row[4]])
                generation, revision = row[3] + 1, row[6] + 1
                state["batches"].remove(row)
            else:
                generation, revision = 1, 1
            state["attempts"].append([len(state["attempts"]) + 1, fingerprint, owner])
            row = [fingerprint, "pending", None, generation, owner, False, revision]
            state["batches"].append(row)
            state["batches"].sort(key=lambda r: r[0])
            return True, self._record(state, row)

        return self._mutate(change)

    def claim(self, fingerprint, owner):
        validate_request(fingerprint, owner)

        def change(state):
            row = self._row(state, fingerprint)
            if not row or row[4] != owner or row[1] != "pending" or row[5]:
                return False, False
            row[5] = True
            row[6] += 1
            return True, True

        return self._mutate(change)

    def settle(self, fingerprint, owner, status, payload=None):
        validate_request(fingerprint, owner)
        if status not in ("complete", "failed", "interrupted") or status == "complete" and payload is None:
            raise ValueError("valid settlement required")
        if payload is not None:
            json.loads(payload)
        if self.replies is not None and status == "complete":
            try:
                return self._mutate_records(lambda state: self._plan_settle(state, fingerprint, owner, payload))
            except (RecordUncertain, RecordLimit, RecordConflict, RecordUnavailable) as error:
                # Only lake failures land here (journal outcomes are other exceptions). The provider has
                # been paid, so keep the reply in the journal the old way instead of dropping it.
                log.warning("reply record not written (%s); embedding the reply in the ledger", type(error).__name__)

        def change(state):
            row = self._row(state, fingerprint)
            if row and row[4] == owner and row[1] == status and same_payload(row[2], payload):
                return False, self._record(state, row)
            if not row or row[4] != owner or row[1] != "pending":
                raise BatchConflict("stale shared request owner")
            if status == "complete" and not row[5]:
                raise BatchConflict("completion requires dispatch claim")
            row[1] = status
            row[2] = payload
            row[6] += 1
            return True, self._record(state, row)

        return self._mutate(change)

    def _plan_settle(self, state, fingerprint, owner, payload):
        row = self._row(state, fingerprint)
        if row and row[4] == owner and row[1] == "complete" and same_payload(row[2], payload):
            return None, None, self._record(state, row)
        if not row or row[4] != owner or row[1] != "pending":
            raise BatchConflict("stale shared request owner")
        if not row[5]:
            raise BatchConflict("completion requires dispatch claim")
        generation, revision = row[3], row[6] + 1

        def apply(new_state, refs):
            target = self._row(new_state, fingerprint)
            target[1] = "complete"
            target[2] = reply_reference(refs[self.replies.name][0].seq, payload)
            target[6] = revision
            self._check(new_state)

        result = BatchRecord(fingerprint, "complete", payload, generation, owner, True)
        return [reply_value(fingerprint, owner, generation, payload)], apply, result

    def _mutate_records(self, plan):
        """Like ``_mutate`` for a change that first writes reply records, then flips the control state."""
        for _ in range(self.max_conflicts):
            expected = self._read()
            values, apply, result = plan(deepcopy(expected.value))
            if apply is None:
                return result
            try:
                commit_with_records(self.journal, expected, [(self.replies, values)], apply)
            except JournalConflict:
                continue
            except JournalUncertain:
                raise BatchUncertain("S3 transaction uncertain; reconcile before provider dispatch") from None
            return result
        raise BatchConflict("S3 run contention limit reached; retry operation with its same owner")

    @staticmethod
    def _embedded_slots(state):
        """Complete rows whose reply text is still inside the ledger, in conversion order: batches, then history."""
        slots = [(r, 2, r[0], r[4], r[3]) for r in state["batches"] if r[1] == "complete" and isinstance(r[2], str)]
        slots += [(r, 4, r[1], r[5], r[2]) for r in state["history"] if r[3] == "complete" and isinstance(r[4], str)]
        return slots

    def audit_replies(self):
        """Read-only: the head record and the embedded replies as ``(fingerprint, owner, generation, text)``."""
        record = self._read()
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
        """Move every reply still embedded in the ledger into the reply lake, in one commit.

        Reply records are written first (same record shape ``settle`` writes), then ONE commit swaps each
        embedded text for its ``{seq, sha256, length}`` note and stores the coverage. Reservation, claims,
        attempts and ordering are untouched. Needs the workspace ownership the live worker holds. Returns
        ``RepliesConverted`` or None when nothing is embedded (idempotent).
        """
        if self.replies is None:
            raise ValueError("a reply record lake is required to convert embedded replies")
        for _ in range(self.max_conflicts):
            record = self._read()
            slots = self._embedded_slots(record.value)
            if not slots:
                return None
            originals = [(f, o, g, row[index]) for row, index, f, o, g in slots]

            def apply(state, refs, originals=originals):
                cited = iter(refs[self.replies.name])
                for row, index, *_ in self._embedded_slots(state):
                    row[index] = reply_reference(next(cited).seq, row[index])
                self._check(state)

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

    def operation_mark(self):
        return len(self._read().value["attempts"])

    def pending_operations(self):
        state = self._read().value
        rows = {r[0]: r for r in state["batches"]}
        return [
            (ordinal, key, owner)
            for ordinal, key, owner in state["attempts"]
            if rows[key][1] == "pending" and rows[key][4] == owner
        ]

    def abandon_learning(self, fingerprint, owner):
        validate_request(fingerprint, owner)

        def change(state):
            row = self._row(state, fingerprint)
            if not row or row[4] != owner or row[1] != "pending":
                raise BatchConflict("stale shared request owner")
            if row[5]:
                raise BatchUncertain("claimed cloud request requires explicit provider reconciliation; never automatically repay")
            row[1] = "interrupted"
            row[2] = '{"learning_abandoned":true}'
            row[6] += 1
            return True, self._record(state, row)

        return self._mutate(change)

    def page(self, *, after=None, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000 or after is not None and not isinstance(after, str):
            raise ValueError("bounded page required")
        state = self._read().value
        rows = [r for r in state["batches"] if r[0] > (after or "")][: limit + 1]
        return [self._record(state, r) for r in rows[:limit]], rows[limit - 1][0] if len(rows) > limit else None

    def export(self, *, max_records=10000):
        """The portable snapshot, with every raw reply back inline, whichever way it is stored."""
        if type(max_records) is not int or max_records < 1:
            raise ValueError("bounded export required")
        state = deepcopy(self._check(self._read().value, max_records))
        snapshot = {name: state[name] for name in EMPTY}
        for row in snapshot["batches"]:
            if is_reply_reference(row[2]):
                row[2] = self._reply(state, row[0], row[4], row[3], row[2])
        for row in snapshot["history"]:
            if is_reply_reference(row[4]):
                row[4] = self._reply(state, row[1], row[5], row[2], row[4])
        validate_snapshot(snapshot)
        return snapshot

    def restore(self, snapshot):
        snapshot = deepcopy(snapshot)
        self._check(snapshot)

        if self.replies is None:

            def change(state):
                if state["attempts"] or state["batches"]:
                    raise BatchConflict("restore requires empty destination")
                state.update(deepcopy(snapshot))
                return True, None

            return self._mutate(change)

        # Replies go to the lake, in ledger order (batches, then history), and the rows cite them.
        slots = [(r, 2, r[0], r[4], r[3]) for r in snapshot["batches"] if r[1] == "complete"]
        slots += [(r, 4, r[1], r[5], r[2]) for r in snapshot["history"] if r[3] == "complete"]

        def plan(state):
            if state["attempts"] or state["batches"]:
                raise BatchConflict("restore requires empty destination")

            def apply(new_state, refs):
                restored = deepcopy(snapshot)
                cited = iter(refs[self.replies.name])
                rows = [r for r in restored["batches"] if r[1] == "complete"] + [
                    r for r in restored["history"] if r[3] == "complete"
                ]
                for row, (_, index, *_rest) in zip(rows, slots):
                    row[index] = reply_reference(next(cited).seq, row[index])
                new_state.update(restored)
                self._check(new_state)

            return [reply_value(f, o, g, row[index]) for row, index, f, o, g in slots], apply, None

        return self._mutate_records(plan)

    def close(self):
        pass  # Injected SDK clients belong to the host.

"""Bounded console domain transactions with S3 authority and no SQL fallback.

Fixed portable record fields are shared with native migration. Named domain
operations, not a SQL parser, own revisions, pins, feedback and command claims.
"""

import json
import time
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
from itertools import islice
from threading import RLock
from uuid import uuid4

from .catalog_cold_rows import (
    COLD_TABLES,
    TERMINAL,
    ColdRows,
    Legacy,
    in_review,
    is_evictable,
    load_legacy,
    partition,
    row_key,
)
from .catalog_storage import encoded, timestamp
from .console_snapshot import SCHEMA, SEQUENCES, TABLES
from .console_snapshot import validate as validate_console
from .cyclotrons import load_config
from .journal import Journal, commit_with_contract, decode, encode, uuid
from .models import DecisionTask
from .record_lake import (
    RECORDS_KEY,
    Coverage,
    FileRecordLake,
    S3RecordLake,
    commit_with_records,
    coverage_in,
    with_coverage,
)
from .run_storage import _UNSET
from .storage_errors import CommandNotReady, StorageConflict
from .storage_payloads import FilePayloads, PayloadRef, S3Payloads
from .value_records import externalized, resolved, same_values

# Completed job results live in the record lake; the catalog row keeps only this small reference.
RESULTS_DOMAIN, RESULTS_KIND = "console", "job-results"
RESULTS_LAKE = f"{RESULTS_DOMAIN}/{RESULTS_KIND}"
RESULT_REF = "cyclotron-job-result-ref/v1"
_REF_FIELDS = {"domain", "kind", "seq", "digest", "length"}


def result_ref(raw):
    """The record reference a job row's ``result`` text holds, or None for an embedded (legacy) result."""
    if not isinstance(raw, str) or RESULT_REF not in raw:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    if isinstance(parsed, dict) and set(parsed) == {"schema", "ref"} and parsed["schema"] == RESULT_REF:
        return parsed["ref"]
    return None


# A transient key in a change being built: item rows brought into the head for an edit, with their prior content.
_HYDRATED = "_hydrated"

# The worker Lambda times out after 3 minutes (infra/lab/template.yaml); a claim older than this never finishes.
STUCK_HEAD_MINUTES = 10


def same_claim(claim, owner, fence):
    """A claim is its owner and fence; the optional ``at`` only records when it was taken."""
    return isinstance(claim, dict) and claim.get("owner") == owner and claim.get("fence") == fence


def now():
    return datetime.now(timezone.utc).isoformat()


SCHEMA_V2 = "cyclotron-s3-console/v2"
SCHEMA_V3 = "cyclotron-s3-console/v3"
SCHEMA_V4 = "cyclotron-s3-console/v4"
# Tables that change rarely (runs, classifiers, definitions, drafts...). The head names each by the digest of
# its content-addressed payload, so a commit that leaves a table alone does not write it again.
STATIC_TABLES = tuple(t for t in TABLES if t not in COLD_TABLES)
# Tables a v3 head still held whole; a v4 head keeps their rows as records like any other growing table.
_V4_COLD = ("item_feedback", "item_feedback_retractions", "web_events", "item_predictions")


def empty_cold():
    """Counters and positions for the tables whose rows leave the head (catalog_cold_rows.py).

    ``rowids`` and ``ordinals`` only ever grow: a row keeps its rowid (its place in table order) and its ordinal
    (its position in its run or list) wherever it lives. ``hot`` holds the position of each row still in the head:
    a job's ordinal and whether its by-id pointers exist yet, an item's ordinal and slot, a revision's ordinal.
    ``next`` is each run's first job that has not started: jobs start strictly in acceptance order, so the run's
    queue is exactly the ordinals ``next`` to its counter, and the job just before ``next`` is the one running
    (if it has not finished). ``review`` is each run's items waiting for a human, as ordinal intervals.
    ``distinct`` counts the item ids of each list; ``claimed`` names the runs a worker has ever claimed in.
    """
    return {
        "rowids": {t: 0 for t in COLD_TABLES},
        "ordinals": {t: {} for t in COLD_TABLES},
        "hot": {t: {} for t in COLD_TABLES},
        "next": {},
        "review": {},
        "distinct": {},
        "claimed": [],
    }


def initial():
    return {
        "schema": SCHEMA_V4,
        "snapshot": {"schema": SCHEMA, "tables": {k: [] for k in TABLES}, "sequences": {k: 0 for k in SEQUENCES}},
        "claims": {},
        "actors": {},
        "cold": empty_cold(),
        "legacy": None,
        "parts": {},
    }


def with_ordinal(intervals, n, member):
    """A sorted list of disjoint ``[first, last]`` intervals, with ``n`` added (member) or taken out."""
    result = []
    for first, last in intervals:
        if first <= n <= last:
            result += [[first, n - 1]] if first < n else []
            result += [[n + 1, last]] if n < last else []
        else:
            result.append([first, last])
    if member:
        result.append([n, n])
    merged = []
    for first, last in sorted(result):
        if merged and first <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], last)
        else:
            merged.append([first, last])
    return merged


def queue_bounds(value, run_id):
    """(first unstarted ordinal, last accepted ordinal) of a run's queue; empty when first > last."""
    cold = value["cold"]
    last = cold["ordinals"]["web_jobs"].get(run_id, 0)
    return cold["next"].get(run_id, last + 1), last


def rows(value, domain, **fields):
    return [r for r in value["snapshot"]["tables"][domain] if all(r[k] == v for k, v in fields.items())]


def one(value, domain, **fields):
    return next(iter(rows(value, domain, **fields)), None)


def clean(row):
    return {k: deepcopy(v) for k, v in row.items() if k != "_rowid"}


def decoded(row, *fields):
    value = clean(row)
    for field in fields:
        value[field] = (
            (
                load_config(value[field])
                if field == "config" and "mode" in value and "status" in value
                else json.loads(value[field])
            )
            if value[field] is not None
            else None
        )
    return value


def insert(value, domain, fields):
    records = value["snapshot"]["tables"][domain]
    cold = value.get("cold")
    if cold is not None and domain in COLD_TABLES:
        cold["rowids"][domain] += 1
        record = {"_rowid": cold["rowids"][domain], **fields}
        if domain in SEQUENCES:
            value["snapshot"]["sequences"][domain] += 1
            record["_rowid"] = record[SEQUENCES[domain]] = cold["rowids"][domain] = value["snapshot"]["sequences"][domain]
        if set(record) != set(TABLES[domain]) | {"_rowid"}:
            raise ValueError("complete known domain record required")
        part = partition(domain, record)
        ordinal = cold["ordinals"][domain][part] = cold["ordinals"][domain].get(part, 0) + 1
        cold["hot"][domain][row_key(domain, record)] = (
            [ordinal, 1] if domain == "web_items" else [ordinal, 0] if domain == "web_jobs" else ordinal
        )
        if domain == "web_jobs":
            cold["next"].setdefault(part, ordinal)
        if domain == "list_item_revisions" and record["revision"] == 1:
            cold["distinct"][part] = cold["distinct"].get(part, 0) + 1
        records.append(record)
        return record
    record = {"_rowid": max((r["_rowid"] for r in records), default=0) + 1, **fields}
    if domain in SEQUENCES:
        value["snapshot"]["sequences"][domain] += 1
        record[SEQUENCES[domain]] = value["snapshot"]["sequences"][domain]
        record["_rowid"] = record[SEQUENCES[domain]]
    if set(record) != set(TABLES[domain]) | {"_rowid"}:
        raise ValueError("complete known domain record required")
    records.append(record)
    return record


# Tables whose rows refer to item revisions that may live outside the head; the head checks them row by row.
_REFERRING = ("list_item_revisions", "item_feedback", "item_predictions", "item_feedback_retractions")


def validate_hot(snapshot):
    """Validate a head snapshot: complete rows, with references only to what a head can see.

    A head holds only some jobs, items and revisions, so the whole-catalog checks that join them (complete
    revision history, feedback naming a revision) run on export. Writes check what they reference.
    """
    tables = snapshot.get("tables") if isinstance(snapshot, dict) else None
    if not isinstance(tables, dict) or not all(isinstance(tables.get(t), list) for t in _REFERRING):
        raise ValueError("complete native console tables required")
    validate_console({**snapshot, "tables": {**tables, **{t: [] for t in _REFERRING}}})
    from .console_snapshot import JSON_FIELDS, KEYS, NULLABLE

    for table in _REFERRING:
        identities = set()
        for row in tables[table]:
            if not isinstance(row, dict) or set(row) != set(TABLES[table]) | {"_rowid"} or type(row["_rowid"]) is not int:
                raise ValueError("invalid console row columns")
            for column in TABLES[table]:
                cell = row[column]
                if cell is None and (table, column) in NULLABLE:
                    continue
                if column in ("revision", "sequence", "classifier_revision", "item_revision"):
                    if type(cell) is not int or cell < 1:
                        raise ValueError("invalid native integer")
                elif not isinstance(cell, str):
                    raise ValueError("invalid native text")
            key = tuple(row[c] for c in KEYS[table])
            if key in identities:
                raise ValueError("duplicate console identity")
            identities.add(key)
            for column in JSON_FIELDS.get(table, ()):
                json.loads(row[column])
        if table == "list_item_revisions":
            for row in tables[table]:
                payload = json.loads(row["payload"])
                if (
                    not isinstance(payload, dict)
                    or payload.get("id") != row["id"]
                    or payload.get("occurred_at") != row["occurred_at"]
                    or sha256(row["payload"].encode()).hexdigest() != row["fingerprint"]
                ):
                    raise ValueError("invalid canonical item revision")
    watermark = snapshot["sequences"]["item_feedback"]
    if max((r["sequence"] for r in tables["item_feedback"]), default=0) > watermark:
        raise ValueError("invalid native sequence watermark")
    # Classifiers stay whole in the head, so every label in it is checked against its pinned classifier here.
    classifiers = {(r["id"], r["revision"]): r for r in tables["classifiers"]}
    feedback = set()
    for row in tables["item_feedback"]:
        classifier = classifiers.get((row["classifier_id"], row["classifier_revision"]))
        if classifier is None:
            raise ValueError("feedback references unknown pinned revisions")
        config = json.loads(classifier["config"])
        DecisionTask(row["classifier_id"], tuple(c["label"] for c in config["classes"]), config["question"]).validate_label(
            row["label"]
        )
        if row["request_id"] in feedback:
            raise ValueError("duplicate feedback identity")
        feedback.add(row["request_id"])
    return snapshot


def queue_start(statuses):
    """The first unstarted position (1-based) of a run whose jobs have these statuses in acceptance order.

    Jobs start strictly in acceptance order, so the unfinished jobs are the run's newest, at most the oldest of
    them running. A catalog that is not in that shape is refused: its queue needs an operator first.
    """
    unfinished = [n for n, status in enumerate(statuses, 1) if status in ("pending", "running")]
    if (
        unfinished != list(range(len(statuses) - len(unfinished) + 1, len(statuses) + 1))
        or "running" in statuses[(unfinished[0] if unfinished else len(statuses) + 1) :]
    ):
        raise ValueError("run queue is out of acceptance order; reconcile it before this catalog moves")
    return unfinished[0] + (statuses[unfinished[0] - 1] == "running") if unfinished else len(statuses) + 1


def _counted(cold, tables):
    """Fill counters, queue starts and review intervals from complete tables."""
    by_run = {}
    for table in COLD_TABLES:
        for row in tables[table]:
            cold["rowids"][table] = max(cold["rowids"][table], row["_rowid"])
            part = partition(table, row)
            ordinal = cold["ordinals"][table][part] = cold["ordinals"][table].get(part, 0) + 1
            if table == "web_jobs":
                by_run.setdefault(part, []).append(row["status"])
            if table == "web_items" and in_review(row):
                cold["review"][part] = with_ordinal(cold["review"].get(part, []), ordinal, True)
    for run_id, statuses in by_run.items():
        cold["next"][run_id] = queue_start(statuses)
    seen = set()
    for row in tables["list_item_revisions"]:
        if (row["list_id"], row["id"]) not in seen:
            seen.add((row["list_id"], row["id"]))
            cold["distinct"][row["list_id"]] = cold["distinct"].get(row["list_id"], 0) + 1
    return by_run


def adopt_snapshot(value, snapshot):
    """Make every row of a complete snapshot a head row, with fresh counters; the next commit moves them out."""
    value["snapshot"] = snapshot
    cold = value["cold"] = empty_cold()
    _counted(cold, snapshot["tables"])
    ordinals = {}
    for table in COLD_TABLES:
        for row in snapshot["tables"][table]:
            part = partition(table, row)
            ordinal = ordinals[table, part] = ordinals.get((table, part), 0) + 1
            meta = [ordinal, 1] if table == "web_items" else [ordinal, 0] if table == "web_jobs" else ordinal
            cold["hot"][table][row_key(table, row)] = meta


def _hold_all(cold, table, held_rows):
    """Give rows that stay in the head their positions (the newest commit files them as records)."""
    counted = {}
    for row in held_rows:
        part = partition(table, row)
        counted[part] = counted.get(part, 0) + 1
        cold["hot"][table][row_key(table, row)] = counted[part]


def migrated_v3(value):
    """A v3 head as a v4 head: labels, retractions, events and predictions get positions and leave next commit."""
    tables = value["snapshot"]["tables"]
    cold = {**value["cold"]}
    for key in ("rowids", "ordinals", "hot"):
        cold[key] = {**cold[key], **{t: ({} if key != "rowids" else 0) for t in _V4_COLD}}
    for table in _V4_COLD:
        for row in tables[table]:
            cold["rowids"][table] = max(cold["rowids"][table], row["_rowid"])
            part = partition(table, row)
            cold["ordinals"][table][part] = cold["ordinals"][table].get(part, 0) + 1
        _hold_all(cold, table, tables[table])
    return {**value, "schema": SCHEMA_V4, "cold": cold, "parts": {}}


def migrated(value, reference):
    """A v2 whole-state catalog as a v3 head: its old blob becomes the read-only legacy layer.

    Nothing is copied. The head keeps only the running job (with its claim and actor) and counters: each run's
    queue start, the review intervals and the position the legacy layer's unfinished jobs start at. Every other
    row stays readable in the immutable blob the head names, so an old catalog never has to move its rows.
    """
    tables = value["snapshot"]["tables"]
    cold = empty_cold()
    by_run = _counted(cold, tables)
    runs = {r["id"]: r["run_id"] for r in tables["web_jobs"]}
    cold["claimed"] = sorted({runs[job_id] for job_id in value["claims"] if job_id in runs})
    legacy = Legacy(value) if any(tables[t] for t in COLD_TABLES) else None
    hot = {t: [] for t in COLD_TABLES}
    for row in tables["web_jobs"]:
        if row["status"] == "running":
            hot["web_jobs"].append(row)
            cold["hot"]["web_jobs"][row["id"]] = [legacy.jobs[row["id"]]["ordinal"], 1]
    for table in _V4_COLD:
        hot[table] = tables[table]
        _hold_all(cold, table, tables[table])
    kept = {r["id"] for r in hot["web_jobs"]}
    opened = {}
    for run_id, statuses in by_run.items():
        unfinished = [n for n, status in enumerate(statuses, 1) if status in ("pending", "running")]
        if unfinished:
            opened[run_id] = unfinished[0]
    result = {
        **{k: v for k, v in value.items() if k not in ("schema", "snapshot", "claims", "actors")},
        "schema": SCHEMA_V4,
        "parts": {},
        "snapshot": {**value["snapshot"], "tables": {**tables, **hot}},
        "claims": {k: v for k, v in value["claims"].items() if k in kept},
        "actors": {k: v for k, v in value["actors"].items() if k in kept},
        "cold": cold,
        "legacy": None if legacy is None else {"digest": reference.digest, "length": reference.length, "open": opened},
    }
    return result, legacy


def results_lake_for(journal):
    """The job-results lake that sits beside a catalog journal, on the same store."""
    payloads = journal.payloads
    if isinstance(payloads, S3Payloads):
        return S3RecordLake(payloads, RESULTS_DOMAIN, RESULTS_KIND)
    if isinstance(payloads, FilePayloads):
        return FileRecordLake(payloads, RESULTS_DOMAIN, RESULTS_KIND)
    raise ValueError("an S3 or file payload adapter is required for the job-results lake")


@dataclass(frozen=True)
class ResultsConverted:
    originals: list  # (job id, run id, status, raw text) in the order the records were written
    refs: list  # RecordRef of each result record written
    before: object  # JournalRecord before the commit
    after: object  # JournalRecord the commit produced


class S3ConsoleStorage:
    def __init__(
        self,
        journal,
        *,
        max_conflicts=8,
        results=None,
        snapshot_cache=None,
        reuse_reads_within_request=False,
        phase_log=None,
    ):
        if results is not None and (results.scope != getattr(journal, "boundary", (None,))[0] or results.name != RESULTS_LAKE):
            raise ValueError("job result lake must match the catalog scope and be console/job-results")
        if not isinstance(journal, Journal) or type(max_conflicts) is not int or max_conflicts < 1:
            raise ValueError("scoped canonical console journal and a positive conflict retry count required")
        self.journal = journal
        self.max_conflicts = max_conflicts
        # Without a lake, results stay embedded exactly as before. With one, new results go to the lake
        # and embedded ones keep reading.
        self.results = results
        self._tx = ContextVar("s3_console_transaction", default=None)
        self._claim = ContextVar("s3_console_claim", default=None)
        self._granted = set()
        # Optional per-process memory of decoded catalogs, revalidated by the head on every read.
        self.snapshot_cache = snapshot_cache
        # Request-scoped stores hand every read of one request the same snapshot, until this store writes.
        self.reuse_reads_within_request = reuse_reads_within_request
        self.phase_log = phase_log
        self._request_snapshot = None
        self._request_lock = RLock()
        self._cold_rows = None
        self._legacy_index = {}
        self._parts = {}

    def release_request_snapshot(self):
        """Stop reusing one snapshot, for a store that outlives a single request."""
        with self._request_lock:
            self.reuse_reads_within_request = False
            self._request_snapshot = None

    def _validate(self, value):
        if (
            not isinstance(value, dict)
            or set(value) - {RECORDS_KEY} != set(initial())
            or value["schema"] != SCHEMA_V4
            or not isinstance(value["claims"], dict)
            or not isinstance(value["actors"], dict)
        ):
            raise ValueError("invalid canonical console transaction")
        validate_hot(value["snapshot"])
        self._validate_cold(value)
        held = value.get(RECORDS_KEY, {})
        if not isinstance(held, dict) or set(held) - {RESULTS_LAKE}:
            raise ValueError("invalid record coverage")
        coverage = Coverage.from_json(held[RESULTS_LAKE]) if RESULTS_LAKE in held else Coverage()
        for job in value["snapshot"]["tables"]["web_jobs"]:
            ref = result_ref(job["result"])
            if ref is not None and (
                not isinstance(ref, dict)
                or set(ref) != _REF_FIELDS
                or (ref["domain"], ref["kind"]) != (RESULTS_DOMAIN, RESULTS_KIND)
                or not coverage.covers(ref["seq"])
            ):
                raise ValueError("job result reference is not covered by the catalog")
        # One index of jobs by id: looking each claim and actor up by scanning every job made this quadratic, and
        # it ran on every uncached catalog read.
        jobs = {r["id"]: r for r in value["snapshot"]["tables"]["web_jobs"]}
        for job_id, claim in value["claims"].items():
            job = jobs.get(job_id)
            if job is None or set(claim) - {"at"} != {"owner", "fence"} or type(claim["fence"]) is not int or claim["fence"] < 1:
                raise ValueError("invalid command claim")
            uuid(claim["owner"])
        for job_id, actor in value["actors"].items():
            job = jobs.get(job_id)
            if not isinstance(actor, dict):
                raise ValueError("invalid queued actor")
            parsed = self._parse_actor(actor)
            if job is None or parsed.run_id != job["run_id"] or parsed.operation != "console:write":
                raise ValueError("queued actor run boundary refused")

    @staticmethod
    def _validate_cold(value):
        cold, tables = value["cold"], value["snapshot"]["tables"]
        if not isinstance(cold, dict) or set(cold) != set(empty_cold()):
            raise ValueError("invalid catalog counters")
        for table in COLD_TABLES:
            hot = cold["hot"][table]
            keys = [row_key(table, r) for r in tables[table]]
            if set(keys) != set(hot) or len(keys) != len(hot):
                raise ValueError("catalog head rows and positions disagree")
            order = [r["_rowid"] for r in tables[table]]
            if order != sorted(order) or (order and order[-1] > cold["rowids"][table]):
                raise ValueError("catalog head rows out of order")
            for row in tables[table]:
                held = hot[row_key(table, row)]
                ordinal = held[0] if table in ("web_jobs", "web_items") else held
                if type(ordinal) is not int or not 1 <= ordinal <= cold["ordinals"][table].get(partition(table, row), 0):
                    raise ValueError("catalog row position outside its counter")
                if table in ("web_jobs", "web_items") and (len(held) != 2 or type(held[1]) is not int or held[1] < 0):
                    raise ValueError("invalid catalog row slot")
        running = set()
        for row in tables["web_jobs"]:
            ordinal = cold["hot"]["web_jobs"][row["id"]][0]
            first, last = queue_bounds(value, row["run_id"])
            if row["status"] == "running":
                if ordinal != first - 1 or row["run_id"] in running:
                    raise ValueError("a running job must be its run's one started, unfinished job")
                running.add(row["run_id"])
            if row["status"] == "pending" and not first <= ordinal <= last:
                raise ValueError("a pending job must sit in its run's queue")
            if row["status"] in TERMINAL and ordinal >= first:
                raise ValueError("a finished job must have started")
        for run_id, first in cold["next"].items():
            if type(first) is not int or not 1 <= first <= cold["ordinals"]["web_jobs"].get(run_id, 0) + 1:
                raise ValueError("invalid run queue start")
        for run_id, spans in cold["review"].items():
            if with_ordinal(spans, 0, False) != spans or not spans or spans[0][0] < 1:
                raise ValueError("invalid review intervals")
            if spans[-1][1] > cold["ordinals"]["web_items"].get(run_id, 0):
                raise ValueError("review interval outside its run")
        legacy = value["legacy"]
        if legacy is not None and (
            not isinstance(legacy, dict) or set(legacy) != {"digest", "length", "open"} or not isinstance(legacy["open"], dict)
        ):
            raise ValueError("invalid legacy catalog reference")
        parts = value["parts"]
        if not isinstance(parts, dict) or set(parts) - set(STATIC_TABLES):
            raise ValueError("invalid catalog parts")
        for ref in parts.values():
            if not isinstance(ref, dict) or set(ref) != {"digest", "length"} or type(ref["length"]) is not int:
                raise ValueError("invalid catalog part reference")

    @staticmethod
    def _parse_actor(actor):
        from .machine_access import MachineActor
        from .queued_account_authorization import QueuedActor

        return MachineActor(**actor) if actor.get("kind") == "machine" else QueuedActor(**actor)

    @staticmethod
    def _v2(value):
        if (
            isinstance(value, dict)
            and set(value) == {"schema", "snapshot", "claims"}
            and value["schema"] == "cyclotron-s3-console/v1"
        ):
            return {**value, "schema": SCHEMA_V2, "actors": {}}
        return value

    def _upgrade(self, record):
        """The record's value in the current layout, every table whole in memory.

        A whole-state (v1/v2) catalog keeps its blob as the legacy layer; a v3 head moves its labels, events and
        predictions out at its next commit; a v4 head names its rarely changing tables by digest.
        """
        value = self._v2(record.value)
        if isinstance(value, dict) and value.get("schema") == SCHEMA_V2:
            value, legacy = migrated(value, record.reference)
            if legacy is not None:
                self._remember_legacy(record.reference.digest, legacy)
        if isinstance(value, dict) and value.get("schema") == SCHEMA_V3:
            value = migrated_v3(value)
        return self._expand(value)

    def _expand(self, value):
        parts = value.get("parts") if isinstance(value, dict) else None
        if not parts or not isinstance(parts, dict):
            return value
        tables = dict(value["snapshot"]["tables"])
        for table, ref in parts.items():
            tables[table] = self._part(table, ref)
        return {**value, "snapshot": {**value["snapshot"], "tables": tables}}

    def _part(self, table, ref):
        """One table's rows from its content-addressed payload, kept in process memory while it is current."""
        cache = self.snapshot_cache
        found = self._parts.get(ref["digest"])
        if found is None and cache is not None and hasattr(cache, "part"):
            found = cache.part(self.journal, table, ref["digest"])
        if found is None:
            found = decode(self.journal.payloads.get(PayloadRef(self.journal.payloads.scope, ref["digest"], ref["length"])))
            if not isinstance(found, list):
                raise ValueError("invalid catalog part")
        self._remember_part(table, ref["digest"], found)
        return found

    def _remember_part(self, table, digest, found):
        self._parts[digest] = found
        cache = self.snapshot_cache
        if cache is not None and hasattr(cache, "remember_part"):
            cache.remember_part(self.journal, table, digest, found)

    def _stored(self, value):
        """The head as stored: each rarely changing table as a digest. Writes a table's payload only when it changed.

        Updates ``value["parts"]`` to the digests it names and returns the stored form.
        """
        tables, parts = dict(value["snapshot"]["tables"]), dict(value["parts"])
        for table in STATIC_TABLES:
            if not tables[table]:
                parts.pop(table, None)
                continue
            content = encode(tables[table])
            ref = {"digest": sha256(content).hexdigest(), "length": len(content)}
            if parts.get(table) != ref:
                self.journal.payloads.put(content)
                self._remember_part(table, ref["digest"], tables[table])
                parts[table] = ref
            tables[table] = []
        value["parts"] = parts
        return {**value, "parts": parts, "snapshot": {**value["snapshot"], "tables": tables}}

    def initialize(self, *, verify_existing=True):
        if verify_existing:
            self._validate(self._upgrade(self.journal.initialize(initial())))
        else:
            self.journal.initialize(initial(), read_existing=False)

    def _load(self):
        """The head's record: reused from memory when the head still names the cached snapshot."""
        started = time.monotonic()
        cache = self.snapshot_cache
        known = cache.get(self.journal) if cache is not None else None
        record = self.journal.read(known=known) if known is not None else self.journal.read()
        reused = known is not None and record.value is known.value
        if not reused:
            record = replace(record, value=self._upgrade(record))
            self._validate(record.value)
            if cache is not None:
                cache.put(self.journal, record)
        if cache is not None:
            cache.observe(self.journal, record, self._still_held)
        if self.phase_log is not None:
            self.phase_log("catalog_read", started, cache="hit" if reused else "miss")
        return record

    # Rows outside the head (catalog_cold_rows.py) -------------------------------------------------------
    def _cold(self):
        if self._cold_rows is None:
            self._cold_rows = ColdRows(self.journal.payloads)
        return self._cold_rows

    def _remember_legacy(self, digest, legacy):
        self._legacy_index[digest] = legacy
        remember = getattr(self.snapshot_cache, "remember_legacy", None)
        if remember is not None:
            remember(digest, legacy)

    def _legacy(self, value):
        held = value.get("legacy")
        if held is None:
            return None
        found = self._legacy_index.get(held["digest"])
        if found is None:
            recall = getattr(self.snapshot_cache, "legacy", None)
            found = recall(held["digest"]) if recall is not None else None
        if found is None:
            found = load_legacy(self.journal.payloads, held, self._v2)
        self._remember_legacy(held["digest"], found)
        return found

    @staticmethod
    def _hot_meta(value, table, row):
        return value["cold"]["hot"][table][row_key(table, row)]

    def _hot_entry(self, value, row):
        return {
            "row": row,
            "ordinal": self._hot_meta(value, "web_jobs", row)[0],
            "actor": value["actors"].get(row["id"]),
            "claim": value["claims"].get(row["id"]),
        }

    def _job_at(self, value, run_id, n):
        """The job at position ``n`` of the run, wherever it lives: ``{"row", "ordinal", "actor", "claim"}``.

        Jobs start in acceptance order, so the position alone says where a job that is not in the head lives:
        at or after the run's queue start it is still waiting (its accepted record, or the legacy blob), before
        it it has finished (its final record, or the legacy blob if it finished before this layout).
        """
        last = value["cold"]["ordinals"]["web_jobs"].get(run_id, 0)
        if type(n) is not int or not 1 <= n <= last:
            return None
        hot = next((r for r in rows(value, "web_jobs", run_id=run_id) if self._hot_meta(value, "web_jobs", r)[0] == n), None)
        if hot is not None:
            return self._hot_entry(value, hot)
        first, _ = queue_bounds(value, run_id)
        legacy = self._legacy(value)
        if legacy is not None and n <= legacy.job_counts.get(run_id, 0):
            opened = value["legacy"]["open"].get(run_id, legacy.job_counts[run_id] + 1)
            if n < opened or n >= first:
                return legacy.jobs[legacy.job_order[run_id, n]]
        kind = ColdRows.queued(run_id) if n >= first else ColdRows.run_jobs(run_id)
        entry = self._cold().get(kind, n)
        if entry is None:
            raise ValueError("catalog job record missing")
        return entry

    def _job_entry(self, value, job_id):
        """One job wherever it lives: ``{"row", "ordinal", "actor", "claim"}``, or None for an unknown id."""
        row = one(value, "web_jobs", id=job_id)
        if row is not None:
            return self._hot_entry(value, row)
        legacy = self._legacy(value)
        if legacy is not None and job_id in legacy.jobs:
            found = legacy.jobs[job_id]
            return self._job_at(value, found["row"]["run_id"], found["ordinal"])
        if not isinstance(job_id, str):
            return None
        pointer = self._cold().get(ColdRows.job(job_id), 1)
        return None if pointer is None else self._job_at(value, pointer["run"], pointer["ordinal"])

    def _request_entry(self, value, run_id, request_id):
        row = one(value, "web_jobs", run_id=run_id, request_id=request_id)
        if row is not None:
            return self._hot_entry(value, row)
        legacy = self._legacy(value)
        if legacy is not None and (run_id, request_id) in legacy.requests:
            return self._job_entry(value, legacy.requests[run_id, request_id])
        pointer = self._cold().get(ColdRows.request(run_id, request_id), 1)
        return None if pointer is None else self._job_at(value, run_id, pointer["ordinal"])

    def _run_job_entries(self, value, run_id):
        """The run's jobs, newest first, read one at a time so a caller can stop early."""
        for n in range(value["cold"]["ordinals"]["web_jobs"].get(run_id, 0), 0, -1):
            yield self._job_at(value, run_id, n)

    @staticmethod
    def _running(value, run_id):
        return next((r for r in rows(value, "web_jobs", run_id=run_id) if r["status"] == "running"), None)

    def _has_unfinished(self, value, run_id):
        first, last = queue_bounds(value, run_id)
        return self._running(value, run_id) is not None or first <= last

    def _queue_head(self, value, run_id):
        """The run's next job to start, or None (nothing waiting, or a job still running)."""
        first, last = queue_bounds(value, run_id)
        if self._running(value, run_id) is not None or first > last:
            return None
        return self._job_at(value, run_id, first)

    def _item_entry(self, value, run_id, item_id):
        """One run item wherever it lives: ``{"row", "ordinal", "slot"}``, or None."""
        row = one(value, "web_items", run_id=run_id, id=item_id)
        if row is not None:
            ordinal, slot = self._hot_meta(value, "web_items", row)
            return {"row": row, "ordinal": ordinal, "slot": slot}
        found, slot, kind = None, 1, ColdRows.item(run_id, item_id)
        while True:
            entry = self._cold().get(kind, slot)
            if entry is None:
                break
            found, slot = entry, slot + 1
        if found is not None:
            return found
        legacy = self._legacy(value)
        if legacy is not None and (run_id, item_id) in legacy.items:
            return {**legacy.items[run_id, item_id], "slot": 0}
        return None

    def _item_row(self, value, run_id, item_id, *, for_update=False):
        """The item's row. ``for_update`` brings a row from outside into this change's head, to edit in place."""
        entry = self._item_entry(value, run_id, item_id)
        if entry is None or not for_update or one(value, "web_items", run_id=run_id, id=item_id) is not None:
            return None if entry is None else entry["row"]
        row = deepcopy(entry["row"])
        table = value["snapshot"]["tables"]["web_items"]
        position = next((i for i, r in enumerate(table) if r["_rowid"] > row["_rowid"]), len(table))
        table.insert(position, row)
        key = row_key("web_items", row)
        value["cold"]["hot"]["web_items"][key] = [entry["ordinal"], entry["slot"] + 1]
        value.setdefault(_HYDRATED, {})[key] = deepcopy(row)
        return row

    def _item_at(self, value, run_id, n):
        """The item row at position ``n`` of the run."""
        hot = next((r for r in rows(value, "web_items", run_id=run_id) if self._hot_meta(value, "web_items", r)[0] == n), None)
        if hot is not None:
            return hot
        legacy = self._legacy(value)
        if legacy is not None and n <= legacy.item_counts.get(run_id, 0):
            item_id = legacy.item_order[run_id, n]
        else:
            pointer = self._cold().get(ColdRows.run_items(run_id), n)
            if pointer is None:
                raise ValueError("catalog item record missing")
            item_id = pointer["id"]
        return self._item_entry(value, run_id, item_id)["row"]

    def _run_items(self, value, run_id):
        """The run's item rows in acceptance order."""
        hot = {self._hot_meta(value, "web_items", r)[0]: r for r in rows(value, "web_items", run_id=run_id)}
        legacy = self._legacy(value)
        for n in range(1, value["cold"]["ordinals"]["web_items"].get(run_id, 0) + 1):
            if n in hot:
                yield hot[n]
                continue
            if legacy is not None and n <= legacy.item_counts.get(run_id, 0):
                item_id = legacy.item_order[run_id, n]
            else:
                pointer = self._cold().get(ColdRows.run_items(run_id), n)
                if pointer is None:
                    raise ValueError("catalog item record missing")
                item_id = pointer["id"]
            yield self._item_entry(value, run_id, item_id)["row"]

    def _revision_row(self, value, list_id, item_id, revision):
        row = one(value, "list_item_revisions", list_id=list_id, id=item_id, revision=revision)
        if row is not None:
            return row
        legacy = self._legacy(value)
        if legacy is not None and (list_id, item_id, revision) in legacy.revisions:
            return legacy.revisions[list_id, item_id, revision]["row"]
        if type(revision) is not int or revision < 1 or not isinstance(list_id, str) or not isinstance(item_id, str):
            return None
        entry = self._cold().get(ColdRows.revision(list_id, item_id), revision)
        return None if entry is None else entry["row"]

    def _latest_revision(self, value, list_id, item_id):
        """The newest revision row of one item in one list, or None. Reads only that item's own records."""
        hot = rows(value, "list_item_revisions", list_id=list_id, id=item_id)
        if hot:
            return max(hot, key=lambda r: r["revision"])
        legacy = self._legacy(value)
        latest = legacy.latest.get((list_id, item_id), 0) if legacy is not None else 0
        found = legacy.revisions[list_id, item_id, latest]["row"] if latest else None
        kind = ColdRows.revision(list_id, item_id)
        while True:
            entry = self._cold().get(kind, latest + 1)
            if entry is None:
                return found
            found, latest = entry["row"], latest + 1

    def _list_revisions(self, value, list_id):
        """Every revision row of the list in import order."""
        hot = {self._hot_meta(value, "list_item_revisions", r): r for r in rows(value, "list_item_revisions", list_id=list_id)}
        legacy = self._legacy(value)
        for n in range(1, value["cold"]["ordinals"]["list_item_revisions"].get(list_id, 0) + 1):
            if n in hot:
                yield hot[n]
            elif legacy is not None and n <= legacy.revision_counts.get(list_id, 0):
                yield legacy.revisions[legacy.revision_order[list_id, n]]["row"]
            else:
                entry = self._cold().get(ColdRows.list_revisions(list_id), n)
                if entry is None:
                    raise ValueError("catalog revision record missing")
                yield entry["row"]

    def _all_rows(self, value, table):
        """Every row of a table, wherever it lives, in table order. For export and audit, never a decision."""
        if table == "web_jobs":
            found = [e["row"] for run in rows(value, "web_runs") for e in self._run_job_entries(value, run["id"])]
        elif table == "web_items":
            found = [r for run in rows(value, "web_runs") for r in self._run_items(value, run["id"])]
        elif table == "list_item_revisions":
            found = [r for lst in rows(value, "item_lists") for r in self._list_revisions(value, lst["id"])]
        else:
            found = [r for part in value["cold"]["ordinals"][table] for r in self._rows_of(value, table, part)]
        return sorted(found, key=lambda r: r["_rowid"])

    @staticmethod
    def _settle(value):
        """Return rows brought in for an edit that did not change to where they came from, and file every item
        the change holds in or out of its run's review intervals (an item can change only while in the head)."""
        for key, original in value.pop(_HYDRATED, {}).items():
            table = value["snapshot"]["tables"]["web_items"]
            row = next((r for r in table if row_key("web_items", r) == key), None)
            if row == original:
                table.remove(row)
                del value["cold"]["hot"]["web_items"][key]
        review = value["cold"]["review"]
        for row in value["snapshot"]["tables"]["web_items"]:
            ordinal = value["cold"]["hot"]["web_items"][row_key("web_items", row)][0]
            spans = with_ordinal(review.get(row["run_id"], []), ordinal, in_review(row))
            if spans:
                review[row["run_id"]] = spans
            else:
                review.pop(row["run_id"], None)

    def _evict(self, base, value):
        """Move rows the base head committed and this change leaves alone out of the head, into their records.

        Records are written before the commit that drops the rows, and only from committed state, so a key never
        names two contents and a reader that misses a row in its head finds it in the records. A waiting job
        leaves as its accepted record (it cannot change until it starts); a running job stays.
        """
        if base.get("schema") != SCHEMA_V4:
            return
        for table in COLD_TABLES:
            current = {row_key(table, r): r for r in value["snapshot"]["tables"][table]}
            for row in base["snapshot"]["tables"][table]:
                key = row_key(table, row)
                held = base["cold"]["hot"][table][key]
                if not is_evictable(table, row) or current.get(key) != row or value["cold"]["hot"][table].get(key) != held:
                    continue
                if self._write_cold(base, table, row, held):
                    value["snapshot"]["tables"][table].remove(current[key])
                    del value["cold"]["hot"][table][key]
                    if table == "web_jobs":
                        value["claims"].pop(key, None)
                        value["actors"].pop(key, None)
                else:
                    # An abandoned commit wrote an older state into this slot; this state takes the next one.
                    value["cold"]["hot"][table][key] = [held[0], held[1] + 1]

    def _write_cold(self, base, table, row, held):
        """Write one committed row's records. False only when an item's slot holds another state."""
        cold = self._cold()
        if table == "web_jobs":
            ordinal, pointed = held
            entry = {
                "row": row,
                "ordinal": ordinal,
                "actor": base["actors"].get(row["id"]),
                "claim": base["claims"].get(row["id"]),
            }
            kind = ColdRows.queued(row["run_id"]) if row["status"] == "pending" else ColdRows.run_jobs(row["run_id"])
            written = cold.put(kind, ordinal, entry)
            if written and not pointed:
                pointer = {"run": row["run_id"], "ordinal": ordinal, "id": row["id"]}
                written = cold.put(ColdRows.job(row["id"]), 1, pointer) and cold.put(
                    ColdRows.request(row["run_id"], row["request_id"]), 1, pointer
                )
        elif table == "web_items":
            ordinal, slot = held
            if not cold.put(ColdRows.item(row["run_id"], row["id"]), slot, {"row": row, "ordinal": ordinal, "slot": slot}):
                return False
            written = cold.put(ColdRows.run_items(row["run_id"]), ordinal, {"id": row["id"]})
        elif table == "list_item_revisions":
            entry = {"row": row, "ordinal": held}
            written = cold.put(ColdRows.revision(row["list_id"], row["id"]), row["revision"], entry) and cold.put(
                ColdRows.list_revisions(row["list_id"]), held, entry
            )
        else:
            entry = {"row": row, "ordinal": held}
            if table == "item_feedback":
                keyed = [(ColdRows.labels(), held), (ColdRows.label(row["request_id"]), 1)]
                sets = [ColdRows.item_labels(row["list_id"], row["item_id"], row["item_revision"])]
            elif table == "item_feedback_retractions":
                keyed = [
                    (ColdRows.retractions(), held),
                    (ColdRows.retraction(row["request_id"]), 1),
                    (ColdRows.retracted(row["feedback_request_id"]), 1),
                ]
                sets = []
            elif table == "web_events":
                keyed = [(ColdRows.run_events(row["run_id"]), held), (ColdRows.event(row["run_id"], row["source_id"]), 1)]
                sets = []
            else:
                keyed = [
                    (ColdRows.run_predictions(row["run_id"]), held),
                    (ColdRows.prediction(row["run_id"], row["list_id"], row["item_id"], row["item_revision"]), 1),
                ]
                sets = [
                    ColdRows.item_predictions(row["list_id"], row["item_id"], row["item_revision"]),
                    ColdRows.run_item_predictions(row["run_id"], row["item_id"]),
                ]
            written = all(cold.put(kind, seq, entry) for kind, seq in keyed)
            for kind in sets if written else ():
                cold.file(kind, entry)
        if not written:
            raise ValueError("a catalog record already holds different committed content")
        return True

    def _commit(self, record, value):
        self._evict(record.value, value)
        self._validate(value)
        stored = self._stored(value)
        return self._committed(replace(commit_with_contract(self.journal, record, stored), value=value))

    def _committed(self, record):
        """Remember what this store just committed: the next read only revalidates the head."""
        cache = self.snapshot_cache
        if cache is not None:
            cache.put(self.journal, record)
            cache.observe(self.journal, record, self._still_held)
        return record

    # Claim holds (catalog_snapshot_cache.py) ------------------------------------------------------------
    @staticmethod
    def _still_held(value, claim):
        job_id, owner, fence = claim
        job = one(value, "web_jobs", id=job_id)
        return job is not None and job["status"] == "running" and same_claim(value["claims"].get(job_id), owner, fence)

    def _run_value(self, run_id):
        """The value for a read of one run's own rows.

        While this process holds a confirmed claim on the run's running job, only that holder can change the run's
        rows, and every head this process reads or commits refreshes the hold. Such a read then uses the newest
        record that showed the claim instead of reading the head again. Everything else reads the head.
        """
        held = None
        if self._tx.get() is None and self.snapshot_cache is not None:
            held = self.snapshot_cache.held(self.journal, run_id)
        if held is None:
            return self._read()
        self._check_claim(held.value)
        return held.value

    def _record(self):
        with self._request_lock:
            self._request_snapshot = None
        return self._load()

    def _check_claim(self, value):
        claim = self._claim.get()
        if claim is None:
            return
        job_id, owner, fence = claim
        job = one(value, "web_jobs", id=job_id)
        if job is None or job["status"] != "running" or not same_claim(value["claims"].get(job_id), owner, fence):
            raise ValueError("stale command execution boundary")

    def _worker_write(self, value, run_id):
        if (
            self._claim.get() is not None
            or run_id in value["cold"]["claimed"]
            or any(one(value, "web_jobs", id=key)["run_id"] == run_id for key in value["claims"])
        ):
            self._check_claim(value)
            claim = self._claim.get()
            if claim is None or one(value, "web_jobs", id=claim[0])["run_id"] != run_id:
                raise ValueError("worker mutation requires command-bound cloud store")

    def _read(self):
        if self._tx.get() is not None:
            value = self._tx.get()
        elif self.reuse_reads_within_request:
            with self._request_lock:
                if self._request_snapshot is None:
                    self._request_snapshot = self._load().value
                value = self._request_snapshot
        else:
            value = self._load().value
        self._check_claim(value)
        return value

    def _result_text(self, value, row):
        """The job row's result as JSON text, fetched by reference when the row holds one."""
        ref = result_ref(row["result"])
        if ref is None:
            return row["result"]
        if self.results is None:
            raise ValueError("job result lives in the record lake, but no lake is configured")
        if not coverage_in(value, self.results).covers(ref["seq"]):
            raise ValueError("job result record is not covered by the catalog")
        record = self.results.read(ref["seq"])
        if (
            not isinstance(record, dict)
            or record.get("jobId") != row["id"]
            or sha256(self.results._encode(ref["seq"], record)).hexdigest() != ref["digest"]
        ):
            raise ValueError("job result record does not match its reference")
        return encoded(record["result"])

    def _job(self, value, row):
        return decoded({**row, "result": self._result_text(value, row)}, "payload", "result")

    def job_result(self, job_id):
        """One job's decoded result (fetched by reference if needed), or None while it has none."""
        value = self._read()
        entry = self._job_entry(value, job_id)
        if entry is None:
            raise ValueError("unknown command")
        return self._job(value, entry["row"])["result"]

    def for_command(self, job_id, owner, fence):
        uuid(owner)
        if type(fence) is not int or fence < 1:
            raise ValueError("valid command fence required")
        if (job_id, owner, fence) not in self._granted:
            raise ValueError("command claim receipt not confirmed in this worker")
        record = self._load() if self._tx.get() is None else None
        value = self._read() if record is None else record.value
        self._check_claim(value)
        job = one(value, "web_jobs", id=job_id)
        if job is None or job["status"] != "running" or not same_claim(value["claims"].get(job_id), owner, fence):
            raise ValueError("stale command capability")
        if record is not None and self.snapshot_cache is not None:
            self.snapshot_cache.hold(self.journal, job["run_id"], (job_id, owner, fence), record)
        return _CommandView(self, (job_id, owner, fence), job["run_id"])

    def _mutate(self, fn):
        if self._tx.get() is not None:
            return fn(self._tx.get())
        for _ in range(self.max_conflicts):
            record = self._record()
            self._check_claim(record.value)
            value = deepcopy(record.value)
            result = fn(value)
            self._settle(value)
            self._validate(value)
            if value == record.value:
                return result
            try:
                self._commit(record, value)
            except StorageConflict:
                continue
            return result
        raise ValueError("console contention limit reached; retry with same operation identity")

    @contextmanager
    def transaction(self):
        if self._tx.get() is not None:
            raise ValueError("nested console transaction")
        record = self._record()
        self._check_claim(record.value)
        value = deepcopy(record.value)
        token = self._tx.set(value)
        try:
            yield self
            self._settle(value)
            self._validate(value)
            if value != record.value:
                self._commit(record, value)
        finally:
            self._tx.reset(token)

    def close(self):
        pass

    def export_storage(self):
        value = self._read()
        if any(one(value, "web_jobs", id=key)["status"] == "running" for key in value["claims"]):
            raise ValueError("quiesce cloud command claims before portable export")
        snapshot = {
            **value["snapshot"],
            "tables": {**value["snapshot"]["tables"], **{t: self._all_rows(value, t) for t in COLD_TABLES}},
        }
        if any(result_ref(r["result"]) is not None for r in snapshot["tables"]["web_jobs"]):
            # A portable snapshot is self-contained: results come back embedded.
            jobs = [{**r, "result": self._result_text(value, r)} for r in snapshot["tables"]["web_jobs"]]
            snapshot = {**snapshot, "tables": {**snapshot["tables"], "web_jobs": jobs}}
        return deepcopy(validate_console(snapshot))

    def restore_storage(self, snapshot):
        snapshot = validate_console(snapshot)
        if any(r["status"] == "running" for r in snapshot["tables"]["web_jobs"]):
            raise ValueError("reconcile native running jobs before cloud import")

        def apply(value):
            if any(value["snapshot"]["tables"].values()) or value["claims"] or any(value["cold"]["rowids"].values()):
                raise ValueError("console restore requires empty destination")
            adopt_snapshot(value, snapshot)

        self._mutate(apply)

    def export(self, **limits):
        from .catalog_snapshot import COLUMNS
        from .catalog_snapshot import SCHEMA as catalog_schema
        from .catalog_snapshot import validate as catalog_validate

        snapshot = self.export_storage(**limits)
        return catalog_validate(
            {
                "schema": catalog_schema,
                "tables": {t: [clean(r) for r in snapshot["tables"][t]] for t in COLUMNS},
                "feedback_sequence": snapshot["sequences"]["item_feedback"],
            },
            **limits,
        )

    # Catalog revisions and definition activation share this same transaction.
    @staticmethod
    def _classifier(value, identifier, revision=None):
        candidates = rows(value, "classifiers", id=identifier)
        row = (
            next((r for r in candidates if r["revision"] == revision), None)
            if revision is not None
            else max(candidates, key=lambda r: r["revision"], default=None)
        )
        if row is None:
            raise ValueError("unknown classifier revision")
        return row

    def classifier(self, identifier, revision=None):
        return decoded(self._classifier(self._read(), identifier, revision), "config")

    def classifiers(self):
        value = self._read()
        ids = {r["id"] for r in rows(value, "classifiers")}
        return sorted((decoded(self._classifier(value, i), "config") for i in ids), key=lambda r: (r["name"], r["id"]))

    def classifier_versions(self, identifier):
        result = [
            decoded(r, "config") for r in sorted(rows(self._read(), "classifiers", id=identifier), key=lambda r: r["revision"])
        ]
        if not result:
            raise ValueError("unknown classifier")
        return result

    @staticmethod
    def _definition_insert(value, identifier, name, refs, settings, created):
        revision = max((r["revision"] for r in rows(value, "cyclotron_definitions", id=identifier)), default=0) + 1
        return insert(
            value,
            "cyclotron_definitions",
            dict(
                id=identifier,
                revision=revision,
                name=name,
                classifiers=encoded(refs),
                settings=encoded(settings),
                fingerprint=sha256(encoded({"name": name, "classifiers": refs, "settings": settings}).encode()).hexdigest(),
                created_at=created,
            ),
        )

    @staticmethod
    def _activate(value, identifier, revision):
        active = one(value, "cyclotron_catalog", id=identifier)
        if active:
            active["active_revision"] = revision
        else:
            insert(value, "cyclotron_catalog", dict(id=identifier, active_revision=revision))

    def save_classifier(self, identifier, name, config):
        labels = [r.get("label") for r in config.get("classes", [])]
        if (
            not identifier
            or not name.strip()
            or not isinstance(config.get("question"), str)
            or not config["question"].strip()
            or len(labels) < 2
            or len(set(labels)) != len(labels)
            or any(not isinstance(v, str) or not v.strip() for v in labels)
            or any(r.get("role") not in (None, "positive", "negative") for r in config["classes"])
        ):
            raise ValueError("classifier requires a question and distinct ordered classes")
        DecisionTask(identifier, tuple(labels), config["question"])
        if "criteria" in config:
            from .classifier_criteria import validate_provenance

            validate_provenance(config["criteria"])
        payload = encoded(config)
        created = now()

        def apply(value):
            previous = max(rows(value, "classifiers", id=identifier), key=lambda r: r["revision"], default=None)
            if previous and (previous["name"], previous["config"]) == (name, payload):
                return decoded(previous, "config")
            revision = previous["revision"] + 1 if previous else 1
            row = insert(
                value, "classifiers", dict(id=identifier, revision=revision, name=name, config=payload, created_at=created)
            )
            for active in rows(value, "cyclotron_catalog"):
                old = one(value, "cyclotron_definitions", id=active["id"], revision=active["active_revision"])
                refs = json.loads(old["classifiers"])
                if any(ref["id"] == identifier for ref in refs):
                    for ref in refs:
                        if ref["id"] == identifier:
                            ref["revision"] = revision
                    new = self._definition_insert(value, old["id"], old["name"], refs, json.loads(old["settings"]), created)
                    active["active_revision"] = new["revision"]
            return decoded(row, "config")

        return self._mutate(apply)

    def save_item_list(self, identifier, name):
        if not identifier or not name.strip():
            raise ValueError("item list identity and name required")

        def apply(value):
            row = one(value, "item_lists", id=identifier)
            if row:
                row["name"] = name
            else:
                insert(value, "item_lists", dict(id=identifier, name=name))
            return {"id": identifier, "name": name}

        return self._mutate(apply)

    def item_lists(self):
        value = self._read()
        return [
            {**clean(r), "count": value["cold"]["distinct"].get(r["id"], 0)}
            for r in sorted(rows(value, "item_lists"), key=lambda r: (r["name"], r["id"]))
        ]

    def upsert_list_items(self, list_id, items):
        if not items:
            raise ValueError("upsert at least one item")
        created = now()

        def apply(value):
            if not one(value, "item_lists", id=list_id):
                raise ValueError("unknown item list")
            counts = {"inserted": 0, "updated": 0, "unchanged": 0}
            for item in items:
                if not isinstance(item.get("id"), str) or not item["id"] or not isinstance(item.get("values"), dict):
                    raise ValueError("item identity and structured values required")
                occurred = timestamp(item["occurred_at"])
                payload = encoded({**item, "occurred_at": occurred})
                fingerprint = sha256(payload.encode()).hexdigest()
                old = self._latest_revision(value, list_id, item["id"])
                if old and old["fingerprint"] == fingerprint:
                    counts["unchanged"] += 1
                    continue
                insert(
                    value,
                    "list_item_revisions",
                    dict(
                        list_id=list_id,
                        id=item["id"],
                        revision=old["revision"] + 1 if old else 1,
                        occurred_at=occurred,
                        fingerprint=fingerprint,
                        payload=payload,
                        imported_at=created,
                    ),
                )
                counts["updated" if old else "inserted"] += 1
            return counts

        return self._mutate(apply)

    def _items(self, value, list_id):
        if not one(value, "item_lists", id=list_id):
            raise ValueError("unknown item list")
        latest = {}
        for row in self._list_revisions(value, list_id):
            if row["id"] not in latest or row["revision"] > latest[row["id"]]["revision"]:
                latest[row["id"]] = row
        return sorted(latest.values(), key=lambda r: (r["occurred_at"], r["id"]))

    def snapshot_items(self, list_id):
        return [
            {**self._payload(r["payload"]), "revision": r["revision"], "fingerprint": r["fingerprint"]}
            for r in self._items(self._read(), list_id)
        ]

    def list_items(self, list_id, *, after=0, limit=200):
        if type(after) is not int or after < 0 or type(limit) is not int or limit < 1:
            raise ValueError("a non-negative offset and positive page size required")
        return self.snapshot_items(list_id)[after : after + limit]

    def item_revision(self, list_id, identifier, revision):
        row = self._revision_row(self._read(), list_id, identifier, revision)
        if row is None:
            raise ValueError("unknown item revision")
        return self._payload(row["payload"])

    # Labels, retractions, events and displayed predictions, wherever they live ------------------------------
    def _row_at(self, value, table, part, n):
        """The row at position ``n`` of a run or list (labels and retractions: of the catalog), hot or recorded."""
        hot = next(
            (
                r
                for r in value["snapshot"]["tables"][table]
                if partition(table, r) == part and self._hot_meta(value, table, r) == n
            ),
            None,
        )
        if hot is not None:
            return hot
        kind = {
            "item_feedback": ColdRows.labels,
            "item_feedback_retractions": ColdRows.retractions,
            "web_events": ColdRows.run_events,
            "item_predictions": ColdRows.run_predictions,
        }[table]
        entry = self._cold().get(kind(part) if part else kind(), n)
        if entry is None:
            raise ValueError("catalog record missing")
        return entry["row"]

    def _rows_of(self, value, table, part, first=1):
        """Every row of one run or list in order, from position ``first``."""
        for n in range(first, value["cold"]["ordinals"][table].get(part, 0) + 1):
            yield self._row_at(value, table, part, n)

    def _keyed(self, value, table, kind, **fields):
        """The one row a key names: the head's, else the record filed under the key."""
        row = one(value, table, **fields)
        if row is not None:
            return row
        entry = self._cold().get(kind, 1)
        return None if entry is None else entry["row"]

    def _filed(self, value, table, kind, **fields):
        """Every row of a set: the head's rows that belong and the committed rows filed under the set's key."""
        found = {row_key(table, r["row"]): r["row"] for r in self._cold().members(kind)}
        found.update({row_key(table, r): r for r in rows(value, table, **fields)})
        return sorted(found.values(), key=lambda r: r["_rowid"])

    def _label(self, value, request_id):
        return self._keyed(value, "item_feedback", ColdRows.label(request_id), request_id=request_id)

    def _retraction(self, value, request_id):
        return self._keyed(value, "item_feedback_retractions", ColdRows.retraction(request_id), request_id=request_id)

    def _retraction_of(self, value, label_request_id):
        return self._keyed(
            value, "item_feedback_retractions", ColdRows.retracted(label_request_id), feedback_request_id=label_request_id
        )

    def _item_labels(self, value, list_id, item_id, item_revision):
        return self._filed(
            value,
            "item_feedback",
            ColdRows.item_labels(list_id, item_id, item_revision),
            list_id=list_id,
            item_id=item_id,
            item_revision=item_revision,
        )

    def _latest_votes(self, value, list_id, item_id, item_revision):
        result = {}
        for row in self._item_labels(value, list_id, item_id, item_revision):
            key = (row["classifier_id"], row["classifier_revision"])
            if key not in result or row["sequence"] > result[key]["sequence"]:
                result[key] = row
        return [r for _, r in sorted(result.items()) if self._retraction_of(value, r["request_id"]) is None]

    def item_labels(self, list_id, item_id, item_revision):
        return [clean(r) for r in self._latest_votes(self._read(), list_id, item_id, item_revision)]

    def label_item(self, classifier_id, classifier_revision, list_id, item_id, item_revision, label, comment, request_id):
        if not request_id or not isinstance(comment, str):
            raise ValueError("feedback identity and comment required")
        created = now()

        def apply(value):
            classifier = self._classifier(value, classifier_id, classifier_revision)
            config = json.loads(classifier["config"])
            DecisionTask(classifier_id, tuple(r["label"] for r in config["classes"]), config["question"]).validate_label(label)
            if not self._revision_row(value, list_id, item_id, item_revision):
                raise ValueError("unknown item revision")
            fields = dict(
                classifier_id=classifier_id,
                classifier_revision=classifier_revision,
                list_id=list_id,
                item_id=item_id,
                item_revision=item_revision,
                label=label,
                comment=comment,
            )
            old = self._label(value, request_id)
            if old:
                if any(old[k] != v for k, v in fields.items()):
                    raise ValueError("feedback identity has different content")
                return clean(old)
            return clean(insert(value, "item_feedback", dict(sequence=0, request_id=request_id, **fields, created_at=created)))

        return self._mutate(apply)

    def retract_item_label(self, feedback_request_id, request_id):
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("retraction identity required")
        created = now()

        def apply(value):
            existing = self._retraction(value, request_id)
            if existing:
                if existing["feedback_request_id"] != feedback_request_id:
                    raise ValueError("retraction identity has different content")
                return clean(existing)
            vote = self._label(value, feedback_request_id) if isinstance(feedback_request_id, str) else None
            if vote is None:
                raise ValueError("unknown feedback identity")
            same = ("classifier_id", "classifier_revision")
            latest = max(
                (
                    r
                    for r in self._item_labels(value, vote["list_id"], vote["item_id"], vote["item_revision"])
                    if all(r[k] == vote[k] for k in same)
                ),
                key=lambda r: r["sequence"],
            )
            if vote != latest:
                raise ValueError("only the current label can be retracted")
            if self._retraction_of(value, feedback_request_id):
                raise ValueError("label already retracted by another command")
            return clean(
                insert(
                    value,
                    "item_feedback_retractions",
                    dict(request_id=request_id, feedback_request_id=feedback_request_id, created_at=created),
                )
            )

        return self._mutate(apply)

    def record_item_prediction(self, run_id, list_id, item_id, item_revision, prediction):
        payload = encoded(prediction)

        def apply(value):
            self._worker_write(value, run_id)
            if not self._revision_row(value, list_id, item_id, item_revision):
                raise ValueError("unknown item revision")
            fields = dict(run_id=run_id, list_id=list_id, item_id=item_id, item_revision=item_revision)
            old = self._keyed(value, "item_predictions", ColdRows.prediction(run_id, list_id, item_id, item_revision), **fields)
            if old:
                if old["payload"] != payload:
                    raise ValueError("displayed prediction is immutable")
            else:
                insert(value, "item_predictions", dict(**fields, payload=payload))

        self._mutate(apply)

    def item_results(self, list_id, item_id, item_revision):
        value = self._read()
        result = []
        predictions = self._filed(
            value,
            "item_predictions",
            ColdRows.item_predictions(list_id, item_id, item_revision),
            list_id=list_id,
            item_id=item_id,
            item_revision=item_revision,
        )
        for r in predictions:
            run = one(value, "web_runs", id=r["run_id"])
            if run:
                result.append((run["created_at"], {**decoded(r, "payload"), "run_name": run["name"]}))
        return [r for _, r in sorted(result, key=lambda p: p[0], reverse=True)]

    def save_cyclotron_definition(self, identifier, name, classifiers, settings):
        from .cyclotron_settings import validate_shared_settings

        if (
            not isinstance(identifier, str)
            or not identifier.strip()
            or not isinstance(name, str)
            or not name.strip()
            or not isinstance(settings, dict)
        ):
            raise ValueError("cyclotron identity, name and structured settings required")
        settings = validate_shared_settings(settings)
        refs = [{"id": r["id"], "revision": r["revision"]} for r in classifiers]
        if not refs or len({r["id"] for r in refs}) != len(refs):
            raise ValueError("cyclotron requires distinct ordered classifiers")
        created = now()

        def apply(value):
            for ref in refs:
                if type(ref["revision"]) is not int:
                    raise ValueError("unknown classifier revision")
                self._classifier(value, ref["id"], ref["revision"])
            active = one(value, "cyclotron_catalog", id=identifier)
            old = one(value, "cyclotron_definitions", id=identifier, revision=active["active_revision"]) if active else None
            if old and (old["name"], old["classifiers"], old["settings"]) == (name, encoded(refs), encoded(settings)):
                return decoded(old, "classifiers", "settings")
            row = self._definition_insert(value, identifier, name, refs, settings, created)
            self._activate(value, identifier, row["revision"])
            return decoded(row, "classifiers", "settings")

        return self._mutate(apply)

    @staticmethod
    def _definition(value, identifier, revision=None):
        if revision is None:
            active = one(value, "cyclotron_catalog", id=identifier)
            revision = active["active_revision"] if active else None
        row = one(value, "cyclotron_definitions", id=identifier, revision=revision)
        if row is None:
            raise ValueError("unknown cyclotron definition")
        return row

    def cyclotron_definition(self, identifier, revision=None):
        return decoded(self._definition(self._read(), identifier, revision), "classifiers", "settings")

    def cyclotron_definitions(self):
        value = self._read()
        return sorted(
            (decoded(self._definition(value, r["id"]), "classifiers", "settings") for r in rows(value, "cyclotron_catalog")),
            key=lambda r: (r["name"], r["id"]),
        )

    def cyclotron_definition_versions(self, identifier):
        return [
            decoded(r, "classifiers", "settings")
            for r in sorted(rows(self._read(), "cyclotron_definitions", id=identifier), key=lambda r: r["revision"])
        ]

    def activate_cyclotron_definition(self, identifier, revision):
        def apply(value):
            row = self._definition(value, identifier, revision)
            self._activate(value, identifier, revision)
            return decoded(row, "classifiers", "settings")

        return self._mutate(apply)

    def run_cyclotron_definition(self, run_id):
        value = self._read()
        pin = one(value, "run_cyclotron_definitions", run_id=run_id)
        return (
            decoded(self._definition(value, pin["cyclotron_id"], pin["definition_revision"]), "classifiers", "settings")
            if pin
            else None
        )

    def cyclotron_definition_comparison(self, identifier, before_revision, after_revision):
        from .definition_diff import compare_definitions

        value = self._read()
        before = decoded(self._definition(value, identifier, before_revision), "classifiers", "settings")
        after = decoded(self._definition(value, identifier, after_revision), "classifiers", "settings")
        changes = compare_definitions(before, after)
        return {
            "before": before,
            "after": after,
            "changes": changes,
            "classifiers": [
                {
                    "id": c["id"],
                    **{
                        side: decoded(self._classifier(value, c["id"], c[side]["revision"]), "config")
                        if c[side] is not None
                        else None
                        for side in ("before", "after")
                    },
                }
                for c in changes["members"]
            ],
        }

    # Run manifests, items and initial events commit with their definition pin.
    @staticmethod
    def _run(value, identifier):
        row = one(value, "web_runs", id=identifier)
        if row is None:
            raise ValueError("unknown run")
        return row

    def create_run(self, name, mode, config, *, items=(), frozen_feedback=(), initial_events=()):
        if not name.strip() or mode not in ("live", "recorded"):
            raise ValueError("name and supported run mode required")
        identifier = str(uuid4())
        created = now()
        payload = encoded(config)

        def apply(value):
            row = insert(
                value, "web_runs", dict(id=identifier, name=name, mode=mode, status="ready", created_at=created, config=payload)
            )
            for item in items:
                if one(value, "web_items", run_id=identifier, id=item["id"]):
                    raise ValueError("duplicate run item")
                insert(
                    value, "web_items", dict(run_id=identifier, id=item["id"], payload=encoded(item), prediction=None, reviewed=0)
                )
            for item_id, classifier_id, feedback in frozen_feedback:
                insert(
                    value,
                    "replay_feedback",
                    dict(run_id=identifier, item_id=item_id, classifier_id=classifier_id, payload=encoded(feedback)),
                )
            for source_id, event in initial_events:
                self._event_insert(value, identifier, source_id, event)
            if config.get("cyclotron_definition_revision"):
                definition = self._definition(value, config["cyclotron_id"], config["cyclotron_definition_revision"])
                refs = [{"id": c["id"], "revision": c["revision"]} for c in config["classifiers"]]
                if definition["classifiers"] != encoded(refs):
                    raise ValueError("run classifiers do not match pinned definition")
                insert(
                    value,
                    "run_cyclotron_definitions",
                    dict(
                        run_id=identifier,
                        cyclotron_id=config["cyclotron_id"],
                        definition_revision=config["cyclotron_definition_revision"],
                    ),
                )
            return decoded(row, "config")

        return self._mutate(apply)

    def run(self, run_id):
        # Through a command view this read is the execution fence, so it always reads the head.
        value = self._read() if self._claim.get() is not None else self._run_value(run_id)
        row = clean(self._run(value, run_id))
        row["config"] = load_config(row["config"])
        return row

    def runs(self):
        return [decoded(r, "config") for r in reversed(rows(self._read(), "web_runs"))]

    def run_cyclotron_id(self, run_id):
        value = self._read()
        run = self._run(value, run_id)
        edition = one(value, "cyclotron_versions", run_id=run_id)
        return edition["cyclotron_id"] if edition else load_config(run["config"]).get("cyclotron_id")

    @staticmethod
    def _labeling(value, run_id):
        run = S3ConsoleStorage._run(value, run_id)
        if run["mode"] != "live":
            return {"allowed": False, "reason": "Recorded runs are read-only."}
        edition = one(value, "cyclotron_versions", run_id=run_id)
        if edition:
            family = one(value, "cyclotrons", id=edition["cyclotron_id"])
            if edition["revision"] != family["active_revision"]:
                return {"allowed": False, "reason": "Activate this cyclotron version before continuing labeling."}
        return {"allowed": True, "reason": None}

    def labeling_access(self, run_id):
        return self._labeling(self._read(), run_id)

    def counts(self, run_id):
        value = self._read()
        run = self._run(value, run_id)
        events = [json.loads(r["payload"]) for r in self._run_events(value, run_id)]
        result = {
            name: sum(e.get("kind") == kind for e in events)
            for name, kind in [
                ("cycles", "cycle-started"),
                ("predictions", "prediction"),
                ("labels", "human-feedback"),
                ("optimizations", "optimizer-request"),
            ]
        }
        if load_config(run["config"]).get("classifiers"):
            result["cycles"] = len(
                {e.get("cycle_number") for e in events if e.get("kind") == "cycle-started" and e.get("cycle_number") is not None}
            )
        return result

    def set_status(self, run_id, status):
        def apply(value):
            self._worker_write(value, run_id)
            self._run(value, run_id)["status"] = status

        self._mutate(apply)

    def update_run_limits(self, run_id, max_requests, max_optimizer_calls):
        if any(type(v) is not int or v < 1 for v in (max_requests, max_optimizer_calls)):
            raise ValueError("operating limits must be positive integers")

        def apply(value):
            run = self._run(value, run_id)
            if run["mode"] != "live":
                raise ValueError("only live runs accept operating limits")
            if self._has_unfinished(value, run_id):
                raise ValueError("wait for current work before changing limits")
            run["config"] = encoded(
                {**load_config(run["config"]), "max_requests": max_requests, "max_optimizer_calls": max_optimizer_calls}
            )
            return decoded(run, "config")

        return self._mutate(apply)

    def summary(self, run_id):
        value = self._read()
        self._run(value, run_id)
        row = one(value, "web_summaries", run_id=run_id)
        return json.loads(row["payload"]) if row else None

    def _event_insert(self, value, run_id, source_id, payload):
        if not source_id or not isinstance(payload, dict) or not payload.get("kind"):
            raise ValueError("event identity and kind required")
        raw = encoded(payload)
        existing = self._keyed(value, "web_events", ColdRows.event(run_id, source_id), run_id=run_id, source_id=source_id)
        if existing:
            if existing["payload"] != raw:
                raise ValueError("event identity already has different content")
            return existing
        return insert(value, "web_events", dict(sequence=0, run_id=run_id, source_id=source_id, payload=raw))

    def _run_events(self, value, run_id, after=0):
        """The run's events in order, those with a sequence above ``after``. Finds the first one by bisection."""
        low, high = 1, value["cold"]["ordinals"]["web_events"].get(run_id, 0) + 1
        while low < high:
            middle = (low + high) // 2
            if self._row_at(value, "web_events", run_id, middle)["sequence"] > after:
                high = middle
            else:
                low = middle + 1
        return self._rows_of(value, "web_events", run_id, low)

    def complete_run(self, run_id, summary):
        created = now()
        payload = encoded(summary)

        def apply(value):
            self._worker_write(value, run_id)
            run = self._run(value, run_id)
            old = one(value, "web_summaries", run_id=run_id)
            if old and old["payload"] != payload:
                raise ValueError("completed run already has different summary")
            if not old:
                insert(value, "web_summaries", dict(run_id=run_id, payload=payload))
                self._event_insert(
                    value, run_id, "run:completed", {"kind": "run-completed", "summary": summary, "created_at": created}
                )
            run["status"] = "completed"
            return decoded(run, "config")

        return self._mutate(apply)

    def append_batch(self, run_id, events):
        if not events:
            raise ValueError("ingest at least one event")

        def apply(value):
            self._worker_write(value, run_id)
            self._run(value, run_id)
            return [decoded(self._event_insert(value, run_id, key, event), "payload") for key, event in events]

        return self._mutate(apply)

    def append_event(self, run_id, source_id, payload):
        return self.append_batch(run_id, [(source_id, payload)])[0]

    def events(self, run_id, *, after=0, limit=1000):
        if type(after) is not int or after < 0 or type(limit) is not int or limit < 1:
            raise ValueError("a non-negative cursor and positive page size required")
        value = self._read()
        self._run(value, run_id)
        return [decoded(r, "payload") for r in islice(self._run_events(value, run_id, after), limit)]

    def all_events(self, run_id):
        value = self._read()
        self._run(value, run_id)
        return [decoded(r, "payload") for r in self._run_events(value, run_id)]

    def event_cursor(self, run_id):
        value = self._read()
        self._run(value, run_id)
        return self._event_cursor(value, run_id)

    def _event_cursor(self, value, run_id):
        last = value["cold"]["ordinals"]["web_events"].get(run_id, 0)
        return self._row_at(value, "web_events", run_id, last)["sequence"] if last else 0

    def _values(self, document):
        """A stored item document with its values read back (an inline document is returned as is)."""
        return resolved(self.journal.payloads, document)

    def _payload(self, text):
        return self._values(json.loads(text))

    def _stored_command(self, payload):
        """The command as the catalog keeps it: an item's values become a reference to their record."""
        if isinstance(payload, dict) and isinstance(payload.get("item"), dict) and "values" in payload["item"]:
            return {**payload, "item": externalized(self.journal.payloads, payload["item"])}
        return payload

    def _command_payload(self, raw):
        payload = json.loads(raw)
        if isinstance(payload, dict) and isinstance(payload.get("item"), dict):
            return {**payload, "item": self._values(payload["item"])}
        return payload

    def command(self, run_id, request_id, kind, payload):
        identifier = str(uuid4())
        raw = encoded(self._stored_command(payload))
        if not request_id or kind not in (
            "prepare",
            "label",
            "correct",
            "skip",
            "undo",
            "optimize",
            "replay-next",
            "publication",
        ):
            raise ValueError("only live runs accept supported labeling commands")

        def apply(value):
            access = self._labeling(value, run_id)
            if not access["allowed"]:
                raise ValueError(
                    "activate this cyclotron version before continuing its labeling"
                    if self._run(value, run_id)["mode"] == "live"
                    else "only live runs accept supported labeling commands"
                )
            found = self._request_entry(value, run_id, request_id)
            old = found and found["row"]
            if old:
                if old["kind"] != kind or (old["payload"] != raw and self._command_payload(old["payload"]) != payload):
                    raise ValueError("command identity has different content")
                return self._job(value, old)
            # Jobs queue in acceptance order (row order); a command is never refused because others are running or waiting.
            return self._job(
                value,
                insert(
                    value,
                    "web_jobs",
                    dict(
                        id=identifier, run_id=run_id, request_id=request_id, kind=kind, payload=raw, status="pending", result=None
                    ),
                ),
            )

        return self._mutate(apply)

    def enqueue_authorized_command(self, run_id, request_id, kind, payload, actor):
        from .machine_access import MachineActor
        from .queued_account_authorization import QueuedActor

        if not isinstance(actor, (QueuedActor, MachineActor)) or actor.run_id != run_id or actor.operation != "console:write":
            raise ValueError("trusted run-bound queued actor required")
        # Host captures/proves the actor before this transaction. Worker must
        # verify it again; neither a queue delivery nor this record is a lease.
        with self.transaction():
            value = self._tx.get()
            accepted = value["cold"]["rowids"]["web_jobs"]
            job = self.command(run_id, request_id, kind, payload)
            previous = self._job_entry(value, job["id"])["actor"]
            if value["cold"]["rowids"]["web_jobs"] == accepted and previous is None:
                raise ValueError("legacy command has no actor; cannot adopt it")
            provenance = asdict(actor)
            if previous is not None and previous != provenance:
                raise ValueError("command already bound to another actor")
            if previous is None:
                value["actors"][job["id"]] = provenance
        return job

    def command_envelope(self, job_id):
        value = self._read()
        entry = self._job_entry(value, job_id)
        if entry is None:
            raise ValueError("unknown command")
        job, actor = entry["row"], entry["actor"]
        if actor is None:
            raise ValueError("queued actor absent; legacy job cannot execute")
        job = self._job(value, job)
        if isinstance(job["payload"], dict) and isinstance(job["payload"].get("item"), dict):
            job = {**job, "payload": {**job["payload"], "item": self._values(job["payload"]["item"])}}
        return job, self._parse_actor(actor)

    def command_actor(self, job_id, owner, fence):
        view = self.for_command(job_id, owner, fence)
        actor = self._run_value(view._run_id)["actors"].get(job_id)
        if actor is None:
            raise ValueError("queued actor absent; legacy job cannot execute")
        return self._parse_actor(actor)

    def command_recovery(self, job_id):
        # Operator diagnostic only: never a grant, claim reset or takeover.
        value = self._read()
        entry = self._job_entry(value, job_id)
        if entry is None:
            raise ValueError("unknown command")
        job, claim = entry["row"], entry["claim"] or {}
        return {
            "jobId": job_id,
            "runId": job["run_id"],
            "status": job["status"],
            "owner": claim.get("owner"),
            "fence": claim.get("fence"),
            "hasActor": entry["actor"] is not None,
            "automaticTakeover": False,
            "claimedAt": claim.get("at"),
            "runQueue": [
                {"jobId": r["id"], "kind": r["kind"], "status": r["status"], "position": i}
                for i, r in enumerate(self._unfinished(value, job["run_id"]))
            ],
            **self._stuck_head(value, job),
        }

    @staticmethod
    def _stuck_head(value, job, *, minutes=None):
        """Signal a running job that has held its run longer than ``minutes`` (a crashed worker never finishes).

        The worker never takes a running claim over, so only an operator reconcile ends it; jobs queued behind
        it wait until then. Claims written before ``at`` existed report the age as unknown.
        """
        minutes = STUCK_HEAD_MINUTES if minutes is None else minutes
        claim = value["claims"].get(job["id"], {})
        if job["status"] != "running" or not claim.get("at"):
            return {"stuck": False, "runningMinutes": None, "blockedJobs": 0}
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(claim["at"])).total_seconds() / 60
        first, last = queue_bounds(value, job["run_id"])
        return {"stuck": age > minutes, "runningMinutes": round(age, 1), "blockedJobs": max(last - first + 1, 0)}

    def jobs(self, run_id, *, results=True):
        """Every job of the run, newest first. ``results=False`` skips the lake fetch: a lake-held result then shows as
        its small reference, and ``job_result(job_id)`` fetches the one result a caller needs."""
        return list(self.iter_jobs(run_id, results=results))

    def iter_jobs(self, run_id, *, results=False):
        """The run's jobs newest first, read one at a time: a caller that wants the newest few stops early."""
        value = self._read()
        self._run(value, run_id)
        for entry in self._run_job_entries(value, run_id):
            yield self._job(value, entry["row"]) if results else decoded(entry["row"], "payload", "result")

    def _unfinished(self, value, run_id):
        """The run's running and waiting jobs, oldest acceptance first. Reads every waiting job: diagnostics only."""
        running = self._running(value, run_id)
        first, last = queue_bounds(value, run_id)
        return ([running] if running is not None else []) + [
            self._job_at(value, run_id, n)["row"] for n in range(first, last + 1)
        ]

    def _position(self, value, entry):
        """How many unfinished jobs are ahead of this one (0 = next or running), or None for a finished job."""
        job = entry["row"]
        if job["status"] == "running":
            return 0
        if job["status"] != "pending":
            return None
        first, _ = queue_bounds(value, job["run_id"])
        return entry["ordinal"] - first + (self._running(value, job["run_id"]) is not None)

    def queue_head(self, run_id):
        """The pending job that should start next in this run, or None (idle, or the oldest job is still running)."""
        value = self._read()
        self._run(value, run_id)
        head = self._queue_head(value, run_id)
        return None if head is None else self._job(value, head["row"])

    def _ready_heads(self, value):
        heads = [self._queue_head(value, run_id) for run_id in value["cold"]["ordinals"]["web_jobs"]]
        return sorted((h["row"] for h in heads if h is not None), key=lambda r: r["_rowid"])

    def ready_heads(self):
        """Every run's head that is waiting to start (its run has nothing running), oldest run first."""
        return [{"id": r["id"], "run_id": r["run_id"]} for r in self._ready_heads(self._read())]

    def queue_state(self, job_id):
        """Where one job stands in its run's queue: ``position`` counts unfinished jobs ahead of it (0 = next or running)."""
        value = self._read()
        entry = self._job_entry(value, job_id)
        if entry is None:
            raise ValueError("unknown command")
        job = entry["row"]
        position = self._position(value, entry)
        return {
            "status": job["status"],
            "position": position,
            "queued": job["status"] == "pending",
            "ready": job["status"] == "pending" and position == 0,
        }

    def run_has_active_work(self, run_id):
        value = self._read()
        self._run(value, run_id)
        return self._has_unfinished(value, run_id)

    def claim_command(self, *, owner=None, job_id=None):
        """Claim the next job. Strictly one job per run runs at a time, in acceptance order.

        With ``job_id``: claim it only if it heads its run's queue, otherwise raise ``CommandNotReady`` and
        change nothing (a job that is not pending claims nothing, as before). Without: claim the oldest pending
        job of any run that is ready.
        """
        if owner is None:
            raise ValueError("cloud command claim requires an explicit worker operation owner")
        uuid(owner)

        def apply(value):
            if any(c["owner"] == owner for c in value["claims"].values()):
                raise ValueError("worker operation owner already claimed; reconcile its result")
            if job_id is None:
                heads = self._ready_heads(value)
                entry = self._job_entry(value, heads[0]["id"]) if heads else None
            else:
                entry = self._job_entry(value, job_id)
                if entry is not None and entry["row"]["status"] != "pending":
                    entry = None
                if entry is not None and self._position(value, entry) != 0:
                    raise CommandNotReady("an earlier command in this run has not finished")
            if entry is None:
                return None
            job = one(value, "web_jobs", id=entry["row"]["id"])
            if job is None:
                # A waiting job lives in its accepted record; starting it brings it (and its actor) into the head.
                job = deepcopy(entry["row"])
                table = value["snapshot"]["tables"]["web_jobs"]
                table.insert(next((i for i, r in enumerate(table) if r["_rowid"] > job["_rowid"]), len(table)), job)
                value["cold"]["hot"]["web_jobs"][job["id"]] = [entry["ordinal"], 1]
                if entry["actor"] is not None:
                    value["actors"][job["id"]] = deepcopy(entry["actor"])
            previous = value["claims"].get(job["id"])
            fence = previous["fence"] + 1 if previous else 1
            value["claims"][job["id"]] = {"owner": owner, "fence": fence, "at": now()}
            if job["run_id"] not in value["cold"]["claimed"]:
                value["cold"]["claimed"] = sorted([*value["cold"]["claimed"], job["run_id"]])
            job["status"] = "running"
            value["cold"]["next"][job["run_id"]] = entry["ordinal"] + 1
            return {**self._job(value, job), "claim_owner": owner, "claim_fence": fence}

        result = self._mutate(apply)
        if result is not None:
            self._granted.add((result["id"], result["claim_owner"], result["claim_fence"]))
        return result

    def finish_command(self, job_id, status, result, *, owner=None, fence=None):
        if owner is None or type(fence) is not int:
            raise ValueError("cloud completion requires its worker owner and fence")
        uuid(owner)
        raw = encoded(result)
        if (job_id, owner, fence) not in self._granted:
            raise ValueError("command claim receipt not confirmed in this worker")

        def check(value):
            """The running job this completion may settle, or None when it already settled identically."""
            entry = self._job_entry(value, job_id)
            job, claim = (entry["row"], entry["claim"]) if entry is not None else (None, None)
            if job is None or not same_claim(claim, owner, fence):
                raise ValueError("stale cloud command completion")
            if job["status"] == status and self._result_text(value, job) == raw:
                return None
            if job["status"] != "running":
                raise ValueError("command is not running")
            if status not in ("completed", "failed", "interrupted"):
                raise ValueError("terminal command status required")
            return job

        def settle(value, stored):
            job = one(value, "web_jobs", id=job_id)
            job["status"] = status
            job["result"] = stored
            latest = value["cold"]["hot"]["web_jobs"][job_id][0] == value["cold"]["ordinals"]["web_jobs"][job["run_id"]]
            run = self._run(value, job["run_id"])
            if (
                latest
                and status == "completed"
                and isinstance(result, dict)
                and result.get("finished") is True
                and run["mode"] == "live"
            ):
                run["status"] = "completed"

        if self.results is None:
            self._mutate(lambda value: check(value) and settle(value, raw))
            return

        # Result record first, then one catalog commit flips the job and covers the record. A claim lost
        # before that commit leaves the record as an uncovered orphan that no reader can see.
        def record(job):
            return {"jobId": job_id, "runId": job["run_id"], "status": status, "result": result}

        def reference(refs):
            return encoded({"schema": RESULT_REF, "ref": refs[RESULTS_LAKE][0].to_json()})

        if self._tx.get() is not None:
            value = self._tx.get()
            job = check(value)
            if job is None:
                return
            coverage, refs = self.results.append(coverage_in(value, self.results), [record(job)])
            value[RECORDS_KEY] = with_coverage(value, self.results, coverage)[RECORDS_KEY]
            settle(value, reference({RESULTS_LAKE: refs}))
            return
        for _ in range(self.max_conflicts):
            current = self._record()
            self._check_claim(current.value)
            job = check(current.value)
            if job is None:
                return

            whole = {}

            def change(state, refs, base=current.value, whole=whole):
                settle(state, reference(refs))
                self._evict(base, state)
                self._validate(state)
                stored = self._stored(state)
                whole.update(state)
                state.clear()
                state.update(stored)

            try:
                committed, _ = commit_with_records(self.journal, current, [(self.results, [record(job)])], change)
            except StorageConflict:
                continue
            self._committed(replace(committed, value=whole))
            return
        raise ValueError("console contention limit reached; retry with same operation identity")

    TERMINAL = TERMINAL

    def _embedded_results(self, value):
        """(job row, raw text) of finished jobs whose result is still inside the catalog row."""
        return [
            (r, r["result"])
            for r in rows(value, "web_jobs")
            if r["status"] in self.TERMINAL and r["result"] is not None and result_ref(r["result"]) is None
        ]

    def audit_results(self):
        """Read-only: the head record, the movable embedded results ``(job id, run id, status, raw text)``, the
        jobs left alone because their text is not canonical JSON, and the ids of jobs still running."""
        record = self._record()
        movable, unmovable = [], []
        for job, raw in self._embedded_results(record.value):
            try:
                canonical = encoded(json.loads(raw)) == raw
            except ValueError:
                canonical = False
            (movable if canonical else unmovable).append((job["id"], job["run_id"], job["status"], raw))
        running = [r["id"] for r in rows(record.value, "web_jobs") if r["status"] == "running"]
        return record, movable, unmovable, running

    def preview_results_conversion(self, record, movable):
        """(the catalog as it would stand after the conversion, bytes of the records it would write).

        Writes nothing; exact unless a sequence turns out to be occupied when the records are created."""
        state = deepcopy(record.value)
        coverage = coverage_in(state, self.results)
        size = 0
        for n, (job_id, run, status, raw) in enumerate(movable, coverage.last + 1):
            content = self.results._encode(n, {"jobId": job_id, "runId": run, "status": status, "result": json.loads(raw)})
            ref = self.results._ref(n, content)
            one(state, "web_jobs", id=job_id)["result"] = encoded({"schema": RESULT_REF, "ref": ref.to_json()})
            size += len(content)
        state[RECORDS_KEY] = {
            **state.get(RECORDS_KEY, {}),
            RESULTS_LAKE: Coverage(coverage.last + len(movable), coverage.skipped).to_json(),
        }
        return state, size

    def convert_embedded_results(self, *, check=None):
        """Move finished jobs' embedded results into the job-results lake, in one catalog commit.

        Result records first (the shape ``finish_command`` writes), then ONE commit swaps each embedded text
        for its reference and stores the coverage. Only terminal jobs are touched, and a result whose text
        is not canonical JSON stays where it is. Refuses while any job is running. Returns ``ResultsConverted``
        or None when nothing is embedded (idempotent).
        """
        if self.results is None:
            raise ValueError("a job-results lake is required to convert embedded results")
        for _ in range(self.max_conflicts):
            record, movable, _unmovable, running = self.audit_results()
            if running:
                raise ValueError("jobs are running; convert when the catalog has none")
            if not movable:
                return None
            ids = [job_id for job_id, *_ in movable]

            def change(state, refs, ids=ids):
                cited = iter(refs[RESULTS_LAKE])
                for job_id in ids:
                    one(state, "web_jobs", id=job_id)["result"] = encoded({"schema": RESULT_REF, "ref": next(cited).to_json()})
                self._validate(state)
                stored = self._stored(state)
                state.clear()
                state.update(stored)

            values = [
                {"jobId": job_id, "runId": run, "status": status, "result": json.loads(raw)}
                for job_id, run, status, raw in movable
            ]
            try:
                committed, refs = commit_with_records(self.journal, record, [(self.results, values)], change, check=check)
            except StorageConflict:
                continue
            return ResultsConverted(movable, refs[RESULTS_LAKE], record, committed)
        raise ValueError("console contention limit reached; retry")

    def _paid_outcome_evidence(self, value, job):
        """What the catalog records that points at a completed provider call for this job (empty means none)."""
        found = []
        if job["result"] is not None:
            found.append("the job has a recorded result")
        item_id = (json.loads(job["payload"]).get("item") or {}).get("id")
        if item_id is not None:
            item = self._item_row(value, job["run_id"], item_id)
            if item is not None and item["prediction"] is not None:
                found.append("the job's item has a displayed prediction")
            retained = ColdRows.run_item_predictions(job["run_id"], item_id)
            if self._filed(value, "item_predictions", retained, run_id=job["run_id"], item_id=item_id):
                found.append("the job's item has a retained prediction")
        needles = (job["id"], job["request_id"])
        if any(n in r["payload"] or n == r["source_id"] for r in self._run_events(value, job["run_id"]) for n in needles):
            found.append("a run event names the job or its request id")
        return found

    def reconcile_command(self, job_id, *, owner, fence, status, reason, operator, apply=False):
        """Operator-only: end a claimed, never-finished command as failed or interrupted.

        Requires the recorded owner and fence to match exactly, and refuses when the job has a result or any
        recorded evidence of a completed provider call. The audit record (who, when, why, claim, prior status)
        becomes the job's result, committed in the same transaction as the status change. With ``apply=False``
        every check runs and nothing is written. Returns the audit record.
        """
        uuid(owner)
        if type(fence) is not int or fence < 1 or status not in ("failed", "interrupted"):
            raise ValueError("owner, positive fence and a failed or interrupted target are required")
        if not isinstance(reason, str) or len(reason.strip()) < 8 or not isinstance(operator, str) or not operator:
            raise ValueError("a reason (at least 8 characters) and an operator identity are required")
        audit = {
            "reconciliation": {
                "jobId": job_id,
                "from": "running",
                "to": status,
                "owner": owner,
                "fence": fence,
                "reason": reason.strip(),
                "operator": operator,
                "at": now(),
            }
        }

        def plan(value):
            entry = self._job_entry(value, job_id)
            if entry is None:
                raise ValueError("unknown command")
            job = entry["row"]
            if job["status"] != "running":
                raise ValueError("only a running command can be reconciled")
            if not same_claim(value["claims"].get(job_id), owner, fence):
                raise ValueError("recorded owner and fence do not match")
            found = self._paid_outcome_evidence(value, job)
            if found:
                raise ValueError("refused, a provider outcome may exist: " + "; ".join(found))
            return job

        if not apply:
            plan(self._read())
            return audit

        def write(value):
            job = plan(value)
            job["status"] = status
            job["result"] = encoded(audit)

        self._mutate(write)
        return audit

    def recover_interrupted(self):
        raise ValueError(
            "cloud command recovery requires explicit scoped owner/provider reconciliation; no global timeout takeover"
        )

    def resume_feedback_command(self, run_id, job_id):
        raise ValueError("cloud feedback recovery requires reviewed fenced worker composition")

    def current_item(self, run_id):
        value = self._run_value(run_id)
        self._run(value, run_id)
        # The run's items waiting for review are kept as ordinal intervals; the first one is the current item.
        spans = value["cold"]["review"].get(run_id)
        row = self._item_at(value, run_id, spans[0][0]) if spans else None
        return {"item": self._payload(row["payload"]), "prediction": json.loads(row["prediction"])} if row else None

    def update_item(self, run_id, item_id, *, prediction=_UNSET, reviewed=_UNSET):
        if reviewed is not _UNSET and type(reviewed) is not bool:
            raise ValueError("reviewed must be a boolean")

        def apply(value):
            self._worker_write(value, run_id)
            row = self._item_row(value, run_id, item_id, for_update=True)
            if row is None:
                raise ValueError("unknown item")
            if prediction is not _UNSET:
                row["prediction"] = encoded(prediction) if prediction is not None else None
            if reviewed is not _UNSET:
                row["reviewed"] = int(reviewed)

        if prediction is not _UNSET or reviewed is not _UNSET:
            self._mutate(apply)

    def items(self, run_id):
        value = self._run_value(run_id)
        self._run(value, run_id)
        return [self._payload(r["payload"]) for r in self._run_items(value, run_id)]

    def register_standing_item(self, run_id, item_id, values):
        """Atomically register a never-before-seen source (or a new immutable revision) for a standing run.

        Only a fenced worker command may call this. The source joins the run's own operational list and
        the run's item table in the same transaction. An id that exists in any other list (priming or
        evaluation) is refused; identical values return the existing revision without a write.
        """
        if self._claim.get() is None:
            raise ValueError("standing registration requires a command-bound worker")
        if not isinstance(item_id, str) or not isinstance(values, dict):
            raise ValueError("standing item identity and values required")
        created = now()

        def apply(value):
            self._worker_write(value, run_id)
            list_id = load_config(self._run(value, run_id)["config"]).get("item_list_id")
            if not list_id or not one(value, "item_lists", id=list_id):
                raise ValueError("standing run has no operational item list")
            if any(
                other["id"] != list_id and self._latest_revision(value, other["id"], item_id) is not None
                for other in rows(value, "item_lists")
            ):
                raise ValueError("source id belongs to a priming or evaluation list; operational items must be disjoint")
            latest = self._latest_revision(value, list_id, item_id)
            if latest and same_values(json.loads(latest["payload"]), values):
                revision = latest
            else:
                document = {"id": item_id, "values": values, "occurred_at": timestamp(created)}
                # The row keeps only the reference; its fingerprint is of the stored row, which names the values' digest.
                payload = encoded(externalized(self.journal.payloads, document))
                fingerprint = sha256(payload.encode()).hexdigest()
                revision = insert(
                    value,
                    "list_item_revisions",
                    dict(
                        list_id=list_id,
                        id=item_id,
                        revision=latest["revision"] + 1 if latest else 1,
                        occurred_at=timestamp(created),
                        fingerprint=fingerprint,
                        payload=payload,
                        imported_at=created,
                    ),
                )
            row = self._item_row(value, run_id, item_id, for_update=True)
            frozen = encoded(
                {**json.loads(revision["payload"]), "revision": revision["revision"], "fingerprint": revision["fingerprint"]}
            )
            if row is None:
                insert(value, "web_items", dict(run_id=run_id, id=item_id, payload=frozen, prediction=None, reviewed=0))
            elif row["payload"] != frozen:
                row["payload"] = frozen
            return {"id": item_id, "revision": revision["revision"], "fingerprint": revision["fingerprint"]}

        return self._mutate(apply)

    def record_machine_feedback(
        self, run_id, classifier_id, classifier_revision, item_id, item_revision, label, comment, request_id, event
    ):
        """One human label plus its machine/editor provenance event, atomically and idempotently.

        Writes only a catalog label and a run event. It never touches predictions, optimizer or provider state.
        """
        if not isinstance(event, dict) or not event.get("kind") or not request_id:
            raise ValueError("feedback provenance and identity required")
        for _ in range(self.max_conflicts):
            try:
                with self.transaction():
                    value = self._tx.get()
                    run = self._run(value, run_id)
                    list_id = load_config(run["config"]).get("item_list_id")
                    if self._item_row(value, run_id, item_id) is None:
                        raise ValueError("source was never submitted to this run")
                    vote = self.label_item(
                        classifier_id, classifier_revision, list_id, item_id, item_revision, label, comment, request_id
                    )
                    self._event_insert(
                        value, run_id, "machine-feedback:" + request_id, {**event, "feedbackRequestId": request_id}
                    )
                return vote
            except StorageConflict:
                continue
        raise ValueError("console contention limit reached; retry with same operation identity")

    def unreviewed_items(self, run_id):
        value = self._run_value(run_id)
        self._run(value, run_id)
        return [self._payload(r["payload"]) for r in self._run_items(value, run_id) if not r["reviewed"]]

    # PrimingService uses this same domain transaction, including acceptance.
    def draft(self, identifier):
        row = one(self._read(), "priming_drafts", id=identifier)
        return row["payload"] if row else None

    def put_draft(self, identifier, payload):
        def apply(value):
            row = one(value, "priming_drafts", id=identifier)
            if row:
                row["payload"] = payload
            else:
                insert(value, "priming_drafts", dict(id=identifier, payload=payload))

        self._mutate(apply)

    def latest_classifier_revision(self, identifier):
        return max((r["revision"] for r in rows(self._read(), "classifiers", id=identifier)), default=None)

    def item_exists(self, list_id, identifier):
        return self._latest_revision(self._read(), list_id, identifier) is not None

    def insert_item(self, row):
        fields = dict(zip(TABLES["list_item_revisions"], row))

        def apply(value):
            # The head holds only some revisions, so the identity is checked against every one.
            if self._revision_row(value, fields["list_id"], fields["id"], fields["revision"]) is not None:
                raise ValueError("duplicate catalog record")
            insert(value, "list_item_revisions", fields)

        self._mutate(apply)

    def insert_feedback(self, row):
        fields = dict(sequence=0, **dict(zip(TABLES["item_feedback"][1:], row)))

        def apply(value):
            # The head holds only some revisions, so the revision a label pins is checked against every one.
            if self._revision_row(value, fields["list_id"], fields["item_id"], fields["item_revision"]) is None:
                raise ValueError("feedback references unknown pinned revisions")
            insert(value, "item_feedback", fields)

        self._mutate(apply)

    def accepted_records(self, list_id, classifiers):
        value = self._read()
        result = []
        for row in self._items(value, list_id):
            votes = {
                (r["classifier_id"], r["classifier_revision"]): clean(r)
                for r in self._latest_votes(value, list_id, row["id"], row["revision"])
            }
            result.append(
                {
                    "payload": row["payload"],
                    "revision": row["revision"],
                    "votes": {c["id"]: votes[c["id"], c["revision"]] for c in classifiers if (c["id"], c["revision"]) in votes},
                }
            )
        return result

    @property
    def priming_storage(self):
        return self

    @property
    def catalog_storage(self):
        return self

    @property
    def definition_storage(self):
        return self

    @property
    def run_storage(self):
        return self

    def connect(self):
        raise ValueError("cloud workspace has no SQL connection or native fallback")

    def cyclotrons(self):
        value = self._read()
        return [
            {
                **clean(r),
                "versions": [
                    {"revision": v["revision"], "run_id": v["run_id"]}
                    for v in sorted(rows(value, "cyclotron_versions", cyclotron_id=r["id"]), key=lambda v: v["revision"])
                ],
            }
            for r in sorted(rows(value, "cyclotrons"), key=lambda r: (r["name"], r["id"]))
        ]

    def cyclotron_versions(self, cyclotron_id):
        value = self._read()
        result = []
        for version in sorted(rows(value, "cyclotron_versions", cyclotron_id=cyclotron_id), key=lambda r: r["revision"]):
            run = self._run(value, version["run_id"])
            metrics = {}
            for event in self._run_events(value, run["id"]):
                payload = json.loads(event["payload"])
                if payload.get("kind") == "cycle-metrics":
                    metrics[payload["classifier_id"]] = payload["metrics"]
            result.append({**clean(version), "classifiers": load_config(run["config"])["classifiers"], "metrics": metrics})
        return result

    def activate_cyclotron_version(self, cyclotron_id, revision):
        def apply(value):
            version = one(value, "cyclotron_versions", cyclotron_id=cyclotron_id, revision=revision)
            if version is None:
                raise ValueError("unknown cyclotron version")
            ids = {r["run_id"] for r in rows(value, "cyclotron_versions", cyclotron_id=cyclotron_id)}
            if any(self._has_unfinished(value, run_id) for run_id in ids):
                raise ValueError("wait for current work before reverting")
            one(value, "cyclotrons", id=cyclotron_id)["active_revision"] = revision
            return decoded(self._run(value, version["run_id"]), "config")

        return self._mutate(apply)

    def inherited_labels(self, run_id, item_id):
        return [json.loads(r["payload"]) for r in rows(self._read(), "replay_feedback", run_id=run_id, item_id=item_id)]

    @classmethod
    def _register_definition(cls, value, run_id, identifier, name, config, created):
        existing = one(value, "run_cyclotron_definitions", run_id=run_id)
        if existing:
            return existing["definition_revision"]
        refs = [{"id": c["id"], "revision": c["revision"]} for c in config["classifiers"]]
        settings = {
            key: config[key]
            for key in (
                "decisions_provider",
                "decisions_model",
                "optimizer_model",
                "optimizer_transport",
                "selection_policy",
                "seed",
                "optimize_every",
                "rubric_changes_every",
            )
            if key in config
        }
        pinned = config.get("cyclotron_definition_revision")
        if pinned is not None:
            definition = cls._definition(value, identifier, pinned)
            if definition["classifiers"] != encoded(refs):
                raise ValueError("run classifiers do not match pinned definition")
        else:
            definition = next(
                (
                    r
                    for r in sorted(rows(value, "cyclotron_definitions", id=identifier), key=lambda r: r["revision"])
                    if (r["name"], r["classifiers"], r["settings"]) == (name, encoded(refs), encoded(settings))
                ),
                None,
            )
            if definition is None:
                definition = cls._definition_insert(value, identifier, name, refs, settings, created)
        insert(
            value,
            "run_cyclotron_definitions",
            dict(run_id=run_id, cyclotron_id=identifier, definition_revision=definition["revision"]),
        )
        return definition["revision"]

    def extend_cyclotron(self, parent_run_id, classifier_ids, *, name, seed_supporting_questions=None):
        identifier, family_id, created = str(uuid4()), str(uuid4()), now()

        def apply(value):
            parent = self._run(value, parent_run_id)
            config = load_config(parent["config"])
            if self._has_unfinished(value, parent_run_id):
                raise ValueError("wait for current work before extending the cyclotron")
            if parent["mode"] != "live" or not config.get("classifiers"):
                raise ValueError("extend a live multi-classifier run")
            old = config["classifiers"]
            old_ids = {r["id"] for r in old}
            if not classifier_ids or len(set(classifier_ids)) != len(classifier_ids) or old_ids.intersection(classifier_ids):
                raise ValueError("add distinct new classifiers")
            added = []
            for cid in classifier_ids:
                classifier = decoded(self._classifier(value, cid), "config")
                definition = classifier["config"]
                positive = next((r["label"] for r in definition["classes"] if r.get("role") == "positive"), None)
                classifier["config"] = {
                    **definition,
                    "selection_policy": definition.get(
                        "selection_policy",
                        {"primary": "f1", "aggregation": "positive" if positive else "macro", "positive_class": positive},
                    ),
                }
                added.append(classifier)
            config = {
                **config,
                "classifiers": [*old, *added],
                "parent_run_id": parent_run_id,
                "learning_policy": "fresh-replay",
                "review_policy": "missing-labels-first",
            }
            for key in ("seed_supporting_questions", "cyclotron_definition_revision", "cyclotron_definition_fingerprint"):
                config.pop(key, None)
            if seed_supporting_questions:
                config["seed_supporting_questions"] = seed_supporting_questions
            items = [json.loads(r["payload"]) for r in self._run_items(value, parent_run_id)]
            inherited = {}
            for item in items:
                votes = [
                    clean(r)
                    for r in self._latest_votes(value, config["item_list_id"], item["id"], item["revision"])
                    if any(c["id"] == r["classifier_id"] and c["revision"] == r["classifier_revision"] for c in old)
                ]
                if votes:
                    inherited[item["id"]] = votes
            ordered = [r for r in items if r["id"] in inherited] + [r for r in items if r["id"] not in inherited]
            previous = one(value, "cyclotron_versions", run_id=parent_run_id)
            family = previous["cyclotron_id"] if previous else family_id
            if not previous:
                insert(value, "cyclotrons", dict(id=family, name=name, active_revision=1))
                insert(
                    value,
                    "cyclotron_versions",
                    dict(
                        cyclotron_id=family, revision=1, run_id=parent_run_id, parent_run_id=None, created_at=parent["created_at"]
                    ),
                )
            revision = max(r["revision"] for r in rows(value, "cyclotron_versions", cyclotron_id=family)) + 1
            config.update(cyclotron_id=family, cyclotron_revision=revision, cyclotron_name=name, backfill_count=len(inherited))
            run = insert(
                value,
                "web_runs",
                dict(
                    id=identifier,
                    name=f"{name} · v{revision} · missing-label review",
                    mode="live",
                    status="ready",
                    created_at=created,
                    config=encoded(config),
                ),
            )
            for item in ordered:
                insert(
                    value, "web_items", dict(run_id=identifier, id=item["id"], payload=encoded(item), prediction=None, reviewed=0)
                )
                for vote in inherited.get(item["id"], []):
                    insert(
                        value,
                        "replay_feedback",
                        dict(run_id=identifier, item_id=item["id"], classifier_id=vote["classifier_id"], payload=encoded(vote)),
                    )
            insert(
                value,
                "cyclotron_versions",
                dict(cyclotron_id=family, revision=revision, run_id=identifier, parent_run_id=parent_run_id, created_at=created),
            )
            one(value, "cyclotrons", id=family)["active_revision"] = revision
            self._register_definition(value, parent_run_id, family, name, load_config(parent["config"]), created)
            definition_revision = self._register_definition(value, identifier, family, name, config, created)
            definition = self._definition(value, family, definition_revision)
            config.update(
                cyclotron_definition_revision=definition_revision, cyclotron_definition_fingerprint=definition["fingerprint"]
            )
            run["config"] = encoded(config)
            self._activate(value, family, definition_revision)
            return decoded(run, "config")

        return self._mutate(apply)

    def checkpoint_cyclotron(self, run_id, wheels):
        from dataclasses import asdict

        created = now()

        def apply(value):
            self._worker_write(value, run_id)
            config = load_config(self._run(value, run_id)["config"])
            snapshot = {
                "classifiers": {
                    identifier: {"fingerprint": wheel.active.fingerprint, "state": asdict(wheel.active)}
                    for identifier, wheel in wheels.items()
                },
                "definitions": [{k: r[k] for k in ("id", "revision")} for r in config["classifiers"]],
                "decisions_model": config.get("decisions_model"),
                "optimizer_model": config.get("optimizer_model"),
                "selection_policy": config.get("selection_policy"),
                "seed": config.get("seed"),
                "event_cursor": self._event_cursor(value, run_id),
            }
            payload = encoded(snapshot)
            fingerprint = sha256(payload.encode()).hexdigest()
            if not one(value, "cyclotron_checkpoints", run_id=run_id, fingerprint=fingerprint):
                insert(
                    value,
                    "cyclotron_checkpoints",
                    dict(id=0, run_id=run_id, fingerprint=fingerprint, payload=payload, created_at=created),
                )
            return fingerprint

        return self._mutate(apply)

    def cyclotron_checkpoints(self, run_id):
        value = self._read()
        self._run(value, run_id)
        return [decoded(r, "payload") for r in rows(value, "cyclotron_checkpoints", run_id=run_id)]

    # Existing algorithm is record-only; it calls the named cloud methods above.
    from .cyclotrons import Cyclotrons as _NativeAlgorithms

    promote_supporting_question = _NativeAlgorithms.promote_supporting_question
    matched_run_preflight = _NativeAlgorithms.matched_run_preflight
    matched_run_sources = _NativeAlgorithms.matched_run_sources


class _CommandView:
    """Trusted per-call worker capability; no mutable global owner binding."""

    RUN_METHODS = {
        "run",
        "counts",
        "set_status",
        "summary",
        "complete_run",
        "append_batch",
        "append_event",
        "events",
        "all_events",
        "event_cursor",
        "current_item",
        "update_item",
        "items",
        "unreviewed_items",
        "record_item_prediction",
        "inherited_labels",
        "checkpoint_cyclotron",
        "cyclotron_checkpoints",
        "matched_evaluation_inputs",
        "matched_evaluation_target",
        "register_standing_item",
    }

    def __init__(self, store, claim, run_id):
        self._store = store
        self._authority = claim
        self._run_id = run_id

    def __getattr__(self, name):
        if name not in self.RUN_METHODS | {"item_labels", "label_item", "retract_item_label", "finish_command"}:
            raise AttributeError(name)
        target = getattr(self._store, name)
        if not callable(target):
            return target

        def invoke(*args, **kwargs):
            if name in self.RUN_METHODS and (args[0] if args else kwargs.get("run_id")) != self._run_id:
                raise ValueError("command run boundary refused")
            if name == "finish_command" and (args[0] if args else kwargs.get("job_id")) != self._authority[0]:
                raise ValueError("command identity boundary refused")
            token = self._store._claim.set(self._authority)
            try:
                if name in ("label_item", "retract_item_label"):
                    import inspect

                    params = inspect.signature(target).bind(*args, **kwargs).arguments
                    value = self._store._read()
                    job = one(value, "web_jobs", id=self._authority[0])
                    config = load_config(self._store._run(value, self._run_id)["config"])
                    if name == "label_item":
                        fields = params
                        if job["kind"] not in ("label", "correct", "replay-next"):
                            raise ValueError("command does not authorize a catalog vote")
                    else:
                        fields = self._store._label(value, params["feedback_request_id"])
                        if job["kind"] != "undo" or fields is None:
                            raise ValueError("command does not authorize a catalog retraction")
                    classifier = next((c for c in config.get("classifiers", []) if c["id"] == fields["classifier_id"]), None)
                    item = self._store._item_row(value, self._run_id, fields["item_id"])
                    if (
                        classifier is None
                        or classifier["revision"] != fields["classifier_revision"]
                        or config.get("item_list_id") != fields["list_id"]
                        or item is None
                        or json.loads(item["payload"])["revision"] != fields["item_revision"]
                    ):
                        raise ValueError("catalog write outside frozen command inputs")
                    prefix = job["request_id"] if job["kind"] != "replay-next" else "replay:" + fields["item_id"]
                    if params["request_id"] != prefix + ":" + fields["classifier_id"]:
                        raise ValueError("catalog write outside command identity")
                if name == "finish_command":
                    kwargs.setdefault("owner", self._authority[1])
                    kwargs.setdefault("fence", self._authority[2])
                return target(*args, **kwargs)
            finally:
                self._store._claim.reset(token)

        return invoke

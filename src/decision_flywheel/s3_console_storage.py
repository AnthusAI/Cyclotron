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
from threading import RLock
from uuid import uuid4

from .catalog_storage import encoded, timestamp
from .console_snapshot import SCHEMA, SEQUENCES, TABLES
from .console_snapshot import validate as validate_console
from .cyclotrons import load_config
from .journal import Journal, commit_with_contract, uuid
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
from .storage_payloads import FilePayloads, S3Payloads

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


# The worker Lambda times out after 3 minutes (infra/lab/template.yaml); a claim older than this never finishes.
STUCK_HEAD_MINUTES = 10


def same_claim(claim, owner, fence):
    """A claim is its owner and fence; the optional ``at`` only records when it was taken."""
    return isinstance(claim, dict) and claim.get("owner") == owner and claim.get("fence") == fence


def now():
    return datetime.now(timezone.utc).isoformat()


def initial():
    return {
        "schema": "cyclotron-s3-console/v2",
        "snapshot": {"schema": SCHEMA, "tables": {k: [] for k in TABLES}, "sequences": {k: 0 for k in SEQUENCES}},
        "claims": {},
        "actors": {},
    }


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
    record = {"_rowid": max((r["_rowid"] for r in records), default=0) + 1, **fields}
    if domain in SEQUENCES:
        value["snapshot"]["sequences"][domain] += 1
        record[SEQUENCES[domain]] = value["snapshot"]["sequences"][domain]
        record["_rowid"] = record[SEQUENCES[domain]]
    if set(record) != set(TABLES[domain]) | {"_rowid"}:
        raise ValueError("complete known domain record required")
    records.append(record)
    return record


def results_lake_for(journal, *, max_record_bytes=None):
    """The job-results lake that sits beside a catalog journal, on the same store."""
    payloads, budget = journal.payloads, max_record_bytes or journal.max_bytes
    if isinstance(payloads, S3Payloads):
        return S3RecordLake(payloads, RESULTS_DOMAIN, RESULTS_KIND, max_record_bytes=budget)
    if isinstance(payloads, FilePayloads):
        return FileRecordLake(payloads, RESULTS_DOMAIN, RESULTS_KIND, max_record_bytes=budget)
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
        max_records,
        max_conflicts=8,
        results=None,
        snapshot_cache=None,
        reuse_reads_within_request=False,
        phase_log=None,
    ):
        if results is not None and (results.scope != getattr(journal, "boundary", (None,))[0] or results.name != RESULTS_LAKE):
            raise ValueError("job result lake must match the catalog scope and be console/job-results")
        if (
            not isinstance(journal, Journal)
            or type(max_records) is not int
            or max_records < 1
            or type(max_conflicts) is not int
            or not 1 <= max_conflicts <= 100
        ):
            raise ValueError("scoped canonical console journal and explicit limits required")
        self.journal = journal
        self.max_records = max_records
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

    def release_request_snapshot(self):
        """Stop reusing one snapshot, for a store that outlives a single request."""
        with self._request_lock:
            self.reuse_reads_within_request = False
            self._request_snapshot = None

    def _validate(self, value):
        if (
            not isinstance(value, dict)
            or set(value) - {RECORDS_KEY} != set(initial())
            or value["schema"] != "cyclotron-s3-console/v2"
            or not isinstance(value["claims"], dict)
            or not isinstance(value["actors"], dict)
        ):
            raise ValueError("invalid canonical console transaction")
        validate_console(value["snapshot"], max_records=self.max_records, max_bytes=self.journal.max_bytes)
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
        if sum(map(len, value["snapshot"]["tables"].values())) + len(value["claims"]) + len(value["actors"]) > self.max_records:
            raise ValueError("console record budget exceeded")
        for job_id, claim in value["claims"].items():
            job = one(value, "web_jobs", id=job_id)
            if job is None or set(claim) - {"at"} != {"owner", "fence"} or type(claim["fence"]) is not int or claim["fence"] < 1:
                raise ValueError("invalid command claim")
            uuid(claim["owner"])
        for job_id, actor in value["actors"].items():
            job = one(value, "web_jobs", id=job_id)
            if not isinstance(actor, dict):
                raise ValueError("invalid queued actor")
            parsed = self._parse_actor(actor)
            if job is None or parsed.run_id != job["run_id"] or parsed.operation != "console:write":
                raise ValueError("queued actor run boundary refused")

    @staticmethod
    def _parse_actor(actor):
        from .machine_access import MachineActor
        from .queued_account_authorization import QueuedActor

        return MachineActor(**actor) if actor.get("kind") == "machine" else QueuedActor(**actor)

    @staticmethod
    def _upgrade(value):
        if (
            isinstance(value, dict)
            and set(value) == {"schema", "snapshot", "claims"}
            and value["schema"] == "cyclotron-s3-console/v1"
        ):
            return {**value, "schema": "cyclotron-s3-console/v2", "actors": {}}
        return value

    def initialize(self, *, verify_existing=True):
        if verify_existing:
            self._validate(self._upgrade(self.journal.initialize(initial()).value))
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
            record = replace(record, value=self._upgrade(record.value))
            self._validate(record.value)
            if cache is not None:
                cache.put(self.journal, record)
        if self.phase_log is not None:
            self.phase_log("catalog_read", started, cache="hit" if reused else "miss")
        return record

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
        if self._claim.get() is not None or any(one(value, "web_jobs", id=key)["run_id"] == run_id for key in value["claims"]):
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
        job = one(value, "web_jobs", id=job_id)
        if job is None:
            raise ValueError("unknown command")
        return self._job(value, job)["result"]

    def for_command(self, job_id, owner, fence):
        uuid(owner)
        if type(fence) is not int or fence < 1:
            raise ValueError("valid command fence required")
        if (job_id, owner, fence) not in self._granted:
            raise ValueError("command claim receipt not confirmed in this worker")
        value = self._read()
        job = one(value, "web_jobs", id=job_id)
        if job is None or job["status"] != "running" or not same_claim(value["claims"].get(job_id), owner, fence):
            raise ValueError("stale command capability")
        return _CommandView(self, (job_id, owner, fence), job["run_id"])

    def _mutate(self, fn):
        if self._tx.get() is not None:
            return fn(self._tx.get())
        for _ in range(self.max_conflicts):
            record = self._record()
            self._check_claim(record.value)
            value = deepcopy(record.value)
            result = fn(value)
            self._validate(value)
            if value == record.value:
                return result
            try:
                commit_with_contract(self.journal, record, value)
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
            self._validate(value)
            if value != record.value:
                commit_with_contract(self.journal, record, value)
        finally:
            self._tx.reset(token)

    def close(self):
        pass

    def export_storage(self, *, max_records=10000, max_bytes=64000000):
        value = self._read()
        if any(one(value, "web_jobs", id=key)["status"] == "running" for key in value["claims"]):
            raise ValueError("quiesce cloud command claims before portable export")
        snapshot = value["snapshot"]
        if any(result_ref(r["result"]) is not None for r in snapshot["tables"]["web_jobs"]):
            # A portable snapshot is self-contained: results come back embedded.
            jobs = [{**r, "result": self._result_text(value, r)} for r in snapshot["tables"]["web_jobs"]]
            snapshot = {**snapshot, "tables": {**snapshot["tables"], "web_jobs": jobs}}
        return deepcopy(
            validate_console(
                snapshot, max_records=min(max_records, self.max_records), max_bytes=min(max_bytes, self.journal.max_bytes)
            )
        )

    def restore_storage(self, snapshot, *, max_records=10000, max_bytes=64000000):
        snapshot = validate_console(
            snapshot, max_records=min(max_records, self.max_records), max_bytes=min(max_bytes, self.journal.max_bytes)
        )
        if any(r["status"] == "running" for r in snapshot["tables"]["web_jobs"]):
            raise ValueError("reconcile native running jobs before cloud import")

        def apply(value):
            if any(value["snapshot"]["tables"].values()) or value["claims"]:
                raise ValueError("console restore requires empty destination")
            value["snapshot"] = snapshot

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
            {**clean(r), "count": len({i["id"] for i in rows(value, "list_item_revisions", list_id=r["id"])})}
            for r in sorted(rows(value, "item_lists"), key=lambda r: (r["name"], r["id"]))
        ]

    def upsert_list_items(self, list_id, items):
        if not 1 <= len(items) <= 200:
            raise ValueError("upsert between one and 200 items")
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
                if len(payload.encode()) > 4000000:
                    raise ValueError("item exceeds size limit")
                fingerprint = sha256(payload.encode()).hexdigest()
                old = max(
                    rows(value, "list_item_revisions", list_id=list_id, id=item["id"]), key=lambda r: r["revision"], default=None
                )
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

    @staticmethod
    def _items(value, list_id):
        if not one(value, "item_lists", id=list_id):
            raise ValueError("unknown item list")
        latest = {}
        for row in rows(value, "list_item_revisions", list_id=list_id):
            if row["id"] not in latest or row["revision"] > latest[row["id"]]["revision"]:
                latest[row["id"]] = row
        return sorted(latest.values(), key=lambda r: (r["occurred_at"], r["id"]))

    def snapshot_items(self, list_id):
        return [
            {**json.loads(r["payload"]), "revision": r["revision"], "fingerprint": r["fingerprint"]}
            for r in self._items(self._read(), list_id)
        ]

    def list_items(self, list_id, *, after=0, limit=200):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("item page must be bounded")
        return self.snapshot_items(list_id)[after : after + limit]

    def item_revision(self, list_id, identifier, revision):
        row = one(self._read(), "list_item_revisions", list_id=list_id, id=identifier, revision=revision)
        if row is None:
            raise ValueError("unknown item revision")
        return json.loads(row["payload"])

    @staticmethod
    def _latest_votes(value, list_id, item_id, item_revision):
        result = {}
        for row in rows(value, "item_feedback", list_id=list_id, item_id=item_id, item_revision=item_revision):
            key = (row["classifier_id"], row["classifier_revision"])
            if key not in result or row["sequence"] > result[key]["sequence"]:
                result[key] = row
        return [
            r
            for _, r in sorted(result.items())
            if not one(value, "item_feedback_retractions", feedback_request_id=r["request_id"])
        ]

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
            if not one(value, "list_item_revisions", list_id=list_id, id=item_id, revision=item_revision):
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
            old = one(value, "item_feedback", request_id=request_id)
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
            existing = one(value, "item_feedback_retractions", request_id=request_id)
            if existing:
                if existing["feedback_request_id"] != feedback_request_id:
                    raise ValueError("retraction identity has different content")
                return clean(existing)
            vote = one(value, "item_feedback", request_id=feedback_request_id)
            if vote is None:
                raise ValueError("unknown feedback identity")
            latest = max(
                rows(
                    value,
                    "item_feedback",
                    **{k: vote[k] for k in ("classifier_id", "classifier_revision", "list_id", "item_id", "item_revision")},
                ),
                key=lambda r: r["sequence"],
            )
            if vote != latest:
                raise ValueError("only the current label can be retracted")
            if one(value, "item_feedback_retractions", feedback_request_id=feedback_request_id):
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
            if not one(value, "list_item_revisions", list_id=list_id, id=item_id, revision=item_revision):
                raise ValueError("unknown item revision")
            fields = dict(run_id=run_id, list_id=list_id, item_id=item_id, item_revision=item_revision)
            old = one(value, "item_predictions", **fields)
            if old:
                if old["payload"] != payload:
                    raise ValueError("displayed prediction is immutable")
            else:
                insert(value, "item_predictions", dict(**fields, payload=payload))

        self._mutate(apply)

    def item_results(self, list_id, item_id, item_revision):
        value = self._read()
        result = []
        for r in rows(value, "item_predictions", list_id=list_id, item_id=item_id, item_revision=item_revision):
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
        row = clean(self._run(self._read(), run_id))
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
        events = [json.loads(r["payload"]) for r in rows(value, "web_events", run_id=run_id)]
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
            if any(r["status"] in ("pending", "running") for r in rows(value, "web_jobs", run_id=run_id)):
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

    @staticmethod
    def _event_insert(value, run_id, source_id, payload):
        if not source_id or not isinstance(payload, dict) or not payload.get("kind"):
            raise ValueError("event identity and kind required")
        raw = encoded(payload)
        if len(raw.encode()) > 4000000:
            raise ValueError("event exceeds size limit")
        existing = one(value, "web_events", run_id=run_id, source_id=source_id)
        if existing:
            if existing["payload"] != raw:
                raise ValueError("event identity already has different content")
            return existing
        return insert(value, "web_events", dict(sequence=0, run_id=run_id, source_id=source_id, payload=raw))

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
        if not 1 <= len(events) <= 200:
            raise ValueError("ingest between one and 200 events")

        def apply(value):
            self._worker_write(value, run_id)
            self._run(value, run_id)
            return [decoded(self._event_insert(value, run_id, key, event), "payload") for key, event in events]

        return self._mutate(apply)

    def append_event(self, run_id, source_id, payload):
        return self.append_batch(run_id, [(source_id, payload)])[0]

    def events(self, run_id, *, after=0, limit=1000):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("event page must be bounded")
        value = self._read()
        self._run(value, run_id)
        return [decoded(r, "payload") for r in rows(value, "web_events", run_id=run_id) if r["sequence"] > after][:limit]

    def all_events(self, run_id):
        value = self._read()
        self._run(value, run_id)
        return [decoded(r, "payload") for r in rows(value, "web_events", run_id=run_id)]

    def event_cursor(self, run_id):
        value = self._read()
        self._run(value, run_id)
        return max((r["sequence"] for r in rows(value, "web_events", run_id=run_id)), default=0)

    def command(self, run_id, request_id, kind, payload):
        identifier = str(uuid4())
        raw = encoded(payload)
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
            old = one(value, "web_jobs", run_id=run_id, request_id=request_id)
            if old:
                if (old["kind"], old["payload"]) != (kind, raw):
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
            existing = one(value, "web_jobs", run_id=run_id, request_id=request_id)
            if existing is not None and existing["id"] not in value["actors"]:
                raise ValueError("legacy command has no actor; cannot adopt it")
            job = self.command(run_id, request_id, kind, payload)
            previous = value["actors"].get(job["id"])
            provenance = asdict(actor)
            if previous is not None and previous != provenance:
                raise ValueError("command already bound to another actor")
            value["actors"][job["id"]] = provenance
        return job

    def command_envelope(self, job_id):
        value = self._read()
        job = one(value, "web_jobs", id=job_id)
        if job is None:
            raise ValueError("unknown command")
        actor = value["actors"].get(job_id)
        if actor is None:
            raise ValueError("queued actor absent; legacy job cannot execute")
        return self._job(value, job), self._parse_actor(actor)

    def command_actor(self, job_id, owner, fence):
        self.for_command(job_id, owner, fence)
        actor = self._read()["actors"].get(job_id)
        if actor is None:
            raise ValueError("queued actor absent; legacy job cannot execute")
        return self._parse_actor(actor)

    def command_recovery(self, job_id):
        # Operator diagnostic only: never a grant, claim reset or takeover.
        value = self._read()
        job = one(value, "web_jobs", id=job_id)
        if job is None:
            raise ValueError("unknown command")
        claim = value["claims"].get(job_id, {})
        return {
            "jobId": job_id,
            "runId": job["run_id"],
            "status": job["status"],
            "owner": claim.get("owner"),
            "fence": claim.get("fence"),
            "hasActor": job_id in value["actors"],
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
        waiting = sum(r["status"] == "pending" for r in rows(value, "web_jobs", run_id=job["run_id"]))
        return {"stuck": age > minutes, "runningMinutes": round(age, 1), "blockedJobs": waiting}

    def jobs(self, run_id, *, results=True):
        """The run's 20 newest jobs. ``results=False`` skips the lake fetch: a lake-held result then shows as
        its small reference, and ``job_result(job_id)`` fetches the one result a caller needs."""
        value = self._read()
        self._run(value, run_id)
        newest = list(reversed(rows(value, "web_jobs", run_id=run_id)))[:20]
        if results:
            return [self._job(value, r) for r in newest]
        return [decoded(r, "payload", "result") for r in newest]

    @staticmethod
    def _unfinished(value, run_id):
        """The run's pending and running jobs, oldest acceptance first."""
        return [r for r in rows(value, "web_jobs", run_id=run_id) if r["status"] in ("pending", "running")]

    @classmethod
    def _ready(cls, value, job):
        """A pending job may start only when nothing in its run is running and it heads the run's queue."""
        unfinished = cls._unfinished(value, job["run_id"])
        return job["status"] == "pending" and bool(unfinished) and unfinished[0]["id"] == job["id"]

    def queue_head(self, run_id):
        """The pending job that should start next in this run, or None (idle, or the oldest job is still running)."""
        value = self._read()
        self._run(value, run_id)
        unfinished = self._unfinished(value, run_id)
        return self._job(value, unfinished[0]) if unfinished and unfinished[0]["status"] == "pending" else None

    def queue_state(self, job_id):
        """Where one job stands in its run's queue: ``position`` counts unfinished jobs ahead of it (0 = next or running)."""
        value = self._read()
        job = one(value, "web_jobs", id=job_id)
        if job is None:
            raise ValueError("unknown command")
        unfinished = self._unfinished(value, job["run_id"])
        ids = [r["id"] for r in unfinished]
        position = ids.index(job_id) if job_id in ids else None
        return {
            "status": job["status"],
            "position": position,
            "queued": job["status"] == "pending",
            "ready": self._ready(value, job),
        }

    def run_has_active_work(self, run_id):
        value = self._read()
        self._run(value, run_id)
        return bool(self._unfinished(value, run_id))

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
                job = next((r for r in rows(value, "web_jobs") if self._ready(value, r)), None)
            else:
                job = next((r for r in rows(value, "web_jobs") if r["status"] == "pending" and r["id"] == job_id), None)
                if job is not None and not self._ready(value, job):
                    raise CommandNotReady("an earlier command in this run has not finished")
            if job is None:
                return None
            previous = value["claims"].get(job["id"])
            fence = previous["fence"] + 1 if previous else 1
            value["claims"][job["id"]] = {"owner": owner, "fence": fence, "at": now()}
            job["status"] = "running"
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
            job = one(value, "web_jobs", id=job_id)
            claim = value["claims"].get(job_id)
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
            latest = rows(value, "web_jobs", run_id=job["run_id"])[-1]
            run = self._run(value, job["run_id"])
            if (
                latest["id"] == job_id
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

            def change(state, refs):
                settle(state, reference(refs))
                self._validate(state)

            try:
                commit_with_records(self.journal, current, [(self.results, [record(job)])], change)
            except StorageConflict:
                continue
            return
        raise ValueError("console contention limit reached; retry with same operation identity")

    TERMINAL = ("completed", "failed", "interrupted")

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

    @staticmethod
    def _paid_outcome_evidence(value, job):
        """What the catalog records that points at a completed provider call for this job (empty means none)."""
        found = []
        if job["result"] is not None:
            found.append("the job has a recorded result")
        item_id = (json.loads(job["payload"]).get("item") or {}).get("id")
        if item_id is not None:
            if any(r["prediction"] is not None for r in rows(value, "web_items", run_id=job["run_id"], id=item_id)):
                found.append("the job's item has a displayed prediction")
            if rows(value, "item_predictions", run_id=job["run_id"], item_id=item_id):
                found.append("the job's item has a retained prediction")
        needles = (job["id"], job["request_id"])
        if any(
            n in r["payload"] or n == r["source_id"] for r in rows(value, "web_events", run_id=job["run_id"]) for n in needles
        ):
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
        if not isinstance(reason, str) or not 8 <= len(reason.strip()) <= 500 or not isinstance(operator, str) or not operator:
            raise ValueError("a reason (8 to 500 characters) and an operator identity are required")
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
            job = one(value, "web_jobs", id=job_id)
            if job is None:
                raise ValueError("unknown command")
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
        value = self._read()
        self._run(value, run_id)
        row = next(
            (r for r in rows(value, "web_items", run_id=run_id) if not r["reviewed"] and r["prediction"] is not None), None
        )
        return {"item": json.loads(row["payload"]), "prediction": json.loads(row["prediction"])} if row else None

    def update_item(self, run_id, item_id, *, prediction=_UNSET, reviewed=_UNSET):
        if reviewed is not _UNSET and type(reviewed) is not bool:
            raise ValueError("reviewed must be a boolean")

        def apply(value):
            self._worker_write(value, run_id)
            row = one(value, "web_items", run_id=run_id, id=item_id)
            if row is None:
                raise ValueError("unknown item")
            if prediction is not _UNSET:
                row["prediction"] = encoded(prediction) if prediction is not None else None
            if reviewed is not _UNSET:
                row["reviewed"] = int(reviewed)

        if prediction is not _UNSET or reviewed is not _UNSET:
            self._mutate(apply)

    def items(self, run_id):
        value = self._read()
        self._run(value, run_id)
        return [json.loads(r["payload"]) for r in rows(value, "web_items", run_id=run_id)]

    def register_standing_item(self, run_id, item_id, values):
        """Atomically register a never-before-seen source (or a new immutable revision) for a standing run.

        Only a fenced worker command may call this. The source joins the run's own operational list and
        the run's item table in the same transaction. An id that exists in any other list (priming or
        evaluation) is refused; identical values return the existing revision without a write.
        """
        if self._claim.get() is None:
            raise ValueError("standing registration requires a command-bound worker")
        if not isinstance(item_id, str) or not isinstance(values, dict):
            raise ValueError("bounded standing item required")
        created = now()

        def apply(value):
            self._worker_write(value, run_id)
            list_id = load_config(self._run(value, run_id)["config"]).get("item_list_id")
            if not list_id or not one(value, "item_lists", id=list_id):
                raise ValueError("standing run has no operational item list")
            if any(r["list_id"] != list_id for r in rows(value, "list_item_revisions", id=item_id)):
                raise ValueError("source id belongs to a priming or evaluation list; operational items must be disjoint")
            latest = max(
                rows(value, "list_item_revisions", list_id=list_id, id=item_id), key=lambda r: r["revision"], default=None
            )
            if latest and json.loads(latest["payload"]).get("values") == values:
                revision = latest
            else:
                payload = encoded({"id": item_id, "values": values, "occurred_at": timestamp(created)})
                revision = insert(
                    value,
                    "list_item_revisions",
                    dict(
                        list_id=list_id,
                        id=item_id,
                        revision=latest["revision"] + 1 if latest else 1,
                        occurred_at=timestamp(created),
                        fingerprint=sha256(payload.encode()).hexdigest(),
                        payload=payload,
                        imported_at=created,
                    ),
                )
            row = one(value, "web_items", run_id=run_id, id=item_id)
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
                    if one(value, "web_items", run_id=run_id, id=item_id) is None:
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
        value = self._read()
        self._run(value, run_id)
        return [json.loads(r["payload"]) for r in rows(value, "web_items", run_id=run_id) if not r["reviewed"]]

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
        return bool(rows(self._read(), "list_item_revisions", list_id=list_id, id=identifier))

    def insert_item(self, row):
        self._mutate(lambda value: insert(value, "list_item_revisions", dict(zip(TABLES["list_item_revisions"], row))))

    def insert_feedback(self, row):
        self._mutate(
            lambda value: insert(value, "item_feedback", dict(sequence=0, **dict(zip(TABLES["item_feedback"][1:], row))))
        )

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
            for event in rows(value, "web_events", run_id=run["id"]):
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
            if any(r["run_id"] in ids and r["status"] in ("pending", "running") for r in rows(value, "web_jobs")):
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
            if any(r["status"] in ("pending", "running") for r in rows(value, "web_jobs", run_id=parent_run_id)):
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
            items = [json.loads(r["payload"]) for r in rows(value, "web_items", run_id=parent_run_id)]
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
                "event_cursor": max((r["sequence"] for r in rows(value, "web_events", run_id=run_id)), default=0),
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
                        fields = one(value, "item_feedback", request_id=params["feedback_request_id"])
                        if job["kind"] != "undo" or fields is None:
                            raise ValueError("command does not authorize a catalog retraction")
                    classifier = next((c for c in config.get("classifiers", []) if c["id"] == fields["classifier_id"]), None)
                    item = one(value, "web_items", run_id=self._run_id, id=fields["item_id"])
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

"""Read-only lab views of a standing publication run: its decisions, one item's detail, and health.

Every route needs a signed-in person with ``console:read`` on the pinned account (``X-Cyclotron-Account``),
re-authorized by the account service on each request, as the lab's other routes are. Nothing here writes.

    GET /api/lab/standing-runs                          the account's standing runs, outputs and classes
    GET /api/lab/runs/{run}/decisions                   one page of delivered decisions, newest first
    GET /api/lab/runs/{run}/items/{item}?job={jobId}    one item: revisions, the decision, its exchange, labels
    GET /api/lab/runs/{run}/health                      throughput, spacing, queue and failures

Where the rows come from. A decision is a completed publication job of the run: the catalog keeps the run's
jobs by position (1 = oldest), and a job's result names its decision, every output's label, confidence and
review routing, and the item revision it decided. A page reads the jobs at the positions it covers, and for
each the item revision and the item's current labels, all found by key: a page costs the same whatever the
run's size. The cursor is a position, so a page is the same whenever it is read and every page is reachable.

Filters are applied to the rows of the positions a page covers. A page therefore says which positions it
examined (``examined``) and may hold fewer rows than it covered, or none; ``next`` is null only once the
oldest position (or the ``since`` bound) is reached. ``until`` and the health windows find their positions by
bisection on decision times, never by walking from the newest.

The exact decision request and response are the trace events of each output's runtime, read without owning
it. They are found by bisection on event time around the decision's time, then by reading back to the item's
cycle, so a detail reads a logarithmic number of records per output.
"""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from threading import Lock

from fastapi import HTTPException, Request

from .decision_kinds import is_decision
from .hosted_publications import configured_targets
from .journal import uuid
from .machine_access import MachineActor
from .s3_runtime_storage import S3RuntimeStores, runtime_events_lake
from .storage_errors import StorageMissing

READ = "console:read"
ROUTE = "/api/lab"
PAGE_SIZE = 50  # the default page: positions a page covers when the caller names none. Every page is reachable.
READ_WORKERS = 16  # concurrent record reads within one request; not a limit on how many are read
TRACE_PAGE = 100  # events read together while walking back to an item's cycle; not a limit on how many
TERMINAL_FAILURES = ("failed", "interrupted")
TRUE, FALSE = ("true", "1", "yes"), ("false", "0", "no")


def parse_time(value):
    """An aware datetime from ISO text (``Z`` accepted), or None for anything else."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _parallel(fn, values):
    values = list(values)
    if len(values) <= 1:
        return [fn(value) for value in values]
    with ThreadPoolExecutor(min(READ_WORKERS, len(values))) as pool:
        return list(pool.map(lambda value: copy_context().run(fn, value), values))


def _review(value):
    return {key: value.get(key) for key in ("selected", "reason", "propensity", "detail")}


def delivered(job):
    """What a completed publication job decided, or None for any other job.

    ``{"decisionId", "decidedAt", "itemId", "itemRevision", "itemFingerprint", "outputs"}``; ``outputs`` maps
    each output id to its publication, label, confidence, probabilities, version and review routing.
    """
    if not is_decision(job.get("kind")) or job.get("status") != "completed":
        return None
    payload = job.get("payload")
    if not isinstance(payload, dict) or not isinstance(payload.get("item"), dict):
        return None
    response = (job.get("result") or {}).get("response")
    if not isinstance(response, dict):
        return None
    if isinstance(response.get("publications"), dict):
        projections = response["publications"]
    elif isinstance(response.get("decision"), dict):
        projections = {response.get("publicationId"): response}
    else:
        return None
    found = {"outputs": {}, "itemRevision": None, "itemFingerprint": None}
    for binding, projection in projections.items():
        decision = projection.get("decision")
        if not isinstance(decision, dict):
            continue
        found.update(decisionId=decision.get("decisionId"), decidedAt=decision.get("createdAt"), itemId=decision.get("itemId"))
        source = projection.get("source")
        if isinstance(source, dict):
            found.update(itemRevision=source.get("revision"), itemFingerprint=source.get("fingerprint"))
        review = decision.get("review") or {}
        for output, result in (decision.get("classifiers") or {}).items():
            found["outputs"][output] = {
                "binding": binding,
                "label": result.get("label"),
                "confidence": result.get("confidence"),
                "probabilities": result.get("probabilities"),
                "version": result.get("version"),
                "review": _review((review.get("outputs") or {}).get(output) or review),
            }
    return found if found.get("decisionId") and found["outputs"] else None


class Filters:
    """The filters a decisions page applies to the rows of the positions it covers."""

    FIELDS = ("output", "label", "minConfidence", "maxConfidence", "needsReview", "labeled", "source", "since", "until")

    def __init__(self, query, outputs):
        def flag(name):
            raw = query.get(name)
            if raw is None or raw == "":
                return None
            if raw.lower() in TRUE:
                return True
            if raw.lower() in FALSE:
                return False
            raise ValueError(f"{name} must be true or false")

        def number(name):
            raw = query.get(name)
            if raw is None or raw == "":
                return None
            try:
                value = float(raw)
            except ValueError:
                raise ValueError(f"{name} must be a number from 0 to 1") from None
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be a number from 0 to 1")
            return value

        def moment(name):
            raw = query.get(name)
            if raw is None or raw == "":
                return None
            value = parse_time(raw)
            if value is None:
                raise ValueError(f"{name} must be an ISO 8601 time")
            return value

        self.output = query.get("output") or None
        if self.output is not None and self.output not in outputs:
            raise ValueError("output is not one of this run's outputs")
        self.label = query.get("label") or None
        self.low, self.high = number("minConfidence"), number("maxConfidence")
        self.needs_review, self.labeled = flag("needsReview"), flag("labeled")
        self.source = query.get("source") or None
        self.since, self.until = moment("since"), moment("until")

    @property
    def decision_level(self):
        return any(v is not None for v in (self.output, self.label, self.low, self.high))

    def _output_matches(self, value, *, labels_known):
        if self.label is not None and value["label"] != self.label:
            return False
        confidence = value["confidence"]
        if self.low is not None and (confidence is None or confidence < self.low):
            return False
        if self.high is not None and (confidence is None or confidence > self.high):
            return False
        if labels_known:
            human = value.get("humanLabel") is not None
            if self.labeled is not None and human != self.labeled:
                return False
            if self.needs_review is not None and (bool(value["review"]["selected"]) and not human) != self.needs_review:
                return False
        return True

    def matches(self, row, *, labels_known=True):
        """Whether a row passes: its source, and one output (the named one, if any) passing every output filter."""
        if labels_known and self.source is not None and row["item"].get("source") != self.source:
            return False
        names = [self.output] if self.output is not None else list(row["outputs"])
        return any(
            name in row["outputs"] and self._output_matches(row["outputs"][name], labels_known=labels_known) for name in names
        )


class FinishedJobs:
    """Summaries of a run's finished jobs by position, kept while they are inside the health window.

    A finished job never changes, so what was read once stays true; a health request reads only the positions
    it has not seen. Each request drops the positions that have left its window, so this holds one day of jobs.
    """

    def __init__(self):
        self._runs, self._lock = {}, Lock()

    def get(self, key, n):
        with self._lock:
            return self._runs.get(key, {}).get(n)

    def put(self, key, n, summary):
        with self._lock:
            self._runs.setdefault(key, {})[n] = summary

    def keep_above(self, key, n):
        with self._lock:
            held = self._runs.get(key, {})
            for position in [p for p in held if p <= n]:
                del held[position]


class RunReader:
    """Reads one run's jobs by position, each once per request, with job summaries shared across requests."""

    def __init__(self, store, run_id, finished=None, key=None):
        self.store, self.run_id, self.finished, self.key = store, run_id, finished, key
        self._jobs, self._lock = {}, Lock()

    def job(self, n):
        with self._lock:
            if n in self._jobs:
                return self._jobs[n]
        job = self.store.run_job_at(self.run_id, n)
        with self._lock:
            self._jobs[n] = job
        return job

    def summary(self, n, *, timed=False):
        """``{"status", "decision"}`` for position ``n``, with ``at`` (the decision's time) when ``timed``.

        Whether a job is a decision is in its row; only its time needs the result record.
        """
        known = self.finished.get(self.key, n) if self.finished is not None else None
        if known is None:
            row = self.store.run_job_at(self.run_id, n, results=False)
            payload = row["payload"] if isinstance(row["payload"], dict) else {}
            decision = is_decision(row["kind"]) and row["status"] == "completed" and isinstance(payload.get("item"), dict)
            known = {"status": row["status"], "decision": decision}
        if timed and known["decision"] and "at" not in known:
            found = delivered(self.job(n))
            known = {**known, "decision": found is not None, "at": found["decidedAt"] if found else None}
        if self.finished is not None and known["status"] not in ("pending", "running"):
            self.finished.put(self.key, n, known)
        return known

    def time_at(self, n):
        """The time of the newest decision at or below position ``n``, or None when there is none."""
        while n >= 1:
            summary = self.summary(n, timed=True)
            if summary["decision"]:
                return parse_time(summary["at"])
            n -= 1
        return None

    def boundary(self, top, cutoff, *, strict):
        """The highest position in ``0 .. top`` whose newest decision at or below it is before ``cutoff``
        (``strict``) or not after it; positions above it decided at or after the cutoff. Bisection."""
        low, high = 0, top
        while low < high:
            middle = (low + high + 1) // 2
            at = self.time_at(middle)
            if at is None or (at < cutoff if strict else at <= cutoff):
                low = middle
            else:
                high = middle - 1
        return low


def run_outputs(run):
    return {row["id"]: row for row in run["config"].get("classifiers", [])}


def _values_view(values):
    keep = ("title", "url", "source")
    return {key: values.get(key) for key in keep if key in values and isinstance(values.get(key), (str, int, float))}


def build_row(n, job):
    found = delivered(job)
    return {
        "position": n,
        "jobId": job["id"],
        "decisionId": found["decisionId"],
        "decidedAt": found["decidedAt"],
        "item": {"id": found["itemId"], "revision": found["itemRevision"], "fingerprint": found["itemFingerprint"]},
        "outputs": {name: {**value, "humanLabel": None} for name, value in found["outputs"].items()},
    }


def enrich(store, list_id, row):
    """The row with its item's title, url and source and each output's current human label (all by key)."""
    revision = row["item"]["revision"]
    if list_id and type(revision) is int:
        try:
            row["item"].update(_values_view(store.item_revision(list_id, row["item"]["id"], revision).get("values") or {}))
        except ValueError:
            pass
        for vote in store.item_labels(list_id, row["item"]["id"], revision):
            if vote["classifier_id"] in row["outputs"]:
                row["outputs"][vote["classifier_id"]]["humanLabel"] = vote["label"]
    return row


def decisions_page(store, run, *, before, page_size, filters):
    """One page of decisions, newest first, covering positions ``before - 1`` downward."""
    run_id = run["id"]
    list_id = run["config"].get("item_list_id")
    reader = RunReader(store, run_id)
    total = store.run_job_count(run_id)
    top = total if before is None else min(before - 1, total)
    if filters.until is not None and top > 0:
        top = reader.boundary(top, filters.until, strict=False)
    positions = list(range(top, max(top - page_size, 0), -1))
    jobs = _parallel(reader.job, positions)
    candidates, reached = [], False
    for n, job in zip(positions, jobs):
        if job is None or delivered(job) is None:
            continue
        row = build_row(n, job)
        if filters.since is not None and (parse_time(row["decidedAt"]) or filters.since) < filters.since:
            reached = True
            break
        if filters.until is not None and (parse_time(row["decidedAt"]) or filters.until) > filters.until:
            continue
        if filters.matches(row, labels_known=False):
            candidates.append(row)
    rows = [row for row in _parallel(lambda row: enrich(store, list_id, row), candidates) if filters.matches(row)]
    lowest = positions[-1] if positions else 0
    return {
        "runId": run_id,
        "decisions": rows,
        "examined": {"from": top, "to": lowest},
        "next": None if reached or lowest <= 1 else str(lowest),
        "total": total,
    }


def _quantile(values, q):
    """The ``q`` quantile of sorted ``values`` by linear interpolation, or None for no values."""
    if not values:
        return None
    position = (len(values) - 1) * q
    low = int(position)
    high = min(low + 1, len(values) - 1)
    return round(values[low] + (values[high] - values[low]) * (position - low), 3)


def health(store, run_id, finished, key, now):
    reader = RunReader(store, run_id, finished, key)
    total = store.run_job_count(run_id)
    queue = store.queue_counts(run_id)
    day = reader.boundary(total, now - timedelta(days=1), strict=True)
    hour = reader.boundary(total, now - timedelta(hours=1), strict=True)
    finished.keep_above(key, day)
    window = list(range(total, day, -1))
    summaries = dict(zip(window, _parallel(lambda n: reader.summary(n, timed=n > hour), window)))
    completed = [n for n, s in summaries.items() if s["decision"] and s["status"] == "completed"]
    times = sorted(t for t in (parse_time(summaries[n]["at"]) for n in completed if n > hour) if t is not None)
    gaps = sorted((b - a).total_seconds() for a, b in zip(times, times[1:]))
    return {
        "runId": run_id,
        "generatedAt": now.isoformat(),
        "decisions": {"lastHour": sum(1 for n in completed if n > hour), "lastDay": len(completed)},
        "secondsBetweenDecisions": {
            "window": "last hour",
            "samples": len(gaps),
            "median": _quantile(gaps, 0.5),
            "p90": _quantile(gaps, 0.9),
        },
        "queue": queue,
        "failed": {"lastDay": sum(1 for s in summaries.values() if s["status"] in TERMINAL_FAILURES)},
        "latestPosition": total,
    }


# Trace events ----------------------------------------------------------------------------------------------
def runtime_reader(runtime, account, cyclotron_id, output):
    """A read-only handle on one output's runtime of the run's shared cyclotron (the worker's own journals)."""
    name = "publication-" + sha256(cyclotron_id.encode()).hexdigest()
    root = runtime.journal(account, "workspace", name)
    journal = runtime.journal(account, "runtime", sha256((root.namespace + "\0" + output).encode()).hexdigest())
    return S3RuntimeStores(journal, events_lake=runtime_events_lake(journal))


def _event_time(event):
    return parse_time(event.get("created_at"))


def item_events(reader, item_id, decided):
    """This output's events of the item's cycle that produced the decision made at ``decided``, oldest first."""
    try:
        last = reader.observed_events(after_event_id=0, limit=1)["last"]
    except StorageMissing:
        return []
    low, high = 0, last
    while low < high:  # the newest event at or before the decision
        middle = (low + high + 1) // 2
        found = reader.observed_events(after_event_id=middle - 1, limit=1, same_head=True)["events"]
        at = _event_time(found[0]) if found else None
        if at is None or at <= decided:
            low = middle
        else:
            high = middle - 1
    mine, top = [], low
    while top > 0:
        start = max(0, top - TRACE_PAGE)
        page = reader.observed_events(after_event_id=start, limit=top - start, same_head=True)["events"]
        for event in reversed(page):
            ours = item_id in (event.get("target_id"), event.get("cycle_item_id"))
            if ours:
                mine.append(event)
                if event.get("kind") == "cycle-started" and any(e.get("kind") == "prediction" for e in mine):
                    return mine[::-1]
            elif event.get("kind") == "prediction":
                return mine[::-1]  # an earlier decision of another item: this item's events are all read
        top = start
    return mine[::-1]


def exchange_of(events):
    """(prediction, request, response) of the newest prediction among ``events`` and its own cycle."""
    prediction = next((e for e in reversed(events) if e.get("kind") == "prediction"), None)
    cycle = prediction.get("cycle_id") if prediction else None
    scoped = [e for e in events if cycle is None or e.get("cycle_id") == cycle]
    if prediction is not None:
        scoped = [e for e in scoped if e["event_id"] <= prediction["event_id"]]
    request = next((e for e in reversed(scoped) if e.get("kind") in ("decision-request", "features-cached")), None)
    response = next((e for e in reversed(scoped) if e.get("kind") == "decision-response"), None)
    return prediction, request, response


def output_trace(runtime, account, cyclotron_id, output, item_id, decided):
    reader = runtime_reader(runtime, account, cyclotron_id, output)
    prediction, request, response = exchange_of(item_events(reader, item_id, decided))
    return {
        "cycleId": prediction.get("cycle_id") if prediction else None,
        "composition": prediction.get("request_composition") if prediction else None,
        "model": (response or request or prediction or {}).get("model"),
        "prediction": prediction,
        "request": request,
        "response": response,
    }


# Item detail -----------------------------------------------------------------------------------------------
def label_source(store, run_id, row):
    """Who gave a label, where the catalog knows: a reviewer at the caller (Papyrus, say), or the lab."""
    event = None
    if row["request_id"].startswith("machine-feedback:"):
        event = store.run_event_by_source(run_id, "machine-feedback:" + row["request_id"])
    payload = (event or {}).get("payload") or {}
    if payload.get("kind") == "publication-machine-feedback":
        # Stored feedback events keep Papyrus's field names (publicationId, editor.site, editor.subject).
        editor = payload.get("editor") or {}
        return {
            "kind": "caller",
            "binding": payload.get("publicationId"),
            "caller": editor.get("site"),
            "reviewer": editor.get("subject"),
            "asserted": bool(editor.get("asserted")),
            "principal": (payload.get("machine") or {}).get("principal"),
        }
    return {"kind": "lab"}


def item_detail(store, runtime, account, run, item_id, job_id):
    run_id, list_id = run["id"], run["config"].get("item_list_id")
    entry = store.job_position(job_id)
    if entry is None or entry["job"]["run_id"] != run_id:
        raise LookupError("unknown decision for this run")
    found = delivered(entry["job"])
    if found is None or found["itemId"] != item_id:
        raise LookupError("unknown decision for this item")
    revisions = store.item_revisions(list_id, item_id) if list_id else []
    actor = entry["actor"]
    if isinstance(actor, MachineActor):
        via = {"kind": "machine", "principal": actor.principal, "session": actor.session}
    elif actor is not None and getattr(actor, "person_id", None):
        via = {"kind": "person", "personId": actor.person_id}
    else:
        via = None
    history = [
        {
            "revision": row["revision"],
            "fingerprint": row["fingerprint"],
            "occurredAt": row.get("occurred_at"),
            "importedAt": row.get("imported_at"),
            "values": row.get("values") or {},
            "provenance": (
                {**via, "requestId": entry["job"]["request_id"], "jobId": job_id}
                if via is not None and row["revision"] == found["itemRevision"]
                else None
            ),
        }
        for row in revisions
    ]
    labels = []
    for row in revisions:
        for label in store.item_label_records(list_id, item_id, row["revision"]):
            labels.append(
                {
                    "output": label["classifier_id"],
                    "classifierRevision": label["classifier_revision"],
                    "itemRevision": label["item_revision"],
                    "label": label["label"],
                    "comment": label["comment"],
                    "createdAt": label["created_at"],
                    "retracted": label["retracted"],
                    "requestId": label["request_id"],
                    "source": label_source(store, run_id, label),
                }
            )
    decided = parse_time(found["decidedAt"])
    cyclotron_id = run["config"].get("cyclotron_id")
    names = list(found["outputs"])
    traces = (
        _parallel(lambda name: output_trace(runtime, account, cyclotron_id, name, item_id, decided), names)
        if cyclotron_id and decided is not None
        else [None] * len(names)
    )
    exchanges, outputs = {}, {}
    classifiers = run_outputs(run)
    for name, trace in zip(names, traces):
        exchange = None
        if trace is not None and (trace["request"] or trace["response"]):
            shared = (trace["request"] or trace["response"]).get("shared_request_fingerprint")
            exchange = shared or f"{name}:{trace['cycleId']}"
            exchanges.setdefault(exchange, {"request": trace["request"], "response": trace["response"], "outputs": []})
            exchanges[exchange]["outputs"].append(name)
        outputs[name] = {
            **found["outputs"][name],
            "name": (classifiers.get(name) or {}).get("name", name),
            "model": trace["model"] if trace else None,
            "composition": trace["composition"] if trace else None,
            "prediction": trace["prediction"] if trace else None,
            "exchange": exchange,
        }
    current = history[-1] if history else None
    return {
        "runId": run_id,
        "item": {
            "id": item_id,
            "revision": current["revision"] if current else None,
            "values": current["values"] if current else {},
        },
        "revisions": history,
        "decision": {
            "jobId": job_id,
            "position": entry["position"],
            "decisionId": found["decisionId"],
            "decidedAt": found["decidedAt"],
            "itemRevision": found["itemRevision"],
            "outputs": outputs,
        },
        "exchanges": exchanges,
        "labels": labels,
    }


# Routes ----------------------------------------------------------------------------------------------------
def install_lab_decision_routes(app, runtime):
    finished = FinishedJobs()

    async def access(request):
        try:
            account = uuid(request.headers.get("X-Cyclotron-Account"))
        except (ValueError, TypeError):
            raise HTTPException(400, "explicit account pin required") from None
        session = await runtime.gateway._require(request)
        await asyncio.to_thread(runtime.accounts.authorize, session.access_token, account, session.person_id, READ)
        return account

    def standing_target(account, run_id):
        target = next(
            (t for t in configured_targets(runtime) if t.account_id == account and t.run_id == run_id and t.standing), None
        )
        if target is None:
            raise HTTPException(404, "unknown standing run")
        return target

    def load_run(store, run_id):
        try:
            return store.run(run_id)
        except (ValueError, StorageMissing):
            raise HTTPException(404, "standing run unavailable") from None

    headers = {"Cache-Control": "private, no-store"}

    @app.get(ROUTE + "/standing-runs")
    async def standing_runs(request: Request):
        account = await access(request)
        store = runtime.console(account, request=True)
        result = []
        for target in configured_targets(runtime):
            if target.account_id != account or not target.standing:
                continue
            run = await asyncio.to_thread(load_run, store, target.run_id)
            classifiers = run_outputs(run)
            # The cyclotron definition the run is pinned to, so the catalog can show which cyclotrons are in use.
            pin = await asyncio.to_thread(store.run_cyclotron_definition, target.run_id)
            result.append(
                {
                    "runId": target.run_id,
                    "name": run["name"],
                    "cyclotron": {"id": pin["id"], "revision": pin["revision"]} if pin else None,
                    "bindings": [{"id": b.publication_id, "outputs": list(b.outputs)} for b in target.bindings],
                    "outputs": [
                        {
                            "id": cid,
                            "name": row.get("name", cid),
                            "question": row["config"].get("question"),
                            "classes": [c["label"] for c in row["config"].get("classes", [])],
                        }
                        for cid, row in classifiers.items()
                    ],
                }
            )
        return _json(result, headers)

    @app.get(ROUTE + "/runs/{run_id}/decisions")
    async def decisions(run_id: str, request: Request):
        account = await access(request)
        standing_target(account, run_id)
        query = request.query_params
        unknown = set(query) - {"before", "pageSize", *Filters.FIELDS}
        if unknown:
            raise HTTPException(400, "unknown filter: " + ", ".join(sorted(unknown)))
        try:
            before = _positive(query.get("before"), "before")
            page_size = _positive(query.get("pageSize"), "pageSize") or PAGE_SIZE
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        store = runtime.console(account, request=True)
        run = await asyncio.to_thread(load_run, store, run_id)
        try:
            filters = Filters(query, run_outputs(run))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        page = await asyncio.to_thread(decisions_page, store, run, before=before, page_size=page_size, filters=filters)
        return _json(page, headers)

    @app.get(ROUTE + "/runs/{run_id}/items/{item_id}")
    async def item(run_id: str, item_id: str, request: Request):
        account = await access(request)
        standing_target(account, run_id)
        job_id = request.query_params.get("job")
        try:
            uuid(job_id)
        except (ValueError, TypeError):
            raise HTTPException(400, "the decision's job id is required (open the item from the decisions list)") from None
        store = runtime.console(account, request=True)
        run = await asyncio.to_thread(load_run, store, run_id)
        try:
            detail = await asyncio.to_thread(item_detail, store, runtime, account, run, item_id, job_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None
        return _json(detail, headers)

    @app.get(ROUTE + "/runs/{run_id}/health")
    async def run_health(run_id: str, request: Request):
        account = await access(request)
        standing_target(account, run_id)
        store = runtime.console(account, request=True)
        await asyncio.to_thread(load_run, store, run_id)
        value = await asyncio.to_thread(health, store, run_id, finished, (account, run_id), datetime.now(timezone.utc))
        return _json(value, headers)


def _positive(raw, name):
    if raw is None or raw == "":
        return None
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a positive integer") from None
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _json(value, headers):
    from fastapi.responses import JSONResponse

    return JSONResponse(json.loads(json.dumps(value, default=str)), headers=headers)

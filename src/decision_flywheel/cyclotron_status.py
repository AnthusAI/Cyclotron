"""The cyclotron-status/v1 snapshot: what one classifier is doing right now.

Applications render this snapshot instead of parsing trace events. It is built
from durable flywheel events only and never calls a model. Alignment numbers
use reviews the cyclotron selected; reviews a reviewer chose for themselves
(``selected_by == "reviewer"``) are excluded because they are not a random
or policy-selected sample of decisions.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .plain_status import plain_change_summary, plain_next_step, plain_reason
from .rolling_metrics import recent_reviewed_metrics

SCHEMA = "cyclotron-status/v1"
SCHEMA_PATH = Path(__file__).with_name("schemas") / "cyclotron-status.v1.schema.json"
REVIEW_RATE_STATES = ("full", "onboarding", "tapering", "steady", "raised", "manual")
CHANGE_KINDS = ("promoted", "refit", "dropped", "definition")
MEASURED_ON = "reviews selected by the cyclotron"


@dataclass(frozen=True)
class CalibrationBin:
    bin: float
    count: int
    confidence: float
    accuracy: float


@dataclass(frozen=True)
class Calibration:
    says_sure: float | None
    is_right: float | None
    gap_points: int | None
    curve: tuple[CalibrationBin, ...] = ()


@dataclass(frozen=True)
class Alignment:
    window: int
    labels: int
    accuracy: float | None
    precision: float | None
    recall: float | None
    positive_label: str | None
    measured_on: str = MEASURED_ON


@dataclass(frozen=True)
class ReviewRateOverride:
    rate: float
    expires_at: str | None
    set_by: str
    set_at: str


@dataclass(frozen=True)
class ReviewRate:
    state: str
    rate: float
    reason: str
    audit_floor: float | None = None
    confidence_threshold: float | None = None
    override: ReviewRateOverride | None = None
    next_check_after_decisions: int | None = None
    next_step: str | None = None
    expected_reviews_per_week: float | None = None

    def __post_init__(self):
        if self.state not in REVIEW_RATE_STATES:
            raise ValueError(f"review rate state must be one of {REVIEW_RATE_STATES}")
        for name in ("rate", "audit_floor", "confidence_threshold"):
            value = getattr(self, name)
            if value is not None and not 0 <= value <= 1:
                raise ValueError(f"{name} must be between zero and one")
        if not self.reason.strip():
            raise ValueError("review rate needs a reason a person can read")


FULL_REVIEW = ReviewRate("full", 1.0, "No review-rate program is configured, so every decision is reviewed.")


@dataclass(frozen=True)
class LastChange:
    kind: str
    from_version: int | None
    to_version: int
    at: str | None
    summary: str
    labels: int | None = None

    def __post_init__(self):
        if self.kind not in CHANGE_KINDS:
            raise ValueError(f"change kind must be one of {CHANGE_KINDS}")


@dataclass(frozen=True)
class Pending:
    decisions_awaiting_review: int
    stale_since: str | None


@dataclass(frozen=True)
class CyclotronStatus:
    cyclotron_id: str
    classifier: str
    version: int
    fingerprint: str
    as_of: str
    alignment: Alignment
    calibration: Calibration
    review_rate: ReviewRate
    last_change: LastChange | None
    pending: Pending
    refits: int = 0
    primed_examples: int = 0
    schema: str = field(default=SCHEMA)

    def to_json(self) -> dict[str, Any]:
        """The wire shape described by cyclotron-status.v1.schema.json."""
        a, c, r, p = self.alignment, self.calibration, self.review_rate, self.pending
        return {
            "schema": self.schema,
            "cyclotron": {
                "id": self.cyclotron_id,
                "classifier": self.classifier,
                "version": self.version,
                "refits": self.refits,
                "fingerprint": self.fingerprint,
                "primedExamples": self.primed_examples,
            },
            "asOf": self.as_of,
            "alignment": {
                "window": a.window,
                "labels": a.labels,
                "accuracy": a.accuracy,
                "precision": a.precision,
                "recall": a.recall,
                "positiveLabel": a.positive_label,
                "measuredOn": a.measured_on,
            },
            "calibration": {
                "saysSure": c.says_sure,
                "isRight": c.is_right,
                "gapPoints": c.gap_points,
                "curve": [{"bin": b.bin, "count": b.count, "confidence": b.confidence, "accuracy": b.accuracy} for b in c.curve],
            },
            "reviewRate": {
                "state": r.state,
                "rate": r.rate,
                "reason": r.reason,
                "plainReason": plain_reason(r.state, r.rate, None if r.override is None else r.override.set_by),
                "auditFloor": r.audit_floor,
                "confidenceThreshold": r.confidence_threshold,
                "override": None
                if r.override is None
                else {
                    "rate": r.override.rate,
                    "expiresAt": r.override.expires_at,
                    "setBy": r.override.set_by,
                    "setAt": r.override.set_at,
                },
                "nextCheckAfterDecisions": r.next_check_after_decisions,
                "nextStep": r.next_step,
                "plainNextStep": None if r.next_step is None else plain_next_step(r.state, r.rate),
                "expectedReviewsPerWeek": r.expected_reviews_per_week,
            },
            "lastChange": None
            if self.last_change is None
            else {
                "kind": self.last_change.kind,
                "fromVersion": self.last_change.from_version,
                "toVersion": self.last_change.to_version,
                "at": self.last_change.at,
                "summary": self.last_change.summary,
                "plainSummary": plain_change_summary(self.last_change.kind, self.last_change.labels),
                "labels": self.last_change.labels,
            },
            "pending": {"decisionsAwaitingReview": p.decisions_awaiting_review, "staleSince": p.stale_since},
        }


@dataclass(frozen=True)
class CyclotronOverview:
    """Every output of a cyclotron at once, with the worst one named.

    ``worst`` is the output with the lowest measured alignment accuracy (None
    until one is measured). ``limiting`` is the output that decided the last
    review-rate window check (None before the first check, or with one output).
    """

    cyclotron_id: str
    review_rate: ReviewRate
    outputs: tuple[CyclotronStatus, ...]
    worst: str | None
    limiting: str | None

    def to_json(self) -> dict[str, Any]:
        statuses = [status.to_json() for status in self.outputs]
        return {
            "cyclotron": self.cyclotron_id,
            "worstOutput": self.worst,
            "limitingOutput": self.limiting,
            "reviewRate": statuses[0]["reviewRate"] if statuses else None,
            "outputs": statuses,
        }


def worst_output(statuses) -> str | None:
    measured = [status for status in statuses if status.alignment.accuracy is not None]
    return min(measured, key=lambda status: status.alignment.accuracy).classifier if measured else None


def load_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def all_events(wheel) -> list[dict]:
    """Every durable event of a DecisionFlywheel, oldest first."""
    events, cursor = [], 0
    while True:
        page = wheel.trace_events(after_event_id=cursor, limit=1000)
        events.extend(page["events"])
        if len(page["events"]) < 1000:
            return events
        cursor = page["cursor"]


STRUCTURE = (
    ("rubric", "rubric"),
    ("example_ids", "example list"),
    ("tasks", "supporting questions"),
    ("dynamic_elements", "dynamic inputs"),
)


def _plain(value):
    """Compare snapshots after JSON normalisation (tuples and lists alike)."""
    return json.loads(json.dumps(value, sort_keys=True, default=list)) if value is not None else None


class VersionTracker:
    """Follow one classifier's versions through its events.

    A version is a change of language or structure: rubric, example list,
    supporting questions or dynamic inputs. An ML model refit activates a new
    fingerprint but is not a version; it is counted in ``refits`` and reported
    as a ``refit`` change with its label count.
    """

    def __init__(self, initial_snapshot: Mapping[str, Any] | None = None):
        self.version = 1
        self.refits = 0
        self.last_change: LastChange | None = None
        self.snapshot = _plain(initial_snapshot)

    def to_json(self) -> dict[str, Any]:
        """Everything ``feed`` has learned, so a reopened engine resumes here instead of replaying history."""
        return {
            "version": self.version,
            "refits": self.refits,
            "last_change": asdict(self.last_change) if self.last_change is not None else None,
            "snapshot": self.snapshot,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> VersionTracker:
        tracker = cls()
        tracker.version, tracker.refits, tracker.snapshot = value["version"], value["refits"], value["snapshot"]
        tracker.last_change = LastChange(**value["last_change"]) if value["last_change"] is not None else None
        return tracker

    def feed(self, event: Mapping[str, Any]) -> LastChange | None:
        """Update from one event; return the change it caused, if any."""
        kind = event.get("kind")
        if kind == "cycle-started" and self.snapshot is None:
            self.snapshot = _plain(event.get("classifier_snapshot"))
        elif kind == "classifier-activated":
            current = _plain(event.get("classifier_snapshot")) or {}
            before = (self.snapshot or {}).get("config") or {}
            after = current.get("config") or {}
            changed = [name for key, name in STRUCTURE if before.get(key) != after.get(key)]
            if changed:
                self.last_change = LastChange(
                    "promoted",
                    self.version,
                    self.version + 1,
                    event.get("created_at"),
                    event.get("change_summary") or "Changed the " + ", ".join(changed) + ".",
                )
                self.version, self.refits = self.version + 1, 0
            else:
                head = current.get("head") or {}
                count = len(((head.get("provenance") or {}).get("training_ids")) or ())
                self.refits += 1
                self.last_change = LastChange(
                    "refit",
                    self.version,
                    self.version,
                    event.get("created_at"),
                    f"Refit the ML model on {count} labels.",
                    labels=count,
                )
            self.snapshot = current
            return self.last_change
        elif kind == "candidate-rejected":
            self.last_change = LastChange(
                "dropped", self.version, self.version, event.get("created_at"), str(event.get("reason") or "Candidate dropped.")
            )
            return self.last_change
        return None


class FoldRows:
    """A status fold's per-item rows and its review-order index.

    ``row/<item>`` holds what the fold knows of one item (the prediction shown, whether it awaits review, its
    latest review's id, the counted review); ``order/<n>`` names the item whose review was given number ``n``.
    Held in memory by default. With ``load``, rows not yet seen are read on demand from a keyed store (one
    key at a time, never a scan), and ``changes()`` lists what the fold changed, for saving with it.
    """

    def __init__(self, load=None):
        self.load = load
        self.values: dict[str, Any] = {}
        self.dirty: set[str] = set()

    def _get(self, key):
        if key not in self.values:
            text = self.load(key) if self.load is not None else None
            self.values[key] = json.loads(text) if text is not None else None
        return self.values[key]

    def _put(self, key, value):
        self.values[key] = value
        self.dirty.add(key)

    def row(self, item_id: str) -> dict:
        return dict(self._get("row/" + item_id) or {})

    def put_row(self, item_id: str, row: dict) -> None:
        if row != (self._get("row/" + item_id) or {}):
            self._put("row/" + item_id, row or None)

    def reviewed(self, number: int) -> str | None:
        return self._get(f"order/{number}")

    def put_reviewed(self, number: int, item_id: str | None) -> None:
        self._put(f"order/{number}", item_id)

    def changes(self) -> dict[str, str | None]:
        return {
            key: None if self.values[key] is None else json.dumps(self.values[key], sort_keys=True, separators=(",", ":"))
            for key in sorted(self.dirty)
        }

    def present(self) -> dict[str, Any]:
        return {key: value for key, value in sorted(self.values.items()) if value is not None}


class StatusFold:
    """Everything a status snapshot needs from a classifier's events, folded one event at a time.

    A saved fold resumes where it stopped, so a snapshot reads only the events since the last one. Per-item
    state lives in ``rows`` (``FoldRows``), so a fold kept in a keyed store reads and writes only the items an
    event names. The fold itself keeps counts and the most recent counted reviews (``recent``, oldest first),
    which is all an alignment window reads; an older review is fetched from the order index only when a
    retraction or a correction pulls it into the window.
    """

    def __init__(self, labels: tuple[str, ...], initial_snapshot: Mapping[str, Any] | None = None, rows: FoldRows | None = None):
        self.labels = tuple(labels)
        self.versions = VersionTracker(initial_snapshot)
        self.rows = rows if rows is not None else FoldRows()
        self.awaiting = 0  # items whose shown prediction awaits a review
        self.stale_since: str | None = None
        self.reviewed = 0  # numbers given to counted reviews so far
        self.counted = 0  # counted reviews that still stand
        self.recent: list[list] = []  # [number, item, label, predicted, probabilities]: the latest that stand

    def _waiting(self, row, waiting):
        if waiting and not row.get("waiting"):
            row["waiting"] = True
            self.awaiting += 1
        elif not waiting and row.get("waiting"):
            del row["waiting"]
            self.awaiting -= 1

    def _drop_review(self, row):
        number = row.pop("review")[4]
        self.rows.put_reviewed(number, None)
        self.counted -= 1
        self.recent = [entry for entry in self.recent if entry[0] != number]

    def feed(self, event: Mapping[str, Any]) -> None:
        kind = event.get("kind")
        self.versions.feed(event)
        if kind == "promoted":
            self.stale_since = None
        elif kind == "human-feedback" and event.get("action") != "retracted" and self.stale_since is None:
            self.stale_since = event.get("created_at")
        if kind == "prediction":
            item_id = str(event["target_id"])
            row = self.rows.row(item_id)
            row["shown"] = [event["label"], event.get("probabilities")]
            self._waiting(row, True)
            self.rows.put_row(item_id, row)
        elif kind == "human-feedback":
            feedback = event["feedback"]
            item_id = str(feedback.get("item_id", feedback.get("id")))
            row = self.rows.row(item_id)
            self._feedback(event, feedback, row)
            self.rows.put_row(item_id, row)
        elif kind in ("human-skipped", "review-not-requested"):
            item_id = str(event.get("target_id"))
            row = self.rows.row(item_id)
            self._waiting(row, False)
            self.rows.put_row(item_id, row)

    def _feedback(self, event, feedback, row):
        identifier = feedback.get("id")
        if event.get("action") == "retracted":
            if "latest" in row and row["latest"] == identifier:
                del row["latest"]
                if "shown" in row:
                    self._waiting(row, True)
            if "review" in row and row["review"][3] == identifier:
                self._drop_review(row)
            return
        self._waiting(row, False)
        row["latest"] = identifier
        if event.get("selected_by", "program") == "reviewer":
            return
        label = feedback.get("final_answer_value")
        prediction = row.get("shown")
        predicted = prediction[0] if prediction else feedback.get("initial_answer_value")
        if label not in self.labels or predicted not in self.labels:
            return
        probabilities = prediction[1] if prediction else None
        if probabilities is not None and set(probabilities) != set(self.labels):
            probabilities = None
        if "review" in row:
            self._drop_review(row)
        self.reviewed += 1
        self.counted += 1
        row["review"] = [label, predicted, probabilities, identifier, self.reviewed]
        item_id = str(feedback.get("item_id", feedback.get("id")))
        self.rows.put_reviewed(self.reviewed, item_id)
        self.recent.append([self.reviewed, item_id, label, predicted, probabilities])

    def window(self, size: int) -> list[tuple]:
        """The latest ``size`` counted reviews, oldest first, as (item, label, predicted, probabilities)."""
        need = min(size, self.counted)
        number = self.recent[0][0] - 1 if self.recent else self.reviewed
        while len(self.recent) < need and number > 0:
            item_id = self.rows.reviewed(number)
            review = self.rows.row(item_id).get("review") if item_id is not None else None
            if review is not None and review[4] == number:
                self.recent.insert(0, [number, item_id, *review[:3]])
            number -= 1
        return [tuple(entry[1:]) for entry in self.recent[-size:]]

    def summary(self) -> dict[str, Any]:
        """The counts the fold keeps beside its rows (everything but versions, recent reviews and rows)."""
        return {
            "labels": list(self.labels),
            "awaiting": self.awaiting,
            "stale_since": self.stale_since,
            "reviewed": self.reviewed,
            "counted": self.counted,
        }

    def to_json(self) -> dict[str, Any]:
        """The whole fold, rows included, for a store that keeps it as one value."""
        return {
            "layout": 2,
            **self.summary(),
            "versions": self.versions.to_json(),
            "recent": self.recent,
            "rows": self.rows.present(),
        }

    @classmethod
    def restore(cls, summary, versions, recent, rows: FoldRows) -> StatusFold:
        fold = cls(tuple(summary["labels"]), rows=rows)
        fold.versions = VersionTracker.from_json(versions)
        fold.awaiting, fold.stale_since = summary["awaiting"], summary["stale_since"]
        fold.reviewed, fold.counted = summary["reviewed"], summary["counted"]
        fold.recent = [list(entry) for entry in recent]
        return fold

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> StatusFold:
        if value.get("layout") == 2:
            rows = FoldRows()
            rows.values = {key: row for key, row in value["rows"].items()}
            return cls.restore(value, value["versions"], value["recent"], rows)
        return cls._from_whole_maps(value)

    @classmethod
    def _from_whole_maps(cls, value: Mapping[str, Any]) -> StatusFold:
        """Read a fold saved before rows existed: whole maps of shown predictions, reviews and awaiting items."""
        fold = cls(tuple(value["labels"]))
        fold.versions = VersionTracker.from_json(value["versions"])
        fold.stale_since = value["stale_since"]
        shown, reviews, latest, awaiting = value["shown"], value["reviews"], value["latest_review"], set(value["awaiting"])
        for item_id in sorted(set(shown) | set(reviews) | set(latest) | awaiting):
            row = {}
            if item_id in shown:
                row["shown"] = shown[item_id]
            if item_id in awaiting:
                row["waiting"] = True
            if item_id in latest:
                row["latest"] = latest[item_id]
            fold.rows.values["row/" + item_id] = row
        for number, item_id in enumerate(value["order"], 1):
            fold.rows.values["row/" + item_id]["review"] = [*reviews[item_id], number]
            fold.rows.values[f"order/{number}"] = item_id
            fold.recent.append([number, item_id, *reviews[item_id][:3]])
        fold.rows.dirty = set(fold.rows.values)
        fold.reviewed = fold.counted = len(value["order"])
        fold.awaiting = len(awaiting)
        return fold

    def status(
        self,
        *,
        cyclotron_id: str,
        classifier: str,
        fingerprint: str,
        positive_label: str | None = None,
        window: int = 200,
        review_rate: ReviewRate | None = None,
        now: datetime | None = None,
    ) -> CyclotronStatus:
        valid = type(window) is int and window >= 1
        # An invalid window is refused by the metrics, exactly as when every review was kept in memory.
        records = self.window(window) if valid else [tuple(entry[1:]) for entry in self.recent]
        metrics = recent_reviewed_metrics(self.labels, records, limit=window) if records else None
        return CyclotronStatus(
            cyclotron_id,
            classifier,
            self.versions.version,
            fingerprint,
            (now or datetime.now(timezone.utc)).isoformat(),
            _alignment(metrics, window, positive_label),
            _calibration(metrics),
            review_rate or FULL_REVIEW,
            self.versions.last_change,
            Pending(self.awaiting, self.stale_since),
            refits=self.versions.refits,
        )


def _positive(labels, positive_label):
    if positive_label is None and len(labels) == 2:
        positive_label = labels[0]
    if positive_label is not None and positive_label not in labels:
        raise ValueError("positive label must be one of the classifier labels")
    return positive_label


def status_from_events(
    events: Iterable[Mapping[str, Any]],
    *,
    cyclotron_id: str,
    classifier: str,
    labels: tuple[str, ...],
    fingerprint: str,
    positive_label: str | None = None,
    window: int = 200,
    review_rate: ReviewRate | None = None,
    now: datetime | None = None,
    initial_snapshot: Mapping[str, Any] | None = None,
) -> CyclotronStatus:
    """Build the snapshot from flywheel events; no model calls, no hidden state."""
    labels = tuple(labels)
    positive_label = _positive(labels, positive_label)
    fold = StatusFold(labels, initial_snapshot)
    for event in events:
        fold.feed(event)
    return fold.status(
        cyclotron_id=cyclotron_id,
        classifier=classifier,
        fingerprint=fingerprint,
        positive_label=positive_label,
        window=window,
        review_rate=review_rate,
        now=now,
    )


STATUS_FOLD_KEY = "status-fold"
# A runtime storage with keyed state (``save_derived``) keeps the fold in parts, so a decision rewrites only
# what changed: the summary and cursor here, the version tracker and recent reviews in their own state values,
# and each row as its own ``status`` keyed document under the fold's origin.
STATUS_VERSIONS_KEY = "status-fold-versions"
STATUS_RECENT_KEY = "status-fold-recent"
STATUS_ROWS = "status"


def _compact(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def status_from_flywheel(
    wheel,
    *,
    cyclotron_id: str,
    positive_label: str | None = None,
    window: int = 200,
    review_rate: ReviewRate | None = None,
    now: datetime | None = None,
) -> CyclotronStatus:
    """Snapshot one running classifier (a DecisionFlywheel) of a cyclotron.

    The fold is kept in the classifier's runtime state with the event it has reached, so a snapshot reads
    only newer events. It resumes only from the same labels and initial snapshot; otherwise it starts over.
    """
    task = wheel.initial.task
    labels = tuple(task.labels)
    positive_label = _positive(labels, positive_label)
    start = initial_snapshot(wheel)
    origin = hashlib.sha256(json.dumps([list(labels), _plain(start)], sort_keys=True).encode()).hexdigest()
    storage = wheel.runtime_storage
    keyed = callable(getattr(storage, "save_derived", None))
    prefix = origin[:16] + "/"
    rows = FoldRows((lambda key: storage.keyed(STATUS_ROWS, prefix + key)) if keyed else None)
    saved = storage.state(STATUS_FOLD_KEY)
    saved = json.loads(saved) if saved is not None else None
    changed = False
    if saved is not None and saved["origin"] == origin:
        cursor = saved["cursor"]
        if "fold" in saved:
            fold = StatusFold.from_json(saved["fold"])
            changed = keyed  # a fold saved whole moves into rows the first time
            if keyed:
                rows.values, rows.dirty = fold.rows.values, set(fold.rows.values)
                fold.rows = rows
        else:
            versions = json.loads(storage.state(STATUS_VERSIONS_KEY))
            recent = json.loads(storage.state(STATUS_RECENT_KEY))
            fold = StatusFold.restore(saved, versions, recent, rows)
    else:
        fold, cursor = StatusFold(labels, start, rows=rows), 0
    reached = cursor
    while True:
        page = wheel.trace_events(after_event_id=reached, limit=1000)
        for event in page["events"]:
            fold.feed(event)
        if not page["events"]:
            break
        reached = page["cursor"]
    status = fold.status(
        cyclotron_id=cyclotron_id,
        classifier=task.name,
        fingerprint=wheel.active.fingerprint,
        positive_label=positive_label,
        window=window,
        review_rate=review_rate,
        now=now,
    )
    if reached != cursor or changed:
        if keyed:
            fold.recent = fold.recent[-window:]  # the reporting window; older reviews stay in the order index
            storage.save_derived(
                {
                    STATUS_FOLD_KEY: _compact({"origin": origin, "cursor": reached, **fold.summary()}),
                    STATUS_VERSIONS_KEY: _compact(fold.versions.to_json()),
                    STATUS_RECENT_KEY: _compact(fold.recent),
                },
                {STATUS_ROWS: {prefix + key: text for key, text in fold.rows.changes().items()}},
            )
        else:
            storage.put_state(STATUS_FOLD_KEY, _compact({"origin": origin, "cursor": reached, "fold": fold.to_json()}))
    return status


def initial_snapshot(wheel) -> dict:
    """The classifier snapshot before any activation: the configuration it was created with."""
    from dataclasses import asdict

    from .flywheel import FittedClassifier

    return asdict(FittedClassifier(getattr(wheel, "version_baseline", wheel.initial)))


def _alignment(metrics, window, positive_label) -> Alignment:
    if not metrics:
        return Alignment(window, 0, None, None, None, positive_label)
    if positive_label is not None:
        group = metrics["per_class"][positive_label]
        precision, recall = group["precision"], group["recall"]
    else:
        groups = list(metrics["per_class"].values())
        precisions = [g["precision"] for g in groups if g["precision"] is not None]
        recalls = [g["recall"] for g in groups if g["recall"] is not None]
        precision = sum(precisions) / len(precisions) if precisions else None
        recall = sum(recalls) / len(recalls) if recalls else None
    return Alignment(window, metrics["count"], metrics["accuracy"], precision, recall, positive_label)


def _calibration(metrics) -> Calibration:
    curve = metrics["calibration"] if metrics else None
    if not curve or not curve["count"]:
        return Calibration(None, None, None, ())
    bins = tuple(CalibrationBin(b["lower"], b["count"], b["mean_confidence"], b["accuracy"]) for b in curve["bins"] if b["count"])
    total = sum(b.count for b in bins)
    says_sure = sum(b.confidence * b.count for b in bins) / total
    is_right = sum(b.accuracy * b.count for b in bins) / total
    return Calibration(says_sure, is_right, round(abs(says_sure - is_right) * 100), bins)

"""The cyclotron-status/v1 snapshot: what one classifier is doing right now.

Applications render this snapshot instead of parsing trace events. It is built
from durable flywheel events only and never calls a model. Alignment numbers
use reviews the cyclotron selected; reviews a reviewer chose for themselves
(``selected_by == "reviewer"``) are excluded because they are not a random
or policy-selected sample of decisions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from .rolling_metrics import recent_reviewed_metrics

SCHEMA = "cyclotron-status/v1"
SCHEMA_PATH = Path(__file__).with_name("schemas") / "cyclotron-status.v1.schema.json"
REVIEW_RATE_STATES = ("full", "onboarding", "tapering", "steady", "raised", "manual")
CHANGE_KINDS = ("promoted", "dropped", "definition")
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
    next_check_after_labels: int | None = None
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
    schema: str = field(default=SCHEMA)

    def to_json(self) -> dict[str, Any]:
        """The wire shape described by cyclotron-status.v1.schema.json."""
        a, c, r, p = self.alignment, self.calibration, self.review_rate, self.pending
        return {
            "schema": self.schema,
            "cyclotron": {"id": self.cyclotron_id, "classifier": self.classifier,
                          "version": self.version, "fingerprint": self.fingerprint},
            "asOf": self.as_of,
            "alignment": {"window": a.window, "labels": a.labels, "accuracy": a.accuracy,
                          "precision": a.precision, "recall": a.recall,
                          "positiveLabel": a.positive_label, "measuredOn": a.measured_on},
            "calibration": {"saysSure": c.says_sure, "isRight": c.is_right, "gapPoints": c.gap_points,
                            "curve": [{"bin": b.bin, "count": b.count, "confidence": b.confidence,
                                       "accuracy": b.accuracy} for b in c.curve]},
            "reviewRate": {"state": r.state, "rate": r.rate, "reason": r.reason,
                           "auditFloor": r.audit_floor, "confidenceThreshold": r.confidence_threshold,
                           "override": None if r.override is None else {
                               "rate": r.override.rate, "expiresAt": r.override.expires_at,
                               "setBy": r.override.set_by, "setAt": r.override.set_at},
                           "nextCheckAfterLabels": r.next_check_after_labels,
                           "expectedReviewsPerWeek": r.expected_reviews_per_week},
            "lastChange": None if self.last_change is None else {
                "kind": self.last_change.kind, "fromVersion": self.last_change.from_version,
                "toVersion": self.last_change.to_version, "at": self.last_change.at,
                "summary": self.last_change.summary},
            "pending": {"decisionsAwaitingReview": p.decisions_awaiting_review, "staleSince": p.stale_since},
        }


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


def note_version(numbers: dict[str, int], event: Mapping[str, Any]) -> None:
    """Number versions in the order this classifier decided with or activated them.

    Every activation (a promoted candidate, a refined rubric, or a refit ML
    model) is a new version; reactivating a known fingerprint keeps its number.
    """
    kind = event.get("kind")
    version = (event.get("version") if kind == "prediction"
               else event.get("classifier_version") if kind == "classifier-activated" else None)
    if version and version not in numbers:
        numbers[version] = len(numbers) + 1


def describe_activation(previous: Mapping[str, Any] | None, current: Mapping[str, Any]) -> str:
    """Say what an activation changed, from the two classifier snapshots."""
    before = (previous or {}).get("config") or {}
    after = current.get("config") or {}
    changes = [name for key, name in (("rubric", "rubric"), ("example_ids", "example list"),
                                      ("tasks", "classifier questions"), ("dynamic_elements", "dynamic inputs"))
               if before.get(key) != after.get(key)]
    if changes:
        return "Changed the " + ", ".join(changes) + "."
    head = current.get("head") or {}
    count = len(((head.get("provenance") or {}).get("training_ids")) or ())
    return f"Refit the ML model on {count} labels." if head else "Activated a new version."


def status_from_events(events: Iterable[Mapping[str, Any]], *, cyclotron_id: str, classifier: str,
                       labels: tuple[str, ...], fingerprint: str, positive_label: str | None = None,
                       window: int = 200, review_rate: ReviewRate | None = None,
                       now: datetime | None = None) -> CyclotronStatus:
    """Build the snapshot from flywheel events; no model calls, no hidden state."""
    labels = tuple(labels)
    if positive_label is None and len(labels) == 2:
        positive_label = labels[0]
    if positive_label is not None and positive_label not in labels:
        raise ValueError("positive label must be one of the classifier labels")
    shown: dict[str, Mapping[str, Any]] = {}
    reviews: dict[str, tuple[str, str, dict | None, str | None]] = {}
    order: list[str] = []
    version_numbers: dict[str, int] = {}
    last_change = None
    awaiting: set[str] = set()
    active_version: int | None = None
    snapshot: Mapping[str, Any] | None = None
    latest_review: dict[str, str | None] = {}
    for event in events:
        kind = event.get("kind")
        if kind == "classifier-activated" and not version_numbers:
            version_numbers["initial"], active_version = 1, 1  # the version before the first activation
        note_version(version_numbers, event)
        if kind == "prediction":
            if active_version is None:
                active_version = version_numbers.get(event.get("version"))
            item_id = str(event["target_id"])
            shown[item_id] = event
            awaiting.add(item_id)
        elif kind == "human-feedback":
            feedback = event["feedback"]
            item_id = str(feedback.get("item_id", feedback.get("id")))
            if event.get("action") == "retracted":
                if latest_review.get(item_id) == feedback.get("id"):
                    latest_review.pop(item_id)
                    if item_id in shown:
                        awaiting.add(item_id)
                if item_id in reviews and reviews[item_id][3] == feedback.get("id"):
                    reviews.pop(item_id)
                    order.remove(item_id)
                continue
            awaiting.discard(item_id)
            latest_review[item_id] = feedback.get("id")
            if event.get("selected_by", "program") == "reviewer":
                continue
            label = feedback.get("final_answer_value")
            prediction = shown.get(item_id)
            predicted = prediction["label"] if prediction else feedback.get("initial_answer_value")
            if label not in labels or predicted not in labels:
                continue
            probabilities = prediction.get("probabilities") if prediction else None
            if probabilities is not None and set(probabilities) != set(labels):
                probabilities = None
            if item_id in reviews:
                order.remove(item_id)
            reviews[item_id] = (label, predicted, probabilities, feedback.get("id"))
            order.append(item_id)
        elif kind == "human-skipped":
            awaiting.discard(str(event.get("target_id")))
        elif kind == "classifier-activated":
            to_version = version_numbers[event["classifier_version"]]
            last_change = LastChange("promoted", active_version, to_version, event.get("created_at"),
                                     describe_activation(snapshot, event.get("classifier_snapshot") or {}))
            active_version, snapshot = to_version, event.get("classifier_snapshot") or {}
        elif kind == "candidate-rejected":
            last_change = LastChange("dropped", active_version, active_version or 1, event.get("created_at"),
                                     str(event.get("reason") or "Candidate dropped."))
        elif kind == "cycle-started" and snapshot is None:
            snapshot = event.get("classifier_snapshot")
    if fingerprint not in version_numbers:
        version_numbers[fingerprint] = len(version_numbers) + 1
    records = [(item_id, *reviews[item_id][:3]) for item_id in order]
    metrics = recent_reviewed_metrics(labels, records, limit=window) if records else None
    alignment = _alignment(metrics, window, positive_label)
    calibration = _calibration(metrics)
    stale_since = _stale_since(events)
    return CyclotronStatus(cyclotron_id, classifier, version_numbers[fingerprint], fingerprint,
                           (now or datetime.now(timezone.utc)).isoformat(), alignment, calibration,
                           review_rate or FULL_REVIEW, last_change, Pending(len(awaiting), stale_since))


def status_from_flywheel(wheel, *, cyclotron_id: str, positive_label: str | None = None, window: int = 200,
                         review_rate: ReviewRate | None = None, now: datetime | None = None) -> CyclotronStatus:
    """Snapshot one running classifier (a DecisionFlywheel) of a cyclotron."""
    task = wheel.initial.task
    return status_from_events(all_events(wheel), cyclotron_id=cyclotron_id, classifier=task.name,
                              labels=tuple(task.labels), fingerprint=wheel.active.fingerprint,
                              positive_label=positive_label, window=window, review_rate=review_rate, now=now)


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
    bins = tuple(CalibrationBin(b["lower"], b["count"], b["mean_confidence"], b["accuracy"])
                 for b in curve["bins"] if b["count"])
    total = sum(b.count for b in bins)
    says_sure = sum(b.confidence * b.count for b in bins) / total
    is_right = sum(b.accuracy * b.count for b in bins) / total
    return Calibration(says_sure, is_right, round(abs(says_sure - is_right) * 100), bins)


def _stale_since(events) -> str | None:
    """When the earliest review newer than the active version arrived."""
    earliest = None
    for event in events:
        if event.get("kind") == "promoted":
            earliest = None
        elif (event.get("kind") == "human-feedback" and event.get("action") != "retracted"
              and earliest is None):
            earliest = event.get("created_at")
    return earliest

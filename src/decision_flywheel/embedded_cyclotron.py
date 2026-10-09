"""Embed one cyclotron in an application: decide, review, status, subscribe.

An application keys everything by its own item identities. The cyclotron owns
the rest: item partitions, held-out reviews, selection propensities, versions,
and the learning loop. Learning runs inside ``DecisionFlywheel`` through the
same ``learning_loop`` functions the web workspace uses.

    cyclotron = Cyclotron.open("var/cyclotrons/relevance", definition, model, optimizer)
    decision = await cyclotron.decide(Item("ref-123", {"text": "..."}))
    await cyclotron.review(decision.decision_id, "include", explanation="On our beat.")
    snapshot = cyclotron.status().to_json()
    page = cyclotron.subscribe(after=0)

One call runs at a time per open cyclotron. One process may open a store at
a time; a lease for several workers is a separate concern.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Mapping, Sequence
from uuid import uuid4

from .calibration_history import reviewed_prediction_bindings
from .classifier_config import ClassifierConfig
from .cyclotron_status import CyclotronStatus, ReviewRate, describe_activation, note_version, status_from_flywheel
from .feedback import LABEL_SOURCE_VETTED, FeedbackItem
from .flywheel import DecisionFlywheel
from .learning_loop import (REVIEWER_SELECTED, decide_with_shared_context, feedback_partitions,
                            learn_from_review, record_cycle_metrics, review_role)
from .models import DecisionTask, Item
from .selection_policy import SelectionPolicy
from .shared_decisions import SharedDecisions

SELECTED_BY = ("program", "reviewer")
FULL_REVIEW_REASON = "Every decision is reviewed until a review-rate program is configured."


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ClassifierSpec:
    """One decision question inside the cyclotron."""
    id: str
    labels: tuple[str, ...]
    question: str
    positive_label: str | None = None
    input_field: str = "text"

    def __post_init__(self):
        object.__setattr__(self, "labels", tuple(self.labels))
        task = self.task
        if self.positive_label is not None:
            task.validate_label(self.positive_label)

    @property
    def task(self) -> DecisionTask:
        return DecisionTask(self.id, self.labels, self.question, self.input_field)

    @property
    def selection_policy(self) -> SelectionPolicy:
        if self.positive_label:
            return SelectionPolicy("f1", positive_class=self.positive_label)
        return SelectionPolicy("f1", aggregation="macro")


@dataclass(frozen=True)
class CyclotronDefinition:
    """The application's cyclotron: an identity, its classifiers, and learning cadence."""
    id: str
    classifiers: tuple[ClassifierSpec, ...]
    seed: str = "cyclotron-v1"
    optimize_every: int = 20
    rubric_changes_every: int = 2

    def __post_init__(self):
        object.__setattr__(self, "classifiers", tuple(self.classifiers))
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("cyclotron id must be a non-empty string")
        ids = [spec.id for spec in self.classifiers]
        if not ids or len(set(ids)) != len(ids):
            raise ValueError("a cyclotron needs distinct classifiers")
        if not isinstance(self.seed, str) or not self.seed:
            raise ValueError("seed must be a non-empty string")
        for value in (self.optimize_every, self.rubric_changes_every):
            if type(value) is not int or value < 1:
                raise ValueError("learning cadences must be positive integers")

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(_json(asdict(self)).encode()).hexdigest()


@dataclass(frozen=True)
class ReviewSelection:
    """Whether a decision goes to a reviewer, why, and with what probability."""
    selected: bool
    reason: str
    propensity: float


@dataclass(frozen=True)
class ClassifierDecision:
    classifier: str
    label: str
    confidence: float | None
    probabilities: Mapping[str, float] | None
    version: int
    fingerprint: str


@dataclass(frozen=True)
class Decision:
    """The cyclotron's decision about one item, with one result per classifier."""
    decision_id: str
    item_id: str
    created_at: str
    results: Mapping[str, ClassifierDecision]
    review: ReviewSelection

    def _only(self) -> ClassifierDecision:
        if len(self.results) != 1:
            raise ValueError("this cyclotron has several classifiers; read decision.results")
        return next(iter(self.results.values()))

    @property
    def label(self) -> str:
        return self._only().label

    @property
    def confidence(self) -> float | None:
        return self._only().confidence

    @property
    def probabilities(self) -> Mapping[str, float] | None:
        return self._only().probabilities

    @property
    def version(self) -> int:
        return self._only().version

    def to_json(self) -> dict[str, Any]:
        return {"decisionId": self.decision_id, "itemId": self.item_id, "createdAt": self.created_at,
                "classifiers": {cid: {"label": r.label, "confidence": r.confidence,
                                      "probabilities": dict(r.probabilities) if r.probabilities else None,
                                      "version": r.version, "fingerprint": r.fingerprint}
                                for cid, r in self.results.items()},
                "review": {"selected": self.review.selected, "reason": self.review.reason,
                           "propensity": self.review.propensity}}

    @classmethod
    def from_json(cls, value) -> "Decision":
        return cls(value["decisionId"], value["itemId"], value["createdAt"],
                   {cid: ClassifierDecision(cid, r["label"], r["confidence"], r["probabilities"], r["version"],
                                            r["fingerprint"]) for cid, r in value["classifiers"].items()},
                   ReviewSelection(**value["review"]))


@dataclass(frozen=True)
class Review:
    """One recorded review: a label, a correction, an undo, or a review without a label."""
    review_id: str
    decision_id: str
    item_id: str
    classifier: str
    kind: str
    label: str | None
    explanation: str | None = None
    reason_code: str | None = None
    reviewer: str | None = None
    selected_by: str = "program"
    created_at: str = field(default_factory=_now)
    optimization_warnings: tuple = ()

    def to_json(self) -> dict[str, Any]:
        return {"reviewId": self.review_id, "decisionId": self.decision_id, "itemId": self.item_id,
                "classifier": self.classifier, "kind": self.kind, "label": self.label,
                "explanation": self.explanation, "reasonCode": self.reason_code, "reviewer": self.reviewer,
                "selectedBy": self.selected_by, "createdAt": self.created_at}


class Cyclotron:
    """An embedded cyclotron backed by a directory of SQLite files."""

    def __init__(self, directory, definition: CyclotronDefinition, model, optimizer=None, *,
                 max_requests: int = 100, redact: Sequence[str] = (), observer: Callable[[dict], None] | None = None):
        if not isinstance(definition, CyclotronDefinition):
            raise ValueError("definition must be a CyclotronDefinition")
        if type(max_requests) is not int or max_requests < 1:
            raise ValueError("max_requests must be a positive integer")
        self.definition, self.optimizer = definition, optimizer
        self.observer = observer or (lambda event: None)
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.directory / "cyclotron.sqlite3")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS items (id TEXT PRIMARY KEY, item_values TEXT NOT NULL, fingerprint TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS decisions (id TEXT PRIMARY KEY, item_id TEXT NOT NULL, item_fingerprint TEXT NOT NULL,
                state TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS decisions_by_item ON decisions(item_id);
            CREATE TABLE IF NOT EXISTS reviews (seq INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, decision_id TEXT NOT NULL,
                classifier TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
        """)
        saved = self.db.execute("SELECT value FROM meta WHERE key='definition'").fetchone()
        if saved and json.loads(saved[0])["fingerprint"] != definition.fingerprint:
            self.db.close()
            raise ValueError("this store belongs to a different cyclotron definition")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO meta VALUES ('definition', ?)",
                            (_json({"fingerprint": definition.fingerprint, "definition": asdict(definition)}),))
        self.shared = SharedDecisions(self.directory / "shared.sqlite3", model, max_requests=0,
                                      observer=lambda event: None)
        # The shared ceiling counts every attempt in the store's lifetime; this
        # open authorizes max_requests more.
        self.shared.max_requests = self.shared.requests + max_requests
        self.specs = {spec.id: spec for spec in definition.classifiers}
        self.wheels: dict[str, DecisionFlywheel] = {}
        self._versions: dict[str, dict[str, int]] = {}
        self._version_cursor: dict[str, int] = {}
        self._snapshots: dict[str, Mapping[str, Any]] = {}
        (self.directory / "classifiers").mkdir(exist_ok=True)
        try:
            for spec in definition.classifiers:
                path = self.directory / "classifiers" / (hashlib.sha256(spec.id.encode()).hexdigest() + ".sqlite3")
                self.wheels[spec.id] = DecisionFlywheel(
                    path, ClassifierConfig(spec.task), self.shared.adapter(spec.id), optimizer,
                    max_requests=max_requests, redact=redact, selection_policy=spec.selection_policy,
                    min_evaluation_per_class=2, observer=lambda event, cid=spec.id: self._observe(cid, event))
                self._snapshots[spec.id] = json.loads(_json(asdict(self.wheels[spec.id].active)))
        except Exception:
            self.close()
            raise
        self._lock = asyncio.Lock()

    @classmethod
    def open(cls, directory, definition: CyclotronDefinition, model, optimizer=None, **options) -> "Cyclotron":
        """Open or create the cyclotron store at ``directory``. No model call is made."""
        return cls(directory, definition, model, optimizer, **options)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self) -> None:
        for wheel in self.wheels.values():
            wheel.close()
        self.wheels = {}
        if getattr(self, "shared", None) is not None:
            self.shared.close()
            self.shared = None
        self.db.close()

    # Decide ---------------------------------------------------------------

    async def decide(self, item: Item) -> Decision:
        """Decide one item. An unchanged item keeps its decision; no model call is repeated."""
        async with self._lock:
            if not isinstance(item, Item):
                raise ValueError("decide needs an Item")
            for spec in self.definition.classifiers:
                spec.task.validate_target(item)
            fingerprint = hashlib.sha256(_json(dict(item.values)).encode()).hexdigest()
            latest = self._latest_decision(item.id)
            if latest is not None:
                state, decision = latest
                unchanged = decision.item_id == item.id and self._item_fingerprint(decision.decision_id) == fingerprint
                current = all(decision.results[cid].fingerprint == wheel.active.fingerprint
                              for cid, wheel in self.wheels.items())
                if unchanged and (state == "reviewed" or current):
                    return decision
                if state == "open":
                    self._close_waiting_cycles(item.id, decision.decision_id, "superseded by a new decision")
                with self.db:
                    self.db.execute("UPDATE decisions SET state='superseded' WHERE id=?", (decision.decision_id,))
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO items VALUES (?,?,?)",
                                (item.id, _json(dict(item.values)), fingerprint))
            results, _ = await decide_with_shared_context(self.wheels, self.shared, item, self._partitions,
                                                          now=datetime.now(timezone.utc))
            decision = Decision(str(uuid4()), item.id, _now(), {
                cid: ClassifierDecision(cid, result.label, result.confidence,
                                        dict(result.probabilities) if result.probabilities else None,
                                        self._version(cid, self.wheels[cid].active.fingerprint),
                                        self.wheels[cid].active.fingerprint)
                for cid, result in results.items()}, ReviewSelection(True, FULL_REVIEW_REASON, 1.0))
            with self.db:
                self.db.execute("INSERT INTO decisions VALUES (?,?,?,?,?)",
                                (decision.decision_id, item.id, fingerprint, "open", _json(decision.to_json())))
            self._record({"kind": "decision", "decision": decision.to_json()})
            return decision

    def decision(self, decision_id: str) -> Decision:
        row = self.db.execute("SELECT payload FROM decisions WHERE id=?", (decision_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown decision {decision_id}")
        return Decision.from_json(json.loads(row[0]))

    # Review ---------------------------------------------------------------

    async def review(self, decision_id: str, label: str | None, *, classifier: str | None = None,
                     explanation: str | None = None, reason_code: str | None = None, reviewer: str | None = None,
                     selected_by: str = "program", review_id: str | None = None) -> Review:
        """Record a reviewer's label for a decision.

        The first label for a decision teaches the cyclotron; a later label is
        a correction. ``label=None`` records a review without a label (for
        example a duplicate), which closes the decision without teaching.
        ``selected_by="reviewer"`` marks a review the reviewer chose to make:
        it trains the ML model but never counts as alignment or audit evidence.
        """
        async with self._lock:
            decision = self.decision(decision_id)
            cid = self._classifier(decision, classifier)
            wheel, spec = self.wheels[cid], self.specs[cid]
            if label is not None:
                label = spec.task.validate_label(label)
            if selected_by not in SELECTED_BY:
                raise ValueError(f"selected_by must be one of {SELECTED_BY}")
            for name, value in (("explanation", explanation), ("reason_code", reason_code), ("reviewer", reviewer)):
                if value is not None and not isinstance(value, str):
                    raise ValueError(f"{name} must be text")
            explanation = explanation.strip() or None if explanation else None
            review_id = review_id or str(uuid4())
            existing = self._review(review_id)
            if existing is not None:
                same = (existing.decision_id, existing.classifier, existing.label, existing.explanation,
                        existing.reason_code) == (decision_id, cid, label, explanation, reason_code)
                if not same:
                    raise ValueError("review identity has different content")
                return existing
            active = self._active_review(decision_id, cid)
            target = Item(decision.item_id, self._item_values()[decision.item_id])
            annotations = {"decision_id": decision_id, "review_id": review_id, "selected_by": selected_by,
                           "reason_code": reason_code, "reviewer": reviewer}
            warnings: list = []
            if label is None:
                if active is not None:
                    raise ValueError("this decision already has a label; undo it before closing it without one")
                cycle = wheel.resume_cycle(target)
                if cycle is not None:
                    wheel._emit({"kind": "human-skipped", "target_id": target.id, **annotations})
                    cycle.__exit__(None, None, None)
                kind = "no-label"
            elif active is None:
                cycle = wheel.resume_cycle(target) or wheel.cycle(target, reason="review").__enter__()
                try:
                    propensity = decision.review.propensity if selected_by == "program" else 1.0
                    wheel.record_feedback_event(FeedbackItem(
                        review_id, target.id, cid, initial_answer_value=decision.results[cid].label,
                        final_answer_value=label, edit_comment_value=explanation, label_source=LABEL_SOURCE_VETTED,
                        selection_propensity=propensity, review_provenance=f"{selected_by}-selected-review"),
                        assignment=REVIEWER_SELECTED if selected_by == "reviewer" else review_role(self.definition.seed, target.id),
                        annotations=annotations)
                    warnings = await self._learn(cid, cycle)
                    record_cycle_metrics(wheel, self._class_config(spec))
                    cycle.__exit__(None, None, None)
                except Exception as error:
                    cycle.__exit__(type(error), error, None)
                    raise
                kind = "label"
            else:
                original = self._feedback_event(wheel, active.review_id)
                prior = original["feedback"]
                with wheel.cycle(target, reason="human-correction"):
                    wheel.record_feedback_event(FeedbackItem(
                        review_id, target.id, cid, initial_answer_value=prior.get("initial_answer_value"),
                        final_answer_value=label, edit_comment_value=explanation, label_source=prior["label_source"],
                        selection_propensity=prior.get("selection_propensity"),
                        review_provenance=f"corrected-human-vote:{prior['id']}"),
                        assignment=original.get("assignment"),
                        annotations={**annotations, "selected_by": original.get("selected_by", selected_by)})
                    self._reconcile(cid)
                    record_cycle_metrics(wheel, self._class_config(spec))
                    wheel._emit({"kind": "feedback-correction-completed", "feedback_id": review_id,
                                 "target_id": target.id, "previous_feedback_id": prior["id"]})
                kind = "correction"
            result = Review(review_id, decision_id, target.id, cid, kind, label, explanation, reason_code, reviewer,
                            selected_by, optimization_warnings=tuple(warnings))
            self._store_review(result)
            return result

    async def undo_review(self, decision_id: str, *, classifier: str | None = None,
                          review_id: str | None = None) -> Review:
        """Retract the active label for a decision and wait for a new review."""
        async with self._lock:
            decision = self.decision(decision_id)
            cid = self._classifier(decision, classifier)
            wheel, spec = self.wheels[cid], self.specs[cid]
            review_id = review_id or str(uuid4())
            existing = self._review(review_id)
            if existing is not None:
                if existing.kind != "undo" or existing.decision_id != decision_id or existing.classifier != cid:
                    raise ValueError("review identity has different content")
                return existing
            active = self._active_review(decision_id, cid)
            if active is None or active.label is None:
                raise ValueError("this decision has no label to undo")
            original = self._feedback_event(wheel, active.review_id)
            target = Item(decision.item_id, self._item_values()[decision.item_id])
            shown = reviewed_prediction_bindings(wheel.history(100000)).get(original["feedback"]["id"])
            if shown is None:
                raise ValueError("the reviewed label has no recorded decision")
            with wheel.cycle(target, reason="human-retraction"):
                wheel.record_feedback_event(FeedbackItem(**original["feedback"]), action="retracted",
                                            assignment=original.get("assignment"),
                                            annotations={"decision_id": decision_id, "review_id": review_id})
                self._reconcile(cid)
                record_cycle_metrics(wheel, self._class_config(spec))
            # The decision shown before the review is still the one to review.
            cycle = wheel.cycle(target, reason="review-after-undo").__enter__()
            try:
                wheel._emit({"kind": "displayed-prediction-reused", "target_id": target.id,
                             "prediction_event_id": shown["event_id"], "reason": "review after undo"})
                cycle.suspend()
            except Exception as error:
                cycle.__exit__(type(error), error, None)
                raise
            result = Review(review_id, decision_id, target.id, cid, "undo", None)
            self._store_review(result)
            return result

    # Status and events ----------------------------------------------------

    def status(self, classifier: str | None = None, *, review_rate: ReviewRate | None = None) -> CyclotronStatus:
        """The cyclotron-status/v1 snapshot for one classifier. No model call."""
        if classifier is None:
            if len(self.specs) != 1:
                raise ValueError("name the classifier; this cyclotron has several")
            classifier = next(iter(self.specs))
        spec = self.specs[classifier]
        return status_from_flywheel(self.wheels[classifier], cyclotron_id=self.definition.id,
                                    positive_label=spec.positive_label, review_rate=review_rate)

    def subscribe(self, after: int = 0, limit: int = 100) -> dict[str, Any]:
        """Committed cyclotron events after a cursor, oldest first.

        Kinds: decision, review, promoted (a new active version), dropped. Pass the returned cursor
        back to continue; the cursor survives restarts.
        """
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("cursor must be nonnegative and page size between 1 and 1000")
        rows = self.db.execute("SELECT id,payload FROM events WHERE id>? ORDER BY id LIMIT ?", (after, limit)).fetchall()
        return {"events": [{**json.loads(payload), "cursor": event_id} for event_id, payload in rows],
                "cursor": rows[-1][0] if rows else after}

    # Internals ------------------------------------------------------------

    def _record(self, event: Mapping[str, Any]) -> dict:
        event = {"cyclotronId": self.definition.id, "at": _now(), **event}
        with self.db:
            row = self.db.execute("INSERT INTO events(payload) VALUES (?)", (_json(event),))
        event = {**event, "cursor": row.lastrowid}
        self.observer(event)
        return event

    def _observe(self, classifier: str, event: Mapping[str, Any]) -> None:
        kind = event.get("kind")
        if kind == "classifier-activated":
            previous = self._snapshots.get(classifier)
            current = event.get("classifier_snapshot") or {}
            self._snapshots[classifier] = current
            self._record({"kind": "promoted", "classifier": classifier,
                          "version": self._version(classifier, event["classifier_version"]),
                          "fingerprint": event["classifier_version"],
                          "summary": describe_activation(previous, current)})
        elif kind == "candidate-rejected":
            self._record({"kind": "dropped", "classifier": classifier,
                          "summary": str(event.get("reason") or "Candidate dropped.")})

    def _version(self, classifier: str, fingerprint: str) -> int:
        numbers = self._versions.setdefault(classifier, {})
        wheel = self.wheels.get(classifier)
        if wheel is not None:
            cursor = self._version_cursor.get(classifier, 0)
            while True:
                page = wheel.trace_events(after_event_id=cursor, limit=1000)
                for event in page["events"]:
                    note_version(numbers, event)
                cursor = page["cursor"]
                if len(page["events"]) < 1000:
                    break
            self._version_cursor[classifier] = cursor
        if fingerprint not in numbers:
            numbers[fingerprint] = len(numbers) + 1
        return numbers[fingerprint]

    def _partitions(self, classifier: str):
        return feedback_partitions(self.wheels[classifier], self._item_values(), self.definition.seed)

    def _item_values(self) -> dict[str, dict]:
        return {item_id: json.loads(values) for item_id, values in
                self.db.execute("SELECT id,item_values FROM items ORDER BY rowid")}

    def _item_fingerprint(self, decision_id: str) -> str:
        return self.db.execute("SELECT item_fingerprint FROM decisions WHERE id=?", (decision_id,)).fetchone()[0]

    def _latest_decision(self, item_id: str):
        row = self.db.execute("SELECT state,payload FROM decisions WHERE item_id=? AND state!='superseded' "
                              "ORDER BY rowid DESC LIMIT 1", (item_id,)).fetchone()
        return None if row is None else (row[0], Decision.from_json(json.loads(row[1])))

    def _classifier(self, decision: Decision, classifier: str | None) -> str:
        if classifier is None:
            if len(decision.results) != 1:
                raise ValueError("name the classifier; this cyclotron has several")
            return next(iter(decision.results))
        if classifier not in decision.results:
            raise ValueError("classifier not in this cyclotron")
        return classifier

    def _review(self, review_id: str) -> Review | None:
        row = self.db.execute("SELECT payload FROM reviews WHERE id=?", (review_id,)).fetchone()
        return None if row is None else self._load_review(row[0])

    @staticmethod
    def _load_review(payload: str) -> Review:
        values = json.loads(payload)
        return Review(**{**values, "optimization_warnings": tuple(values.get("optimization_warnings", ()))})

    def _active_review(self, decision_id: str, classifier: str) -> Review | None:
        """The latest labeled review that has not been undone, if any."""
        active = None
        for (payload,) in self.db.execute("SELECT payload FROM reviews WHERE decision_id=? AND classifier=? ORDER BY seq",
                                          (decision_id, classifier)):
            review = self._load_review(payload)
            if review.kind in ("label", "correction"):
                active = review
            elif review.kind == "undo":
                active = None
        return active

    def _store_review(self, review: Review) -> None:
        payload = {**asdict(review), "optimization_warnings": list(review.optimization_warnings)}
        with self.db:
            self.db.execute("INSERT INTO reviews(id,decision_id,classifier,payload) VALUES (?,?,?,?)",
                            (review.review_id, review.decision_id, review.classifier, _json(payload)))
            reviewed = all(self._last_review_kind(review.decision_id, cid) in ("label", "correction", "no-label")
                           for cid in self.specs)
            self.db.execute("UPDATE decisions SET state=? WHERE id=? AND state!='superseded'",
                            ("reviewed" if reviewed else "open", review.decision_id))
        self._record({"kind": "review", "review": review.to_json()})

    def _last_review_kind(self, decision_id: str, classifier: str) -> str | None:
        row = self.db.execute("SELECT payload FROM reviews WHERE decision_id=? AND classifier=? ORDER BY seq DESC LIMIT 1",
                              (decision_id, classifier)).fetchone()
        return None if row is None else json.loads(row[0])["kind"]

    @staticmethod
    def _feedback_event(wheel, review_id: str) -> dict:
        event = next((e for e in reversed(wheel.history(100000))
                      if e["kind"] == "human-feedback" and e["feedback"]["id"] == review_id), None)
        if event is None:
            raise ValueError("the reviewed label is missing from the classifier history")
        return event

    def _close_waiting_cycles(self, item_id: str, decision_id: str, reason: str) -> None:
        target = Item(item_id, self._item_values()[item_id])
        for wheel in self.wheels.values():
            cycle = wheel.resume_cycle(target)
            if cycle is not None:
                wheel._emit({"kind": "human-skipped", "target_id": item_id, "decision_id": decision_id, "reason": reason})
                cycle.__exit__(None, None, None)

    def _reconcile(self, classifier: str) -> None:
        wheel = self.wheels[classifier]
        training, development, _ = self._partitions(classifier)
        wheel.reconcile_feedback(training, development=development)
        wheel.set_optimizer_context([r.context["human_feedback"] for r in training if r.context.get("human_feedback")])

    async def _learn(self, classifier: str, cycle) -> list:
        if self.optimizer is None:
            # Without an LLM optimizer the cyclotron decides and records only.
            self._reconcile(classifier)
            return []
        from .observability import StepFailed
        try:
            return await learn_from_review(self.wheels, self.shared, classifier, cycle, self._partitions,
                                           optimize_every=self.definition.optimize_every,
                                           rubric_changes_every=self.definition.rubric_changes_every)
        except (StepFailed, RuntimeError) as error:
            warning = {"classifier_id": classifier, "reason": "optimization failed; labels retained, no automatic retry",
                       "error_type": type(error).__name__}
            self.wheels[classifier]._emit({"kind": "optimization-unavailable", **warning})
            return [warning]

    @staticmethod
    def _class_config(spec: ClassifierSpec) -> list[dict]:
        return [{"label": label, **({"role": "positive"} if label == spec.positive_label else {})}
                for label in spec.labels]

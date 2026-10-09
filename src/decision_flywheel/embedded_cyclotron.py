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

One call runs at a time per open cyclotron, and one writer holds a store at a
time (``FileLease`` by default; an application can supply its own lease).
``snapshot`` and ``restore`` copy a store to object storage and back.

The classifier stores are the record of what happened. Reviews, versions and
the ``subscribe`` log are derived from their events in one transaction with a
durable cursor, so a crash between the two stores loses nothing: the next
open (or call) catches up.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field, replace
import io
import os
import tarfile
import tempfile
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Mapping, Sequence
from uuid import uuid4

from .calibration_history import reviewed_prediction_bindings
from .classifier_config import ClassifierConfig
from .cyclotron_status import (FULL_REVIEW, CyclotronStatus, ReviewRate, ReviewRateOverride, VersionTracker,
                              initial_snapshot, status_from_flywheel)
from .feedback import LABEL_SOURCE_VETTED, FeedbackItem
from .flywheel import DecisionFlywheel
from .learning_loop import (REVIEWER_SELECTED, decide_with_shared_context, feedback_partitions,
                            learn_from_review, record_cycle_metrics, review_role)
from .models import DecisionTask, Item
from .selection_policy import SelectionPolicy
from .review_program import (REASONS, Override, ProgramState, ReviewProgram, check_window, current_rate,
                             expected_reviews, next_step, raise_for_version, select, state_name, window_evidence)
from .shared_decisions import SharedDecisions

SELECTED_BY = REASONS
STORE_SCHEMA = "cyclotron-store/v1"
STORE_FILES = ("cyclotron.sqlite3", "shared.sqlite3")
FULL_REVIEW_DETAIL = "Every decision is reviewed; no review-rate program is configured."


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
    """Whether a decision goes to a reviewer, why, and with what probability.

    ``reason`` is ``program`` (low confidence or sampled at the review rate),
    ``audit`` (the random audit share), or None when the decision is not sent
    to review. ``detail`` says the same in words.
    """
    selected: bool
    reason: str | None
    propensity: float
    detail: str = ""


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
                           "propensity": self.review.propensity, "detail": self.review.detail}}

    @classmethod
    def from_json(cls, value) -> "Decision":
        return cls(value["decisionId"], value["itemId"], value["createdAt"],
                   {cid: ClassifierDecision(cid, r["label"], r["confidence"], r["probabilities"], r["version"],
                                            r["fingerprint"]) for cid, r in value["classifiers"].items()},
                   _selection(value["review"]))


def _selection(value: Mapping[str, Any]) -> ReviewSelection:
    if "detail" not in value:  # decisions saved before the review-rate program
        return ReviewSelection(value["selected"], "program", value["propensity"], value["reason"])
    return ReviewSelection(value["selected"], value["reason"], value["propensity"], value["detail"])


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


class StoreLocked(RuntimeError):
    """Another writer holds this cyclotron store."""


class FileLease:
    """The default single-writer lease: an exclusive lock file in the store directory.

    The operating system releases it when the process ends, including a crash.
    An application with several workers supplies its own lease with the same
    two methods (Papyrus uses an exclusive Assignment claim).
    """

    def __init__(self, directory):
        self.path = Path(directory) / "writer.lock"
        self._handle = None

    def acquire(self) -> None:
        import fcntl
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise StoreLocked(f"another writer holds {self.path.parent}") from None
        self._handle = handle

    def release(self) -> None:
        if self._handle is not None:
            import fcntl
            fcntl.flock(self._handle, fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None


def _store_paths(directory: Path, definition: CyclotronDefinition) -> dict[str, Path]:
    paths = {name: directory / name for name in STORE_FILES}
    for spec in definition.classifiers:
        name = "classifiers/" + hashlib.sha256(spec.id.encode()).hexdigest() + ".sqlite3"
        paths[name] = directory / name
    return paths


class Cyclotron:
    """An embedded cyclotron backed by a directory of SQLite files."""

    def __init__(self, directory, definition: CyclotronDefinition, model, optimizer=None, *,
                 max_requests: int = 100, redact: Sequence[str] = (), observer: Callable[[dict], None] | None = None,
                 lease=None, review_program: ReviewProgram | None = ReviewProgram()):
        if not isinstance(definition, CyclotronDefinition):
            raise ValueError("definition must be a CyclotronDefinition")
        if type(max_requests) is not int or max_requests < 1:
            raise ValueError("max_requests must be a positive integer")
        if review_program is not None and not isinstance(review_program, ReviewProgram):
            raise ValueError("review_program must be a ReviewProgram or None")
        self.definition, self.optimizer, self.program = definition, optimizer, review_program
        self.observer = observer or (lambda event: None)
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.wheels: dict[str, DecisionFlywheel] = {}
        self.shared = None
        self.db = None
        # The lease comes first: nothing is opened, and no model is called,
        # while another writer holds the store.
        self.lease = lease if lease is not None else FileLease(self.directory)
        self.lease.acquire()
        try:
            self._open(model, optimizer, max_requests, redact)
        except Exception:
            self.close()
            raise
        self._lock = asyncio.Lock()

    def _open(self, model, optimizer, max_requests, redact):
        definition = self.definition
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
        self.versions: dict[str, VersionTracker] = {}
        self._seen: dict[str, int] = {}
        (self.directory / "classifiers").mkdir(exist_ok=True)
        paths = _store_paths(self.directory, definition)
        for spec in definition.classifiers:
            path = paths["classifiers/" + hashlib.sha256(spec.id.encode()).hexdigest() + ".sqlite3"]
            wheel = DecisionFlywheel(
                path, ClassifierConfig(spec.task), self.shared.adapter(spec.id), optimizer,
                max_requests=max_requests, redact=redact, selection_policy=spec.selection_policy,
                min_evaluation_per_class=2)
            self.wheels[spec.id] = wheel
            self.versions[spec.id] = VersionTracker(initial_snapshot(wheel))
            self._seen[spec.id] = 0
        self._sync()

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
        if self.shared is not None:
            self.shared.close()
            self.shared = None
        if self.db is not None:
            self.db.close()
            self.db = None
        if self.lease is not None:
            self.lease.release()
            self.lease = None

    # Decide ---------------------------------------------------------------

    async def decide(self, item: Item) -> Decision:
        """Decide one item and say whether it goes to review.

        A decision a person may have seen is stable: an unchanged item keeps
        its decision, label and confidence until it is reviewed or a new
        version (not an ML model refit) is promoted. No model call is repeated.
        """
        async with self._lock:
            if not isinstance(item, Item):
                raise ValueError("decide needs an Item")
            for spec in self.definition.classifiers:
                spec.task.validate_target(item)
            self._sync()
            fingerprint = hashlib.sha256(_json(dict(item.values)).encode()).hexdigest()
            latest = self._latest_decision(item.id)
            if latest is not None:
                state, decision = latest
                unchanged = self._item_fingerprint(decision.decision_id) == fingerprint
                same_version = all(decision.results[cid].version == self.versions[cid].version
                                   for cid in self.wheels)
                if unchanged and (state == "reviewed" or same_version):
                    return decision
                if state == "open":
                    self._close_waiting_cycles(item.id, decision.decision_id, "superseded by a new decision")
                with self.db:
                    self.db.execute("UPDATE decisions SET state='superseded' WHERE id=?", (decision.decision_id,))
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO items VALUES (?,?,?)",
                                (item.id, _json(dict(item.values)), fingerprint))
            # A suspended cycle from an interrupted decide is resumed here, and
            # the shared request cache answers it without a new model call.
            results, _ = await decide_with_shared_context(self.wheels, self.shared, item, self._partitions,
                                                          now=datetime.now(timezone.utc))
            self._sync()
            decision_id = str(uuid4())
            confidences = [result.confidence for result in results.values()]
            confidence = None if any(c is None for c in confidences) else min(confidences)
            selection = self._select(item.id, confidence)
            decision = Decision(decision_id, item.id, _now(), {
                cid: ClassifierDecision(cid, result.label, result.confidence,
                                        dict(result.probabilities) if result.probabilities else None,
                                        self.versions[cid].version, self.wheels[cid].active.fingerprint)
                for cid, result in results.items()},
                ReviewSelection(selection.selected, selection.reason, selection.propensity, selection.detail))
            if not selection.selected:
                # Nobody is asked; close the waiting cycles so nothing reads as pending.
                for wheel in self.wheels.values():
                    cycle = wheel.resume_cycle(item)
                    if cycle is not None:
                        wheel._emit({"kind": "review-not-requested", "target_id": item.id, "decision_id": decision_id,
                                     "propensity": selection.propensity, "detail": selection.detail})
                        cycle.__exit__(None, None, None)
            self._insert_decision(decision, fingerprint)
            return decision

    # Review rate ----------------------------------------------------------

    def _select(self, item_id: str, confidence: float | None):
        """Advance the review-rate program to this decision, then choose.

        The random draw is keyed by the item and the decision's position, so a
        store replays the same selections; it never reads a label.
        """
        from .review_program import Selection
        if self.program is None:
            return Selection(True, "program", 1.0, FULL_REVIEW_DETAIL)
        now = datetime.now(timezone.utc)
        state = self._program_state()
        events = []
        if state.override and not Override(**state.override).active(now):
            expired = Override(**state.override)
            state.override = None
            events.append(self._rate_event(state, now, f"The manual rate of {round(expired.rate * 100)}% set by "
                                                       f"{expired.set_by} expired."))
        state, raised = raise_for_version(self.program, state, self._primary_version())
        if raised:
            events.append(self._rate_event(state, now, state.reason))
        decided = self.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        while state.checked_windows < decided // self.program.window:
            window = state.checked_windows + 1
            evidence = window_evidence(self.program, self._window_reviews(window), self._gap_points())
            state, change = check_window(self.program, state, evidence)
            if change:
                events.append(self._rate_event(state, now, state.reason, evidence=evidence, window=window))
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('review_program_state', ?)", (_json(state.to_json()),))
            appended = [self._append(event) for event in events]
        for event in appended:
            self.observer(event)
        return select(self.program, current_rate(self.program, state, now), key=f"{item_id}:{decided + 1}",
                      confidence=confidence)

    def set_review_rate(self, rate: float, *, set_by: str, expires_at: datetime | None = None) -> None:
        """An operator override of the review rate, with an optional expiry.

        Low-confidence decisions and the audit share are still reviewed.
        """
        if self.program is None:
            raise ValueError("this cyclotron has no review-rate program")
        if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not 0 <= rate <= 1:
            raise ValueError("rate must be between zero and one")
        if not isinstance(set_by, str) or not set_by.strip():
            raise ValueError("an override needs the person who set it")
        if expires_at is not None and (expires_at.tzinfo is None or expires_at <= datetime.now(timezone.utc)):
            raise ValueError("expiry must be a future, timezone-aware time")
        state = self._program_state()
        state.override = {"rate": float(rate), "expires_at": expires_at.isoformat() if expires_at else None,
                          "set_by": set_by, "set_at": _now()}
        now = datetime.now(timezone.utc)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('review_program_state', ?)", (_json(state.to_json()),))
            event = self._append(self._rate_event(state, now, f"Manual rate of {round(rate * 100)}% set by {set_by}."))
        self.observer(event)

    def clear_review_rate(self, *, set_by: str) -> None:
        """End an operator override; the program's own rate applies again."""
        state = self._program_state()
        if not state.override:
            return
        state.override = None
        now = datetime.now(timezone.utc)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('review_program_state', ?)", (_json(state.to_json()),))
            event = self._append(self._rate_event(state, now, f"Manual rate cleared by {set_by}."))
        self.observer(event)

    def _program_state(self) -> ProgramState:
        row = self.db.execute("SELECT value FROM meta WHERE key='review_program_state'").fetchone()
        return ProgramState.from_json(json.loads(row[0]) if row else None)

    def _rate_event(self, state: ProgramState, now: datetime, reason: str, **details) -> dict:
        return {"kind": "review-rate-changed", "state": state_name(self.program, state, now),
                "rate": current_rate(self.program, state, now), "reason": reason, **details}

    def _primary(self) -> ClassifierSpec:
        return self.definition.classifiers[0]

    def _primary_version(self) -> int:
        return self.versions[self._primary().id].version

    def _gap_points(self) -> int | None:
        spec = self._primary()
        return status_from_flywheel(self.wheels[spec.id], cyclotron_id=self.definition.id,
                                    positive_label=spec.positive_label,
                                    window=self.program.calibration_window).calibration.gap_points

    def _window_reviews(self, window: int) -> list[dict]:
        """Program and audit reviews of the decisions in one finished window."""
        cid = self._primary().id
        size = self.program.window
        rows = []
        for decision_id, payload in self.db.execute(
                "SELECT id,payload FROM decisions ORDER BY rowid LIMIT ? OFFSET ?", (size, (window - 1) * size)):
            review = self._active_review(decision_id, cid)
            if review is None or review.selected_by not in ("program", "audit"):
                continue
            result = json.loads(payload)["classifiers"][cid]
            rows.append({"confidence": result["confidence"], "decision": result["label"], "label": review.label})
        return rows

    def _review_rate_status(self) -> ReviewRate:
        if self.program is None:
            return FULL_REVIEW
        now = datetime.now(timezone.utc)
        state = self._program_state()
        override = Override(**state.override) if state.override else None
        active = override is not None and override.active(now)
        decided = self.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        until = self.program.window - decided % self.program.window
        week_ago = (now - timedelta(days=7)).isoformat()
        confidences = []
        for (payload,) in self.db.execute("SELECT payload FROM decisions"):
            decision = json.loads(payload)
            if decision["createdAt"] >= week_ago:
                values = [r["confidence"] for r in decision["classifiers"].values()]
                confidences.append(None if None in values else min(values))
        rate = current_rate(self.program, state, now)
        return ReviewRate(state_name(self.program, state, now), rate,
                          (f"Manual rate of {round(override.rate * 100)}% set by {override.set_by}."
                           if active else state.reason),
                          audit_floor=self.program.audit_share, confidence_threshold=self.program.confidence_threshold,
                          override=ReviewRateOverride(override.rate, override.expires_at, override.set_by, override.set_at)
                          if active else None,
                          next_check_after_decisions=until,
                          next_step=next_step(self.program, state, decisions_until_check=until, now=now),
                          expected_reviews_per_week=expected_reviews(self.program, rate, confidences))

    def _insert_decision(self, decision: Decision, fingerprint: str) -> None:
        with self.db:
            self.db.execute("INSERT INTO decisions VALUES (?,?,?,?,?)",
                            (decision.decision_id, decision.item_id, fingerprint, "open", _json(decision.to_json())))
            event = self._append({"kind": "decision", "decision": decision.to_json()})
        self.observer(event)

    def decision(self, decision_id: str) -> Decision:
        row = self.db.execute("SELECT payload FROM decisions WHERE id=?", (decision_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown decision {decision_id}")
        return Decision.from_json(json.loads(row[0]))

    # Review ---------------------------------------------------------------

    async def review(self, decision_id: str, label: str | None, *, classifier: str | None = None,
                     explanation: str | None = None, reason_code: str | None = None, reviewer: str | None = None,
                     selected_by: str | None = None, review_id: str | None = None, _replay: bool = False) -> Review:
        """Record a reviewer's label for a decision.

        The first label for a decision teaches the cyclotron; a later label is
        a correction. ``label=None`` records a review without a label (for
        example a duplicate), which closes the decision without teaching.
        ``selected_by`` says who chose to review it: ``program`` or ``audit``
        (the decision was sent to review; the default when it was) or
        ``reviewer`` (a person chose it; the default when it was not sent). A
        reviewer's own choice trains the ML model but never counts as
        alignment or audit evidence.
        Pass the application's own ``review_id`` to make resubmission safe.
        """
        async with self._lock:
            self._sync()
            decision = self.decision(decision_id)
            cid = self._classifier(decision, classifier)
            wheel, spec = self.wheels[cid], self.specs[cid]
            if label is not None:
                label = spec.task.validate_label(label)
            if selected_by is None:
                selected_by = decision.review.reason if decision.review.selected else "reviewer"
            if selected_by not in SELECTED_BY:
                raise ValueError(f"selected_by must be one of {SELECTED_BY}")
            if selected_by != "reviewer" and not decision.review.selected and not _replay:
                raise ValueError("this decision was not sent to review; a review of it is selected_by='reviewer'")
            for name, value in (("explanation", explanation), ("reason_code", reason_code), ("reviewer", reviewer)):
                if value is not None and not isinstance(value, str):
                    raise ValueError(f"{name} must be text")
            explanation = (explanation.strip() or None) if explanation else None
            review_id = review_id or str(uuid4())
            existing = self._review(review_id)
            if existing is not None:
                same = (existing.decision_id, existing.classifier, existing.label, existing.reason_code) == (
                    decision_id, cid, label, reason_code) and (existing.explanation or None) == self._redacted(cid, explanation)
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
                cycle = wheel.resume_cycle(target) or wheel.cycle(target, reason="review").__enter__()
                wheel._emit({"kind": "human-skipped", "target_id": target.id, "explanation": explanation, **annotations})
                cycle.__exit__(None, None, None)
            elif active is None:
                cycle = wheel.resume_cycle(target) or wheel.cycle(target, reason="review").__enter__()
                try:
                    propensity = decision.review.propensity if selected_by != "reviewer" else 1.0
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
            self._sync()
            return replace(self._review(review_id), optimization_warnings=tuple(warnings))

    async def undo_review(self, decision_id: str, *, classifier: str | None = None,
                          review_id: str | None = None) -> Review:
        """Retract the active label for a decision and wait for a new review."""
        async with self._lock:
            self._sync()
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
            self._sync()
            return self._review(review_id)

    # Status, events and labels -------------------------------------------

    def status(self, classifier: str | None = None) -> CyclotronStatus:
        """The cyclotron-status/v1 snapshot for one classifier. No model call."""
        if classifier is None:
            if len(self.specs) != 1:
                raise ValueError("name the classifier; this cyclotron has several")
            classifier = next(iter(self.specs))
        spec = self.specs[classifier]
        return status_from_flywheel(self.wheels[classifier], cyclotron_id=self.definition.id,
                                    positive_label=spec.positive_label, review_rate=self._review_rate_status())

    def subscribe(self, after: int = 0, limit: int = 100) -> dict[str, Any]:
        """Committed cyclotron events after a cursor, oldest first.

        Kinds: decision, review, promoted (a new version), refit (a new ML
        model, same version), dropped. Pass the returned cursor back to
        continue; the cursor survives restarts.
        """
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("cursor must be nonnegative and page size between 1 and 1000")
        self._sync()
        rows = self.db.execute("SELECT id,payload FROM events WHERE id>? ORDER BY id LIMIT ?", (after, limit)).fetchall()
        return {"events": [{**json.loads(payload), "cursor": event_id} for event_id, payload in rows],
                "cursor": rows[-1][0] if rows else after}

    def labels(self) -> list[dict[str, Any]]:
        """Every active label with its item: what an application can replay into a new store."""
        values = self._item_values()
        rows = []
        for decision_id, item_id in self.db.execute("SELECT id,item_id FROM decisions ORDER BY rowid"):
            for cid in self.specs:
                review = self._active_review(decision_id, cid)
                if review is not None:
                    rows.append({"item": Item(item_id, values[item_id]), "classifier": cid, "label": review.label,
                                 "explanation": review.explanation, "reason_code": review.reason_code,
                                 "reviewer": review.reviewer, "selected_by": review.selected_by,
                                 "review_id": review.review_id})
        return rows

    async def replay(self, labels: Sequence[Mapping[str, Any]]) -> int:
        """Rebuild learning from an application's recorded labels, in order.

        Each record needs ``item`` and ``label``, and may name the classifier,
        explanation, reason code, reviewer, selected_by and review_id. Items are
        decided again, so a new store makes decision-model calls.
        """
        count = 0
        for record in labels:
            decision = await self.decide(record["item"])
            await self.review(decision.decision_id, record["label"], classifier=record.get("classifier"),
                              explanation=record.get("explanation"), reason_code=record.get("reason_code"),
                              reviewer=record.get("reviewer"), selected_by=record.get("selected_by"),
                              review_id=record.get("review_id"), _replay=True)
            count += 1
        return count

    # Snapshot and restore -------------------------------------------------

    def snapshot(self, path) -> dict[str, Any]:
        """Write a consistent copy of this store to one archive file.

        Call it between operations. SQLite's backup API copies each file, so
        the archive is consistent even though the store stays open. The
        archive holds item values, decisions, reviews, transcripts and caches,
        never provider keys; values passed as ``redact`` are already redacted.
        """
        path = Path(path)
        manifest = {"schema": STORE_SCHEMA, "cyclotron": self.definition.id,
                    "definition_fingerprint": self.definition.fingerprint, "created_at": _now(), "files": []}
        connections = {"cyclotron.sqlite3": self.db, "shared.sqlite3": self.shared.db}
        for spec in self.definition.classifiers:
            name = "classifiers/" + hashlib.sha256(spec.id.encode()).hexdigest() + ".sqlite3"
            connections[name] = self.wheels[spec.id].db
        with tempfile.TemporaryDirectory() as scratch:
            with tarfile.open(Path(scratch) / "store.tar.gz", "w:gz") as archive:
                for name, connection in connections.items():
                    copy = Path(scratch) / hashlib.sha256(name.encode()).hexdigest()
                    target = sqlite3.connect(copy)
                    with target:
                        connection.backup(target)
                    target.close()
                    archive.add(copy, arcname=name)
                    manifest["files"].append({"name": name, "sha256": hashlib.sha256(copy.read_bytes()).hexdigest()})
                data = json.dumps(manifest, indent=2).encode()
                info = tarfile.TarInfo("manifest.json")
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
            path.parent.mkdir(parents=True, exist_ok=True)
            partial = path.with_name(path.name + ".partial")
            os.replace(Path(scratch) / "store.tar.gz", partial)
            os.replace(partial, path)
        return manifest

    @staticmethod
    def restore(archive_path, directory, *, lease=None) -> dict[str, Any]:
        """Restore a snapshot into an empty directory, under the store's lease.

        Open the restored store with ``Cyclotron.open`` and the same definition.
        """
        directory = Path(directory)
        if any((directory / name).exists() for name in STORE_FILES) or (directory / "classifiers").exists():
            raise ValueError("restore needs a directory without a cyclotron store")
        lease = lease if lease is not None else FileLease(directory)
        lease.acquire()
        try:
            with tarfile.open(archive_path, "r:gz") as archive:
                manifest = json.loads(archive.extractfile("manifest.json").read())
                if manifest.get("schema") != STORE_SCHEMA:
                    raise ValueError("not a cyclotron store snapshot")
                allowed = {entry["name"]: entry["sha256"] for entry in manifest["files"]}
                for name, digest in allowed.items():
                    if name not in STORE_FILES and not (name.startswith("classifiers/") and name.count("/") == 1
                                                        and name.endswith(".sqlite3") and ".." not in name):
                        raise ValueError(f"unexpected file in snapshot: {name}")
                    data = archive.extractfile(name).read()
                    if hashlib.sha256(data).hexdigest() != digest:
                        raise ValueError(f"snapshot file is damaged: {name}")
                    target = directory / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
            return manifest
        finally:
            lease.release()

    # Derived records ------------------------------------------------------

    def _sync(self) -> None:
        """Derive reviews, versions and subscribe events from classifier events.

        Each classifier's new events are applied in one transaction together
        with its durable cursor, so the derived log is never ahead of or behind
        the classifier store after a crash.
        """
        for cid, wheel in self.wheels.items():
            cursor_row = self.db.execute("SELECT value FROM meta WHERE key=?", (f"cursor:{cid}",)).fetchone()
            cursor = int(cursor_row[0]) if cursor_row else 0
            appended = []
            while True:
                page = wheel.trace_events(after_event_id=self._seen[cid], limit=1000)
                if not page["events"]:
                    break
                with self.db:
                    for event in page["events"]:
                        change = self.versions[cid].feed(event)
                        if event["event_id"] > cursor:
                            appended.extend(self._derive(cid, event, change))
                    self._seen[cid] = page["cursor"]
                    if page["cursor"] > cursor:
                        cursor = page["cursor"]
                        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (f"cursor:{cid}", str(cursor)))
            for event in appended:
                self.observer(event)

    def _derive(self, classifier: str, event: Mapping[str, Any], change) -> list[dict]:
        kind = event.get("kind")
        review = None
        if kind == "human-feedback" and event.get("review_id"):
            feedback = event["feedback"]
            retracted = event.get("action") == "retracted"
            review = Review(event["review_id"], event["decision_id"], feedback["item_id"], classifier,
                            "undo" if retracted else "correction"
                            if (feedback.get("review_provenance") or "").startswith("corrected-human-vote:") else "label",
                            None if retracted else feedback["final_answer_value"],
                            None if retracted else feedback.get("edit_comment_value"),
                            event.get("reason_code"), event.get("reviewer"),
                            event.get("selected_by") or "program", event.get("created_at") or _now())
        elif kind == "human-skipped" and event.get("review_id"):
            review = Review(event["review_id"], event["decision_id"], event["target_id"], classifier, "no-label", None,
                            event.get("explanation"), event.get("reason_code"), event.get("reviewer"),
                            event.get("selected_by") or "program", event.get("created_at") or _now())
        if review is not None:
            if self._review(review.review_id) is not None:
                return []
            self.db.execute("INSERT INTO reviews(id,decision_id,classifier,payload) VALUES (?,?,?,?)",
                            (review.review_id, review.decision_id, classifier, _json(self._review_payload(review))))
            reviewed = all(self._last_review_kind(review.decision_id, cid) in ("label", "correction", "no-label")
                           for cid in self.specs)
            self.db.execute("UPDATE decisions SET state=? WHERE id=? AND state!='superseded'",
                            ("reviewed" if reviewed else "open", review.decision_id))
            return [self._append({"kind": "review", "review": review.to_json()})]
        if change is not None:
            tracker = self.versions[classifier]
            return [self._append({"kind": change.kind, "classifier": classifier, "version": change.to_version,
                                  "refits": tracker.refits if change.kind == "refit" else 0,
                                  "summary": change.summary, "labels": change.labels,
                                  "fingerprint": event.get("classifier_version")})]
        return []

    def _append(self, event: Mapping[str, Any]) -> dict:
        """Add one event to the subscribe log inside the caller's transaction."""
        event = {"cyclotronId": self.definition.id, "at": _now(), **event}
        row = self.db.execute("INSERT INTO events(payload) VALUES (?)", (_json(event),))
        return {**event, "cursor": row.lastrowid}

    # Internals ------------------------------------------------------------

    def _redacted(self, classifier: str, text: str | None) -> str | None:
        if text is None:
            return None
        for secret in self.wheels[classifier].redact:
            text = text.replace(secret, "[REDACTED]")
        return text

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

    @staticmethod
    def _review_payload(review: Review) -> dict:
        return {**asdict(review), "optimization_warnings": []}

    @staticmethod
    def _load_review(payload: str) -> Review:
        values = json.loads(payload)
        return Review(**{**values, "optimization_warnings": tuple(values.get("optimization_warnings", ()))})

    def _review(self, review_id: str) -> Review | None:
        row = self.db.execute("SELECT payload FROM reviews WHERE id=?", (review_id,)).fetchone()
        return None if row is None else self._load_review(row[0])

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

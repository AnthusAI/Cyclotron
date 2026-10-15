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
from copy import copy, deepcopy
from contextlib import asynccontextmanager
from contextvars import ContextVar
import inspect
from dataclasses import asdict, dataclass, field, replace
import io
import os
import tarfile
import tempfile
import time
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Awaitable, Callable, Mapping, Sequence
from uuid import uuid4

from .calibration_history import reviewed_prediction_bindings
from .classifier_config import ClassifierConfig
from .cyclotron_status import (FULL_REVIEW, CyclotronOverview, CyclotronStatus, ReviewRate, ReviewRateOverride, VersionTracker,
                              initial_snapshot, status_from_flywheel, worst_output)
from .feedback import LABEL_SOURCE_VETTED, FeedbackItem
from .flywheel import DecisionFlywheel
from .learning_loop import (REVIEWER_SELECTED, decide_with_shared_context, feedback_partitions,
                            learn_from_review, record_cycle_metrics, review_role)
from .models import DecisionTask, Item
from .selection_policy import SelectionPolicy
from .review_program import (REASONS, Override, ProgramState, ReviewProgram, check_window, combine_evidence,
                             current_rate, raise_for_versions,
                             expected_item_reviews, expected_reviews, next_step, raise_for_version, select, state_name, window_evidence)
from .optimizer_agent import OptimizationSuperseded
from .shared_decisions import SharedDecisions
from .sibling_check import SiblingPolicy, install_sibling_checks

SELECTED_BY = REASONS
LEARNING_MODES = ("inline", "deferred")
ExecutionGuard = Callable[[], None | Awaitable[None]]
_NO_LABEL = object()  # review() was called without a single label
STORE_SCHEMA = "cyclotron-store/v1"
STORE_FILES = ("cyclotron.sqlite3", "shared.sqlite3")
FULL_REVIEW_DETAIL = "Every decision is reviewed; no review-rate program is configured."


def _usage_counts(usage) -> tuple[int, int, int]:
    from .run_usage import _usage
    return _usage(usage)


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ClassifierSpec:
    """One classifier: a reviewed output of the cyclotron, with its own labels and ML model."""
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
    # What promotion does when a change to one classifier would regress a
    # sibling on the sibling's development labels. Policy, not identity: it
    # is left out of the fingerprint so a store can change it.
    sibling_policy: SiblingPolicy = field(default_factory=SiblingPolicy)

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
        if not isinstance(self.sibling_policy, SiblingPolicy):
            raise ValueError("sibling_policy must be SiblingPolicy")

    @property
    def fingerprint(self) -> str:
        identity = asdict(self)
        identity.pop("sibling_policy")
        return hashlib.sha256(_json(identity).encode()).hexdigest()


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
    # With several outputs, each output's own selection; the fields above
    # summarize them (selected when any output is requested).
    outputs: Mapping[str, "ReviewSelection"] | None = None

    def for_output(self, classifier: str) -> "ReviewSelection":
        """This output's selection: its own with several outputs, else the decision's."""
        return self.outputs[classifier] if self.outputs else self

    @property
    def requested(self) -> tuple[str, ...] | None:
        """The outputs sent to review (None when the decision is selected as a whole)."""
        return None if self.outputs is None else tuple(cid for cid, s in self.outputs.items() if s.selected)


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
                "review": _selection_json(self.review)}

    @classmethod
    def from_json(cls, value) -> "Decision":
        return cls(value["decisionId"], value["itemId"], value["createdAt"],
                   {cid: ClassifierDecision(cid, r["label"], r["confidence"], r["probabilities"], r["version"],
                                            r["fingerprint"]) for cid, r in value["classifiers"].items()},
                   _selection(value["review"]))


def _selection_json(selection: ReviewSelection) -> dict[str, Any]:
    value = {"selected": selection.selected, "reason": selection.reason,
             "propensity": selection.propensity, "detail": selection.detail}
    if selection.outputs is not None:
        value["outputs"] = {cid: _selection_json(output) for cid, output in selection.outputs.items()}
    return value


def _selection(value: Mapping[str, Any]) -> ReviewSelection:
    if "detail" not in value:  # decisions saved before the review-rate program
        return ReviewSelection(value["selected"], "program", value["propensity"], value["reason"])
    outputs = value.get("outputs")
    return ReviewSelection(value["selected"], value["reason"], value["propensity"], value["detail"],
                           {cid: _selection(output) for cid, output in outputs.items()} if outputs is not None else None)


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


def _supporting_questions(definition: CyclotronDefinition, seeds) -> dict[str, list[dict]]:
    """Validate seed supporting questions into the optimizer's own question shape."""
    specs = {spec.id: spec for spec in definition.classifiers}
    seeds = dict(seeds or {})
    if set(seeds) - set(specs):
        raise ValueError("seed_supporting_questions maps classifier ids to supporting questions")
    normalized = {}
    for classifier, questions in seeds.items():
        if isinstance(questions, (str, Mapping)) or not isinstance(questions, Sequence):
            raise ValueError("seed_supporting_questions needs a list of questions per classifier")
        rows = []
        for question in questions:
            if isinstance(question, DecisionTask):
                question = {"name": question.name, "instructions": question.instructions,
                            "labels": list(question.labels)}
            if not isinstance(question, Mapping) or set(question) != {"name", "instructions", "labels"}:
                raise ValueError("a supporting question has exactly name, instructions and labels")
            task = DecisionTask(question["name"], question["labels"], question["instructions"],
                                specs[classifier].input_field)
            rows.append({"name": task.name, "instructions": task.instructions, "labels": list(task.labels)})
        # The same validation the optimizer's proposals pass: unique, not "decision".
        ClassifierConfig(specs[classifier].task, tasks=tuple(
            DecisionTask(row["name"], tuple(row["labels"]), row["instructions"]) for row in rows))
        normalized[classifier] = rows
    return normalized


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
                 lease=None, review_program: ReviewProgram | None = ReviewProgram(),
                 seed_rubrics: Mapping[str, str] | None = None,
                 priming_examples: Mapping[str, Sequence[LabeledItem]] | None = None,
                 seed_supporting_questions: Mapping[str, Sequence[Mapping[str, Any] | DecisionTask]] | None = None,
                 share_supporting_questions: bool | None = None, learning: str = "inline",
                 max_superseded_passes: int = 3):
        if not isinstance(definition, CyclotronDefinition):
            raise ValueError("definition must be a CyclotronDefinition")
        if type(max_requests) is not int or max_requests < 1:
            raise ValueError("max_requests must be a positive integer")
        if review_program is not None and not isinstance(review_program, ReviewProgram):
            raise ValueError("review_program must be a ReviewProgram or None")
        seed_rubrics = dict(seed_rubrics or {})
        if set(seed_rubrics) - {spec.id for spec in definition.classifiers} or any(
                not isinstance(text, str) for text in seed_rubrics.values()):
            raise ValueError("seed_rubrics maps classifier ids to rubric text")
        self.seed_rubrics = seed_rubrics
        from .priming_context import encode_priming
        self.requested_priming = None if priming_examples is None else encode_priming(
            dict(priming_examples), {spec.id:spec.task for spec in definition.classifiers}, redact=redact)
        self.seed_supporting_questions = _supporting_questions(definition, seed_supporting_questions)
        if share_supporting_questions is not None and not isinstance(share_supporting_questions, bool):
            raise ValueError("share_supporting_questions must be a bool")
        self.requested_sharing = share_supporting_questions
        if learning not in LEARNING_MODES:
            raise ValueError(f"learning must be one of {LEARNING_MODES}")
        self.learning = learning
        if type(max_superseded_passes) is not int or max_superseded_passes < 1:
            raise ValueError("max_superseded_passes must be a positive integer")
        # After this many superseded passes in a row, an output's next pass keeps the store lock through
        # its provider calls, so it cannot be superseded again.
        self.max_superseded_passes = max_superseded_passes
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
        # The output a deferred learning pass is optimizing, in that pass's task only.
        self._learning: ContextVar[str | None] = ContextVar("cyclotron_learning", default=None)
        # One learning pass at a time, even across concurrent learn() calls.
        self._learn_lock = asyncio.Lock()
        # The running pass's evidence, row marks and lent-lock intervals, in that pass's task only.
        self._pass_state: ContextVar[dict | None] = ContextVar("cyclotron_pass_state", default=None)

    def _open(self, model, optimizer, max_requests, redact):
        definition = self.definition
        from .embedded_storage import SQLiteEmbeddedStorage
        self.root_storage = SQLiteEmbeddedStorage(self.directory/'cyclotron.sqlite3')
        self.db = self.root_storage.db  # Native snapshot compatibility only.
        saved = self.root_storage.meta('definition')
        if saved and json.loads(saved[0])["fingerprint"] != definition.fingerprint:
            raise ValueError("this store belongs to a different cyclotron definition")
        with self.root_storage.transaction():
            self.root_storage.insert_meta('definition',_json({'fingerprint': definition.fingerprint, 'definition': asdict(definition)}),ignore=True)
        from .priming_context import decode_priming
        stored_priming = self.root_storage.meta('priming_examples')
        saved_priming = json.loads(stored_priming[0]) if stored_priming else {}
        if stored_priming and self.requested_priming is not None and self.requested_priming != saved_priming:
            raise ValueError("this store has different accepted priming; create a new store")
        if not stored_priming and self.requested_priming:
            if saved:
                raise ValueError("priming must be supplied when the store is first created")
            saved_priming = self.requested_priming
        with self.root_storage.transaction():
            self.root_storage.insert_meta('priming_examples',_json(saved_priming),ignore=True)
        self.priming_examples = decode_priming(saved_priming)
        self._batch_log: ContextVar[list | None] = ContextVar(f"cyclotron_batches_{id(self)}", default=None)
        self._batches = []
        self.shared = SharedDecisions(self.directory / "shared.sqlite3", model, max_requests=0,
                                      observer=self._on_batch,
                                      share_supporting_questions=self._sharing(created=saved is None))
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
                path, ClassifierConfig(spec.task, example_ids=tuple(row.item.id for row in self.priming_examples.get(spec.id,())), rubric=self._seed(spec.id),
                                       tasks=self._seed_questions(spec, created=saved is None)),
                # Each output gets its own copy so its optimizer events reach only its own
                # trace; the copies share the transport, so the call limit stays cyclotron-wide.
                self.shared.adapter(spec.id), self._optimizer_for(optimizer, spec.id),
                max_requests=max_requests, redact=redact, selection_policy=spec.selection_policy,
                min_evaluation_per_class=2)
            self.wheels[spec.id] = wheel
            self.versions[spec.id] = VersionTracker(initial_snapshot(wheel))
            self._seen[spec.id] = 0
        install_sibling_checks(self.wheels, self.shared, self._partitions, definition.sibling_policy)
        self._sync()

    def _seed(self, classifier: str) -> str:
        """The first rubric for a classifier: fixed when its store is first opened.

        The seed is not part of the definition, so an application can derive it
        from its own parameters (for example a publication's doctrine). Later
        changes to those parameters do not rewrite a store's rubric; the LLM
        optimizer owns it from then on.
        """
        key = f"seed_rubric:{classifier}"
        row = self.root_storage.meta(key)
        if row is not None:
            return json.loads(row[0])
        seed = self.seed_rubrics.get(classifier, "")
        with self.root_storage.transaction():
            self.root_storage.insert_meta(key,_json(seed))
        return seed

    def _sharing(self, *, created: bool) -> bool:
        """Whether identical supporting questions are asked once: fixed when the store is created.

        New stores share unless the application opts out. A store created
        before sharing existed keeps asking every copy separately, so its
        requests, answers and ML model features never change on upgrade.
        """
        row = self.root_storage.meta('share_supporting_questions')
        if row is not None:
            return json.loads(row[0])
        sharing = (self.requested_sharing is not False) if created else False
        with self.root_storage.transaction():
            self.root_storage.insert_meta('share_supporting_questions',_json(sharing))
        return sharing

    def _seed_questions(self, spec: ClassifierSpec, *, created: bool) -> tuple[DecisionTask, ...]:
        """The first supporting questions for a classifier: fixed when its store is first opened.

        Like the seed rubric, they are a starting point the optimizer may
        revise, replace or drop. A store created before seeding existed keeps
        an empty seed, so reopening it never changes its initial context.
        """
        key = f"seed_supporting_questions:{spec.id}"
        row = self.root_storage.meta(key)
        if row is None:
            raw = self.seed_supporting_questions.get(spec.id, []) if created else []
            with self.root_storage.transaction():
                self.root_storage.insert_meta(key,_json(raw))
        else:
            raw = json.loads(row[0])
        return tuple(DecisionTask(q["name"], tuple(q["labels"]), q["instructions"], spec.input_field) for q in raw)

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
            self.root_storage.close()
            self.db = None
        if self.lease is not None:
            self.lease.release()
            self.lease = None

    @asynccontextmanager
    async def execution(self, *, execution_guard: ExecutionGuard | None = None):
        """Own execution and run a trusted per-call guard before state access.

        The optional guard returns None on success, synchronously or after an
        await. Denial, outage and cancellation propagate unchanged, releasing
        ownership without running the body. No guard is retained on the engine.
        Hosts recheck their pinned account/person, explicit publication grants
        and current binding here; console roles alone grant no publication access.

        Use this scope for synchronous reads/steering. Do not call lock-owning
        async methods inside it: pass execution_guard to those methods instead.
        The guard must not reenter the engine's async operations. Successful
        authorization covers this execution, not later calls or deferred passes.
        """
        async with self._lock:
            await self._authorize_execution(execution_guard)
            yield self

    @staticmethod
    async def _authorize_execution(execution_guard):
        if execution_guard is not None:
            if not callable(execution_guard):
                raise TypeError('execution_guard must be callable')
            result = execution_guard()
            if inspect.isawaitable(result):
                result = await result
            if result is not None:
                raise TypeError('execution_guard must return None or raise to deny')

    # Decide ---------------------------------------------------------------

    async def decide(self, item: Item, *, execution_guard: ExecutionGuard | None = None) -> Decision:
        """Decide one item and say whether it goes to review.

        A decision a person may have seen is stable: an unchanged item keeps
        its decision, label and confidence until it is reviewed or a new
        version (not an ML model refit) is promoted. No model call is repeated.
        """
        async with self.execution(execution_guard=execution_guard):
            if not isinstance(item, Item):
                raise ValueError("decide needs an Item")
            if any(item.id == row.item.id for rows in self.priming_examples.values() for row in rows):
                raise ValueError("priming items cannot be operational targets")
            self._batches = []
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
                with self.root_storage.transaction():
                    self.root_storage.set_decision_state(decision.decision_id,'superseded')
            with self.root_storage.transaction():
                self.root_storage.put_item(item.id,_json(dict(item.values)),fingerprint)
            # A suspended cycle from an interrupted decide is resumed here, and
            # the shared request cache answers it without a new model call.
            results, _ = await decide_with_shared_context(self.wheels, self.shared, item, self._partitions,
                                                          now=datetime.now(timezone.utc))
            self._sync()
            decision_id = str(uuid4())
            selection = self._select(item.id, {cid: result.confidence for cid, result in results.items()})
            decision = Decision(decision_id, item.id, _now(), {
                cid: ClassifierDecision(cid, result.label, result.confidence,
                                        dict(result.probabilities) if result.probabilities else None,
                                        self.versions[cid].version, self.wheels[cid].active.fingerprint)
                for cid, result in results.items()}, selection)
            for cid, wheel in self.wheels.items():
                own = selection.for_output(cid)
                if own.selected:
                    continue
                # This output is not asked; close its waiting cycle so it does not read as pending.
                cycle = wheel.resume_cycle(item)
                if cycle is not None:
                    wheel._emit({"kind": "review-not-requested", "target_id": item.id, "decision_id": decision_id,
                                 "propensity": own.propensity, "detail": own.detail})
                    cycle.__exit__(None, None, None)
            self._insert_decision(decision, fingerprint)
            return decision

    # Review rate ----------------------------------------------------------

    def _select(self, item_id: str, confidences: Mapping[str, float | None]) -> ReviewSelection:
        """Advance the review-rate program to this decision, then choose which outputs to review.

        With one output the whole decision is selected, as before. With
        several, each output is selected on its own confidence and its own
        random draw, so a confident output is not sent just because a sibling
        is unsure, and audit sampling and propensities stay per output. Draws
        are keyed by the item, the decision's position and the output, so a
        store replays the same selections; they never read a label.
        """
        if self.program is None:
            whole = ReviewSelection(True, "program", 1.0, FULL_REVIEW_DETAIL)
            return whole if len(confidences) == 1 else replace(whole, outputs={cid: whole for cid in confidences})
        rate, decided = self._advance_program()
        if len(confidences) == 1:
            confidence = next(iter(confidences.values()))
            chosen = select(self.program, rate, key=f"{item_id}:{decided + 1}", confidence=confidence)
            return ReviewSelection(chosen.selected, chosen.reason, chosen.propensity, chosen.detail)
        outputs = {}
        for cid, confidence in confidences.items():
            chosen = select(self.program, rate, key=f"{item_id}:{decided + 1}:{cid}", confidence=confidence)
            outputs[cid] = ReviewSelection(chosen.selected, chosen.reason, chosen.propensity, chosen.detail)
        requested = [cid for cid, own in outputs.items() if own.selected]
        reasons = {outputs[cid].reason for cid in requested}
        detail = ("Requested: " + "; ".join(f"{cid} ({outputs[cid].detail})" for cid in requested) if requested
                  else "No output requested: " + "; ".join(f"{cid} ({own.detail})" for cid, own in outputs.items()))
        return ReviewSelection(bool(requested), ("program" if "program" in reasons else "audit") if requested else None,
                               max(own.propensity for own in outputs.values()), detail, outputs)

    def _advance_program(self) -> tuple[float, int]:
        """Apply expired overrides, version raises and finished windows; return the rate and decision count."""
        now = datetime.now(timezone.utc)
        state = self._program_state()
        events = []
        if state.override and not Override(**state.override).active(now):
            expired = Override(**state.override)
            state.override = None
            events.append(self._rate_event(state, now, f"The manual rate of {round(expired.rate * 100)}% set by "
                                                       f"{expired.set_by} expired."))
        if len(self.specs) == 1:
            state, raised = raise_for_version(self.program, state, self._primary_version())
            if raised:
                events.append(self._rate_event(state, now, state.reason))
        else:
            # Every output is watched: a new version of any one raises the rate.
            state, output = raise_for_versions(self.program, state,
                                               {cid: self.versions[cid].version for cid in self.specs})
            if output is not None:
                events.append(self._rate_event(state, now, state.reason, output=output))
        decided = self.root_storage.decision_count()
        while state.checked_windows < decided // self.program.window:
            window = state.checked_windows + 1
            evidence = self._window_evidence(window)
            state, change = check_window(self.program, state, evidence)
            if change:
                events.append(self._rate_event(state, now, state.reason, evidence=evidence, window=window,
                                               **({"output": evidence["output"]} if "output" in evidence else {})))
        with self.root_storage.transaction():
            self.root_storage.put_meta('review_program_state',_json(state.to_json()))
            appended = [self._append(event) for event in events]
        for event in appended:
            self.observer(event)
        return current_rate(self.program, state, now), decided

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
        with self.root_storage.transaction():
            self.root_storage.put_meta('review_program_state',_json(state.to_json()))
            event = self._append(self._rate_event(state, now, f"Manual rate of {round(rate * 100)}% set by {set_by}."))
        self.observer(event)

    def clear_review_rate(self, *, set_by: str) -> None:
        """End an operator override; the program's own rate applies again."""
        state = self._program_state()
        if not state.override:
            return
        state.override = None
        now = datetime.now(timezone.utc)
        with self.root_storage.transaction():
            self.root_storage.put_meta('review_program_state',_json(state.to_json()))
            event = self._append(self._rate_event(state, now, f"Manual rate cleared by {set_by}."))
        self.observer(event)

    def _program_state(self) -> ProgramState:
        row = self.root_storage.meta('review_program_state')
        return ProgramState.from_json(json.loads(row[0]) if row else None)

    def _rate_event(self, state: ProgramState, now: datetime, reason: str, **details) -> dict:
        return {"kind": "review-rate-changed", "state": state_name(self.program, state, now),
                "rate": current_rate(self.program, state, now), "reason": reason, **details}

    def _primary(self) -> ClassifierSpec:
        return self.definition.classifiers[0]

    def _primary_version(self) -> int:
        return self.versions[self._primary().id].version

    def _window_evidence(self, window: int) -> dict:
        """One finished window's evidence: the only output's, or the worst of every output's."""
        evidence = {cid: window_evidence(self.program, self._window_reviews(window, cid), self._gap_points(cid))
                    for cid in self.specs}
        return evidence[self._primary().id] if len(evidence) == 1 else combine_evidence(evidence)

    def _gap_points(self, classifier: str | None = None) -> int | None:
        spec = self.specs[classifier] if classifier else self._primary()
        return status_from_flywheel(self.wheels[spec.id], cyclotron_id=self.definition.id,
                                    positive_label=spec.positive_label,
                                    window=self.program.calibration_window).calibration.gap_points

    def _window_reviews(self, window: int, classifier: str | None = None) -> list[dict]:
        """Program and audit reviews of one output's decisions in one finished window."""
        cid = classifier or self._primary().id
        size = self.program.window
        rows = []
        for decision_id, payload in self.root_storage.window_decisions(size,(window - 1) * size):
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
        decided = self.root_storage.decision_count()
        until = self.program.window - decided % self.program.window
        week_ago = (now - timedelta(days=7)).isoformat()
        decisions = []
        for (payload,) in self.root_storage.decision_payloads():
            decision = json.loads(payload)
            if decision["createdAt"] >= week_ago:
                values = [r["confidence"] for r in decision["classifiers"].values()]
                # Routed decisions draw per output; older ones were selected as a whole on the lowest confidence.
                routed = "outputs" in decision["review"]
                decisions.append(tuple(values) if routed else (None if None in values else min(values),))
        rate = current_rate(self.program, state, now)
        return ReviewRate(state_name(self.program, state, now), rate,
                          (f"Manual rate of {round(override.rate * 100)}% set by {override.set_by}."
                           if active else state.reason),
                          audit_floor=self.program.audit_share, confidence_threshold=self.program.confidence_threshold,
                          override=ReviewRateOverride(override.rate, override.expires_at, override.set_by, override.set_at)
                          if active else None,
                          next_check_after_decisions=until,
                          next_step=next_step(self.program, state, decisions_until_check=until, now=now),
                          expected_reviews_per_week=expected_item_reviews(self.program, rate, decisions))

    def _insert_decision(self, decision: Decision, fingerprint: str) -> None:
        with self.root_storage.transaction():
            self.root_storage.insert_decision((decision.decision_id, decision.item_id, fingerprint, 'open', _json(decision.to_json())))
            self._record_batches(decision.decision_id)
            event = self._append({"kind": "decision", "decision": decision.to_json()})
        self.observer(event)

    @property
    def _batches(self) -> list:
        """Paid batches of the current operation. Each task keeps its own list, so a decision or
        review made while a learning pass awaits its optimizer never takes or drops the pass's batches."""
        batches = self._batch_log.get()
        if batches is None:
            batches = []
            self._batch_log.set(batches)
        return batches

    @_batches.setter
    def _batches(self, value: list) -> None:
        self._batch_log.set(list(value))

    def _on_batch(self, event: Mapping[str, Any]) -> None:
        """A paid shared decision-model request; cached answers cost nothing."""
        if event.get("kind") == "shared-decision-batch" and not event.get("cached"):
            self._batches.append((*_usage_counts(event.get("usage")), event.get("output_shares")))

    def _record_batches(self, decision_id: str) -> None:
        """Attribute the paid requests of this call to its decision (caller's transaction)."""
        for prompt, cached, output, shares in self._batches:
            row = self.root_storage.insert_usage(decision_id,'decision_model',prompt,cached,output,'shared-batch')
            for classifier, share in (shares or {}).items():
                self.root_storage.insert_usage_share(row,classifier,share)
        self._batches = []

    def usage(self, prices: Mapping[str, Mapping[str, float]], *, window: int = 100) -> dict[str, Any]:
        """Measured model usage and list-price cost per window of decisions, and in total.

        ``prices`` gives ``decision_model`` and ``optimizer`` list prices in USD
        per million tokens (``input_usd_per_mtok``, optional
        ``cached_input_usd_per_mtok``, ``output_usd_per_mtok``). Window 1 holds
        decisions 1 to ``window`` in the order they were made; a decision-model
        request made while reviewing or learning counts toward the decision it
        served. The shape matches ``run_usage.usage_by_window``.
        """
        from .run_usage import _block, _empty
        if type(window) is not int or window < 1:
            raise ValueError("window must be a positive integer")
        if set(prices) != {"decision_model", "optimizer"}:
            raise ValueError("prices must give decision_model and optimizer list prices")
        position = {decision_id: index for index, (decision_id,) in
                    enumerate(self.root_storage.decision_ids())}
        times = [json.loads(payload)["createdAt"] for (payload,) in self.root_storage.decision_payloads()]
        windows: dict[int, dict] = {}
        total = {"decision_model": _empty(), "optimizer": _empty()}
        for decision_id, model, prompt, cached, output in self.root_storage.usage_counts():
            index = position.get(decision_id)
            targets = [total[model]]
            if index is not None:
                targets.append(windows.setdefault(index // window, {"decision_model": _empty(), "optimizer": _empty()})[model])
            for counts in targets:
                counts["requests"] += 1
                counts["input_tokens"] += prompt
                counts["cached_input_tokens"] += cached
                counts["output_tokens"] += output
        blocks = []
        for index in range((len(times) + window - 1) // window):
            first, last = index * window, min(len(times), (index + 1) * window) - 1
            models = windows.get(index, {"decision_model": _empty(), "optimizer": _empty()})
            blocks.append({"window": index + 1, "first_cycle": first + 1, "last_cycle": last + 1,
                           **_block(models, prices, times[first], times[last])})
        return {"window_size": window, "windows": blocks,
                "total": _block(total, prices, times[0], times[-1]) if times else _block(total, prices),
                "outputs": self._usage_by_output(prices)}

    def _usage_by_output(self, prices: Mapping[str, Mapping[str, float]]) -> dict[str, Any]:
        """An estimated split of the total by output; the outputs sum to the total.

        Optimizer calls are measured per output (each output's learning makes
        its own calls). A shared decision-model request is split by each
        output's share of the request bytes; a request recorded before shares
        were kept is split evenly.
        """
        from .run_usage import _block, _empty
        ids = list(self.specs)
        per = {cid: {"decision_model": _empty(), "optimizer": _empty()} for cid in ids}
        shares: dict[int, dict[str, float]] = {}
        for usage_id, classifier, share in self.root_storage.usage_shares():
            shares.setdefault(usage_id, {})[classifier] = share
        for usage_id, model, prompt, cached, output, source in self.root_storage.usage_by_output():
            if model == "optimizer":
                owner = (source or "").rsplit(":", 1)[0]
                split = {owner: 1.0} if owner in per else {cid: 1 / len(ids) for cid in ids}
            else:
                split = {cid: share for cid, share in shares.get(usage_id, {}).items() if cid in per}
                if not split:
                    split = {cid: 1 / len(ids) for cid in ids}
                scale = sum(split.values())
                split = {cid: share / scale for cid, share in split.items()}
            for cid, share in split.items():
                counts = per[cid][model]
                counts["requests"] += share
                counts["input_tokens"] += prompt * share
                counts["cached_input_tokens"] += cached * share
                counts["output_tokens"] += output * share
        return {"estimate": ("Decision-model usage is split by each output's share of the shared request bytes "
                             "(evenly for requests recorded before shares were kept); optimizer usage is measured "
                             "per output. Outputs sum to the total."),
                "by_output": {cid: _block(per[cid], prices) for cid in ids}}

    def decision_log(self) -> list[dict[str, Any]]:
        """Every decision in the order it was made, with its item values, state and active reviews (for recordings)."""
        rows = []
        values = self._item_values()
        for number, (decision_id, state, payload) in enumerate(
                self.root_storage.decision_rows(), start=1):
            reviews = {}
            for cid in self.specs:
                review = self._active_review(decision_id, cid)
                last = self._last_review_kind(decision_id, cid)
                if review is not None:
                    reviews[cid] = review.to_json()
                elif last == "no-label":
                    reviews[cid] = {"kind": "no-label"}
            decision = json.loads(payload)
            rows.append({"n": number, "state": state, "decision": decision, "item": values.get(decision["itemId"], {}),
                         "reviews": reviews})
        return rows

    def decision(self, decision_id: str) -> Decision:
        row = self.root_storage.decision(decision_id)
        if row is None:
            raise KeyError(f"unknown decision {decision_id}")
        return Decision.from_json(json.loads(row[0]))

    # Review ---------------------------------------------------------------

    async def review(self, decision_id: str, label: str | None = _NO_LABEL, *, classifier: str | None = None,
                     labels: Mapping[str, str | None] | None = None,
                     explanation: str | None = None, reason_code: str | None = None, reviewer: str | None = None,
                     selected_by: str | None = None, review_id: str | None = None, _replay: bool = False,
                     execution_guard: ExecutionGuard | None = None):
        """Record a reviewer's label for a decision, or one checklist of labels for several outputs.

        The first label for a decision teaches the cyclotron; a later label is
        a correction. ``label=None`` records a review without a label (for
        example a duplicate), which closes the decision without teaching.
        ``selected_by`` says who chose to review it: ``program`` or ``audit``
        (the decision was sent to review; the default when it was) or
        ``reviewer`` (a person chose it; the default when it was not sent). A
        reviewer's own choice trains the ML model but never counts as
        alignment or audit evidence.
        Pass the application's own ``review_id`` to make resubmission safe.

        ``labels={classifier: label, ...}`` saves one review of several
        outputs and returns ``{classifier: Review}``. Every entry is validated
        before anything is recorded; outputs left out stay open for review.
        Each output gets its own review (``<review_id>:<classifier>``) and
        review event, every label is recorded before any learning, and then
        each labeled output runs its learning pass once. Resubmitting the
        same ``review_id`` returns the recorded reviews and completes any that
        an interruption left unrecorded.
        """
        if labels is not None:
            if label is not _NO_LABEL or classifier is not None:
                raise ValueError("pass labels for several outputs, or label and classifier for one")
            if not isinstance(labels, Mapping) or not labels:
                raise ValueError("labels maps classifier ids to labels")
        elif label is _NO_LABEL:
            raise ValueError("review needs a label (None to close without one) or labels")
        async with self.execution(execution_guard=execution_guard):
            self._sync()
            self._batches = []
            decision = self.decision(decision_id)
            if labels is None:
                entries = {self._classifier(decision, classifier): label}
                ids = {cid: review_id or str(uuid4()) for cid in entries}
            else:
                if any(cid not in decision.results for cid in labels):
                    raise ValueError("classifier not in this cyclotron")
                base = review_id or str(uuid4())
                entries = {cid: labels[cid] for cid in self.specs if cid in labels}
                ids = {cid: f"{base}:{cid}" for cid in entries}
            if selected_by is not None and selected_by not in SELECTED_BY:
                raise ValueError(f"selected_by must be one of {SELECTED_BY}")
            chosen_by = {}
            for cid in entries:
                own = decision.review.for_output(cid)
                chosen_by[cid] = selected_by or (own.reason if own.selected else "reviewer")
                if chosen_by[cid] != "reviewer" and not own.selected and not _replay:
                    raise ValueError("this decision was not sent to review"
                                     + (f" for {cid}" if decision.review.outputs else "")
                                     + "; a review of it is selected_by='reviewer'")
            for name, value in (("explanation", explanation), ("reason_code", reason_code), ("reviewer", reviewer)):
                if value is not None and not isinstance(value, str):
                    raise ValueError(f"{name} must be text")
            explanation = (explanation.strip() or None) if explanation else None
            target = Item(decision.item_id, self._item_values()[decision.item_id])
            # Validate every entry before recording any of them.
            plans = []
            for cid, value in entries.items():
                if value is not None:
                    value = self.specs[cid].task.validate_label(value)
                existing = self._review(ids[cid])
                if existing is not None:
                    same = (existing.decision_id, existing.classifier, existing.label, existing.reason_code) == (
                        decision_id, cid, value, reason_code) and (existing.explanation or None) == self._redacted(cid, explanation)
                    if not same:
                        raise ValueError("review identity has different content")
                    continue
                active = self._active_review(decision_id, cid)
                if value is None and active is not None:
                    raise ValueError("this decision already has a label; undo it before closing it without one")
                plans.append((cid, value, active))
            warnings: dict[str, list] = {cid: [] for cid in entries}
            learning = {}
            try:
                for cid, value, active in plans:
                    cycle = self._record_review(decision, target, cid, value, active, ids[cid], explanation,
                                                reason_code, reviewer, chosen_by[cid])
                    if cycle is not None:
                        learning[cid] = cycle
                # Every label is recorded before any output learns, so each
                # learning pass (and its sibling check) sees the whole review.
                for cid in list(learning):
                    cycle = learning[cid]
                    if self.learning == "deferred" and self.optimizer is not None:
                        # Queue the pass; learn() runs it after the review returns.
                        self._enqueue_learning(cid)
                    else:
                        warnings[cid] = await self._learn(cid, cycle)
                    record_cycle_metrics(self.wheels[cid], self._class_config(self.specs[cid]))
                    cycle.__exit__(None, None, None)
                    del learning[cid]
            except Exception as error:
                for cycle in learning.values():
                    cycle.__exit__(type(error), error, None)
                raise
            self._sync()
            with self.root_storage.transaction():
                self._record_batches(decision_id)
            reviews = {cid: replace(self._review(ids[cid]), optimization_warnings=tuple(warnings[cid]))
                       for cid in entries}
            return reviews if labels is not None else next(iter(reviews.values()))

    def _record_review(self, decision, target, cid, label, active, review_id, explanation, reason_code, reviewer,
                       selected_by):
        """Record one output's review; return its open cycle when a new label needs a learning pass."""
        wheel, spec = self.wheels[cid], self.specs[cid]
        annotations = {"decision_id": decision.decision_id, "review_id": review_id, "selected_by": selected_by,
                       "reason_code": reason_code, "reviewer": reviewer}
        if label is None:
            cycle = wheel.resume_cycle(target) or wheel.cycle(target, reason="review").__enter__()
            wheel._emit({"kind": "human-skipped", "target_id": target.id, "explanation": explanation, **annotations})
            cycle.__exit__(None, None, None)
            return None
        if active is None:
            cycle = wheel.resume_cycle(target) or wheel.cycle(target, reason="review").__enter__()
            try:
                propensity = decision.review.for_output(cid).propensity if selected_by != "reviewer" else 1.0
                wheel.record_feedback_event(FeedbackItem(
                    review_id, target.id, cid, initial_answer_value=decision.results[cid].label,
                    final_answer_value=label, edit_comment_value=explanation, label_source=LABEL_SOURCE_VETTED,
                    selection_propensity=propensity, review_provenance=f"{selected_by}-selected-review"),
                    assignment=REVIEWER_SELECTED if selected_by == "reviewer" else review_role(self.definition.seed, target.id),
                    annotations=annotations)
            except Exception as error:
                cycle.__exit__(type(error), error, None)
                raise
            return cycle
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
        return None

    async def undo_review(self, decision_id: str, *, classifier: str | None = None,
                          review_id: str | None = None, execution_guard: ExecutionGuard | None = None) -> Review:
        """Retract the active label for a decision and wait for a new review."""
        async with self.execution(execution_guard=execution_guard):
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

    def overview(self) -> CyclotronOverview:
        """Every output's status, the worst output, and the output limiting the review rate. No model call."""
        statuses = tuple(self.status(cid) for cid in self.specs)
        last = self._program_state().last_check if self.program is not None else None
        return CyclotronOverview(self.definition.id, statuses[0].review_rate, statuses, worst_output(statuses),
                                 (last or {}).get("output"))

    def status(self, classifier: str | None = None) -> CyclotronStatus:
        """The cyclotron-status/v1 snapshot for one classifier. No model call."""
        if classifier is None:
            if len(self.specs) != 1:
                raise ValueError("name the classifier; this cyclotron has several (overview() shows them all)")
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
        rows = self.root_storage.events(after,limit)
        return {"events": [{**json.loads(payload), "cursor": event_id} for event_id, payload in rows],
                "cursor": rows[-1][0] if rows else after}

    def labels(self) -> list[dict[str, Any]]:
        """Every active label with its item: what an application can replay into a new store."""
        values = self._item_values()
        rows = []
        for decision_id, item_id in self.root_storage.decision_items():
            for cid in self.specs:
                review = self._active_review(decision_id, cid)
                if review is not None:
                    rows.append({"item": Item(item_id, values[item_id]), "classifier": cid, "label": review.label,
                                 "explanation": review.explanation, "reason_code": review.reason_code,
                                 "reviewer": review.reviewer, "selected_by": review.selected_by,
                                 "review_id": review.review_id})
        return rows

    async def replay(self, labels: Sequence[Mapping[str, Any]], *,
                     execution_guard: ExecutionGuard | None = None) -> int:
        """Rebuild learning from an application's recorded labels, in order.

        Each record needs ``item`` and ``label``, and may name the classifier,
        explanation, reason code, reviewer, selected_by and review_id. Items are
        decided again, so a new store makes decision-model calls.
        """
        count = 0
        for record in labels:
            decision = await self.decide(record["item"], execution_guard=execution_guard)
            await self.review(decision.decision_id, record["label"], classifier=record.get("classifier"),
                              explanation=record.get("explanation"), reason_code=record.get("reason_code"),
                              reviewer=record.get("reviewer"), selected_by=record.get("selected_by"),
                              review_id=record.get("review_id"), _replay=True, execution_guard=execution_guard)
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
            cursor_row = self.root_storage.meta(f'cursor:{cid}')
            cursor = int(cursor_row[0]) if cursor_row else 0
            appended = []
            while True:
                page = wheel.trace_events(after_event_id=self._seen[cid], limit=1000)
                if not page["events"]:
                    break
                previous_tracker=deepcopy(self.versions[cid])
                page_events=[]
                next_cursor=max(cursor,page['cursor'])
                try:
                    with self.root_storage.transaction():
                        for event in page['events']:
                            change=self.versions[cid].feed(event)
                            if event['event_id']>cursor:
                                page_events.extend(self._derive(cid,event,change))
                        if next_cursor>cursor:
                            self.root_storage.put_meta(f'cursor:{cid}',str(next_cursor))
                except BaseException:
                    # A rolled-back derived page must also roll back the
                    # in-memory source position/version before an in-process retry.
                    self.versions[cid]=previous_tracker
                    raise
                self._seen[cid]=page['cursor']
                cursor=next_cursor
                appended.extend(page_events)
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
            self.root_storage.insert_review(review.review_id,review.decision_id,classifier,_json(self._review_payload(review)))
            requested = self._requested_outputs(review.decision_id)
            reviewed = all(self._last_review_kind(review.decision_id, cid) in ("label", "correction", "no-label")
                           for cid in requested)
            self.root_storage.set_decision_state(review.decision_id,'reviewed' if reviewed else 'open',unless_superseded=True)
            return [self._append({"kind": "review", "review": review.to_json()})]
        if kind == "optimizer-response":
            # A deferred learning pass has no item; it counts toward the latest decision.
            row = (self.root_storage.latest_id(event['cycle_item_id']) if event.get("cycle_item_id")
                   else self.root_storage.latest_id())
            if row is not None:
                prompt, cached, output = _usage_counts(event.get("usage"))
                self.root_storage.insert_usage(row[0],'optimizer',prompt,cached,output,f"{classifier}:{event.get('event_id')}")
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
        row = self.root_storage.append_event(_json(event))
        return {**event, "cursor": row}

    # Internals ------------------------------------------------------------

    def _requested_outputs(self, decision_id: str) -> tuple[str, ...]:
        """The outputs a decision is waiting on: those sent to review, or every output."""
        row = self.root_storage.decision(decision_id)
        requested = _selection(json.loads(row[0])["review"]).requested if row else None
        return tuple(requested) if requested else tuple(self.specs)

    def _redacted(self, classifier: str, text: str | None) -> str | None:
        if text is None:
            return None
        for secret in self.wheels[classifier].redact:
            text = text.replace(secret, "[REDACTED]")
        return text

    def _partitions(self, classifier: str):
        training, development, protected = feedback_partitions(self.wheels[classifier], self._item_values(), self.definition.seed)
        return (*self.priming_examples.get(classifier,()),*training), development, protected

    def _item_values(self) -> dict[str, dict]:
        return {item_id: json.loads(values) for item_id, values in
                self.root_storage.items()}

    def _item_fingerprint(self, decision_id: str) -> str:
        return self.root_storage.decision_fingerprint(decision_id)

    def _latest_decision(self, item_id: str):
        row = self.root_storage.latest_decision(item_id)
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
        row = self.root_storage.review(review_id)
        return None if row is None else self._load_review(row[0])

    def _active_review(self, decision_id: str, classifier: str) -> Review | None:
        """The latest labeled review that has not been undone, if any."""
        active = None
        for (payload,) in self.root_storage.reviews(decision_id,classifier):
            review = self._load_review(payload)
            if review.kind in ("label", "correction"):
                active = review
            elif review.kind == "undo":
                active = None
        return active

    def _last_review_kind(self, decision_id: str, classifier: str) -> str | None:
        row = self.root_storage.last_review(decision_id,classifier)
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

    def _optimizer_for(self, optimizer, classifier: str):
        """Each output's own optimizer copy, lending its provider calls the deferred-learning window."""
        if optimizer is None:
            return None
        own = copy(optimizer)
        if hasattr(own, "complete"):
            own.call_window = lambda: self._provider_window(classifier)
        return own

    @asynccontextmanager
    async def _provider_window(self, classifier: str):
        """Release the store lock while learn() awaits an asynchronous optimizer provider call.

        Only a deferred learning pass lends its call; elsewhere this does
        nothing. While the call is in flight, reviews and decisions run, and
        their events are written on the event-loop thread as usual (so they
        can appear in the history between a pass's own events). Afterwards
        the lock is taken back even if the pass is being cancelled, and the
        pass gets back its cycle and its shared decision context. If any
        output's version changed meanwhile, or a label the pass rests on was
        corrected or undone, the pass is superseded: it unwinds without
        caching anything and stays queued.
        """
        state = self._pass_state.get()
        if self._learning.get() != classifier or state is None or state["hold_lock"]:
            yield
            return
        wheel = self.wheels[classifier]
        # Criteria, not ML models: the pass refits ML models itself, so a refit meanwhile is no conflict.
        criteria = {cid: other.active.config.fingerprint for cid, other in self.wheels.items()}
        flags = (wheel._cycle_running, wheel._step_running)
        binding = self.shared.snapshot()
        lent = self._row_marks(wheel)
        wheel._cycle_running = wheel._step_running = False
        self._lock.release()
        try:
            yield
        finally:
            cancelled = None
            while True:
                try:
                    await self._lock.acquire()
                    break
                except asyncio.CancelledError as error:  # get the lock back before unwinding
                    cancelled = error
            wheel._cycle_running, wheel._step_running = flags
            self.shared.restore(binding)
            # Rows other operations wrote while the lock was lent are theirs, never the pass's to remove.
            state["lent"].append((lent, self._row_marks(wheel)))
            if cancelled is not None:
                raise cancelled
        reason = self._superseded(classifier, criteria)
        if reason:
            raise OptimizationSuperseded(reason)

    def _superseded(self, classifier: str, criteria: Mapping[str, str]) -> str | None:
        """Why a pass's state no longer holds after its optimizer call, or None."""
        changed = [cid for cid, other in self.wheels.items() if other.active.config.fingerprint != criteria[cid]]
        if changed:
            return "new criteria for " + ", ".join(changed) + " were activated during the optimizer call"
        covered = self._pass_state.get()["evidence"]
        if covered:
            training, development, _ = self._partitions(classifier)
            current = self.wheels[classifier]._evidence((*training, *development))
            if any(current.get(key) != value for key, value in covered.items()):
                return "a label this pass rests on was corrected or undone during the optimizer call"
        return None

    def _row_marks(self, wheel) -> dict[str, int]:
        """The highest row id of each table a pass can leave pending work in."""
        marks = wheel.pending_storage.marks()
        marks["shared:batches"] = self.shared.storage.operation_mark()
        return marks

    def _drop_pending(self, wheel, state) -> None:
        """Remove pending work the stopped pass itself started, so its next pass needs no retry authorization.

        Rows other operations wrote while the pass lent the store lock, and
        older interrupted work, keep their explicit-retry guard. Dropping the
        guard on the pass's own interrupted decision-model request is the one
        exception to "no silent repayment": it is recorded as
        ``decision-request-abandoned``, because asking again may be charged again.
        """
        start = state["start"]

        def own(table, rowid):
            return rowid > start.get(table, 0) and not any(
                before.get(table, 0) < rowid <= after.get(table, 0) for before, after in state["lent"])
        for kind, rowid, key in wheel.pending_storage.pending():
            if not own(kind, rowid):
                continue
            if kind == "runtime_answers":
                wheel._emit({"kind": "decision-request-abandoned", "request_fingerprint": key,
                             "reason": "the learning pass that made this request stopped before its answer was recorded",
                             "repeat_charge_possible": True})
            wheel.pending_storage.discard(kind, rowid, key)
        for rowid, key, owner in self.shared.storage.pending_operations():
            if own("shared:batches", rowid):
                wheel._emit({"kind": "decision-request-abandoned", "request_fingerprint": key, "shared_batch": True,
                             "reason": "the learning pass that made this request stopped before its answer was recorded",
                             "repeat_charge_possible": True})
                self.shared.storage.abandon_learning(key, owner)

    def _enqueue_learning(self, classifier: str) -> None:
        """Queue one output's learning pass; queued reviews of one output share a pass."""
        training, development, _ = self._partitions(classifier)
        with self.root_storage.transaction():
            # The earliest unlearned count is kept, so the pass sees every cadence boundary crossed since.
            self.root_storage.enqueue_learning(classifier,len(training) + len(development) - 1,_now())

    def pending_learning(self) -> list[str]:
        """Outputs with a queued learning pass, oldest first."""
        return self.root_storage.pending_learning()

    async def learn(self, *, limit: int | None = None, execution_guard: ExecutionGuard | None = None) -> list[dict[str, Any]]:
        """Run queued learning passes, one output at a time, oldest first.

        With ``learning="deferred"``, ``review()`` records labels and returns;
        an application runs this afterwards (for example from a background
        task). The store lock is held for one output's pass at a time, so
        reviews and decisions interleave between outputs, and, with an
        asynchronous optimizer transport, during each provider call. Each
        output's optimizer stages share the cyclotron's optimizer and its
        call limit. Returns one record per pass: the output, its warnings and
        duration, and ``superseded`` when its state changed during a provider
        call. A pass stays queued when labels arrived during it, when it was
        superseded, or when it raised or was cancelled.
        A guarded pass retains ownership through provider calls; its per-call guard
        runs before reading state on each separately owned pass.
        """
        if limit is not None and (type(limit) is not int or limit < 1):
            raise ValueError("limit must be a positive integer")
        done, ran = [], set()
        while limit is None or len(done) < limit:
            async with self._learn_lock, self.execution(execution_guard=execution_guard):
                rows = self.root_storage.learning_rows()
                row = next((row for row in rows if row[1] not in ran), None)
                if row is None:
                    break
                seq, cid, since, superseded_before = row
                ran.add(cid)
                self._sync()
                self._batches = []
                wheel = self.wheels[cid]
                training, development, _ = self._partitions(cid)
                covered, reviews = len(training) + len(development), self._review_count(cid)
                hold_lock = execution_guard is not None or superseded_before >= self.max_superseded_passes
                if hold_lock and execution_guard is None:
                    wheel._emit({"kind": "learning-window-withheld", "superseded_passes": superseded_before,
                                 "limit": self.max_superseded_passes,
                                 "reason": "repeatedly superseded; this pass keeps the store lock through its optimizer calls"})
                state = {"evidence": wheel._evidence((*training, *development)), "start": self._row_marks(wheel),
                         "lent": [], "hold_lock": hold_lock}
                started = time.perf_counter()
                cycle = wheel.cycle(None, reason="deferred-learning").__enter__()
                tokens = (self._learning.set(cid), self._pass_state.set(state))
                superseded = None
                try:
                    warnings = await self._learn(cid, cycle, since_count=since)
                except OptimizationSuperseded as error:
                    superseded, warnings = str(error), []
                except BaseException as error:
                    # Failure or cancellation: close the cycle so decisions keep working, keep the pass queued.
                    cycle.__exit__(type(error), error, None)
                    self._drop_pending(wheel, state)
                    self._record_pass_batches()
                    raise
                finally:
                    self._learning.reset(tokens[0])
                    self._pass_state.reset(tokens[1])
                cycle.__exit__(None, None, None)
                self._record_pass_batches()
                record = {"classifier": cid, "since_count": since, "warnings": warnings,
                          "duration_ms": round((time.perf_counter() - started) * 1000, 2)}
                if superseded:
                    self._drop_pending(wheel, state)
                    record.update(superseded=True, reason=superseded)
                    wheel._emit({"kind": "deferred-learning-superseded", **record,
                                 "superseded_passes": superseded_before + 1})
                    with self.root_storage.transaction():
                        self.root_storage.supersede_learning(seq)
                else:
                    wheel._emit({"kind": "deferred-learning-completed", **record})
                    self._sync()
                    with self.root_storage.transaction():
                        if self._review_count(cid) > reviews:
                            # Reviews arrived during the pass (any kind, held-out ones too): run again from here.
                            self.root_storage.requeue_learning(seq, covered)
                        else:
                            self.root_storage.delete_learning(seq)
                self._sync()
                done.append(record)
        return done

    def _review_count(self, classifier: str) -> int:
        return self.root_storage.review_count(classifier)

    def _record_pass_batches(self) -> None:
        latest = self.root_storage.latest_id()
        if latest is not None:
            with self.root_storage.transaction():
                # Decision-model requests made while learning count toward the latest decision.
                self._record_batches(latest[0])

    async def _learn(self, classifier: str, cycle, *, since_count: int | None = None) -> list:
        if self.optimizer is None:
            # Without an LLM optimizer the cyclotron decides and records only.
            self._reconcile(classifier)
            return []
        from .observability import StepFailed
        try:
            return await learn_from_review(self.wheels, self.shared, classifier, cycle, self._partitions,
                                           optimize_every=self.definition.optimize_every,
                                           rubric_changes_every=self.definition.rubric_changes_every,
                                           **({"since_count": since_count} if since_count is not None else {}))
        except (StepFailed, RuntimeError) as error:
            warning = {"classifier_id": classifier, "reason": "optimization failed; labels retained, no automatic retry",
                       "error_type": type(error).__name__}
            self.wheels[classifier]._emit({"kind": "optimization-unavailable", **warning})
            return [warning]

    @staticmethod
    def _class_config(spec: ClassifierSpec) -> list[dict]:
        return [{"label": label, **({"role": "positive"} if label == spec.positive_label else {})}
                for label in spec.labels]

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
import hashlib
import inspect
import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from contextlib import asynccontextmanager, nullcontext
from contextvars import ContextVar
from copy import copy, deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .calibration_history import REVIEW_BINDING_EVENT_KINDS, reviewed_prediction_bindings
from .classifier_config import ClassifierConfig
from .classifier_set import (
    ADDED,
    CLASSIFIER_SET_CHANGES_KEY,
    RETIRED,
    change_record,
    definition_matches_store,
    last_known_specs,
    recorded_definition,
    retired_classifier_ids,
    stale_definition_error,
    stored_classifier_set_changes,
)
from .concurrent_calls import call_serially
from .cyclotron_status import (
    CyclotronOverview,
    CyclotronStatus,
    VersionTracker,
    initial_snapshot,
    status_from_flywheel,
    worst_output,
)
from .deferred_learning import DeferredLearning
from .embedded_records import (
    ClassifierDecision,
    ClassifierSpec,
    CyclotronDefinition,
    Decision,
    ExecutionGuard,
    FileLease,
    Review,
    ReviewSelection,
    StoreLocked,
    _json,
    _now,
    _selection,
)
from .feedback import LABEL_SOURCE_VETTED, FeedbackItem
from .flywheel import DecisionFlywheel
from .held_out_evaluation import HeldOutEvaluation
from .learning_loop import (
    REVIEWER_SELECTED,
    decide_with_shared_context,
    feedback_partitions,
    labeled_partitions,
    learn_from_review,
    record_cycle_metrics,
    review_role,
)
from .models import DecisionTask, Item, LabeledItem
from .review_program import REASONS, ProgramState, ReviewProgram
from .review_rate_controller import ReviewRateController
from .run_usage import _usage as _usage_counts
from .run_usage import stored_usage_by_window
from .shared_decisions import SharedDecisions
from .sibling_check import install_sibling_checks
from .store_seeding import (
    StoreSeeds,
    apply_priming_additions,
    apply_priming_retractions,
    normalize_supporting_questions,
    priming_includes,
    retracted_example_ids,
    stored_priming_additions,
    stored_priming_retractions,
)
from .store_snapshot import classifier_store_name, restore_store_snapshot, store_paths, write_store_snapshot

__all__ = [
    "ClassifierDecision",
    "ClassifierSpec",
    "Cyclotron",
    "CyclotronDefinition",
    "Decision",
    "ExecutionGuard",
    "FileLease",
    "Review",
    "ReviewSelection",
    "StoreLocked",
]
SELECTED_BY = REASONS
LEARNING_MODES = ("inline", "deferred")
_NO_LABEL = object()  # review() was called without a single label


class Cyclotron:
    """An embedded cyclotron backed by a directory of SQLite files."""

    def __init__(
        self,
        directory,
        definition: CyclotronDefinition,
        model,
        optimizer=None,
        *,
        max_requests: int | None = 100,
        redact: Sequence[str] = (),
        observer: Callable[[dict], None] | None = None,
        lease=None,
        review_program: ReviewProgram | None = ReviewProgram(),
        seed_rubrics: Mapping[str, str] | None = None,
        priming_examples: Mapping[str, Sequence[LabeledItem]] | None = None,
        seed_supporting_questions: Mapping[str, Sequence[Mapping[str, Any] | DecisionTask]] | None = None,
        share_supporting_questions: bool | None = None,
        learning: str = "inline",
        max_superseded_passes: int = 3,
        storage=None,
    ):
        if not isinstance(definition, CyclotronDefinition):
            raise ValueError("definition must be a CyclotronDefinition")
        if max_requests is not None and (type(max_requests) is not int or max_requests < 1):
            raise ValueError("max_requests must be a positive integer or None")
        if review_program is not None and not isinstance(review_program, ReviewProgram):
            raise ValueError("review_program must be a ReviewProgram or None")
        seed_rubrics = dict(seed_rubrics or {})
        if set(seed_rubrics) - {spec.id for spec in definition.classifiers} or any(
            not isinstance(text, str) for text in seed_rubrics.values()
        ):
            raise ValueError("seed_rubrics maps classifier ids to rubric text")
        self.seed_rubrics = seed_rubrics
        from .priming_context import encode_priming

        self.priming_redaction = tuple(redact)
        self.requested_priming = (
            None
            if priming_examples is None
            else encode_priming(dict(priming_examples), {spec.id: spec.task for spec in definition.classifiers}, redact=redact)
        )
        self.seed_supporting_questions = normalize_supporting_questions(definition, seed_supporting_questions)
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
        self.storage = storage
        if getattr(storage, "requires_durable_optimizer", False):
            from .s3_optimizer_transport import require_durable_optimizer

            require_durable_optimizer(optimizer)
        if storage is not None and (directory is not None or lease is not None):
            raise ValueError("injected cloud stores cannot use a local directory or file lease")
        self.directory = Path(directory) if storage is None else None
        if self.directory is not None:
            self.directory.mkdir(parents=True, exist_ok=True)
        self.wheels: dict[str, DecisionFlywheel] = {}
        self.shared = None
        self.db = None
        # The lease comes first: nothing is opened, and no model is called,
        # while another writer holds the store.
        self.lease = storage.lease if storage is not None else lease if lease is not None else FileLease(self.directory)
        self.lease.acquire()
        try:
            self._open(model, optimizer, max_requests, redact)
        except Exception:
            self.close()
            raise
        self._lock = asyncio.Lock()
        # One learning pass at a time, even across concurrent learn() calls.
        self._learn_lock = asyncio.Lock()
        self.deferred_learning = DeferredLearning(self)

    def _open(self, model, optimizer, max_requests, redact):
        definition = self.definition
        if self.storage is None:
            from .embedded_storage import SQLiteEmbeddedStorage

            self.root_storage = SQLiteEmbeddedStorage(self.directory / "cyclotron.sqlite3")
        else:
            self.root_storage = self.storage.root_storage
        self.db = getattr(self.root_storage, "db", None)  # Native snapshot compatibility only.
        saved = self.root_storage.meta("definition")
        if saved:
            saved_definition = json.loads(saved[0])
            changes = stored_classifier_set_changes(self.root_storage)
            if not definition_matches_store(saved_definition, definition, changes):
                raise stale_definition_error(saved_definition, definition, changes)
        with self.root_storage.transaction():
            self.root_storage.insert_meta("definition", recorded_definition(definition), ignore=True)
        from .priming_context import decode_priming

        stored_priming = self.root_storage.meta("priming_examples")
        saved_priming = json.loads(stored_priming[0]) if stored_priming else {}
        if (
            stored_priming
            and self.requested_priming is not None
            and not priming_includes(saved_priming, self.requested_priming, retracted_example_ids(self.root_storage))
        ):
            raise ValueError("this store has different accepted priming; add new examples with add_priming_examples")
        if not stored_priming and self.requested_priming:
            if saved:
                raise ValueError("this store already exists; add priming examples with add_priming_examples")
            saved_priming = self.requested_priming
        with self.root_storage.transaction():
            self.root_storage.insert_meta("priming_examples", _json(saved_priming), ignore=True)
        self.priming_examples = decode_priming(saved_priming)
        seeds = StoreSeeds(
            self.root_storage,
            seed_rubrics=self.seed_rubrics,
            seed_supporting_questions=self.seed_supporting_questions,
            requested_sharing=self.requested_sharing,
        )
        self._batch_log: ContextVar[list | None] = ContextVar(f"cyclotron_batches_{id(self)}", default=None)
        self._batches = []
        self.shared = SharedDecisions(
            self.directory / "shared.sqlite3" if self.directory is not None else None,
            model,
            max_requests=0,
            observer=self._on_batch,
            share_supporting_questions=seeds.sharing(created=saved is None),
            storage=self.storage.batch_storage if self.storage is not None else None,
        )
        # None is reserved for explicitly uncapped shared workloads (hosted
        # publication runs); bounded offline callers retain per-open limits.
        self.shared.max_requests = (
            self.shared.requests + max_requests if self.storage is None and max_requests is not None else max_requests
        )
        self.specs = {spec.id: spec for spec in definition.classifiers}
        self.versions: dict[str, VersionTracker] = {}
        self._seen: dict[str, int] = {}
        self._seeds, self._optimizer, self._max_requests, self._redact = seeds, optimizer, max_requests, redact
        self._created = saved is None
        if self.directory is not None:
            (self.directory / "classifiers").mkdir(exist_ok=True)
        open_runtimes = getattr(self.storage, "open_classifier_runtimes", None)
        if open_runtimes is not None:
            # Acquire every output's runtime at once; each output then opens over its own.
            open_runtimes([spec.id for spec in definition.classifiers])
        for spec in definition.classifiers:
            self._open_classifier(spec)
        install_sibling_checks(self.wheels, self.shared, self._partitions, definition.sibling_policy)
        self.held_out = HeldOutEvaluation(self)
        self.review_rate = ReviewRateController(
            program=self.program,
            definition=definition,
            specs=self.specs,
            versions=self.versions,
            wheels=self.wheels,
            root_storage=self.root_storage,
            append_event=self._append,
            active_review=self._active_review,
            observer=self.observer,
            clock=lambda: datetime.now(timezone.utc),
        )
        self._apply_priming_retractions()
        self._apply_priming_additions()
        self._sync()

    def _open_classifier(self, spec: ClassifierSpec, *, created: bool | None = None) -> None:
        """Open one classifier's store and register it as active. Nothing is decided."""
        created = self._created if created is None else created
        path = None
        if self.directory is not None:
            path = store_paths(self.directory, replace(self.definition, classifiers=(spec,)))[classifier_store_name(spec.id)]
        primed_example_ids = tuple(row.item.id for row in self.priming_examples.get(spec.id, ()))
        added_example_ids = {key for addition in self._priming_additions() for key in addition["examples"].get(spec.id, ())}
        # A classifier's stored contract names the decision model before any shared request was made.
        request_identity, self.shared.identity = self.shared.identity, "unprepared"
        try:
            wheel = DecisionFlywheel(
                path,
                ClassifierConfig(
                    spec.task,
                    example_ids=tuple(key for key in primed_example_ids if key not in added_example_ids),
                    rubric=self._seeds.rubric(spec.id),
                    tasks=self._seeds.supporting_questions(spec, created=created),
                ),
                # Each output gets its own copy so its optimizer events reach only its own
                # trace; the copies share the transport, so the call limit stays cyclotron-wide.
                self.shared.adapter(spec.id),
                self._optimizer_for(self._optimizer, spec.id),
                max_requests=self._max_requests,
                redact=self._redact,
                selection_policy=spec.selection_policy,
                min_evaluation_per_class=2,
                storage=self.storage.runtime_for_classifier(spec.id) if self.storage is not None else None,
            )
        finally:
            self.shared.identity = request_identity
        wheel.initial = replace(wheel.initial, example_ids=primed_example_ids)
        self.specs[spec.id] = spec
        self.wheels[spec.id] = wheel
        self.versions[spec.id] = VersionTracker(initial_snapshot(wheel))
        self._seen[spec.id] = 0
        self._resume_tracker(spec.id)

    def _close_classifier(self, classifier: str) -> None:
        self.wheels.pop(classifier).close()
        if self.storage is not None:
            self.storage.release_classifier_runtime(classifier)
        del self.specs[classifier], self.versions[classifier], self._seen[classifier]

    def _adopt_classifier_set(self, classifiers: Sequence[ClassifierSpec]) -> None:
        """Make the engine's shared parts describe the classifiers that are now active."""
        self.definition = replace(self.definition, classifiers=tuple(classifiers))
        self.review_rate.definition = self.definition
        self.shared.unbind_context()
        install_sibling_checks(self.wheels, self.shared, self._partitions, self.definition.sibling_policy)

    # Classifier set -------------------------------------------------------

    async def add_classifiers(
        self,
        classifiers: Sequence[ClassifierSpec],
        *,
        added_by: str,
        seed_rubrics: Mapping[str, str] | None = None,
        execution_guard: ExecutionGuard | None = None,
    ) -> dict[str, Any]:
        """Add classifiers to an existing store. No model call.

        A new classifier starts unfitted, with its seed rubric and no labels;
        it learns only from reviews made after it was added, and the other
        classifiers keep their labels, rubrics and versions. The shared
        decision request now carries the new classifier, so an unreviewed
        decision is decided again with it. A retired classifier that is added
        again with the same specification is reactivated with the history and
        labels it had. Adding an active classifier again with the same
        specification changes nothing; a different specification is refused.
        """
        if not isinstance(added_by, str) or not added_by.strip():
            raise ValueError("added_by names who added the classifiers")
        if isinstance(classifiers, (str, bytes)) or not isinstance(classifiers, Sequence) or not classifiers:
            raise ValueError("add_classifiers needs a list of ClassifierSpec")
        if any(not isinstance(spec, ClassifierSpec) for spec in classifiers):
            raise ValueError("add_classifiers needs a list of ClassifierSpec")
        identifiers = [spec.id for spec in classifiers]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("classifiers to add must have distinct ids")
        seed_rubrics = dict(seed_rubrics or {})
        if set(seed_rubrics) - set(identifiers) or any(not isinstance(text, str) for text in seed_rubrics.values()):
            raise ValueError("seed_rubrics maps the ids of the added classifiers to rubric text")
        async with self._learn_lock, self.execution(execution_guard=execution_guard):
            changes = stored_classifier_set_changes(self.root_storage)
            previous = last_known_specs(changes)
            already_active, to_add, reactivated = [], [], []
            for spec in classifiers:
                if spec.id in self.specs:
                    if self.specs[spec.id] != spec:
                        raise ValueError(f"classifier {spec.id!r} is already active with a different specification")
                    already_active.append(spec.id)
                    continue
                if spec.id in previous and previous[spec.id] != spec:
                    raise ValueError(
                        f"classifier {spec.id!r} was retired with a different specification; "
                        "a retired classifier keeps its labels and is added again with the specification it had"
                    )
                if spec.id in previous:
                    reactivated.append(spec.id)
                to_add.append(spec)
            if to_add:
                for spec in to_add:
                    if spec.id in seed_rubrics:
                        self._seeds.seed_rubrics[spec.id] = seed_rubrics[spec.id]
                opened = []
                try:
                    for spec in to_add:
                        self._open_classifier(spec, created=True)
                        opened.append(spec.id)
                    changes.append(
                        change_record(
                            ADDED, [spec.id for spec in to_add], to_add, added_by.strip(), _now(), reactivated=reactivated
                        )
                    )
                    definition = replace(self.definition, classifiers=(*self.definition.classifiers, *to_add))
                    with self.root_storage.transaction():
                        self.root_storage.put_meta("definition", recorded_definition(definition))
                        self.root_storage.put_meta(CLASSIFIER_SET_CHANGES_KEY, _json(changes))
                except BaseException:
                    for identifier in opened:
                        self._close_classifier(identifier)
                    raise
                self._adopt_classifier_set(definition.classifiers)
                self._apply_priming_retractions()
                self._apply_priming_additions()
                self._sync()
            return {
                "added": [spec.id for spec in to_add if spec.id not in reactivated],
                "reactivated": reactivated,
                "already_active": already_active,
                "classifiers": list(self.specs),
                "versions": {cid: self.versions[cid].version for cid in self.specs},
            }

    async def retire_classifiers(
        self, classifier_ids: Sequence[str], *, retired_by: str, execution_guard: ExecutionGuard | None = None
    ) -> dict[str, Any]:
        """Retire classifiers from an existing store. No model call.

        A retired classifier stops deciding and stops learning. Its history,
        labels and reviews stay in the store, and decisions already made keep
        their results for it. It can be added again with ``add_classifiers``.
        Retiring a classifier that is already retired changes nothing. An id
        the store never had is refused, and so is retiring every classifier.
        """
        if not isinstance(retired_by, str) or not retired_by.strip():
            raise ValueError("retired_by names who retired the classifiers")
        if isinstance(classifier_ids, (str, bytes)) or not isinstance(classifier_ids, Sequence) or not classifier_ids:
            raise ValueError("retire_classifiers needs a list of classifier ids")
        if any(not isinstance(identifier, str) or not identifier.strip() for identifier in classifier_ids):
            raise ValueError("retire_classifiers needs classifier ids")
        if len(set(classifier_ids)) != len(classifier_ids):
            raise ValueError("classifiers to retire must have distinct ids")
        async with self._learn_lock, self.execution(execution_guard=execution_guard):
            changes = stored_classifier_set_changes(self.root_storage)
            retired = retired_classifier_ids(changes, list(self.specs))
            unknown = [identifier for identifier in classifier_ids if identifier not in self.specs and identifier not in retired]
            if unknown:
                raise ValueError(f"classifier not in this cyclotron: {', '.join(unknown)}")
            already_retired = [identifier for identifier in classifier_ids if identifier in retired]
            to_retire = [identifier for identifier in classifier_ids if identifier in self.specs]
            if to_retire and len(to_retire) == len(self.specs):
                raise ValueError("a cyclotron needs at least one active classifier")
            if to_retire:
                self._sync()
                specs = [self.specs[identifier] for identifier in to_retire]
                changes.append(change_record(RETIRED, to_retire, specs, retired_by.strip(), _now()))
                definition = replace(
                    self.definition, classifiers=tuple(spec for spec in self.definition.classifiers if spec.id not in to_retire)
                )
                with self.root_storage.transaction():
                    self.root_storage.put_meta("definition", recorded_definition(definition))
                    self.root_storage.put_meta(CLASSIFIER_SET_CHANGES_KEY, _json(changes))
                    for seq, classifier, _, _ in self.root_storage.learning_rows():
                        if classifier in to_retire:
                            self.root_storage.delete_learning(seq)
                for identifier in to_retire:
                    self._close_classifier(identifier)
                self._adopt_classifier_set(definition.classifiers)
            return {
                "retired": to_retire,
                "already_retired": already_retired,
                "classifiers": list(self.specs),
                "versions": {cid: self.versions[cid].version for cid in self.specs},
            }

    def classifier_set_changes(self) -> list[dict[str, Any]]:
        """Who added or retired which classifiers, and when, oldest first."""
        return [
            {key: value for key, value in change.items() if key != "specs"}
            for change in stored_classifier_set_changes(self.root_storage)
        ]

    def retired_classifiers(self) -> list[str]:
        """Classifiers retired from this store and not added again; their history and labels stay in it."""
        return retired_classifier_ids(stored_classifier_set_changes(self.root_storage), list(self.specs))

    def _known_classifiers(self) -> list[str]:
        return [*self.specs, *self.retired_classifiers()]

    def _require_active(self, classifier: str) -> None:
        if classifier not in self.specs:
            if classifier in self.retired_classifiers():
                raise ValueError(f"classifier {classifier!r} was retired; add it again to review for it")
            raise ValueError("classifier not in this cyclotron")

    @classmethod
    def open(cls, directory, definition: CyclotronDefinition, model, optimizer=None, **options) -> Cyclotron:
        """Open or create the cyclotron store at ``directory``. No model call is made."""
        return cls(directory, definition, model, optimizer, **options)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self) -> None:
        # Ownership is released even when closing an opened part fails, so a
        # failure part-way through opening never leaves a store owned.
        try:
            wheels, self.wheels = list(self.wheels.values()), {}
            self._per_output([wheel.close for wheel in wheels])
            if self.shared is not None:
                self.shared.close()
                self.shared = None
            if getattr(self, "root_storage", None) is not None:
                self.root_storage.close()
                self.db = None
        finally:
            if self.lease is not None:
                lease, self.lease = self.lease, None
                lease.release()

    def _per_output(self, calls):
        """Independent per-output calls: at once over stores that allow it, otherwise one after another.

        Every call runs even when one fails; the first failure in output order is then raised.
        """
        concurrently = getattr(self.storage, "run_concurrently", None)
        return concurrently(calls) if concurrently is not None else call_serially(calls)

    def flush(self) -> None:
        """Publish what the stores hold in memory, keeping ownership, so an open engine can serve another call.

        Call it inside ``execution`` so a guard runs first. A SQLite store commits as it goes: nothing to do.
        """
        flush = getattr(self.storage, "flush", None)
        if flush is not None:
            flush()

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
            if self.storage is not None:
                self.storage.execution_boundary()
            yield self

    @staticmethod
    async def _authorize_execution(execution_guard):
        if execution_guard is not None:
            if not callable(execution_guard):
                raise TypeError("execution_guard must be callable")
            result = execution_guard()
            if inspect.isawaitable(result):
                result = await result
            if result is not None:
                raise TypeError("execution_guard must return None or raise to deny")

    # Priming --------------------------------------------------------------

    async def add_priming_examples(
        self, examples: Mapping[str, Sequence[LabeledItem]], *, added_by: str, execution_guard: ExecutionGuard | None = None
    ) -> dict[str, Any]:
        """Add accepted, human-labeled examples to an existing store. No model call.

        Each output that gains examples appends them to its fixed example list
        as a new version, so its next decision sees them. Reviewed decisions
        keep their labels; an unreviewed decision is decided again by the new
        version. Adding the same examples again changes nothing. A known
        identity with different content, or an item already decided, is refused.
        An identity that was retracted may be added again; that is a new
        addition, with the content it is given now.
        """
        if not isinstance(added_by, str) or not added_by.strip():
            raise ValueError("added_by names who accepted the examples")
        from .priming_context import decode_priming, encode_priming

        tasks = {cid: spec.task for cid, spec in self.specs.items()}
        requested = encode_priming(dict(examples), tasks, redact=self.priming_redaction)
        async with self._learn_lock, self.execution(execution_guard=execution_guard):
            stored = self.root_storage.meta("priming_examples")
            saved = json.loads(stored[0]) if stored else {}
            operational_item_ids = set(self._item_values())
            merged = {cid: list(rows) for cid, rows in saved.items()}
            added_example_ids: dict[str, list[str]] = {}
            for cid, rows in requested.items():
                known = {row["item"]["id"]: row for row in merged.get(cid, ())}
                for row in rows:
                    identity = row["item"]["id"]
                    if identity in known:
                        if known[identity] != row:
                            raise ValueError(f"primed example {identity!r} already exists with different content")
                        continue
                    if identity in operational_item_ids:
                        raise ValueError("priming and operational items must be disjoint")
                    merged.setdefault(cid, []).append(row)
                    added_example_ids.setdefault(cid, []).append(identity)
            if added_example_ids:
                merged = encode_priming(decode_priming(merged), tasks, redact=self.priming_redaction)
                additions = self._priming_additions()
                additions.append({"added_by": added_by.strip(), "at": _now(), "examples": added_example_ids, "applied": {}})
                with self.root_storage.transaction():
                    self.root_storage.put_meta("priming_examples", _json(merged))
                    self.root_storage.put_meta("priming_additions", _json(additions))
                self.priming_examples = decode_priming(merged)
                self._apply_priming_retractions()
                self._apply_priming_additions()
                self._sync()
            return {
                "added": added_example_ids,
                "primed_examples": {cid: len(self.priming_examples.get(cid, ())) for cid in self.specs},
                "versions": {cid: self.versions[cid].version for cid in self.specs},
            }

    async def retract_priming_examples(
        self,
        example_ids: Mapping[str, Sequence[str]],
        *,
        retracted_by: str,
        execution_guard: ExecutionGuard | None = None,
    ) -> dict[str, Any]:
        """Withdraw primed examples from a store. No model call.

        Each output that loses examples takes them off its example list as a
        new version, so its next decision no longer carries them as training,
        and the ML model is refitted without them by the next learning pass.
        Who retracted them and when is recorded and stays in the store.
        Retracting an identity that is not primed, or was already retracted,
        changes nothing and is reported as absent. An output that is not in
        this store is refused.
        """
        if not isinstance(retracted_by, str) or not retracted_by.strip():
            raise ValueError("retracted_by names who withdrew the examples")
        if not isinstance(example_ids, Mapping) or set(example_ids) - set(self.specs):
            raise ValueError("retraction maps known classifier ids to example identities")
        requested: dict[str, list[str]] = {}
        for cid, keys in example_ids.items():
            if isinstance(keys, (str, bytes)) or not isinstance(keys, Sequence):
                raise ValueError("retraction needs a list of example identities per classifier")
            if any(not isinstance(key, str) or not key.strip() for key in keys):
                raise ValueError("retraction needs example identities")
            requested[cid] = list(dict.fromkeys(keys))
        from .priming_context import decode_priming

        async with self._learn_lock, self.execution(execution_guard=execution_guard):
            stored = self.root_storage.meta("priming_examples")
            saved = json.loads(stored[0]) if stored else {}
            remaining = {cid: list(rows) for cid, rows in saved.items()}
            retracted: dict[str, list[str]] = {}
            absent: dict[str, list[str]] = {}
            for cid, keys in requested.items():
                primed = {row["item"]["id"] for row in remaining.get(cid, ())}
                for key in keys:
                    (retracted if key in primed else absent).setdefault(cid, []).append(key)
                remaining[cid] = [row for row in remaining.get(cid, ()) if row["item"]["id"] not in retracted.get(cid, ())]
            if retracted:
                retractions = stored_priming_retractions(self.root_storage)
                retractions.append({"retracted_by": retracted_by.strip(), "at": _now(), "examples": retracted, "applied": {}})
                with self.root_storage.transaction():
                    self.root_storage.put_meta("priming_examples", _json(remaining))
                    self.root_storage.put_meta("priming_retractions", _json(retractions))
                self.priming_examples = decode_priming(remaining)
                self._apply_priming_retractions()
                self._apply_priming_additions()
                self._sync()
            return {
                "retracted": retracted,
                "absent": absent,
                "primed_examples": {cid: len(self.priming_examples.get(cid, ())) for cid in self.specs},
                "versions": {cid: self.versions[cid].version for cid in self.specs},
            }

    def priming_additions(self) -> list[dict[str, Any]]:
        """Who added primed examples to this store after it was created, and when.

        Each addition lists the retractions of the examples it added, so an
        audit sees that a primed example was withdrawn, by whom and when.
        """
        retractions = self.priming_retractions()
        rows = []
        for addition in self._priming_additions():
            row = {key: value for key, value in addition.items() if key != "applied"}
            row["retractions"] = [
                {"retracted_by": retraction["retracted_by"], "at": retraction["at"], "examples": withdrawn}
                for retraction in retractions
                if retraction["at"] >= addition["at"]
                and (
                    withdrawn := {
                        cid: [key for key in keys if key in addition["examples"].get(cid, ())]
                        for cid, keys in retraction["examples"].items()
                    }
                )
                and any(withdrawn.values())
            ]
            rows.append(row)
        return rows

    def priming_retractions(self) -> list[dict[str, Any]]:
        """Every retraction of primed examples, including examples the store was created with."""
        return [
            {key: value for key, value in row.items() if key != "applied"}
            for row in stored_priming_retractions(self.root_storage)
        ]

    def _priming_additions(self) -> list[dict[str, Any]]:
        return stored_priming_additions(self.root_storage)

    def _apply_priming_retractions(self) -> None:
        apply_priming_retractions(self.root_storage, self.wheels)

    def _apply_priming_additions(self) -> None:
        apply_priming_additions(self.root_storage, self.wheels)

    # Decide ---------------------------------------------------------------

    async def decide(self, item: Item, *, execution_guard: ExecutionGuard | None = None) -> Decision:
        """Decide one item and say whether it goes to review.

        A decision a person may have seen is stable: an unchanged item keeps
        its decision, label and confidence until it is reviewed or a new
        version (not an ML model refit) is promoted. No model call is repeated.
        """
        async with self.execution(execution_guard=execution_guard):
            # An S3 root publishes everything one decision changes in one commit; SQLite commits as before.
            with getattr(self.root_storage, "coalesced", nullcontext)():
                return await self._decide(item)

    async def _decide(self, item: Item) -> Decision:
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
            same_version = all(
                cid in decision.results and decision.results[cid].version == self.versions[cid].version for cid in self.wheels
            )
            if unchanged and (state == "reviewed" or same_version):
                return decision
            if state == "open":
                self._close_waiting_cycles(item.id, decision.decision_id, "superseded by a new decision")
            with self.root_storage.transaction():
                self.root_storage.set_decision_state(decision.decision_id, "superseded")
        with self.root_storage.transaction():
            self.root_storage.put_item(item.id, _json(dict(item.values)), fingerprint)
        # A suspended cycle from an interrupted decide is resumed here, and
        # the shared request cache answers it without a new model call.
        results, _ = await decide_with_shared_context(
            self.wheels,
            self.shared,
            item,
            self._partitions,
            now=datetime.now(timezone.utc),
            labeled_only_partitions=self._labeled_partitions,
            per_output=self._per_output,
        )
        self._sync()
        decision_id = str(uuid4())
        selection = self._select(item.id, {cid: result.confidence for cid, result in results.items()})
        decision = Decision(
            decision_id,
            item.id,
            _now(),
            {
                cid: ClassifierDecision(
                    cid,
                    result.label,
                    result.confidence,
                    dict(result.probabilities) if result.probabilities else None,
                    self.versions[cid].version,
                    self.wheels[cid].active.fingerprint,
                )
                for cid, result in results.items()
            },
            selection,
        )
        for cid, wheel in self.wheels.items():
            own = selection.for_output(cid)
            if own.selected:
                continue
            # This output is not asked; close its waiting cycle so it does not read as pending.
            cycle = wheel.resume_cycle(item)
            if cycle is not None:
                wheel._emit(
                    {
                        "kind": "review-not-requested",
                        "target_id": item.id,
                        "decision_id": decision_id,
                        "propensity": own.propensity,
                        "detail": own.detail,
                    }
                )
                cycle.__exit__(None, None, None)
        self._insert_decision(decision, fingerprint)
        return decision

    # Review rate ----------------------------------------------------------

    def _select(self, item_id: str, confidences: Mapping[str, float | None]) -> ReviewSelection:
        return self.review_rate.select(item_id, confidences)

    def set_review_rate(self, rate: float, *, set_by: str, expires_at: datetime | None = None) -> None:
        """An operator override of the review rate, with an optional expiry.

        Low-confidence decisions and the audit share are still reviewed.
        """
        self.review_rate.set_rate(rate, set_by=set_by, expires_at=expires_at)

    def clear_review_rate(self, *, set_by: str) -> None:
        """End an operator override; the program's own rate applies again."""
        self.review_rate.clear_rate(set_by=set_by)

    def _program_state(self) -> ProgramState:
        return self.review_rate.state()

    def _insert_decision(self, decision: Decision, fingerprint: str) -> None:
        with self.root_storage.transaction():
            self.root_storage.insert_decision(
                (decision.decision_id, decision.item_id, fingerprint, "open", _json(decision.to_json()))
            )
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
            row = self.root_storage.insert_usage(decision_id, "decision_model", prompt, cached, output, "shared-batch")
            for classifier, share in (shares or {}).items():
                self.root_storage.insert_usage_share(row, classifier, share)
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
        return stored_usage_by_window(self.root_storage, self._known_classifiers(), prices, window=window)

    def decision_log(self) -> list[dict[str, Any]]:
        """Every decision in the order it was made, with its item values, state and active reviews (for recordings)."""
        rows = []
        values = self._item_values()
        for number, (decision_id, state, payload) in enumerate(self.root_storage.decision_rows(), start=1):
            reviews = {}
            for cid in self._known_classifiers():
                review = self._active_review(decision_id, cid)
                last = self._last_review_kind(decision_id, cid)
                if review is not None:
                    reviews[cid] = review.to_json()
                elif last == "no-label":
                    reviews[cid] = {"kind": "no-label"}
            decision = json.loads(payload)
            rows.append(
                {
                    "n": number,
                    "state": state,
                    "decision": decision,
                    "item": values.get(decision["itemId"], {}),
                    "reviews": reviews,
                }
            )
        return rows

    def decision(self, decision_id: str) -> Decision:
        row = self.root_storage.decision(decision_id)
        if row is None:
            raise KeyError(f"unknown decision {decision_id}")
        return Decision.from_json(json.loads(row[0]))

    # Review ---------------------------------------------------------------

    async def review(
        self,
        decision_id: str,
        label: str | None = _NO_LABEL,
        *,
        classifier: str | None = None,
        labels: Mapping[str, str | None] | None = None,
        explanation: str | None = None,
        reason_code: str | None = None,
        reviewer: str | None = None,
        selected_by: str | None = None,
        review_id: str | None = None,
        _replay: bool = False,
        execution_guard: ExecutionGuard | None = None,
    ):
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
                for cid in labels:
                    self._require_active(cid)
                if any(cid not in decision.results for cid in labels):
                    raise ValueError("this decision was made before that classifier was added; review a newer decision")
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
                    raise ValueError(
                        "this decision was not sent to review"
                        + (f" for {cid}" if decision.review.outputs else "")
                        + "; a review of it is selected_by='reviewer'"
                    )
            for name, value in (("explanation", explanation), ("reason_code", reason_code), ("reviewer", reviewer)):
                if value is not None and not isinstance(value, str):
                    raise ValueError(f"{name} must be text")
            explanation = (explanation.strip() or None) if explanation else None
            target = Item(decision.item_id, self._known_item_values(decision.item_id))
            # Validate every entry before recording any of them.
            plans = []
            for cid, value in entries.items():
                if value is not None:
                    value = self.specs[cid].task.validate_label(value)
                existing = self._review(ids[cid])
                if existing is not None:
                    same = (existing.decision_id, existing.classifier, existing.label, existing.reason_code) == (
                        decision_id,
                        cid,
                        value,
                        reason_code,
                    ) and (existing.explanation or None) == self._redacted(cid, explanation)
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
                    cycle = self._record_review(
                        decision, target, cid, value, active, ids[cid], explanation, reason_code, reviewer, chosen_by[cid]
                    )
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
            reviews = {cid: replace(self._review(ids[cid]), optimization_warnings=tuple(warnings[cid])) for cid in entries}
            return reviews if labels is not None else next(iter(reviews.values()))

    def _record_review(self, decision, target, cid, label, active, review_id, explanation, reason_code, reviewer, selected_by):
        """Record one output's review; return its open cycle when a new label needs a learning pass."""
        wheel, spec = self.wheels[cid], self.specs[cid]
        annotations = {
            "decision_id": decision.decision_id,
            "review_id": review_id,
            "selected_by": selected_by,
            "reason_code": reason_code,
            "reviewer": reviewer,
        }
        if label is None:
            cycle = wheel.resume_cycle(target) or wheel.cycle(target, reason="review").__enter__()
            wheel._emit({"kind": "human-skipped", "target_id": target.id, "explanation": explanation, **annotations})
            cycle.__exit__(None, None, None)
            return None
        if active is None:
            cycle = wheel.resume_cycle(target) or wheel.cycle(target, reason="review").__enter__()
            try:
                propensity = decision.review.for_output(cid).propensity if selected_by != "reviewer" else 1.0
                wheel.record_feedback_event(
                    FeedbackItem(
                        review_id,
                        target.id,
                        cid,
                        initial_answer_value=decision.results[cid].label,
                        final_answer_value=label,
                        edit_comment_value=explanation,
                        label_source=LABEL_SOURCE_VETTED,
                        selection_propensity=propensity,
                        review_provenance=f"{selected_by}-selected-review",
                    ),
                    assignment=REVIEWER_SELECTED if selected_by == "reviewer" else review_role(self.definition.seed, target.id),
                    annotations=annotations,
                )
            except Exception as error:
                cycle.__exit__(type(error), error, None)
                raise
            return cycle
        original = self._feedback_event(wheel, active.review_id)
        prior = original["feedback"]
        with wheel.cycle(target, reason="human-correction"):
            wheel.record_feedback_event(
                FeedbackItem(
                    review_id,
                    target.id,
                    cid,
                    initial_answer_value=prior.get("initial_answer_value"),
                    final_answer_value=label,
                    edit_comment_value=explanation,
                    label_source=prior["label_source"],
                    selection_propensity=prior.get("selection_propensity"),
                    review_provenance=f"corrected-human-vote:{prior['id']}",
                ),
                assignment=original.get("assignment"),
                annotations={**annotations, "selected_by": original.get("selected_by", selected_by)},
            )
            self._reconcile(cid)
            record_cycle_metrics(wheel, self._class_config(spec))
            wheel._emit(
                {
                    "kind": "feedback-correction-completed",
                    "feedback_id": review_id,
                    "target_id": target.id,
                    "previous_feedback_id": prior["id"],
                }
            )
        return None

    async def undo_review(
        self,
        decision_id: str,
        *,
        classifier: str | None = None,
        review_id: str | None = None,
        execution_guard: ExecutionGuard | None = None,
    ) -> Review:
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
            target = Item(decision.item_id, self._known_item_values(decision.item_id))
            shown = reviewed_prediction_bindings(wheel.events_of(REVIEW_BINDING_EVENT_KINDS)).get(original["feedback"]["id"])
            if shown is None:
                raise ValueError("the reviewed label has no recorded decision")
            with wheel.cycle(target, reason="human-retraction"):
                wheel.record_feedback_event(
                    FeedbackItem(**original["feedback"]),
                    action="retracted",
                    assignment=original.get("assignment"),
                    annotations={"decision_id": decision_id, "review_id": review_id},
                )
                self._reconcile(cid)
                record_cycle_metrics(wheel, self._class_config(spec))
            # The decision shown before the review is still the one to review.
            cycle = wheel.cycle(target, reason="review-after-undo").__enter__()
            try:
                wheel._emit(
                    {
                        "kind": "displayed-prediction-reused",
                        "target_id": target.id,
                        "prediction_event_id": shown["event_id"],
                        "reason": "review after undo",
                    }
                )
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
        return CyclotronOverview(
            self.definition.id, statuses[0].review_rate, statuses, worst_output(statuses), (last or {}).get("output")
        )

    def status(self, classifier: str | None = None) -> CyclotronStatus:
        """The cyclotron-status/v1 snapshot for one classifier. No model call."""
        if classifier is None:
            if len(self.specs) != 1:
                raise ValueError("name the classifier; this cyclotron has several (overview() shows them all)")
            classifier = next(iter(self.specs))
        spec = self.specs[classifier]
        status = status_from_flywheel(
            self.wheels[classifier],
            cyclotron_id=self.definition.id,
            positive_label=spec.positive_label,
            review_rate=self.review_rate.status(),
        )
        return replace(status, primed_examples=len(self.priming_examples.get(classifier, ())))

    def subscribe(self, after: int = 0, limit: int = 100) -> dict[str, Any]:
        """Committed cyclotron events after a cursor, oldest first.

        Kinds: decision, review, promoted (a new version), refit (a new ML
        model, same version), dropped. Pass the returned cursor back to
        continue; the cursor survives restarts.
        """
        if type(after) is not int or after < 0 or type(limit) is not int or limit < 1:
            raise ValueError("cursor must be nonnegative and page size positive")
        self._sync()
        rows = self.root_storage.events(after, limit)
        return {
            "events": [{**json.loads(payload), "cursor": event_id} for event_id, payload in rows],
            "cursor": rows[-1][0] if rows else after,
        }

    def labels(self, *, include_retired: bool = False) -> list[dict[str, Any]]:
        """Every active label with its item: what an application can replay into a new store.

        Labels of retired classifiers stay in the store; ``include_retired`` lists them too.
        """
        values = self._item_values()
        rows = []
        classifiers = self._known_classifiers() if include_retired else list(self.specs)
        for decision_id, item_id in self.root_storage.decision_items():
            for cid in classifiers:
                review = self._active_review(decision_id, cid)
                if review is not None:
                    rows.append(
                        {
                            "item": Item(item_id, values[item_id]),
                            "classifier": cid,
                            "label": review.label,
                            "explanation": review.explanation,
                            "reason_code": review.reason_code,
                            "reviewer": review.reviewer,
                            "selected_by": review.selected_by,
                            "review_id": review.review_id,
                        }
                    )
        return rows

    async def replay(self, labels: Sequence[Mapping[str, Any]], *, execution_guard: ExecutionGuard | None = None) -> int:
        """Rebuild learning from an application's recorded labels, in order.

        Each record needs ``item`` and ``label``, and may name the classifier,
        explanation, reason code, reviewer, selected_by and review_id. Items are
        decided again, so a new store makes decision-model calls.
        """
        count = 0
        for record in labels:
            decision = await self.decide(record["item"], execution_guard=execution_guard)
            await self.review(
                decision.decision_id,
                record["label"],
                classifier=record.get("classifier"),
                explanation=record.get("explanation"),
                reason_code=record.get("reason_code"),
                reviewer=record.get("reviewer"),
                selected_by=record.get("selected_by"),
                review_id=record.get("review_id"),
                _replay=True,
                execution_guard=execution_guard,
            )
            count += 1
        return count

    # Held-out measurement --------------------------------------------------

    def freeze_held_out_set(self, classifier: str | None = None) -> dict[str, Any]:
        """Freeze the labeled audit items so far into a held-out set that never changes. No model call.

        Returns the set's summary. Freezing again with no new audit labels
        returns the same set; with new labels, it adds a newer set.
        """
        return self.held_out.freeze(classifier)

    def held_out_sets(self, classifier: str | None = None) -> list[dict[str, Any]]:
        """Summaries of the frozen held-out sets, oldest first. No model call."""
        return self.held_out.sets(classifier)

    def held_out_reports(self, classifier: str | None = None) -> list[dict[str, Any]]:
        """The latest cyclotron-held-out-report/v1 documents, oldest first. No model call."""
        return self.held_out.reports(classifier)

    def held_out_request_bound(self, classifier: str | None = None, *, set_id: str | None = None) -> int:
        """The most new decision-model requests ``evaluate_held_out`` can make: two per held-out item."""
        return self.held_out.request_bound(classifier, set_id=set_id)

    def _held_out_set(self, classifier: str, set_id: str | None):
        return self.held_out.held_out_set(classifier, set_id)

    async def evaluate_held_out(
        self,
        classifier: str | None = None,
        *,
        set_id: str | None = None,
        minimum_per_label: int | None = None,
        execution_guard: ExecutionGuard | None = None,
    ) -> dict[str, Any]:
        """Score the store's first classifier and its active one on a frozen held-out set.

        Makes at most ``held_out_request_bound()`` decision-model requests;
        repeating an evaluation answers the unchanged endpoint from the request
        cache. Nothing is learned, fitted or promoted. Returns the report and
        keeps it for ``held_out_reports``.
        """
        async with self.execution(execution_guard=execution_guard):
            return await self.held_out.evaluate(classifier, set_id=set_id, minimum_per_label=minimum_per_label)

    # Snapshot and restore -------------------------------------------------

    def snapshot(self, path) -> dict[str, Any]:
        """Write a consistent copy of this store to one archive file.

        Call it between operations. SQLite's backup API copies each file, so
        the archive is consistent even though the store stays open. The
        archive holds item values, decisions, reviews, transcripts and caches,
        never provider keys; values passed as ``redact`` are already redacted.
        """
        if self.storage is not None:
            raise ValueError("cloud records require canonical S3 export, not a SQLite binary snapshot")
        connections = {"cyclotron.sqlite3": self.db, "shared.sqlite3": self.shared.db}
        for spec in self.definition.classifiers:
            connections[classifier_store_name(spec.id)] = self.wheels[spec.id].db
        retired = {}
        try:
            for classifier in self.retired_classifiers():
                name = classifier_store_name(classifier)
                retired[name] = sqlite3.connect(self.directory / name)
            return write_store_snapshot(path, self.definition, {**connections, **retired})
        finally:
            for connection in retired.values():
                connection.close()

    @staticmethod
    def restore(archive_path, directory, *, lease=None) -> dict[str, Any]:
        """Restore a snapshot into an empty directory, under the store's lease.

        Open the restored store with ``Cyclotron.open`` and the same definition.
        """
        return restore_store_snapshot(archive_path, directory, lease=lease)

    # Derived records ------------------------------------------------------

    def _tracker_origin(self, cid: str) -> str:
        """Identifies where replay starts for this classifier; a saved tracker resumes only from the same start."""
        return hashlib.sha256(_json(initial_snapshot(self.wheels[cid])).encode()).hexdigest()

    def _resume_tracker(self, cid: str) -> None:
        """Pick up the version tracker saved with the cursor, so opening replays only newer events."""
        row = self.root_storage.meta(f"tracker:{cid}")
        if row is None:
            return
        saved = json.loads(row[0])
        if saved["origin"] == self._tracker_origin(cid):
            self.versions[cid] = VersionTracker.from_json(saved["tracker"])
            self._seen[cid] = saved["seen"]

    def _sync(self) -> None:
        """Derive reviews, versions and subscribe events from classifier events.

        Each classifier's new events are applied in one transaction together
        with its durable cursor, so the derived log is never ahead of or behind
        the classifier store after a crash.
        """
        for cid, wheel in self.wheels.items():
            cursor_row = self.root_storage.meta(f"cursor:{cid}")
            cursor = int(cursor_row[0]) if cursor_row else 0
            appended = []
            while True:
                page = wheel.trace_events(after_event_id=self._seen[cid], limit=1000)
                if not page["events"]:
                    break
                previous_tracker = deepcopy(self.versions[cid])
                page_events = []
                next_cursor = max(cursor, page["cursor"])
                try:
                    with self.root_storage.transaction():
                        for event in page["events"]:
                            change = self.versions[cid].feed(event)
                            if event["event_id"] > cursor:
                                page_events.extend(self._derive(cid, event, change))
                        if next_cursor > cursor:
                            self.root_storage.put_meta(f"cursor:{cid}", str(next_cursor))
                        tracker = {
                            "origin": self._tracker_origin(cid),
                            "seen": page["cursor"],
                            "tracker": self.versions[cid].to_json(),
                        }
                        self.root_storage.put_meta(f"tracker:{cid}", _json(tracker))
                except BaseException:
                    # A rolled-back derived page must also roll back the
                    # in-memory source position/version before an in-process retry.
                    self.versions[cid] = previous_tracker
                    raise
                self._seen[cid] = page["cursor"]
                cursor = next_cursor
                appended.extend(page_events)
            for event in appended:
                self.observer(event)

    def _derive(self, classifier: str, event: Mapping[str, Any], change) -> list[dict]:
        kind = event.get("kind")
        review = None
        if kind == "human-feedback" and event.get("review_id"):
            feedback = event["feedback"]
            retracted = event.get("action") == "retracted"
            review = Review(
                event["review_id"],
                event["decision_id"],
                feedback["item_id"],
                classifier,
                "undo"
                if retracted
                else "correction"
                if (feedback.get("review_provenance") or "").startswith("corrected-human-vote:")
                else "label",
                None if retracted else feedback["final_answer_value"],
                None if retracted else feedback.get("edit_comment_value"),
                event.get("reason_code"),
                event.get("reviewer"),
                event.get("selected_by") or "program",
                event.get("created_at") or _now(),
            )
        elif kind == "human-skipped" and event.get("review_id"):
            review = Review(
                event["review_id"],
                event["decision_id"],
                event["target_id"],
                classifier,
                "no-label",
                None,
                event.get("explanation"),
                event.get("reason_code"),
                event.get("reviewer"),
                event.get("selected_by") or "program",
                event.get("created_at") or _now(),
            )
        if review is not None:
            if self._review(review.review_id) is not None:
                return []
            self.root_storage.insert_review(review.review_id, review.decision_id, classifier, _json(self._review_payload(review)))
            requested = self._requested_outputs(review.decision_id)
            reviewed = all(
                self._last_review_kind(review.decision_id, cid) in ("label", "correction", "no-label") for cid in requested
            )
            self.root_storage.set_decision_state(review.decision_id, "reviewed" if reviewed else "open", unless_superseded=True)
            return [self._append({"kind": "review", "review": review.to_json()})]
        if kind == "optimizer-response":
            # A deferred learning pass has no item; it counts toward the latest decision.
            row = (
                self.root_storage.latest_id(event["cycle_item_id"])
                if event.get("cycle_item_id")
                else self.root_storage.latest_id()
            )
            if row is not None:
                prompt, cached, output = _usage_counts(event.get("usage"))
                self.root_storage.insert_usage(
                    row[0], "optimizer", prompt, cached, output, f"{classifier}:{event.get('event_id')}"
                )
        if change is not None:
            tracker = self.versions[classifier]
            return [
                self._append(
                    {
                        "kind": change.kind,
                        "classifier": classifier,
                        "version": change.to_version,
                        "refits": tracker.refits if change.kind == "refit" else 0,
                        "summary": change.summary,
                        "labels": change.labels,
                        "fingerprint": event.get("classifier_version"),
                    }
                )
            ]
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
        if row is None:
            return tuple(self.specs)
        payload = json.loads(row[0])
        requested = _selection(payload["review"]).requested
        names = requested if requested else tuple(payload["classifiers"])
        return tuple(cid for cid in names if cid in self.specs)

    def _redacted(self, classifier: str, text: str | None) -> str | None:
        if text is None:
            return None
        for secret in self.wheels[classifier].redact:
            text = text.replace(secret, "[REDACTED]")
        return text

    def _partitions(self, classifier: str):
        training, development, protected = feedback_partitions(self.wheels[classifier], self._item_values(), self.definition.seed)
        return (*self.priming_examples.get(classifier, ()), *training), development, protected

    def _labeled_partitions(self, classifier: str):
        training, development = labeled_partitions(self.wheels[classifier], self._item_values_for, self.definition.seed)
        return (*self.priming_examples.get(classifier, ()), *training), development

    def _item_values_for(self, item_id: str) -> dict | None:
        values = self.root_storage.item_values(item_id)
        return None if values is None else json.loads(values)

    def _known_item_values(self, item_id: str) -> dict:
        values = self._item_values_for(item_id)
        if values is None:
            raise KeyError(item_id)
        return values

    def _item_values(self) -> dict[str, dict]:
        return {item_id: json.loads(values) for item_id, values in self.root_storage.items()}

    def _item_fingerprint(self, decision_id: str) -> str:
        return self.root_storage.decision_fingerprint(decision_id)

    def _latest_decision(self, item_id: str):
        row = self.root_storage.latest_decision(item_id)
        return None if row is None else (row[0], Decision.from_json(json.loads(row[1])))

    def _classifier(self, decision: Decision, classifier: str | None) -> str:
        if classifier is None:
            if len(self.specs) != 1:
                raise ValueError("name the classifier; this cyclotron has several")
            return self._classifier(decision, next(iter(self.specs)))
        self._require_active(classifier)
        if classifier not in decision.results:
            raise ValueError("this decision was made before that classifier was added; review a newer decision")
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
        for (payload,) in self.root_storage.reviews(decision_id, classifier):
            review = self._load_review(payload)
            if review.kind in ("label", "correction"):
                active = review
            elif review.kind == "undo":
                active = None
        return active

    def _last_review_kind(self, decision_id: str, classifier: str) -> str | None:
        row = self.root_storage.last_review(decision_id, classifier)
        return None if row is None else json.loads(row[0])["kind"]

    @staticmethod
    def _feedback_event(wheel, review_id: str) -> dict:
        event = next(
            (
                e
                for e in reversed(wheel.events_of(("human-feedback",)))
                if e["kind"] == "human-feedback" and e["feedback"]["id"] == review_id
            ),
            None,
        )
        if event is None:
            raise ValueError("the reviewed label is missing from the classifier history")
        return event

    def _close_waiting_cycles(self, item_id: str, decision_id: str, reason: str) -> None:
        target = Item(item_id, self._known_item_values(item_id))
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

    def _provider_window(self, classifier: str):
        return self.deferred_learning.provider_window(classifier)

    def _enqueue_learning(self, classifier: str) -> None:
        self.deferred_learning.enqueue(classifier)

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
        return await self.deferred_learning.run(limit=limit, execution_guard=execution_guard)

    async def _learn(self, classifier: str, cycle, *, since_count: int | None = None) -> list:
        if self.optimizer is None:
            # Without an LLM optimizer the cyclotron decides and records only.
            self._reconcile(classifier)
            return []
        from .observability import StepFailed

        try:
            return await learn_from_review(
                self.wheels,
                self.shared,
                classifier,
                cycle,
                self._partitions,
                optimize_every=self.definition.optimize_every,
                rubric_changes_every=self.definition.rubric_changes_every,
                **({"since_count": since_count} if since_count is not None else {}),
            )
        except (StepFailed, RuntimeError) as error:
            warning = {
                "classifier_id": classifier,
                "reason": "optimization failed; labels retained, no automatic retry",
                "error_type": type(error).__name__,
            }
            self.wheels[classifier]._emit({"kind": "optimization-unavailable", **warning})
            return [warning]

    @staticmethod
    def _class_config(spec: ClassifierSpec) -> list[dict]:
        return [{"label": label, **({"role": "positive"} if label == spec.positive_label else {})} for label in spec.labels]

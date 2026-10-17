"""Deferred learning passes of an embedded cyclotron, and the lock they lend to provider calls.

With ``learning="deferred"`` a review records its label and queues the output's
pass. ``DeferredLearning.run`` runs the queued passes one output at a time.
While a pass awaits an asynchronous optimizer provider call it lends the
engine's lock, so reviews and decisions interleave; a pass whose state changed
meanwhile is superseded and stays queued. The cyclotron delegates to it.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

from .embedded_records import ExecutionGuard, _now
from .optimizer_agent import OptimizationSuperseded


class DeferredLearning:
    def __init__(self, engine):
        self.engine = engine
        # The output a deferred learning pass is optimizing, in that pass's task only.
        self._learning: ContextVar[str | None] = ContextVar("cyclotron_learning", default=None)
        # The running pass's evidence, row marks and lent-lock intervals, in that pass's task only.
        self._pass_state: ContextVar[dict | None] = ContextVar("cyclotron_pass_state", default=None)

    @asynccontextmanager
    async def provider_window(self, classifier: str):
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
        engine = self.engine
        state = self._pass_state.get()
        if self._learning.get() != classifier or state is None or state["hold_lock"]:
            yield
            return
        wheel = engine.wheels[classifier]
        # Criteria, not ML models: the pass refits ML models itself, so a refit meanwhile is no conflict.
        criteria = {cid: other.active.config.fingerprint for cid, other in engine.wheels.items()}
        flags = (wheel._cycle_running, wheel._step_running)
        binding = engine.shared.snapshot()
        lent = self._row_marks(wheel)
        wheel._cycle_running = wheel._step_running = False
        engine._lock.release()
        try:
            yield
        finally:
            cancelled = None
            while True:
                try:
                    await engine._lock.acquire()
                    break
                except asyncio.CancelledError as error:  # get the lock back before unwinding
                    cancelled = error
            wheel._cycle_running, wheel._step_running = flags
            engine.shared.restore(binding)
            # Rows other operations wrote while the lock was lent are theirs, never the pass's to remove.
            state["lent"].append((lent, self._row_marks(wheel)))
            if cancelled is not None:
                raise cancelled
        reason = self._superseded(classifier, criteria)
        if reason:
            raise OptimizationSuperseded(reason)

    def enqueue(self, classifier: str) -> None:
        """Queue one output's learning pass; queued reviews of one output share a pass."""
        engine = self.engine
        training, development, _ = engine._partitions(classifier)
        with engine.root_storage.transaction():
            # The earliest unlearned count is kept, so the pass sees every cadence boundary crossed since.
            engine.root_storage.enqueue_learning(classifier, len(training) + len(development) - 1, _now())

    async def run(self, *, limit: int | None = None,
                  execution_guard: ExecutionGuard | None = None) -> list[dict[str, Any]]:
        engine = self.engine
        if limit is not None and (type(limit) is not int or limit < 1):
            raise ValueError("limit must be a positive integer")
        done, ran = [], set()
        while limit is None or len(done) < limit:
            async with engine._learn_lock, engine.execution(execution_guard=execution_guard):
                rows = engine.root_storage.learning_rows()
                row = next((row for row in rows if row[1] not in ran), None)
                if row is None:
                    break
                seq, cid, since, superseded_before = row
                ran.add(cid)
                engine._sync()
                engine._batches = []
                wheel = engine.wheels[cid]
                training, development, _ = engine._partitions(cid)
                covered, reviews = len(training) + len(development), engine.root_storage.review_count(cid)
                hold_lock = execution_guard is not None or superseded_before >= engine.max_superseded_passes
                if hold_lock and execution_guard is None:
                    wheel._emit({"kind": "learning-window-withheld", "superseded_passes": superseded_before,
                                 "limit": engine.max_superseded_passes,
                                 "reason": "repeatedly superseded; this pass keeps the store lock through its optimizer calls"})
                state = {"evidence": wheel._evidence((*training, *development)), "start": self._row_marks(wheel),
                         "lent": [], "hold_lock": hold_lock}
                started = time.perf_counter()
                cycle = wheel.cycle(None, reason="deferred-learning").__enter__()
                tokens = (self._learning.set(cid), self._pass_state.set(state))
                superseded = None
                try:
                    warnings = await engine._learn(cid, cycle, since_count=since)
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
                    with engine.root_storage.transaction():
                        engine.root_storage.supersede_learning(seq)
                else:
                    wheel._emit({"kind": "deferred-learning-completed", **record})
                    engine._sync()
                    with engine.root_storage.transaction():
                        if engine.root_storage.review_count(cid) > reviews:
                            # Reviews arrived during the pass (any kind, held-out ones too): run again from here.
                            engine.root_storage.requeue_learning(seq, covered)
                        else:
                            engine.root_storage.delete_learning(seq)
                engine._sync()
                done.append(record)
        return done

    def _superseded(self, classifier: str, criteria: Mapping[str, str]) -> str | None:
        """Why a pass's state no longer holds after its optimizer call, or None."""
        engine = self.engine
        changed = [cid for cid, other in engine.wheels.items() if other.active.config.fingerprint != criteria[cid]]
        if changed:
            return "new criteria for " + ", ".join(changed) + " were activated during the optimizer call"
        covered = self._pass_state.get()["evidence"]
        if covered:
            training, development, _ = engine._partitions(classifier)
            current = engine.wheels[classifier]._evidence((*training, *development))
            if any(current.get(key) != value for key, value in covered.items()):
                return "a label this pass rests on was corrected or undone during the optimizer call"
        return None

    def _row_marks(self, wheel) -> dict[str, int]:
        """The highest row id of each table a pass can leave pending work in."""
        marks = wheel.pending_storage.marks()
        marks["shared:batches"] = self.engine.shared.storage.operation_mark()
        return marks

    def _drop_pending(self, wheel, state) -> None:
        """Remove pending work the stopped pass itself started, so its next pass needs no retry authorization.

        Rows other operations wrote while the pass lent the store lock, and
        older interrupted work, keep their explicit-retry guard. Dropping the
        guard on the pass's own interrupted decision-model request is the one
        exception to "no silent repayment": it is recorded as
        ``decision-request-abandoned``, because asking again may be charged again.
        """
        shared = self.engine.shared
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
        for rowid, key, owner in shared.storage.pending_operations():
            if own("shared:batches", rowid):
                wheel._emit({"kind": "decision-request-abandoned", "request_fingerprint": key, "shared_batch": True,
                             "reason": "the learning pass that made this request stopped before its answer was recorded",
                             "repeat_charge_possible": True})
                shared.storage.abandon_learning(key, owner)

    def _record_pass_batches(self) -> None:
        engine = self.engine
        latest = engine.root_storage.latest_id()
        if latest is not None:
            with engine.root_storage.transaction():
                # Decision-model requests made while learning count toward the latest decision.
                engine._record_batches(latest[0])

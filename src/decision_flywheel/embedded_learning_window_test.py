"""While a deferred learning pass awaits an asynchronous optimizer, reviews and decisions go on safely.

The provider here is a fake that waits on a gate the spec opens, so each spec
can act while a learning pass is inside a provider call.
"""
import asyncio
from dataclasses import replace
import threading

import pytest

from .embedded_cyclotron import Cyclotron
from .embedded_cyclotron_test import item, run
from .embedded_deferred_learning_test import OUTPUTS, REPLY, Model, checklist, definition
from .optimizer_agent import OptimizerAgent, OptimizerReply


class GatedTransport:
    """An asynchronous provider. Call number ``gate_call`` waits until the spec opens the gate."""

    def __init__(self, max_calls=None, gate_call=1):
        self.calls, self.gate_call = 0, gate_call
        self.entered, self.gate = asyncio.Event(), asyncio.Event()
        if max_calls is not None:
            self.max_calls = max_calls

    def __call__(self, messages):
        raise AssertionError("an asynchronous transport must be awaited")

    async def acomplete(self, messages):
        self.calls += 1
        if self.calls == self.gate_call:
            self.entered.set()
            await self.gate.wait()
        return OptimizerReply(REPLY, "fake-async-optimizer")

    def renew(self):
        """Fresh events for a new event loop (each asyncio.run is a new loop)."""
        self.entered, self.gate = asyncio.Event(), asyncio.Event()


def store(tmp_path, transport, outputs=1, observer=None, **options):
    agent = OptimizerAgent(transport, **({"observer": observer} if observer else {}))
    return Cyclotron.open(tmp_path / "c", definition(outputs, **options), Model(), agent, max_requests=2000,
                          learning="deferred")


def kinds(wheel, kind):
    return [e for e in wheel.history(100000) if e["kind"] == kind]


def pending_rows(wheel):
    tables = {row[0] for row in wheel.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return sum(wheel.db.execute(f"SELECT COUNT(*) FROM {table} WHERE status='pending'").fetchone()[0]
               for table in ("runtime_rounds", "optimization_stages", "classifier_training") if table in tables)


def cyclotron_pending_after(passes):
    return [p["classifier"] for p in passes if p.get("superseded")]


def labels_counted(cyclotron, classifier):
    training, development, _ = cyclotron._partitions(classifier)
    return len(training) + len(development)


def test_reviews_and_decisions_complete_while_learning_awaits_the_optimizer(tmp_path):
    transport, observed = GatedTransport(), []
    with store(tmp_path, transport, 2, observer=lambda event: observed.append(threading.get_ident())) as cyclotron:
        for index in range(12):
            checklist(cyclotron, index, OUTPUTS[:2])
        waiting = run(cyclotron.decide(item("waiting", "yes waiting")))

        async def scenario():
            transport.renew()
            learning = asyncio.create_task(cyclotron.learn())
            await transport.entered.wait()
            in_flight = transport.calls
            review = await cyclotron.review(waiting.decision_id, labels={"timely": "include"})
            fresh = await cyclotron.decide(item("fresh", "yes fresh"))
            overlapped = not learning.done() and transport.calls == in_flight
            transport.gate.set()
            await learning
            return threading.get_ident(), review, fresh, overlapped
        loop_thread, review, fresh, overlapped = asyncio.run(scenario())
        requests = sum(len(kinds(wheel, "optimizer-request")) for wheel in cyclotron.wheels.values())
        responses = sum(len(kinds(wheel, "optimizer-response")) for wheel in cyclotron.wheels.values())
    assert overlapped and review["timely"].label == "include" and fresh.decision_id
    assert set(observed) == {loop_thread}
    assert requests == responses == transport.calls > 0


def test_a_review_recorded_during_the_call_gets_its_own_learning_pass(tmp_path):
    transport = GatedTransport()
    with store(tmp_path, transport) as cyclotron:
        for index in range(12):
            checklist(cyclotron, index, OUTPUTS[:1])
        late = run(cyclotron.decide(item("late", "yes late")))
        covered = labels_counted(cyclotron, "relevant")

        async def scenario():
            transport.renew()
            learning = asyncio.create_task(cyclotron.learn())
            await transport.entered.wait()
            await cyclotron.review(late.decision_id, "include", selected_by="reviewer")
            transport.gate.set()
            return await learning
        passes = asyncio.run(scenario())
        pending = cyclotron.pending_learning()

        async def again():
            transport.renew()
            transport.gate.set()
            return await cyclotron.learn()
        later = asyncio.run(again())
    # Each output runs once per learn() call; the label that arrived mid-pass keeps it queued for the next call.
    assert [p["classifier"] for p in passes] == ["relevant"] and pending == ["relevant"]
    assert later[0]["since_count"] == covered and cyclotron_pending_after(later) == []


def test_cancelling_learning_mid_call_leaves_the_store_usable_and_the_pass_queued(tmp_path):
    transport = GatedTransport()
    with store(tmp_path, transport) as cyclotron:
        for index in range(12):
            checklist(cyclotron, index, OUTPUTS[:1])
        wheel = cyclotron.wheels["relevant"]

        async def scenario():
            transport.renew()
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(cyclotron.learn(), timeout=0.2)
            await cyclotron.decide(item("after-cancel", "yes after"))   # no "one operational cycle" error
        asyncio.run(scenario())
        cancelled, rows, queued = kinds(wheel, "optimizer-cancelled"), pending_rows(wheel), cyclotron.pending_learning()

        async def retry():
            transport.renew()
            transport.gate.set()
            return await cyclotron.learn()
        passes = asyncio.run(retry())
    assert cancelled and rows == 0 and queued == ["relevant"]
    assert passes[0]["classifier"] == "relevant" and passes[0]["warnings"] == []
    assert transport.calls >= 2


def test_cancelling_while_waiting_to_get_the_lock_back_keeps_the_lock_consistent(tmp_path):
    transport = GatedTransport()
    with store(tmp_path, transport) as cyclotron:
        for index in range(12):
            checklist(cyclotron, index, OUTPUTS[:1])

        async def scenario():
            transport.renew()
            learning = asyncio.create_task(cyclotron.learn())
            await transport.entered.wait()
            async with cyclotron._lock:            # another operation holds the store when the call returns
                transport.gate.set()
                for _ in range(5):
                    await asyncio.sleep(0)         # learn() is now waiting to get the lock back
                learning.cancel()
                for _ in range(5):
                    await asyncio.sleep(0)
            with pytest.raises(asyncio.CancelledError):
                await learning
            assert not cyclotron._lock.locked()
            await cyclotron.decide(item("after", "yes after"))
        asyncio.run(scenario())


def test_usage_a_pass_paid_for_before_its_call_survives_a_decision_during_the_call(tmp_path):
    transport = GatedTransport()
    with store(tmp_path, transport) as cyclotron:
        for index in range(12):
            checklist(cyclotron, index, OUTPUTS[:1])
        original = cyclotron._learn

        async def paying_learn(*args, **kwargs):
            # A paid decision-model request the pass makes before its provider call (marked by its token count).
            cyclotron._on_batch({"kind": "shared-decision-batch", "cached": False,
                                 "usage": {"input_tokens": 777, "output_tokens": 1}, "output_shares": None})
            return await original(*args, **kwargs)
        cyclotron._learn = paying_learn

        async def scenario():
            transport.renew()
            learning = asyncio.create_task(cyclotron.learn())
            await transport.entered.wait()
            await cyclotron.decide(item("during", "yes during"))
            decision = await cyclotron.decide(item("during-2", "no during"))
            await cyclotron.review(decision.decision_id, "exclude", selected_by="reviewer")
            transport.gate.set()
            return await learning
        passes = asyncio.run(scenario())
        rows = cyclotron.db.execute("SELECT COUNT(*) FROM usage WHERE input_tokens=777").fetchone()[0]
    assert rows == len(passes) >= 1   # each pass's own paid request is counted exactly once


def test_a_window_in_a_later_stage_lets_reviews_and_decisions_through_too(tmp_path):
    transport = GatedTransport(gate_call=2)
    with store(tmp_path, transport, optimize_every=4) as cyclotron:
        for index in range(12):
            checklist(cyclotron, index, OUTPUTS[:1])
        waiting = run(cyclotron.decide(item("waiting", "no waiting")))

        async def scenario():
            transport.renew()
            learning = asyncio.create_task(cyclotron.learn())
            await transport.entered.wait()                    # the pass's second provider call, in a later stage
            review = await cyclotron.review(waiting.decision_id, "exclude", selected_by="reviewer")
            fresh = await cyclotron.decide(item("fresh", "yes fresh"))
            overlapped = not learning.done()
            transport.gate.set()
            return review, fresh, overlapped, await learning
        review, fresh, overlapped, passes = asyncio.run(scenario())
        stages = [e.get("step_stage") for e in kinds(cyclotron.wheels["relevant"], "optimizer-request")]
    assert overlapped and review.label == "exclude" and fresh.decision_id and transport.calls >= 2
    assert len(set(stages[:2])) == 2, f"the two provider calls must be in different stages: {stages}"
    assert passes and not any(p.get("superseded") for p in passes)


def superseded_by(tmp_path, change, outputs=2, items=12):
    """Run one pass, make ``change`` while it is inside its provider call, and return what happened."""
    transport = GatedTransport()
    with store(tmp_path, transport, outputs) as cyclotron:
        for index in range(items):
            checklist(cyclotron, index, OUTPUTS[:outputs])
        wheel = cyclotron.wheels["relevant"]

        async def scenario():
            transport.renew()
            learning = asyncio.create_task(cyclotron.learn())
            await transport.entered.wait()
            await change(cyclotron)
            transport.gate.set()
            return await learning
        first = asyncio.run(scenario())
        events = {kind: len(kinds(wheel, kind)) for kind in ("optimizer-superseded", "optimizer-failed")}
        queued, rows = cyclotron.pending_learning(), pending_rows(wheel)
        failed_rounds = wheel.db.execute(
            "SELECT COUNT(*) FROM runtime_rounds WHERE status='complete' AND payload LIKE '%round failed%'").fetchone()[0]

        async def retry():
            transport.renew()
            transport.gate.set()
            return await cyclotron.learn(limit=1)
        second = asyncio.run(retry())
    return first, events, queued, rows, failed_rounds, second


def assert_superseded_and_retried(outcome):
    first, events, queued, rows, failed_rounds, second = outcome
    assert first[0]["superseded"] and first[0]["classifier"] == "relevant"
    # A superseded output is not rerun within the same learn() call, even with no limit.
    assert [p["classifier"] for p in first].count("relevant") == 1
    assert events == {"optimizer-superseded": 1, "optimizer-failed": 0}
    assert "relevant" in queued and rows == 0 and failed_rounds == 0
    assert second[0]["classifier"] == "relevant" and not second[0].get("superseded") and second[0]["warnings"] == []


def test_a_new_version_of_the_learning_output_during_its_call_supersedes_the_pass_and_it_is_retried(tmp_path):
    async def change(cyclotron):
        wheel = cyclotron.wheels["relevant"]
        async with cyclotron._lock:
            wheel._activate(replace(wheel.active, config=replace(wheel.active.config, rubric="Changed elsewhere.")))
    assert_superseded_and_retried(superseded_by(tmp_path, change))


def test_a_sibling_change_during_the_call_supersedes_the_pass(tmp_path):
    async def change(cyclotron):
        sibling = cyclotron.wheels["timely"]
        async with cyclotron._lock:
            sibling._activate(replace(sibling.active, config=replace(sibling.active.config, rubric="Sibling changed.")))
    assert_superseded_and_retried(superseded_by(tmp_path, change))


def test_a_correction_to_a_development_item_during_the_call_supersedes_the_pass(tmp_path):
    async def change(cyclotron):
        _, development, _ = cyclotron._partitions("relevant")
        target = development[0]
        decision = cyclotron._latest_decision(target.item.id)[1]
        await cyclotron.review(decision.decision_id, "include" if target.label == "exclude" else "exclude",
                               classifier="relevant")
    assert_superseded_and_retried(superseded_by(tmp_path, change, outputs=1, items=24))


def test_a_correction_during_the_call_never_leaves_a_version_built_on_the_old_label(tmp_path):
    from .flywheel import FittedClassifier
    transport = GatedTransport()
    with store(tmp_path, transport) as cyclotron:
        for index in range(12):
            checklist(cyclotron, index, OUTPUTS[:1])
        wheel = cyclotron.wheels["relevant"]
        training, development, _ = cyclotron._partitions("relevant")
        # A learned version that rests on the current labels.
        wheel._activate(FittedClassifier(replace(wheel.active.config, rubric="Learned rubric."), None,
                                         wheel._evidence(training), wheel._evidence(development), "evaluated"))
        target = training[0]
        decision = cyclotron._latest_decision(target.item.id)[1]
        flipped = "include" if target.label == "exclude" else "exclude"

        async def scenario():
            transport.renew()
            learning = asyncio.create_task(cyclotron.learn(limit=1))
            await transport.entered.wait()
            await cyclotron.review(decision.decision_id, flipped, classifier="relevant")
            transport.gate.set()
            passes = await learning
            await cyclotron.decide(item("after", "yes after"))
            return passes
        passes = asyncio.run(scenario())
        training, development, _ = cyclotron._partitions("relevant")
        current = wheel._evidence((*training, *development))
        promoted = [e for e in wheel.history(100000) if e["kind"] in ("promoted", "context-refined", "context-initialized")]
    assert passes[0]["superseded"] and not promoted
    assert current[target.item.id] != wheel._evidence((target,))[target.item.id]    # the correction is kept
    for evidence in (wheel.active.training_evidence, wheel.active.development_evidence):
        assert all(current.get(key) == value for key, value in (evidence or {}).items())


def test_the_shared_call_limit_holds_for_an_asynchronous_transport(tmp_path):
    transport = GatedTransport(max_calls=1)
    with store(tmp_path, transport, 3) as cyclotron:
        for index in range(12):
            checklist(cyclotron, index, OUTPUTS[:3])

        async def scenario():
            transport.renew()
            transport.gate.set()
            return await cyclotron.learn()
        passes = asyncio.run(scenario())
    assert transport.calls == 1
    assert any(w["reason"].startswith("optimizer call limit reached") for p in passes for w in p["warnings"])


class BlockingModel(Model):
    """Waits on ``gate`` for requests ``blocks(configs, target)`` selects, like a slow provider."""

    def __init__(self, blocks):
        self.blocks, self.entered, self.gate = blocks, asyncio.Event(), asyncio.Event()

    async def classify_many(self, configs, target, training, **kwargs):
        if self.blocks(configs, target):
            self.entered.set()
            await self.gate.wait()
        return await super().classify_many(configs, target, training, **kwargs)


def pending_batches(cyclotron):
    return cyclotron.shared.db.execute("SELECT COUNT(*) FROM batches WHERE status='pending'").fetchone()[0]


def test_a_stopped_pass_never_removes_the_retry_guard_of_a_decision_cancelled_during_its_call(tmp_path):
    transport = GatedTransport()
    model = BlockingModel(lambda configs, target: target.values["text"].startswith("blocked"))
    agent = OptimizerAgent(transport)
    with Cyclotron.open(tmp_path / "c", definition(1), model, agent, max_requests=2000, learning="deferred") as cyclotron:
        for index in range(12):
            checklist(cyclotron, index, OUTPUTS[:1])

        async def scenario():
            transport.renew()
            model.entered, model.gate = asyncio.Event(), asyncio.Event()
            learning = asyncio.create_task(cyclotron.learn())
            await transport.entered.wait()
            deciding = asyncio.create_task(cyclotron.decide(item("blocked", "blocked item")))
            await model.entered.wait()                       # the decision's paid request is in flight
            deciding.cancel()
            with pytest.raises(asyncio.CancelledError):
                await deciding
            guarded = pending_batches(cyclotron)
            async with cyclotron._lock:                      # then the pass is superseded
                wheel = cyclotron.wheels["relevant"]
                wheel._activate(replace(wheel.active, config=replace(wheel.active.config, rubric="Changed.")))
            transport.gate.set()
            passes = await learning
            return guarded, passes
        guarded, passes = asyncio.run(scenario())
        after = pending_batches(cyclotron)
    assert passes[0]["superseded"]
    assert guarded == after == 1          # the cancelled decision keeps its no-silent-repayment guard


def test_a_cancelled_pass_abandons_its_own_paid_request_openly(tmp_path):
    from datetime import datetime, timezone
    transport = GatedTransport(gate_call=99)       # the optimizer never waits here
    model = BlockingModel(lambda configs, target: any(c.rubric == "Pass-only rubric" for c in configs.values()))
    with Cyclotron.open(tmp_path / "c", definition(1), model, OptimizerAgent(transport), max_requests=2000,
                        learning="deferred") as cyclotron:
        for index in range(12):
            checklist(cyclotron, index, OUTPUTS[:1])
        wheel = cyclotron.wheels["relevant"]
        original = cyclotron._learn

        async def asking_learn(classifier, cycle, **kwargs):
            # The pass asks the decision model about a candidate, a paid request of its own.
            training, _, _ = cyclotron._partitions(classifier)
            candidate = replace(wheel.active.config, rubric="Pass-only rubric")
            cyclotron.shared.bind_context({classifier: candidate}, {classifier: training})
            await wheel._answers(candidate, training[0].item, training, datetime.now(timezone.utc))
            return await original(classifier, cycle, **kwargs)
        cyclotron._learn = asking_learn

        async def scenario():
            transport.renew()
            model.entered, model.gate = asyncio.Event(), asyncio.Event()
            learning = asyncio.create_task(cyclotron.learn())
            await model.entered.wait()                       # the pass's own paid request is in flight
            learning.cancel()
            with pytest.raises(asyncio.CancelledError):
                await learning
        asyncio.run(scenario())
        abandoned = kinds(wheel, "decision-request-abandoned")
        guards = pending_batches(cyclotron), pending_rows(wheel)
        answers = wheel.db.execute("SELECT COUNT(*) FROM runtime_answers WHERE status='pending'").fetchone()[0]
        queued = cyclotron.pending_learning()
    assert {bool(e.get("shared_batch")) for e in abandoned} == {True, False}
    assert all(e["repeat_charge_possible"] for e in abandoned)
    assert guards == (0, 0) and answers == 0 and queued == ["relevant"]


def test_a_stopped_step_and_round_are_closed_in_the_trace(tmp_path):
    async def change(cyclotron):
        wheel = cyclotron.wheels["relevant"]
        async with cyclotron._lock:
            wheel._activate(replace(wheel.active, config=replace(wheel.active.config, rubric="Changed elsewhere.")))
    transport = GatedTransport()
    with store(tmp_path, transport) as cyclotron:
        for index in range(12):
            checklist(cyclotron, index, OUTPUTS[:1])
        wheel = cyclotron.wheels["relevant"]

        async def scenario():
            transport.renew()
            learning = asyncio.create_task(cyclotron.learn())
            await transport.entered.wait()
            await change(cyclotron)
            transport.gate.set()
            return await learning
        passes = asyncio.run(scenario())
        steps = kinds(wheel, "step-started"), kinds(wheel, "step-completed") + kinds(wheel, "step-failed")
    assert passes[0]["superseded"]
    assert len(steps[0]) == len(steps[1]) and any(e.get("status") == "superseded" for e in steps[1])


def test_a_head_refit_during_the_call_does_not_supersede_the_pass(tmp_path):
    from .candidate_fitting import fit_candidate
    transport = GatedTransport()
    with store(tmp_path, transport) as cyclotron:
        for index in range(24):
            checklist(cyclotron, index, OUTPUTS[:1])
        wheel = cyclotron.wheels["relevant"]
        training, development, _ = cyclotron._partitions("relevant")
        cyclotron.shared.bind_context({"relevant": wheel.active.config}, {"relevant": training})
        fitted, _ = run(fit_candidate(wheel, wheel.active.config, training, development, protected=(),
                                      propensities={row.item.id: 1. for row in training}, validation_status="evaluated"))
        wheel._activate(fitted)
        assert wheel.active.head is not None
        waiting = run(cyclotron.decide(item("waiting", "yes waiting")))

        async def scenario():
            transport.renew()
            learning = asyncio.create_task(cyclotron.learn())
            await transport.entered.wait()
            await cyclotron.review(waiting.decision_id, "include", selected_by="reviewer")
            await cyclotron.decide(item("fresh", "yes fresh"))
            async with cyclotron._lock:                      # the ML model alone changes (a refit or a dropped head)
                wheel._activate(replace(wheel.active, head=None, answer_dependencies=()))
            transport.gate.set()
            return await learning
        passes = asyncio.run(scenario())
    assert passes[0]["classifier"] == "relevant" and not passes[0].get("superseded")


def test_a_repeatedly_superseded_output_keeps_the_lock_on_its_next_pass(tmp_path):
    transport = GatedTransport()
    with Cyclotron.open(tmp_path / "c", definition(1), Model(), OptimizerAgent(transport), max_requests=2000,
                        learning="deferred", max_superseded_passes=1) as cyclotron:
        for index in range(12):
            checklist(cyclotron, index, OUTPUTS[:1])
        wheel = cyclotron.wheels["relevant"]

        async def superseded():
            transport.renew()
            learning = asyncio.create_task(cyclotron.learn())
            await transport.entered.wait()
            async with cyclotron._lock:
                wheel._activate(replace(wheel.active, config=replace(wheel.active.config, rubric="Changed.")))
            transport.gate.set()
            return await learning
        first = asyncio.run(superseded())

        async def held():
            transport.renew()
            transport.gate_call = transport.calls + 1
            learning = asyncio.create_task(cyclotron.learn())
            await transport.entered.wait()
            lock_held = cyclotron._lock.locked()
            transport.gate.set()
            return lock_held, await learning
        lock_held, second = asyncio.run(held())
        withheld = kinds(wheel, "learning-window-withheld")
    assert first[0]["superseded"] and lock_held and not second[0].get("superseded")
    assert withheld and withheld[0]["limit"] == 1


def test_a_held_out_review_during_the_call_keeps_the_output_queued(tmp_path):
    from .learning_loop import review_role
    transport = GatedTransport()
    with store(tmp_path, transport) as cyclotron:
        for index in range(12):
            checklist(cyclotron, index, OUTPUTS[:1])
        held_out = next(f"held-{n}" for n in range(1000) if review_role("deferred-seed", f"held-{n}") == "scoreboard")
        waiting = run(cyclotron.decide(item(held_out, "yes held")))

        async def scenario():
            transport.renew()
            learning = asyncio.create_task(cyclotron.learn())
            await transport.entered.wait()
            await cyclotron.review(waiting.decision_id, "include")
            transport.gate.set()
            return await learning
        asyncio.run(scenario())
        queued = cyclotron.pending_learning()
    assert queued == ["relevant"]


def test_a_busy_output_does_not_starve_the_others_in_one_learn_call(tmp_path):
    transport = GatedTransport()
    with store(tmp_path, transport, 2) as cyclotron:
        for index in range(12):
            checklist(cyclotron, index, OUTPUTS[:2])
        waiting = run(cyclotron.decide(item("waiting", "yes waiting")))

        async def scenario():
            transport.renew()
            learning = asyncio.create_task(cyclotron.learn())
            await transport.entered.wait()
            await cyclotron.review(waiting.decision_id, "include", classifier="relevant", selected_by="reviewer")
            transport.gate.set()
            return await learning
        passes = asyncio.run(scenario())
        queued = cyclotron.pending_learning()
    assert [p["classifier"] for p in passes] == ["relevant", "timely"] and queued == ["relevant"]

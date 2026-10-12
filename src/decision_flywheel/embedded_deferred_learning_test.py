"""A reviewer saving a checklist does not wait while every output's optimization runs."""
import asyncio

import pytest

from .batched_classification import BatchedAnswers
from .embedded_cyclotron import ClassifierSpec, Cyclotron, CyclotronDefinition
from .embedded_cyclotron_test import item, run
from .models import DecisionResult
from .optimizer_agent import OptimizerAgent, OptimizerReply

OUTPUTS = tuple(ClassifierSpec(name, ("include", "exclude"), f"Is this {name}?", positive_label="include")
                for name in ("relevant", "timely", "local", "original", "sourced"))
REPLY = ('{"rationale":"Look for practical work","rubric":"Practical research",'
         '"example_ids":["t0","t1"],"tasks":[{"name":"practical",'
         '"instructions":"Is this practical?","labels":["yes","no"]}]}')


class Transport:
    """A counting optimizer transport; ``max_calls`` is the cyclotron-wide limit when given."""

    def __init__(self, max_calls=None):
        self.calls = 0
        if max_calls is not None:
            self.max_calls = max_calls

    def __call__(self, messages):
        self.calls += 1
        return OptimizerReply(REPLY, "fake-optimizer")


class Model:
    """Yields to the event loop on every request, like a real provider call."""
    model_identity = "yielding"

    async def classify_many(self, configs, target, training, **kwargs):
        await asyncio.sleep(0)
        p = .8 if "yes" in target.values["text"] else .3
        return BatchedAnswers({cid: {"decision": DecisionResult("include" if p > .5 else "exclude",
                                                                {"include": p, "exclude": 1 - p})} for cid in configs},
                              "scripted", {}, 1)


def definition(count=5, **options):
    return CyclotronDefinition("checklist", OUTPUTS[:count], seed="deferred-seed",
                               **{"rubric_changes_every": 1, **options})


def label(index):
    return "include" if index % 2 else "exclude"


def checklist(cyclotron, index, outputs):
    decision = run(cyclotron.decide(item(f"learn-{index}", ("yes " if index % 2 else "no ") + str(index))))
    return run(cyclotron.review(decision.decision_id, labels={spec.id: label(index) for spec in outputs}))


def test_a_deferred_review_returns_before_any_optimizer_call_and_queues_each_output(tmp_path):
    transport = Transport()
    with Cyclotron.open(tmp_path / "c", definition(), Model(), OptimizerAgent(transport), max_requests=1000,
                        learning="deferred") as cyclotron:
        for index in range(12):
            checklist(cyclotron, index, OUTPUTS)
        assert transport.calls == 0
        assert cyclotron.pending_learning() == [spec.id for spec in OUTPUTS]
        passes = run(cyclotron.learn())
        assert cyclotron.pending_learning() == []
        recorded = {cid: [e for e in wheel.history(100000) if e["kind"] == "deferred-learning-completed"]
                    for cid, wheel in cyclotron.wheels.items()}
    assert [p["classifier"] for p in passes] == [spec.id for spec in OUTPUTS]
    assert transport.calls > 0
    assert all(len(events) == 1 for events in recorded.values())


def test_learning_after_each_review_matches_inline_learning(tmp_path):
    def runs(directory, learning):
        transport, calls = Transport(), []
        with Cyclotron.open(directory, definition(3), Model(), OptimizerAgent(transport, observer=calls.append),
                            max_requests=1000, learning=learning) as cyclotron:
            for index in range(12):
                checklist(cyclotron, index, OUTPUTS[:3])
                run(cyclotron.learn())
            return transport.calls, {cid: sum(e["kind"] == "optimization-stage-started" for e in wheel.history(100000))
                                     for cid, wheel in cyclotron.wheels.items()}
    inline, deferred = runs(tmp_path / "inline", "inline"), runs(tmp_path / "deferred", "deferred")
    assert inline == deferred and inline[0] > 0


def test_one_deferred_pass_still_runs_a_cadence_crossed_by_several_reviews(tmp_path):
    with Cyclotron.open(tmp_path / "c", definition(1, optimize_every=4, rubric_changes_every=1000), Model(),
                        OptimizerAgent(Transport()), max_requests=1000, learning="deferred") as cyclotron:
        for index in range(12):
            checklist(cyclotron, index, OUTPUTS[:1])
        run(cyclotron.learn())
        triggers = [e for e in cyclotron.wheels["relevant"].history(100000)
                    if e["kind"] == "trigger-evaluated" and e["stage"] == "classifier"]
    assert len(triggers) == 1
    assert triggers[0]["due"] and triggers[0]["details"]["since_count"] < triggers[0]["details"]["feedback_count"]


def test_a_review_waits_for_at_most_one_output_pass_while_learning_runs(tmp_path):
    with Cyclotron.open(tmp_path / "c", definition(), Model(), OptimizerAgent(Transport()), max_requests=1000,
                        learning="deferred") as cyclotron:
        for index in range(12):
            checklist(cyclotron, index, OUTPUTS)
        later = run(cyclotron.decide(item("later", "yes later")))
        original = cyclotron._learn

        async def provider_bound_learn(*args, **kwargs):
            await asyncio.sleep(0)  # an optimizer call that awaits its provider
            return await original(*args, **kwargs)

        cyclotron._learn = provider_bound_learn

        async def scenario():
            learning = asyncio.create_task(cyclotron.learn())
            await asyncio.sleep(0)
            await cyclotron.review(later.decision_id, labels={"relevant": "include"})
            waiting = cyclotron.pending_learning()
            await learning
            return waiting

        waiting = asyncio.run(scenario())
    # The review finished while most outputs were still queued.
    assert len(waiting) >= len(OUTPUTS) - 1


def test_the_optimizer_call_limit_is_shared_across_outputs_and_enforced(tmp_path):
    transport = Transport(max_calls=2)
    with Cyclotron.open(tmp_path / "c", definition(), Model(), OptimizerAgent(transport), max_requests=1000,
                        learning="deferred") as cyclotron:
        for index in range(12):
            checklist(cyclotron, index, OUTPUTS)
        passes = run(cyclotron.learn())
        paused = sum(e["kind"] == "optimization-paused" for wheel in cyclotron.wheels.values()
                     for e in wheel.history(100000))
    assert transport.calls == 2 and paused > 0
    assert any(w["reason"].startswith("optimizer call limit reached") for p in passes for w in p["warnings"])


def test_queued_learning_survives_a_restart(tmp_path):
    with Cyclotron.open(tmp_path / "c", definition(2), Model(), OptimizerAgent(Transport()), max_requests=1000,
                        learning="deferred") as cyclotron:
        checklist(cyclotron, 1, OUTPUTS[:2])
    with Cyclotron.open(tmp_path / "c", definition(2), Model(), OptimizerAgent(Transport()), max_requests=1000,
                        learning="deferred") as cyclotron:
        assert cyclotron.pending_learning() == ["relevant", "timely"]
        assert len(run(cyclotron.learn(limit=1))) == 1
        assert cyclotron.pending_learning() == ["timely"]


def test_an_unknown_learning_mode_is_refused(tmp_path):
    with pytest.raises(ValueError, match="learning"):
        Cyclotron.open(tmp_path / "c", definition(1), Model(), learning="later")

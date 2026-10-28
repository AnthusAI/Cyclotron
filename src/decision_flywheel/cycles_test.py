import asyncio

import pytest

from .classifier_config import ClassifierConfig
from .flywheel import DecisionFlywheel
from .flywheel_test import TASK, FakeModel, agent
from .models import Item


def test_a_cycle_links_predictions_and_trigger_checks_and_records_before_and_after_configuration(tmp_path):
    wheel = DecisionFlywheel(tmp_path / "runtime.sqlite3", ClassifierConfig(TASK), FakeModel(), agent([]))
    with wheel.cycle(Item("paper", {"text": "yes paper"})) as cycle:
        asyncio.run(wheel.predict(Item("paper", {"text": "yes paper"}), ()))
        cycle.check_trigger("rubric", due=False, reason="not enough new feedback", details={"count": 1, "threshold": 10})
    events = wheel.history()
    assert events[0]["kind"] == "cycle-started"
    assert events[-1]["kind"] == "cycle-completed"
    assert len({event["cycle_id"] for event in events}) == 1
    assert events[0]["item"]["id"] == "paper"
    assert events[0]["classifier_snapshot"] == events[-1]["classifier_snapshot"]
    assert next(e for e in events if e["kind"] == "prediction")["probabilities"]
    assert next(e for e in events if e["kind"] == "trigger-evaluated")["due"] is False
    wheel.close()


def test_cycles_do_not_leak_context_and_failures_close_the_cycle_without_hiding_the_error(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    wheel = DecisionFlywheel(path, ClassifierConfig(TASK), FakeModel(), agent([]))
    with pytest.raises(RuntimeError):
        with wheel.cycle(None):
            raise RuntimeError("failed work")
    assert wheel.history()[-1]["kind"] == "cycle-failed"
    wheel._emit({"kind": "outside"})
    assert "cycle_id" not in wheel.history()[-1]
    with wheel.cycle(None):
        pass
    assert wheel.history()[-1]["cycle_number"] == 2
    wheel.close()
    wheel = DecisionFlywheel(path, ClassifierConfig(TASK), FakeModel(), agent([]))
    with wheel.cycle(None):
        pass
    assert wheel.history()[-1]["cycle_number"] == 3
    wheel.close()


def test_an_unfinished_cycle_can_resume_after_restart_without_predicting_again(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    item = Item("paper", {"text": "yes paper"})
    wheel = DecisionFlywheel(path, ClassifierConfig(TASK), FakeModel(), agent([]))
    original = wheel.cycle(item).__enter__()
    asyncio.run(wheel.predict(item, ()))
    cycle_id = original.context["cycle_id"]
    wheel.close()
    reopened = DecisionFlywheel(path, ClassifierConfig(TASK), FakeModel(), agent([]))
    resumed = reopened.resume_cycle(item)
    assert resumed.context["cycle_id"] == cycle_id
    assert reopened.model.calls == 0
    resumed.__exit__(None, None, None)
    assert reopened.history()[-1]["kind"] == "cycle-completed"
    assert reopened.resume_cycle(item) is None
    reopened.close()


def test_a_waiting_review_survives_a_later_completed_optimization_cycle(tmp_path):
    item = Item("waiting-paper", {"text": "yes paper"})
    wheel = DecisionFlywheel(tmp_path / "runtime.sqlite3", ClassifierConfig(TASK), FakeModel(), agent([]))
    cycle = wheel.cycle(item).__enter__()
    asyncio.run(wheel.predict(item, ()))
    expected = cycle.context["cycle_id"]
    cycle.suspend()
    with wheel.cycle(Item("older-paper", {"text": "previously reviewed"}), reason="optimization-resume"):
        pass
    resumed = wheel.resume_cycle(item)
    assert resumed is not None
    assert resumed.context["cycle_id"] == expected
    assert wheel.model.calls == 1
    resumed.__exit__(None, None, None)
    assert wheel.resume_cycle(item) is None
    wheel.close()


def test_events_name_the_fit_proof_that_only_the_fit_records_in_full(tmp_path):
    from .flywheel_test import DEV, TRAIN

    wheel = DecisionFlywheel(tmp_path / "runtime.sqlite3", ClassifierConfig(TASK), FakeModel(), agent([]), max_requests=30)
    asyncio.run(wheel.improve(TRAIN, DEV, protected=(), propensities={r.item.id: 1.0 for r in TRAIN}))
    asyncio.run(wheel.step("classifier", TRAIN, DEV, protected=(), propensities={r.item.id: 1.0 for r in TRAIN}))
    with wheel.cycle(Item("paper", {"text": "yes paper"})):
        pass
    events = wheel.history(1000)
    activated = next(e for e in reversed(events) if e["kind"] == "classifier-activated")["classifier_snapshot"]
    fitted = next(
        e["head"]
        for e in events
        if e["kind"] == "fit-completed"
        and e["head"]["refitted_cyclotron_fingerprint"] == activated["head"]["refitted_cyclotron_fingerprint"]
    )
    full = fitted["out_of_fold"]
    assert full["fit_ids"] and full["normalizers"]
    for kind in ("classifier-activated", "step-completed", "cycle-started", "cycle-completed"):
        snapshot = next(e for e in reversed(events) if e["kind"] == kind)["classifier_snapshot"]
        compact = snapshot["head"]["out_of_fold"]
        assert not {"fit_ids", "fit_labels", "normalization_fit_ids", "normalizers"} & set(compact), kind
        assert compact["per_item_fit_proof"] == {
            "omitted": ["fit_ids", "fit_labels", "normalization_fit_ids", "normalizers"],
            "recorded_in": "fit-completed",
            "head_fingerprint": fitted["refitted_cyclotron_fingerprint"],
        }
        assert {key: value for key, value in full.items() if key in compact} == {
            key: value for key, value in compact.items() if key != "per_item_fit_proof"
        }
        assert snapshot["head"]["provenance"] == fitted["provenance"]
    wheel.close()


def test_the_unfinished_cycle_index_keeps_open_and_failed_cycles_and_drops_completed_ones():
    from .cycles import note_cycle_event, unfinished_cycles_from

    def event(kind, cycle_id, number, item="paper"):
        return {"kind": kind, "cycle_id": cycle_id, "cycle_number": number, "cycle_item_id": item, "reason": "item-processing"}

    events = [
        event("cycle-started", "a", 1),
        event("cycle-completed", "a", 1),
        event("cycle-started", "b", 2),
        event("cycle-failed", "b", 2),
        event("cycle-started", "c", 3),
        event("cycle-started", "x", 4, item="other"),
        {"kind": "prediction", "cycle_item_id": "paper"},
    ]
    assert unfinished_cycles_from(events, "paper") == (("b", 2, "item-processing", True), ("c", 3, "item-processing", False))
    index = {}
    for row in events:
        note_cycle_event(index, row)
    note_cycle_event(index, event("cycle-completed", "x", 4, item="other"))
    assert set(index) == {"paper"}


def test_resume_reattaches_the_latest_unfinished_cycle_and_a_failed_one_only_for_replay(tmp_path):
    wheel = DecisionFlywheel(tmp_path / "runtime.sqlite3", ClassifierConfig(TASK), FakeModel(), agent([]))
    paper = Item("paper", {"text": "yes paper"})
    with pytest.raises(RuntimeError):
        with wheel.cycle(paper):
            raise RuntimeError("interrupted")
    assert wheel.resume_cycle(paper) is None
    cycle = wheel.resume_cycle(paper, include_failed=True)
    assert cycle is not None and cycle.context["cycle_item_id"] == "paper"
    cycle.__exit__(None, None, None)
    assert wheel.resume_cycle(paper, include_failed=True) is None

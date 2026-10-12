"""A reviewer answers the whole checklist for an item and saves it as one review."""
import pytest

from .embedded_cyclotron import ClassifierSpec, Cyclotron, CyclotronDefinition
from .embedded_cyclotron_test import RELEVANT, Model, item, run
from .embedded_review_rate_test import FAST, Model as ConfidentModel
from .flywheel_test import agent

TIMELY = ClassifierSpec("timely", ("include", "exclude"), "Is this timely?", positive_label="include")
LOCAL = ClassifierSpec("local", ("include", "exclude"), "Is this local?", positive_label="include")
CHECKLIST = CyclotronDefinition("newsroom", (RELEVANT, TIMELY, LOCAL), seed="checklist-seed")
ALL = {"relevant": "include", "timely": "exclude", "local": "include"}


def review_events(cyclotron):
    return [e["review"] for e in cyclotron.subscribe(limit=1000)["events"] if e["kind"] == "review"]


def test_one_call_labels_every_output_with_one_review_event_each(tmp_path):
    with Cyclotron.open(tmp_path / "c", CHECKLIST, Model()) as cyclotron:
        decision = run(cyclotron.decide(item("ref-1")))
        reviews = run(cyclotron.review(decision.decision_id, labels=ALL, explanation="Local news, but stale",
                                       review_id="checklist-1"))
        events = review_events(cyclotron)
        pending = {cid: cyclotron.status(cid).pending.decisions_awaiting_review for cid in ALL}
        state = cyclotron.db.execute("SELECT state FROM decisions WHERE id=?", (decision.decision_id,)).fetchone()[0]
    assert {cid: review.label for cid, review in reviews.items()} == ALL
    assert {review.review_id for review in reviews.values()} == {f"checklist-1:{cid}" for cid in ALL}
    assert sorted(e["classifier"] for e in events) == sorted(ALL)
    assert all(e["explanation"] == "Local news, but stale" for e in events)
    assert pending == {cid: 0 for cid in ALL} and state == "reviewed"


def test_a_partial_checklist_leaves_the_unlabeled_output_open(tmp_path):
    with Cyclotron.open(tmp_path / "c", CHECKLIST, Model()) as cyclotron:
        decision = run(cyclotron.decide(item("ref-1")))
        reviews = run(cyclotron.review(decision.decision_id, labels={"relevant": "include", "timely": "exclude"}))
        pending = {cid: cyclotron.status(cid).pending.decisions_awaiting_review for cid in ALL}
        state = cyclotron.db.execute("SELECT state FROM decisions WHERE id=?", (decision.decision_id,)).fetchone()[0]
        run(cyclotron.review(decision.decision_id, "exclude", classifier="local"))
        later = cyclotron.db.execute("SELECT state FROM decisions WHERE id=?", (decision.decision_id,)).fetchone()[0]
    assert set(reviews) == {"relevant", "timely"}
    assert pending == {"relevant": 0, "timely": 0, "local": 1} and state == "open"
    assert later == "reviewed"


def test_a_checklist_counts_toward_the_window_of_each_labeled_output_only(tmp_path):
    with Cyclotron.open(tmp_path / "c", CHECKLIST, ConfidentModel(), review_program=FAST) as cyclotron:
        for index in range(10):
            decision = run(cyclotron.decide(item(f"i-{index}", f"sure {index}")))
            run(cyclotron.review(decision.decision_id, labels={"relevant": "include", "timely": "include"}))
        run(cyclotron.decide(item("next", "sure next")))
        check = cyclotron._program_state().last_check
    counted = {cid: evidence["labels"] for cid, evidence in check["outputs"].items()}
    assert counted["relevant"] == counted["timely"] > 0 and counted["local"] == 0
    assert check["output"] == "local" and not check["enough_evidence"]


def test_each_labeled_output_learns_once_and_no_optimizer_run_is_duplicated(tmp_path):
    definition = CyclotronDefinition("newsroom", (RELEVANT, TIMELY, LOCAL), seed="checklist-seed",
                                     rubric_changes_every=1)

    def optimizer_runs(directory, together):
        calls = []
        with Cyclotron.open(directory, definition, Model(), agent(calls), max_requests=500) as cyclotron:
            for index in range(12):
                text, label = ("yes", "include") if index % 2 else ("no", "exclude")
                decision = run(cyclotron.decide(item(f"learn-{index}", f"{text} {index}")))
                if together:
                    run(cyclotron.review(decision.decision_id, labels={cid: label for cid in ALL}))
                else:
                    for cid in ALL:
                        run(cyclotron.review(decision.decision_id, label, classifier=cid))
            per_output = {cid: (sum(e["kind"] == "optimization-stage-started" for e in wheel.history(100000)),
                                sum(e["kind"] == "trigger-evaluated" and e["stage"] == "rubric"
                                    for e in wheel.history(100000)))
                          for cid, wheel in cyclotron.wheels.items()}
            return per_output, len(calls)

    together = optimizer_runs(tmp_path / "together", True)
    separately = optimizer_runs(tmp_path / "separately", False)
    assert together == separately
    per_output, optimizer_events = together
    assert all(rubric_checks == 12 for _, rubric_checks in per_output.values())  # one learning pass per review
    assert all(stages > 0 for stages, _ in per_output.values()) and optimizer_events > 0


def test_resubmitting_a_checklist_is_safe_and_a_changed_one_is_refused(tmp_path):
    with Cyclotron.open(tmp_path / "c", CHECKLIST, Model()) as cyclotron:
        decision = run(cyclotron.decide(item("ref-1")))
        first = run(cyclotron.review(decision.decision_id, labels=ALL, review_id="r-1"))
        again = run(cyclotron.review(decision.decision_id, labels=ALL, review_id="r-1"))
        events = review_events(cyclotron)
        with pytest.raises(ValueError, match="different content"):
            run(cyclotron.review(decision.decision_id, labels={**ALL, "local": "exclude"}, review_id="r-1"))
    assert {cid: r.review_id for cid, r in first.items()} == {cid: r.review_id for cid, r in again.items()}
    assert len(events) == 3


@pytest.mark.parametrize("labels", [{"relevant": "include", "unknown": "include"},
                                    {"relevant": "include", "timely": "maybe"}, {}])
def test_an_invalid_checklist_records_nothing(tmp_path, labels):
    with Cyclotron.open(tmp_path / "c", CHECKLIST, Model()) as cyclotron:
        decision = run(cyclotron.decide(item("ref-1")))
        with pytest.raises(ValueError):
            run(cyclotron.review(decision.decision_id, labels=labels))
        assert review_events(cyclotron) == []


def test_a_checklist_can_close_one_output_without_a_label(tmp_path):
    with Cyclotron.open(tmp_path / "c", CHECKLIST, Model()) as cyclotron:
        decision = run(cyclotron.decide(item("ref-1")))
        reviews = run(cyclotron.review(decision.decision_id, labels={"relevant": "include", "timely": None}))
    assert reviews["relevant"].kind == "label" and reviews["timely"].kind == "no-label"


def test_labels_and_a_single_label_cannot_be_combined(tmp_path):
    with Cyclotron.open(tmp_path / "c", CHECKLIST, Model()) as cyclotron:
        decision = run(cyclotron.decide(item("ref-1")))
        with pytest.raises(ValueError, match="labels"):
            run(cyclotron.review(decision.decision_id, "include", labels=ALL))
        with pytest.raises(ValueError, match="labels"):
            run(cyclotron.review(decision.decision_id))

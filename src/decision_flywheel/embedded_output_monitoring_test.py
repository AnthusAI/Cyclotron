"""The review-rate program watches every output, not just the first one."""
import json

from .classifier_config import ClassifierConfig
from .embedded_cyclotron import ClassifierSpec, Cyclotron, CyclotronDefinition
from .embedded_cyclotron_test import DEFINITION, RELEVANT, item, run
from .embedded_review_rate_test import FAST, Model, decide_and_review, rate_events
from .flywheel import FittedClassifier

TIMELY = ClassifierSpec("timely", ("include", "exclude"), "Is this timely?", positive_label="include")
TWO = CyclotronDefinition("newsroom", (RELEVANT, TIMELY), seed="monitor-seed")


def decide_and_review_both(cyclotron, start, count, timely_label="include"):
    for index in range(start, start + count):
        decision = run(cyclotron.decide(item(f"i-{index}", f"sure {index}")))
        # Each output is reviewed when it is requested (outputs are routed separately).
        if decision.review.for_output("relevant").selected:
            run(cyclotron.review(decision.decision_id, "include", classifier="relevant"))
        if decision.review.for_output("timely").selected:
            run(cyclotron.review(decision.decision_id, timely_label, classifier="timely"))


def test_when_the_second_output_drifts_the_rate_rises_and_the_event_names_it(tmp_path):
    with Cyclotron.open(tmp_path / "c", TWO, Model(), review_program=FAST) as cyclotron:
        decide_and_review_both(cyclotron, 0, 20)
        decide_and_review_both(cyclotron, 20, 10, timely_label="exclude")  # only timely's reviewers disagree
        run(cyclotron.decide(item("next", "sure next")))
        events = rate_events(cyclotron)
        overview = cyclotron.overview()
    assert [e["state"] for e in events] == ["tapering", "raised"]
    raised = events[-1]
    assert raised["output"] == "timely" and "for timely" in raised["reason"]
    relevant, timely = raised["evidence"]["outputs"]["relevant"], raised["evidence"]["outputs"]["timely"]
    assert relevant["accuracy"] == 1.0 and timely["enough_evidence"] and not timely["met"]
    assert overview.review_rate.state == "raised" and overview.limiting == "timely"


def test_every_output_must_meet_the_target_before_the_rate_steps_down(tmp_path):
    with Cyclotron.open(tmp_path / "c", TWO, Model(), review_program=FAST) as cyclotron:
        for index in range(20):
            decision = run(cyclotron.decide(item(f"i-{index}", f"sure {index}")))
            run(cyclotron.review(decision.decision_id, "include", classifier="relevant"))  # timely never reviewed
        run(cyclotron.decide(item("next", "sure next")))
        rate = cyclotron.overview().review_rate
    assert rate.rate == 1.0 and "for timely" in rate.reason


def test_a_new_version_of_any_output_raises_the_rate_and_names_it(tmp_path):
    with Cyclotron.open(tmp_path / "c", TWO, Model(), review_program=FAST) as cyclotron:
        decide_and_review_both(cyclotron, 0, 20)
        run(cyclotron.decide(item("probe", "sure probe")))
        assert cyclotron.overview().review_rate.rate == .5
        wheel = cyclotron.wheels["timely"]
        wheel._activate(FittedClassifier(ClassifierConfig(wheel.initial.task, rubric="Prefer this week's news.")))
        decision = run(cyclotron.decide(item("after", "sure after")))
        raised = rate_events(cyclotron)[-1]
    assert decision.review.selected and decision.results["timely"].version == 2
    assert raised["output"] == "timely" and "version 2 of timely was promoted" in raised["reason"]


def test_the_overview_lists_every_output_and_names_the_worst(tmp_path):
    with Cyclotron.open(tmp_path / "c", TWO, Model(), review_program=FAST) as cyclotron:
        decide_and_review_both(cyclotron, 0, 6, timely_label="exclude")
        overview = cyclotron.overview()
    assert [status.classifier for status in overview.outputs] == ["relevant", "timely"]
    assert overview.worst == "timely"
    wire = overview.to_json()
    assert wire["worstOutput"] == "timely" and [s["cyclotron"]["classifier"] for s in wire["outputs"]] == ["relevant", "timely"]


def test_a_single_output_store_keeps_its_program_state_and_evidence_shape(tmp_path):
    with Cyclotron.open(tmp_path / "c", DEFINITION, Model(), review_program=FAST) as cyclotron:
        decide_and_review(cyclotron, 0, 20)
        run(cyclotron.decide(item("probe", "sure probe")))
        state = json.loads(cyclotron.db.execute(
            "SELECT value FROM meta WHERE key='review_program_state'").fetchone()[0])
        events = rate_events(cyclotron)
        overview = cyclotron.overview()
    assert set(state) == {"index", "raised", "consecutive", "checked_windows", "version", "reason",
                          "last_check", "override"}
    assert "output" not in state["last_check"] and "output" not in events[-1]
    assert overview.limiting is None and [s.classifier for s in overview.outputs] == ["relevant"]

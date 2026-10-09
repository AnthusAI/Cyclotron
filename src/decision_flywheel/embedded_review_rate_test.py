"""The cyclotron decides how many items people review, and says why."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from . import embedded_cyclotron
from .batched_classification import BatchedAnswers
from .classifier_config import ClassifierConfig
from .embedded_cyclotron import Cyclotron
from .embedded_cyclotron_test import DEFINITION, item, run
from .flywheel import FittedClassifier
from .models import DecisionResult
from .review_program import ReviewProgram

FAST = ReviewProgram(window=10, min_window_labels=5, max_gap_points=100)


class Model:
    """'low' items get 60% confidence; everything else 90%."""
    model_identity = "scripted-confidence"

    def __init__(self):
        self.calls = 0

    async def classify_many(self, configs, target, training, **kwargs):
        self.calls += 1
        p = .6 if "low" in target.values["text"] else .9
        return BatchedAnswers({cid: {"decision": DecisionResult("include", {"include": p, "exclude": 1 - p})}
                               for cid in configs}, "scripted", {}, 1)


def decide_and_review(cyclotron, start, count, label="include", text="sure"):
    decisions = []
    for index in range(start, start + count):
        decision = run(cyclotron.decide(item(f"i-{index}", f"{text} {index}")))
        decisions.append(decision)
        if decision.review.selected:
            run(cyclotron.review(decision.decision_id, label))
    return decisions


def rate_events(cyclotron):
    return [e for e in cyclotron.subscribe(limit=1000)["events"] if e["kind"] == "review-rate-changed"]


def test_onboarding_reviews_everything_and_says_what_comes_next(tmp_path):
    with Cyclotron.open(tmp_path / "c", DEFINITION, Model(), review_program=FAST) as cyclotron:
        decisions = decide_and_review(cyclotron, 0, 3)
        rate = cyclotron.status().to_json()["reviewRate"]
    assert all(d.review.selected and d.review.reason in ("program", "audit") for d in decisions)
    assert all(d.review.propensity == 1.0 and d.review.detail for d in decisions)
    assert rate["state"] == "onboarding" and rate["rate"] == 1.0 and rate["auditFloor"] == .05
    assert rate["nextCheckAfterDecisions"] == 7
    assert rate["nextStep"].startswith("Steps down to 50% after 2 more windows of 10 decisions")


def test_two_good_windows_step_the_rate_down_and_confident_decisions_are_sampled(tmp_path):
    with Cyclotron.open(tmp_path / "c", DEFINITION, Model(), review_program=FAST) as cyclotron:
        decide_and_review(cyclotron, 0, 20)
        later = decide_and_review(cyclotron, 20, 60)
        rate = cyclotron.status().to_json()["reviewRate"]
        events = rate_events(cyclotron)
    assert events[0]["state"] == "tapering" and events[0]["rate"] == .5
    assert events[0]["evidence"]["accuracy"] == 1.0 and events[0]["window"] == 2
    assert rate["state"] in ("tapering", "steady") and rate["rate"] < 1.0
    unselected = [d for d in later if not d.review.selected]
    assert unselected and all(d.review.reason is None and d.review.propensity < 1 for d in unselected)
    assert {d.review.reason for d in later if d.review.selected} <= {"program", "audit"}


def test_low_confidence_is_reviewed_at_every_rate(tmp_path):
    with Cyclotron.open(tmp_path / "c", DEFINITION, Model(), review_program=FAST) as cyclotron:
        decide_and_review(cyclotron, 0, 40)
        low = run(cyclotron.decide(item("unsure", "low confidence")))
    assert low.review.selected and low.review.propensity == 1.0 and "below the 70% threshold" in low.review.detail


def test_unselected_decisions_are_not_pending_and_a_reviewer_may_still_review_them(tmp_path):
    with Cyclotron.open(tmp_path / "c", DEFINITION, Model(), review_program=FAST) as cyclotron:
        cyclotron.set_review_rate(0., set_by="managing-editor")
        decisions = [run(cyclotron.decide(item(f"i-{i}", f"sure {i}"))) for i in range(40)]
        skipped = next(d for d in decisions if not d.review.selected)
        selected = [d for d in decisions if d.review.selected]
        assert {d.review.reason for d in selected} == {"audit"}
        status = cyclotron.status()
        assert status.pending.decisions_awaiting_review == len(selected)
        with pytest.raises(ValueError, match="not sent to review"):
            run(cyclotron.review(skipped.decision_id, "include", selected_by="program"))
        review = run(cyclotron.review(skipped.decision_id, "exclude"))
        assert review.selected_by == "reviewer"
        assert cyclotron.status().alignment.labels == 0


def test_a_failed_window_after_stepping_down_goes_back_to_full_review(tmp_path):
    with Cyclotron.open(tmp_path / "c", DEFINITION, Model(), review_program=FAST) as cyclotron:
        decide_and_review(cyclotron, 0, 20)
        decide_and_review(cyclotron, 20, 10, label="exclude")  # reviewers now disagree
        run(cyclotron.decide(item("next", "sure next")))
        events = rate_events(cyclotron)
        rate = cyclotron.status().to_json()["reviewRate"]
    assert [e["state"] for e in events] == ["tapering", "raised"]
    assert rate["state"] == "raised" and rate["rate"] == 1.0 and "below the 85% target" in rate["reason"]


def test_a_large_calibration_gap_prevents_stepping_down(tmp_path):
    strict = replace(FAST, max_gap_points=5)  # says 90% sure but every label agrees: a 10-point gap
    with Cyclotron.open(tmp_path / "c", DEFINITION, Model(), review_program=strict) as cyclotron:
        decide_and_review(cyclotron, 0, 30)
        run(cyclotron.decide(item("next", "sure next")))
        rate = cyclotron.status().to_json()["reviewRate"]
    assert rate["state"] == "onboarding" and rate["rate"] == 1.0
    assert "calibration gap was 10 points" in rate["reason"]


def test_a_promoted_version_goes_back_to_full_review(tmp_path):
    with Cyclotron.open(tmp_path / "c", DEFINITION, Model(), review_program=FAST) as cyclotron:
        decide_and_review(cyclotron, 0, 20)
        run(cyclotron.decide(item("probe", "sure probe")))
        assert cyclotron.status().review_rate.rate == .5
        wheel = cyclotron.wheels["relevant"]
        wheel._activate(FittedClassifier(ClassifierConfig(wheel.initial.task, rubric="Prefer primary sources.")))
        decision = run(cyclotron.decide(item("after", "sure after")))
        rate = cyclotron.status().to_json()["reviewRate"]
    assert decision.version == 2 and decision.review.selected
    assert rate["state"] == "raised" and "version 2 was promoted" in rate["reason"]


def test_an_override_reports_who_set_it_and_ends_at_its_expiry(tmp_path, monkeypatch):
    with Cyclotron.open(tmp_path / "c", DEFINITION, Model(), review_program=FAST) as cyclotron:
        expires = datetime.now(timezone.utc) + timedelta(days=1)
        cyclotron.set_review_rate(.25, set_by="managing-editor", expires_at=expires)
        rate = cyclotron.status().to_json()["reviewRate"]
        assert rate["state"] == "manual" and rate["rate"] == .25
        assert rate["override"]["setBy"] == "managing-editor" and rate["override"]["expiresAt"] == expires.isoformat()
        assert "Manual rate of 25% set by managing-editor" in rate["reason"]

        real = datetime

        class Later(real):
            @classmethod
            def now(cls, tz=None):
                return real.now(tz) + timedelta(days=2)
        monkeypatch.setattr(embedded_cyclotron, "datetime", Later)
        run(cyclotron.decide(item("later", "sure later")))
        rate = cyclotron.status().to_json()["reviewRate"]
        assert rate["state"] == "onboarding" and rate["override"] is None
        assert "expired" in rate_events(cyclotron)[-1]["reason"]
        with pytest.raises(ValueError):
            cyclotron.set_review_rate(.5, set_by="")


def test_a_cyclotron_without_a_program_reviews_everything(tmp_path):
    with Cyclotron.open(tmp_path / "c", DEFINITION, Model(), review_program=None) as cyclotron:
        decisions = decide_and_review(cyclotron, 0, 25)
        assert cyclotron.status().review_rate.state == "full"
    assert all(d.review.selected for d in decisions)


def test_selection_propensities_reach_the_feedback_used_for_fitting(tmp_path):
    with Cyclotron.open(tmp_path / "c", DEFINITION, Model(), review_program=FAST) as cyclotron:
        decisions = decide_and_review(cyclotron, 0, 60)
        sampled = next(d for d in decisions[20:] if d.review.selected and d.review.propensity < 1)
        feedback = next(e["feedback"] for e in cyclotron.wheels["relevant"].history(100000)
                        if e["kind"] == "human-feedback" and e["feedback"]["item_id"] == sampled.item_id)
    assert feedback["selection_propensity"] == sampled.review.propensity


def test_a_shown_decision_is_stable_through_a_refit_and_changes_with_a_new_version(tmp_path):
    model = Model()
    with Cyclotron.open(tmp_path / "c", DEFINITION, model, review_program=FAST) as cyclotron:
        shown = run(cyclotron.decide(item("shown", "sure shown")))
        wheel = cyclotron.wheels["relevant"]
        wheel._activate(replace(wheel.active, validation_status="spec-refit"))  # an ML model refit
        assert run(cyclotron.decide(item("shown", "sure shown"))) == shown
        assert cyclotron.status().refits == 1 and cyclotron.status().version == 1
        wheel._activate(FittedClassifier(ClassifierConfig(wheel.initial.task, rubric="Prefer primary sources.")))
        changed = run(cyclotron.decide(item("shown", "sure shown")))
    assert changed.decision_id != shown.decision_id and changed.version == 2
